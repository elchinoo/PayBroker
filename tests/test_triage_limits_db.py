"""Tier 3 throttle against a real Postgres: pacing, the daily cap, grouping, gateway 429s and the breaker, the admin API, upgrade safety."""
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests.conftest import needs_db

pytestmark = needs_db

from psycopg.types.json import Jsonb  # noqa: E402

from paybroker import triage, triage_limits, worker  # noqa: E402
from paybroker.db import connect  # noqa: E402
from paybroker.llm_http import LLMError  # noqa: E402
from tests.test_db import KEY, bearer  # noqa: E402

GOOD = json.dumps({"risk_category": "CARD_TESTING", "confidence_score": 0.9, "summary": "x", "key_risk_factors": [],
                   "recommended_action": "PERMANENT_BLOCK"})


class Gateway:
    """Stands in for the LLM: counts calls, answers from a script, and checks that the client's own retries are off."""

    def __init__(self):
        self.calls, self.script = 0, []

    def __call__(self, cfg, messages, max_tokens=700, temperature=0.1, retries=3):
        assert retries == 0, "the worker owns retries; the client must send one request per attempt"
        self.calls += 1
        step = self.script.pop(0) if self.script else GOOD
        if isinstance(step, Exception):
            raise step
        return {"text": step, "usage": {"total_tokens": 1260}}


@pytest.fixture
def op(monkeypatch):
    monkeypatch.setenv("PAYBROKER_API_KEY", KEY)
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def gw(client, monkeypatch):
    g = Gateway()
    monkeypatch.setattr(triage, "chat", g)
    monkeypatch.setattr(triage.accounts, "triage_account", lambda conn: 1)
    monkeypatch.setattr(triage.accounts, "chat_config_for", lambda conn, account: SimpleNamespace(model="m", label="gw"))
    monkeypatch.setattr(triage_limits, "BUCKET", triage_limits.Bucket())
    for var in triage_limits.ENV.values():
        monkeypatch.delenv(var, raising=False)
    return g


def put(conn, **values):
    for f, v in values.items():
        triage_limits.store.put(conn, triage_limits.KEYS[f], str(v))
    conn.commit()


def case(conn, n: int, *, device="dev_1", ip="198.51.100.20", age_s: float = 0) -> str:
    """A flagged payment with its assessment and a queued triage job. Returns the transaction id."""
    conn.execute("INSERT INTO merchant (id, name) VALUES ('mch_tier3', 'T') ON CONFLICT DO NOTHING")
    req = {"customer_id": "c", "ip": ip, "device": device, "telemetry": {}}
    tx = conn.execute("INSERT INTO transaction (payment_id, merchant_id, idempotency_key, request_hash, amount, currency, status, request, fraud_score) "
                      "VALUES (%s,'mch_tier3',%s,'h',500,'USD','DECLINED',%s,800) RETURNING id",
                      (f"pay_{n}", f"k{n}", Jsonb(req))).fetchone()["id"]
    conn.execute("INSERT INTO fraud_assessment (transaction_id, tier2_ml_score, tier2_feature_snapshot, reasons, decision, tier3_state) "
                 "VALUES (%s, 800, '{}', %s, 'DECLINE', 'queued')", (tx, ["HIGH_VELOCITY_IP"]))
    conn.execute("INSERT INTO outbox (topic, payload, created_at) VALUES ('fraud.triage', %s, now() - %s * interval '1 second')",
                 (Jsonb({"transaction_id": str(tx)}), age_s))
    conn.commit()
    return str(tx)


def analysis(conn, tx):
    return conn.execute("SELECT tier3_state, tier3_llm_analysis AS a FROM fraud_assessment WHERE transaction_id=%s", (tx,)).fetchone()


def job(conn, tx):
    return conn.execute("SELECT attempts, done_at, last_error, available_at > now() AS later, extract(epoch FROM available_at - now()) AS wait "
                        "FROM outbox WHERE payload->>'transaction_id'=%s", (tx,)).fetchone()


def logged(conn, tx, outcome, minutes_ago=0):
    conn.execute("INSERT INTO triage_llm_call (transaction_id, outcome, at) VALUES (%s,%s, now() - %s * interval '1 minute')", (tx, outcome, minutes_ago))
    conn.commit()


