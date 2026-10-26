"""State changes for media and jobs. Every function takes an open connection and
runs inside the caller's transaction.

Invariants:
- media goes to `published` only through apply_result with a complete, validated
  result for every frame, or through an admin approving media that was screened.
- provider_results has at most one row per media: the result we paid for. Any later
  answer for the same media (duplicate callback, redelivered job) is ignored.
"""

import random
import uuid

from psycopg.types.json import Jsonb

from . import config
from .logs import log
from .policy import MalformedResult, decide, validate_result

OUTAGE_REASON = "no moderation decision yet ({why}); held until the provider answers, re-screened automatically"


def create_media(conn, workspace_id: str, user_id: str, item: dict) -> dict:
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
    frames = validate_result(raw_frames, media["frame_count"])
    conn.execute(
        """INSERT INTO provider_results (media_id, request_id, frames, frames_scored, via)
           VALUES (%s, %s, %s, %s, %s) ON CONFLICT (media_id) DO NOTHING""",
        (media["id"], request_id, Jsonb(frames), len(frames), via),
    )
    return frames


def apply_result(conn, media_id, raw_frames, *, request_id=None, via: str, job_id=None, attempt=None) -> str:
    """Apply a provider result. Idempotent: safe for duplicate callbacks, redelivered
    jobs and late answers. Returns what happened, for logging and tests."""
    media = lock_media(conn, media_id)
    if media is None:
        return "unknown_media"
    ids = dict(workspace_id=media["workspace_id"], media_id=str(media_id), job_id=job_id, attempt=attempt)
    try:
        frames = store_result(conn, media, raw_frames, request_id, via)
    except MalformedResult as e:
        log("provider_result_malformed", conn=conn, level="warning", via=via, error=str(e), **ids)
        raise

    if media["screened_at"] is not None or media["decided_by"] is not None:
        log("provider_result_duplicate_ignored", conn=conn, via=via, status=media["status"], **ids)
        _finish_job(conn, media_id)
        return "duplicate"

    d = decide(frames, media["verified"])
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
    conn.execute(
        """UPDATE jobs SET status = 'done', done_at = coalesce(done_at, now()), lease_token = NULL,
                  lease_until = NULL, updated_at = now() WHERE media_id = %s""",
        (media_id,),
    )


def hold_for_outage(conn, media_id, why: str, ids: dict) -> None:
    """Rule 5: no decision means held, never published. Only touches media that has
    no decision yet."""
    row = conn.execute(
        """UPDATE media SET status = 'held', hold_kind = 'provider_unavailable', reasons = %s, updated_at = now()
           WHERE id = %s AND screened_at IS NULL AND decided_by IS NULL AND status IN ('pending', 'held')
           RETURNING status""",
        (Jsonb([OUTAGE_REASON.format(why=why)]), media_id),
    ).fetchone()
    if row:
        log("media_held_no_decision", conn=conn, why=why, **ids)


def requeue(conn, job: dict, delay_s: float, *, why: str, **counters) -> bool:
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
    recover (worker.recover_parked)."""
    sets = ", ".join(f"{k} = %s" for k in counters)
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
    return min(config.BACKOFF_MAX_S, config.BACKOFF_BASE_S * 2 ** max(0, n - 1)) * (0.5 + random.random() / 2)
