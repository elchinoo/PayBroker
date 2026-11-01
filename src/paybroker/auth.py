"""Credentials. Only hashes are stored.

* Operators (people, services, local tokens, the static key): agentpg-kit's `Authenticator` (see `core.py`). A person signs
  in through the ecosystem identity provider, or with the local admin password when running standalone.
* Merchants: secret keys (sk_test_...) call the payment API; publishable keys (pk_test_...) can only tokenize cards.
  They are a separate kind of principal that can never reach an operator route. Keys are shown once and revocable.
"""
import secrets
import time

from apgkit.auth import Principal
from apgkit.ids import sha256
from psycopg.types.json import Jsonb

from . import audit, core, rowmac
from .db import system_conn

local = core.local
SECRET_PREFIX, PUBLISHABLE_PREFIX = core.SECRET_PREFIX, core.PUBLISHABLE_PREFIX
MERCHANT_PERMISSIONS = {"secret": frozenset({"payments:write", "vault:tokenize"}), "publishable": frozenset({"vault:tokenize"})}


def admin_configured(conn) -> bool:
    return local.admin_configured(conn)


def static_key_configured() -> bool:
    return local.static_key_configured()


def needs_setup(conn) -> bool:
    return local.needs_setup(conn)


def setup_code() -> str:
    return local.setup_code()


# ---- merchant keys ------------------------------------------------------------------------------------
def _audit(conn, actor: str | None, action: str, detail: dict) -> None:
    if actor is not None:
        conn.execute("INSERT INTO admin_audit (actor, action, detail) VALUES (%s,%s,%s)", (actor, action, Jsonb(detail)))


def create_merchant_key(conn, merchant_id: str, kind: str, name: str = "", actor: str | None = None) -> tuple[int, str]:
    """Create a key; with `actor` the creation leaves an admin_audit row (merchant, key id, kind, name, prefix: never the key)."""
    if kind not in {"secret", "publishable"}:
        raise ValueError("kind must be 'secret' or 'publishable'")
    raw = (SECRET_PREFIX if kind == "secret" else PUBLISHABLE_PREFIX) + secrets.token_urlsafe(32)
    row = conn.execute("INSERT INTO merchant_key (merchant_id, kind, name, prefix, key_hash) VALUES (%s,%s,%s,%s,%s) "
                       "RETURNING id", (merchant_id, kind, name[:60], raw[:12], sha256(raw))).fetchone()
    rowmac.seal(conn, "merchant_key", "id=%s", (row["id"],))
    _audit(conn, actor, "merchant.key_create", {"merchant_id": merchant_id, "key_id": row["id"], "kind": kind, "name": name[:60],
                                                "prefix": raw[:12]})
    audit.record(conn, actor, "merchant.key_create", merchant_id,
                 {"merchant_id": merchant_id, "key_id": row["id"], "kind": kind, "name": name[:60]})   # not even the prefix: it holds random characters
    conn.commit()
    return row["id"], raw


_key_cache: dict[str, tuple[Principal, float]] = {}
KEY_CACHE_SECONDS = 30.0


def forget_keys() -> None:
    """Drop the lookup cache (called when a key is revoked or a merchant suspended in this process)."""
    _key_cache.clear()


def merchant_principal(conn, raw: str) -> Principal | None:
    """Look a merchant key up. Results are cached for 30 s so the hot path does not write to the key row on every
    payment (last_used_at is therefore approximate). A revoked key or suspended merchant stops working within 30 s
    in other processes, immediately in the process that made the change."""
    now = time.monotonic()
    hit = _key_cache.get(raw)
    if hit and now - hit[1] < KEY_CACHE_SECONDS:
        return hit[0]
    p = _merchant_lookup(conn, raw)
    if p:
        if len(_key_cache) > 10_000:
            _key_cache.clear()
        _key_cache[raw] = (p, now)
    else:
        _key_cache.pop(raw, None)
    return p


def _merchant_lookup(conn, raw: str) -> Principal | None:
    row = conn.execute("SELECT * FROM merchant_key WHERE key_hash=%s", (sha256(raw),)).fetchone()
    mer = conn.execute("SELECT * FROM merchant WHERE id=%s", (row["merchant_id"],)).fetchone() if row else None
    # a key or merchant row without a valid MAC is as good as absent (a planted key never authenticates)
    if (not row or row["revoked_at"] is not None or not mer or mer["status"] != "active"
            or not rowmac.verify_row("merchant_key", row) or not rowmac.verify_row("merchant", mer)):
        conn.rollback()
        return None
    conn.execute("UPDATE merchant_key SET last_used_at = now() WHERE id=%s", (row["id"],))
    conn.commit()
    return Principal("merchant", row["merchant_id"], MERCHANT_PERMISSIONS[row["kind"]], scopes=frozenset({row["kind"]}),
                     name=row["merchant_id"], ident=row["id"], extra={"merchant_id": row["merchant_id"]})


def list_merchant_keys(conn, merchant_id: str) -> list[dict]:
    """The merchant's keys; `verified` says whether the broker would accept the row (a planted one shows as False)."""
    rows = conn.execute("SELECT id, merchant_id, kind, key_hash, name, prefix, created_at, last_used_at, revoked_at, mac FROM merchant_key "
                        "WHERE merchant_id=%s ORDER BY id DESC", (merchant_id,)).fetchall()
    return [{k: v for k, v in r.items() if k not in ("key_hash", "merchant_id")} for r in rowmac.flag("merchant_key", rows)]


def revoke_merchant_key(conn, merchant_id: str, key_id: int, actor: str | None = None) -> bool:
    n = rowmac.update(conn, "merchant_key", "revoked_at = now()", (), "id=%s AND merchant_id=%s AND revoked_at IS NULL", (key_id, merchant_id))
    row = conn.execute("SELECT id, kind, name, prefix FROM merchant_key WHERE id=%s", (key_id,)).fetchone() if n else None
    if row:
        _audit(conn, actor, "merchant.key_revoke", {"merchant_id": merchant_id, "key_id": key_id, "kind": row["kind"], "name": row["name"],
                                                    "prefix": row["prefix"]})
        audit.record(conn, actor, "merchant.key_revoke", merchant_id,
                     {"merchant_id": merchant_id, "key_id": key_id, "kind": row["kind"], "name": row["name"]})
    conn.commit()
    forget_keys()
    return row is not None


def _resolve_merchant_key(raw: str) -> Principal | None:
    with system_conn() as conn:       # authenticating: the merchant is what this lookup finds out, so it cannot be scoped to one yet
        return merchant_principal(conn, raw)


core.auth.bearer_resolvers[SECRET_PREFIX] = _resolve_merchant_key
core.auth.bearer_resolvers[PUBLISHABLE_PREFIX] = _resolve_merchant_key
