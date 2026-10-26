"""Screening worker. A Postgres-backed queue (FOR UPDATE SKIP LOCKED + leases), N
threads per process, plus one sweeper thread that redelivers expired leases, times
out lost callbacks and re-screens media held during an outage once the provider
is healthy again."""

import os
import threading
import time
import uuid

from . import breaker, config, db, provider_client
from .logs import log
from .policy import MalformedResult
from .screening import apply_result, backoff, hold_for_outage, park, requeue

TRANSIENT = ("timeout", "unavailable", "server_error")


def claim(conn) -> dict | None:
    """Fair claim: pick the ready workspace with the fewest jobs in flight (random
    tie-break), skipping any at its in-flight cap, then its oldest ready job. A burst
    in one workspace cannot starve another."""
    ws = conn.execute(
        """WITH ready AS (SELECT DISTINCT workspace_id FROM jobs WHERE status = 'queued' AND run_after <= now()),
                inflight AS (SELECT workspace_id, count(*) AS n FROM jobs WHERE status = 'running' GROUP BY 1)
           SELECT r.workspace_id FROM ready r LEFT JOIN inflight i USING (workspace_id)
           WHERE coalesce(i.n, 0) < %s ORDER BY coalesce(i.n, 0), random() LIMIT 1""",
        (config.MAX_INFLIGHT_PER_WORKSPACE,),
    ).fetchone()
    if not ws:
        return None
    return conn.execute(
        """UPDATE jobs SET status = 'running', lease_token = %s, lease_until = now() + make_interval(secs => %s),
                  updated_at = now()
           WHERE id = (SELECT id FROM jobs WHERE workspace_id = %s AND status = 'queued' AND run_after <= now()
                       ORDER BY run_after, created_at FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING *""",
        (uuid.uuid4(), config.LEASE_S, ws["workspace_id"]),
    ).fetchone()


def process(job: dict) -> None:
    ids = dict(workspace_id=job["workspace_id"], media_id=str(job["media_id"]), job_id=str(job["id"]))
    with db.pool.connection() as conn:
        media = conn.execute("SELECT * FROM media WHERE id = %s", (job["media_id"],)).fetchone()
        stored = conn.execute("SELECT frames FROM provider_results WHERE media_id = %s", (job["media_id"],)).fetchone()
        if media["screened_at"] is not None or media["decided_by"] is not None:
            conn.execute("UPDATE jobs SET status = 'done', done_at = now(), lease_token = NULL WHERE id = %s", (job["id"],))
            log("job_skipped_already_decided", conn=conn, **ids)
            return
        if stored:
            # Scored before but never applied (e.g. crashed between saving and deciding): no new call.
            apply_result(conn, job["media_id"], stored["frames"], via="stored", job_id=ids["job_id"])
            return

        wait = breaker.throttled_for()
        if wait > 0:
            requeue(conn, job, wait, why="provider throttled us (429)")
            return
        if not breaker.allow_call():
            park(conn, job, "provider circuit open", ids)
            return

        attempt = job["attempts"] + 1
        conn.execute("UPDATE jobs SET attempts = %s WHERE id = %s", (attempt, job["id"]))
        ids["attempt"] = attempt
        log("provider_call", conn=conn, frames=media["frame_count"], breaker=breaker.state(), **ids)

    # The provider call happens outside any transaction: no DB connection or row lock is held while waiting.
    t0 = time.monotonic()
    out = provider_client.score(str(job["media_id"]), media["frames"], idempotency_key=str(job["media_id"]))
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _maybe_crash(str(job["media_id"]), out.kind)

    with db.pool.connection() as conn:
        log("provider_answer", conn=conn, outcome=out.kind, elapsed_ms=elapsed_ms, detail=out.detail, **ids)
        if out.kind == "scored":
            try:
                apply_result(conn, job["media_id"], out.frames, request_id=out.request_id, via="sync",
                             job_id=ids["job_id"], attempt=attempt)
            except MalformedResult as e:
                conn.rollback()
                _malformed(conn, job, ids, str(e))
                return
            breaker.record_success()
        elif out.kind == "accepted":
            breaker.record_success()
            row = conn.execute(
                """UPDATE jobs SET status = 'waiting_callback', request_id = %s, lease_token = NULL, lease_until = NULL,
                          callback_deadline = now() + make_interval(secs => %s), updated_at = now()
                   WHERE id = %s AND status = 'running' AND lease_token = %s RETURNING id""",
                (out.request_id, config.CALLBACK_DEADLINE_S, job["id"], job["lease_token"]),
            ).fetchone()
            log("waiting_for_callback", conn=conn, request_id=out.request_id, still_ours=bool(row), **ids)
        elif out.kind == "rate_limited":
            breaker.throttle(out.retry_after or 1.0)
            n = job["rate_limited"] + 1
            if n >= config.MAX_RATE_LIMITED:
                park(conn, job, f"rate limited {n} times", ids, rate_limited=n)
            else:
                requeue(conn, job, out.retry_after or 1.0, why="429", rate_limited=n)
        elif out.kind in TRANSIENT:
            breaker.record_failure()
            _transient(conn, job, ids, out.kind)
        elif out.kind == "malformed":
            _malformed(conn, job, ids, out.detail)
        else:  # client_error: retrying the same request will not help; a human or an engineer must look
            park(conn, job, f"provider rejected request: {out.detail}", ids)


