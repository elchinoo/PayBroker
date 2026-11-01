"""Row MACs: a database WRITE without the vault data key cannot plant a merchant or an API key, redirect a webhook, add a connector, or
change a fraud rule or a blocklist entry.

Every row of the tables in FIELDS carries `mac`, an HMAC under a subkey of the vault data key (agentpg-kit `Vault.row_mac`) over the
columns that decide authority. Reads that feed an authorization or routing decision call `verify_row` / `valid`: a row with a missing or
wrong MAC is treated exactly like a missing row. Writes sign in the same transaction (`seal`, `update`).

Enforced only while the vault is unlocked (with sealed boot it always is before the app serves). While it is locked (dev, tests with no
vault, a standalone install without a password) nothing is checked and a write clears the MAC, so such rows need `paybroker adopt-rowmacs`
once the vault is in use.

Limits (README, "Row MACs"): an older authentic row copied back from a backup verifies; deleting a row is not detected; columns outside
FIELDS (names, timestamps, usage counters, the simulated connectors' behaviour) can be edited freely. `audit_ledger` hash-chains by itself
and is left alone. The kit signs `web_session` and `api_token` itself.
"""
import hmac
import json
import logging
import threading
from datetime import datetime

from prometheus_client import Counter

from . import core

log = logging.getLogger("paybroker.rowmac")

# column kinds: s text/number, arr text[] (sorted), json jsonb (sort_keys), ts timestamptz (epoch seconds), bool (0/1)
FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    # webhook_secret_enc is not signed: it is already an authenticated ciphertext bound to the merchant id (vault v2), so it cannot be forged
    # or moved to another row; a copied-back older value is the replay limit described in the README.
    "merchant": (("id", "s"), ("status", "s"), ("webhook_url", "s"), ("allowed_origins", "arr"), ("settings", "json")),
    "merchant_key": (("id", "s"), ("merchant_id", "s"), ("kind", "s"), ("key_hash", "s"), ("revoked_at", "ts")),
    "psp_connector": (("name", "s"), ("enabled", "bool"), ("currencies", "arr"), ("regions", "arr"), ("fee_bps", "s"), ("fixed_fee_cents", "s")),
    "fraud_rule": (("id", "s"), ("name", "s"), ("definition", "json"), ("action", "s"), ("enabled", "bool")),
    "blacklist": (("id", "s"), ("kind", "s"), ("value", "s")),
}
PK: dict[str, tuple[str, ...]] = {
    "merchant": ("id",), "merchant_key": ("id",), "psp_connector": ("name",), "fraud_rule": ("id",), "blacklist": ("id",),
}
TABLES = tuple(FIELDS)

tampered = Counter("payment_broker_tampered_rows_total", "Rows refused because their MAC is missing or wrong", ["table"],
                   registry=core.metrics.registry)
_reported: set[tuple] = set()
_reported_lock = threading.Lock()


def enforced() -> bool:
    return core.vault.is_unlocked()


def _canon(value, kind: str) -> str:
    if value is None:
        return "\x00"                                     # distinct from the empty string
    if kind == "arr":
        return json.dumps(sorted(value))
    if kind == "json":
        return json.dumps(value, sort_keys=True)
    if kind == "ts":
        return str(int(value.timestamp())) if isinstance(value, datetime) else str(int(value))
    if kind == "bool":
        return "1" if value else "0"
    return str(value)


def sign_row(table: str, row: dict) -> str:
    """The MAC for `row` (a dict with at least the MAC'd columns of `table`). Raises VaultLocked when the vault is locked."""
    return core.vault.row_mac(table, *(_canon(row[c], k) for c, k in FIELDS[table]))


def _key(table: str, row: dict) -> tuple:
    return tuple(str(row[c]) for c in PK[table])


def _good(table: str, row: dict) -> bool:
    try:
        return core.vault.check_row_mac(table, row.get("mac"), *(_canon(row[c], k) for c, k in FIELDS[table]))
    except Exception:  # noqa: BLE001  (locked mid-call, odd column types: fail closed)
        return False


def verify_row(table: str, row: dict | None) -> bool:
    """True when the row may be used: it carries a valid MAC, or the vault is locked (nothing is enforced then). A missing row, no MAC
    or a wrong MAC while unlocked is False (the caller treats it as absent); that is logged (table and key only, never values), counted
    and recorded once per row per process."""
    if not row:
        return False
    if not enforced():
        return True
    if _good(table, row):
        return True
    key = (table, *_key(table, row))
    tampered.labels(table).inc()
    with _reported_lock:
        first = key not in _reported
        _reported.add(key)
    if first:
        pk = "/".join(key[1:])
        log.warning("row refused, MAC missing or wrong: table=%s key=%s", table, pk)
        try:
            from . import audit
            from .db import connect
            with connect(retries=1) as c:
                audit.record(c, audit.SYSTEM, "row_mac_invalid", f"{table}:{pk}"[:120], {"table": table, "key": pk})
                c.commit()
        except Exception:  # noqa: BLE001  (auditing must never break a lookup)
            pass
    return False


def valid(table: str, rows: list[dict]) -> list[dict]:
    return [r for r in rows if verify_row(table, r)]


def strip(row: dict) -> dict:
    """The row without its MAC (what leaves this layer: API responses never carry it)."""
    return {k: v for k, v in row.items() if k != "mac"}


def flag(table: str, rows: list[dict]) -> list[dict]:
    """For operator listings: every row, minus the MAC, with `verified` saying whether the broker would use it."""
    return [strip(r) | {"verified": verify_row(table, r)} for r in rows]


