"""The tamper-evident audit ledger (migration 007): who decided or changed what, chained by SHA-256, written in the SAME database
transaction as the change it records. If the change rolls back the entry rolls back with it; if the entry cannot be written the
change does not happen.

The hashing, the append and the verification are SQL functions (`audit_append`, `audit_verify`, `audit_verify_detail`): one
definition, usable from psql by an auditor without this application. This module only builds the payloads, and builds them from
the masking rules of hardening.py: never a card number, a key, a secret, a full IP or a raw customer id. The database refuses
payloads that look like card data or hold a field named like a secret (trigger `audit_ledger_guard`).

Call `record` as the LAST statement before the commit: the append takes the ledger's head row lock and holds it until the commit,
so everything after it makes other writers wait. Nothing here commits.
"""
import hashlib
import re

from psycopg.types.json import Jsonb

MERCHANT = "merchant:"        # actor prefix of a merchant API key
SYSTEM = "system"


def short_hash(value) -> str | None:
    """First 12 hex of SHA-256: the same mask hardening.py applies to identifiers, so a ledger entry joins to a masked view."""
    return None if value in (None, "") else hashlib.sha256(str(value).encode()).hexdigest()[:12]


def mask_ip(ip: str | None) -> str | None:
    """Last octet (IPv4) or last group (IPv6) hidden: 203.0.113.xxx, like hardening.py."""
    if not ip:
        return None
    if ":" in ip:
        return ip.rsplit(":", 1)[0] + ":xxxx"
    head, _, _ = ip.rpartition(".")
    return f"{head}.xxx" if head else "xxx"


def origin(url: str | None) -> str | None:
    """scheme://host[:port] only: a webhook URL can carry a token in its path or query."""
    if not url:
        return None
    m = re.match(r"^(https?://[^/?#]*)", url)
    return (m.group(1) + "/[path hidden]") if m else "[hidden]"


def mask_value(kind: str, value: str | None) -> str | None:
    """A blocklist value, masked like blacklist_masked: IP truncated, bin and e-mail domain as they are, the rest hashed."""
    if value is None:
        return None
    if kind == "ip":
        return mask_ip(value)
    if kind in ("bin", "email_domain"):
        return value
    return short_hash(value)


def merchant_actor(merchant_id: str) -> str:
    return MERCHANT + merchant_id


def record(conn, actor: str | None, action: str, subject: str = "", payload: dict | None = None) -> int:
    """Append one entry in the caller's transaction (it commits or rolls back with it). Returns its seq."""
    row = conn.execute("SELECT audit_append(%s, %s, %s, %s) AS seq",
                       ((actor or SYSTEM)[:120], action[:60], (subject or "")[:120], Jsonb(payload or {}))).fetchone()
    return row["seq"]


def record_all(conn, actor: str | None, events: list[tuple[str, str, dict]]) -> None:
    """Append several (action, subject, payload) entries, in order, as one actor."""
    for action, subject, payload in events:
        record(conn, actor, action, subject, payload)


# ---- reading --------------------------------------------------------------------------------------------------------------
def verify(conn) -> dict:
    """Recompute the whole chain in the database. {"intact", "entries", "first_broken", "reason", "head": {"seq", "hash"}}."""
    row = conn.execute("SELECT * FROM audit_verify_detail()").fetchone()
    conn.rollback()
    return {"intact": row["first_broken"] is None, "entries": row["entries"], "first_broken": row["first_broken"],
            "reason": row["reason"], "head": {"seq": row["head_seq"], "hash": row["head_hash"]}}


def recent(conn, limit: int = 50, subject: str | None = None) -> list[dict]:
    """Newest first."""
    rows = conn.execute("SELECT seq, ts, actor, action, subject, payload, prev_hash, hash FROM audit_ledger "
                        "WHERE (%s::text IS NULL OR subject = %s) ORDER BY seq DESC LIMIT %s", (subject, subject, limit)).fetchall()
    conn.rollback()
    return rows


# ---- payloads --------------------------------------------------------------------------------------------------------------
def payment_payload(tx: dict, fa: dict | None = None, card: dict | None = None, **extra) -> dict:
    """What an auditor needs to reconstruct a decision, with nothing sensitive: ids, amounts, the decision and why."""
    req = tx.get("request") or {}
    out = {"merchant_id": tx["merchant_id"], "amount": tx["amount"], "currency": tx["currency"], "status": tx["status"],
           "capture_method": tx.get("capture_method"), "fraud_score": tx.get("fraud_score"), "fraud_decision": tx.get("fraud_decision"),
           "psp": tx.get("psp_selected"), "decline_code": tx.get("decline_code"),
           "amount_captured": tx.get("amount_captured"), "amount_refunded": tx.get("amount_refunded"),
           "customer": short_hash(tx.get("customer_id")), "ip": mask_ip(req.get("ip")),
           "idempotency_key": short_hash(tx.get("idempotency_key"))}
    if fa:
        out |= {"action_taken": fa.get("action_taken"), "rules_triggered": list(fa.get("tier1_rules_triggered") or []),
                "reasons": list(fa.get("reasons") or []), "model_version": fa.get("model_version")}
    if card:
        out["card"] = {"brand": card.get("brand"), "last4": card.get("last4"), "token": short_hash(card.get("token"))}
    return {**{k: v for k, v in out.items() if v is not None}, **extra}
