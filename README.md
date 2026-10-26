# Content moderation service: incident reproduction and fix

A FastAPI and React service that screens uploaded and AI-generated media. It keeps prohibited content away from everyone and explicit content away from unverified workspaces, and nothing goes live unchecked, even when the moderation provider is slow, down or answers twice.

## Run it

```bash
docker compose up --build              # fixed handler. API :8000, provider :8001, UI http://localhost:3000
HANDLER=broken docker compose up -d    # the deliberately broken handler (incident reproduction)

docker compose run --rm tests pytest tests/integration    # integration tests against the running stack
cd server && uv run pytest tests/unit                      # 114 unit tests, no services needed
cd ui && npm ci && npm test                                # 17 UI tests
cd server && uv run python scripts/workload.py --label fixed --out ../evidence/workload-fixed.json
```

Full usage guide (every route with curl, provider control, tests, configuration, troubleshooting): [docs/USAGE.md](docs/USAGE.md). Tools and why each was chosen: [docs/TOOLS.md](docs/TOOLS.md).

Export `HANDLER=broken` in the shell for **every** compose command when you use the broken handler. Without it, `docker compose run tests` recreates the API with the fixed handler and you get a mixed stack. This happened to me once (see Evidence).

## Design in one screen

| Piece | What it does |
| --- | --- |
| `api` (FastAPI, 2 processes) | Routes from the brief. Screens generation prompts before anything is stored. Verifies the HMAC on callbacks. |
| `worker` (2 replicas × 8 threads) | Postgres queue: `FOR UPDATE SKIP LOCKED` with a 30s lease. Fair claim: the ready workspace with the fewest jobs in flight goes first, with a cap of 4 in flight per workspace. |
| sweeper (thread in each worker) | Hands expired leases to another worker, times out lost callbacks, and re-screens media held during an outage once the provider is back. |
| Redis | Circuit breaker (closed, open, then one probe), shared 429 throttle, and locks for the sweeper and the fault injection. |
| Postgres | `media`, `jobs`, `provider_results` (**one row per media: the scoring we paid for**), and `events` (the job log). |
| `provider` | Fake provider: echoes scores, fills in every label, counts calls per media ID, has `ok/slow/timeout/error/rate_limit/malformed` modes, sends signed async callbacks for videos over 30 frames, and can send each callback twice. |
| `ui` (React + TS) | Review queue with distinct loading, failed (with retry and stale data) and empty states. Also a user switcher, recent media and an upload box. |

The core invariant: **media becomes `published` only through `apply_result` with a validated result covering every frame, or when an admin approves media that has already been screened.** Every other path, including timeouts, 5xx, 429s, malformed answers, an open breaker or a lost callback, ends in `held`.

## The failure

| Incident symptom | Root cause (reproduced in `server/app/broken.py`) |
| --- | --- |
| Uploads went live unchecked during the outage | A provider timeout was treated as "nothing found" and the media was published. |
| Explicit video published in an unverified workspace | Only the first 8 frames were sent, and the short answer was read as complete. |
| Prompt asking for a 16-year-old reached the generator | Generation prompts were never checked. |
| Invoice shows uploads scored 2–3 times | The queue's visibility timeout (1s) was shorter than a slow provider call, so a job was handed out again while the first attempt was still running. Nothing checked whether the media had already been scored. |
| "Nothing to review" while 40 items waited | Two bugs: the UI showed a failed `/review` request as an empty list, and outage media was never `held`. It was published, or retried forever as `pending`, so it never reached the queue. |

"No deployment since" fits: these were latent bugs in error paths that only an outage exercised.

## Evidence

All of it is in `evidence/` and was produced on this machine.

* `1-broken-integration-tests.txt`, with `HANDLER=broken`: **16 failed, 28 passed.** Every incident test fails for its own reason:
  * a `forbidden state reached: published` failure
  * `202 == 422` for the prompt
  * `3 == 1` provider calls
  * outage items stuck in `pending`

  The first run looked like 11 failures because `compose run` had recreated the API with the fixed handler. I caught that the prompt test was passing when it shouldn't, found the mixed stack and re-ran.
