"""The fake provider behaves as the brief specifies, so the integration tests can trust it."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.signing import verify
from fakeprovider import main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def reset():
    client.post("/_reset")


def score(media_id, frames, **extra):
    return client.post("/score", json={"media_id": media_id, "frames": frames, **extra})


def test_echoes_scores_and_fills_every_label_with_zero():
    r = score("a", [{"explicit": 0.95, "self_harm": 0.1}])
    assert r.status_code == 200
    (frame,) = r.json()["frames"]
    assert frame["explicit"] == 0.95 and frame["self_harm"] == 0.1
    assert all(frame[label] == 0.0 for label in ("minor", "sexual_violence", "bestiality", "real_person"))


def test_counts_every_call_per_media_id():
    score("a", [{}]), score("a", [{}]), score("b", [{}])
    client.post("/_control", json={"mode": "error"})
    score("a", [{}])  # failed calls count too: they are still requests we sent
    assert client.get("/_stats").json()["calls"] == {"a": 3, "b": 1}


@pytest.mark.parametrize("mode,code", [("error", 500), ("rate_limit", 429)])
def test_failure_modes(mode, code):
    client.post("/_control", json={"mode": mode, "delay_ms": 0})
    assert score("a", [{}]).status_code == code


def test_malformed_mode_returns_unparseable_body():
    client.post("/_control", json={"mode": "malformed"})
    r = score("a", [{}])
    assert r.status_code == 200
    with pytest.raises(ValueError):
        json.loads(r.text)


def test_timeout_mode_hangs(monkeypatch):
    monkeypatch.setattr(main, "TIMEOUT_HANG_S", 0.05)
    client.post("/_control", json={"mode": "timeout"})
    assert score("a", [{}]).status_code == 504  # after the hang; real callers have given up long before


def test_control_rejects_unknown_mode():
    assert client.post("/_control", json={"mode": "sideways"}).status_code == 422


@pytest.fixture
def sent(monkeypatch):
    """Capture callbacks instead of posting them (the provider module's httpx only)."""
    calls = []

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content, headers):
            calls.append((url, content, headers))
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(main, "httpx", SimpleNamespace(AsyncClient=FakeAsyncClient, HTTPError=httpx.HTTPError))
    return calls


def test_long_video_is_async_and_callback_is_signed(sent):
    rid = asyncio.run(_score_async([{"explicit": 0.1}] * 31))
    (url, body, headers), = sent
    assert url == "http://cb/hook"
    payload = json.loads(body)
    assert payload["request_id"] == rid and len(payload["frames"]) == 31
    assert verify(body, headers["X-Signature"], main.SECRET)


def test_thirty_frames_is_still_sync():
    assert score("v", [{}] * 30).status_code == 200


def test_duplicate_callbacks_option(sent):
    asyncio.run(main._callback("http://cb/hook", "v", "r1", [{}] * 31, 0, duplicate=True))
    assert len(sent) == 2 and sent[0][1] == sent[1][1]


async def _score_async(frames):
    """Call the endpoint in-loop so the background callback task runs to completion."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://p") as c:
        r = await c.post("/score", json={"media_id": "v", "frames": frames, "callback_url": "http://cb/hook"})
    assert r.status_code == 202
    await asyncio.gather(*list(main._tasks))
    return r.json()["request_id"]
