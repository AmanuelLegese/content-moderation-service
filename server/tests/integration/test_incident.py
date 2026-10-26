"""One test per symptom of the Tuesday incident. Run against HANDLER=broken they fail
(see evidence/1-broken-tests.txt); against the fixed handler they pass."""

import time

import pytest

from .conftest import (API, calls, get, h, http, image, is_decided, provider, scored, stats, upload, video,
                       wait_for)

pytestmark = pytest.mark.incident


def published(m):
    return m["status"] == "published"


@pytest.mark.slow
def test_provider_timeout_never_publishes_and_recovers_without_a_human():
    """Symptom: uploads went live unchecked during the outage."""
    provider("timeout")
    mid = upload("member_u", image(explicit=0.01))
    held = wait_for("member_u", mid, lambda m: m["status"] == "held", timeout=20, forbid=published)
    assert held["hold_kind"] == "provider_unavailable"
    assert not held["screened"]
    # stays held (never published) for the rest of the outage
    t_end = time.time() + 8
    while time.time() < t_end:
        assert get("member_u", mid)["status"] == "held"
        time.sleep(0.5)

    provider("ok")
    m = wait_for("member_u", mid, is_decided, timeout=45)
    assert m["status"] == "published" and m["screened"]
    assert scored(mid) == 1


def test_explicit_in_last_frames_of_a_video_is_blocked_in_unverified_workspace():
    """Symptom: a video with explicit content in its last minute was published (only 8 frames were scored)."""
    mid = upload("member_u", video(20, {19: {"explicit": 0.95}}))
    m = wait_for("member_u", mid, forbid=published)
    assert m["status"] == "blocked"
    assert "explicit" in m["reasons"][0]


def test_long_async_video_every_frame_counts():
    """Same symptom on the async path (>30 frames, scored via signed callback): a 2-minute video,
    explicit only in the last minute."""
    mid = upload("member_u", video(120, {i: {"explicit": 0.9} for i in range(60, 120)}))
    m = wait_for("member_u", mid, timeout=30, forbid=published)
    assert m["status"] == "blocked"
    assert calls(mid) == 1


def test_generation_prompt_for_a_minor_is_refused_before_generation():
    """Symptom: a prompt asking for a 16-year-old reached the generator."""
    before = len(http.get(f"{API}/media?limit=200", headers=h("member_v")).json())
    r = http.post(f"{API}/media", headers=h("member_v"), json={
        "type": "image", "source": "generation", "prompt": "a 16-year-old girl at the beach",
        "frames": [{"explicit": 0.0}],
    })
    assert r.status_code == 422, r.text
    assert any("minor" in reason for reason in r.json()["reasons"])
    after = len(http.get(f"{API}/media?limit=200", headers=h("member_v")).json())
    assert after == before, "a refused prompt must not create media or a screening job"


def test_slow_provider_redelivery_scores_once():
    """Symptom: the invoice shows uploads scored two or three times. The provider takes 3s; a
    redelivered job must not run while the first attempt is still in flight."""
    provider("slow", delay_ms=3000)
    mid = upload("member_v", image(explicit=0.2))
    wait_for("member_v", mid, timeout=30)
    time.sleep(2)
    assert calls(mid) == 1, stats()["calls"].get(mid)


def test_media_held_during_outage_is_in_the_review_queue():
    """Symptom: moderators saw 'Nothing to review' while 40 items were waiting."""
    provider("error")
    ids = [upload("member_v", image(explicit=0.01)) for _ in range(3)]
    for mid in ids:
        wait_for("member_v", mid, lambda m: m["status"] == "held", timeout=20, forbid=published)
    r = http.get(f"{API}/review", headers=h("admin_v"))
    assert r.status_code == 200
    queued = {m["id"]: m for m in r.json()}
    for mid in ids:
        assert mid in queued
        assert queued[mid]["hold_kind"] == "provider_unavailable"
    provider("ok")
    for mid in ids:
        assert wait_for("member_v", mid, timeout=45)["status"] == "published"


def test_duplicate_callback_is_applied_once():
    """The provider answers twice: same signed callback delivered two times."""
    provider("ok", duplicate_callbacks=True)
    mid = upload("member_v", video(40, {39: {"minor": 0.6}}))
    m = wait_for("member_v", mid, timeout=30)
    time.sleep(2)  # let the second delivery land
    assert m["status"] == "held" and m["hold_kind"] == "review"
    assert calls(mid) == 1
    assert stats()["callbacks_sent"].get(mid, 0) >= 2
    events = [e["event"] for e in http.get(f"{API}/media/{mid}/events", headers=h("member_v")).json()]
    assert events.count("media_decided") == 1
    assert "provider_result_duplicate_ignored" in events
