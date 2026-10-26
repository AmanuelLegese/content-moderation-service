"""Integration tests run against the live stack (api, worker, provider, postgres, redis):
    docker compose run --rm tests
They talk to the service exactly as an external checker would: over HTTP, with the
X-User-Id / X-Workspace-Id headers, and through the provider's /_control and /_stats."""

import os
import time

import httpx
import pytest

API = os.environ.get("API_URL", "http://localhost:8000")
PROVIDER = os.environ.get("PROVIDER_URL", "http://localhost:8001")

USERS = {
    "admin_v": "ws_verified", "member_v": "ws_verified",
    "admin_u": "ws_unverified", "member_u": "ws_unverified",
}
FINAL = ("published", "blocked")

http = httpx.Client(timeout=30)


def h(user: str) -> dict:
    return {"X-User-Id": user, "X-Workspace-Id": USERS[user]}


def upload(user: str, item: dict) -> str:
    r = http.post(f"{API}/media", json=item, headers=h(user))
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "pending"
    return r.json()["id"]


def get(user: str, media_id: str) -> dict:
    r = http.get(f"{API}/media/{media_id}", headers=h(user))
    assert r.status_code == 200, r.text
    return r.json()


def image(**scores) -> dict:
    return {"type": "image", "source": "upload", "frames": [scores]}


def video(n: int, at: dict[int, dict] | None = None) -> dict:
    frames = [{"explicit": 0.01} for _ in range(n)]
    for i, s in (at or {}).items():
        frames[i] = s
    return {"type": "video", "source": "upload", "frames": frames}


def is_decided(m: dict) -> bool:
    """A real decision: published/blocked, or held *for review* after screening.
    'held' because the provider was down is not a decision."""
    return m["status"] in FINAL or (m["status"] == "held" and m["hold_kind"] == "review")


def wait_for(user: str, media_id: str, pred=is_decided, timeout: float = 30, forbid=None) -> dict:
    """Poll until pred(media). If forbid(media) is ever true, fail immediately: this is
    how tests assert that something *never* happens while waiting (e.g. published while unchecked)."""
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        m = get(user, media_id)
        if not seen or seen[-1] != m["status"]:
            seen.append(m["status"])
        if forbid and forbid(m):
            pytest.fail(f"forbidden state reached: {m} (status history {seen})")
        if pred(m):
            return m
        time.sleep(0.25)
    pytest.fail(f"timed out after {timeout}s waiting on {media_id}; status history {seen}; last {m}")


def provider(mode: str = "ok", delay_ms: int = 0, **extra) -> None:
    r = http.post(f"{PROVIDER}/_control", json={"mode": mode, "delay_ms": delay_ms, **extra})
    assert r.status_code == 200, r.text


def stats() -> dict:
    return http.get(f"{PROVIDER}/_stats").json()


def calls(media_id: str) -> int:
    return stats()["calls"].get(media_id, 0)


def scored(media_id: str) -> int:
    return stats().get("scored", {}).get(media_id, 0)


def wait_breaker_not_open(timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        b = http.get(f"{API}/health").json().get("breaker", "closed")
        if b != "open":
            return
        time.sleep(0.5)


@pytest.fixture(autouse=True)
def healthy_provider():
    provider("ok")
    wait_breaker_not_open()
    yield
    provider("ok")
