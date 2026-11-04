# ruff: noqa: F811
"""AIDB mode on plain Postgres (what CI runs): the switches and their defaults, the derived views and what they leave out, the fallbacks when AIDB is
absent, errs or answers badly, the narrative bookkeeping, the API routes and the operator commands. Nothing here needs the AIDB extension; the tests
against a real AIDB are in test_aidb.py (marker `aidb`)."""
import json
import logging

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from tests.conftest import needs_db

pytestmark = needs_db

from paybroker import (  # noqa: E402
    aidb_features,
    aidb_mode,
    aidb_objects,
    audit,
    cases,
    cli,
    demo,
    maintenance,
    triage,
    worker,
)
from paybroker.db import connect  # noqa: E402
from tests.test_db import CSRF, op  # noqa: E402,F401  (op is a fixture)

SWITCHES = ("PAYBROKER_RETRIEVAL_BACKEND", "PAYBROKER_TRIAGE_METHOD", "PAYBROKER_ANALYST_QA")


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for v in SWITCHES:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("PAYBROKER_AIDB_ADMIN_DSN", raising=False)
    aidb_mode.reset_cache()
    aidb_features.reset_retriever()
    aidb_mode._reported.clear()
    yield
    aidb_mode.reset_cache()
    aidb_features.reset_retriever()


def flagged(client, n=150, seed=3) -> list[dict]:
    with connect() as conn:
        demo.run(conn, n, seed=seed)
        rows = conn.execute("SELECT fc.id AS case_id, fa.id AS assessment_id, t.id AS tx, t.payment_id FROM fraud_case fc "
                            "JOIN transaction t ON t.id = fc.transaction_id JOIN fraud_assessment fa ON fa.transaction_id = t.id ORDER BY fc.id").fetchall()
        conn.rollback()
    assert rows, "the demo traffic must flag something"
    return rows


# ---- the switches and the defaults --------------------------------------------------------------------------------------------------
def test_every_switch_defaults_to_todays_behaviour():
    assert aidb_mode.requested() == {"retrieval_backend": "app", "triage_method": "app", "analyst_qa": "off"}
    assert not aidb_mode.any_requested() and not aidb_mode.narratives_on()


def test_switch_values_are_parsed_and_an_unknown_one_means_the_default(monkeypatch, caplog):
    monkeypatch.setenv("PAYBROKER_RETRIEVAL_BACKEND", " AIDB ")
    monkeypatch.setenv("PAYBROKER_TRIAGE_METHOD", "aidb_agent")
    monkeypatch.setenv("PAYBROKER_ANALYST_QA", "aidb")
    assert aidb_mode.requested() == {"retrieval_backend": "aidb", "triage_method": "aidb_agent", "analyst_qa": "aidb"} and aidb_mode.narratives_on()
    monkeypatch.setenv("PAYBROKER_TRIAGE_METHOD", "agent")
    monkeypatch.setenv("PAYBROKER_ANALYST_QA", "yes")
    monkeypatch.setenv("PAYBROKER_RETRIEVAL_BACKEND", "vector")
    with caplog.at_level(logging.ERROR):
        assert aidb_mode.requested() == {"retrieval_backend": "app", "triage_method": "app", "analyst_qa": "off"}
    assert sum("is not one of" in r.message for r in caplog.records) == 3


def test_with_every_switch_off_nothing_of_aidb_mode_runs(client, op, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("AIDB mode code ran with every switch off")
    monkeypatch.setattr(aidb_mode, "usable", forbidden)
    monkeypatch.setattr(aidb_mode, "state", forbidden)
    monkeypatch.setattr(aidb_features, "call_agent", forbidden)
    with connect() as conn:
        demo.run(conn, 150, seed=3)
        while worker.run_once(conn):
            pass
        rows = conn.execute("SELECT tier3_llm_analysis->>'source' AS s FROM fraud_assessment WHERE tier2_ml_score >= 700").fetchall()
        assert rows and {r["s"] for r in rows} == {"heuristic"}
        assert conn.execute("SELECT count(*) AS n FROM pb_ai.case_narrative").fetchone()["n"] == 0, "the default path writes no narrative"
        assert not [e for e in audit.recent(conn, 500) if e["action"].startswith(("triage.aidb", "analyst.ask"))]
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["aidb"] == {"enabled": False}


# ---- the derived views: what the agents can read ---------------------------------------------------------------------------------------
def test_the_views_have_exactly_the_documented_columns(client):
    flagged(client)
    with connect() as conn:
        for view, columns in aidb_objects.VIEWS.items():
            schema, name = view.split(".")
            got = [r["column_name"] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                                                          "ORDER BY ordinal_position", (schema, name)).fetchall()]
            assert tuple(got) == columns, view
        conn.rollback()


