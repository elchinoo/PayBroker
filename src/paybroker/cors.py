"""CORS for exactly one thing: tokenizing a card from a customer's browser with a publishable key.

Only the paths in BROWSER_PATHS ever get CORS headers. Everything else, the secret-key payment API included, is untouched, so a
page on another origin cannot read its responses. The policy is deliberately narrow:

* The `Access-Control-Allow-Origin` value is the request's own origin, never `*`, and only when that origin is on a merchant's list.
* A preflight carries no credential, so it is answered when ANY merchant lists the origin. The real request is checked again against
  the key's own merchant (see `api.tokenize_auth`), which records the verdict in `scope["state"]`; this middleware only adds the
  headers to the response of a request that passed. A request that did not pass gets none.
* No `Access-Control-Allow-Credentials`: the key travels in the Authorization header, never in cookies.
"""
import json
import time
from collections.abc import Callable

from starlette.concurrency import run_in_threadpool

BROWSER_PATHS = frozenset({"/v1/vault/tokens"})      # POST, publishable (or secret) key: the whole browser-facing surface
ALLOW_METHODS = "POST, OPTIONS"
ALLOW_HEADERS = "Authorization, Content-Type, Idempotency-Key"
MAX_AGE = "600"
STATE_KEY = "cors_origin"                            # set in scope["state"] by the route once the merchant's list allowed the origin
_CACHE_SECONDS, _CACHE_MAX = 5.0, 1000


class OriginLookup:
    """Is this origin on any merchant's list? Cached for a few seconds, so a flood of preflights costs one query per origin, not one each."""

    def __init__(self, check: Callable[[str], bool]):
        self._check = check
        self._seen: dict[str, tuple[bool, float]] = {}

    def clear(self) -> None:
        self._seen.clear()

    def __call__(self, origin: str) -> bool:
        now = time.monotonic()
        hit = self._seen.get(origin)
        if hit and now - hit[1] < _CACHE_SECONDS:
            return hit[0]
        ok = self._check(origin)
        if len(self._seen) >= _CACHE_MAX:
            self._seen.clear()
        self._seen[origin] = (ok, now)
        return ok


class BrowserCors:
    def __init__(self, app, lookup: Callable[[str], bool]):
        self.app, self.lookup = app, lookup

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] not in BROWSER_PATHS:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        origin = headers.get("origin")
        if origin is None:
            return await self.app(scope, receive, send)
        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            return await self._preflight(scope, send, origin, headers["access-control-request-method"])

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                allowed = (scope.get("state") or {}).get(STATE_KEY)
                if allowed == origin:
                    out = [(k, v) for k, v in message["headers"] if k.lower() not in {b"access-control-allow-origin"}]
                    vary = [v for k, v in out if k.lower() == b"vary"]
                    out = [(k, v) for k, v in out if k.lower() != b"vary"]
                    out.append((b"access-control-allow-origin", origin.encode("latin-1")))
                    out.append((b"vary", b", ".join([*vary, b"Origin"]) if vary else b"Origin"))
                    message = {**message, "headers": out}
            await send(message)

        await self.app(scope, receive, send_with_cors)

    async def _preflight(self, scope, send, origin: str, method: str):
        try:
            ok = method.upper() == "POST" and await run_in_threadpool(self.lookup, origin)
        except Exception:  # noqa: BLE001 - the database may be asleep: no headers, the browser blocks the call, nothing leaks
            ok = False
        if not ok:
            body = json.dumps({"error": {"code": "origin_not_allowed", "message": "this origin is not allowed to call this endpoint"}}).encode()
            await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json"),
                                                                                 (b"content-length", str(len(body)).encode())]})
            return await send({"type": "http.response.body", "body": body})
        await send({"type": "http.response.start", "status": 204, "headers": [
            (b"access-control-allow-origin", origin.encode("latin-1")), (b"access-control-allow-methods", ALLOW_METHODS.encode()),
            (b"access-control-allow-headers", ALLOW_HEADERS.encode()), (b"access-control-max-age", MAX_AGE.encode()),
            (b"vary", b"Origin"), (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})