def _transient(conn, job, ids, why: str) -> None:
    n = job["round_failures"] + 1
    hold_for_outage(conn, job["media_id"], why, ids)
    if n >= config.MAX_ATTEMPTS:
        park(conn, job, f"{why} after {n} attempts", ids, round_failures=n)
    else:
        delay = backoff(n)
        if requeue(conn, job, delay, why=why, round_failures=n):
            log("job_retry_scheduled", conn=conn, why=why, delay_s=round(delay, 2), **ids)


def _malformed(conn, job, ids, detail: str) -> None:
    breaker.record_failure()
    n = job["malformed"] + 1
    hold_for_outage(conn, job["media_id"], "malformed provider response", ids)
    if n >= config.MAX_MALFORMED:
        park(conn, job, f"malformed provider response x{n}: {detail}", ids, malformed=n)
    else:
        requeue(conn, job, backoff(n), why=f"malformed: {detail}", malformed=n)


def _maybe_crash(media_id: str, kind: str) -> None:
    """Fault injection for the crash test: die after the provider scored, before saving."""
    if os.environ.get("ALLOW_FAULTS") != "1" or kind != "scored":
        return
    if breaker.r.get("fault:crash_after_score") == media_id:
        breaker.r.delete("fault:crash_after_score")
        log("fault_injected_crash", media_id=media_id)
        os._exit(137)


def sweep() -> None:
    with db.pool.connection() as conn:
        for j in conn.execute(
            """UPDATE jobs SET status = 'queued', lease_token = NULL, lease_until = NULL, run_after = now(),
                      last_error = 'lease expired', updated_at = now()
               WHERE status = 'running' AND lease_until < now() RETURNING id, media_id, workspace_id, attempts"""
        ).fetchall():
            log("lease_expired_redelivered", conn=conn, workspace_id=j["workspace_id"], media_id=str(j["media_id"]),
                job_id=str(j["id"]), attempt=j["attempts"])

        for j in conn.execute(
            """UPDATE jobs SET status = 'running', lease_token = gen_random_uuid(), lease_until = now() + interval '30 seconds'
               WHERE status = 'waiting_callback' AND callback_deadline < now() RETURNING *"""
        ).fetchall():
            ids = dict(workspace_id=j["workspace_id"], media_id=str(j["media_id"]), job_id=str(j["id"]), attempt=j["attempts"])
            log("callback_deadline_passed", conn=conn, request_id=j["request_id"], **ids)
            breaker.record_failure()
            _transient(conn, j, ids, "callback never arrived")

    recover_parked()


def recover_parked() -> None:
    """Re-screen media held because of the outage, without a human, once the provider
    has answered successfully after it was parked."""
    if not breaker.r.set("lock:recover", "1", nx=True, ex=5):
        return
    try:
        last_ok = breaker.last_success()
        with db.pool.connection() as conn:
            if last_ok:
                rows = conn.execute(
                    """UPDATE jobs SET status = 'queued', run_after = now(), parked_at = NULL, round_failures = 0,
                              malformed = 0, rate_limited = 0, rescreen_rounds = rescreen_rounds + 1, updated_at = now()
                       WHERE status = 'parked' AND parked_at < to_timestamp(%s) AND rescreen_rounds < %s
                       RETURNING id, media_id, workspace_id, rescreen_rounds""",
                    (last_ok, config.MAX_RESCREEN_ROUNDS),
                ).fetchall()
                for j in rows:
                    log("rescreen_scheduled", conn=conn, workspace_id=j["workspace_id"], media_id=str(j["media_id"]),
                        job_id=str(j["id"]), round=j["rescreen_rounds"])
            if breaker.state() == "half_open":
                ready = conn.execute(
                    "SELECT 1 FROM jobs WHERE status = 'queued' AND run_after <= now() LIMIT 1").fetchone()
                if not ready:
                    # Nothing to probe with: use the oldest parked job as the probe.
                    j = conn.execute(
                        """UPDATE jobs SET status = 'queued', run_after = now(), parked_at = NULL, round_failures = 0
                           WHERE id = (SELECT id FROM jobs WHERE status = 'parked' AND rescreen_rounds < %s
                                       ORDER BY parked_at LIMIT 1 FOR UPDATE SKIP LOCKED)
                           RETURNING id, media_id, workspace_id""",
                        (config.MAX_RESCREEN_ROUNDS,),
                    ).fetchone()
                    if j:
                        log("probe_scheduled", conn=conn, workspace_id=j["workspace_id"], media_id=str(j["media_id"]),
                            job_id=str(j["id"]))
    finally:
        breaker.r.delete("lock:recover")


def _loop(fn, idle_s: float, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            if not fn():
                stop.wait(idle_s)
        except Exception as e:  # keep the thread alive; the lease makes the job come back
            log("worker_error", level="error", error=f"{type(e).__name__}: {e}")
            stop.wait(1.0)


def work_once() -> bool:
    with db.pool.connection() as conn:
        job = claim(conn)
    if not job:
        return False
    process(job)
    return True


def main() -> None:
    db.open_pool()
    db.init_db()
    if config.HANDLER == "broken":
        from . import broken
        target, sweeper = broken.work_once, (lambda: False)
    else:
        target, sweeper = work_once, (lambda: sweep() or False)
    log("worker_started", handler=config.HANDLER, concurrency=config.WORKER_CONCURRENCY)
    stop = threading.Event()
    threads = [threading.Thread(target=_loop, args=(target, 0.2, stop), daemon=True)
               for _ in range(config.WORKER_CONCURRENCY)]
    threads.append(threading.Thread(target=_loop, args=(sweeper, 1.0, stop), daemon=True))
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
