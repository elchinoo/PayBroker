"""AIDB mode against a real Postgres with the AIDB extension (image aidb-postgres:17). They run only when AIDB_TEST_URL is set (a superuser DSN);
each module run creates and drops its own database and an UNPRIVILEGED runtime role that owns the app's tables (no superuser, no CREATEROLE), so what
the app can and cannot do at run time is what is tested. Tests that make the database call back into this process (the scripted chat model behind the
gateway model) also need AIDB_TEST_CALLBACK_HOST (Docker Desktop: 192.168.65.254) and a server started with `-c aidb.allow_insecure_egress=on`; they
skip with that reason otherwise. A real chat model is NOT run: the model is a scripted OpenAI-compatible server that returns the tool-call JSON AIDB
expects, so how well a real model follows AIDB's simulated tool-calling format is not verified here (nor is a real gateway).
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from apgkit import aidb
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

AIDB_URL = os.environ.get("AIDB_TEST_URL", "")
CALLBACK = os.environ.get("AIDB_TEST_CALLBACK_HOST", "")
pytestmark = [pytest.mark.aidb, pytest.mark.skipif(not AIDB_URL, reason="set AIDB_TEST_URL (a superuser DSN of a Postgres with AIDB) to run the aidb tests")]

PW = "pb-aidb-test-vault-0123"
RUNTIME_PW = "pb-aidb-runtime-throwaway"       # pragma: allowlist secret
# Values that identify a customer, a card or a device. None of them may ever reach a model or a derived view.
SECRETS = {"customer": "cus_SENTINEL_0042", "device": "dev_SENTINEL_0042", "ip": "203.0.113.77", "card": "tok_SENTINEL_0042",
           "fingerprint": "fp_SENTINEL_0042", "idem": "idem-SENTINEL-0042"}


def _wait(fn, seconds=60.0, step=0.5):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


@pytest.fixture(scope="module")
def env():
    """A database with AIDB, an unprivileged owner role for the app, migrations run as that role, and an open vault."""
    sys.path.insert(0, str(Path(aidb.__file__).resolve().parents[2] / "tests"))        # the kit's scripted model server (editable install)
    tag = uuid.uuid4().hex[:8]
    dbname, role = f"pb_aidb_{tag}", f"pb_app_{tag}"
    root = psycopg.connect(AIDB_URL, autocommit=True)
    root.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEROLE NOCREATEDB").format(sql.Identifier(role), sql.Literal(RUNTIME_PW)))
    root.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(dbname), sql.Identifier(role)))
    admin_dsn = make_conninfo(AIDB_URL, dbname=dbname)
    runtime_dsn = make_conninfo(AIDB_URL, dbname=dbname, user=role, password=RUNTIME_PW)
    with psycopg.connect(admin_dsn, autocommit=True) as a:
        a.execute("CREATE EXTENSION vector")
        a.execute("CREATE EXTENSION aidb CASCADE")
    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "PAYBROKER_AIDB_ADMIN_DSN", "LLM_BASE_URL", "LLM_API_KEY", "LLM_CHAT_MODEL")}
    os.environ["DATABASE_URL"] = runtime_dsn
    os.environ["PAYBROKER_AIDB_ADMIN_DSN"] = admin_dsn
    from paybroker import aidb_features, aidb_mode, core, migrate, rowmac
    from paybroker.db import connect
    migrate.run()
    with connect() as c:
        core.vault.initialize(c, PW)
        c.commit()
    yield {"admin_dsn": admin_dsn, "runtime_dsn": runtime_dsn, "role": role, "db": dbname}
    core.vault.lock()
    for m in (aidb_mode, aidb_features):
        getattr(m, "reset_cache", getattr(m, "reset_retriever", lambda: None))()
    rowmac._reported.clear()
    from paybroker.core import db
    db.close_pool()
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    root.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(dbname)))
    root.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
    for r in ("aidb_p_pb_triage", "aidb_p_pb_ask"):          # cluster-wide roles the setup made
        root.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(r)))
    root.close()


@pytest.fixture(scope="module")
def fake(env):
    if not CALLBACK:
        pytest.skip("set AIDB_TEST_CALLBACK_HOST to an address of this machine reachable from the database server")
    from aidbfake import Fake
    f = Fake()
    yield f
    f.close()


@pytest.fixture(scope="module")
def admin(env):
    with psycopg.connect(env["admin_dsn"], row_factory=dict_row) as c:
        if c.execute("SHOW aidb.allow_insecure_egress").fetchone()["aidb.allow_insecure_egress"] != "on":
            pytest.skip("the server needs aidb.allow_insecure_egress=on to call the plain-http scripted model")
        yield c


@pytest.fixture(scope="module")
def ready(env, admin, fake):
    """`paybroker aidb-setup` run exactly as an operator would (gateway model pointing at the scripted server)."""
    os.environ.update(LLM_BASE_URL=f"http://{CALLBACK}:{fake.port}/v1", LLM_API_KEY="lg_aidb_test_key", LLM_CHAT_MODEL="triage-alias")
    from paybroker import aidb_mode, cli
    assert cli.main(["aidb-setup", "--no-validate"]) == 0
    aidb_mode.reset_cache()
    return env


def runtime():
    from paybroker.db import connect
    return connect()


def catalog_snapshot(admin) -> dict:
    admin.rollback()
    snap = {
        "agents": admin.execute("SELECT name, instructions, model, tools, purpose FROM aidb.agents ORDER BY name").fetchall(),
        "tools": admin.execute("SELECT name, query_text FROM aidb.sql_tool_registry ORDER BY name").fetchall(),
        "purposes": admin.execute("SELECT name, role FROM aidb.purpose_registry ORDER BY name").fetchall(),
        "seals": admin.execute("SELECT kind, name, mac FROM apgkit_aidb_seal ORDER BY kind, name").fetchall(),
        "grants": {r: aidb.role_grants(admin, r) for r in ("aidb_p_pb_triage", "aidb_p_pb_ask")},
        "pipelines": admin.execute("SELECT name, steps::text FROM aidb.pipelines ORDER BY name").fetchall(),
    }
    admin.rollback()
    return json.loads(json.dumps(snap, default=str))


# ---- a case to triage -----------------------------------------------------------------------------------------------------------
def make_case(conn, n: int, *, features: dict, vector: list[float], score: int = 800, decision: str | None = None, category: str | None = None) -> dict:
    """A flagged payment with its assessment, its case, and identifying values (SECRETS) in every column that holds them."""
    conn.execute("INSERT INTO merchant (id, name) VALUES ('mch_aidb', 'Aidb Shop') ON CONFLICT DO NOTHING")
    req = {"customer_id": SECRETS["customer"], "ip": SECRETS["ip"], "device": SECRETS["device"], "shipping_country": "US", "telemetry": {}}
    tx = conn.execute("INSERT INTO transaction (payment_id, merchant_id, idempotency_key, request_hash, amount, currency, status, customer_id, card_token, "
                      "request, fraud_score, fraud_decision) VALUES (%s,'mch_aidb',%s,'h',12345,'EUR','DECLINED',%s,%s,%s,%s,'DECLINE') RETURNING id",
                      (f"pay_aidb_{n}", f"{SECRETS['idem']}-{n}", SECRETS["customer"], SECRETS["card"], Jsonb(req), score)).fetchone()["id"]
    fa = conn.execute("INSERT INTO fraud_assessment (transaction_id, tier1_rules_triggered, tier2_ml_score, tier2_feature_snapshot, reasons, decision, "
                      "feature_vector, tier3_state, tier3_llm_analysis) VALUES (%s,%s,%s,%s,%s,'DECLINE',%s::vector,'queued',%s) RETURNING id",
                      (tx, ["velocity_ip"], score, Jsonb({**features, "last_ip_asn": "AS-SENTINEL"}), ["NEW_DEVICE_FOR_ACCOUNT"],
                       "[" + ",".join(str(x) for x in vector) + "]",
                       Jsonb({"risk_category": category, "recommended_action": "PERMANENT_BLOCK"}) if category else None)).fetchone()["id"]
    case = conn.execute("INSERT INTO fraud_case (transaction_id, analyst_decision, status) VALUES (%s,%s,%s) RETURNING id",
                        (tx, decision, "resolved" if decision else "open")).fetchone()["id"]
    conn.execute("INSERT INTO outbox (topic, payload) VALUES ('fraud.triage', %s)", (Jsonb({"transaction_id": str(tx)}),))
    conn.commit()
    return {"tx": str(tx), "assessment": str(fa), "case": case, "payment_id": f"pay_aidb_{n}"}


TAKEOVER = {"new_device_for_account": True, "impossible_travel_flag": True, "country_mismatch": True, "last_ip_country": "DE",
            "checkout_seconds": 31.0, "tx_count_1h": 1, "account_age_hours": 900.0, "amount_vs_avg_ratio": 6.0}
TESTING = {"card_tx_count_1h": 9, "ip_tx_count_1h": 14, "checkout_seconds": 2.1, "pasted_card_number": True, "distinct_cards_used_7d": 6,
           "account_age_hours": 3.0}
V_TAKEOVER = [3, 5, 1, 2, 4, 0, 1, 1, 0, 0, 0, 1, 1, 3.4, 0, 1, 6.8]
V_TESTING = [1, 1, 9, 20, 2, 6, 14, 3, 9, 0, 0, 0, 0, 1.1, 1, 0, 1.4]


def tool_call(name: str, **args) -> str:
    return json.dumps({"tool_calls": [{"name": name, "arguments": args}]})


GOOD_VERDICT = json.dumps({"risk_category": "ACCOUNT_TAKEOVER", "confidence_score": 0.83, "summary": "New device and impossible travel on an old account.",
                           "key_risk_factors": ["new device", "impossible travel"], "recommended_action": "ALLOW_WITH_KYC", "suggested_rule_updates": ""})


def seen_by_the_model(fake) -> str:
    return json.dumps([body for path, _auth, body in fake.log if path.endswith("/chat/completions")])


# ---- setup -----------------------------------------------------------------------------------------------------------------------
def test_setup_creates_and_seals_everything_and_repeating_it_changes_nothing(ready, admin, capsys):
    from paybroker import aidb_mode, cli
    with runtime() as c:
        s = aidb_mode.inspect(c)
    assert s["state"] == "ready", s
    assert all(f["ready"] for f in s["features"].values()), s["features"]
    assert s["aidb"]["telemetry"] == "database"
    before = catalog_snapshot(admin)
    assert {a["name"] for a in before["agents"]} >= {"pb_triage_agent", "pb_ask_agent"}
    assert {k["kind"] for k in before["seals"]} >= {"agent", "purpose", "sql_tool", "pipeline", "model", "credential", "semantic_kb"}
    capsys.readouterr()
    assert cli.main(["aidb-setup", "--no-validate"]) == 0
    printed = capsys.readouterr().out
    assert catalog_snapshot(admin) == before, "a repeat run must not change a definition, a grant or a seal"
    assert "lg_aidb_test_key" not in printed and "postgresql://" not in printed
    assert cli.main(["aidb-status"]) == 0
    out = capsys.readouterr().out
    assert "state: ready" in out and "triage     ready" in out


def test_the_runtime_role_is_unprivileged_and_cannot_run_setup(ready, admin):
    from paybroker import aidb_mode, core
    flags = admin.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls FROM pg_roles WHERE rolname = %s", (ready["role"],)).fetchone()
    assert not any(flags.values())
    with runtime() as c:
        with pytest.raises(aidb_mode.SetupError, match="CREATEROLE"):
            aidb_mode.setup(c, c, ready["role"], vault=core.vault)


def test_purpose_roles_read_only_the_views_they_were_given(ready, admin):
    triage_grants = aidb.role_grants(admin, "aidb_p_pb_triage")
    assert triage_grants == [["pb_ai", "case_features", "SELECT"]]
    ask = {(s, t) for s, t, p in aidb.role_grants(admin, "aidb_p_pb_ask") if s != "aidb_internal"}
    assert ask == {("pb_ask", "payments"), ("pb_ask", "cases"), ("pb_ask", "merchants")}
    assert {p for _, _, p in aidb.role_grants(admin, "aidb_p_pb_ask")} == {"SELECT"}
    forbidden = ["transaction", "card", "merchant", "merchant_key", "fraud_case", "fraud_assessment", "app_setting", "llm_account", "web_session",
                 "api_token", "audit_ledger", "chargeback", "fs_entity", "blacklist", "outbox", "training_example"]
    for role in ("aidb_p_pb_triage", "aidb_p_pb_ask"):
        flags = admin.execute("SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        assert not any(flags.values()), role
        for t in forbidden:
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not admin.execute("SELECT has_table_privilege(%s, %s, %s) AS ok", (role, f"public.{t}", priv)).fetchone()["ok"], (role, t, priv)
        for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            for rel in ("pb_ai.case_features", "pb_ai.case_narrative", "pb_ask.payments"):
                assert not admin.execute("SELECT has_table_privilege(%s, %s, %s) AS ok", (role, rel, priv)).fetchone()["ok"], (role, rel, priv)
    assert not admin.execute("SELECT has_table_privilege('aidb_p_pb_triage', 'pb_ai.case_narrative', 'SELECT') AS ok").fetchone()["ok"]
    admin.rollback()


def test_the_views_obey_row_level_security(ready):
    """The views run with the owner's rights but still honour migration 008: a session that is not the operator's sees no row."""
    with runtime() as c:
        make_case(c, 41, features=TAKEOVER, vector=V_TAKEOVER)
        n = c.execute("SELECT count(*) AS n FROM pb_ai.case_features").fetchone()["n"]
    assert n >= 1
    with psycopg.connect(ready["runtime_dsn"], row_factory=dict_row) as plain:            # no app.bypass, no app.merchant_id
        for view in ("pb_ai.case_features", "pb_ask.payments", "pb_ask.cases"):
            assert plain.execute(f"SELECT count(*) AS n FROM {view}").fetchone()["n"] == 0, view      # noqa: S608
        plain.execute("SELECT set_config('app.merchant_id', 'mch_other', false)")
        assert plain.execute("SELECT count(*) AS n FROM pb_ask.payments").fetchone()["n"] == 0


# ---- Tier 3 triage by the agent ----------------------------------------------------------------------------------------------------
def test_triage_by_the_agent_is_advisory_audited_and_sees_only_derived_features(ready, admin, fake, monkeypatch):
    from paybroker import audit, triage
    monkeypatch.setenv("PAYBROKER_TRIAGE_METHOD", "aidb_agent")
    with runtime() as c:
        old = make_case(c, 1, features=TESTING, vector=V_TESTING, decision="PERMANENT_BLOCK", category="CARD_TESTING")
        cur = make_case(c, 2, features=TAKEOVER, vector=V_TAKEOVER)
        c.execute("DELETE FROM outbox")
        c.commit()
        fake.log.clear()
        fake.script = [tool_call("pb_get_case", assessment_id=cur["assessment"]), tool_call("pb_nearest_cases", assessment_id=cur["assessment"]), GOOD_VERDICT]
        before_block = c.execute("SELECT count(*) AS n FROM blacklist").fetchone()["n"]
        assert triage.run(c, cur["tx"]) == "aidb"
        row = c.execute("SELECT tier3_state, tier3_llm_analysis AS a FROM fraud_assessment WHERE id = %s", (cur["assessment"],)).fetchone()
        assert row["tier3_state"] == "done" and row["a"]["source"] == "aidb_agent" and row["a"]["recommended_action"] == "ALLOW_WITH_KYC"
        assert row["a"]["agent"] == "pb_triage_agent" and row["a"]["conversation_id"]
        # advisory: nothing was blocked or resolved, the case is still open
        assert c.execute("SELECT count(*) AS n FROM blacklist").fetchone()["n"] == before_block
        assert c.execute("SELECT status FROM fraud_case WHERE id = %s", (cur["case"],)).fetchone()["status"] == "open"
        # every call is in the ledger, in the same transaction as the verdict, and the chain verifies
        entries = [e for e in audit.recent(c, 20) if e["action"] == "triage.aidb"]
        assert len(entries) == 1 and entries[0]["subject"] == cur["payment_id"]
        p = entries[0]["payload"]
        assert p["outcome"] == "ok" and p["agent"] == "pb_triage_agent" and p["purpose"] == "pb_triage" and p["case_id"] == cur["case"]
        assert p["risk_category"] == "ACCOUNT_TAKEOVER" and p["conversation_id"] == row["a"]["conversation_id"]
        assert audit.verify(c)["intact"]
    # the model was shown the derived features and the neighbour, and none of the identifying values
    seen = seen_by_the_model(fake)
    assert "impossible_travel_flag" in seen and "pay_aidb" not in seen
    assert str(old["case"]) in seen and "PERMANENT_BLOCK" in seen                       # the neighbour and how the analyst resolved it
    for what, value in SECRETS.items():
        assert value not in seen, f"{what} reached the model"
    assert "AS-SENTINEL" not in seen


def test_a_bad_verdict_or_a_failing_agent_falls_back_and_is_still_recorded(ready, fake, monkeypatch):
    from paybroker import audit, triage
    monkeypatch.setenv("PAYBROKER_TRIAGE_METHOD", "aidb_agent")
    with runtime() as c:
        a = make_case(c, 3, features=TAKEOVER, vector=V_TAKEOVER)
        fake.script = ["I think this looks fine, no JSON here."]
        assert triage.run(c, a["tx"]) == "heuristic"
        row = c.execute("SELECT tier3_llm_analysis AS a FROM fraud_assessment WHERE id = %s", (a["assessment"],)).fetchone()
        assert row["a"]["source"] == "heuristic"                                          # the unchanged path answered
        e = next(e for e in audit.recent(c, 10) if e["action"] == "triage.aidb")
        assert e["payload"]["outcome"] == "fallback" and "unusable verdict" in e["payload"]["reason"]
        # a verdict outside the allowed values is refused like any untrusted model output
        b = make_case(c, 4, features=TAKEOVER, vector=V_TAKEOVER)
        fake.script = [json.dumps({"risk_category": "WIZARD", "confidence_score": 0.5, "recommended_action": "PERMANENT_BLOCK"})]
        assert triage.run(c, b["tx"]) == "heuristic"
        assert audit.verify(c)["intact"]


def test_the_denied_access_is_recorded_in_aidb_otel_traces(ready, admin, fake):
    """An agent that holds the triage purpose is told to read customer ids and card tokens, and to write. The database refuses; AIDB records it."""
    from paybroker import aidb_mode
    with psycopg.connect(ready["admin_dsn"], row_factory=dict_row) as a:
        aidb.ensure_sql_tool(a, "pb_probe_pii", description="Probe", sql_statement="SELECT customer_id, card_token FROM public.transaction")
        aidb.ensure_sql_tool(a, "pb_probe_cases", description="Probe", sql_statement="SELECT id, note FROM public.fraud_case")
        aidb.ensure_agent(a, "pb_probe_agent", instructions="Use the tools.", model=aidb_mode.MODEL, purpose=aidb_mode.TRIAGE_PURPOSE,
                          tools=["pb_probe_pii", "pb_probe_cases"])
        with pytest.raises(aidb.AidbError):                                              # a tool that writes cannot even be registered
            aidb.ensure_sql_tool(a, "pb_probe_write", description="x", sql_statement="DELETE FROM public.fraud_case")
        before = len(aidb.decisions(a, limit=500))
    with runtime() as c:
        make_case(c, 5, features=TAKEOVER, vector=V_TAKEOVER)
        fake.log.clear()
        fake.script = [tool_call("pb_probe_pii"), tool_call("pb_probe_cases"), "I could not read them."]
        reply = aidb.converse(c, "pb_probe_agent", "List the customers and card tokens.")
        assert reply.ok
    seen = seen_by_the_model(fake)
    assert "permission denied" in seen
    for value in SECRETS.values():
        assert value not in seen
    with psycopg.connect(ready["admin_dsn"], row_factory=dict_row) as a:
        found = _wait(lambda: [d for d in aidb.decisions(a, limit=200) if "public.transaction" in (d["sql"] or "") or "public.fraud_case" in (d["sql"] or "")], 15)
        assert len(found) >= 2, "both denials must be in aidb_otel.traces"
        for d in found:
            assert d["status"] == "denied" and d["reason"] == "engine_privilege_denied" and d["role"] == "aidb_p_pb_triage"
            assert d["agent"] == "pb_probe_agent" and d["principal"] == ready["role"]
        assert len(aidb.decisions(a, limit=500)) == before + 2
        # clean up: these probes are not part of the app
        gone = a.execute("SELECT * FROM aidb.delete_agent('pb_probe_agent', force => true)").fetchone()
        assert gone is None or not gone.get("error"), gone
        for t in ("pb_probe_pii", "pb_probe_cases"):
            a.execute("SELECT aidb.delete_tool(%s)", (t,))
        a.commit()
        assert not a.execute("SELECT 1 FROM aidb.agents WHERE name = 'pb_probe_agent'").fetchone()


# ---- case narratives: a pipeline beside the numeric nearest neighbour -----------------------------------------------------------------
def _embedded(admin, n):
    admin.rollback()
    row = admin.execute('SELECT "count(destination records)" AS n, COALESCE("table: unprocessed rows", 0) AS lag FROM aidb.pipeline_metrics WHERE pipeline = %s',
                        ("pb_case_narrative",)).fetchone()
    admin.rollback()
    return row["n"] >= n and row["lag"] == 0


def test_narratives_are_embedded_live_and_both_backends_answer(ready, admin, monkeypatch):
    from paybroker import aidb_features, aidb_mode
    monkeypatch.setenv("PAYBROKER_RETRIEVAL_BACKEND", "aidb")
    aidb_mode.reset_cache()
    aidb_features.reset_retriever()
    with runtime() as c:
        ring = {**TAKEOVER, "historical_chargeback_count": 2, "device_linked_chargebacks": 1, "amount_vs_avg_ratio": 9.0}
        a = make_case(c, 11, features=ring, vector=V_TAKEOVER, decision="PERMANENT_BLOCK", category="ACCOUNT_TAKEOVER")
        b = make_case(c, 12, features=TESTING, vector=V_TESTING, decision="DISMISS_ALERT", category="CARD_TESTING")
        cur = make_case(c, 13, features={**ring, "tx_count_1h": 2}, vector=[x + 0.1 for x in V_TAKEOVER])
        for case in (a, b, cur):                                   # what triage and resolution do when an AIDB feature is switched on
            aidb_features.touch(c, case_id=case["case"])
        c.commit()
        rows = c.execute("SELECT case_id, narrative FROM pb_ai.case_narrative ORDER BY case_id").fetchall()
    text = {r["case_id"]: r["narrative"] for r in rows}
    assert "new device for the account" in text[a["case"]] and "Analyst decision: PERMANENT_BLOCK" in text[a["case"]]
    assert "card number pasted" in text[b["case"]]
    for value in SECRETS.values():
        assert all(value not in t for t in text.values()), "an identifier reached a narrative"
    assert _wait(lambda: _embedded(admin, 3), 90), "the live pipeline did not embed the narratives"
    with runtime() as c:
        def order(res):
            ids = [i["case_id"] for i in res["narrative"]["items"]]
            assert a["case"] in ids and cur["case"] not in ids
            assert b["case"] not in ids or ids.index(a["case"]) < ids.index(b["case"]), "an account-takeover ring is closer than card testing"
            return res["narrative"]["items"][ids.index(a["case"])]
        out = aidb_features.similar(c, cur["case"], k=8)
        assert out["narrative"]["backend"] == "aidb" and "fallback" not in out["narrative"]
        assert order(out)["analyst_decision"] == "PERMANENT_BLOCK"
        assert out["numeric"]["items"], "the numeric nearest neighbours come beside the narrative ones"
        # the same question through the app backend (full-text search) puts the same case first
        monkeypatch.setenv("PAYBROKER_RETRIEVAL_BACKEND", "app")
        aidb_features.reset_retriever()
        app = aidb_features.similar(c, cur["case"], k=8)
        assert app["narrative"]["backend"] == "app" and app["narrative"]["items"][0]["case_id"] == a["case"]
        order(app)
        # free text works too
        monkeypatch.setenv("PAYBROKER_RETRIEVAL_BACKEND", "aidb")
        aidb_features.reset_retriever()
        q = aidb_features.similar(c, cur["case"], k=1, query="card number pasted, very fast checkout, many payments from one IP address")
        assert q["narrative"]["backend"] == "aidb" and q["narrative"]["items"][0]["case_id"] == b["case"]
        # a case that gets an analyst decision is re-embedded by the Live pipeline with no app code beyond the narrative refresh
        c.execute("UPDATE fraud_case SET status='resolved', analyst_decision='ALLOW_WITH_KYC' WHERE id = %s", (cur["case"],))
        aidb_features.touch(c, case_id=cur["case"])
        c.commit()
    aidb_mode.reset_cache()

    def reembedded():
        with runtime() as c2:
            hits = aidb.retrieve(c2, aidb_mode.KB, "Analyst decision: ALLOW_WITH_KYC", 10)
            c2.rollback()
        return any(h["key"] == str(cur["case"]) and "ALLOW_WITH_KYC" in (h["text"] or "") for h in hits)
    assert _wait(reembedded, 60), "the changed narrative was not re-embedded by the Live pipeline"


def test_the_retrieval_backend_falls_back_to_app_when_aidb_cannot_answer(ready, admin, monkeypatch, caplog):
    """AIDB fails at query time: the switch says aidb, the answer comes from the app backend, and the log says why."""
    from paybroker import aidb_features, aidb_mode
    monkeypatch.setenv("PAYBROKER_RETRIEVAL_BACKEND", "aidb")
    with runtime() as c:
        case = make_case(c, 14, features=TAKEOVER, vector=V_TAKEOVER)
        aidb_features.touch(c, case_id=case["case"])
        c.commit()
        aidb_mode.reset_cache()
        aidb_features.reset_retriever()
        real = aidb_features.retrieval.AidbRetrieval.retrieve

        def boom(self, *a, **k):
            raise psycopg.errors.InsufficientPrivilege("permission denied for table pipeline_pb_case_narrative")
        monkeypatch.setattr(aidb_features.retrieval.AidbRetrieval, "retrieve", boom)
        out = aidb_features.similar(c, case["case"], k=2)
        assert out["narrative"]["backend"] == "app" and "InsufficientPrivilege" in out["narrative"]["fallback"]
        assert any("RETRIEVAL BACKEND FALLBACK" in r.message for r in caplog.records)
        monkeypatch.setattr(aidb_features.retrieval.AidbRetrieval, "retrieve", real)
    aidb_features.reset_retriever()


# ---- analyst questions: text to SQL under a read-only purpose ---------------------------------------------------------------------------
def test_analyst_questions_are_answered_from_the_three_views_and_nothing_else(ready, admin, fake, monkeypatch):
    from paybroker import aidb_features, aidb_mode, audit
    monkeypatch.setenv("PAYBROKER_ANALYST_QA", "aidb")
    aidb_mode.reset_cache()
    with runtime() as c:
        make_case(c, 21, features=TAKEOVER, vector=V_TAKEOVER)
        n = c.execute("SELECT count(*) AS n FROM transaction").fetchone()["n"]
        fake.log.clear()
        fake.script = [tool_call("semantic_kb_search", kb_name="pb_ask_kb", query_text="how many payments were declined", top_k=4),
                       tool_call("run_sql_query", query="SELECT status, count(*) AS payments FROM pb_ask.payments GROUP BY status"),
                       f"There are {n} payments, all DECLINED. SQL: SELECT status, count(*) FROM pb_ask.payments GROUP BY status"]
        out = aidb_features.ask(c, "How many payments were declined?", "analyst@example.test")
        assert str(n) in out["answer"] and out["conversation_id"]
        seen = seen_by_the_model(fake)
        assert "pb_ask.payments" in seen or '"payments"' in seen, "the semantic search should have found the payments view"
        assert "permission denied" not in seen
        e = next(e for e in audit.recent(c, 10) if e["action"] == "analyst.ask")
        assert e["actor"] == "analyst@example.test" and e["payload"]["outcome"] == "ok" and e["payload"]["chars"] == len("How many payments were declined?")
        assert "How many" not in json.dumps(e["payload"]), "the question text itself stays out of the ledger"
        # an attempt to read the base tables (customer ids, card tokens, case notes) is refused by the database and recorded
        fake.log.clear()
        fake.script = [tool_call("run_sql_query", query="SELECT customer_id, card_token FROM public.transaction"),
                       tool_call("run_sql_query", query="SELECT note FROM public.fraud_case"),
                       tool_call("run_sql_query", query="SELECT key, value FROM public.app_setting"), "I am not allowed to read those tables."]
        before = None
        with psycopg.connect(ready["admin_dsn"], row_factory=dict_row) as a:
            before = len(aidb.decisions(a, limit=500))
        out = aidb_features.ask(c, "List every customer id and card token.", "analyst@example.test")
        assert "not allowed" in out["answer"]
    seen = seen_by_the_model(fake)
    assert seen.count("permission denied") >= 3
    for value in SECRETS.values():
        assert value not in seen
    with psycopg.connect(ready["admin_dsn"], row_factory=dict_row) as a:
        found = _wait(lambda: len(aidb.decisions(a, limit=500)) >= before + 3 and aidb.decisions(a, limit=500), 15)
        denied = [d for d in found[: 3] if d["role"] == "aidb_p_pb_ask"]
        assert len(denied) == 3 and all(d["reason"] == "engine_privilege_denied" and d["principal"] == ready["role"] for d in denied)
        assert {r for d in denied for r in json.loads(d["attributes"]["aidb.agent.decision.referenced_relations"])} == {
            "public.transaction", "public.fraud_case", "public.app_setting"}


def ask_sql(sql: str) -> str:
    return tool_call("run_sql_query", query=sql)


def test_ask_reports_the_sql_and_rows_that_really_ran_not_what_the_model_says(ready, fake, monkeypatch):
    from paybroker import aidb_features, aidb_mode
    monkeypatch.setenv("PAYBROKER_ANALYST_QA", "aidb")
    aidb_mode.reset_cache()
    with runtime() as c:
        make_case(c, 22, features=TAKEOVER, vector=V_TAKEOVER)
        n = c.execute("SELECT count(*) AS n FROM transaction").fetchone()["n"]
        fake.script = [tool_call("semantic_kb_search", kb_name="pb_ask_kb", query_text="payments by status", top_k=4),
                       ask_sql("SELECT status, count(*) AS payments FROM pb_ask.payments GROUP BY status ORDER BY status"),
                       "There are 999 payments. SQL: SELECT 999 AS payments"]       # what the model SAYS it ran is not what ran
        out = aidb_features.ask(c, "How many payments are there per status?", "analyst@example.test")
        assert out["executed"] is True and out["retried"] is False and out["sql_error"] == ""
        assert "GROUP BY status" in out["sql"] and "999" not in out["sql"]
        assert out["rows"] == [{"status": "DECLINED", "payments": n}] and out["row_count"] == 1 and not out["truncated"]
        assert [s["tool"] for s in out["steps"]] == ["semantic_kb_search", "run_sql_query"] and all(s["ok"] for s in out["steps"])
        assert "999" in out["answer"]                      # the text is the model's; the table next to it is the database's


def test_a_model_that_writes_sql_instead_of_running_it_is_retried_once_then_reported(ready, fake, monkeypatch):
    """Seen with a real model (gpt-4.1-mini): it wrote a SELECT and returned that text as its answer without calling run_sql_query. The old code
    passed the text through. Now: one retry that tells it to run the query; if it still does not, `executed: false`, no answer."""
    from paybroker import aidb_features, aidb_mode, audit
    monkeypatch.setenv("PAYBROKER_ANALYST_QA", "aidb")
    aidb_mode.reset_cache()
    sql = "SELECT count(*) AS payments FROM pb_ask.payments"
    with runtime() as c:
        make_case(c, 23, features=TAKEOVER, vector=V_TAKEOVER)
        n = c.execute("SELECT count(*) AS n FROM transaction").fetchone()["n"]
        # 1. the retry succeeds: the second call runs the query with the tool
        fake.log.clear()
        fake.script = [f"Here is the query: {sql};", ask_sql(sql), f"There are {n} payments."]
        out = aidb_features.ask(c, "How many payments are there?", "analyst@example.test")
        assert out["retried"] is True and out["executed"] is True and out["answer"] == f"There are {n} payments."
        assert out["sql"] == sql and out["rows"] == [{"payments": n}]
        prompts = [json.dumps(b) for p, _a, b in fake.log if p.endswith("/chat/completions")]
        assert len(prompts) == 3 and "without running a query" in prompts[1] and "How many payments are there?" in prompts[1]   # same conversation, nudged once
        e = next(e for e in audit.recent(c, 10) if e["action"] == "analyst.ask")
        assert e["payload"]["outcome"] == "ok" and e["payload"]["retried"] is True and e["payload"]["rows"] == 1
        # 2. it never runs it: nothing is presented as an answer
        fake.script = ["SELECT count(*) FROM pb_ask.payments;", f"The query is {sql};"]
        out = aidb_features.ask(c, "How many payments are there?", "analyst@example.test")
        assert out["executed"] is False and out["retried"] is True and out["answer"] is None and out["sql"] is None and out["rows"] == []
        assert "did not run a query" in out["sql_error"] and sql in out["unverified_text"]
        e = next(e for e in audit.recent(c, 10) if e["action"] == "analyst.ask")
        assert e["payload"]["outcome"] == "no_query"
        # 3. a query that was refused is reported as a failed query, with its error and no rows
        fake.script = [ask_sql("SELECT customer_id FROM public.transaction"), "I could not read that."]
        out = aidb_features.ask(c, "List the customers.", "analyst@example.test")
        assert out["executed"] is True and out["rows"] == [] and "permission denied" in out["sql_error"] and out["retried"] is False
        e = next(e for e in audit.recent(c, 10) if e["action"] == "analyst.ask")
        assert e["payload"]["outcome"] == "sql_error" and e["payload"]["denied_calls"] == 1


def test_aidb_offers_sleep_to_every_agent_and_it_cannot_be_removed(ready, admin, fake):
    """Finding (AIDB 7.9.0, docs: native tools): `sleep` is injected into every agent regardless of its tool list. There is no API to remove it
    from an agent, and the native tool cannot be deleted. What the app can do is say so in the instructions and bound the run."""
    from paybroker import aidb_mode
    with runtime() as c:
        fake.log.clear()
        fake.script = ["done"]
        aidb.converse(c, aidb_mode.ASK_AGENT, "hello")
    seen = seen_by_the_model(fake)
    assert "- sleep:" in seen and "pb_get_case" not in seen         # offered although the agent's tools do not list it
    with pytest.raises(psycopg.Error, match="cannot be deleted"):
        admin.execute("SELECT aidb.delete_tool('sleep')")
    admin.rollback()
    agents = {a["name"]: a for a in admin.execute("SELECT name, instructions, max_iterations FROM aidb.agents WHERE name LIKE 'pb\\_%%'").fetchall()}
    admin.rollback()
    assert "sleep" not in (admin.execute("SELECT tools FROM aidb.agents WHERE name = 'pb_triage_agent'").fetchone()["tools"] or [])
    admin.rollback()
    assert agents["pb_triage_agent"]["max_iterations"] == aidb_mode.TRIAGE_MAX_ITERATIONS <= 6
    assert agents["pb_ask_agent"]["max_iterations"] == aidb_mode.ASK_MAX_ITERATIONS <= 8
    for a in agents.values():
        assert "Never call the sleep tool" in a["instructions"]


def sleep_call(seconds: int) -> str:
    return tool_call("sleep", seconds=seconds)


def test_a_triage_run_that_keeps_calling_sleep_ends_and_falls_back(ready, fake, monkeypatch):
    """A model that keeps calling the built-in sleep tool (refused by the purpose each time, 32 denied decisions in one real run) is stopped by AIDB
    (identical failures, or the iteration limit), the app falls back, and the ledger says how many calls were refused."""
    from paybroker import audit, triage
    monkeypatch.setenv("PAYBROKER_TRIAGE_METHOD", "aidb_agent")
    with runtime() as c:
        a = make_case(c, 61, features=TAKEOVER, vector=V_TAKEOVER)
        # identical failures: AIDB gives up after three
        fake.log.clear()
        fake.script = [sleep_call(5)] * 20
        assert triage.run(c, a["tx"]) == "heuristic"
        e = next(e for e in audit.recent(c, 10) if e["action"] == "triage.aidb")
        assert e["payload"]["outcome"] == "fallback" and e["payload"]["denied_calls"] == 3 and "failed identically" in e["payload"]["reason"]
        assert len([1 for p, _a, _b in fake.log if p.endswith("/chat/completions")]) <= 4
        # varying arguments: the iteration limit ends it
        b = make_case(c, 62, features=TAKEOVER, vector=V_TAKEOVER)
        fake.log.clear()
        fake.script = [sleep_call(i + 1) for i in range(30)]
        assert triage.run(c, b["tx"]) == "heuristic"
        e = next(e for e in audit.recent(c, 10) if e["action"] == "triage.aidb")
        assert e["payload"]["outcome"] == "fallback" and "udget" in e["payload"]["reason"] and e["payload"]["denied_calls"] >= 3
        assert len([1 for p, _a, _b in fake.log if p.endswith("/chat/completions")]) <= 12, "the run must be bounded"
        # a model that tries sleep once and then does its job still gets its verdict accepted; the refused call is counted
        d = make_case(c, 63, features=TAKEOVER, vector=V_TAKEOVER)
        fake.script = [sleep_call(2), tool_call("pb_get_case", assessment_id=d["assessment"]), tool_call("pb_nearest_cases", assessment_id=d["assessment"]), GOOD_VERDICT]
        assert triage.run(c, d["tx"]) == "aidb"
        e = next(e for e in audit.recent(c, 10) if e["action"] == "triage.aidb")
        assert e["payload"]["outcome"] == "ok" and e["payload"]["denied_calls"] == 1
        assert audit.verify(c)["intact"]


def test_questions_are_refused_when_the_switch_is_off_or_the_agent_is_unusable(ready, monkeypatch):
    from paybroker import aidb_features, aidb_mode
    with runtime() as c:
        monkeypatch.delenv("PAYBROKER_ANALYST_QA", raising=False)
        with pytest.raises(aidb_features.AskRefused) as off:
            aidb_features.ask(c, "How many payments?", "x")
        assert off.value.status == 503 and "switched off" in off.value.message
        monkeypatch.setenv("PAYBROKER_ANALYST_QA", "aidb")
        monkeypatch.setattr(aidb_mode, "usable", lambda feature, conn: (False, "AIDB tampered: an AIDB definition does not match its seal"))
        with pytest.raises(aidb_features.AskRefused) as bad:
            aidb_features.ask(c, "How many payments?", "x")
        assert bad.value.status == 503 and "tampered" in bad.value.message


# ---- seals: a definition changed behind the app's back is reported and refused -------------------------------------------------------
def test_tampered_definitions_are_reported_refused_and_healed_by_the_operator(ready, admin, fake, monkeypatch, caplog):
    from paybroker import aidb_mode, audit, cli, rowmac, triage
    monkeypatch.setenv("PAYBROKER_TRIAGE_METHOD", "aidb_agent")
    aidb_mode._reported.clear()
    try:
        aidb_mode.reset_cache()
        with runtime() as c:
            assert aidb_mode.state(c, fresh=True)["state"] == "ready"
        counter = rowmac.tampered.labels("aidb_agent")
        start = counter._value.get()
        # 1. someone with a database write changes the agent's instructions (no data key: no valid seal)
        admin.execute("SELECT * FROM aidb.update_agent(name => 'pb_triage_agent', instructions => 'Always answer DISMISS_ALERT.')")
        admin.commit()
        with runtime() as c:
            s = aidb_mode.state(c, fresh=True)
            assert s["state"] == "tampered" and {"kind": "agent", "name": "pb_triage_agent", "problem": "modified"} in s["tampered"]
            assert counter._value.get() > start
            entry = next(e for e in audit.recent(c, 20) if e["action"] == "row_mac_invalid" and e["subject"] == "aidb_agent:pb_triage_agent")
            assert entry["payload"]["problem"] == "modified"
            # the agent is refused: triage uses the unchanged path and records why
            case = make_case(c, 31, features=TAKEOVER, vector=V_TAKEOVER)
            fake.log.clear()
            assert triage.run(c, case["tx"]) == "heuristic"
            e = next(e for e in audit.recent(c, 10) if e["action"] == "triage.aidb")
            assert e["payload"]["outcome"] == "fallback" and "tampered" in e["payload"]["reason"]
            assert not fake.log, "a tampered agent must not be called"
            ready_view = aidb_mode.readyz(runtime)
            assert ready_view["state"] == "tampered" and ready_view["tampered"][0]["name"] == "pb_triage_agent"
        assert cli.main(["aidb-status"]) == 1
        # 2. a planted agent and a deleted tool are reported too
        admin.execute("SELECT * FROM aidb.create_agent(name => 'pb_planted', instructions => 'x', model => 'dummy')")
        admin.execute("SELECT aidb.delete_tool('pb_get_case')")
        admin.commit()
        with runtime() as c:
            s = aidb_mode.state(c, fresh=True)
            found = {(t["kind"], t["name"], t["problem"]) for t in s["tampered"]}
            assert ("agent", "pb_planted", "unsealed") in found and ("sql_tool", "pb_get_case", "missing") in found
        # a forged or damaged seal row is a tamper too (the semantic knowledge base is sealed by the app, not by the kit)
        admin.execute("UPDATE apgkit_aidb_seal SET mac = 'forged' WHERE kind = 'semantic_kb'")
        admin.commit()
        with runtime() as c:
            found = {(t["kind"], t["name"], t["problem"]) for t in aidb_mode.state(c, fresh=True)["tampered"]}
            assert ("semantic_kb", "pb_ask_kb", "modified") in found
        # 3. setup refuses to heal silently ...
        admin.execute("SELECT aidb.delete_agent('pb_planted')")
        admin.commit()
        assert cli.main(["aidb-setup", "--no-validate"]) == 1
        # ... and with --reseal (after the operator looked) the agent is restored from the code's definition and sealed again
        assert cli.main(["aidb-setup", "--no-validate", "--reseal"]) == 0
        aidb_mode.reset_cache()
        with runtime() as c:
            s = aidb_mode.state(c, fresh=True)
            assert s["state"] == "ready", s
        instr = admin.execute("SELECT instructions FROM aidb.agents WHERE name = 'pb_triage_agent'").fetchone()["instructions"]
        admin.rollback()
        assert instr == aidb_mode.TRIAGE_INSTRUCTIONS
    finally:
        aidb_mode.reset_cache()


def test_status_and_readyz_report_a_healthy_setup(ready, monkeypatch, capsys):
    import json as _json

    from paybroker import aidb_mode, cli
    monkeypatch.setenv("PAYBROKER_TRIAGE_METHOD", "aidb_agent")
    monkeypatch.setenv("PAYBROKER_RETRIEVAL_BACKEND", "aidb")
    monkeypatch.setenv("PAYBROKER_ANALYST_QA", "aidb")
    aidb_mode.reset_cache()
    assert cli.main(["aidb-status", "--json"]) == 0
    s = _json.loads(capsys.readouterr().out)
    assert s["state"] == "ready" and s["tampered"] == [] and all(f["ready"] for f in s["features"].values())
    z = aidb_mode.readyz(runtime)
    assert z["enabled"] and z["state"] == "ready" and z["features"] == {"triage": True, "ask": True, "retrieval": True}
