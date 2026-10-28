"""Row-level security as defence in depth (migration 008, db.py). Needs a database (set TEST_DATABASE_URL).

The CI database user is usually a superuser, and Postgres exempts superusers from every policy. So the tests that PROVE the policies
connect (and, where it matters, run the whole application) as an ordinary login role made here, with the grants an application role
has and nothing else. Where the connecting role is itself an ordinary owner, one test also proves FORCE binds the owner."""
import httpx
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from tests.conftest import needs_db
from tests.test_db import bearer, pay, tokenize

pytestmark = needs_db

import os  # noqa: E402

from paybroker import audit, core, db, hardening, payments, webhooks, worker  # noqa: E402
from paybroker.db import connect  # noqa: E402

KEY = "operator-test-key-0123456789"


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

PROBE, PROBE_PW = "pb_rls_probe", "rls-probe-pw-throwaway"
SCOPED = ("merchant", "merchant_key", "card", "transaction", "webhook_event", "three_ds_challenge", "fraud_assessment")
OPERATOR_ONLY = ("fraud_case", "chargeback", "admin_audit")


def _url(user: str | None = None, password: str | None = None) -> str:
    d = conninfo_to_dict(os.environ["TEST_DATABASE_URL"])
    return (f"postgresql://{user or d['user']}:{password or d.get('password', '')}@{d.get('host', '127.0.0.1')}:{d.get('port', 5432)}/"
            f"{d['dbname']}")


@pytest.fixture(scope="module")
def probe(_schema):
    """An ordinary login role: the privileges an application role has on the tables (not the audit ledger's UPDATE/DELETE), no ownership,
    no SUPERUSER, no BYPASSRLS."""
    with connect() as c:
        c.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{PROBE}') THEN "
                  f"CREATE ROLE {PROBE} LOGIN PASSWORD '{PROBE_PW}' NOSUPERUSER NOBYPASSRLS; END IF; END $$")
        c.execute(f"ALTER ROLE {PROBE} PASSWORD '{PROBE_PW}'")
        c.execute(f"GRANT USAGE ON SCHEMA public TO {PROBE}")
        c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public TO {PROBE}")
        c.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_ledger FROM {PROBE}")
        c.execute(f"REVOKE DELETE, TRUNCATE ON audit_ledger_head FROM {PROBE}")
        c.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {PROBE}")
        c.commit()
    yield _url(PROBE, PROBE_PW)
    core.db.close_pool()
    with connect() as c:         # (REVOKE, not DROP OWNED: a role creator without SUPERUSER may not drop objects "owned" by a role it cannot use)
        c.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {PROBE}")
        c.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {PROBE}")
        c.execute(f"REVOKE ALL ON SCHEMA public FROM {PROBE}")
        c.execute(f"DROP ROLE {PROBE}")
        c.commit()


def as_probe(url: str, merchant_id: str = "", bypass: bool = False):
    conn = psycopg.connect(url, row_factory=dict_row)
    if merchant_id or bypass:
        conn.execute("SELECT set_config('app.merchant_id', %s, false), set_config('app.bypass', %s, false)",
                     (merchant_id, "on" if bypass else ""))
        conn.commit()
    return conn


@pytest.fixture
def app_as_probe(probe, monkeypatch):
    """The whole application, pool included, connected as the ordinary role for the length of a test."""
    core.db.close_pool()
    monkeypatch.setenv("DATABASE_URL", probe)
    yield
    core.db.close_pool()


def two_shops(client, op):
    out = []
    for name in ("Shop A", "Shop B"):
        mid = client.post("/api/merchants", json={"name": name}, headers=op).json()["id"]
        sk = client.post(f"/api/merchants/{mid}/keys", json={"kind": "secret", "name": "s"}, headers=op).json()["key"]
        pk = client.post(f"/api/merchants/{mid}/keys", json={"kind": "publishable", "name": "p"}, headers=op).json()["key"]
        out.append((mid, bearer(sk), bearer(pk)))
    return out


def populate(client, op):
    """Rows of every kind for two merchants, then the operator-only tables."""
    (a, ask, apk), (b, bsk, bpk) = shops = two_shops(client, op)
    pa = pay(client, ask, tokenize(client, apk, "4000002500003155"), "rls-order-a1", customer="cus_a").json()   # 3-D Secure: a challenge row
    pb = pay(client, bsk, tokenize(client, bpk), "rls-order-b1", customer="cus_b").json()
    with connect() as c:
        for mid, pid in ((a, pa["payment_id"]), (b, pb["payment_id"])):
            c.execute("INSERT INTO webhook_event (event_id, merchant_id, type, body) VALUES (%s,%s,'payment.authorized','{}')", (f"evt_{mid}", mid))
            c.execute("INSERT INTO fraud_case (transaction_id) SELECT id FROM transaction WHERE payment_id=%s", (pid,))
            c.execute("INSERT INTO chargeback (payment_id, amount) VALUES (%s, 100)", (pid,))
        c.commit()
    return shops, pa["payment_id"], pb["payment_id"]


