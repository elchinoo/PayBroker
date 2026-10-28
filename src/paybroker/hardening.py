"""Read-only access hardening for AgentPG's `a1p_readonly` group role (database users made with `--permission read`, and
AgentPG's restricted MCP server, which runs as one).

AgentPG grants SELECT on every new public table to that role by default. This module, run after every migration, takes that back:

* tables with personal data get a `<table>_masked` view (IPs lose the last octet, identifiers become short hashes, merchant
  webhook URLs keep only their origin) and the role loses the base table;
* tables with secrets, hashes or ciphertext (the kit's settings/vault store, sessions, operator tokens, merchant API key
  hashes, LLM account keys, OIDC login state) are taken away from the role with NO view;
* everything else (rules, routing connectors, models, the fraud assessment numbers) stays readable.

Idempotent, and skips quietly when the role does not exist (plain Postgres). The app's own connection owns the tables and sees
everything: this protects the read-only path only. Run it as the owner of the tables (a non-owner REVOKE does nothing, reported as
`base_denied: False`).
"""
import logging

import psycopg
from apgkit import masking
from psycopg import sql

log = logging.getLogger("paybroker.hardening")

READONLY_ROLE = "a1p_readonly"


def _ip(col: str) -> str:
    """Last octet (IPv4) or last group (IPv6) hidden."""
    return (f"CASE WHEN {col} IS NULL THEN NULL WHEN position(':' in {col}) > 0 THEN regexp_replace({col}, '[0-9A-Fa-f]*$', 'xxxx') "
            f"ELSE regexp_replace({col}, '[0-9]+$', 'xxx') END")


def _hash(col: str) -> str:
    return f"substr(encode(sha256(convert_to({col}, 'UTF8')), 'hex'), 1, 12)"


def _origin(col: str) -> str:
    """scheme://host[:port] only: a webhook or return URL can carry a token in its path or query."""
    return f"CASE WHEN {col} IS NULL THEN NULL ELSE regexp_replace({col}, '^(https?://[^/?#]*).*$', '\\1/[path hidden]') END"


_REQ_IP = "(request->>'ip')"
_REQUEST = ("CASE WHEN jsonb_exists(request, 'ip') THEN jsonb_set(request - 'device' - 'customer_id', '{ip}', to_jsonb(" + _ip(_REQ_IP)
            + ")) ELSE request - 'device' - 'customer_id' END")

# table -> (view, {column: rule}). Columns not listed are not exposed (request_hash, auth_code, processor_tx_id, webhook secret...).
MASKED: dict[str, tuple[str, dict[str, str]]] = {
    "transaction": ("transaction_masked", {
        "id": "pass", "payment_id": "pass", "merchant_id": "pass", "idempotency_key": "hash", "amount": "pass", "currency": "pass",
        "status": "pass", "customer_id": "hash", "card_token": "hash", "capture_method": "pass", "request": _REQUEST,
        "fraud_score": "pass", "fraud_decision": "pass", "psp_selected": "pass", "decline_code": "pass", "amount_captured": "pass",
        "amount_refunded": "pass", "latency_ms": "pass", "created_at": "pass", "updated_at": "pass"}),
    "card": ("card_masked", {
        "token": "hash", "merchant_id": "pass", "brand": "pass", "bin": "pass", "last4": "pass", "fingerprint": "hash",
        "sim_behavior": "pass", "created_at": "pass"}),
    "merchant": ("merchant_masked", {
        "id": "pass", "name": "pass", "status": "pass", "webhook_url": _origin("webhook_url"), "settings": "pass",
        "allowed_origins": "pass", "created_at": "pass"}),
    "three_ds_challenge": ("three_ds_challenge_masked", {
        "id": "hash", "transaction_id": "pass", "status": "pass", "created_at": "pass", "expires_at": "pass",
        "return_url": _origin("return_url")}),
    "fs_entity": ("fs_entity_masked", {
        "entity_type": "pass", "entity_id": f"CASE WHEN entity_type = 'ip' THEN {_ip('entity_id')} ELSE {_hash('entity_id')} END",
        "features": "features - 'events' - 'links'", "updated_at": "pass"}),
    "blacklist": ("blacklist_masked", {
        "id": "pass", "kind": "pass",
        "value": f"CASE kind WHEN 'ip' THEN {_ip('value')} WHEN 'bin' THEN value WHEN 'email_domain' THEN value ELSE {_hash('value')} END",
        "reason": "pass", "source": "pass", "created_at": "pass"}),
    "chargeback": ("chargeback_masked", {
        "id": "pass", "payment_id": "pass", "customer_id": "hash", "amount": "pass", "reason": "pass", "reason_code": "pass",
        "created_at": "pass"}),
    "fraud_case": ("fraud_case_masked", {
        "id": "pass", "transaction_id": "pass", "status": "pass", "analyst_decision": "pass", "note": "redact",
        "resolved_by": "initial", "resolved_at": "pass", "created_at": "pass"}),
    "admin_audit": ("admin_audit_masked", {
        "id": "pass", "actor": "initial", "action": "pass", "detail": "pass", "at": "pass"}),
    "webhook_event": ("webhook_event_masked", {
        "event_id": "pass", "merchant_id": "pass", "type": "pass", "status": "pass", "attempts": "pass", "last_code": "pass",
        "created_at": "pass", "delivered_at": "pass"}),
}

