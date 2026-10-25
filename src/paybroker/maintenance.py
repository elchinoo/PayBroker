"""Data reset: wipe the TRANSACTION data and keep the configuration.

Transaction data is everything the traffic produced: payments (their events, attempts, captures, refunds, idempotency records
and card references are columns of `transaction`), 3-D Secure challenges, fraud assessments, cases (their decision trail is
columns of `fraud_case`), chargebacks, webhook deliveries, the outbox queue, the feature store (the per-entity velocity
documents the fraud model reads: a reset that kept them would keep the old velocity), and optionally the captured training
rows and the suggested rules. Configuration is never touched: merchants, API keys, webhook endpoints, card tokens, connectors,
rules, the blocked list, settings, AI accounts, uploaded models, users, sessions and operator tokens (see README, "Reset the
data", for the full list and the reasons).

Safe to interrupt and to run again: tables are emptied children first, in chunks of at most CHUNK rows with one commit per
chunk, so every commit leaves the database consistent and a second run simply continues. A session advisory lock keeps two
resets from running at the same time (in any process). Every run leaves one row in `admin_audit`.
"""
import time
from collections.abc import Callable

from psycopg.types.json import Jsonb

from . import routing, rules, training_data
from .db import connect

PHRASE = "RESET PAYBROKER DATA"
CHUNK = 5000
LOCK_NAME = "paybroker.data_reset"
ACTION = "data.reset"

# (table, option that must be on, or None) in the order they are emptied: a table is emptied before the tables it references.
STEPS: tuple[tuple[str, str | None], ...] = (
    ("outbox", None),                       # the queue first, so a worker stops picking up work about payments that are going
    ("webhook_event", None),
    ("three_ds_challenge", None),           # -> transaction
    ("fraud_assessment", None),             # -> transaction
    ("fraud_case", None),                   # -> transaction
    ("chargeback", None),                   # -> transaction.payment_id
    ("transaction", None),
    ("fs_entity", None),                    # the feature store
    ("training_example", "include_training_data"),
    ("rule_suggestion", "include_rule_suggestions"),
)
TABLES = tuple(t for t, _ in STEPS)

KEPT = (
    "merchant", "merchant_key", "card", "psp_connector", "fraud_rule", "blacklist", "llm_account", "model_version",
    "app_setting", "web_session", "oidc_login", "api_token", "admin_audit", "triage_llm_call", "schema_migrations",
)


class ResetError(ValueError):
    pass


class ResetBusy(ResetError):
    """Another reset holds the lock."""


def check_phrase(phrase: str | None) -> None:
    if phrase != PHRASE:
        raise ResetError(f'type the exact phrase "{PHRASE}" to confirm')


def invalidate_caches() -> None:
    """Nothing process-local depends on transaction data (merchant settings, API keys, rules, connectors and the training
    settings are all configuration, which a reset keeps), so this only makes the next request re-read them: harmless, and
    it keeps the contract simple. Other running instances need nothing: their caches expire on their own within 5 to 30 s."""
    routing.invalidate()
    rules.invalidate()
    training_data.invalidate()


def preview(conn) -> dict:
    """Exact row counts of what a reset would delete, and the last resets."""
    counts = {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in TABLES}   # noqa: S608 - constant names
    conn.rollback()
    return {"phrase": PHRASE, "counts": counts, "recent": recent(conn)}


def recent(conn, limit: int = 10) -> list[dict]:
    rows = conn.execute("SELECT id, actor, detail, at FROM admin_audit WHERE action=%s ORDER BY id DESC LIMIT %s",
                        (ACTION, limit)).fetchall()
    conn.rollback()
    return rows


def reset(*, actor: str, include_training_data: bool = True, include_rule_suggestions: bool = True, chunk: int = CHUNK,
          on_chunk: Callable[[str, int], None] | None = None) -> dict:
    """Delete the transaction data. Returns {"deleted": {table: rows}, "skipped": [...], "chunks", "max_chunk_rows", "seconds",
    "audit_id"}. Raises ResetBusy when another reset is running. `on_chunk(table, rows)` is called after each committed chunk
    (tests use it to interrupt a run)."""
    chunk = max(1, min(int(chunk), CHUNK))
    options = {"include_training_data": include_training_data, "include_rule_suggestions": include_rule_suggestions}
    steps = [t for t, opt in STEPS if opt is None or options[opt]]
    skipped = [t for t, opt in STEPS if opt is not None and not options[opt]]
    started = time.monotonic()
    conn = connect(retries=1)
    locked = False
    audit_id = None
    deleted = {t: 0 for t in steps}
    stats = {"chunks": 0, "max_chunk_rows": 0}
    try:
        locked = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS ok", (LOCK_NAME,)).fetchone()["ok"]
        conn.commit()
        if not locked:
            raise ResetBusy("another reset is already running; wait for it to finish")
        audit_id = conn.execute("INSERT INTO admin_audit (actor, action, detail) VALUES (%s,%s,%s) RETURNING id",
                                (actor[:120], ACTION, Jsonb({"status": "running", **options}))).fetchone()["id"]
        conn.commit()
        try:
            for table in steps:
                while True:
                    n = conn.execute(f"DELETE FROM {table} WHERE ctid IN (SELECT ctid FROM {table} LIMIT %s)",   # noqa: S608
                                     (chunk,)).rowcount
                    conn.commit()
                    if n == 0:
                        break
                    deleted[table] += n
                    stats["chunks"] += 1
                    stats["max_chunk_rows"] = max(stats["max_chunk_rows"], n)
                    if on_chunk:
                        on_chunk(table, n)
        except BaseException as exc:
            conn.rollback()
            _finish_audit(conn, audit_id, {"status": "interrupted", "deleted": deleted, "error": type(exc).__name__, **options})
            raise
        seconds = round(time.monotonic() - started, 2)
        _finish_audit(conn, audit_id, {"status": "completed", "deleted": deleted, "skipped": skipped, "seconds": seconds,
                                       **stats, **options})
    finally:
        try:
            if locked:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (LOCK_NAME,))
                conn.commit()
        finally:
            conn.close()
    invalidate_caches()
    return {"deleted": deleted, "skipped": skipped, "seconds": seconds, "audit_id": audit_id, **stats}


def _finish_audit(conn, audit_id: int | None, detail: dict) -> None:
    if audit_id is None:
        return
    try:
        conn.execute("UPDATE admin_audit SET detail=%s WHERE id=%s", (Jsonb(detail), audit_id))
        conn.commit()
    except Exception:  # noqa: BLE001 - the audit row stays "running"; never mask the real outcome
        conn.rollback()
