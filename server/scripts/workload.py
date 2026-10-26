"""Repeatable workload: ws_verified uploads a burst of 200 videos while ws_unverified
uploads one image per second. The provider goes down part-way through and comes back.

    uv run python scripts/workload.py --label fixed --out ../evidence/workload-fixed.json

Every media item is generated with a known ground truth (computed with app.policy over
*all* frames), so the report counts unsafe media that went live, not just job states.
"""

import argparse
import json
import random
import sys
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.policy import LABELS, decide  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--api", default="http://localhost:8000")
p.add_argument("--provider", default="http://localhost:8001")
p.add_argument("--label", required=True)
p.add_argument("--burst", type=int, default=200)
p.add_argument("--outage-at", type=float, default=10)
p.add_argument("--outage-for", type=float, default=40)
p.add_argument("--outage-mode", default="timeout")
p.add_argument("--images-for", type=float, default=80, help="seconds of single-image uploads")
p.add_argument("--drain", type=float, default=180, help="max seconds to wait for decisions at the end")
p.add_argument("--seed", type=int, default=7)
p.add_argument("--out")
p.add_argument("--resummarize", help="recompute the summary of a saved result file and rewrite it")
args = p.parse_args()

rng = random.Random(args.seed)
http = httpx.Client(timeout=30)
H = {"ws_verified": {"X-User-Id": "member_v", "X-Workspace-Id": "ws_verified"},
     "ws_unverified": {"X-User-Id": "member_u", "X-Workspace-Id": "ws_unverified"}}
VERIFIED = {"ws_verified": True, "ws_unverified": False}

items: dict[str, dict] = {}  # media id -> info
lock = threading.Lock()
t0 = time.time()


def now() -> float:
    return round(time.time() - t0, 2)


def make_video() -> dict:
    n = rng.choice([10, 20, 25, 40, 60])
    frames = [{"explicit": round(rng.random() * 0.3, 2)} for _ in range(n)]
    kind = rng.random()
    if kind < 0.15:    # prohibited content near the end
        frames[-1 - rng.randrange(3)][rng.choice(LABELS[:5])] = 0.9
    elif kind < 0.25:  # borderline: needs a human
        frames[rng.randrange(n)]["self_harm"] = 0.6
    elif kind < 0.6:   # explicit, in the last part of the video
        frames[-1]["explicit"] = 0.95
    return {"type": "video", "source": "upload", "frames": frames}


def make_image() -> dict:
    r = rng.random()
    f = {"explicit": 0.95} if r < 0.4 else {"minor": 0.85} if r < 0.5 else {"explicit": 0.1}
    return {"type": "image", "source": "upload", "frames": [f]}


def expected(item: dict, ws: str) -> str:
    full = [{label: f.get(label, 0.0) for label in LABELS} for f in item["frames"]]
    return decide(full, VERIFIED[ws]).status


def submit(ws: str, item: dict) -> None:
    r = http.post(f"{args.api}/media", json=item, headers=H[ws])
    r.raise_for_status()
    with lock:
        items[r.json()["id"]] = {"ws": ws, "type": item["type"], "frames": len(item["frames"]),
                                 "expected": expected(item, ws), "uploaded_at": now(), "history": []}


def burst() -> None:
    for _ in range(args.burst):
        submit("ws_verified", make_video())


def trickle() -> None:
    end = time.time() + args.images_for
    while time.time() < end:
        submit("ws_unverified", make_image())
        time.sleep(1)


def is_decided(m: dict) -> bool:
    return m["status"] in ("published", "blocked") or (m["status"] == "held" and m.get("hold_kind") == "review")


def poll(stop: threading.Event, samples: list) -> None:
    while not stop.is_set():
        with lock:
            ids = list(items)
        for mid in ids:
            info = items[mid]
            if info.get("decided_at") is not None:
                continue
            m = http.get(f"{args.api}/media/{mid}", headers=H[info["ws"]]).json()
            if not info["history"] or info["history"][-1][1] != m["status"]:
                info["history"].append((now(), m["status"]))
            if m["status"] == "published" and info["expected"] != "published":
                info["unsafe_published_at"] = now()
            if is_decided(m):
                info["decided_at"], info["final"] = now(), m["status"]
        met = http.get(f"{args.api}/metrics", params={"since": t0}).json()
        samples.append({"t": now(), **{k: met[k] for k in ("oldest_waiting_job_s", "oldest_undecided_media_s",
                                                            "pending", "held_no_decision", "published_unchecked")}})
        stop.wait(1.0)


