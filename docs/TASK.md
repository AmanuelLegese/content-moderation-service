# Test task for Senior Full-Stack Engineer @FansCreator

Build a small content-moderation service in FastAPI and React, then use it to reproduce and fix a production incident. Show us how you keep unsafe content from going live, protect valid data and verify recovery.

---

**TL;DR:** Screen uploaded and AI-generated images and videos so that nothing prohibited reaches anyone, nothing explicit reaches an unverified workspace, and nothing goes live unchecked, even when the moderation provider is slow, down or answers twice.

---

You have six-seven 🤷‍♂️ hours, including setup.

We expect working code, evidence that you tested it and a clear explanation of your decisions. Feel free to use AI, documentation and existing packages. You should understand and be able to explain everything you submit, including AI-generated code.

Submit when the time is up. Explain anything unfinished, why you prioritized other work, and what remains broken.

## Setup and scope

Use Python 3.12, FastAPI, PostgreSQL and Redis, with React and TypeScript for the one page of UI. `docker compose up` must start everything. Use any queue or worker library, or write your own.

Keep the application small. It needs workspaces, media, screening jobs, a fake moderation provider, a review queue page and enough logs to follow a job. Login is not required: identify the caller with the `X-User-Id` and `X-Workspace-Id` headers. The UI can be very basic.

**No real images and no real classifier.** A media item is a JSON document listing its frames and the scores the provider will return for each frame. An image has one frame; a video has one frame per second. A label left out of a frame scores 0. Media is either uploaded by a user or made by our AI generator; generated media also carries the prompt it was made from.

```json
{"type": "video", "source": "generation", "prompt": "two adults on a beach at sunset", "frames": [{"explicit": 0.02}, {"explicit": 0.95, "self_harm": 0.1}]}
```

Build the fake provider as its own service in the same compose file. It stands in for whatever classifies content in production: a paid moderation API, models you host yourself, or a mix. Part 3 asks which you would choose. It scores frames by echoing back their scores, always returns every label, and counts every call it receives per media ID. It can be switched to slow, timing out or failing. Videos longer than 30 frames are scored asynchronously: the provider answers `202` and later posts the result to your callback, signed with HMAC-SHA256 over the raw body in the `X-Signature` header.

Seed these on startup: workspace `ws_verified` (verified) with users `admin_v` (admin) and `member_v` (member), and workspace `ws_unverified` (not verified) with `admin_u` and `member_u`.

## What must never get through

Payment processors require adult platforms to block the following for every account, verified or not, both on upload and on generation. Explicit content between consenting adults is allowed, but only in verified workspaces.

- Anyone under 18, or who appears to be: label `minor`
- Sexual violence, or sex without consent: `sexual_violence`
- Sexual content involving animals: `bestiality`
- Self-harm: `self_harm`
- A real, identifiable person who has not consented, such as a celebrity: `real_person`

Generation prompts that ask for any of these, or for incest or trafficking, are refused.

## The rules

Media status is one of `pending`, `published`, `held` or `blocked`.

1. A workspace sees only its own media.
2. A frame scoring 0.8 or more on any prohibited label blocks the media in every workspace. A score from 0.5 up to 0.8 holds it for a human.
3. A frame with `explicit` ≥ 0.8 makes the media explicit. Verified workspaces may publish explicit media. In unverified workspaces it is blocked.
4. A generation prompt asking for prohibited content is refused before anything is generated or scored.
5. If the service cannot get a decision, the media is held. It is never published unchecked.
6. Only workspace admins can approve or reject held media.
7. Provider callbacks without a valid signature are rejected.
8. Each media item is scored successfully at most once. Every call costs money, whether it goes to a paid API or our own GPUs.
9. Every frame of a video counts, not just the beginning.

## The incident

During a 40-minute provider outage last Tuesday, uploads went live without being checked. A video with explicit content in its last minute was published in an unverified workspace. A generation prompt asking for a 16-year-old reached the generator. The provider invoice shows some uploads scored two or three times. Moderators saw "Nothing to review" all morning while 40 items were waiting. There has been no deployment since.

