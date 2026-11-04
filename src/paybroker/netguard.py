"""The outbound-URL guard now lives in agentpg-kit; this module keeps the old import path and this app's environment switch."""
import ipaddress
import os
import re
import socket  # noqa: F401  (tests patch netguard.socket.getaddrinfo)
from urllib.parse import urlsplit

from apgkit.netguard import Resolved, UnsafeURL, classify

from . import origins
from .core import netguard as _guard

DEV_WEBHOOK_HOSTS_VAR = "PAYBROKER_DEV_WEBHOOK_HOSTS"
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def check_url(url: str) -> str:
    return _guard.check(url)


def resolve_url(url: str):
    """Validate `url` and pin the connection to the address it resolves to (`Resolved.request_args()` gives httpx the literal IP,
    the Host header and the TLS server name)."""
    return _guard.resolve(url)


def _valid_dev_host(entry: str) -> bool:
    """An exact host name or IP literal. No wildcards, no CIDR ranges, no ports, paths or credentials."""
    try:
        ipaddress.ip_address(entry)
        return True
    except ValueError:
        pass
    return len(entry) <= 253 and all(_LABEL.match(label) for label in entry.split("."))


def dev_webhook_hosts() -> tuple[str, ...]:
    """The exact hosts PAYBROKER_DEV_WEBHOOK_HOSTS names, but ONLY while PAYBROKER_DEV_HTTP is also on; otherwise empty. Entries that are not a
    plain host name or IP literal (wildcards, CIDR ranges, ports, paths) are ignored. Read from configuration only, never from a request."""
    if not origins.dev_http():
        return ()
    out: list[str] = []
    for raw in os.environ.get(DEV_WEBHOOK_HOSTS_VAR, "").split(","):
        entry = raw.strip().lower().strip("[]")
        if entry and _valid_dev_host(entry) and entry not in out:
            out.append(entry)
    return tuple(out)


def _resolve_listed(url: str) -> Resolved:
    """A webhook URL whose host the development allowlist names: private and loopback addresses and plain http are accepted for exactly that
    name; link-local (cloud metadata) is refused whatever is listed, and every resolved address is checked, then the connection is pinned to one."""
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise UnsafeURL("URL must start with http:// or https://")
    if parts.username or parts.password:
        raise UnsafeURL("credentials must not be embedded in the URL")
    if parts.query or parts.fragment:
        raise UnsafeURL("URL must not contain a query string or fragment")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, ValueError) as exc:
        raise UnsafeURL(f"cannot resolve host {parts.hostname!r}") from exc
    if not infos:
        raise UnsafeURL(f"cannot resolve host {parts.hostname!r}")
    pinned: str | None = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if classify(ip) == "link-local":
            raise UnsafeURL("link-local addresses (including cloud metadata services) are not allowed")
        pinned = pinned or str(ip)
    return Resolved(url.strip().rstrip("/"), parts.hostname, port, parts.scheme, pinned)


def resolve_webhook_url(url: str):
    """Like resolve_url, for a merchant's webhook: a host named in PAYBROKER_DEV_WEBHOOK_HOSTS (development only) gets the narrow exemption above."""
    try:
        host = (urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        host = ""
    if host and host in dev_webhook_hosts():
        return _resolve_listed(url)
    return _guard.resolve(url)


def check_webhook_url(url: str) -> str:
    return resolve_webhook_url(url).url


__all__ = ["UnsafeURL", "check_url", "resolve_url", "check_webhook_url", "resolve_webhook_url", "dev_webhook_hosts"]