def outage() -> None:
    time.sleep(args.outage_at)
    http.post(f"{args.provider}/_control", json={"mode": args.outage_mode, "delay_ms": 0})
    print(f"[{now()}] provider -> {args.outage_mode}", flush=True)
    time.sleep(args.outage_for)
    http.post(f"{args.provider}/_control", json={"mode": "ok", "delay_ms": 0})
    print(f"[{now()}] provider -> ok", flush=True)


def main() -> None:
    http.post(f"{args.provider}/_reset")
    stop, samples = threading.Event(), []
    threads = [threading.Thread(target=f) for f in (burst, trickle, outage)]
    poller = threading.Thread(target=poll, args=(stop, samples))
    for t in threads + [poller]:
        t.start()
    for t in threads:
        t.join()
    print(f"[{now()}] uploads done ({len(items)}), draining", flush=True)
    deadline = time.time() + args.drain
    while time.time() < deadline and any(i.get("decided_at") is None for i in items.values()):
        time.sleep(2)
    stop.set()
    poller.join()

    calls = http.get(f"{args.provider}/_stats").json()["calls"]
    met = http.get(f"{args.api}/metrics", params={"since": t0}).json()
    report = summarize(items, samples, calls, met)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1, default=str))


def summarize(items, samples, calls, met) -> dict:
    outage_start, outage_end = args.outage_at, args.outage_at + args.outage_for

    def phase(u):
        return "before" if u < outage_start else "during" if u < outage_end else "after"

    images = [i for i in items.values() if i["type"] == "image"]
    by_phase = {}
    for ph in ("before", "during", "after"):
        lat = sorted(i["decided_at"] - i["uploaded_at"] for i in images
                     if phase(i["uploaded_at"]) == ph and i.get("decided_at") is not None)
        # time until the item first left 'pending' (held or decided): how quickly the system responded
        first_change = sorted(next(t for t, s in i["history"] if s != "pending") - i["uploaded_at"] for i in images
                              if phase(i["uploaded_at"]) == ph and any(s != "pending" for _, s in i["history"]))
        by_phase[ph] = {
            "images": sum(1 for i in images if phase(i["uploaded_at"]) == ph),
            "decided": len(lat),
            "p50_decision_s": round(lat[len(lat) // 2], 1) if lat else None,
            "max_decision_s": round(lat[-1], 1) if lat else None,
            "p50_left_pending_s": round(first_change[len(first_change) // 2], 1) if first_change else None,
        }

    report = {
        "label": args.label,
        "uploads": len(items),
        "media_published_unchecked": met["published_unchecked"],
        "unsafe_published_ever": sum(1 for i in items.values() if "unsafe_published_at" in i),
        "wrong_final_decision": sum(1 for i in items.values() if i.get("final") and i["final"] != i["expected"]),
        "undecided_at_end": sum(1 for i in items.values() if i.get("decided_at") is None),
        "provider_calls_total": sum(calls.get(m, 0) for m in items),
        "provider_calls_per_upload": round(sum(calls.get(m, 0) for m in items) / max(1, len(items)), 2),
        "uploads_called_more_than_once": sum(1 for m in items if calls.get(m, 0) > 1),
        "max_oldest_waiting_job_s": round(max((s["oldest_waiting_job_s"] for s in samples), default=0), 1),
        "max_oldest_undecided_media_s": round(max((s["oldest_undecided_media_s"] for s in samples), default=0), 1),
        "ws_unverified_images_by_phase": by_phase,
        "final_metrics": met,
        "samples": samples,
        "items": items,
    }
    report["calls"] = calls
    summary = {k: v for k, v in report.items() if k not in ("samples", "items", "final_metrics", "calls")}
    print(json.dumps(summary, indent=2))
    return report


if __name__ == "__main__":
    if args.resummarize:
        saved = json.loads(Path(args.resummarize).read_text())
        items = {k: {**v, "history": [tuple(h) for h in v["history"]]} for k, v in saved["items"].items()}
        report = summarize(items, saved["samples"], saved["calls"], saved["final_metrics"])
        Path(args.resummarize).write_text(json.dumps(report, indent=1, default=str))
    else:
        main()
