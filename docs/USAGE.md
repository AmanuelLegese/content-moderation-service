# Usage

## Requirements

Docker with Compose v2. For running tests outside containers you also need [uv](https://docs.astral.sh/uv/) (Python 3.12) and Node 22.

## Start

```bash
docker compose up --build -d --wait
```

| Service | URL |
| --- | --- |
| API | http://localhost:8000 (OpenAPI docs at `/docs`) |
| Fake provider | http://localhost:8001 |
| UI | http://localhost:3000 |
| Postgres / Redis | localhost:5432 (`mod`/`mod`) / localhost:6379 |

To stop the stack: `docker compose down`. Add `-v` to also wipe the database.

## Seeded users

Every request identifies its caller with two headers. The user must belong to the workspace, otherwise the API returns 403.

| `X-User-Id` | `X-Workspace-Id` | Role | Workspace verified |
| --- | --- | --- | --- |
| `admin_v` | `ws_verified` | admin | yes |
| `member_v` | `ws_verified` | member | yes |
| `admin_u` | `ws_unverified` | admin | no |
| `member_u` | `ws_unverified` | member | no |

```bash
V='-H X-User-Id:member_v -H X-Workspace-Id:ws_verified'
AV='-H X-User-Id:admin_v -H X-Workspace-Id:ws_verified'
U='-H X-User-Id:member_u -H X-Workspace-Id:ws_unverified'
```

## API

### Upload or generate media: `POST /media`

```bash
curl -s $V -H content-type:application/json localhost:8000/media \
  -d '{"type":"image","source":"upload","frames":[{"explicit":0.95}]}'
# 202 {"id":"…","status":"pending"}

curl -s $U -H content-type:application/json localhost:8000/media \
  -d '{"type":"image","source":"generation","prompt":"a 16-year-old girl","frames":[{}]}'
# 422 {"reasons":["prompt refused: minor ('16-year-old')"]}
```

Media format:
* `type` is `image` (exactly one frame) or `video` (one frame per second).
* `source` is `upload` (the default) or `generation`. Generated media needs a `prompt`.
* Each frame maps a label to a score between 0 and 1. A label that's left out scores 0.

Labels: `minor`, `sexual_violence`, `bestiality`, `self_harm`, `real_person`, `explicit`.

### Read media: `GET /media/{id}`, `GET /media`, `GET /media/{id}/events`

```bash
curl -s $V localhost:8000/media/<id>          # {"id","status","reasons", …}; 404 from another workspace
curl -s $V 'localhost:8000/media?limit=20'    # this workspace's recent media
curl -s $V localhost:8000/media/<id>/events   # the job's full log: calls, answers, retries, decision
```

Statuses:
* `pending`: not screened yet.
* `published`: allowed.
* `blocked`: refused.
* `held`: waiting. The `hold_kind` field says why: `review` means a human must decide; `provider_unavailable` means there's no decision yet and the item will be re-screened automatically.

### Review (admins only)

```bash
curl -s $AV localhost:8000/review                      # held media of the admin's workspace
curl -s $AV -XPOST localhost:8000/review/<id>/approve  # 200 → published; 403 for members
curl -s $AV -XPOST localhost:8000/review/<id>/reject   # 200 → blocked
```

Approve returns 409 if the media isn't `held`, or if it has never been screened (`hold_kind=provider_unavailable`). Reject is always allowed for held media.

### Provider callback: `POST /provider/callback`

Used by the provider for videos longer than 30 frames. The `X-Signature` header must hold the HMAC-SHA256 of the raw body, as hex, using `PROVIDER_WEBHOOK_SECRET`. A missing or invalid signature returns 401.

### Operations

```bash
curl -s localhost:8000/health    # handler in use, breaker state
curl -s localhost:8000/metrics   # published_unchecked, held_no_decision, oldest waiting job/media, jobs by status
```

## Controlling the fake provider

```bash
curl -s -XPOST localhost:8001/_control -H content-type:application/json -d '{"mode":"timeout","delay_ms":0}'
curl -s localhost:8001/_stats     # {"calls":{"<media id>":n}, "scored":{…}, "callbacks_sent":{…}}
curl -s -XPOST localhost:8001/_reset
```

| `mode` | Behaviour |
| --- | --- |
| `ok` | Answers immediately |
| `slow` | Waits `delay_ms` before answering (or before sending the callback, for long videos) |
| `timeout` | Hangs for 120 s. The service gives up after `PROVIDER_TIMEOUT_S` (5 s). |
| `error` | 500 |
| `rate_limit` | 429 with `Retry-After: 1` |
| `malformed` | 200 with a truncated JSON body |

Add `"duplicate_callbacks": true` to deliver every async callback twice.

`calls` counts every request the provider received, failed ones included. `scored` counts only successful scorings.

## Reproduce the incident

```bash
export HANDLER=broken            # export it: every compose command must see it
docker compose up -d --build --wait
curl -s localhost:8000/health    # "handler":"broken"

curl -s -XPOST localhost:8001/_control -H content-type:application/json -d '{"mode":"timeout"}'
curl -s $U -H content-type:application/json localhost:8000/media -d '{"type":"image","frames":[{"explicit":0.99}]}'
# after ~5 s: GET /media/<id> → "published", explicit, in an unverified workspace

docker compose run --rm tests pytest tests/integration   # 16 failures, one per symptom
unset HANDLER && docker compose up -d --build --wait      # back to the fixed handler
```

The UI shows a red "BROKEN handler" banner while `HANDLER=broken`. In that mode it also shows a failed review request as "Nothing to review".

## Tests

```bash
cd server && uv sync && uv run pytest tests/unit          # 114 tests, no services needed
cd ui && npm ci && npm test                               # 17 tests
docker compose run --rm tests pytest tests/integration    # 44 tests against the running stack (~2 min)
docker compose run --rm tests pytest tests/integration -m incident       # only the incident symptoms
docker compose run --rm tests pytest tests/integration -m "not crash"    # skip the test that kills a worker
```

The crash test makes one worker exit right after the provider scores a video and before anything is saved. Compose restarts the worker, and the job is handed to another worker once its 30-second lease expires.

## Workload

```bash
cd server
uv run python scripts/workload.py --label fixed --out ../evidence/workload-fixed.json
uv run python scripts/workload.py --help    # burst size, outage start/length/mode, seed
```

The script uploads 200 videos to `ws_verified` and 1 image per second to `ws_unverified`, with a 40-second provider outage. It prints a summary: media published unchecked, unsafe media that went live, calls per upload, the oldest waiting job, and per-phase latency for the image workspace. Run it once with each handler to compare.

## Configuration

Environment variables (see `server/app/config.py`). Compose passes these through:

| Variable | Default (compose) | Meaning |
| --- | --- | --- |
| `HANDLER` | `fixed` | `broken` reproduces the incident |
| `PROVIDER_TIMEOUT_S` | 5 | Timeout for each provider call |
| `BREAKER_COOLDOWN_S` | 5 | How long the breaker stays open before one probe |
| `CALLBACK_DEADLINE_S` | 60 | How long to wait for an async callback before resubmitting |
| `WORKER_CONCURRENCY` | 8 | Threads per worker replica (2 replicas) |
| `PROVIDER_WEBHOOK_SECRET` | `dev-secret-change-me` | Shared HMAC secret for callbacks |

`MAX_ATTEMPTS`, `MAX_MALFORMED`, `MAX_RATE_LIMITED`, `MAX_RESCREEN_ROUNDS`, `LEASE_S` and `MAX_INFLIGHT_PER_WORKSPACE` can be set the same way.

## Troubleshooting

* **The broken handler behaves partly fixed.** `HANDLER` wasn't exported, so `docker compose run` recreated the API with the default. Check with `docker compose exec api env | grep HANDLER`.
* **Items stay `held` with `provider_unavailable`.** The provider is still failing, or the breaker is open. Check `/health` and `docker compose logs worker | grep provider_answer`. They clear automatically within a few seconds of the provider answering.
* **A port is already in use.** Ports 5432, 6379, 8000, 8001 and 3000 must be free; change the left side of `ports:` in `docker-compose.yml`.
* **Following one item:** `docker compose logs api worker | grep <media id>`, or `GET /media/<id>/events`.
