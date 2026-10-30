"""Signed webhook delivery to merchants.

    X-Timestamp: <unix seconds>
    X-Signature: HMAC-SHA256(merchant webhook secret, timestamp + "." + raw body), hex

Merchants should recompute the signature over the raw body, compare in constant time and reject timestamps older than
five minutes. Delivery retries with back-off for about a day (seven attempts); the merchant sees every attempt in the dashboard.
"""
import json
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from . import vault
from .ids import hmac_hex
from .netguard import UnsafeURL, resolve_url

BACKOFF = [30, 300, 1800, 7200, 21600, 54000]    # seconds to wait after failed attempt 1, 2, ... (30 s ... 15 h: about 23.6 h in all)
ATTEMPTS = len(BACKOFF) + 1                      # the first try plus one per delay


def _client() -> httpx.Client:    # indirection so tests can inject a transport
    return httpx.Client(timeout=10, follow_redirects=False)


def sign(secret: str, timestamp: str, raw: str) -> str:
    return hmac_hex(secret, f"{timestamp}.{raw}")


def signed_return_url(secret_enc: str, merchant_id: str, url: str, payment_id: str, status: str, now: float | None = None) -> str | None:
    """Where the hosted 3-D Secure page sends the customer: the merchant's return_url plus
    payment_id, status, timestamp and signature = HMAC-SHA256(webhook secret, timestamp + "." + "payment_id=<id>&status=<STATUS>"), hex,
    the webhook scheme over that query string. None when the secret cannot be read (vault locked): no unsigned redirect is ever made."""
    try:
        secret = vault.decrypt(secret_enc, vault.webhook_secret_context(merchant_id))
    except (vault.VaultLocked, vault.VaultError):
        return None
    ts = str(int(time.time() if now is None else now))
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [("payment_id", payment_id), ("status", status), ("timestamp", ts),
              ("signature", sign(secret, ts, urlencode([("payment_id", payment_id), ("status", status)])))]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def deliver(conn, event_id: str) -> tuple[str, str]:
    """Attempt one delivery. Returns (outcome, detail): delivered | retry | failed | locked | skipped."""
    ev = conn.execute("SELECT e.event_id, e.merchant_id, e.body, e.status, e.attempts, m.webhook_url, m.webhook_secret_enc "
                      "FROM webhook_event e JOIN merchant m ON m.id=e.merchant_id WHERE e.event_id=%s", (event_id,)).fetchone()
    if not ev or ev["status"] == "delivered":
        return "skipped", "gone or already delivered"
    if not ev["webhook_url"] or not ev["webhook_secret_enc"]:
        conn.execute("UPDATE webhook_event SET status='skipped' WHERE event_id=%s", (event_id,))
        conn.commit()
        return "skipped", "the merchant has no webhook configured any more"
    try:
        secret = vault.decrypt(ev["webhook_secret_enc"], vault.webhook_secret_context(ev["merchant_id"]))
    except vault.VaultLocked:
        return "locked", "the vault is locked: sign in or configure PAYBROKER_ENCRYPTION_KEY"
    except vault.VaultError:
        conn.execute("UPDATE webhook_event SET status='failed' WHERE event_id=%s", (event_id,))
        conn.commit()
        return "failed", "the stored webhook secret cannot be read: rotate it"
    raw = json.dumps(ev["body"], separators=(",", ":"), sort_keys=True)
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": sign(secret, ts, raw),
               "User-Agent": "PayBroker-Webhooks/1"}
    code, detail = None, ""
    try:
        # The merchant's URL is untrusted input: resolve it once, check every address, and connect to that address (no second DNS
        # lookup for a rebinding name to answer). Host header and TLS server name stay the merchant's. Redirects are never followed.
        url, pin_headers, extensions = resolve_url(ev["webhook_url"]).request_args()
        with _client() as c:
            resp = c.post(url, content=raw, headers={**headers, **pin_headers}, extensions=extensions)
        code = resp.status_code
        ok = 200 <= code < 300
        detail = f"HTTP {code}"
    except UnsafeURL as exc:
        ok, detail = False, f"URL refused: {exc}"
    except httpx.HTTPError as exc:
        ok, detail = False, type(exc).__name__
    attempts = ev["attempts"] + 1
    if ok:
        conn.execute("UPDATE webhook_event SET status='delivered', attempts=%s, last_code=%s, delivered_at=now() WHERE event_id=%s",
                     (attempts, code, event_id))
        conn.commit()
        return "delivered", detail
    final = attempts >= ATTEMPTS
    conn.execute("UPDATE webhook_event SET status=%s, attempts=%s, last_code=%s WHERE event_id=%s",
                 ("failed" if final else "pending", attempts, code, event_id))
    conn.commit()
    return ("failed" if final else "retry"), detail
