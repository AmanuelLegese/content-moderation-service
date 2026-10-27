"""HTTP API (FastAPI). The routes and response shapes the brief says will be tested,
plus a few extras for following jobs (/media list, /events, /health, /metrics).

What it does NOT do: call the moderation provider. POST /media only stores the media
and queues a job; workers do the screening. That keeps uploads fast and means a slow
or dead provider can never block, time out or (as in the incident) publish an upload.

Most endpoints are plain `def`: FastAPI runs them in a thread pool, which suits the
synchronous psycopg driver. Only the callback is `async`, because it must read the
raw request body to verify the signature.
"""

import json
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from . import breaker, config, db
from .logs import log
from .policy import MalformedResult
from .prompts import check_prompt
from .screening import apply_result, create_media
from .signing import verify


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once per API process: connect, create schema + seed, then serve; close the pool on shutdown."""
    db.open_pool()
    db.init_db()
    log("api_started", handler=config.HANDLER)
    yield
    db.pool.close()


app = FastAPI(title="Moderation service", lifespan=lifespan)


class MediaIn(BaseModel):
    """The media JSON document from the brief. Pydantic rejects anything else with 422."""
    type: Literal["image", "video"]
    source: Literal["upload", "generation"] = "upload"
    prompt: str | None = None
    # Each frame maps label -> score. Max 4 hours of video at 1 frame per second.
    frames: list[dict[str, float]] = Field(min_length=1, max_length=4 * 3600)

    @model_validator(mode="after")
    def _shape(self):
        """Cross-field checks that a type annotation can't express."""
        if self.type == "image" and len(self.frames) != 1:
            raise ValueError("an image has exactly one frame")
        if self.source == "generation" and not (self.prompt and self.prompt.strip()):
            raise ValueError("generated media must carry its prompt")
        for i, f in enumerate(self.frames):
            for label, score in f.items():
                if not 0 <= score <= 1:
                    raise ValueError(f"frame {i} label {label}: score must be between 0 and 1")
        return self


class Caller(BaseModel):
    user_id: str
    workspace_id: str
    role: str


def caller(x_user_id: str | None = Header(None), x_workspace_id: str | None = Header(None)) -> Caller:
    """Who is calling? FastAPI dependency used by every user-facing route.

    No login (per the brief): identity comes from X-User-Id / X-Workspace-Id. We still
    check the pair exists, so nobody can claim to be in a workspace they aren't in.
    """
    if not x_user_id or not x_workspace_id:
        raise HTTPException(401, "X-User-Id and X-Workspace-Id headers are required")
    with db.pool.connection() as conn:
        u = conn.execute("SELECT role FROM users WHERE id = %s AND workspace_id = %s",
                         (x_user_id, x_workspace_id)).fetchone()
    if not u:
        raise HTTPException(403, "user is not a member of this workspace")
    return Caller(user_id=x_user_id, workspace_id=x_workspace_id, role=u["role"])


def admin(c: Caller = Depends(caller)) -> Caller:
    """Rule 6: only workspace admins can see the review queue or approve/reject."""
    if c.role != "admin":
        raise HTTPException(403, "only workspace admins can review media")
    return c


def _public(m: dict) -> dict:
    """What clients see of a media row. Always includes id/status/reasons (the tested shape).
    `screened` is true only when a complete provider result was applied."""
    return {
        "id": str(m["id"]), "status": m["status"], "reasons": m["reasons"],
        "workspace_id": m["workspace_id"], "type": m["type"], "source": m["source"],
        "frame_count": m["frame_count"], "hold_kind": m["hold_kind"], "explicit": m["explicit"],
        "screened": m["screened_at"] is not None, "decided_by": m["decided_by"],
        "created_at": m["created_at"].isoformat(), "updated_at": m["updated_at"].isoformat(),
    }


@app.post("/media", status_code=202)
def post_media(item: MediaIn, c: Caller = Depends(caller)):
    """Upload or generate media. Returns 202 + pending; screening happens in the worker."""
    # Rule 4: refuse prohibited prompts BEFORE anything is stored, generated or scored.
    # A refused prompt creates no media and no job, so nothing downstream can ever see it.
    if item.source == "generation" and config.HANDLER != "broken":  # INCIDENT 3 when broken
        reasons = check_prompt(item.prompt or "")
        if reasons:
            log("generation_refused", workspace_id=c.workspace_id, user_id=c.user_id, reasons=reasons)
            return JSONResponse({"reasons": reasons}, status_code=422)
    with db.pool.connection() as conn:
        created = create_media(conn, c.workspace_id, c.user_id, item.model_dump())
    return {"id": created["id"], "status": "pending"}