def count(conn, table: str) -> int:
    n = conn.execute(f'SELECT count(*) AS n FROM "{table}"').fetchone()["n"]
    conn.rollback()
    return n


# ---- the policies exist, bind the owner, and say so --------------------------------------------------------------------------
def test_every_merchant_scoped_and_operator_table_has_forced_row_level_security(client):
    with connect() as c:
        st = hardening.rls_status(c)
        assert st["enforced"] and st["missing"] == [] and set(st["tables"]) == set(SCOPED + OPERATOR_ONLY)
        assert set(SCOPED + OPERATOR_ONLY) == set(hardening.RLS_MERCHANT + hardening.RLS_OPERATOR)
        policies = {(r["tablename"], r["policyname"]) for r in c.execute("SELECT tablename, policyname FROM pg_policies WHERE schemaname='public'")}
        for t in SCOPED + OPERATOR_ONLY:
            assert (t, "pb_scope") in policies, t
        c.rollback()
        # the scope policies read the setting through (SELECT ...): one evaluation per statement, not per row
        quals = {r["tablename"]: r["qual"] for r in c.execute("SELECT tablename, qual FROM pg_policies WHERE policyname='pb_scope'")}
        assert "SELECT current_setting('app.merchant_id'" in quals["transaction"] and "merchant_id" in quals["card"]
        idx = {r["indexdef"] for r in c.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='public'")}
        assert any("ON public.transaction USING btree (merchant_id, created_at DESC)" in i for i in idx)
        for t in ("card", "merchant_key", "webhook_event"):
            assert any(f"ON public.{t} " in i and "(merchant_id" in i for i in idx), f"no index on {t}.merchant_id"
        c.rollback()


def test_the_unscoped_pool_and_a_returned_connection_see_nothing(client, shop, app_as_probe):
    mid, sk, pk = shop
    pay(client, sk, tokenize(client, pk), "rls-pool-0001")
    pool = db.pool()
    with db.merchant_conn(mid) as c:
        assert count(c, "transaction") == 1
        pid = c.info.backend_pid
    held = []
    try:
        for _ in range(db.pool().max_size):          # take every connection: the one that just served the merchant is among them
            held.append(pool.getconn())
        for conn in held:
            s = conn.execute("SELECT current_setting('app.merchant_id', true) AS m, current_setting('app.bypass', true) AS b").fetchone()
            conn.rollback()
            assert (s["m"] in (None, "")) and (s["b"] in (None, "")), "a returned connection kept its context"
            assert count(conn, "transaction") == 0, "an unscoped pooled connection must see no rows"
        assert pid in {conn.info.backend_pid for conn in held}, "the connection that served the merchant was not among those checked"
    finally:
        for conn in held:
            pool.putconn(conn)


# ---- fail closed ----------------------------------------------------------------------------------------------------------
def test_an_unset_context_sees_nothing_and_writes_nothing(client, op, probe):
    populate(client, op)
    with connect() as c:                 # (the owner / operator view: the rows are there)
        assert all(count(c, t) > 0 for t in SCOPED + OPERATOR_ONLY)
    with as_probe(probe) as p:
        for t in SCOPED + OPERATOR_ONLY:
            assert count(p, t) == 0, f"{t}: an unset context must see no rows"
        for ddl, args in (("INSERT INTO card (token, merchant_id, brand, bin, last4, exp_month, exp_year, fingerprint) "
                           "SELECT 'tok_unset_1', id, 'visa', '424242', '4242', 12, 2035, 'fp' FROM merchant LIMIT 1", ()),):
            # (the SELECT sees no merchant, so nothing is inserted: also fail closed)
            assert p.execute(ddl, args).rowcount == 0
            p.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
            p.execute("INSERT INTO merchant (id, name) VALUES ('mch_sneaky1', 'x')")
        p.rollback()
        assert p.execute("UPDATE transaction SET amount = 1").rowcount == 0
        assert p.execute("DELETE FROM card").rowcount == 0
        p.rollback()
    with connect() as c:
        assert count(c, "merchant") == 2 and count(c, "card") == 2


@pytest.mark.skipif(os.environ.get("TEST_DATABASE_URL") is None, reason="needs a database")
def test_force_binds_the_table_owner_too(client, op):
    """The application connects as the owner of its tables. When that role is an ordinary one, an unscoped connection of it sees nothing."""
    with connect() as c:
        role = c.execute("SELECT rolsuper OR rolbypassrls AS exempt FROM pg_roles WHERE rolname = current_user").fetchone()["exempt"]
        c.rollback()
    if role:
        pytest.skip("the test database user is a superuser (or BYPASSRLS): Postgres exempts it from policies; the probe-role tests cover it")
    populate(client, op)
    raw = core.db.connect()               # the kit's plain connection: the owner, no context at all
    try:
        assert count(raw, "transaction") == 0 and count(raw, "merchant") == 0 and count(raw, "fraud_case") == 0
        raw.execute("SELECT set_config('app.bypass', 'on', false)")
        assert count(raw, "transaction") == 2
    finally:
        raw.close()


# ---- one merchant cannot see or touch another's rows, even with a buggy query ------------------------------------------------
def test_merchant_a_cannot_read_b_even_with_a_query_that_forgets_the_filter(client, op, probe):
    (a, *_), (b, *_) = populate(client, op)[0]
    with as_probe(probe, merchant_id=a) as p:
        # no WHERE merchant_id anywhere: this is the bug RLS exists for
        for t in ("transaction", "card", "merchant_key", "webhook_event", "three_ds_challenge", "fraud_assessment", "merchant"):
            assert count(p, t) >= 1
        assert {r["merchant_id"] for r in p.execute("SELECT merchant_id FROM transaction")} == {a}
        assert {r["merchant_id"] for r in p.execute("SELECT merchant_id FROM card")} == {a}
        assert {r["merchant_id"] for r in p.execute("SELECT merchant_id FROM merchant_key")} == {a}
        assert {r["merchant_id"] for r in p.execute("SELECT merchant_id FROM webhook_event")} == {a}
        assert [r["id"] for r in p.execute("SELECT id FROM merchant")] == [a]
        assert p.execute("SELECT * FROM transaction WHERE merchant_id = %s", (b,)).fetchall() == []         # asking for B by name: nothing
        assert p.execute("SELECT * FROM card WHERE merchant_id = %s", (b,)).fetchall() == []
        # children are visible only through a payment of A's
        n_tx = count(p, "transaction")
        assert count(p, "fraud_assessment") == n_tx == 1 and count(p, "three_ds_challenge") == 1
        # operator-only tables stay closed to a merchant
        for t in OPERATOR_ONLY:
            assert count(p, t) == 0, t
        # a blanket write touches only A's rows
        assert p.execute("UPDATE transaction SET latency_ms = 1").rowcount == 1
        assert p.execute("DELETE FROM webhook_event").rowcount == 1
        p.rollback()
    with as_probe(probe, merchant_id=b) as p:
        assert [r["merchant_id"] for r in p.execute("SELECT merchant_id FROM transaction")] == [b]
        assert count(p, "three_ds_challenge") == 0              # B's payment had no challenge, and A's is not B's to see


def test_a_merchant_cannot_write_a_row_for_another_merchant(client, op, probe):
    (a, *_), (b, *_) = populate(client, op)[0]
    with as_probe(probe, merchant_id=a) as p:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
            p.execute("INSERT INTO card (token, merchant_id, brand, bin, last4, exp_month, exp_year, fingerprint) "
                      "VALUES ('tok_x_1', %s, 'visa', '424242', '4242', 12, 2035, 'fp')", (b,))
        p.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
            p.execute("INSERT INTO merchant_key (merchant_id, kind, prefix, key_hash) VALUES (%s, 'secret', 'sk_test_xxxx', 'h')", (b,))
        p.rollback()
        # moving one's own row to another merchant is refused too (WITH CHECK)
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
            p.execute("UPDATE card SET merchant_id = %s", (b,))
        p.rollback()
        assert p.execute("UPDATE transaction SET amount = amount WHERE merchant_id = %s", (b,)).rowcount == 0
        p.rollback()


def test_the_payment_pipeline_may_open_a_case_for_its_own_payment_only(client, op, probe):
    (a, *_), (b, *_) = populate(client, op)[0]
    with connect() as c:
        pid_b = c.execute("SELECT id FROM transaction WHERE merchant_id=%s", (b,)).fetchone()["id"]
        c.execute("DELETE FROM fraud_case")
        c.commit()
    with as_probe(probe, merchant_id=a) as p:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
            p.execute("INSERT INTO fraud_case (transaction_id) VALUES (%s)", (pid_b,))
        p.rollback()
        assert p.execute("INSERT INTO fraud_case (transaction_id) SELECT id FROM transaction WHERE merchant_id=%s", (a,)).rowcount == 1
        assert count(p, "fraud_case") == 0                              # insert-only: it cannot read the case back
        p.commit()


# ---- the operator bypass ---------------------------------------------------------------------------------------------------
def test_the_admin_bypass_sees_everything(client, op, probe):
    populate(client, op)
    with as_probe(probe, bypass=True) as p:
        assert count(p, "merchant") == 2 and count(p, "transaction") == 2 and count(p, "card") == 2 and count(p, "merchant_key") == 4
        assert count(p, "fraud_assessment") == 2 and count(p, "three_ds_challenge") == 1 and count(p, "webhook_event") == 2
        assert all(count(p, t) >= 1 for t in OPERATOR_ONLY)
        assert p.execute("UPDATE transaction SET latency_ms = 2").rowcount == 2
        p.rollback()


def test_a_bad_value_for_the_bypass_does_not_open_anything(client, op, probe):
    populate(client, op)
    for value in ("ON", "true", "1", "yes", " on"):
        with psycopg.connect(probe, row_factory=dict_row) as p:
            p.execute("SELECT set_config('app.bypass', %s, false)", (value,))
            assert count(p, "transaction") == 0, value


# ---- the application, end to end, as an ordinary role ---------------------------------------------------------------------
def test_the_app_still_works_as_an_ordinary_role_and_a_buggy_handler_cannot_leak(client, op, shop, app_as_probe, monkeypatch):
    mid, sk, pk = shop
    other = client.post("/api/merchants", json={"name": "Other Shop"}, headers=op).json()["id"]
    osk = bearer(client.post(f"/api/merchants/{other}/keys", json={"kind": "secret", "name": "o"}, headers=op).json()["key"])
    opk = bearer(client.post(f"/api/merchants/{other}/keys", json={"kind": "publishable", "name": "p"}, headers=op).json()["key"])
    mine = pay(client, sk, tokenize(client, pk), "rls-e2e-0001")
    assert mine.status_code == 200
    pid = mine.json()["payment_id"]
    theirs = pay(client, osk, tokenize(client, opk), "rls-e2e-0002").json()["payment_id"]
    # the lifecycle and the dashboard work: merchant routes are scoped, operator routes bypass
    assert client.post(f"/v1/payments/{pid}/capture", headers=sk).json()["status"] == "CAPTURED"
    assert {i["payment_id"] for i in client.get("/v1/payments", headers=sk).json()["items"]} == {pid}
    assert {i["payment_id"] for i in client.get("/api/payments", headers=op).json()["items"]} == {pid, theirs}
    assert client.get(f"/api/payments/{theirs}", headers=op).status_code == 200
    assert client.get("/api/audit/verify", headers=op).json()["intact"] is True
    assert client.get("/api/audit/verify", headers=op).json()["row_level_security"]["effective"] is True

    # now the bug: a handler that forgets the merchant filter
    real_load = payments.load
    monkeypatch.setattr(payments, "load", lambda conn, payment_id, merchant_id=None: real_load(conn, payment_id, None))
    r = client.get(f"/v1/payments/{theirs}", headers=sk)
    assert r.status_code == 404, "the database must hide another merchant's payment even when the query does not ask it to"
    assert client.get(f"/v1/payments/{pid}", headers=sk).status_code == 200
    with connect() as c:                  # the same buggy call, with the operator's context, would have found it: the bug is real
        assert real_load(c, theirs, None)[0]["payment_id"] == theirs
        c.rollback()


def test_workers_and_the_hosted_3ds_page_keep_working_under_row_level_security(client, op, shop, app_as_probe, monkeypatch):
    """The background paths have no merchant: they run with the operator bypass (db.connect). Webhook delivery and the issuer's 3-D Secure page."""
    mid, sk, pk = shop
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert client.post("/api/admin-password", json={"new": "correct horse battery staple"}, headers=op).status_code == 200
    assert client.put(f"/api/merchants/{mid}/webhook", json={"url": "https://merchant.example/hook"}, headers=op).status_code == 200
    monkeypatch.setattr(webhooks, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    p = pay(client, sk, tokenize(client, pk, "4000002500003155"), "rls-3ds-0001").json()
    cid = p["action_required"]["redirect_url"].rsplit("/", 1)[1]
    assert client.post(f"/api/3ds/{cid}/complete", json={"outcome": "succeeded"}, headers={"X-PayBroker-CSRF": "1"}).status_code == 200
    with connect() as c:
        while worker.run_once(c):
            pass
        statuses = {r["status"] for r in c.execute("SELECT status FROM webhook_event")}
        c.rollback()
    assert statuses == {"delivered"}


def test_the_ledger_is_written_by_a_merchant_request_under_row_level_security(client, op, shop, app_as_probe):
    mid, sk, pk = shop
    assert pay(client, sk, tokenize(client, pk), "rls-ledger-0001").status_code == 200
    with connect() as c:
        assert [r["action"] for r in c.execute("SELECT action FROM audit_ledger WHERE action='payment.authorize'")] == ["payment.authorize"]
        assert audit.verify(c)["intact"]
