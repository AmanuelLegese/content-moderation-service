"""HMAC-SHA256 signatures for provider callbacks (rule 7).

What: proves a callback really came from the provider and was not changed in transit.
How: the provider computes HMAC-SHA256(secret, raw request body) and sends it as hex
in X-Signature; we recompute it over the exact bytes we received and compare.
Why the raw body: re-serialising parsed JSON can reorder keys or change whitespace,
which would change the signature, so verification must happen before parsing.
"""

import hashlib
import hmac


def sign(body: bytes, secret: str) -> str:
    """Hex HMAC-SHA256 of the body. Used by the fake provider and by tests to forge valid callbacks."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str | None, secret: str) -> bool:
    """True only for a correct signature. Missing or empty means rejected."""
    if not signature:
        return False
    # Accept both "abc123..." and the common "sha256=abc123..." header format.
    sig = signature.removeprefix("sha256=").strip()
    # compare_digest takes the same time however many characters match, so an attacker
    # cannot guess the signature one character at a time by measuring response times.
    return hmac.compare_digest(sign(body, secret), sig)
