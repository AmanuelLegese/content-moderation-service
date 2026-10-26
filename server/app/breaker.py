"""Circuit breaker shared by all workers through Redis.

closed     -> calls go through
open       -> no calls; claimed jobs are parked (media held) without spending money
half-open  -> after the cooldown exactly one worker may send a probe call
A success closes it and records `last_success`, which is what triggers automatic
re-screening of media held during the outage (worker.recover_parked)."""

import time

import redis

from . import config

r = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)

FAILS = "breaker:fails"
OPEN_UNTIL = "breaker:open_until"
PROBE = "breaker:probe"
LAST_SUCCESS = "provider:last_success"
THROTTLE_UNTIL = "provider:throttle_until"


def allow_call() -> bool:
    open_until = r.get(OPEN_UNTIL)
    if open_until is None:
        return True
    if time.time() < float(open_until):
        return False
    # half-open: one probe at a time
    return bool(r.set(PROBE, "1", nx=True, ex=int(config.PROVIDER_TIMEOUT_S) + 5))


def state() -> str:
    open_until = r.get(OPEN_UNTIL)
    if open_until is None:
        return "closed"
    return "open" if time.time() < float(open_until) else "half_open"


def record_success() -> None:
    p = r.pipeline()
    p.delete(FAILS, OPEN_UNTIL, PROBE)
    p.set(LAST_SUCCESS, time.time())
    p.execute()


def record_failure() -> bool:
    """Returns True if this failure opened (or re-opened) the breaker."""
    fails = r.incr(FAILS)
    if fails >= config.BREAKER_THRESHOLD:
        r.set(OPEN_UNTIL, time.time() + config.BREAKER_COOLDOWN_S)
        r.delete(PROBE)
        return True
    return False


def last_success() -> float:
    v = r.get(LAST_SUCCESS)
    return float(v) if v else 0.0


def throttle(seconds: float) -> None:
    r.set(THROTTLE_UNTIL, time.time() + seconds)


def throttled_for() -> float:
    v = r.get(THROTTLE_UNTIL)
    return max(0.0, float(v) - time.time()) if v else 0.0
