"""Merchants and their routing and risk preferences."""
import secrets

from psycopg.types.json import Jsonb

from . import auth, vault
from .ids import new_id
from .netguard import UnsafeURL, check_url

CURRENCIES = {"USD", "EUR", "GBP", "BRL", "CAD", "AUD", "MXN", "INR", "JPY", "CHF", "SEK"}
DEFAULTS = {"preferred_psps": [], "max_fee_bps": None, "weights": {"auth": 100.0, "cost": 30.0, "latency": 5.0},
            "require_3ds_over_cents": None, "allowed_currencies": sorted(CURRENCIES), "region": None,
            "capture_method": "manual"}


class MerchantError(ValueError):
    pass


class NotFound(MerchantError):
    pass


def clean_settings(raw: dict, psp_names: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise MerchantError("settings must be an object")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise MerchantError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    out = dict(DEFAULTS) | {k: v for k, v in raw.items() if k in DEFAULTS}
    prefs = out["preferred_psps"]
    if not isinstance(prefs, list) or any(p not in psp_names for p in prefs):
        raise MerchantError(f"preferred_psps must be a list of connector names ({', '.join(sorted(psp_names))})")
    fee = out["max_fee_bps"]
    if fee is not None and (isinstance(fee, bool) or not isinstance(fee, int) or not 0 <= fee <= 1000):
        raise MerchantError("max_fee_bps must be a whole number from 0 to 1000 (or null)")
    w = out["weights"]
    if not isinstance(w, dict) or set(w) != {"auth", "cost", "latency"} or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1000 for v in w.values()):
        raise MerchantError("weights must be {auth, cost, latency}, each a number from 0 to 1000")
    over = out["require_3ds_over_cents"]
    if over is not None and (isinstance(over, bool) or not isinstance(over, int) or not 1 <= over <= 10**9):
        raise MerchantError("require_3ds_over_cents must be a positive whole number (or null)")
    cur = out["allowed_currencies"]
    if not isinstance(cur, list) or not cur or any(c not in CURRENCIES for c in cur):
        raise MerchantError(f"allowed_currencies must be a non-empty list from: {', '.join(sorted(CURRENCIES))}")
    if out["region"] is not None and (not isinstance(out["region"], str) or len(out["region"]) != 2 or not out["region"].isalpha()):
        raise MerchantError("region must be a 2-letter country code (or null)")
    if out["region"]:
        out["region"] = out["region"].upper()
    if out["capture_method"] not in {"manual", "automatic"}:
        raise MerchantError("capture_method must be 'manual' or 'automatic'")
    return out


def psp_names(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM psp_connector")}


def create(conn, name: str, settings: dict | None = None) -> dict:
    name = (name or "").strip()
    if not 1 <= len(name) <= 100:
        raise MerchantError("name must be 1 to 100 characters")
    s = clean_settings(settings or {}, psp_names(conn))
    mid = new_id("mch_test_", 8)
    conn.execute("INSERT INTO merchant (id, name, settings) VALUES (%s,%s,%s)", (mid, name, Jsonb(s)))
    conn.commit()
    return get(conn, mid)


def get(conn, merchant_id: str) -> dict:
    row = conn.execute("SELECT id, name, status, webhook_url, (webhook_secret_enc IS NOT NULL) AS has_webhook_secret, "
                       "settings, created_at FROM merchant WHERE id=%s", (merchant_id,)).fetchone()
    if not row:
        raise NotFound("merchant not found")
    row["settings"] = DEFAULTS | row["settings"]
    return row


def listing(conn) -> list[dict]:
    return conn.execute(
        "SELECT m.id, m.name, m.status, m.webhook_url, m.created_at, "
        "(SELECT count(*) FROM transaction t WHERE t.merchant_id=m.id) AS payments "
        "FROM merchant m ORDER BY m.created_at DESC").fetchall()


def update(conn, merchant_id: str, *, name: str | None, status: str | None, settings: dict | None) -> dict:
    cur = get(conn, merchant_id)
    if status is not None and status not in {"active", "suspended"}:
        raise MerchantError("status must be active or suspended")
    s = clean_settings({**cur["settings"], **settings}, psp_names(conn)) if settings is not None else cur["settings"]
    conn.execute("UPDATE merchant SET name=%s, status=%s, settings=%s WHERE id=%s",
                 ((name or cur["name"]).strip()[:100], status or cur["status"], Jsonb(s), merchant_id))
    conn.commit()
    return get(conn, merchant_id)


def set_webhook(conn, merchant_id: str, url: str | None) -> tuple[dict, str | None]:
    """Set (or clear) the webhook URL. Setting one generates a fresh signing secret, returned exactly once."""
    get(conn, merchant_id)
    if not url:
        conn.execute("UPDATE merchant SET webhook_url=NULL, webhook_secret_enc=NULL WHERE id=%s", (merchant_id,))
        conn.commit()
        return get(conn, merchant_id), None
    if not vault.is_initialized(conn):
        raise MerchantError("set an admin password first: webhook secrets are stored encrypted under it")
    try:
        url = check_url(url)
    except UnsafeURL as exc:
        raise MerchantError(str(exc)) from exc
    secret = "whsec_" + secrets.token_urlsafe(32)
    conn.execute("UPDATE merchant SET webhook_url=%s, webhook_secret_enc=%s WHERE id=%s",
                 (url, vault.encrypt(secret), merchant_id))
    conn.commit()
    return get(conn, merchant_id), secret


def new_key(conn, merchant_id: str, kind: str, name: str) -> tuple[int, str]:
    get(conn, merchant_id)
    return auth.create_merchant_key(conn, merchant_id, kind, name)
