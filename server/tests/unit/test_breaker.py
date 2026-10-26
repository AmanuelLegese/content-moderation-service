import time

import fakeredis
import pytest

from app import breaker, config


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    monkeypatch.setattr(breaker, "r", fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(config, "BREAKER_THRESHOLD", 3)
    monkeypatch.setattr(config, "BREAKER_COOLDOWN_S", 0.2)


def test_closed_allows_calls():
    assert breaker.state() == "closed"
    assert breaker.allow_call() and breaker.allow_call()


def test_opens_after_threshold_consecutive_failures():
    assert not breaker.record_failure()
    assert not breaker.record_failure()
    assert breaker.record_failure()
    assert breaker.state() == "open"
    assert not breaker.allow_call()


def test_success_resets_the_count():
    breaker.record_failure(), breaker.record_failure()
    breaker.record_success()
    assert not breaker.record_failure()
    assert breaker.state() == "closed"


def test_half_open_allows_exactly_one_probe():
    for _ in range(3):
        breaker.record_failure()
    time.sleep(0.25)
    assert breaker.state() == "half_open"
    assert breaker.allow_call() is True
    assert breaker.allow_call() is False  # every other worker parks instead of calling


def test_failed_probe_reopens():
    for _ in range(3):
        breaker.record_failure()
    time.sleep(0.25)
    assert breaker.allow_call()
    breaker.record_failure()
    assert breaker.state() == "open"


def test_probe_success_closes_and_records_recovery_time():
    for _ in range(3):
        breaker.record_failure()
    time.sleep(0.25)
    assert breaker.allow_call()
    before = time.time()
    breaker.record_success()
    assert breaker.state() == "closed"
    assert breaker.last_success() >= before  # what triggers re-screening of parked media


def test_throttle():
    assert breaker.throttled_for() == 0
    breaker.throttle(2)
    assert 1.5 < breaker.throttled_for() <= 2