def test_no_identifier_reaches_a_view_or_a_narrative(client, op):
    flagged(client)
    with connect() as conn:
        secret = set()
        for r in conn.execute("SELECT customer_id, card_token, idempotency_key, request->>'ip' AS ip, request->>'device' AS dev FROM transaction").fetchall():
            secret |= {v for v in r.values() if v}
        secret |= {r["fingerprint"] for r in conn.execute("SELECT fingerprint FROM card").fetchall() if r["fingerprint"]}
        secret |= {r["key_hash"] for r in conn.execute("SELECT key_hash FROM merchant_key").fetchall()}
        secret |= {r["webhook_url"] for r in conn.execute("SELECT webhook_url FROM merchant WHERE webhook_url IS NOT NULL").fetchall()}
        assert len(secret) > 20, "the scenario must hold identifiers to look for"
        total = 0
        for view in aidb_objects.VIEWS:
            for row in conn.execute(f"SELECT v::text AS t FROM {view} v").fetchall():           # noqa: S608  (constant names)
                total += 1
                for s in secret:
                    assert s not in row["t"], f"{s!r} found in {view}"
        assert total > 100
        assert aidb_features.backfill_narratives(conn) > 0
        for row in conn.execute("SELECT narrative FROM pb_ai.case_narrative").fetchall():
            for s in secret:
                assert s not in row["narrative"]
        conn.rollback()


def test_the_feature_allow_list_keeps_an_unlisted_feature_out(client):
    c = flagged(client)[0]
    with connect() as conn:
        conn.execute("UPDATE fraud_assessment SET tier2_feature_snapshot = tier2_feature_snapshot || '{\"customer_email\": \"a@example.com\", \"tx_count_1h\": 4}' "
                     "WHERE id = %s", (c["assessment_id"],))
        f = conn.execute("SELECT features FROM pb_ai.case_features WHERE assessment_id = %s", (c["assessment_id"],)).fetchone()["features"]
        conn.rollback()
    assert "customer_email" not in f and f["tx_count_1h"] == 4 and "last_ip_asn" not in f


def test_the_objects_hook_is_idempotent_and_a_role_that_may_not_create_schemas_does_not_stop_the_app(client):
    with connect() as conn:
        assert aidb_objects.apply(conn) is True and aidb_objects.apply(conn) is True and aidb_objects.present(conn)
        conn.commit()
        conn.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='pb_noschema') THEN "
                     "CREATE ROLE pb_noschema LOGIN PASSWORD 'pb-noschema-throwaway' NOSUPERUSER; END IF; END $$")     # pragma: allowlist secret
        conn.commit()
    import os
    dsn = make_conninfo(os.environ["TEST_DATABASE_URL"], user="pb_noschema", password="pb-noschema-throwaway")        # pragma: allowlist secret
    try:
        with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as weak:
            assert aidb_objects.apply(weak) is False            # logged, not raised
    finally:
        with connect() as conn:
            conn.execute("DROP ROLE IF EXISTS pb_noschema")
            conn.commit()


# ---- Tier 3 by the agent: fallbacks and the verdict's limits -------------------------------------------------------------------------------
def _run(conn, case):
    conn.execute("INSERT INTO outbox (topic, payload) VALUES ('fraud.triage', %s)", (psycopg.types.json.Jsonb({"transaction_id": str(case["tx"])}),))
    conn.commit()
    return triage.run(conn, str(case["tx"]))


def _ledger(conn, action="triage.aidb"):
    return [e for e in audit.recent(conn, 50) if e["action"] == action]


