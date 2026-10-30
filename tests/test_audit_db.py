"""The tamper-evident audit ledger (migration 007, audit.py): written in the payment's own transaction, append-only, chained by
SHA-256, verified by a SQL function. Needs a database (set TEST_DATABASE_URL); the tests tamper with rows on the throwaway one."""
import concurrent.futures as cf
import json

import psycopg
import pytest

from tests.conftest import needs_db, reset_ledger
from tests.test_db import CSRF, bearer, pay, tokenize

pytestmark = needs_db

from paybroker import audit  # noqa: E402
from paybroker.db import connect  # noqa: E402

KEY = "operator-test-key-0123456789-abcdefgh"


@pytest.fixture
def op(monkeypatch):
    monkeypatch.setenv("PAYBROKER_API_KEY", KEY)
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def shop(client, op):
    mid = client.post("/api/merchants", json={"name": "Ledger Shop"}, headers=op).json()["id"]
    sk = client.post(f"/api/merchants/{mid}/keys", json={"kind": "secret", "name": "server"}, headers=op).json()["key"]
    pk = client.post(f"/api/merchants/{mid}/keys", json={"kind": "publishable", "name": "browser"}, headers=op).json()["key"]
    return mid, bearer(sk), bearer(pk)


def entries(conn=None) -> list[dict]:
    with (conn or connect()) as c:
        rows = c.execute("SELECT * FROM audit_ledger ORDER BY seq").fetchall()
        c.rollback()
    return rows


def verify() -> dict:
    with connect() as c:
        return audit.verify(c)


def seed(n: int) -> None:
    with connect() as c:
        for i in range(n):
            audit.record(c, f"tester{i}", "test.event", f"subject-{i}", {"i": i, "note": "entry"})
        c.commit()


def tamper(sql: str, params=()) -> None:
    """What only the table owner can do: let the UPDATE through (privilege granted back, trigger off), then put everything as it was."""
    with connect() as c:
        me = c.execute("SELECT current_user AS u").fetchone()["u"]
        c.execute(f'GRANT UPDATE, DELETE ON audit_ledger TO "{me}"')
        c.execute("ALTER TABLE audit_ledger DISABLE TRIGGER audit_ledger_no_change")
        c.execute(sql, params)
        c.execute("ALTER TABLE audit_ledger ENABLE TRIGGER audit_ledger_no_change")
        c.execute(f'REVOKE UPDATE, DELETE ON audit_ledger FROM "{me}"')
        c.commit()


# ---- what gets written, and in which transaction ----------------------------------------------------------------------
def test_every_decision_and_change_leaves_an_entry_and_the_chain_is_intact(client, shop, op):
    mid, sk, pk = shop
    p = pay(client, sk, tokenize(client, pk), "audit-order-1", capture_method="manual").json()
    pid = p["payment_id"]
    client.post(f"/v1/payments/{pid}/capture", json={"amount": 4000}, headers=sk)
    client.post(f"/v1/payments/{pid}/refund", json={"amount": 1000}, headers=sk)
    v = pay(client, sk, tokenize(client, pk), "audit-order-2").json()
    client.post(f"/v1/payments/{v['payment_id']}/void", headers=sk)
    ch = pay(client, sk, tokenize(client, pk, "4000002500003155"), "audit-order-3").json()
    cid = ch["action_required"]["redirect_url"].rsplit("/", 1)[1]
    assert client.post(f"/api/3ds/{cid}/complete", json={"outcome": "succeeded"}, headers=CSRF).status_code == 200
    rule = client.post("/api/rules", json={"name": "audit-rule", "action": "challenge",
                       "definition": {"all": [{"field": "amount", "op": ">", "value": 10**6}]}}, headers=op).json()["id"]
    client.patch(f"/api/rules/{rule}", json={"enabled": False}, headers=op)
    client.patch(f"/api/rules/{rule}", json={"enabled": True}, headers=op)
    client.delete(f"/api/rules/{rule}", headers=op)
    bl = client.post("/api/blacklist", json={"kind": "ip", "value": "198.51.100.99", "reason": "audit"}, headers=op).json()["id"]
    client.delete(f"/api/blacklist/{bl}", headers=op)
    key = client.post(f"/api/merchants/{mid}/keys", json={"kind": "secret", "name": "second"}, headers=op).json()
    client.delete(f"/api/merchants/{mid}/keys/{key['id']}", headers=op)

    rows = entries()
    actions = [r["action"] for r in rows]
    for want in ("merchant.create", "merchant.key_create", "merchant.key_revoke", "payment.authorize", "payment.captured",
                 "payment.refunded", "payment.voided", "payment.3ds_complete", "rule.create", "rule.disable", "rule.enable",
                 "rule.delete", "blocklist.add", "blocklist.remove"):
        assert want in actions, want
    assert actions.count("payment.authorize") == 3                       # one per authorization decision
    first = next(r for r in rows if r["action"] == "payment.authorize")
    assert first["subject"] == pid and first["actor"] == f"merchant:{mid}"
    assert first["payload"]["fraud_decision"] == "APPROVE" and first["payload"]["amount"] == 5000
    assert first["payload"]["card"]["last4"] == "4242" and first["payload"]["rules_triggered"] == []
    three = next(r for r in rows if r["action"] == "payment.3ds_complete")
    assert three["actor"] == "issuer:3ds-page" and three["payload"]["outcome"] == "succeeded" and three["payload"]["status"] == "AUTHORIZED"
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))     # gapless
    out = verify()
    assert out["intact"] and out["first_broken"] is None and out["entries"] == len(rows) and out["head"]["seq"] == len(rows)
    assert out["head"]["hash"] == rows[-1]["hash"]


