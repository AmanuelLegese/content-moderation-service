import os


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://mod:mod@localhost:5432/mod")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
PROVIDER_URL = os.environ.get("PROVIDER_URL", "http://localhost:8001")
CALLBACK_URL = os.environ.get("CALLBACK_URL", "http://localhost:8000/provider/callback")
WEBHOOK_SECRET = os.environ.get("PROVIDER_WEBHOOK_SECRET", "dev-secret-change-me")

# "fixed" is the real handler. "broken" reproduces the Tuesday incident on purpose.
HANDLER = os.environ.get("HANDLER", "fixed")

# Provider calls
PROVIDER_TIMEOUT_S = _f("PROVIDER_TIMEOUT_S", 8.0)
ASYNC_FRAME_THRESHOLD = 30  # provider scores videos longer than this asynchronously

# Retries: budget per screening round, for transient failures (timeout, connection, 5xx)
MAX_ATTEMPTS = _i("MAX_ATTEMPTS", 3)
MAX_MALFORMED = _i("MAX_MALFORMED", 2)
MAX_RATE_LIMITED = _i("MAX_RATE_LIMITED", 10)
BACKOFF_BASE_S = _f("BACKOFF_BASE_S", 1.0)
BACKOFF_MAX_S = _f("BACKOFF_MAX_S", 30.0)
# How many times outage-held media is automatically re-screened before it is left for a human
MAX_RESCREEN_ROUNDS = _i("MAX_RESCREEN_ROUNDS", 5)

# Job lease: must be longer than one provider call so a healthy attempt is never redelivered
LEASE_S = _f("LEASE_S", 30.0)
CALLBACK_DEADLINE_S = _f("CALLBACK_DEADLINE_S", 120.0)

# Circuit breaker, shared through Redis by every worker
BREAKER_THRESHOLD = _i("BREAKER_THRESHOLD", 3)
BREAKER_COOLDOWN_S = _f("BREAKER_COOLDOWN_S", 5.0)

WORKER_CONCURRENCY = _i("WORKER_CONCURRENCY", 8)
# Cap on jobs one workspace may have in flight at once, so a burst cannot take every worker
MAX_INFLIGHT_PER_WORKSPACE = _i("MAX_INFLIGHT_PER_WORKSPACE", 4)

# Broken handler only: queue visibility timeout shorter than a slow provider call
BROKEN_VISIBILITY_S = _f("BROKEN_VISIBILITY_S", 1.0)
BROKEN_MAX_FRAMES = 8
