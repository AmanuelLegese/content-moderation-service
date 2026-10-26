"""DELIBERATELY BROKEN handler: reproduces the Tuesday incident. Enabled with
HANDLER=broken. Never use it for anything else. Each bug is marked INCIDENT.

1. A provider timeout is treated as a pass.
2. Only the first 8 frames of a video are scored.
3. Generation prompts are never checked (see api.py: the check is skipped when HANDLER=broken).
4. The queue visibility timeout (1s) is shorter than a slow provider call and nothing
   checks whether the media was already scored, so a redelivered job runs while the
   first attempt is still in flight and the provider is paid two or three times.
5. The review page shows a failed request as an empty list (ui/src/ReviewQueue.tsx, broken prop).
"""

import uuid

from psycopg.types.json import Jsonb

from . import config, db, provider_client
from .logs import log
from .policy import decide


def claim(conn) -> dict | None:
    # INCIDENT 4: a "running" job becomes visible again after 1s, even if it is still running.
    return conn.execute(
        """UPDATE jobs SET status = 'running', lease_token = %s, lease_until = now() + make_interval(secs => %s),
                  attempts = attempts + 1, updated_at = now()
           WHERE id = (SELECT id FROM jobs
                       WHERE (status = 'queued' AND run_after <= now()) OR (status = 'running' AND lease_until < now())
                       ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING *""",
        (uuid.uuid4(), config.BROKEN_VISIBILITY_S),
    ).fetchone()


def work_once() -> bool:
    with db.pool.connection() as conn:
        job = claim(conn)
        if not job:
            return False
        media = conn.execute(
            "SELECT m.*, w.verified FROM media m JOIN workspaces w ON w.id = m.workspace_id WHERE m.id = %s",
            (job["media_id"],),
        ).fetchone()
    ids = dict(workspace_id=job["workspace_id"], media_id=str(job["media_id"]), job_id=str(job["id"]),
               attempt=job["attempts"])

    frames = media["frames"][: config.BROKEN_MAX_FRAMES]  # INCIDENT 2
    log("provider_call", frames=len(frames), handler="broken", **ids)
    out = provider_client.score(str(job["media_id"]), frames, idempotency_key=str(uuid.uuid4()))
    log("provider_answer", outcome=out.kind, detail=out.detail, handler="broken", **ids)

    with db.pool.connection() as conn:
        if out.kind == "timeout":
            # INCIDENT 1: no answer is read as "nothing found".
            _set(conn, job, "published", [], False)
            log("media_decided", conn=conn, status="published", via="timeout_as_pass", **ids)
        elif out.kind == "scored" and isinstance(out.frames, list):
            d = decide(out.frames, media["verified"])
            conn.execute(
                """INSERT INTO provider_results (media_id, frames, frames_scored, via) VALUES (%s, %s, %s, 'sync')
                   ON CONFLICT (media_id) DO NOTHING""",
                (job["media_id"], Jsonb(out.frames), len(out.frames)),
            )
            _set(conn, job, d.status, d.reasons, d.explicit)
            log("media_decided", conn=conn, status=d.status, frames_scored=len(out.frames), **ids)
        else:
            # Errors are retried forever, one second apart.
            conn.execute("UPDATE jobs SET status = 'queued', run_after = now() + interval '1 second' WHERE id = %s",
                         (job["id"],))
    return True


def _set(conn, job, status, reasons, explicit) -> None:
    conn.execute(
        """UPDATE media SET status = %s, reasons = %s, explicit = %s, hold_kind = %s, screened_at = now(),
                  updated_at = now() WHERE id = %s""",
        (status, Jsonb(reasons), explicit, "review" if status == "held" else None, job["media_id"]),
    )
    conn.execute("UPDATE jobs SET status = 'done', done_at = now() WHERE id = %s", (job["id"],))
