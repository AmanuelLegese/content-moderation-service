"""Provider failure modes are handled differently, retries are bounded, and media held
during an outage is re-screened automatically once the provider recovers."""

import os
import time

import pytest
import redis

from .conftest import API, calls, h, http, image, provider, scored, upload, video, wait_for

pytestmark = pytest.mark.slow


def published(m):
    return m["status"] == "published"


def test_500s_are_retried_with_a_limit_then_held_then_rescreened():
    provider("error")
    mid = upload("member_u", image(explicit=0.01))
    wait_for("member_u", mid, lambda m: m["status"] == "held", timeout=20, forbid=published)
    time.sleep(10)
    n = calls(mid)
    # 3 attempts per round, then the breaker stops calls; at most a couple of half-open probes on top
    assert 1 <= n <= 5, n
    provider("ok")
    assert wait_for("member_u", mid, timeout=45)["status"] == "published"
    assert scored(mid) == 1


def test_429_backs_off_and_is_not_held_for_long():
    provider("rate_limit")
    mid = upload("member_v", image(explicit=0.2))
    time.sleep(3)
    assert http.get(f"{API}/media/{mid}", headers=h("member_v")).json()["status"] in ("pending", "held")
    provider("ok")
    m = wait_for("member_v", mid, timeout=30)
    assert m["status"] == "published"
    assert scored(mid) == 1


def test_malformed_response_is_not_a_pass():
    provider("malformed")
    mid = upload("member_u", image(explicit=0.01))
    m = wait_for("member_u", mid, lambda m: m["status"] == "held", timeout=20, forbid=published)
    assert "decision" in m["reasons"][0]
    time.sleep(5)
    assert calls(mid) <= 4, calls(mid)
    provider("ok")
    assert wait_for("member_u", mid, timeout=45)["status"] == "published"


def test_slow_but_within_timeout_is_fine():
    provider("slow", delay_ms=1500)
    mid = upload("member_v", video(10, {9: {"bestiality": 0.9}}))
    assert wait_for("member_v", mid, timeout=20)["status"] == "blocked"
    assert calls(mid) == 1


def test_admin_cannot_approve_unscreened_media():
    provider("error")
    mid = upload("member_v", image(explicit=0.01))
    wait_for("member_v", mid, lambda m: m["status"] == "held", timeout=20)
    r = http.post(f"{API}/review/{mid}/approve", headers=h("admin_v"))
    assert r.status_code == 409  # approving would publish it unchecked (rule 5)
    provider("ok")
    wait_for("member_v", mid, timeout=45)


@pytest.mark.crash
def test_worker_dies_after_provider_scored_before_result_saved():
    """Fault injection: the worker process os._exit()s right after the provider answered
    and before anything is written. The lease expires (LEASE_S=30), another worker
    redelivers the job and calls the provider again. Proves: nothing is published
    unchecked and the media is decided correctly. Costs: a second provider call."""
    r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    provider("slow", delay_ms=2000)
    mid = upload("member_u", video(12, {11: {"explicit": 0.9}}))
    r.set("fault:crash_after_score", mid)
    provider("ok")  # the call already in flight keeps its 2s delay
    m = wait_for("member_u", mid, timeout=60, forbid=published)
    assert m["status"] == "blocked"
    events = [e["event"] for e in http.get(f"{API}/media/{mid}/events", headers=h("member_u")).json()]
    assert "lease_expired_redelivered" in events
    assert calls(mid) == 2  # the price of a crash in that window; see README
