"""The outbound-URL guard now lives in agentpg-kit; this module keeps the old import path and this app's environment switch."""
import socket  # noqa: F401  (tests patch netguard.socket.getaddrinfo)

from apgkit.netguard import UnsafeURL

from .core import netguard as _guard


def check_url(url: str) -> str:
    return _guard.check(url)


def resolve_url(url: str):
    """Validate `url` and pin the connection to the address it resolves to (`Resolved.request_args()` gives httpx the literal IP,
    the Host header and the TLS server name)."""
    return _guard.resolve(url)


__all__ = ["UnsafeURL", "check_url", "resolve_url"]