def test_a_refused_authorization_still_leaves_evidence(client, shop, op):
    mid, sk, pk = shop
    client.patch(f"/api/merchants/{mid}", json={"settings": {"allowed_currencies": ["EUR"]}}, headers=op)
    r = pay(client, sk, tokenize(client, pk), "audit-refused-1")
    assert r.status_code == 422 and r.json()["error"]["code"] == "CURRENCY_NOT_ALLOWED"
    with connect() as c:
        assert c.execute("SELECT count(*) AS n FROM transaction").fetchone()["n"] == 0     # no payment survived ...
        c.rollback()
    refused = [e for e in entries() if e["action"] == "payment.refused"]                    # ... but the refusal is on record
    assert len(refused) == 1 and refused[0]["payload"]["code"] == "CURRENCY_NOT_ALLOWED"
    assert verify()["intact"]


def test_an_entry_rolls_back_with_the_change_it_records_and_leaves_no_gap(client):
    seed(2)
    with connect() as c:
        audit.record(c, "x", "test.rolled_back", "s", {})
        c.rollback()
    seed(1)
    assert [r["seq"] for r in entries()] == [1, 2, 3] and verify()["intact"]


def test_payloads_never_hold_card_data_secrets_full_ips_or_raw_identifiers(client, shop, op):
    mid, sk, pk = shop
    client.put(f"/api/merchants/{mid}/webhook", json={"url": "https://hooks.example.com/path/token-123"}, headers=op)
    pay(client, sk, tokenize(client, pk), "audit-clean-1", ip="198.51.100.20", customer="cus_audit_secret")
    secret_key = sk["Authorization"].split()[1]
    text = json.dumps([r["payload"] for r in entries()], default=str)
    for leak in ("4242424242424242", secret_key, "198.51.100.20", "cus_audit_secret", "token-123", "pk_test_", "sk_test_", "cvc"):
        assert leak not in text, leak
    auth_entry = next(r for r in entries() if r["action"] == "payment.authorize")
    assert auth_entry["payload"]["ip"] == "198.51.100.xxx" and len(auth_entry["payload"]["customer"]) == 12


def test_the_database_itself_refuses_card_data_and_secret_fields_in_a_payload(client):
    for bad in ({"number": "4242 4242"}, {"cvv": "123"}, {"card": {"pan": "x"}}, {"password": "p"}, {"api_key": "k"},
                {"note": "4242424242424242"}, {"n": 4242424242424242}, {"list": ["4000002500003155"]}):
        with connect() as c, pytest.raises(psycopg.errors.InvalidParameterValue):
            audit.record(c, "x", "test.bad", "s", bad)
    seed(1)                                   # amounts, short hashes and ids are fine
    with connect() as c:
        audit.record(c, "x", "test.ok", "s", {"amount": 99_999_999, "hash": "0123456789ab", "ids": ["pay_abc123"]})
        c.commit()
    assert verify()["intact"]


# ---- append-only: a trigger and privileges, independently -----------------------------------------------------------------
def test_update_delete_and_truncate_are_refused_by_privileges(client):
    """Layer two. (A superuser ignores privileges by design, so there the ACL itself is what is checked; for any other role, the statements.)"""
    seed(3)
    with connect() as c:
        for t, forbidden in (("audit_ledger", {"UPDATE", "DELETE", "TRUNCATE"}), ("audit_ledger_head", {"DELETE", "TRUNCATE"})):
            granted = {r["privilege_type"] for r in c.execute(
                "SELECT (aclexplode(relacl)).privilege_type FROM pg_class WHERE oid = %s::regclass", (t,)).fetchall()}
            assert granted and not (granted & forbidden), (t, granted)         # nobody holds them, the owner included
            assert c.execute("SELECT count(*) AS n FROM information_schema.role_table_grants WHERE table_name=%s AND grantee='PUBLIC'",
                             (t,)).fetchone()["n"] == 0
        is_super = c.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()["rolsuper"]
        c.rollback()
    if not is_super:
        for ddl in ("UPDATE audit_ledger SET actor = 'mallory'", "DELETE FROM audit_ledger", "TRUNCATE audit_ledger",
                    "DELETE FROM audit_ledger_head", "TRUNCATE audit_ledger_head"):
            with connect() as c:
                with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
                    c.execute(ddl)
                c.rollback()
    assert verify()["intact"] and len(entries()) == 3