def _where(table: str, pk: tuple) -> tuple[str, tuple]:
    return " AND ".join(f"{c}=%s" for c in PK[table]), pk


def seal(conn, table: str, where: str, params: tuple = ()) -> int:
    """Sign the rows of `table` matching `where`, in the caller's transaction. ONLY for rows this process has just written (or `adopt`):
    it signs whatever the row holds now. With the vault locked the MAC is cleared (the row is ignored once the vault is in use, until
    adopted)."""
    rows = conn.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall()           # noqa: S608  (table is a constant of this module)
    unlocked = enforced()
    for r in rows:
        w, p = _where(table, tuple(r[c] for c in PK[table]))
        conn.execute(f"UPDATE {table} SET mac=%s WHERE {w}", (sign_row(table, r) if unlocked else None, *p))   # noqa: S608
    return len(rows)


def update(conn, table: str, assignments: str, set_params: tuple, where: str, params: tuple = ()) -> int:
    """UPDATE ... SET `assignments` WHERE `where`, then re-sign the rows that carried a valid MAC BEFORE the change (a planted or edited
    row is never legitimised by an unrelated update). Returns the number of rows changed."""
    before = conn.execute(f"SELECT * FROM {table} WHERE {where} FOR UPDATE", params).fetchall()             # noqa: S608
    good = [tuple(r[c] for c in PK[table]) for r in before if not enforced() or _good(table, r)]
    n = conn.execute(f"UPDATE {table} SET {assignments} WHERE {where}", (*set_params, *params)).rowcount     # noqa: S608
    for pk in good:
        w, p = _where(table, pk)
        seal(conn, table, w, p)
    return n


def snapshot(conn) -> dict[str, list]:
    """Keys of every row that verifies now (taken around a data-key rotation, which changes every MAC). Includes the kit's own
    web_session and api_token rows: their MACs also change with the data key and the kit does not re-sign them."""
    out: dict[str, list] = {}
    loc = core.local
    out["web_session"] = [r["token_hash"] for r in conn.execute("SELECT * FROM web_session").fetchall()
                          if r["mac"] and hmac.compare_digest(r["mac"], loc.session_mac(
                              r["token_hash"], r["subject"], r["name"], r["email"], r["roles"], r["source"], r["expires_at"]))]
    out["api_token"] = [r["id"] for r in conn.execute("SELECT * FROM api_token").fetchall()
                        if r["mac"] and hmac.compare_digest(r["mac"], loc.token_mac(
                            r["id"], r["name"], r["token_hash"], r["prefix"], r["scope"], r["revoked_at"] is not None))]
    for t in TABLES:
        out[t] = [tuple(r[c] for c in PK[t]) for r in conn.execute(f"SELECT * FROM {t}").fetchall() if _good(t, r)]        # noqa: S608
    return out


def reseal(conn, snap: dict[str, list]) -> int:
    """Sign again, under the new key, the rows `snapshot` saw verify. Rows that did not verify stay unsigned (still ignored)."""
    n = 0
    loc = core.local
    for token_hash in snap.pop("web_session", []):
        r = conn.execute("SELECT * FROM web_session WHERE token_hash=%s", (token_hash,)).fetchone()
        if r:
            conn.execute("UPDATE web_session SET mac=%s WHERE token_hash=%s", (loc.session_mac(
                r["token_hash"], r["subject"], r["name"], r["email"], r["roles"], r["source"], r["expires_at"]), token_hash))
    for tid in snap.pop("api_token", []):
        r = conn.execute("SELECT * FROM api_token WHERE id=%s", (tid,)).fetchone()
        if r:
            conn.execute("UPDATE api_token SET mac=%s WHERE id=%s", (loc.token_mac(
                r["id"], r["name"], r["token_hash"], r["prefix"], r["scope"], r["revoked_at"] is not None), tid))
    for t, pks in snap.items():
        for pk in pks:
            w, p = _where(t, pk)
            n += seal(conn, t, w, p)
    return n


def rotate_data_key(conn, *, password: str | None) -> dict[str, int]:
    """`Vault.rotate_data_key`, plus signing again every row that verified under the old key. Raises what the vault raises (nothing is
    changed then). Commits."""
    snap = snapshot(conn) if enforced() else {}
    conn.rollback()                                            # rotate_data_key needs an idle connection
    counts = core.vault.rotate_data_key(conn, password=password)
    if snap:
        reseal(conn, snap)
    conn.commit()
    return counts


def adopt(conn) -> dict[str, int]:
    """Sign every row that has no MAC (a database from before row MACs), kit tables included. TRUSTS THE DATABASE AS IT IS NOW. Needs the
    vault open. Commits."""
    if not enforced():
        raise RuntimeError("adopt needs an open vault")
    kit = core.local.adopt_rows(conn)
    counts = {"web_session": kit["sessions"], "api_token": kit["tokens"]}
    counts |= adopt_tables(conn)
    conn.commit()
    return counts


def adopt_tables(conn) -> dict[str, int]:
    """Sign this module's unsigned rows in the caller's transaction (no commit). Used by `adopt` and when the vault is first created."""
    return {t: seal(conn, t, "mac IS NULL") for t in TABLES}


def clear(conn) -> None:
    """Forget every MAC of this module's tables (the data key is gone: none can verify any more; `adopt-rowmacs` signs afresh)."""
    for t in TABLES:
        conn.execute(f"UPDATE {t} SET mac = NULL")             # noqa: S608
