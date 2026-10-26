"""Talks to the moderation provider and classifies every outcome. The caller decides
what each class means; nothing here turns a failure into a pass."""

from dataclasses import dataclass

import httpx

from . import config

_client = httpx.Client(timeout=httpx.Timeout(config.PROVIDER_TIMEOUT_S, connect=3.0))


@dataclass
class Outcome:
    kind: str  # scored | accepted | timeout | unavailable | rate_limited | server_error | client_error | malformed
    frames: list | None = None
    request_id: str | None = None
    retry_after: float | None = None
    detail: str = ""


def score(media_id: str, frames: list[dict], idempotency_key: str) -> Outcome:
    body = {"media_id": media_id, "frames": frames, "callback_url": config.CALLBACK_URL}
    try:
        resp = _client.post(
            f"{config.PROVIDER_URL}/score", json=body, headers={"Idempotency-Key": idempotency_key}
        )
    except httpx.TimeoutException as e:
        return Outcome("timeout", detail=type(e).__name__)
    except httpx.TransportError as e:
        return Outcome("unavailable", detail=f"{type(e).__name__}: {e}")

    if resp.status_code == 429:
        try:
            retry_after = float(resp.headers.get("Retry-After", "1"))
        except ValueError:
            retry_after = 1.0
        return Outcome("rate_limited", retry_after=retry_after, detail="429")
    if resp.status_code >= 500:
        return Outcome("server_error", detail=f"{resp.status_code}: {resp.text[:200]}")
    if resp.status_code not in (200, 202):
        return Outcome("client_error", detail=f"{resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError:
        return Outcome("malformed", detail=f"non-JSON body: {resp.text[:100]!r}")
    if not isinstance(data, dict):
        return Outcome("malformed", detail="body is not an object")

    if resp.status_code == 202:
        rid = data.get("request_id")
        if not isinstance(rid, str) or not rid:
            return Outcome("malformed", detail="202 without request_id")
        return Outcome("accepted", request_id=rid)
    # frames are validated against the media's frame count by the caller (policy.validate_result)
    return Outcome("scored", frames=data.get("frames"), request_id=data.get("request_id"))
