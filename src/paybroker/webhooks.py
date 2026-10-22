"""Signed webhook delivery to merchants.

    X-Timestamp: <unix seconds>
    X-Signature: HMAC-SHA256(merchant webhook secret, timestamp + "." + raw body), hex

Merchants should recompute the signature over the raw body, compare in constant time and reject timestamps older than
five minutes. Delivery retries with back-off for a day; the merchant sees every attempt in the dashboard.
"""
import json
import time

import httpx

from . import vault
from .ids import hmac_hex
from .netguard import UnsafeURL, check_url

BACKOFF = [5, 30, 300, 1800, 7200, 21600]    # seconds before attempt 2, 3, ...; six attempts in all


def _client() -> httpx.Client:    # indirection so tests can inject a transport
    return httpx.Client(timeout=10, follow_redirects=False)


def sign(secret: str, timestamp: str, raw: str) -> str:
    return hmac_hex(secret, f"{timestamp}.{raw}")


def deliver(conn, event_id: str) -> tuple[str, str]:
    """Attempt one delivery. Returns (outcome, detail): delivered | retry | failed | locked | skipped."""
    ev = conn.execute("SELECT e.event_id, e.body, e.status, e.attempts, m.webhook_url, m.webhook_secret_enc "
                      "FROM webhook_event e JOIN merchant m ON m.id=e.merchant_id WHERE e.event_id=%s", (event_id,)).fetchone()
    if not ev or ev["status"] == "delivered":
        return "skipped", "gone or already delivered"
    if not ev["webhook_url"] or not ev["webhook_secret_enc"]:
        conn.execute("UPDATE webhook_event SET status='skipped' WHERE event_id=%s", (event_id,))
        conn.commit()
        return "skipped", "the merchant has no webhook configured any more"
    try:
        secret = vault.decrypt(ev["webhook_secret_enc"])
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
        url = check_url(ev["webhook_url"])
        with _client() as c:
            resp = c.post(url, content=raw, headers=headers)
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
    final = attempts >= len(BACKOFF)
    conn.execute("UPDATE webhook_event SET status=%s, attempts=%s, last_code=%s WHERE event_id=%s",
                 ("failed" if final else "pending", attempts, code, event_id))
    conn.commit()
    return ("failed" if final else "retry"), detail
