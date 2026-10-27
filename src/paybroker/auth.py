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

from . import core

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
    _audit(conn, actor, "merchant.key_create", {"merchant_id": merchant_id, "key_id": row["id"], "kind": kind, "name": name[:60],
                                                "prefix": raw[:12]})
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
    row = conn.execute(
        "UPDATE merchant_key k SET last_used_at = now() FROM merchant m WHERE k.key_hash=%s AND k.revoked_at IS NULL "
        "AND m.id = k.merchant_id AND m.status='active' RETURNING k.id, k.kind, k.merchant_id", (sha256(raw),)).fetchone()
    conn.commit()
    if not row:
        return None
    return Principal("merchant", row["merchant_id"], MERCHANT_PERMISSIONS[row["kind"]], scopes=frozenset({row["kind"]}),
                     name=row["merchant_id"], ident=row["id"], extra={"merchant_id": row["merchant_id"]})


def list_merchant_keys(conn, merchant_id: str) -> list[dict]:
    return conn.execute("SELECT id, kind, name, prefix, created_at, last_used_at, revoked_at FROM merchant_key "
                        "WHERE merchant_id=%s ORDER BY id DESC", (merchant_id,)).fetchall()


def revoke_merchant_key(conn, merchant_id: str, key_id: int, actor: str | None = None) -> bool:
    row = conn.execute("UPDATE merchant_key SET revoked_at = now() WHERE id=%s AND merchant_id=%s AND revoked_at IS NULL "
                       "RETURNING id, kind, name, prefix", (key_id, merchant_id)).fetchone()
    if row:
        _audit(conn, actor, "merchant.key_revoke", {"merchant_id": merchant_id, "key_id": key_id, "kind": row["kind"], "name": row["name"],
                                                    "prefix": row["prefix"]})
    conn.commit()
    forget_keys()
    return row is not None


def _resolve_merchant_key(raw: str) -> Principal | None:
    with core.db.pool().connection() as conn:
        return merchant_principal(conn, raw)


core.auth.bearer_resolvers[SECRET_PREFIX] = _resolve_merchant_key
core.auth.bearer_resolvers[PUBLISHABLE_PREFIX] = _resolve_merchant_key