# ---- rate limit ----------------------------------------------------------------------------------------------------
def test_the_bucket_defers_surplus_jobs_without_using_an_attempt(gw):
    with connect() as conn:
        put(conn, rpm=1, group_minutes=0)
        txs = [case(conn, n, device=f"d{n}", ip=f"198.51.100.{n}") for n in range(3)]
        assert worker.run_once(conn) == 3
        assert gw.calls == 1                                                    # one slot, one call
        states = [(analysis(conn, t)["tier3_state"], job(conn, t)) for t in txs]
        done = [t for t, j in states if j["done_at"]]
        waiting = [j for t, j in states if not j["done_at"]]
        assert len(done) == 1 and done[0] == "done" and len(waiting) == 2
        for j in waiting:
            assert j["attempts"] == 0 and j["later"] and 5 <= j["wait"] <= 100 and "rate limit" in j["last_error"]   # free attempt, short delay, a reason
        assert triage_limits.calls_24h(conn) == 1


def test_unlimited_rate_and_cap_send_every_case_to_the_model(gw):
    with connect() as conn:
        put(conn, rpm=0, daily_cap=0, group_minutes=0)
        for n in range(8):
            case(conn, n, device=f"d{n}")
        worker.run_once(conn)
        assert gw.calls == 8 and conn.execute("SELECT count(*) AS n FROM outbox WHERE done_at IS NULL").fetchone()["n"] == 0


# ---- daily cap -----------------------------------------------------------------------------------------------------
def test_the_daily_cap_gives_the_heuristic_with_a_note_and_counts_only_the_last_24_hours(gw):
    with connect() as conn:
        put(conn, rpm=0, daily_cap=2, group_minutes=0)
        logged(conn, str(uuid.uuid4()), "ok", minutes_ago=25 * 60)              # yesterday's call does not count
        logged(conn, str(uuid.uuid4()), "error")                                # an unusable answer does
        t1 = case(conn, 1, device="d1")
        worker.run_once(conn)                                                   # 1 used in the last 24 h, cap 2: this one goes to the model
        calls_before = gw.calls
        assert calls_before == 1 and triage_limits.calls_24h(conn) == 2
        t2 = case(conn, 2, device="d2")
        worker.run_once(conn)
        a = analysis(conn, t2)
        assert gw.calls == calls_before and a["tier3_state"] == "done" and job(conn, t2)["done_at"]
        assert a["a"]["source"] == "heuristic" and "daily cap reached" in a["a"]["note"]
        assert analysis(conn, t1)["a"]["source"] == "llm"


# ---- grouping ------------------------------------------------------------------------------------------------------
def test_a_case_from_the_same_device_is_grouped_with_the_one_the_model_read(gw):
    with connect() as conn:
        put(conn, rpm=0, daily_cap=0, group_minutes=30)
        a = case(conn, 1, device="dev_x", ip="203.0.113.1")
        worker.run_once(conn)
        b = case(conn, 2, device="dev_x", ip="203.0.113.2")                       # same device, other IP
        c = case(conn, 3, device="dev_y", ip="203.0.113.1")                       # same IP, other device: the device wins, so not grouped
        worker.run_once(conn)
        assert gw.calls == 2 and analysis(conn, a)["a"]["source"] == "llm" and analysis(conn, c)["a"]["source"] == "llm"
        g = analysis(conn, b)["a"]
        assert g["source"] == "heuristic" and "pay_1" in g["note"] and "device" in g["note"] and job(conn, b)["done_at"]


def test_without_a_device_the_ip_groups_and_the_window_and_the_switch_are_honoured(gw):
    with connect() as conn:
        put(conn, rpm=0, daily_cap=0, group_minutes=30)
        a = case(conn, 1, device=None, ip="203.0.113.9")
        worker.run_once(conn)
        b = case(conn, 2, device=None, ip="203.0.113.9")
        worker.run_once(conn)
        assert analysis(conn, b)["a"]["source"] == "heuristic" and "pay_1" in analysis(conn, b)["a"]["note"] and "IP" in analysis(conn, b)["a"]["note"]
        conn.execute("UPDATE triage_llm_call SET at = now() - interval '31 minutes'")        # outside the window
        c = case(conn, 3, device=None, ip="203.0.113.9")
        worker.run_once(conn)
        assert analysis(conn, c)["a"]["source"] == "llm"
        put(conn, group_minutes=0)                                                           # switched off
        d = case(conn, 4, device=None, ip="203.0.113.9")
        worker.run_once(conn)
        assert analysis(conn, d)["a"]["source"] == "llm" and gw.calls == 3 and a


