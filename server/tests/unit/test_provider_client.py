"""Every provider answer is classified, and nothing that is not a complete answer comes
back as 'scored'. Uses an in-memory transport: no network."""

import httpx
import pytest

from app import provider_client


@pytest.fixture
def respond(monkeypatch):
    def install(handler):
        monkeypatch.setattr(provider_client, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    return install


def call():
    return provider_client.score("m1", [{"explicit": 0.1}], idempotency_key="m1")


def test_scored(respond):
    respond(lambda req: httpx.Response(200, json={"request_id": "r1", "frames": [{"explicit": 0.1}]}))
    out = call()
    assert out.kind == "scored" and out.frames == [{"explicit": 0.1}] and out.request_id == "r1"


def test_sends_all_frames_callback_and_idempotency_key(respond):
    seen = {}

    def handler(req):
        seen["body"] = req.read()
        seen["key"] = req.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"frames": []})

    respond(handler)
    call()
    assert b'"callback_url"' in seen["body"] and b'"frames"' in seen["body"]
    assert seen["key"] == "m1"


def test_accepted_needs_request_id(respond):
    respond(lambda req: httpx.Response(202, json={"request_id": "r9"}))
    assert call().kind == "accepted"
    respond(lambda req: httpx.Response(202, json={}))
    assert call().kind == "malformed"


def test_timeout_is_not_a_pass(respond):
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)
    respond(handler)
    out = call()
    assert out.kind == "timeout" and out.frames is None


def test_connection_error(respond):
    def handler(req):
        raise httpx.ConnectError("refused", request=req)
    respond(handler)
    assert call().kind == "unavailable"


@pytest.mark.parametrize("header,expected", [("3", 3.0), (None, 1.0), ("soon", 1.0)])
def test_rate_limited_reads_retry_after(respond, header, expected):
    headers = {"Retry-After": header} if header else {}
    respond(lambda req: httpx.Response(429, headers=headers))
    out = call()
    assert out.kind == "rate_limited" and out.retry_after == expected


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_server_errors(respond, code):
    respond(lambda req: httpx.Response(code, text="boom"))
    assert call().kind == "server_error"


@pytest.mark.parametrize("code", [400, 401, 404, 422])
def test_client_errors(respond, code):
    respond(lambda req: httpx.Response(code, text="no"))
    assert call().kind == "client_error"


@pytest.mark.parametrize("body", ['{"frames": [{"explicit": "lots"}', "<html>oops</html>", "[1,2]"])
def test_malformed_bodies(respond, body):
    respond(lambda req: httpx.Response(200, text=body))
    assert call().kind == "malformed"


def test_bad_scores_in_valid_json_are_left_to_validation(respond):
    # The client only checks the envelope; policy.validate_result rejects these (test_policy.py)
    respond(lambda req: httpx.Response(200, json={"frames": [{"explicit": "lots"}]}))
    assert call().kind == "scored"