def test_agent_triage_with_aidb_absent_falls_back_to_the_unchanged_path_and_says_so(client, monkeypatch):
    monkeypatch.setenv("PAYBROKER_TRIAGE_METHOD", "aidb_agent")
    cs = flagged(client)
    with connect() as conn:
        assert _run(conn, cs[0]) == "heuristic"
        a = conn.execute("SELECT tier3_state, tier3_llm_analysis AS a FROM fraud_assessment WHERE id=%s", (cs[0]["assessment_id"],)).fetchone()
        assert a["tier3_state"] == "done" and a["a"]["source"] == "heuristic"
        e = _ledger(conn)[0]
        assert e["subject"] == cs[0]["payment_id"] and e["payload"]["outcome"] == "fallback" and "AIDB" in e["payload"]["reason"]
        assert e["payload"]["agent"] == "pb_triage_agent" and e["payload"]["case_id"] == cs[0]["case_id"]
        assert audit.verify(conn)["intact"]
        # and the narrative is bookkept because an AIDB feature is switched on
        assert conn.execute("SELECT count(*) AS n FROM pb_ai.case_narrative WHERE case_id=%s", (cs[0]["case_id"],)).fetchone()["n"] == 1
    z = client.get("/readyz")
    assert z.status_code == 200 and z.json()["aidb"]["state"] == "unavailable" and z.json()["aidb"]["enabled"] is True


@pytest.fixture
def agent(monkeypatch):
    """A usable agent whose answers the test scripts (no database, no model)."""
    monkeypatch.setenv("PAYBROKER_TRIAGE_METHOD", "aidb_agent")
    monkeypatch.setattr(aidb_mode, "usable", lambda feature, conn: (True, ""))
    script: list = []
    calls: list = []

    def call(conn, name, prompt, conversation_id=None):
        calls.append((name, prompt))
        step = script.pop(0)
        return step if isinstance(step, aidb_features.AgentCall) else aidb_features.AgentCall(step, "11111111-2222-3333-4444-555555555555", "", 12)
    monkeypatch.setattr(aidb_features, "call_agent", call)
    return script, calls


VERDICT = {"risk_category": "CARD_TESTING", "confidence_score": 0.9, "summary": "Many tiny payments.", "key_risk_factors": ["velocity"],
           "recommended_action": "PERMANENT_BLOCK", "suggested_rule_updates": ""}


def test_the_agents_verdict_is_stored_like_the_llms_and_changes_nothing_else(client, agent):
    script, calls = agent
    cs = flagged(client)
    script.append(json.dumps(VERDICT))
    with connect() as conn:
        blocked = conn.execute("SELECT count(*) AS n FROM blacklist").fetchone()["n"]
        assert _run(conn, cs[0]) == "aidb"
        row = conn.execute("SELECT tier3_state, tier3_llm_analysis AS a FROM fraud_assessment WHERE id=%s", (cs[0]["assessment_id"],)).fetchone()
        assert row["tier3_state"] == "done" and row["a"]["source"] == "aidb_agent" and row["a"]["risk_category"] == "CARD_TESTING"
        assert row["a"]["recommended_action"] == "PERMANENT_BLOCK" and row["a"]["agent"] == "pb_triage_agent"
        # advice only: PERMANENT_BLOCK blocked nothing and resolved nothing; that is an analyst's click
        assert conn.execute("SELECT count(*) AS n FROM blacklist").fetchone()["n"] == blocked
        assert conn.execute("SELECT status FROM fraud_case WHERE id=%s", (cs[0]["case_id"],)).fetchone()["status"] == "open"
        e = _ledger(conn)[0]
        assert e["payload"]["outcome"] == "ok" and e["payload"]["recommended_action"] == "PERMANENT_BLOCK" and e["payload"]["ms"] == 12
        assert audit.verify(conn)["intact"]
    assert calls == [("pb_triage_agent", aidb_features.triage_prompt(cs[0]["assessment_id"]))]
    assert str(cs[0]["assessment_id"]) in calls[0][1] and cs[0]["payment_id"] not in calls[0][1], "the prompt names the assessment, nothing identifying"


@pytest.mark.parametrize("answer, why", [
    ("no json at all", "unusable verdict"),
    (json.dumps({**VERDICT, "risk_category": "WIZARD"}), "unusable verdict"),
    (json.dumps({**VERDICT, "recommended_action": "WIRE_MONEY"}), "unusable verdict"),
    (json.dumps({**VERDICT, "confidence_score": 7}), "unusable verdict"),
    (None, "boom"),
])
def test_a_bad_answer_or_a_failing_agent_falls_back_and_is_recorded(client, agent, answer, why):
    script, _ = agent
    cs = flagged(client)
    script.append(aidb_features.AgentCall(None, None, "boom", 5) if answer is None else answer)
    with connect() as conn:
        assert _run(conn, cs[0]) == "heuristic"
        a = conn.execute("SELECT tier3_llm_analysis AS a FROM fraud_assessment WHERE id=%s", (cs[0]["assessment_id"],)).fetchone()["a"]
        assert a["source"] == "heuristic"
        e = _ledger(conn)[0]
        assert e["payload"]["outcome"] == "fallback" and why in e["payload"]["reason"]
        assert audit.verify(conn)["intact"]