def test_a_heuristic_case_is_not_itself_a_group_anchor(gw):
    with connect() as conn:
        put(conn, rpm=0, daily_cap=1, group_minutes=30)
        case(conn, 1, device="d1")
        worker.run_once(conn)
        t2 = case(conn, 2, device="d2")
        worker.run_once(conn)                                                                # capped -> heuristic, no call logged
        assert analysis(conn, t2)["a"]["source"] == "heuristic" and triage_limits.calls_24h(conn) == 1
        assert conn.execute("SELECT count(*) AS n FROM triage_llm_call").fetchone()["n"] == 1


# ---- gateway 429 and the breaker -----------------------------------------------------------------------------------
def rate_limited(extra=""):
    return LLMError(f"chat endpoint returned HTTP 429: rate limit exceeded {extra}")


def test_a_429_neither_uses_an_attempt_nor_fails_and_honours_retry_after(gw):
    with connect() as conn:
        put(conn, rpm=0, daily_cap=0, group_minutes=0)
        tx = case(conn, 1)
        gw.script = [rate_limited("Retry-After: 120")]
        worker.run_once(conn)
        j = job(conn, tx)
        assert j["attempts"] == 0 and not j["done_at"] and 110 <= j["wait"] <= 125 and "429" in j["last_error"]
        assert analysis(conn, tx)["tier3_state"] == "queued"                                 # not failed, not given the fallback
        assert triage_limits.breaker(conn)["consecutive_429"] == 1 and not triage_limits.breaker(conn)["open"]
        assert triage_limits.calls_24h(conn) == 0                                           # a 429 never reached the model: no budget used


def test_without_retry_after_it_backs_off_from_a_minute_and_a_success_resets_the_streak(gw):
    with connect() as conn:
        put(conn, rpm=0, daily_cap=0, group_minutes=0)
        tx = case(conn, 1)
        gw.script = [rate_limited(), rate_limited()]
        waits = []
        for _ in range(2):
            conn.execute("UPDATE outbox SET available_at = now()")
            conn.commit()
            worker.run_once(conn)
            waits.append(job(conn, tx)["wait"])
        assert 55 <= waits[0] <= 61 and 115 <= waits[1] <= 121                              # 60 s, then 120 s
        conn.execute("UPDATE outbox SET available_at = now()")
        conn.commit()
        worker.run_once(conn)                                                               # the model answers
        assert analysis(conn, tx)["a"]["source"] == "llm" and triage_limits.breaker(conn)["consecutive_429"] == 0


def test_three_429s_open_the_breaker_new_cases_stay_queued_and_old_ones_get_the_heuristic(gw):
    with connect() as conn:
        put(conn, rpm=0, daily_cap=0, group_minutes=0, pause_minutes=10)
        txs = [case(conn, n, device=f"d{n}") for n in range(3)]
        gw.script = [rate_limited(), rate_limited(), rate_limited()]
        worker.run_once(conn)
        assert gw.calls == 3 and all(job(conn, t)["attempts"] == 0 and not job(conn, t)["done_at"] for t in txs)
        b = triage_limits.breaker(conn)
        assert b["open"] and b["consecutive_429"] == 3 and 590 <= b["paused_until"] - datetime.now(UTC).timestamp() <= 601
        waits = [job(conn, t)["wait"] for t in txs]                                         # 60 s, 120 s, then the whole pause
        assert 55 <= waits[0] <= 61 and 115 <= waits[1] <= 121 and 590 <= waits[2] <= 601
        fresh = case(conn, 10, device="fresh")
        stale = case(conn, 11, device="stale", age_s=2 * 3600)
        worker.run_once(conn)
        assert gw.calls == 3                                                                # paused: no call at all
        jf = job(conn, fresh)
        assert not jf["done_at"] and jf["attempts"] == 0 and 590 <= jf["wait"] <= 601 and analysis(conn, fresh)["tier3_state"] == "queued"
        a = analysis(conn, stale)
        assert job(conn, stale)["done_at"] and a["tier3_state"] == "done" and a["a"]["source"] == "heuristic" and "paused" in a["a"]["note"]
        conn.execute("UPDATE app_setting SET value='0' WHERE key='triage_paused_until'")      # the pause ends: every waiting case (the 3 retried and the fresh one) goes to the model
        conn.execute("UPDATE outbox SET available_at = now()")
        conn.commit()
        worker.run_once(conn)
        assert gw.calls == 7 and analysis(conn, fresh)["a"]["source"] == "llm" and not triage_limits.breaker(conn)["open"]


