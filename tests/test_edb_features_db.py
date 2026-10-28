# ruff: noqa: F811

"""EDB / AgentPG features: database insights, read-only hardening with masked views, and the fuzzystrmatch NAME_MISMATCH signal.
Needs the throwaway Postgres (TEST_DATABASE_URL); the roles it makes are cluster-wide and harmless."""
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from tests.conftest import needs_db
from tests.test_db import bearer, op, pay, shop, tokenize  # noqa: F401  (op and shop are fixtures)

pytestmark = needs_db

import os  # noqa: E402

from paybroker import hardening, namematch, rules  # noqa: E402
from paybroker.db import connect  # noqa: E402

LOGIN = "pb_ro_login"


# ---- kit version -----------------------------------------------------------------------------------------------
def test_installed_kit_is_at_least_0_3_1():
    import apgkit
    assert tuple(int(x) for x in apgkit.__version__.split(".")[:3]) >= (0, 3, 1)


# ---- (a) database insights -----------------------------------------------------------------------------------------
def test_insights_needs_settings_manage(client, op):
    assert client.get("/api/insights").status_code in (401, 403)
    read = client.post("/api/tokens", json={"name": "ro", "scope": "read"}, headers=op).json()["token"]
    assert client.get("/api/insights", headers=bearer(read)).status_code == 403
    metrics = client.post("/api/tokens", json={"name": "m", "scope": "metrics"}, headers=op).json()["token"]
    assert client.get("/api/insights", headers=bearer(metrics)).status_code == 403
    full = client.post("/api/tokens", json={"name": "f", "scope": "full"}, headers=op).json()["token"]
    assert client.get("/api/insights", headers=bearer(full)).status_code == 200


def test_insights_degrades_on_plain_postgres(client, op):
    r = client.get("/api/insights?order=mean&limit=5", headers=op)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"sources", "top_queries", "tuner", "advisor"}
    # edb_stat_monitor, edb_pg_tuner and query_advisor do not exist on the throwaway server: an honest state, never a 500
    assert body["tuner"]["available"] is False and body["tuner"]["reason"]
    assert body["advisor"]["available"] is False and body["advisor"]["reason"]
    assert body["top_queries"].get("source") != "edb_stat_monitor"
    assert body["sources"]["edb_stat_monitor"]["available"] is False
    bogus = client.get("/api/insights?order=bogus", headers=op)
    assert bogus.status_code == 200 and bogus.json()["top_queries"].get("order", "total") == "total"
    assert client.get("/api/insights?limit=0", headers=op).status_code == 422


def test_metrics_carry_the_insights_gauges(client, op):
    text = client.get("/metrics", headers=op).text
    assert "db_insights_available" in text


# ---- (b) read-only hardening ------------------------------------------------------------------------------------------
@pytest.fixture
def ro_role():
    with connect() as c:
        c.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='a1p_readonly') THEN CREATE ROLE a1p_readonly NOLOGIN; END IF; END $$")
        c.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{LOGIN}') THEN CREATE ROLE {LOGIN} LOGIN PASSWORD 'ro-pw-test'; END IF; END $$")
        c.execute(f"GRANT a1p_readonly TO {LOGIN}")
        c.commit()
        # what AgentPG's default privileges do for every table
        c.execute("GRANT USAGE ON SCHEMA public TO a1p_readonly")
        c.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO a1p_readonly")
        c.commit()
        res = hardening.apply(c)
    assert all(r["applied"] for r in res.values()), res
    yield
    with connect() as c:                      # leave the schema as the other tests expect it (the owner is unaffected either way)
        c.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM a1p_readonly")
        c.commit()


def _ro():
    return psycopg.connect(make_conninfo(os.environ["TEST_DATABASE_URL"], user=LOGIN, password="ro-pw-test"), row_factory=dict_row, autocommit=True)


def _denied(ro, table):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute(f'SELECT * FROM "{table}"')