* `2-fixed-integration-tests.txt`: **44 passed.** The first fixed run had **1 real failure**: media held after two malformed answers was never re-screened (details under Fix). It was fixed in commit `c46cbf0`.
* `0-unit-tests.txt`: 114 Python unit tests (policy, prompt screening, provider client, breaker, signing, validation, fake provider) and 17 UI tests.
* `workload-broken.json` and `workload-fixed.json`: full per-item timelines and metric samples.
* `3-crash-trace.txt`: the event log of a worker killed between scoring and saving.

The tests check data, not job status. "Unchecked" means published without a stored result that covers every frame. The broken handler marks its jobs `done` and even sets `screened_at` after a timeout, so job status and that flag prove nothing.

## Before and after: same workload

`scripts/workload.py` (seed 7): `ws_verified` uploads a burst of 200 videos (10–60 frames, a mix of the sync and async paths, with prohibited or explicit content placed at the end). Meanwhile `ws_unverified` uploads one image per second for 80 seconds. The provider is in `timeout` mode from t=10s to t=50s. Every item has a known correct answer, computed with the policy over **all** frames.

| Metric | Broken | Fixed |
| --- | --- | --- |
| Media published unchecked (or only partly checked) | **218 / 279** | **0** |
| Unsafe media that went live at any point | **58** | **0** |
| Wrong final decision vs. correct answer | 58 | 0 |
| Provider calls per upload | 1.36 | 1.03 |
| Uploads with more than one call | 29 | 8 (all calls that timed out during the outage; **0** media scored successfully twice) |
| Oldest waiting job, max | 14.4 s | 47.7 s |
| `ws_unverified` images uploaded during the outage: median time to leave `pending` | 9.9 s (by being published unchecked) | 0.9 s (held) |
| …median time to a real decision | n/a (none were screened) | 29.5 s; all decided within about 9 s of recovery |
| …images after the outage: median / max decision | 0.5 / 1.4 s | 0.3 / 9.3 s |

"Oldest waiting job" gets *worse* after the fix, on purpose. The broken handler had a short queue because it published things it couldn't check. With the fix, media waits (held) for the whole 40-second outage and is re-screened automatically within about 9 seconds of recovery. That's 5 seconds of breaker cooldown, one probe call, then everything waiting is requeued.

**The burst doesn't starve the other workspace.** During the outage, `ws_unverified` images were held within about 1 second even though 200 videos were queued ahead of them by time. After it, they were decided in a median of 0.3 seconds while the burst drained. Two things achieve this: the claim picks the workspace with the fewest jobs in flight, and each workspace is capped at 4 jobs in flight.

## Fix

* **Rule 5, never unchecked.** Timeouts, connection errors and 5xx count as failures. The media is `held` (`hold_kind=provider_unavailable`) after the first failure and retried with exponential backoff and jitter, at most 3 times per round. After that the job is *parked*. An admin can't approve unscreened media (409), because approving a JSON document they can't see would publish it unchecked. They can reject it.
* **Each failure type is handled differently:**

  | Failure | Handling |
  | --- | --- |
  | timeout, connection error, 5xx | Backoff, at most 3 attempts per round, counts towards opening the breaker |
  | 429 | Respects `Retry-After` and throttles every worker through Redis. Doesn't count towards the breaker or the attempt budget. Capped at 10. |
  | malformed (bad JSON, wrong frame count, missing label, score out of range) | Counts towards the breaker. 2 attempts, then parked |
  | other 4xx | Parked immediately: retrying the same request won't help |
