"""Talks to the moderation provider and classifies every outcome. The caller decides
what each class means; nothing here turns a failure into a pass.

Why classify instead of raising: the brief asks for timeouts, 429s, 500s and malformed
responses to be handled differently. Returning one `Outcome` with a `kind` lets
worker.process() handle each case explicitly in one place, and the broken handler's
bug ("timeout = pass") is visible as one wrong branch rather than a hidden except.
"""

from dataclasses import dataclass

import httpx

from . import config

# One shared client per process: reuses TCP connections to the provider.
# Separate timeouts: connecting should be fast (3s); reading waits for the classifier.
_client = httpx.Client(timeout=httpx.Timeout(config.PROVIDER_TIMEOUT_S, connect=3.0))


@dataclass
class Outcome:
    kind: str  # scored | accepted | timeout | unavailable | rate_limited | server_error | client_error | malformed
    frames: list | None = None        # only for "scored"
    request_id: str | None = None     # provider's id; for "accepted" it ties the later callback to this request
    retry_after: float | None = None  # only for "rate_limited"
    detail: str = ""                  # short text for logs


def score(media_id: str, frames: list[dict], idempotency_key: str) -> Outcome:
    """Send every frame of one media item to the provider (rule 9) and classify the answer."""
    body = {"media_id": media_id, "frames": frames, "callback_url": config.CALLBACK_URL}
    try:
        resp = _client.post(
            # Idempotency-Key = media id: a provider that supports it can dedupe retries and
            # not bill twice. Ours doesn't; see README "Remaining limits".
            f"{config.PROVIDER_URL}/score", json=body, headers={"Idempotency-Key": idempotency_key}
        )
    except httpx.TimeoutException as e:
        # The incident's first bug was treating this as "nothing found". It is a failure.
        return Outcome("timeout", detail=type(e).__name__)
    except httpx.TransportError as e:
        # Connection refused, DNS failure, connection reset: the provider is unreachable.
        return Outcome("unavailable", detail=f"{type(e).__name__}: {e}")

    if resp.status_code == 429:
        # Retry-After may be missing or not a number; default to 1s rather than crash.
        try:
            retry_after = float(resp.headers.get("Retry-After", "1"))
        except ValueError:
            retry_after = 1.0
        return Outcome("rate_limited", retry_after=retry_after, detail="429")
    if resp.status_code >= 500:
        return Outcome("server_error", detail=f"{resp.status_code}: {resp.text[:200]}")
    # Other 4xx: our request was rejected; sending the same request again won't help.
    if resp.status_code not in (200, 202):
        return Outcome("client_error", detail=f"{resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError:
        return Outcome("malformed", detail=f"non-JSON body: {resp.text[:100]!r}")
    if not isinstance(data, dict):
        return Outcome("malformed", detail="body is not an object")

    # 202 = long video, scored asynchronously; the result arrives later at /provider/callback.
    if resp.status_code == 202:
        rid = data.get("request_id")
        if not isinstance(rid, str) or not rid:
            return Outcome("malformed", detail="202 without request_id")
        return Outcome("accepted", request_id=rid)
    # frames are validated against the media's frame count by the caller (policy.validate_result)
    return Outcome("scored", frames=data.get("frames"), request_id=data.get("request_id"))