def test_read_only_role_sees_masked_views_and_no_secrets(client, shop, op, ro_role):
    mid, sk, pk = shop
    r = pay(client, sk, tokenize(client, pk), "order-ro-0001", customer="cus_secret_1", ip="203.0.113.77")
    assert r.status_code in (200, 403)
    client.post("/api/tokens", json={"name": "t", "scope": "read"}, headers=op)
    with connect() as c:
        c.execute("UPDATE merchant SET webhook_url = 'https://hooks.example.com/pb/abc123?token=SECRET' WHERE id = %s", (mid,))
        c.commit()
        hardening.apply(c)
    with _ro() as ro:
        for table in [*hardening.MASKED, *hardening.HIDDEN]:
            _denied(ro, table)                                   # base tables of masked views, and every secrets table, are denied
        for table in hardening.READABLE:
            ro.execute(f'SELECT * FROM "{table}" LIMIT 1')       # configuration and numbers stay readable
        tx = ro.execute("SELECT * FROM transaction_masked").fetchone()
        assert tx["request"]["ip"] == "203.0.113.xxx" and "device" not in tx["request"] and "customer_id" not in tx["request"]
        assert tx["customer_id"] != "cus_secret_1" and len(tx["customer_id"]) == 12
        assert "request_hash" not in tx and "auth_code" not in tx and "card_token" in tx and len(tx["card_token"]) == 12
        assert ro.execute("SELECT webhook_url FROM merchant_masked").fetchone()["webhook_url"] == "https://hooks.example.com/[path hidden]"
        assert "webhook_secret_enc" not in ro.execute("SELECT * FROM merchant_masked").fetchone()
        card = ro.execute("SELECT * FROM card_masked").fetchone()
        assert set(card) >= {"bin", "last4"} and len(card["fingerprint"]) == 12 and "exp_month" not in card
        assert all("203.0.113.77" not in str(r) for r in ro.execute("SELECT * FROM fs_entity_masked").fetchall())
        for ddl in ("INSERT INTO transaction_masked (id) VALUES (gen_random_uuid())", "DELETE FROM card_masked"):
            with pytest.raises(psycopg.Error):
                ro.execute(ddl)
    with connect() as c:                                          # the app (the owner) is unaffected
        assert c.execute("SELECT customer_id FROM transaction").fetchone()["customer_id"] == "cus_secret_1"


def test_exactly_the_classified_tables_are_readable(ro_role):
    """Whatever the migrations create, the role may read only the views and the READABLE tables; a new table must be classified."""
    with connect() as c:
        assert hardening.unclassified(c) == []
        rows = c.execute(
            "SELECT c.relname, c.relkind, has_table_privilege(%s, c.oid, 'SELECT') AS ok FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','v','p')", (LOGIN,)).fetchall()
    readable = {r["relname"] for r in rows if r["ok"]}
    assert readable == {v for v, _ in hardening.MASKED.values()} | set(hardening.READABLE)


def test_hardening_is_idempotent_skips_without_the_role_and_survives_migrate(ro_role):
    from paybroker import migrate
    with connect() as c:
        assert all(r["applied"] for r in hardening.apply(c).values())
        res = hardening.apply(c, readonly_role="pb_no_such_role")
        assert all(not r["applied"] and "does not exist" in r["reason"] for r in res.values())
    migrate.run()                                                  # the hook is wired into the migrate path
    with _ro() as ro:
        _denied(ro, "app_setting")
        _denied(ro, "merchant_key")
        assert ro.execute("SELECT count(*) AS n FROM transaction_masked").fetchone()["n"] >= 0


def test_new_tables_are_readable_until_classified(ro_role):
    with connect() as c:
        c.execute("CREATE TABLE pb_unclassified (x int)")
        c.execute("GRANT SELECT ON pb_unclassified TO a1p_readonly")   # AgentPG's default privileges do this
        c.commit()
        try:
            assert hardening.unclassified(c) == ["pb_unclassified"]
        finally:
            c.execute("DROP TABLE pb_unclassified")
            c.commit()


