"""Structured logs. Every line carries the ids needed to follow one job:
workspace_id, media_id, job_id, attempt. Events are also stored in the `events`
table so GET /media/{id}/events and the metrics script can replay a job."""

import json
import logging
import sys
import time

from psycopg.types.json import Jsonb

_log = logging.getLogger("moderation")
if not _log.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(h)
    _log.setLevel(logging.INFO)
    _log.propagate = False


def log(event: str, *, conn=None, level: str = "info", **fields) -> None:
    rec = {"ts": round(time.time(), 3), "level": level, "event": event, **fields}
    _log.info(json.dumps(rec, default=str))
    if conn is not None and fields.get("media_id"):
        conn.execute(
            "INSERT INTO events (media_id, job_id, attempt, event, data) VALUES (%s, %s, %s, %s, %s)",
            (fields.get("media_id"), fields.get("job_id"), fields.get("attempt"), event, Jsonb(fields)),
        )