def _get_own(conn, media_id: str, c: Caller, lock: bool = False) -> dict:
    """Load media only if it belongs to the caller's workspace (rule 1). `lock` = FOR UPDATE,
    used by approve/reject so a provider result can't land halfway through a human decision."""
    try:
        row = conn.execute(
            "SELECT * FROM media WHERE id = %s::uuid AND workspace_id = %s" + (" FOR UPDATE" if lock else ""),
            (media_id, c.workspace_id),
        ).fetchone()
    except Exception:
        # Not a valid UUID: Postgres rejects the cast. Treat it like any unknown id.
        conn.rollback()
        row = None
    if not row:
        # 404, not 403: another workspace's media does not exist as far as the caller can tell
        raise HTTPException(404, "media not found")
    return row


@app.get("/media")
def list_media(limit: int = 50, c: Caller = Depends(caller)):
    """Newest media of the caller's workspace (used by the UI)."""
    with db.pool.connection() as conn:
        rows = conn.execute("SELECT * FROM media WHERE workspace_id = %s ORDER BY created_at DESC LIMIT %s",
                            (c.workspace_id, min(limit, 200))).fetchall()
    return [_public(r) for r in rows]


@app.get("/media/{media_id}")
def get_media(media_id: str, c: Caller = Depends(caller)):
    with db.pool.connection() as conn:
        return _public(_get_own(conn, media_id, c))


@app.get("/media/{media_id}/events")
def media_events(media_id: str, c: Caller = Depends(caller)):
    """The full job log for one item: every call, answer, retry, park and decision, in order."""
    with db.pool.connection() as conn:
        _get_own(conn, media_id, c)  # same workspace check as GET /media/{id}
        rows = conn.execute("SELECT ts, event, job_id, attempt, data FROM events WHERE media_id = %s ORDER BY id",
                            (media_id,)).fetchall()
    return [{**r, "ts": r["ts"].isoformat(), "job_id": str(r["job_id"]) if r["job_id"] else None} for r in rows]


@app.get("/review")
def review_queue(c: Caller = Depends(admin)):
    """All held media of the admin's workspace: held for review AND held because the provider
    was down. Including the second kind fixes "Nothing to review" while items were waiting."""
    with db.pool.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM media WHERE workspace_id = %s AND status = 'held' ORDER BY created_at",
            (c.workspace_id,),
        ).fetchall()
    return [_public(r) for r in rows]


@app.post("/review/{media_id}/approve")
def approve(media_id: str, c: Caller = Depends(admin)):
    """Admin publishes held media."""
    with db.pool.connection() as conn:
        m = _get_own(conn, media_id, c, lock=True)
        if m["status"] != "held":
            raise HTTPException(409, f"media is {m['status']}, not held")
        if m["screened_at"] is None:
            # Rule 5: a human cannot see a JSON document; approving unscreened media would publish it unchecked.
            raise HTTPException(409, "media has not been screened yet; it will be re-screened automatically")
        # decided_by marks it as a human decision: later provider answers are ignored (apply_result).
        conn.execute(
            "UPDATE media SET status = 'published', hold_kind = NULL, decided_by = %s, updated_at = now() WHERE id = %s",
            (c.user_id, m["id"]),
        )
        log("review_approved", conn=conn, workspace_id=c.workspace_id, media_id=str(m["id"]), user_id=c.user_id)
        return _public(conn.execute("SELECT * FROM media WHERE id = %s", (m["id"],)).fetchone())


@app.post("/review/{media_id}/reject")
def reject(media_id: str, c: Caller = Depends(admin)):
    """Admin blocks held media. Allowed even when unscreened: taking something down is always safe."""
    with db.pool.connection() as conn:
        m = _get_own(conn, media_id, c, lock=True)
        if m["status"] != "held":
            raise HTTPException(409, f"media is {m['status']}, not held")
        # `reasons || %s` appends to the JSON array, keeping the provider's reasons for the audit trail.
        conn.execute(
            """UPDATE media SET status = 'blocked', hold_kind = NULL, decided_by = %s,
                      reasons = reasons || %s, updated_at = now() WHERE id = %s""",
            (c.user_id, Jsonb([f"rejected by {c.user_id}"]), m["id"]),
        )
        # Stop spending provider calls on media a human already rejected
        conn.execute("UPDATE jobs SET status = 'done', done_at = now(), lease_token = NULL WHERE media_id = %s "
                     "AND status <> 'done'", (m["id"],))
        log("review_rejected", conn=conn, workspace_id=c.workspace_id, media_id=str(m["id"]), user_id=c.user_id)
        return _public(conn.execute("SELECT * FROM media WHERE id = %s", (m["id"],)).fetchone())