def test_update_delete_and_truncate_are_refused_by_the_trigger_even_with_the_privilege(client):
    seed(3)
    with connect() as c:
        me = c.execute("SELECT current_user AS u").fetchone()["u"]
        c.execute(f'GRANT UPDATE, DELETE, TRUNCATE ON audit_ledger TO "{me}"')
        c.commit()
    try:
        for ddl in ("UPDATE audit_ledger SET actor = 'mallory'", "DELETE FROM audit_ledger", "TRUNCATE audit_ledger"):
            with connect() as c:
                with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
                    c.execute(ddl)
                c.rollback()
    finally:
        with connect() as c:
            c.execute(f'REVOKE UPDATE, DELETE, TRUNCATE ON audit_ledger FROM "{me}"')
            c.commit()
    assert verify()["intact"] and len(entries()) == 3


# ---- tampering is detected, at exactly the entry --------------------------------------------------------------------------
def test_tampering_with_one_row_as_the_table_owner_is_reported_at_exactly_that_seq(client):
    seed(6)
    assert verify()["intact"]
    with connect() as c:
        assert c.execute("SELECT audit_verify() AS v").fetchone()["v"] is None
        c.rollback()
    tamper("UPDATE audit_ledger SET payload = jsonb_set(payload, '{note}', '\"forged\"') WHERE seq = 4")
    out = verify()
    assert not out["intact"] and out["first_broken"] == 4 and "content" in out["reason"] and out["entries"] == 6
    with connect() as c:
        assert c.execute("SELECT audit_verify() AS v").fetchone()["v"] == 4
        c.rollback()
    assert [r["seq"] for r in entries()] == [1, 2, 3, 4, 5, 6]            # everything else is untouched


@pytest.mark.parametrize("column,value", [("actor", "'mallory'"), ("action", "'test.other'"), ("subject", "'elsewhere'"),
                                          ("ts", "ts + interval '1 second'"), ("prev_hash", "repeat('a', 64)")])
def test_every_column_is_covered_by_the_chain(client, column, value):
    seed(5)
    tamper(f"UPDATE audit_ledger SET {column} = {value} WHERE seq = 2")
    assert verify()["first_broken"] == 2


def test_a_forger_who_recomputes_one_hash_is_caught_at_the_next_entry(client):
    seed(5)
    tamper("UPDATE audit_ledger SET payload = '{\"i\": 99}', hash = audit_record_hash(prev_hash, seq, ts, actor, action, subject, '{\"i\": 99}') "
           "WHERE seq = 3")
    out = verify()
    assert out["first_broken"] == 4 and "prev_hash" in out["reason"]


def test_removed_and_inserted_entries_are_detected(client):
    seed(6)
    tamper("DELETE FROM audit_ledger WHERE seq = 3")
    out = verify()
    assert out["first_broken"] == 4 and "missing" in out["reason"]            # the first entry after the hole
    with connect() as c:
        reset_ledger(c)
    seed(6)
    tamper("DELETE FROM audit_ledger WHERE seq = 6")                            # cut off the end: only the head can tell
    out = verify()
    assert out["first_broken"] == 6 and "head" in out["reason"]
    with connect() as c:
        reset_ledger(c)
    seed(3)
    tamper("INSERT INTO audit_ledger (seq, ts, actor, action, subject, payload, prev_hash, hash) "
           "SELECT 4, now(), 'forger', 'x', '', '{}', repeat('b', 64), repeat('c', 64)")
    assert verify()["first_broken"] == 4


# ---- one linear chain, whatever the concurrency ---------------------------------------------------------------------------
def test_concurrent_writers_produce_one_linear_chain_and_no_fork(client):
    workers, each = 8, 15

    def writer(w: int) -> None:
        for i in range(each):
            with connect() as c:
                audit.record(c, f"w{w}", "test.concurrent", f"{w}-{i}", {"w": w, "i": i})
                c.commit()

    with cf.ThreadPoolExecutor(workers) as ex:
        for f in [ex.submit(writer, w) for w in range(workers)]:
            f.result()
    rows = entries()
    assert len(rows) == workers * each
    assert [r["seq"] for r in rows] == list(range(1, workers * each + 1))
    assert rows[0]["prev_hash"] == "0" * 64
    assert all(rows[i]["prev_hash"] == rows[i - 1]["hash"] for i in range(1, len(rows)))     # each entry has exactly one successor
    assert len({r["prev_hash"] for r in rows}) == len(rows)                                    # no two entries share a parent: no fork
    out = verify()
    assert out["intact"] and out["entries"] == workers * each