# ---- (2) NAME_MISMATCH -----------------------------------------------------------------------------------------------
def test_namematch_tokens_and_compare():
    assert namematch.tokens("  José  O'Brien-Smith, Jr. ") == ["josé", "o", "brien", "smith", "jr"]
    with connect() as c:
        assert namematch.available(c, refresh=True)
        for a, b in [("Jon Smith", "John Smyth"), ("John Smith", "Smith, John"), ("J. Smith", "John Smith"), ("Jon Smith", "JON SMITH"),
                     ("Maria Garcia Lopez", "Maria Garcia"), ("Catherine Zeta", "Katherine Zeta")]:
            assert namematch.mismatch(c, a, b) is False, (a, b)
        for a, b in [("Jon Smith", "Peter Wong"), ("Maria Gonzalez", "Akira Tanaka"), ("Alice Cooper", "Zed Quimby")]:
            assert namematch.mismatch(c, a, b) is True, (a, b)
        assert namematch.mismatch(c, None, "John Smith") is None and namematch.mismatch(c, "John", "") is None
        assert namematch.mismatch(c, "1234", "5678") is None                  # nothing comparable


def test_namematch_without_the_extension_gives_no_signal(monkeypatch):
    monkeypatch.setattr(namematch, "available", lambda conn, refresh=False: False)
    with connect() as c:
        assert namematch.compare(c, "Jon Smith", "Peter Wong") is None


def test_namematch_survives_a_failing_query(monkeypatch):
    monkeypatch.setattr(namematch, "_SQL", "SELECT levenshtein_does_not_exist(1)")
    with connect() as c:
        namematch.available(c, refresh=True)
        assert namematch.compare(c, "Jon Smith", "Peter Wong") is None
        assert c.execute("SELECT 1 AS one").fetchone()["one"] == 1            # the transaction is not poisoned
    namematch._avail.update(at=0.0)


def test_tier1_name_mismatch_is_a_soft_signal_and_a_rule_field():
    ctx = {"amount": 5000, "name_mismatch": True}
    t1 = rules.Tier1(None, ["NAME_MISMATCH"], ["NAME_MISMATCH"])
    assert t1.decision is None and rules.NAME_MISMATCH == "NAME_MISMATCH"
    rules.validate({"all": [{"field": "name_mismatch", "op": "==", "value": True}, {"field": "amount", "op": ">=", "value": 30000}]})
    assert rules.evaluate({"field": "name_mismatch", "op": "==", "value": True}, ctx)
    assert any(r[0] == "name_mismatch_large_basket" for r in rules.DEFAULT_RULES)


def _detail(client, op, payment_id):
    return client.get(f"/api/payments/{payment_id}", headers=op).json()


def _pay_names(client, shop, op, key, card_name, customer_name, amount=5000, customer=None):
    _, sk, pk = shop
    pm = {"type": "card", "card_token": tokenize(client, pk)}
    if card_name is not None:
        pm["cardholder_name"] = card_name
    cust = {"id": customer or f"cus_{key}", "email": "a@example.com", "ip_address": "198.51.100.20"}
    if customer_name is not None:
        cust["name"] = customer_name
    body = {"idempotency_key": key, "amount": amount, "currency": "USD", "payment_method": pm, "customer": cust,
            "telemetry": {"device_fingerprint": "dev_1", "checkout_duration_ms": 40_000}, "shipping_address": {"country": "US"}}
    r = client.post("/v1/payments/authorize", json=body, headers=sk)
    return r, r.json()


def test_payment_with_similar_names_raises_no_signal(client, shop, op):
    r, body = _pay_names(client, shop, op, "order-nm-0001", "Jon Smith", "John Smyth")
    assert r.status_code == 200, r.text
    d = _detail(client, op, body["payment_id"])
    assert "NAME_MISMATCH" not in d["fraud"]["reasons"] and d["fraud"]["tier2_feature_snapshot"]["name_mismatch"] is False