* **Circuit breaker** (Redis, shared). It opens after 3 consecutive failures. While it's open, claimed jobs are parked without calling the provider, which costs nothing. After a 5-second cooldown, exactly one probe call goes out.
* **Recovery without a human.** Parked jobs are requeued once the provider succeeds after they were parked. If there's no other traffic, the sweeper uses the oldest parked job as a probe. Its retry counter is set one short of the limit, so a failed probe costs one call and parks it again. There are at most 5 re-screen rounds per item; after that it waits for a human.
* **Rule 9, every frame.** All frames are sent. `validate_result` rejects any answer that doesn't contain exactly one complete set of scores per frame.
* **Rule 4, prompts.** `app/prompts.py` runs before anything is stored. It normalises the text first (Unicode, homoglyphs, leetspeak, spaced-out letters, number words) and then matches patterns for minors and ages under 18, sexual violence, bestiality, self-harm, real people, incest and trafficking. A refused prompt creates no media and no job.
* **Rule 8, scored at most once.**
  * The lease (30 s) is longer than the provider timeout (5 s), so a healthy attempt is never handed out again.
  * Before calling the provider, the worker checks `provider_results`. If a result exists it's applied without a new call; this covers a crash between saving and deciding.
  * `provider_results` has the media ID as its primary key and the first result wins. Duplicate and late callbacks are ignored, which shows up as `provider_result_duplicate_ignored` in the events.
  * An `Idempotency-Key: <media id>` header is sent on every call; see "Remaining limits".
* **Rule 7.** HMAC-SHA256 over the raw body, compared in constant time. A bad signature returns 401. A validly signed but malformed body returns 422, and the job's callback deadline sends it again later.
* **Rules 1 and 6.** The caller must belong to the workspace named in the header (403 otherwise). Another workspace's media returns 404, never 403, so its existence isn't revealed. Review routes are admin-only.
* **UI.** Loading, failed and empty are three different renders. A failed refresh keeps the last list on screen and marks it as possibly out of date. Outage-held items are labelled, and their Approve button is disabled.

### Worker dies after scoring, before saving

`ALLOW_FAULTS=1` together with the Redis key `fault:crash_after_score=<media id>` makes the worker call `os._exit(137)` right after the provider answers, before any write. See `test_worker_dies_after_provider_scored_before_result_saved` and `evidence/3-crash-trace.txt`:

```
provider_call               attempt 1
(worker-2: fault_injected_crash, container restarts)
lease_expired_redelivered   attempt 1   (+30 s)
provider_call               attempt 2
provider_answer             scored
media_decided               blocked: explicit content in unverified workspace (frame 11)
```

**What it proves:** a process dying in that window doesn't publish anything, doesn't lose the media, and doesn't need a human. The lease expires, another worker handles the job, and the decision is correct. **The cost:** the provider is called twice, because the first answer is lost with the process. This is at-least-once processing; exactly-once *billing* needs the provider to deduplicate on the `Idempotency-Key` we already send.