def test_concurrent_payments_produce_one_linear_chain(client, shop):
    _, sk, pk = shop
    toks = [tokenize(client, pk) for _ in range(4)]
    from paybroker import payments
    from paybroker.api import merchant_cached

    def one(i: int) -> int:
        with connect() as c:
            body = {"idempotency_key": f"audit-conc-{i}", "amount": 1000 + i, "currency": "USD",
                    "payment_method": {"type": "card", "card_token": toks[i % 4]},
                    "customer": {"id": f"cus_c{i}", "ip_address": f"198.51.100.{100 + i}"}}
            return payments.authorize(c, merchant_cached(c, shop[0]), body, "http://t")[0]

    with cf.ThreadPoolExecutor(6) as ex:
        statuses = list(ex.map(one, range(18)))
    assert all(s in (200, 402, 403) for s in statuses), statuses          # approved, declined or fraud-rejected: all are decisions
    rows = [r for r in entries() if r["action"] == "payment.authorize"]
    assert len(rows) == 18
    out = verify()
    assert out["intact"] and out["entries"] == len(entries())
    assert len({r["prev_hash"] for r in entries()}) == len(entries())


# ---- the API and who may use it -------------------------------------------------------------------------------------------
def test_api_lists_and_verifies_and_reports_the_first_broken_entry(client, op):
    seed(5)
    got = client.get("/api/audit?limit=3", headers=op).json()["items"]
    assert [e["seq"] for e in got] == [5, 4, 3] and got[0]["payload"]["i"] == 4 and len(got[0]["hash"]) == 64
    assert [e["seq"] for e in client.get("/api/audit?subject=subject-1", headers=op).json()["items"]] == [2]
    ok = client.get("/api/audit/verify", headers=op).json()
    assert ok["intact"] is True and ok["entries"] == 5 and ok["first_broken"] is None and ok["head"]["seq"] == 5
    assert ok["row_level_security"]["enforced"] is True and "role_bypasses" in ok["row_level_security"]
    tamper("UPDATE audit_ledger SET actor = 'mallory' WHERE seq = 2")
    bad = client.get("/api/audit/verify", headers=op).json()
    assert bad["intact"] is False and bad["first_broken"] == 2 and bad["reason"]
    assert client.get("/api/audit?limit=0", headers=op).status_code == 422
    assert client.get("/api/audit?limit=501", headers=op).status_code == 422


def test_only_an_admin_credential_may_read_the_ledger(client, shop, op):
    assert client.get("/api/audit").status_code == 401 and client.get("/api/audit/verify").status_code == 401
    _, sk, pk = shop
    for creds in (sk, pk):                                                    # merchant keys never reach an operator route
        assert client.get("/api/audit", headers=creds).status_code == 403
        assert client.get("/api/audit/verify", headers=creds).status_code == 403
    for scope in ("read", "metrics"):                                         # nor does a read-only or metrics operator token
        token = client.post("/api/tokens", json={"name": f"t-{scope}", "scope": scope}, headers=op).json()["token"]
        assert client.get("/api/audit", headers=bearer(token)).status_code == 403
        assert client.get("/api/audit/verify", headers=bearer(token)).status_code == 403
    assert client.get("/api/audit", headers=op).status_code == 200


def test_the_permission_is_admin_only():
    from paybroker import auth, core
    assert "audit:read" in core.PERMISSIONS and core.ROLE_PERMISSIONS["admin"] == core.ALL
    for role, perms in core.ROLE_PERMISSIONS.items():
        if role != "admin":
            assert "audit:read" not in perms and "*" not in perms, role
    for scope in ("read", "metrics", "paybroker.read", "paybroker.metrics", core.ADMIN_SCOPE):
        assert "audit:read" not in core.SCOPE_PERMISSIONS[scope], scope
    assert all("audit:read" not in p for p in auth.MERCHANT_PERMISSIONS.values())


def test_the_ledger_is_classified_readable_and_verifiable_in_plain_sql(client):
    """An auditor with a read-only role needs no application: audit_verify() is plain SQL over a readable table."""
    from paybroker import hardening
    assert {"audit_ledger", "audit_ledger_head"} <= set(hardening.READABLE)
    seed(3)
    with connect() as c:
        assert c.execute("SELECT * FROM audit_verify_detail()").fetchone()["entries"] == 3
        c.rollback()