def test_other_llm_errors_keep_the_old_retry_behaviour(gw):
    with connect() as conn:
        put(conn, rpm=0, daily_cap=0, group_minutes=0)
        tx = case(conn, 1)
        gw.script = [LLMError("chat endpoint returned HTTP 502: bad gateway")] * 3
        for expected in (1, 2):
            conn.execute("UPDATE outbox SET available_at = now()")
            conn.commit()
            worker.run_once(conn)
            assert job(conn, tx)["attempts"] == expected and not job(conn, tx)["done_at"] and 25 <= job(conn, tx)["wait"] <= 1801
        conn.execute("UPDATE outbox SET available_at = now()")
        conn.commit()
        worker.run_once(conn)
        assert job(conn, tx)["done_at"] and analysis(conn, tx)["tier3_state"] == "failed" and triage_limits.breaker(conn)["consecutive_429"] == 0


def test_an_unusable_answer_counts_against_the_cap_but_a_gateway_error_does_not(gw):
    with connect() as conn:
        put(conn, rpm=0, daily_cap=0, group_minutes=0)
        t1, t2 = case(conn, 1, device="a"), case(conn, 2, device="b")
        gw.script = ["not json at all", LLMError("chat endpoint returned HTTP 500: boom")]
        worker.run_once(conn)
        assert triage_limits.calls_24h(conn) == 1
        assert conn.execute("SELECT outcome FROM triage_llm_call").fetchone()["outcome"] == "error" and t1 and t2


def test_no_account_means_no_throttle_and_no_call(client, monkeypatch):
    g = Gateway()
    monkeypatch.setattr(triage, "chat", g)
    monkeypatch.setattr(triage.accounts, "triage_account", lambda conn: None)
    with connect() as conn:
        tx = case(conn, 1)
        worker.run_once(conn)
        assert g.calls == 0 and analysis(conn, tx)["a"]["source"] == "heuristic" and "note" not in analysis(conn, tx)["a"]


# ---- admin API -----------------------------------------------------------------------------------------------------
URL = "/api/admin/triage/settings"
CSRF = {"X-PayBroker-CSRF": "1"}


def test_settings_default_then_environment_then_setting_with_usage(client, op, gw, monkeypatch):
    out = client.get(URL, headers=op).json()
    assert {f: out[f] for f in triage_limits.KEYS} == {"rpm": 20, "daily_cap": 120, "group_minutes": 30, "pause_minutes": 10}
    assert set(out["sources"].values()) == {"default"}
    assert out["usage"] == {"calls_24h": 0, "queued": 0, "circuit": {"open": False, "paused_until": None, "consecutive_429": 0}}
    monkeypatch.setenv("PAYBROKER_TRIAGE_RPM", "30")
    out = client.get(URL, headers=op).json()
    assert out["rpm"] == 30 and out["sources"]["rpm"] == "environment"
    r = client.put(URL, json={"rpm": 0, "daily_cap": 100}, headers=op)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["rpm"] == 0 and out["daily_cap"] == 100 and out["group_minutes"] == 30 and out["sources"]["rpm"] == "setting"
    with connect() as conn:
        case(conn, 1)
        logged(conn, str(uuid.uuid4()), "ok")
    use = client.get(URL, headers=op).json()["usage"]
    assert use["calls_24h"] == 1 and use["queued"] == 1


