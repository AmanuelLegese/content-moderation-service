"""All tunable settings, read once from environment variables.

What: one place for every timeout, limit and URL the service uses.
How: plain module-level constants; docker-compose.yml sets the environment.
Why: the failure-handling behaviour (timeouts, retry budgets, lease length,
breaker cooldown) is exactly what the incident was about, so it must be easy to
see and to change per environment, e.g. shorter timeouts in tests.
"""

import os


def _f(name: str, default: float) -> float:
    """Float setting from the environment, with a default."""
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    """Integer setting from the environment, with a default."""
    return int(os.environ.get(name, default))


# Where everything lives. Defaults point at localhost so scripts and unit tests run outside Docker.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://mod:mod@localhost:5432/mod")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
PROVIDER_URL = os.environ.get("PROVIDER_URL", "http://localhost:8001")
# Sent to the provider with every request so it knows where to post async (long video) results.
CALLBACK_URL = os.environ.get("CALLBACK_URL", "http://localhost:8000/provider/callback")
# Shared secret for the HMAC-SHA256 signature on provider callbacks (rule 7).
WEBHOOK_SECRET = os.environ.get("PROVIDER_WEBHOOK_SECRET", "dev-secret-change-me")

# "fixed" is the real handler. "broken" reproduces the Tuesday incident on purpose.
HANDLER = os.environ.get("HANDLER", "fixed")

# Provider calls
# How long one provider call may take before we give up. A timeout is a failure, never a pass (rule 5).
PROVIDER_TIMEOUT_S = _f("PROVIDER_TIMEOUT_S", 8.0)
ASYNC_FRAME_THRESHOLD = 30  # provider scores videos longer than this asynchronously

# Retries: budget per screening round, for transient failures (timeout, connection, 5xx)
MAX_ATTEMPTS = _i("MAX_ATTEMPTS", 3)
# Garbage answers get fewer retries: a provider returning nonsense rarely fixes itself in seconds.
MAX_MALFORMED = _i("MAX_MALFORMED", 2)
# 429s don't use the normal budget (the provider is healthy, just busy) but are still capped.
MAX_RATE_LIMITED = _i("MAX_RATE_LIMITED", 10)
# Exponential backoff between retries: 1s, 2s, 4s ... capped at 30s, with jitter (screening.backoff).
BACKOFF_BASE_S = _f("BACKOFF_BASE_S", 1.0)
BACKOFF_MAX_S = _f("BACKOFF_MAX_S", 30.0)
# How many times outage-held media is automatically re-screened before it is left for a human
MAX_RESCREEN_ROUNDS = _i("MAX_RESCREEN_ROUNDS", 5)

# Job lease: must be longer than one provider call so a healthy attempt is never redelivered
# (the broken handler's 1s lease is what caused the 2-3x provider bills).
LEASE_S = _f("LEASE_S", 30.0)
# How long to wait for an async callback before treating it as lost and resubmitting.
CALLBACK_DEADLINE_S = _f("CALLBACK_DEADLINE_S", 120.0)

# Circuit breaker, shared through Redis by every worker
# Consecutive failures before we stop calling the provider at all.
BREAKER_THRESHOLD = _i("BREAKER_THRESHOLD", 3)
# How long it stays open before one probe call is allowed.
BREAKER_COOLDOWN_S = _f("BREAKER_COOLDOWN_S", 5.0)

# Threads per worker process; each thread processes one job at a time.
WORKER_CONCURRENCY = _i("WORKER_CONCURRENCY", 8)
# Cap on jobs one workspace may have in flight at once, so a burst cannot take every worker
MAX_INFLIGHT_PER_WORKSPACE = _i("MAX_INFLIGHT_PER_WORKSPACE", 4)

# Broken handler only: queue visibility timeout shorter than a slow provider call
BROKEN_VISIBILITY_S = _f("BROKEN_VISIBILITY_S", 1.0)
# Broken handler only: how many video frames it sends (the incident's "first 8 frames" bug)
BROKEN_MAX_FRAMES = 8
