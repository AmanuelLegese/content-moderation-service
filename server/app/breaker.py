"""Circuit breaker shared by all workers through Redis.

closed     -> calls go through
open       -> no calls; claimed jobs are parked (media held) without spending money
half-open  -> after the cooldown exactly one worker may send a probe call
A success closes it and records `last_success`, which is what triggers automatic
re-screening of media held during the outage (worker.recover_parked).

Why a breaker: during the incident's 40-minute outage, every worker kept calling a
dead provider, waiting for timeouts and paying for calls. Once a few calls in a row
fail, it is cheaper and faster to stop calling, hold new media immediately, and
test the provider with a single probe now and then.
Why Redis: the breaker must be shared. Two worker processes with 8 threads each
must all see "open" at once; a per-process breaker would still send 16x the calls.
"""

import time

import redis

from . import config

# decode_responses=True: values come back as str, not bytes.
r = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)

# Redis keys
FAILS = "breaker:fails"                      # consecutive failures since the last success
OPEN_UNTIL = "breaker:open_until"            # unix time; present means open (or half-open once passed)
PROBE = "breaker:probe"                      # held by the one worker allowed to probe while half-open
LAST_SUCCESS = "provider:last_success"       # unix time of the last good answer; drives re-screening
THROTTLE_UNTIL = "provider:throttle_until"   # set by a 429: every worker waits until then


def allow_call() -> bool:
    """May this worker call the provider right now?"""
    open_until = r.get(OPEN_UNTIL)
    if open_until is None:
        return True  # closed
    if time.time() < float(open_until):
        return False  # open: still cooling down
    # half-open: one probe at a time
    # SET NX ("only if not exists") is atomic, so exactly one worker wins the probe slot.
    # The expiry frees the slot even if the probing worker crashes mid-call.
    return bool(r.set(PROBE, "1", nx=True, ex=int(config.PROVIDER_TIMEOUT_S) + 5))


def state() -> str:
    """closed | open | half_open. Used for logs, /health and the sweeper."""
    open_until = r.get(OPEN_UNTIL)
    if open_until is None:
        return "closed"
    return "open" if time.time() < float(open_until) else "half_open"


def record_success() -> None:
    """A good answer: close the breaker and remember when, so parked media gets re-screened."""
    # A pipeline sends both commands in one round trip.
    p = r.pipeline()
    p.delete(FAILS, OPEN_UNTIL, PROBE)
    p.set(LAST_SUCCESS, time.time())
    p.execute()


def record_failure() -> bool:
    """Returns True if this failure opened (or re-opened) the breaker."""
    # INCR is atomic, so concurrent failures from many threads are all counted.
    fails = r.incr(FAILS)
    if fails >= config.BREAKER_THRESHOLD:
        # Also covers a failed half-open probe: the count is already over the threshold,
        # so a single probe failure re-opens it for another cooldown.
        r.set(OPEN_UNTIL, time.time() + config.BREAKER_COOLDOWN_S)
        r.delete(PROBE)
        return True
    return False


def last_success() -> float:
    v = r.get(LAST_SUCCESS)
    return float(v) if v else 0.0


def throttle(seconds: float) -> None:
    """The provider said 429 + Retry-After: make every worker back off, not just this one."""
    r.set(THROTTLE_UNTIL, time.time() + seconds)


def throttled_for() -> float:
    """Seconds left before anyone may call again after a 429 (0 if not throttled)."""
    v = r.get(THROTTLE_UNTIL)
    return max(0.0, float(v) - time.time()) if v else 0.0