def test_the_worker_runs_the_agent_path_and_finishes_the_job(client, agent):
    script, _ = agent
    flagged(client)
    with connect() as conn:
        n = conn.execute("SELECT count(*) AS n FROM outbox WHERE topic='fraud.triage' AND done_at IS NULL").fetchone()["n"]
        script.extend([json.dumps(VERDICT)] * n)
        while worker.run_once(conn):
            pass
        assert conn.execute("SELECT count(*) AS n FROM outbox WHERE topic='fraud.triage' AND done_at IS NULL").fetchone()["n"] == 0
        done = conn.execute("SELECT tier3_llm_analysis->>'source' AS s FROM fraud_assessment WHERE tier2_ml_score >= 700").fetchall()
        assert {r["s"] for r in done} == {"aidb_agent"} and not script


# ---- narratives and the retrieval switch ---------------------------------------------------------------------------------------------------
def test_narratives_follow_the_case_and_go_when_the_data_is_reset(client, op, monkeypatch):
    monkeypatch.setenv("PAYBROKER_RETRIEVAL_BACKEND", "aidb")          # switched on (AIDB itself is absent: only the bookkeeping is tested)
    cs = flagged(client)
    with connect() as conn:
        assert aidb_features.backfill_narratives(conn) == len(cs)
        conn.commit()
        before = conn.execute("SELECT narrative FROM pb_ai.case_narrative WHERE case_id=%s", (cs[0]["case_id"],)).fetchone()["narrative"]
        assert "Analyst decision" not in before
    r = client.post(f"/api/cases/{cs[0]['case_id']}/resolve", json={"decision": "PERMANENT_BLOCK", "note": "x"}, headers=op)
    assert r.status_code == 200, r.text
    with connect() as conn:
        after = conn.execute("SELECT narrative FROM pb_ai.case_narrative WHERE case_id=%s", (cs[0]["case_id"],)).fetchone()["narrative"]
        assert after.endswith("Analyst decision: PERMANENT_BLOCK.") and after.startswith(before.rstrip("."))
        assert aidb_features.backfill_narratives(conn) == len(cs)       # a repeat changes nothing
        conn.commit()
        assert conn.execute("SELECT count(*) AS n FROM pb_ai.case_narrative WHERE updated_at > now() - interval '1 second' AND case_id <> %s",
                            (cs[0]["case_id"],)).fetchone()["n"] <= len(cs)
    out = maintenance.reset(actor="test", include_training_data=True, include_rule_suggestions=True)
    assert out["deleted"]["fraud_case"] == len(cs)
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM pb_ai.case_narrative").fetchone()["n"] == 0, "deleting the cases removes their narratives"


def test_a_narrative_failure_never_fails_the_resolution(client, op, monkeypatch):
    monkeypatch.setenv("PAYBROKER_RETRIEVAL_BACKEND", "aidb")
    cs = flagged(client)
    with connect() as conn:
        conn.execute("ALTER TABLE pb_ai.case_narrative RENAME TO case_narrative_gone")
        conn.commit()
    try:
        r = client.post(f"/api/cases/{cs[0]['case_id']}/resolve", json={"decision": "DISMISS_ALERT"}, headers=op)
        assert r.status_code == 200, r.text
    finally:
        with connect() as conn:
            conn.execute("ALTER TABLE pb_ai.case_narrative_gone RENAME TO case_narrative")
            conn.commit()