def test_settings_are_validated_audited_and_need_settings_manage(client, op, shop=None):
    for bad in ({"rpm": -1}, {"rpm": 601}, {"daily_cap": 1_000_001}, {"group_minutes": 1441}, {"pause_minutes": 0}, {"rpm": "fast"}, {"nope": 1}):
        assert client.put(URL, json=bad, headers=op).status_code == 422, bad
    assert client.put(URL, json={"rpm": 40, "pause_minutes": 5}, headers=op).status_code == 200
    assert client.put(URL, json={"rpm": 10}, headers=op).status_code == 200
    with connect() as conn:
        rows = conn.execute("SELECT actor, detail FROM admin_audit WHERE action='triage.settings' ORDER BY id").fetchall()
    assert len(rows) == 2 and rows[0]["actor"] and rows[0]["detail"] == {"rpm": {"from": 20, "to": 40}, "pause_minutes": {"from": 10, "to": 5}}
    assert rows[1]["detail"] == {"rpm": {"from": 40, "to": 10}}
    read = client.post("/api/tokens", json={"name": "ro", "scope": "read"}, headers=op).json()["token"]
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers=bearer(read)).status_code == 403
    assert client.put(URL, json={"rpm": 1}, headers=bearer(read)).status_code == 403
    assert client.get(URL, headers=op).json()["rpm"] == 10


def test_metrics_expose_the_triage_families(client, op, gw):
    with connect() as conn:
        put(conn, rpm=0, daily_cap=1, group_minutes=0)
        case(conn, 1, device="a")
        case(conn, 2, device="b")
        worker.run_once(conn)
        case(conn, 3, device="c")
    text = client.get("/metrics", headers=op).text
    assert 'payment_broker_triage_calls_total{outcome="ok"}' in text and 'payment_broker_triage_calls_total{outcome="capped"}' in text
    assert 'payment_broker_triage_queue_depth{app="paybroker"} 1.0' in text and 'payment_broker_triage_llm_calls_24h{app="paybroker"} 1.0' in text
    assert 'payment_broker_triage_circuit_open{app="paybroker"} 0.0' in text


def test_a_data_reset_keeps_the_call_log_so_it_cannot_hand_back_the_budget(client, op, gw):
    from paybroker import maintenance
    with connect() as conn:
        logged(conn, str(uuid.uuid4()), "ok")
        maintenance.reset(actor="t", include_training_data=True, include_rule_suggestions=True)
        assert triage_limits.calls_24h(conn) == 1


def test_purge_drops_only_old_call_rows(client):
    with connect() as conn:
        logged(conn, str(uuid.uuid4()), "ok", minutes_ago=8 * 24 * 60)
        logged(conn, str(uuid.uuid4()), "ok", minutes_ago=60)
        assert triage_limits.purge(conn) == 1
        assert conn.execute("SELECT count(*) AS n FROM triage_llm_call").fetchone()["n"] == 1


# ---- upgrade safety ------------------------------------------------------------------------------------------------
def test_migration_004_applies_on_top_of_a_database_that_holds_payments():
    """001 to 003 plus data, then 004: nothing is lost, the new table starts empty, and re-running 004 is harmless."""
    from pathlib import Path

    from paybroker import migrate
    sql = {p.name: p.read_text() for p in sorted(Path(migrate.MIGRATIONS).glob("*.sql"))}
    with connect() as conn:
        conn.execute("DROP SCHEMA IF EXISTS mig_upgrade4 CASCADE")
        conn.execute("CREATE SCHEMA mig_upgrade4")
        conn.commit()
        conn.execute("SET search_path = mig_upgrade4, public")
        for name in ("001_init.sql", "002_training.sql", "003_admin_audit.sql"):
            conn.execute(sql[name])
        conn.execute("INSERT INTO merchant (id, name) VALUES ('mch_upgrade', 'Old Shop')")
        conn.execute("INSERT INTO transaction (payment_id, merchant_id, idempotency_key, request_hash, amount, currency, status) "
                     "VALUES ('pay_old','mch_upgrade','k','h',100,'USD','CAPTURED')")
        conn.execute("INSERT INTO admin_audit (actor, action) VALUES ('a', 'data.reset')")
        conn.execute(sql["004_triage_calls.sql"])
        conn.execute(sql["004_triage_calls.sql"])
        assert conn.execute("SELECT count(*) AS n FROM triage_llm_call").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM transaction").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM admin_audit").fetchone()["n"] == 1
        conn.execute("INSERT INTO triage_llm_call (transaction_id, outcome) VALUES (gen_random_uuid(), 'ok')")
        with pytest.raises(Exception, match="outcome"):
            conn.execute("INSERT INTO triage_llm_call (transaction_id, outcome) VALUES (gen_random_uuid(), 'bogus')")
        conn.rollback()
        conn.execute("SET search_path = public")
        conn.execute("DROP SCHEMA mig_upgrade4 CASCADE")
        conn.commit()
