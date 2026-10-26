"""Fake moderation provider. Echoes each frame's scores back with every label filled
in, counts every call per media id, and can be switched to slow, timeout, error,
rate_limit or malformed. Videos longer than 30 frames are scored asynchronously:
202 now, an HMAC-signed callback later."""

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from collections import Counter
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

LABELS = ("minor", "sexual_violence", "bestiality", "self_harm", "real_person", "explicit")
SECRET = os.environ.get("PROVIDER_WEBHOOK_SECRET", "dev-secret-change-me")
ASYNC_THRESHOLD = 30
TIMEOUT_HANG_S = float(os.environ.get("TIMEOUT_HANG_S", "120"))

app = FastAPI(title="Fake moderation provider")


class Control(BaseModel):
    mode: Literal["ok", "slow", "timeout", "error", "rate_limit", "malformed"] = "ok"
    delay_ms: int = 0
    # deliver every async callback twice ("the provider answers twice")
    duplicate_callbacks: bool = False


state = {"control": Control(), "calls": Counter(), "scored": Counter(), "callbacks": Counter()}
_tasks: set[asyncio.Task] = set()


def _log(event: str, **kw) -> None:
    print(json.dumps({"ts": round(time.time(), 3), "service": "provider", "event": event, **kw}), flush=True)


def _score(frames: list[dict]) -> list[dict]:
    out = []
    for f in frames:
        labels = set(LABELS) | set(f)
        out.append({label: float(f.get(label, 0.0)) for label in sorted(labels)})
    return out


@app.post("/_control")
def control(c: Control):
    state["control"] = c
    _log("control", **c.model_dump())
    return c


@app.get("/_stats")
def stats():
    return {"calls": dict(state["calls"]), "scored": dict(state["scored"]),
            "callbacks_sent": dict(state["callbacks"]), "control": state["control"].model_dump()}


@app.post("/_reset")
def reset():
    state.update(control=Control(), calls=Counter(), scored=Counter(), callbacks=Counter())
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/score")
async def score(request: Request):
    c: Control = state["control"]
    try:
        body = await request.json()
        media_id = str(body["media_id"])
        frames = body["frames"]
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    state["calls"][media_id] += 1
    _log("call", media_id=media_id, frames=len(frames), mode=c.mode, n=state["calls"][media_id])

    if c.mode == "timeout":
        await asyncio.sleep(TIMEOUT_HANG_S)
        return JSONResponse({"error": "gateway timeout"}, status_code=504)
    if c.mode == "error":
        return JSONResponse({"error": "internal error"}, status_code=500)
    if c.mode == "rate_limit":
        return JSONResponse({"error": "slow down"}, status_code=429, headers={"Retry-After": "1"})
    if c.mode == "malformed":
        return PlainTextResponse('{"frames": [{"explicit": "lots"}', status_code=200)
    if c.mode == "slow" and len(frames) <= ASYNC_THRESHOLD:
        await asyncio.sleep(c.delay_ms / 1000)

    request_id = str(uuid.uuid4())
    if len(frames) > ASYNC_THRESHOLD:
        callback_url = body.get("callback_url")
        delay = c.delay_ms / 1000 if c.mode == "slow" else 0.2
        t = asyncio.create_task(_callback(callback_url, media_id, request_id, frames, delay, c.duplicate_callbacks))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
        return JSONResponse({"request_id": request_id, "status": "accepted"}, status_code=202)

    state["scored"][media_id] += 1
    return {"media_id": media_id, "request_id": request_id, "frames": _score(frames)}


async def _callback(url: str, media_id: str, request_id: str, frames: list, delay: float, duplicate: bool):
    await asyncio.sleep(delay)
    state["scored"][media_id] += 1
    raw = json.dumps({"media_id": media_id, "request_id": request_id, "frames": _score(frames)}).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(timeout=10) as client:
        for delivery in range(2 if duplicate else 1):
            for attempt in range(5):  # like real webhooks: retry until 2xx
                try:
                    r = await client.post(url, content=raw, headers={"X-Signature": sig, "Content-Type": "application/json"})
                    state["callbacks"][media_id] += 1
                    _log("callback", media_id=media_id, request_id=request_id, status=r.status_code,
                         delivery=delivery, attempt=attempt)
                    if r.status_code < 500:
                        break
                except httpx.HTTPError as e:
                    _log("callback_failed", media_id=media_id, error=str(e), attempt=attempt)
                await asyncio.sleep(1 + attempt)
