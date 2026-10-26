# Tools and libraries

Exact versions are pinned in `server/uv.lock` and `ui/package-lock.json`.

## Runtime

| Tool | Used for | Why this one |
| --- | --- | --- |
| **Python 3.12, FastAPI, Uvicorn** | API and fake provider | Required by the brief. Plain `def` endpoints run in FastAPI's thread pool, which fits the synchronous database driver. |
| **Pydantic v2** | Request validation (`MediaIn`, provider `Control`) | Ships with FastAPI. Invalid media is rejected with 422 before touching the database. |
| **PostgreSQL 16** | Media, jobs, provider results, events. **Also the job queue.** | One transactional store: a job claim, a stored result and a status change commit together. `FOR UPDATE SKIP LOCKED` with leases gives a safe queue without a second system to keep consistent. |
| **psycopg 3 + psycopg_pool** | Database access with plain SQL | The queue logic is written in SQL that has to be readable (the claim query, lease checks), so no ORM sits in between. |
| **Redis 7 (redis-py)** | Circuit breaker, shared 429 throttle, sweeper and probe locks, fault injection | State all workers must see within milliseconds, with expiry built in (`SET NX PX`). It isn't the source of truth for anything: losing Redis loses breaker state, not data. |
| **httpx** | Provider calls (worker); callbacks (fake provider) | Separate connect and read timeouts, and typed exceptions (`TimeoutException` vs `TransportError`) that map directly to the failure classes. |
| **Own worker** (`app/worker.py`) | Screening jobs | See below. |
| **React 18 + TypeScript, Vite** | Review queue page | Required by the brief. Vite gives a fast build with almost no configuration. |
| **nginx** | Serves the built UI and proxies `/api` to the API | Same origin, so no CORS. It re-resolves the API container through Docker DNS. |
| **Docker Compose** | Whole stack: postgres, redis, provider, api, 2 worker replicas, ui, tests | `docker compose up` starts everything, as the brief requires. Health checks order the startup. |

### Why not Celery, RQ, Dramatiq or arq

The incident is about delivery semantics: a redelivered job running alongside the first attempt, and "finished" not meaning "screened". A library would hide exactly the parts that have to be shown and tested:
* lease length compared with provider timeout
* fencing writes with a lease token
* parking a job and re-screening it later
* fair claiming per workspace

About 150 lines of SQL and Python make each of these explicit and testable. With Postgres as the queue, a job and its media change state in the same transaction. Past about a million uploads a day this stops being the right choice; README part 3 covers what replaces it.

## Development and testing

| Tool | Used for |
| --- | --- |
| **uv** | Python environment and lockfile (`uv sync --frozen` in the Dockerfile and CI) |
| **pytest** | Unit tests (`tests/unit`, no services) and integration tests (`tests/integration`, over HTTP against the live stack). Markers: `incident`, `slow`, `crash`. |
| **fakeredis** | Breaker tests without a Redis server |
| **httpx `MockTransport` / `ASGITransport`** | Provider-client tests without a network; calling the fake provider in-process so its background callback can be awaited |
| **Vitest + Testing Library + jsdom** | UI tests: the loading, failed and empty states, actions, role handling, the API helper |
| **GitHub Actions** (`.github/workflows/ci.yml`) | Three jobs: Python unit tests, UI type check, tests and build, and the compose integration suite |
| **`scripts/workload.py`** | Repeatable before-and-after load with an outage; compares every item's final status with the correct answer |
| **Claude Code** | AI pair programmer. What it did and what was checked is in `AI.md`. |

## Fault injection built into the code

These are test hooks, not production features:

* **Provider modes** (`/_control`): `slow`, `timeout`, `error`, `rate_limit`, `malformed`, and `duplicate_callbacks`.
* **`HANDLER=broken`**: the deliberately broken handler (`app/broken.py`) that reproduces the incident.
* **Crash hook:** with `ALLOW_FAULTS=1`, setting the Redis key `fault:crash_after_score=<media id>` makes the worker exit right after the provider answers, before saving. Compose sets `ALLOW_FAULTS=1` for local use; it would be off in production.