**What it doesn't prove:**
* A real crash can land anywhere: mid-transaction, which Postgres rolls back, so that's safe; or during the HTTP read.
* A real crash can also be a network partition, where the worker is alive but can't reach the database. Then a second worker could run while the first is still alive, which is exactly the incident's double-scoring. The lease would need fencing (we check `lease_token` on every write, but the provider call itself isn't fenced).
* An OOM kill or a node loss would behave like this test, but a hung-but-alive worker past its lease wouldn't.
* It runs one crash on one item, with no load.

## Following a job

Every log line is JSON with `workspace_id`, `media_id`, `job_id` and `attempt`. The same events are stored in Postgres:

```bash
docker compose logs worker api | grep <media_id>
curl -H 'X-User-Id: admin_v' -H 'X-Workspace-Id: ws_verified' localhost:8000/media/<id>/events
curl localhost:8000/metrics      # published_unchecked, held_no_decision, oldest waiting job/media, breaker state
```

Events: `media_created → provider_call → provider_answer(outcome, elapsed_ms) → media_decided`. Failure paths add `media_held_no_decision`, `job_retry_scheduled`, `job_parked`, `lease_expired_redelivered`, `callback_deadline_passed`, `rescreen_scheduled`, `probe_scheduled` and `provider_result_duplicate_ignored`.

## Decisions and assumptions

* **`explicit` between 0.5 and 0.8 in an unverified workspace is held for a human.** The rules don't cover it, and it's the safe side of "nothing explicit reaches an unverified workspace". The same score in a verified workspace is published.
* **Admins can't approve unscreened (outage-held) media: 409.** If your checks expect 200 here, this is the reason.
* **Prompt screening leans towards refusing.** "Sexy woman walking her dog" is refused because it pairs a sexual word with an animal word. A refused prompt costs the user a rephrase; a missed one costs a payment processor.
* **Failed calls count towards the provider stats.** Rule 8 is about successful scoring, which `/_stats.scored` reports separately.
* **Lost async callbacks:** after 60 s with no callback (configurable), the job is treated as a transient failure and resubmitted, which costs another call. A real provider's async jobs might take longer, so that deadline should be set from its SLA.

## Remaining limits: what's not done or still broken

* **A crash in the scoring-to-saving window costs a second paid call.** There's no provider-side deduplication.
* **A flapping provider can use up the 5 re-screen rounds.** That item then waits in the queue for a human, and an admin can only reject it, not approve it. There's no "force re-screen" button.
* **No authentication**, by the brief's design. `/metrics` and `/health` are open.
* **The breaker cooldown is a fixed 5 s.** A 40-minute outage means about 480 probe calls; it should back off exponentially.
* **Prompt screening is regex only.** It's shown to work on a fixed list of evasions, not against a determined adversary (see part 3).
* **The `events` table grows without limit** and is written synchronously on the hot path.
* Retry counters for a callback that never arrived share the transient-failure budget. That's fine, but untested beyond the deadline logic.
* **Not tested:** a sustained 429 storm at volume, Postgres or Redis outages, more than 2 worker replicas, and real clock skew between containers.

## Production plan

**First 15 minutes (an outage like Tuesday's, with the fix deployed):**
1. Check `/metrics`: `published_unchecked` must be 0. If it isn't, go to step 4.
2. Check the breaker state, `held_no_decision`, the oldest waiting job, and the provider's latency, error and 429 rate in the logs (`provider_answer` outcome and elapsed_ms).
3. Tell the moderators the review queue shows items held as "No decision yet (provider unavailable)", and that these clear automatically.
4. If anything was published unchecked: unpublish it (set those media to held) with the query behind `published_unchecked`, then re-screen it. Unchecked media is assumed unsafe until scored.

**Rollout:**
1. Ship the queue and handler changes behind `HANDLER`, canary on one worker replica first.
2. Watch `published_unchecked` (must stay 0), provider calls per upload (≈1.0), oldest waiting job, and `job_parked` and `lease_expired_redelivered` rates.
3. Before the rollout, run a one-off backfill: re-screen everything published since Tuesday that has no complete `provider_results` row, holding it first.

**Roll back** if `published_unchecked` > 0, if calls per upload goes above 1.2 with a healthy provider, or if held items aren't re-screened within 2 minutes of provider recovery. Rolling back means going back to the previous worker image while keeping the schema (the change is additive). **Never** roll back to "a timeout counts as a pass".

**Verify recovery:**
* Put the provider into `timeout` in staging and check the same three numbers as the workload table.
* In production, after an outage, check that `held_no_decision` returns to 0 and that every item from the outage has a `provider_results` row covering all of its frames.

## Part 3: cost and the next limit

Prices are from memory and are **not checked against current price sheets**. Treat them as rough sizes.

**Scoring: a mix, mostly paid APIs at this volume.**
* Volume: 10,000 images plus 1,000 one-minute videos. At 1 frame per second (the brief's model) that's 60 frames per video, plus extra frames at scene changes: about 70,000 frames a month.
* A general visual moderation API (Hive-class: explicit, minors, self-harm, violence, bestiality-related classes) costs about $1–3 per 1,000 frames, so **$70–$210 a month**.
* No vendor reliably covers **`real_person`** or **non-consent**:
  * `real_person`: a face-match API (≈$1 per 1,000) against a celebrity index *and* against our own index of verified, consenting performers. Faces that don't match any consenting performer are held.
  * `sexual_violence`: can't be decided from pixels alone. Use the classifier's violence and restraint signals, the generation prompt, and human review in the 0.5–0.8 band.
* Total: **about $100–$300 a month**, plus moderators' time.
* Self-hosting means a GPU running all the time (about $500+ a month for one L4-class card), on-call work and our own evaluation of the models. It only pays off at millions of frames a month, or where a vendor can't take the data.
* How many frames: 1 per second plus scene-change frames. For videos the first pass flags, every frame in the flagged section. Rule 9 means never only the start.

**Generation prompts:**
* **Layer 1:** a fast normalisation and deny-list (as built here).
* **Layer 2:** an LLM safety classifier (a small, fast hosted model, or a self-hosted guard model) with our written policy. It reads the whole prompt, any negative prompt, earlier turns in the session and any reference image. It returns a category and a confidence; a doubtful answer means refuse.
* **Against people trying to get around it:**
  * normalise text before classifying, and translate prompts that aren't in English
  * look for ages written in other ways, and "school", "petite", "teen"-style coded words, combined with sexual terms
  * keep refusal counts per user and per IP, and slow down or review accounts that keep trying
  * test regularly with a red-team set that grows from real refusals
* **The output is screened anyway:** generated media goes through the same frame screening (`test_generated_output_is_screened_even_when_prompt_passes`).

**CSAM hash matching:**
* Run PhotoDNA or Thorn Safer (NCMEC and industry hash lists) on every upload and every generated output, on the bytes *before* any other processing. Also run it on video keyframes.
* **On a match:** block, keep the evidence under restricted access, report to NCMEC as US law requires, suspend the account, and keep it out of the normal moderator queue.
* **While the hash service is down: fail closed**, exactly like rule 5. Publishing needs a recorded hash check *and* a classifier result, so items queue as `held` and are re-checked when it recovers. Alert on the age of the oldest item waiting.

**At 1 million uploads a day** (about 12 per second on average, 50+ at peak, plus video frames):
* **What breaks first:** the Postgres queue.
  * The fair-claim query runs `SELECT DISTINCT workspace_id` over the queued jobs on every claim.
  * Every job writes about 5 rows to `events`, so roughly 5 million rows a day on the same database.
  * Worker threads block on synchronous provider calls, so a slow provider caps throughput at threads ÷ latency (16 threads ÷ 5 s ≈ 3 jobs a second).
* **What to measure:**
  * claim-query latency and rows scanned (`pg_stat_statements`)
  * queue depth and oldest job per workspace
  * worker busy time
  * provider p99 latency and 429 rate
  * database write load and size of the `events` table
* **What to change first:**
  1. Move `events` to the log pipeline.
  2. Make provider calls async (asyncio) and send frames in batches, so each worker can have hundreds of calls in flight.
  3. Replace the queue with a broker that's fair per workspace (Redis Streams or SQS, one stream per busy workspace, a round-robin dispatcher). Keep Postgres as the source of truth for media and `provider_results`.
  4. Then provider quota and cost become the limit. Score fewer frames in videos where the first pass found nothing, and cache results by perceptual hash so re-uploads don't pay twice.

## Time spent

About 1 hour 10 minutes of wall-clock time (22:40–23:50 EAT, 26 Oct 2026), including setup. I worked with Claude Code, which wrote most of the code, tests and docs while I directed and reviewed the work (see `AI.md`). Roughly:
* 30 minutes building the service, the broken handler and the tests
* 20 minutes running the broken and fixed evidence, fixing the recovery bug the tests found, and running the workloads
* 20 minutes on the README, `AI.md` and the usage docs
