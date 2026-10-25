"""Browser origins a merchant allows (CORS for the publishable key, and where 3-D Secure may send the customer back).

An origin is exactly `scheme://host[:port]`: https, or http only for localhost and 127.0.0.1 (local development). No wildcards,
no paths, no credentials, no query. What is stored is the normalized form, which is also what a browser sends in `Origin`.
"""
import ipaddress
import re
from urllib.parse import parse_qsl, urlsplit

MAX_ORIGINS = 20
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
RETURN_PARAMS = frozenset({"payment_id", "status", "timestamp", "signature"})   # appended by the hosted 3-D Secure page
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class OriginError(ValueError):
    pass


def clean_origin(raw) -> str:
    """Validate one origin and return it normalized (lower case, default port dropped). Raises OriginError."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 255:
        raise OriginError("an origin must be a non-empty string of at most 255 characters")
    value = raw.strip()
    if "*" in value:
        raise OriginError(f"{value}: wildcards are not allowed; list every origin")
    if any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
        raise OriginError(f"{value}: spaces and non-ASCII characters are not allowed")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise OriginError(f"{value}: not a valid origin") from exc
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if scheme not in {"https", "http"} or not host:
        raise OriginError(f"{value}: must look like https://shop.example.com")
    if parts.username is not None or parts.password is not None or parts.path or parts.query or parts.fragment or value.endswith("/") \
            or "?" in value or "#" in value:
        raise OriginError(f"{value}: an origin has no path, query, fragment or credentials (scheme, host and optional port only)")
    if scheme == "http" and host not in LOCAL_HOSTS:
        raise OriginError(f"{value}: only https is allowed (http only for localhost and 127.0.0.1)")
    if host not in LOCAL_HOSTS:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise OriginError(f"{value}: use a host name, not an IP address")
        if len(host) > 253 or "." not in host or not all(_LABEL.match(label) for label in host.split(".")):
            raise OriginError(f"{value}: not a valid host name")
    if port is not None and not 1 <= port <= 65535:
        raise OriginError(f"{value}: invalid port")
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + (f":{port}" if port is not None and port != default else "")


def clean_origins(raw) -> list[str]:
    """Validate a list of origins: normalized, de-duplicated, in the order given, at most MAX_ORIGINS."""
    if not isinstance(raw, list):
        raise OriginError("allowed_origins must be a list of origins")
    out: list[str] = []
    for item in raw:
        o = clean_origin(item)
        if o not in out:
            out.append(o)
    if len(out) > MAX_ORIGINS:
        raise OriginError(f"at most {MAX_ORIGINS} origins")
    return out


def origin_of(url: str) -> str | None:
    """The normalized origin of an absolute http(s) URL, or None if it has none."""
    try:
        parts = urlsplit(url)
        return clean_origin(f"{parts.scheme}://{parts.netloc}") if parts.scheme and parts.netloc else None
    except (OriginError, ValueError):
        return None


def clean_return_url(raw, allowed: list[str]) -> str:
    """A 3-D Secure return URL: absolute, no credentials or fragment, and on one of the merchant's allowed origins."""
    if not isinstance(raw, str) or not raw or len(raw) > 500:
        raise OriginError("return_url must be a URL of at most 500 characters")
    parts = urlsplit(raw)
    origin = origin_of(raw)
    if origin is None or parts.username is not None or parts.fragment or "#" in raw or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in raw):
        raise OriginError("return_url must be an absolute https URL without credentials or a fragment")
    if RETURN_PARAMS & {k for k, _ in parse_qsl(parts.query, keep_blank_values=True)}:
        raise OriginError(f"return_url must not use the query parameters {', '.join(sorted(RETURN_PARAMS))}: the page adds them")
    if origin not in allowed:
        raise OriginError(f"return_url's origin {origin} is not in this merchant's allowed origins")
    return raw