@app.post("/provider/callback")
async def provider_callback(request: Request, x_signature: str | None = Header(None)):
    """Results for long videos (> 30 frames), posted by the provider some time after its 202."""
    # Rule 7: verify the signature over the exact raw bytes, before parsing anything.
    body = await request.body()
    if not verify(body, x_signature, config.WEBHOOK_SECRET):
        log("callback_rejected_bad_signature", level="warning", has_signature=bool(x_signature), size=len(body))
        raise HTTPException(401, "invalid signature")
    try:
        data = json.loads(body)
        media_id, frames = data["media_id"], data["frames"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "callback body must be JSON with media_id and frames")

    def _apply():
        with db.pool.connection() as conn:
            try:
                # The same idempotent path as sync answers: a duplicate or late callback is a no-op.
                return apply_result(conn, media_id, frames, request_id=data.get("request_id"), via="callback")
            except MalformedResult:
                # the stored event rolls back with the transaction; the job's callback deadline will resend
                return "malformed"

    # Database work is synchronous: run it in a thread so the async event loop isn't blocked.
    try:
        outcome = await run_in_threadpool(_apply)
    except Exception as e:
        log("callback_error", level="error", media_id=media_id, error=f"{type(e).__name__}: {e}")
        # 500 makes a real webhook sender retry later, which is what we want for a transient DB error.
        raise HTTPException(500, "could not apply result")
    if outcome == "unknown_media":
        raise HTTPException(404, "unknown media")
    if outcome == "malformed":
        raise HTTPException(422, "malformed result")
    # A valid new result proves the provider works: close the breaker, trigger re-screening.
    if outcome != "duplicate":
        breaker.record_success()
    return {"ok": True, "outcome": outcome}


@app.get("/health")
def health():
    """Liveness for Docker's health check; also shows which handler runs and the breaker state."""
    with db.pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True, "handler": config.HANDLER, "breaker": breaker.state()}


@app.get("/metrics")
def metrics(since: float = 0):
    """The three numbers the brief asks for, computed from data rather than job status:
    a 'done' job proves nothing about whether the media was screened.

    `since` (unix time) limits the counts to media created after it, so the workload
    script can measure only its own run.
    """
    with db.pool.connection() as conn:
        # published_unchecked: published, but no stored result, or a result covering fewer
        # frames than the media has. This is independent of anything the handler claims.
        row = conn.execute(
            """SELECT
                 count(*) FILTER (WHERE m.status = 'published' AND (r.media_id IS NULL OR r.frames_scored < m.frame_count))
                   AS published_unchecked,
                 count(*) AS media_total,
                 count(*) FILTER (WHERE m.status = 'pending') AS pending,
                 count(*) FILTER (WHERE m.status = 'held') AS held,
                 count(*) FILTER (WHERE m.status = 'held' AND m.hold_kind = 'provider_unavailable') AS held_no_decision,
                 count(*) FILTER (WHERE m.status = 'published') AS published,
                 count(*) FILTER (WHERE m.status = 'blocked') AS blocked
               FROM media m LEFT JOIN provider_results r ON r.media_id = m.id
               WHERE m.created_at >= to_timestamp(%s)""",
            (since,),
        ).fetchone()
        # Age of the oldest job not finished yet (parked jobs count: they are still waiting).
        oldest = conn.execute(
            """SELECT extract(epoch FROM now() - min(created_at)) AS s FROM jobs
               WHERE status IN ('queued', 'running', 'waiting_callback', 'parked') AND created_at >= to_timestamp(%s)""",
            (since,),
        ).fetchone()
        # The same question asked of the media rather than the jobs: what has no decision yet?
        undecided = conn.execute(
            """SELECT extract(epoch FROM now() - min(created_at)) AS s FROM media
               WHERE (status = 'pending' OR (status = 'held' AND hold_kind = 'provider_unavailable'))
                 AND created_at >= to_timestamp(%s)""",
            (since,),
        ).fetchone()
        jobs = conn.execute("SELECT status, count(*) AS n FROM jobs WHERE created_at >= to_timestamp(%s) GROUP BY status",
                            (since,)).fetchall()
    return {**row, "oldest_waiting_job_s": float(oldest["s"] or 0),
            "oldest_undecided_media_s": float(undecided["s"] or 0), "jobs": {j["status"]: j["n"] for j in jobs},
            "breaker": breaker.state()}