def test_the_similar_cases_route_answers_with_both_kinds_of_neighbour(client, op, monkeypatch, caplog):
    cs = flagged(client)
    case = cs[len(cs) // 2]["case_id"]
    assert client.get(f"/api/cases/{case}/similar").status_code == 401
    assert client.get("/api/cases/99999999/similar", headers=op).status_code == 404
    r = client.get(f"/api/cases/{case}/similar?k=3", headers=op)
    assert r.status_code == 200
    body = r.json()
    assert body["numeric"]["items"] and body["narrative"]["backend"] == "app" and body["narrative"]["items"] == []
    assert "no narrative yet" in body["narrative"]["note"]
    with connect() as conn:
        aidb_features.backfill_narratives(conn)
        conn.commit()
    body = client.get(f"/api/cases/{case}/similar?k=3", headers=op).json()
    assert body["narrative"]["backend"] == "app" and body["narrative"]["items"] and str(case) not in [str(i["case_id"]) for i in body["narrative"]["items"]]
    assert all(set(i) == {"case_id", "score", "distance", "analyst_decision", "narrative"} for i in body["narrative"]["items"])
    free = client.get(f"/api/cases/{case}/similar?q=new+device+impossible+travel", headers=op).json()
    assert free["narrative"]["query"] == "new device impossible travel"
    # asking for aidb where it cannot work: the app backend answers, the log says why
    monkeypatch.setenv("PAYBROKER_RETRIEVAL_BACKEND", "aidb")
    aidb_features.reset_retriever()
    with caplog.at_level(logging.ERROR):
        body = client.get(f"/api/cases/{case}/similar?k=3", headers=op).json()
    assert body["narrative"]["backend"] == "app" and body["narrative"]["items"]
    assert any("RETRIEVAL BACKEND FALLBACK" in r.message for r in caplog.records)


# ---- analyst questions ------------------------------------------------------------------------------------------------------------------------
def test_questions_are_refused_politely_unless_everything_is_in_place(client, op, monkeypatch):
    q = {"question": "How many payments were declined yesterday?"}
    assert client.post("/api/ask", json=q, headers=CSRF).status_code == 401
    r = client.post("/api/ask", json=q, headers=op)
    assert r.status_code == 503 and "switched off" in r.json()["detail"]
    monkeypatch.setenv("PAYBROKER_ANALYST_QA", "aidb")
    r = client.post("/api/ask", json=q, headers=op)
    assert r.status_code == 503 and "not available" in r.json()["detail"] and "AIDB" in r.json()["detail"]
    assert client.post("/api/ask", json={"question": "hi"}, headers=op).status_code == 422
    assert client.post("/api/ask", json={**q, "extra": 1}, headers=op).status_code == 422
    assert client.post("/api/ask", json={**q, "conversation_id": "not a uuid!"}, headers=op).status_code == 422


def _ran(sql="SELECT count(*) AS n FROM pb_ask.payments WHERE status = 'DECLINED'", rows=({"n": 7},), ok=True, error=""):
    return [{"tool": "semantic_kb_search", "sql": None, "ok": True, "rows": None, "error": ""},
            {"tool": "run_sql_query", "sql": sql, "ok": ok, "rows": list(rows) if ok else None, "error": error}]


def test_a_question_goes_to_the_agent_and_into_the_ledger(client, op, monkeypatch):
    monkeypatch.setenv("PAYBROKER_ANALYST_QA", "aidb")
    monkeypatch.setattr(aidb_mode, "usable", lambda feature, conn: (True, ""))
    seen = []
    monkeypatch.setattr(aidb_features, "call_agent", lambda conn, name, prompt, cid=None: seen.append((name, prompt, cid)) or aidb_features.AgentCall(
        "There were 7 declines.", "aaaaaaaa-0000-0000-0000-000000000001", "", 9))
    monkeypatch.setattr(aidb_features, "executed", lambda conn, cid, since: _ran())
    r = client.post("/api/ask", json={"question": "How many payments were declined?", "conversation_id": "aaaaaaaa-0000-0000-0000-000000000000"}, headers=op)
    body = r.json()
    assert r.status_code == 200 and body["answer"] == "There were 7 declines." and body["agent"] == "pb_ask_agent"
    assert body["executed"] is True and body["retried"] is False and body["rows"] == [{"n": 7}] and body["sql_error"] == ""
    assert "DECLINED" in body["sql"]                       # the SQL is the one in the action log, not text from the model
    assert seen == [("pb_ask_agent", "How many payments were declined?", "aaaaaaaa-0000-0000-0000-000000000000")]
    with connect() as conn:
        e = _ledger(conn, "analyst.ask")[0]
        assert e["payload"]["outcome"] == "ok" and "declined" not in json.dumps(e["payload"]) and e["payload"]["rows"] == 1
    monkeypatch.setattr(aidb_features, "call_agent", lambda *a, **k: aidb_features.AgentCall(None, None, "the model timed out", 3))
    r = client.post("/api/ask", json={"question": "How many payments were declined?"}, headers=op)
    assert r.status_code == 502 and "timed out" in r.json()["detail"]
    with connect() as conn:
        assert _ledger(conn, "analyst.ask")[0]["payload"]["outcome"] == "error"


def test_model_text_without_a_query_is_not_returned_as_an_answer(client, op, monkeypatch):
    """The model wrote SQL and presented it as the answer without running it (seen with a real model). One bounded retry tells it to run the query;
    when nothing ran even then the response says so and carries the text only as `unverified_text`."""
    monkeypatch.setenv("PAYBROKER_ANALYST_QA", "aidb")
    monkeypatch.setattr(aidb_mode, "usable", lambda feature, conn: (True, ""))
    calls = []
    text = "SELECT count(*) FROM pb_ask.payments WHERE status = 'DECLINED';"
    monkeypatch.setattr(aidb_features, "call_agent", lambda conn, name, prompt, cid=None: calls.append((prompt, cid)) or aidb_features.AgentCall(
        text, "aaaaaaaa-0000-0000-0000-000000000002", "", 5))
    monkeypatch.setattr(aidb_features, "executed", lambda conn, cid, since: [{"tool": "semantic_kb_search", "sql": None, "ok": True, "rows": None, "error": ""}])
    r = client.post("/api/ask", json={"question": "How many payments were declined?"}, headers=op)
    body = r.json()
    assert r.status_code == 200 and body["executed"] is False and body["retried"] is True
    assert body["answer"] is None and body["sql"] is None and body["rows"] == [] and body["unverified_text"] == text
    assert "did not run a query" in body["sql_error"]
    assert len(calls) == 2 and "run_sql_query" in calls[1][0] and calls[1][1] == "aaaaaaaa-0000-0000-0000-000000000002"   # one retry, same conversation
    with connect() as conn:
        e = _ledger(conn, "analyst.ask")[0]
        assert e["payload"]["outcome"] == "no_query" and e["payload"]["retried"] is True
    # the retry works: the second call runs the query, the response is the normal one and says it was retried
    state = {"n": 0}

    def agent(conn, name, prompt, cid=None):
        state["n"] += 1
        return aidb_features.AgentCall("There were 7 declines." if state["n"] == 2 else text, "aaaaaaaa-0000-0000-0000-000000000003", "", 5)
    monkeypatch.setattr(aidb_features, "call_agent", agent)
    monkeypatch.setattr(aidb_features, "executed", lambda conn, cid, since: _ran() if state["n"] >= 2 else [])
    body = client.post("/api/ask", json={"question": "How many payments were declined?"}, headers=op).json()
    assert body["executed"] is True and body["retried"] is True and body["answer"] == "There were 7 declines." and body["rows"] == [{"n": 7}]
    # a query that ran and failed is reported as such, with the error and no rows
    monkeypatch.setattr(aidb_features, "call_agent", lambda conn, name, prompt, cid=None: aidb_features.AgentCall("I could not read it.", "aaaaaaaa-0000-0000-0000-000000000004", "", 5))
    monkeypatch.setattr(aidb_features, "executed", lambda conn, cid, since: _ran(ok=False, error="Tool error: permission denied for table transaction"))
    body = client.post("/api/ask", json={"question": "List card tokens."}, headers=op).json()
    assert body["executed"] is True and body["retried"] is False and "permission denied" in body["sql_error"] and body["rows"] == []
    with connect() as conn:
        assert _ledger(conn, "analyst.ask")[0]["payload"]["outcome"] == "sql_error" and _ledger(conn, "analyst.ask")[0]["payload"]["denied_calls"] == 1


def test_the_state_route_is_for_administrators(client, op):
    assert client.get("/api/admin/aidb").status_code == 401
    r = client.get("/api/admin/aidb", headers=op)
    assert r.status_code == 200 and r.json()["state"] == "unavailable" and r.json()["requested"]["triage_method"] == "app"
    assert "governance" not in r.json()               # nothing of AIDB's decision trail exists on plain Postgres
    assert client.get("/api/admin/aidb?window=1%3B+drop", headers=op).status_code == 422


# ---- the operator commands ---------------------------------------------------------------------------------------------------------------------------
def test_status_command_says_what_is_missing(client, capsys, monkeypatch):
    assert cli.main(["aidb-status"]) == 1
    out = capsys.readouterr().out
    assert "requested: retrieval_backend=app, triage_method=app, analyst_qa=off" in out and "not available" in out
    assert cli.main(["aidb-status", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "unavailable"


def test_setup_refuses_without_aidb_and_never_prints_the_connection_string(client, capsys, monkeypatch):
    import os
    dsn = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("PAYBROKER_AIDB_ADMIN_DSN", dsn)
    assert cli.main(["aidb-setup", "--model", "dummy"]) == 1
    captured = capsys.readouterr()
    assert "AIDB is not available here" in captured.err
    assert dsn not in captured.out + captured.err and "password" not in (captured.out + captured.err).lower()
    monkeypatch.setenv("PAYBROKER_AIDB_ADMIN_DSN", "postgresql://nobody:pw@127.0.0.1:1/none")      # pragma: allowlist secret
    assert cli.main(["aidb-setup", "--model", "dummy"]) == 1
    err = capsys.readouterr().err
    assert "could not connect" in err and "pw@" not in err


# ---- the operator commands open the vault under sealed boot -----------------------------------------------------------------------------
SEALED_PW = "test-vault-password-0123"      # pragma: allowlist secret      (what the conftest vault was created with: under sealed boot, the unseal passphrase)


@pytest.fixture
def sealed_cli(client, monkeypatch):
    """A sealed-boot install seen from the operator's CLI: the vault is locked, wrapped under the passphrase, there is no platform secret and an
    admin password may exist (which does not open a sealed-boot vault)."""
    from paybroker import vault
    for v in ("PAYBROKER_ENCRYPTION_KEY", "PAYBROKER_ENCRYPTION_KEY_FILE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("APGKIT_SEALED", "not-a-real-blob")       # only whether sealed boot is on matters to the CLI
    vault.lock()
    asked = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": asked.append(prompt) or SEALED_PW)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    yield asked
    vault.lock()


def test_cli_opens_a_sealed_boot_vault_with_the_hidden_passphrase_prompt(sealed_cli, capsys):
    from paybroker import vault
    with connect() as conn:
        assert cli._unlock_vault(conn) is True
    assert vault.is_unlocked()
    assert len(sealed_cli) == 1 and "passphrase" in sealed_cli[0].lower() and "Admin password" not in sealed_cli[0]
    out = capsys.readouterr()
    assert SEALED_PW not in out.out + out.err


def test_cli_refuses_a_wrong_sealed_boot_passphrase_without_echoing_it(sealed_cli, capsys, monkeypatch):
    from paybroker import vault
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "not-the-passphrase-xyz")
    with connect() as conn:
        assert cli._unlock_vault(conn) is False
    assert not vault.is_unlocked()
    out = capsys.readouterr()
    assert "does not open the vault" in out.err and "not-the-passphrase-xyz" not in out.out + out.err


def test_cli_does_not_prompt_without_a_terminal_under_sealed_boot(sealed_cli, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    with connect() as conn:
        assert cli._unlock_vault(conn) is False
    assert sealed_cli == [] and "terminal" in capsys.readouterr().err


def test_cli_platform_secret_path_is_unchanged_under_sealed_boot(sealed_cli, monkeypatch):
    from paybroker import vault
    monkeypatch.setattr(vault, "try_unlock_with_platform_secret", lambda conn: True)
    with connect() as conn:
        assert cli._unlock_vault(conn) is True
    assert sealed_cli == []                                     # never asked


def test_setup_under_sealed_boot_gets_past_the_vault_and_stops_at_aidb(sealed_cli, capsys, monkeypatch):
    import os
    monkeypatch.setenv("PAYBROKER_AIDB_ADMIN_DSN", os.environ["TEST_DATABASE_URL"])
    assert cli.main(["aidb-setup", "--model", "dummy"]) == 1
    err = capsys.readouterr().err
    assert "AIDB is not available here" in err and "vault is locked" not in err


def test_case_resolution_still_works_with_every_switch_off(client, op):
    cs = flagged(client)
    assert client.post(f"/api/cases/{cs[0]['case_id']}/resolve", json={"decision": "DISMISS_ALERT"}, headers=op).status_code == 200
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM pb_ai.case_narrative").fetchone()["n"] == 0
        assert cases.DECISIONS
