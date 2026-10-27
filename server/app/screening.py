"""State changes for media and jobs. Every function takes an open connection and
runs inside the caller's transaction.

Invariants:
- media goes to `published` only through apply_result with a complete, validated
  result for every frame, or through an admin approving media that was screened.
- provider_results has at most one row per media: the result we paid for. Any later
  answer for the same media (duplicate callback, redelivered job) is ignored.

Why one module for this: the API (callbacks), the worker (sync answers) and the
sweeper all change media and job state. If each wrote its own UPDATEs, the
invariants above would be spread over three files; here they are enforced once.
"""

import random
import uuid

from psycopg.types.json import Jsonb

from . import config
from .logs import log
from .policy import MalformedResult, decide, validate_result

# The reason shown to moderators for media we could not screen yet.
OUTAGE_REASON = "no moderation decision yet ({why}); held until the provider answers, re-screened automatically"


def create_media(conn, workspace_id: str, user_id: str, item: dict) -> dict:
    """Store a new media item as `pending` and queue its screening job.

    Both rows go in the same transaction, so there is never media without a job
    (which would stay pending forever) or a job without media.
    """
    media_id = uuid.uuid4()
    job_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO media (id, workspace_id, created_by, type, source, prompt, frames, frame_count, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')""",
        (media_id, workspace_id, user_id, item["type"], item["source"], item.get("prompt"),
         Jsonb(item["frames"]), len(item["frames"])),
    )
    conn.execute(
        "INSERT INTO jobs (id, media_id, workspace_id, status) VALUES (%s, %s, %s, 'queued')",
        (job_id, media_id, workspace_id),
    )
    log("media_created", conn=conn, workspace_id=workspace_id, media_id=str(media_id), job_id=str(job_id),
        type=item["type"], source=item["source"], frames=len(item["frames"]), user_id=user_id)
    return {"id": str(media_id), "job_id": str(job_id)}


def lock_media(conn, media_id) -> dict | None:
    """Load a media row and lock it until the transaction ends.

    FOR UPDATE makes concurrent writers for the same media (a callback arriving while
    a worker applies a sync answer, two duplicate callbacks at once) take turns, so
    the second one sees the first one's result and becomes a no-op.
    """
    return conn.execute(
        """SELECT m.*, w.verified FROM media m JOIN workspaces w ON w.id = m.workspace_id
           WHERE m.id = %s FOR UPDATE OF m""",
        (media_id,),
    ).fetchone()


def store_result(conn, media: dict, raw_frames, request_id: str | None, via: str) -> list[dict]:
    """Validate and store the first complete result for this media. Returns the stored
    frames (an earlier stored result wins over this one). Raises MalformedResult."""
    existing = conn.execute("SELECT frames FROM provider_results WHERE media_id = %s", (media["id"],)).fetchone()
    if existing:
        return existing["frames"]
    # Validate against the number of frames the media really has: a partial answer raises (rule 9).
    frames = validate_result(raw_frames, media["frame_count"])
    # ON CONFLICT DO NOTHING is a second safety net behind the media row lock.
    conn.execute(
        """INSERT INTO provider_results (media_id, request_id, frames, frames_scored, via)
           VALUES (%s, %s, %s, %s, %s) ON CONFLICT (media_id) DO NOTHING""",
        (media["id"], request_id, Jsonb(frames), len(frames), via),
    )
    return frames


def apply_result(conn, media_id, raw_frames, *, request_id=None, via: str, job_id=None, attempt=None) -> str:
    """Apply a provider result. Idempotent: safe for duplicate callbacks, redelivered
    jobs and late answers. Returns what happened, for logging and tests.

    `via` records where the answer came from: "sync" (worker), "callback" (async
    webhook) or "stored" (an earlier answer that was saved but never applied).
    """
    media = lock_media(conn, media_id)
    if media is None:
        return "unknown_media"
    ids = dict(workspace_id=media["workspace_id"], media_id=str(media_id), job_id=job_id, attempt=attempt)
    try:
        frames = store_result(conn, media, raw_frames, request_id, via)
    except MalformedResult as e:
        log("provider_result_malformed", conn=conn, level="warning", via=via, error=str(e), **ids)
        raise

    # Already decided (by an earlier answer or by an admin): never decide twice.
    # This is what makes "the provider answers twice" harmless.
    if media["screened_at"] is not None or media["decided_by"] is not None:
        log("provider_result_duplicate_ignored", conn=conn, via=via, status=media["status"], **ids)
        _finish_job(conn, media_id)
        return "duplicate"

    d = decide(frames, media["verified"])
    # screened_at is set only here, i.e. only with a validated result covering every frame.
    conn.execute(
        """UPDATE media SET status = %s, reasons = %s, explicit = %s, hold_kind = %s,
                  screened_at = now(), updated_at = now() WHERE id = %s""",
        (d.status, Jsonb(d.reasons), d.explicit, "review" if d.status == "held" else None, media_id),
    )
    _finish_job(conn, media_id)
    log("media_decided", conn=conn, via=via, status=d.status, reasons=d.reasons, explicit=d.explicit,
        frames_scored=len(frames), **ids)
    return d.status


