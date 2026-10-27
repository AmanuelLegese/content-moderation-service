"""Structured logs. Every line carries the ids needed to follow one job:
workspace_id, media_id, job_id, attempt. Events are also stored in the `events`
table so GET /media/{id}/events and the metrics script can replay a job.

Why both stdout and a table: stdout JSON is what a real log pipeline would ingest
(`docker compose logs worker | grep <media id>`); the table lets the API and the
tests read one item's history without access to container logs.
"""

import json
import logging
import sys
import time

from psycopg.types.json import Jsonb

# One logger writing bare JSON lines to stdout (Docker collects stdout).
# The guard stops duplicate handlers if the module is imported more than once.
_log = logging.getLogger("moderation")
if not _log.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(h)
    _log.setLevel(logging.INFO)
    _log.propagate = False


def log(event: str, *, conn=None, level: str = "info", **fields) -> None:
    """Write one event as a JSON line, and also to the events table when `conn` is given.

    Passing `conn` puts the event in the caller's transaction: it is stored only if
    the state change it describes commits, so the stored history never claims
    something that was rolled back. Events without a media_id (e.g. worker_started)
    go to stdout only.
    """
    rec = {"ts": round(time.time(), 3), "level": level, "event": event, **fields}
    _log.info(json.dumps(rec, default=str))
    if conn is not None and fields.get("media_id"):
        conn.execute(
            "INSERT INTO events (media_id, job_id, attempt, event, data) VALUES (%s, %s, %s, %s, %s)",
            (fields.get("media_id"), fields.get("job_id"), fields.get("attempt"), event, Jsonb(fields)),
        )
