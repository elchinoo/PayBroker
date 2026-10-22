import hashlib
import hmac
import secrets

_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_id(prefix: str, n: int = 12) -> str:
    return prefix + "".join(secrets.choice(_ALNUM) for _ in range(n))


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hmac_hex(key: str | bytes, message: str) -> str:
    key = key.encode() if isinstance(key, str) else key
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
