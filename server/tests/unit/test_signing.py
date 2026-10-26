import hashlib
import hmac

from app.signing import sign, verify

BODY = b'{"media_id": "m1", "frames": []}'


def test_matches_plain_hmac_sha256():
    assert sign(BODY, "s") == hmac.new(b"s", BODY, hashlib.sha256).hexdigest()


def test_valid_signature_with_or_without_prefix():
    assert verify(BODY, sign(BODY, "s"), "s")
    assert verify(BODY, "sha256=" + sign(BODY, "s"), "s")


def test_rejects_missing_wrong_secret_and_tampered_body():
    assert not verify(BODY, None, "s")
    assert not verify(BODY, "", "s")
    assert not verify(BODY, sign(BODY, "other"), "s")
    assert not verify(BODY + b" ", sign(BODY, "s"), "s")  # signature is over the raw bytes
    assert not verify(BODY, "zz", "s")