def test_payment_with_unrelated_names_raises_the_signal_but_does_not_block_a_small_basket(client, shop, op):
    r, body = _pay_names(client, shop, op, "order-nm-0002", "Jon Smith", "Peter Wong")
    assert r.status_code == 200, r.text                                              # modest weight: no verdict by itself
    d = _detail(client, op, body["payment_id"])
    assert "NAME_MISMATCH" in d["fraud"]["reasons"] and "NAME_MISMATCH" in d["fraud"]["tier1_rules_triggered"]
    assert d["fraud"]["tier2_feature_snapshot"]["name_mismatch"] is True


def test_unrelated_names_on_a_large_basket_ask_for_3ds(client, shop, op):
    r, body = _pay_names(client, shop, op, "order-nm-0003", "Jon Smith", "Peter Wong", amount=40_000)
    assert body["status"] == "REQUIRES_ACTION", body
    d = _detail(client, op, body["payment_id"])
    assert {"NAME_MISMATCH", "NAME_MISMATCH_LARGE_BASKET"} <= set(d["fraud"]["reasons"])


def test_one_name_missing_means_no_signal_and_names_are_not_stored(client, shop, op):
    for i, (a, b) in enumerate([(None, "Peter Wong"), ("Jon Smith", None), (None, None)]):
        r, body = _pay_names(client, shop, op, f"order-nm-01{i}", a, b)
        assert r.status_code == 200
        d = _detail(client, op, body["payment_id"])
        assert "name_mismatch" not in d["fraud"]["tier2_feature_snapshot"] and "NAME_MISMATCH" not in d["fraud"]["reasons"]
    _, body = _pay_names(client, shop, op, "order-nm-0200", "Zebediah Quux", "Peter Wong")
    with connect() as c:
        dump = str(c.execute("SELECT t.request, fa.tier2_feature_snapshot FROM transaction t JOIN fraud_assessment fa ON fa.transaction_id = t.id").fetchall())
    assert "Zebediah" not in dump and "Peter Wong" not in dump


def test_grants_after_startup_are_reasserted_and_reported(client, op, ro_role):
    """`agentpg db-users create` / default privileges re-grant SELECT on base tables: one re-assert tick closes them again, and
    /api/audit/verify says what was open. The forced row-level security report is untouched."""
    from apgkit import masking

    from paybroker import core
    opened = ["transaction", "app_setting", "merchant_key"]          # a masked base table and two secrets tables
    with connect() as c:
        for t in opened:
            c.execute(f'GRANT SELECT ON "{t}" TO a1p_readonly')
        c.commit()
        assert sorted(masking.drift(c, hardening.DENY)) == sorted(opened)
    seen = client.get("/api/audit/verify", headers=op).json()
    assert sorted(seen["masking"]["open_now"]) == sorted(opened) and seen["row_level_security"]["enforced"] is True
    with _ro() as ro:
        assert ro.execute("SELECT count(*) AS n FROM app_setting").fetchone()["n"] >= 0        # the hole is real
    assert masking.reassert_tick(connect, hardening.apply, deny=hardening.DENY, metrics=core.metrics, name="pb_test") is True
    st = masking.status("pb_test")
    assert st["ok"] is True and st["drift"] == len(opened) and st["last_ok"]
    with connect() as c:
        assert masking.drift(c, hardening.DENY) == []
    with _ro() as ro:
        for t in opened:
            _denied(ro, t)
        assert ro.execute("SELECT count(*) AS n FROM transaction_masked").fetchone()["n"] >= 0
    assert "masking_drift_tables_pb_test" in core.metrics.render()[0].decode()
    after = client.get("/api/audit/verify", headers=op).json()
    assert after["masking"]["open_now"] == [] and set(after["masking"]) == {"last_ok", "drift", "ok", "open_now"}
    assert "masking" in client.get("/readyz").json()