def _finish_job(conn, media_id) -> None:
    """Mark the media's job done whatever state it was in (a callback can finish a job that
    is still waiting, queued for retry or parked). coalesce keeps the first completion time."""
    conn.execute(
        """UPDATE jobs SET status = 'done', done_at = coalesce(done_at, now()), lease_token = NULL,
                  lease_until = NULL, updated_at = now() WHERE media_id = %s""",
        (media_id,),
    )


def hold_for_outage(conn, media_id, why: str, ids: dict) -> None:
    """Rule 5: no decision means held, never published. Only touches media that has
    no decision yet.

    Called on the first failure, not after all retries: moderators see the item in the
    review queue straight away (the incident's "Nothing to review" while 40 waited).
    The WHERE clause makes it impossible to overwrite a real decision.
    """
    row = conn.execute(
        """UPDATE media SET status = 'held', hold_kind = 'provider_unavailable', reasons = %s, updated_at = now()
           WHERE id = %s AND screened_at IS NULL AND decided_by IS NULL AND status IN ('pending', 'held')
           RETURNING status""",
        (Jsonb([OUTAGE_REASON.format(why=why)]), media_id),
    ).fetchone()
    if row:
        log("media_held_no_decision", conn=conn, why=why, **ids)


def requeue(conn, job: dict, delay_s: float, *, why: str, **counters) -> bool:
    """Put a running job back in the queue to retry after `delay_s`, updating any counters
    passed as keyword arguments (e.g. round_failures=2).

    Fenced by the lease: the WHERE requires our lease_token. If our lease expired and
    another worker took the job, this updates nothing and returns False, so a slow
    worker can never undo a newer worker's progress.
    """
    # Counter names come from our own code (never user input), so building the SET list is safe.
    sets = ", ".join(f"{k} = %s" for k in counters)
    params = list(counters.values())
    row = conn.execute(
        f"""UPDATE jobs SET status = 'queued', run_after = now() + make_interval(secs => %s),
                   lease_token = NULL, lease_until = NULL, last_error = %s, updated_at = now()
                   {', ' + sets if sets else ''}
            WHERE id = %s AND status = 'running' AND lease_token = %s RETURNING id""",
        [delay_s, why, *params, job["id"], job["lease_token"]],
    ).fetchone()
    return row is not None


def park(conn, job: dict, why: str, ids: dict, **counters) -> None:
    """Stop retrying for now. The media is held and the job waits for the provider to
    recover (worker.recover_parked).

    Why park instead of retrying forever: the broken handler retried every second
    without limit, which multiplied the provider bill during the outage. Parked jobs
    cost nothing until there is evidence the provider works again.
    """
    sets = ", ".join(f"{k} = %s" for k in counters)
    # Same lease fencing as requeue().
    row = conn.execute(
        f"""UPDATE jobs SET status = 'parked', parked_at = now(), lease_token = NULL, lease_until = NULL,
                   last_error = %s, updated_at = now() {', ' + sets if sets else ''}
            WHERE id = %s AND status = 'running' AND lease_token = %s RETURNING id""",
        [why, *counters.values(), job["id"], job["lease_token"]],
    ).fetchone()
    if row:
        hold_for_outage(conn, job["media_id"], why, ids)
        log("job_parked", conn=conn, why=why, **ids)


def backoff(n: int) -> float:
    """Delay before retry number n: 1s, 2s, 4s ... capped, times a random 0.5-1.0.

    The random factor (jitter) spreads retries out, so hundreds of jobs that failed in
    the same second don't all hit a recovering provider in the same second again.
    """
    return min(config.BACKOFF_MAX_S, config.BACKOFF_BASE_S * 2 ** max(0, n - 1)) * (0.5 + random.random() / 2)
