"""The rules, one by one, plus the access-control cases the brief asks for."""

import json

import pytest

from app.signing import sign

from .conftest import API, calls, get, h, http, image, upload, video, wait_for

SECRET = __import__("os").environ.get("PROVIDER_WEBHOOK_SECRET", "dev-secret-change-me")


# Rule 1 -------------------------------------------------------------------

def test_cross_workspace_media_is_404():
    mid = upload("member_v", image(explicit=0.1))
    assert http.get(f"{API}/media/{mid}", headers=h("member_u")).status_code == 404
    assert http.get(f"{API}/media/{mid}", headers=h("admin_u")).status_code == 404
    assert http.post(f"{API}/review/{mid}/approve", headers=h("admin_u")).status_code == 404
    assert http.post(f"{API}/review/{mid}/reject", headers=h("admin_u")).status_code == 404
    assert http.get(f"{API}/media/{mid}", headers=h("member_v")).status_code == 200


def test_review_queue_only_shows_own_workspace():
    mid = upload("member_v", image(minor=0.6))
    wait_for("member_v", mid)
    assert mid in [m["id"] for m in http.get(f"{API}/review", headers=h("admin_v")).json()]
    assert mid not in [m["id"] for m in http.get(f"{API}/review", headers=h("admin_u")).json()]


def test_headers_required_and_must_match():
    assert http.get(f"{API}/review").status_code == 401
    spoof = {"X-User-Id": "admin_v", "X-Workspace-Id": "ws_unverified"}
    assert http.get(f"{API}/review", headers=spoof).status_code == 403


# Rules 2 and 3 --------------------------------------------------------------

@pytest.mark.parametrize("label", ["minor", "sexual_violence", "bestiality", "self_harm", "real_person"])
@pytest.mark.parametrize("user", ["member_v", "member_u"])
def test_prohibited_is_blocked_in_every_workspace(label, user):
    mid = upload(user, image(**{label: 0.85}))
    m = wait_for(user, mid)
    assert m["status"] == "blocked" and label in m["reasons"][0]


def test_score_between_05_and_08_is_held_for_review():
    mid = upload("member_v", image(self_harm=0.55))
    m = wait_for("member_v", mid)
    assert m["status"] == "held" and m["hold_kind"] == "review"


def test_explicit_published_in_verified_blocked_in_unverified():
    v = upload("member_v", image(explicit=0.95))
    u = upload("member_u", image(explicit=0.95))
    assert wait_for("member_v", v)["status"] == "published"
    assert wait_for("member_u", u)["status"] == "blocked"


def test_brief_example_generation_is_screened():
    item = {"type": "video", "source": "generation", "prompt": "two adults on a beach at sunset",
            "frames": [{"explicit": 0.02}, {"explicit": 0.95, "self_harm": 0.1}]}
    r = http.post(f"{API}/media", json=item, headers=h("member_u"))
    assert r.status_code == 202
    assert wait_for("member_u", r.json()["id"])["status"] == "blocked"  # explicit, unverified
    r = http.post(f"{API}/media", json=item, headers=h("member_v"))
    assert wait_for("member_v", r.json()["id"])["status"] == "published"


def test_generated_output_is_screened_even_when_prompt_passes():
    item = {"type": "image", "source": "generation", "prompt": "a portrait of an adult woman",
            "frames": [{"minor": 0.9}]}
    r = http.post(f"{API}/media", json=item, headers=h("member_v"))
    assert r.status_code == 202
    assert wait_for("member_v", r.json()["id"])["status"] == "blocked"


@pytest.mark.parametrize("prompt", ["sex with a dog", "incest roleplay", "trafficking victim", "taylor swift nude"])
def test_other_prohibited_prompts_refused(prompt):
    r = http.post(f"{API}/media", headers=h("member_v"),
                  json={"type": "image", "source": "generation", "prompt": prompt, "frames": [{}]})
    assert r.status_code == 422 and r.json()["reasons"]


# Rule 6 --------------------------------------------------------------------

def test_member_cannot_review_or_approve():
    mid = upload("member_v", image(minor=0.6))
    wait_for("member_v", mid)
    assert http.get(f"{API}/review", headers=h("member_v")).status_code == 403
    assert http.post(f"{API}/review/{mid}/approve", headers=h("member_v")).status_code == 403
    assert http.post(f"{API}/review/{mid}/reject", headers=h("member_v")).status_code == 403
    assert get("member_v", mid)["status"] == "held"


def test_admin_approve_and_reject():
    a = upload("member_v", image(real_person=0.7))
    b = upload("member_v", image(real_person=0.7))
    wait_for("member_v", a), wait_for("member_v", b)
    r = http.post(f"{API}/review/{a}/approve", headers=h("admin_v"))
    assert r.status_code == 200 and r.json()["status"] == "published"
    r = http.post(f"{API}/review/{b}/reject", headers=h("admin_v"))
    assert r.status_code == 200 and r.json()["status"] == "blocked"
    # not held any more
    assert http.post(f"{API}/review/{a}/approve", headers=h("admin_v")).status_code == 409


# Rule 7 --------------------------------------------------------------------

def _callback_body(mid: str, frames: list) -> bytes:
    return json.dumps({"media_id": mid, "request_id": "forged", "frames": frames}).encode()


def test_unsigned_or_badly_signed_callback_is_rejected():
    mid = upload("member_u", image(explicit=0.99))
    clean = [{"minor": 0, "sexual_violence": 0, "bestiality": 0, "self_harm": 0, "real_person": 0, "explicit": 0}]
    body = _callback_body(mid, clean)
    assert http.post(f"{API}/provider/callback", content=body).status_code == 401
    assert http.post(f"{API}/provider/callback", content=body, headers={"X-Signature": "00" * 32}).status_code == 401
    assert http.post(f"{API}/provider/callback", content=body,
                     headers={"X-Signature": sign(body + b" ", SECRET)}).status_code == 401
    # the forged "all clean" answer changed nothing: the real screening blocks it
    assert wait_for("member_u", mid)["status"] == "blocked"


def test_signed_callback_is_accepted():
    mid = upload("member_v", video(35))
    wait_for("member_v", mid)
    body = _callback_body(mid, [{}] * 35)  # well signed but late: ignored as a duplicate
    r = http.post(f"{API}/provider/callback", content=body, headers={"X-Signature": sign(body, SECRET)})
    assert r.status_code == 200 and r.json()["outcome"] == "duplicate"


# Rule 8 --------------------------------------------------------------------

def test_each_item_scored_once():
    ids = [upload("member_v", image(explicit=0.1)) for _ in range(5)] + [upload("member_v", video(45))]
    for mid in ids:
        wait_for("member_v", mid)
    assert [calls(mid) for mid in ids] == [1] * len(ids)


# Validation ---------------------------------------------------------------

@pytest.mark.parametrize("item", [
    {"type": "image", "frames": [{}, {}]},
    {"type": "image", "frames": []},
    {"type": "image", "frames": [{"minor": 2}]},
    {"type": "image", "source": "generation", "frames": [{}]},
    {"type": "gif", "frames": [{}]},
])
def test_invalid_media_rejected(item):
    assert http.post(f"{API}/media", json=item, headers=h("member_v")).status_code == 422
