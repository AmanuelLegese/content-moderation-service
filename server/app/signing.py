import hashlib
import hmac


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str | None, secret: str) -> bool:
    if not signature:
        return False
    sig = signature.removeprefix("sha256=").strip()
    return hmac.compare_digest(sign(body, secret), sig)