Reproduce it first with a deliberately broken handler. It treats a provider timeout as a pass, scores only the first 8 frames of a video, never checks generation prompts, lets a redelivered job run while the first attempt is still running, and shows a failed review-queue request as an empty list. Record what the broken version does and show the failing tests before you fix it. Do not treat a finished job as proof that the media was screened.

## Required work

### 1. Keep unsafe content out

Make every rule in **The rules** hold, on upload and on generation. Add focused tests for each incident symptom, plus cross-workspace access, an unsigned callback, a member trying to approve and a score that should be held for review. Run them and include the results.

The review queue page must tell three states apart: loading, failed and genuinely empty.

### 2. Keep the queue moving

Handle provider timeouts, 429s, 500s and malformed responses differently, with a limit on retries. When the provider recovers, media held because of the outage must be re-screened without waiting for a human.

Create a repeatable workload with both workspaces. Make one upload a burst of 200 videos while the other uploads single images. Show that the burst does not stop the other workspace's images from being screened, during an outage and after it ends.

Show what happens if a worker dies after the provider has scored a video but before the result is saved. Explain what your local test does and does not prove about a real crash.

Log enough to follow a workspace, media item, job and attempt through a failure and recovery. For the same workload before and after your fix, report: media published unchecked, provider calls per upload and the age of the oldest waiting job.

### 3. Explain cost and the next limit

Answer briefly in the README:

- How would you do the scoring in production: a paid moderation API, models you host yourself, or a mix? It must cover every category in **What must never get through**. Roughly what would 10,000 images and 1,000 one-minute videos a month cost, and how many frames per video would you check?
- How would you check generation prompts in production, including people trying to get around the check?
- How would you add CSAM hash matching, and what should happen while that service is down?
- At 1 million uploads a day, what breaks first in your design, what would you measure to confirm it, and what would you change first?

## API we will test

Keep these routes and shapes so we can run our own checks against your service. Everything else is up to you. API on port 8000, fake provider on port 8001.

| Route | Who | Returns |
| --- | --- | --- |
| `POST /media` with the media JSON | Any member | `202` with `{"id", "status": "pending"}`; `422` with `{"reasons"}` when a generation prompt is refused |
| `GET /media/{id}` | Members of the owning workspace | `200` with `{"id", "status", "reasons"}`; `404` for anyone else |
| `GET /review` | Admins | `200` with the held media of their workspace |
| `POST /review/{id}/approve` and `POST /review/{id}/reject` | Admins | `200`; `403` for members |
| `POST /provider/callback` | The provider | `200`; `401` without a valid signature |
| Provider `POST /_control` with `{"mode": "ok" \| "slow" \| "timeout" \| "error", "delay_ms": 0}` | Our checks | Sets the provider's behaviour |
| Provider `GET /_stats` | Our checks | `{"calls": {"<media id>": 1}}` |

## What to submit

Email join@fanscreator.ai a download link to `Firstname_Lastname.zip`, including `.git` so we can see your commits. Include the source, dependency lockfiles, setup instructions, regression tests and a short README.

The README should explain the failure, evidence, fix, before-and-after results, remaining limits, time spent and your answers to part 3. Include a short production plan: what you would check and mitigate in the first 15 minutes, how you would roll out the fix, when you would roll back and how you would verify recovery.

In `AI.md`, briefly explain which tools you used, what you checked and any incorrect suggestion you caught. Say what you have not verified.

This is a standalone exercise. We will not use your code in our product.

## How we assess it

- The rules hold under our own checks, including provider failures: 60%.
- Queue recovery, retries, duplicate work and workspace isolation under load: 20%.
- Cost judgment, logging and explanation: 20%.

We review the submission and have a 30-minute call. You will reproduce one failure and walk through the fix. We will also ask how you would investigate a new symptom, and discuss a production incident you have worked on.