# Secrets, hashes, ciphertext: no view at all.
HIDDEN = (
    "app_setting",      # the kit's settings store: vault wraps and ciphertext, admin password hash
    "web_session",      # session token hashes, user names and emails
    "api_token",        # operator API token hashes
    "oidc_login",       # PKCE verifier and nonce of logins in flight
    "merchant_key",     # merchant API key hashes
    "llm_account",      # encrypted LLM provider keys
)

# Readable as they are: configuration and numbers with no personal data.
READABLE = ("fraud_assessment", "fraud_rule", "rule_suggestion", "psp_connector", "outbox", "model_version", "training_example",
            "triage_llm_call", "schema_migrations",
            "audit_ledger", "audit_ledger_head")    # masked at write time; a read-only auditor can read and verify the chain

# Row-level security (migration 008): tables whose rows belong to one merchant, and tables no merchant may ever read. Both are
# FORCEd, so the owner (the app's own role) is bound too. Everything else is shared by design (docs/GUIDE.md section 18).
RLS_MERCHANT = ("merchant", "merchant_key", "card", "transaction", "webhook_event", "three_ds_challenge", "fraud_assessment")
RLS_OPERATOR = ("fraud_case", "chargeback", "admin_audit")


def hide_table(conn: psycopg.Connection, table: str, readonly_role: str = READONLY_ROLE) -> dict:
    """REVOKE SELECT on `table` from the role, no view. Same result shape as `masking.mask_view`."""
    conn.commit()
    prev, conn.autocommit = conn.autocommit, True
    try:
        row = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (readonly_role,)).fetchone()
        if not row:
            return {"applied": False, "reason": f"role {readonly_role} does not exist", "base_denied": False, "view_readable": False}
        conn.execute(sql.SQL("REVOKE SELECT ON {} FROM {}").format(
            sql.Identifier("public", table), sql.Identifier(readonly_role)))
        ok = not conn.execute("SELECT has_table_privilege(%s, %s::regclass, 'SELECT') AS ok",
                              (readonly_role, f'public."{table}"')).fetchone()["ok"]
    finally:
        conn.autocommit = prev
    if not ok:
        log.warning("hardening %s: %s can still read it (not the owner, or PUBLIC has SELECT)", table, readonly_role)
    return {"applied": ok, "reason": "" if ok else "table still readable by the role", "base_denied": ok, "view_readable": False}


def unclassified(conn: psycopg.Connection) -> list[str]:
    """Public tables that are neither masked, hidden nor listed as readable: a new migration must decide for each one."""
    known = set(MASKED) | set(HIDDEN) | set(READABLE)
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1").fetchall()
    return [r["tablename"] for r in rows if r["tablename"] not in known]


def apply(conn: psycopg.Connection, readonly_role: str = READONLY_ROLE) -> dict:
    """The after-migrate hook (`migrate.run(after_migrate=[...])`). Returns {table: result}; logs what it did."""
    out: dict[str, dict] = {}
    for table, (view, columns) in MASKED.items():
        out[table] = masking.mask_view(conn, table, view, columns, readonly_role)
    for table in HIDDEN:
        out[table] = hide_table(conn, table, readonly_role)
    if out and not any(r["applied"] for r in out.values()):
        log.info("read-only hardening skipped: %s", next(iter(out.values()))["reason"])
        return out
    bad = [t for t, r in out.items() if not r["applied"]]
    if bad:
        log.warning("read-only hardening incomplete for: %s", ", ".join(bad))
    for t in unclassified(conn):
        log.warning("table %s is not classified in paybroker.hardening: the read-only role can read it", t)
    return out


def rls_status(conn: psycopg.Connection) -> dict:
    """Is row-level security really binding this connection? `enforced`: every table in RLS_MERCHANT and RLS_OPERATOR has RLS enabled
    and FORCEd. `role_bypasses`: the connected role is a superuser or has BYPASSRLS, which Postgres exempts from every policy (then
    the policies exist but do nothing for the app: use an ordinary owner role). `readonly_door_open`: this role is a real (inheriting)
    member of a1p_readonly and not of a1p_readwrite, which the policies treat as a read-only auditor and let SELECT every row: the app's own
    role must not be one. `effective` is: enforced, no bypass, door closed."""
    want = RLS_MERCHANT + RLS_OPERATOR
    rows = conn.execute("SELECT c.relname, c.relrowsecurity AS on_, c.relforcerowsecurity AS forced FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = ANY(%s)",
                        (list(want),)).fetchall()
    forced = sorted(r["relname"] for r in rows if r["on_"] and r["forced"])
    role = conn.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    bypasses = bool(role and (role["rolsuper"] or role["rolbypassrls"]))
    enforced = len(forced) == len(want)
    door = bool(conn.execute("SELECT pb_readonly_session() AS open").fetchone()["open"])   # the read-only door admits THIS role?
    return {"enforced": enforced, "role_bypasses": bypasses, "readonly_door_open": door,
            "effective": enforced and not bypasses and not door, "tables": forced, "missing": sorted(set(want) - set(forced))}
