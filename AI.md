# AI use

## Tools

**Claude Code** (Claude Opus 5.5) was used in the terminal and in VS Code. It wrote most of the code, tests, scripts and docs from the brief, and it ran the commands: Docker Compose, pytest, vitest and the workload. I directed the work and reviewed the result. Everything submitted is meant to be explainable line by line. The parts worth reading first:
* `server/app/worker.py`: claim, process, sweep and `recover_parked`
* `server/app/screening.py`: `apply_result`
* `server/app/breaker.py`

## What was checked, and how

* **Every rule and incident symptom has a test that ran against the live stack.** First against `HANDLER=broken`, where 16 failed, each for its incident reason (`evidence/1-broken-integration-tests.txt`). Then against the fixed handler, where all 44 passed (`evidence/2-…`). A test counts only if it fails against the broken handler for the right reason. I read each failure message to confirm that.
* **Outcomes are checked against known answers,** not against the code's own view of itself. The workload computes the correct decision for every item from the input scores over all frames and compares it with the final status. "Published unchecked" is computed from stored results, not from job status.
* **The UI was checked through the nginx container** (`/api/review` through port 3000), not only with unit tests.

## Incorrect suggestions and bugs caught

1. **Prompt patterns that matched too much** (AI-written): `looks like <word> <word>` would refuse "looks like a goddess", and a bare `N years` would treat "married for 5 years" as a minor. Caught by reading the patterns, and fixed before the tests were written. The allowed-prompt tests now guard against this.
2. **Re-screening never fired in a quiet system** (AI-written design flaw). Parked jobs waited for "a success after they were parked". Two malformed answers never open the breaker, so with no other traffic that success never came and the media stayed held forever. Caught by `test_malformed_response_is_not_a_pass` on the first fixed run; fixed in `c46cbf0` with a rate-limited probe.
3. **Invalid reproduction run.** `docker compose run tests` silently recreated the API with the fixed handler because `HANDLER` wasn't exported in that shell. The broken-handler run therefore showed the minor-prompt test *passing*. Caught because that result was impossible. Re-ran with the variable exported, and documented the trap in the README.
4. **Wrong metric in the workload script.** "Time to first status change" read the second entry in the history. For items first seen as already `held`, that was the later `held → published` change, so it reported 20.8 s when the real time to `held` was about 0.9 s. Caught by printing the per-item timelines.
5. **Misleading metric name.** "Uploads scored more than once" was counting *calls*, including calls that timed out. Checked against the provider's `scored` counter (0 media scored successfully twice) and renamed it `uploads_called_more_than_once`.
6. **UI proxy returned 502 after the API was recreated.** nginx had kept the old container IP. Caught by checking the UI through port 3000 rather than trusting the unit tests. Fixed by having nginx re-resolve the address through Docker's DNS.
7. **A test patched `httpx.AsyncClient` globally,** which broke the test's own client. Fixed by patching only the provider module's reference.

## Not verified

* **The prices and vendor capabilities in README part 3** are from memory, not current price sheets.
* **The `integration` CI job has never run on GitHub.** The unit and UI jobs mirror commands that pass locally.
* **Prompt screening has only been tested against the evasion list in `tests/unit/test_prompts.py`,** not red-teamed.
* **Behaviour with more than 2 worker replicas, Postgres or Redis outages, and sustained 429s at volume** is untested.
* **The crash test covers one injected exit.** It doesn't cover partitions or hung workers; see the README.
* **The UI was not clicked through in a real browser.** It was checked with jsdom tests and HTTP requests through the container.
