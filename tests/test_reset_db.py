"""Data reset: the transaction data goes, the configuration stays, nothing else is touched (against a real Postgres)."""
import json
import threading

import httpx
import pytest

from tests.conftest import needs_db

pytestmark = needs_db

from paybroker import chat, cli, core, demo, features, llm_http, maintenance, webhooks, worker  # noqa: E402
from paybroker.db import connect  # noqa: E402
from tests.test_db import KEY, bearer, pay, tokenize  # noqa: E402

PHRASE = "RESET PAYBROKER DATA"
CSRF = {"X-PayBroker-CSRF": "1"}
URL = "/api/admin/data/reset"


@pytest.fixture
def op(monkeypatch):
    monkeypatch.setenv("PAYBROKER_API_KEY", KEY)
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def shop(client, op):
    mid = client.post("/api/merchants", json={"name": "Reset Shop"}, headers=op).json()["id"]
    sk = client.post(f"/api/merchants/{mid}/keys", json={"kind": "secret", "name": "server"}, headers=op).json()["key"]
    pk = client.post(f"/api/merchants/{mid}/keys", json={"kind": "publishable", "name": "browser"}, headers=op).json()["key"]
    return mid, bearer(sk), bearer(pk)


def resets() -> int:
    with connect() as conn:
        n = conn.execute("SELECT count(*) AS n FROM admin_audit WHERE action='data.reset'").fetchone()["n"]
        conn.rollback()
    return n


def counts(tables=None) -> dict:
    with connect() as conn:
        names = tables or [r["tablename"] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")]
        out = {t: conn.execute(f'SELECT count(*) AS n FROM "{t}"').fetchone()["n"] for t in sorted(names)}
        conn.rollback()
    return out


def populate(client, op, shop, monkeypatch) -> dict:
    """Configuration of every kind, then traffic of every kind. Returns what a test needs afterwards."""
    mid, sk, pk = shop
    public = [(2, 1, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: public)
    # no test ever calls out: every webhook delivery and every language-model call is answered by a stub
    monkeypatch.setattr(webhooks, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    def no_network(*a, **k):
        raise llm_http.LLMError("tests never reach a language model")
    monkeypatch.setattr(chat, "post_json", no_network)
    client.post("/api/admin-password", json={"new": "correct horse battery staple"}, headers=op)        # the vault
    assert client.put(f"/api/merchants/{mid}/webhook", json={"url": "https://merchant.example/hook"}, headers=op).status_code == 200
    assert client.post("/api/rules", json={"name": "keep_me", "definition": {"all": [{"field": "amount", "op": ">=", "value": 9_999_00}]},
                                           "action": "challenge"}, headers=op).status_code == 201
    assert client.post("/api/blacklist", json={"kind": "ip", "value": "203.0.113.250", "reason": "manual"}, headers=op).status_code == 201
    assert client.post("/api/accounts", json={"name": "llm", "provider": "custom", "base_url": "https://llm.example/v1",
                                              "api_key": "sk-test-not-real", "chat_model": None}, headers=op).status_code == 201
    from tests.test_training_db import leaf_model, upload
    assert upload(client, op, leaf_model(-3.0, "kept-model")).status_code in (200, 201)
    assert client.put("/api/admin/training/settings", json={"capture": True}, headers=op).status_code == 200
    token = client.post("/api/tokens", json={"name": "ci", "scope": "full"}, headers=op).json()["token"]
    tok = tokenize(client, pk)
    with connect() as conn:
        demo.run(conn, 120, seed=3)
        conn.execute("INSERT INTO rule_suggestion (name, definition, action) VALUES ('s', '{}', 'decline')")
        conn.commit()
    for i in range(4):
        r = pay(client, sk, tok, f"reset-order-{i}", customer=f"cus_r{i}", ip=f"198.51.100.{60 + i}")
        assert r.status_code in (200, 403), r.text
    ok = pay(client, sk, tok, "reset-order-ok", customer="cus_ok", ip="198.51.100.99").json()
    client.post(f"/api/payments/{ok['payment_id']}/chargeback", json={"reason": "fraud claim", "reason_code": "10.4"}, headers=op)
    case = client.get("/api/cases", headers=op).json()["items"][0]
    client.post(f"/api/cases/{case['id']}/resolve", json={"decision": "PERMANENT_BLOCK"}, headers=op)   # adds a case-sourced blacklist row
    with connect() as conn:
        worker.run_once(conn)
    return {"mid": mid, "sk": sk, "pk": pk, "tok": tok, "full": bearer(token)}


def reset(client, headers, **body):
    return client.post(URL, json={"confirm": PHRASE, **body}, headers=headers)


# ---- what is deleted and what is kept ---------------------------------------------------------------------------
def test_reset_deletes_exactly_the_transaction_tables_and_keeps_the_configuration(client, op, shop, monkeypatch):
    ctx = populate(client, op, shop, monkeypatch)
    before = counts()
    for t in maintenance.TABLES:
        assert before[t] > 0, f"the scenario must put rows in {t}"
    assert before["blacklist"] >= 2 and before["card"] > 0
    r = reset(client, ctx["full"])
    assert r.status_code == 200, r.text
    out = r.json()
    after = counts()
    assert set(out["deleted"]) == set(maintenance.TABLES) and out["skipped"] == []
    for t in maintenance.TABLES:
        assert after[t] == 0 and out["deleted"][t] == before[t], t
    for t, n in before.items():                                      # every other table is exactly as it was
        if t not in maintenance.TABLES and t != "admin_audit":
            assert after[t] == n, f"{t} must be kept"
    assert resets() == 1                                              # (other audit rows, like merchant keys, are not resets)
    assert set(before) - set(maintenance.TABLES) <= set(maintenance.KEPT), "KEPT documents every table a reset leaves alone"
    assert out["seconds"] >= 0 and out["max_chunk_rows"] <= maintenance.CHUNK


def test_reset_keeps_the_configuration_working(client, op, shop, monkeypatch):
    ctx = populate(client, op, shop, monkeypatch)
    with connect() as conn:
        keys_before = conn.execute("SELECT key_hash, kind FROM merchant_key ORDER BY id").fetchall()
        hook_before = conn.execute("SELECT webhook_url, webhook_secret_enc FROM merchant WHERE id=%s", (ctx["mid"],)).fetchone()
        conn.rollback()
    assert reset(client, ctx["full"]).status_code == 200
    with connect() as conn:
        assert conn.execute("SELECT key_hash, kind FROM merchant_key ORDER BY id").fetchall() == keys_before
        assert conn.execute("SELECT webhook_url, webhook_secret_enc FROM merchant WHERE id=%s", (ctx["mid"],)).fetchone() == hook_before
        conn.rollback()
    names = {x["name"] for x in client.get("/api/rules", headers=op).json()["items"]}
    assert "keep_me" in names and {"new_account_high_amount", "many_cards_one_account"} <= names
    assert any(b["value"] == "203.0.113.250" for b in client.get("/api/blacklist", headers=op).json()["items"])
    assert [a["name"] for a in client.get("/api/accounts", headers=op).json()["items"]] == ["llm"]
    models = client.get("/api/admin/models", headers=op).json()["items"]
    assert [m["version"] for m in models] == ["kept-model"]
    assert client.get("/api/admin/training/settings", headers=op).json()["capture"] is True
    assert len(client.get("/api/connectors", headers=op).json()["items"]) == 4
    assert client.get("/api/tokens", headers=ctx["full"]).status_code == 200          # the token that did the reset still works
    # the merchant key that existed before authenticates, and the card token it vaulted still pays
    r = pay(client, ctx["sk"], ctx["tok"], "after-reset-1", customer="cus_after", ip="198.51.100.150")
    assert r.status_code == 200 and r.json()["status"] == "AUTHORIZED", r.text
    assert client.get("/api/payments", headers=op).json()["items"][0]["payment_id"] == r.json()["payment_id"]
    assert client.get("/api/overview", headers=op).json()["payments"] == 1
    # idempotency records went with the payments: the old key is free again and is a NEW payment
    again = pay(client, ctx["sk"], ctx["tok"], "reset-order-ok", customer="cus_ok", ip="198.51.100.99")
    assert again.status_code == 200


def test_the_two_options_keep_training_rows_and_suggestions(client, op, shop, monkeypatch):
    ctx = populate(client, op, shop, monkeypatch)
    before = counts(["training_example", "rule_suggestion"])
    assert before["training_example"] > 0 and before["rule_suggestion"] > 0
    out = reset(client, ctx["full"], include_training_data=False, include_rule_suggestions=False).json()
    assert sorted(out["skipped"]) == ["rule_suggestion", "training_example"] and "training_example" not in out["deleted"]
    assert counts(["training_example", "rule_suggestion", "transaction"]) == {**before, "transaction": 0}
    out = reset(client, ctx["full"], include_training_data=True, include_rule_suggestions=False).json()
    assert out["deleted"]["training_example"] == before["training_example"]
    assert counts(["training_example", "rule_suggestion"]) == {"training_example": 0, "rule_suggestion": before["rule_suggestion"]}
    out = reset(client, ctx["full"]).json()                                           # the defaults delete both
    assert out["deleted"]["rule_suggestion"] == before["rule_suggestion"]
    assert counts(["rule_suggestion"]) == {"rule_suggestion": 0}


def test_velocity_is_clean_after_a_reset(client, op, shop):
    """A customer with ten payments scores like a new customer once the feature store is gone."""
    mid, sk, pk = shop
    tok = tokenize(client, pk)

    def snapshot(key):
        p = pay(client, sk, tok, key, customer="cus_busy", ip="198.51.100.140", amount=7_000).json()
        pid = p.get("payment_id") or p["error"]["payment_id"]
        d = client.get(f"/api/payments/{pid}", headers=op).json()["fraud"]
        return d["tier2_ml_score"], d["tier2_feature_snapshot"]

    fresh_score, fresh = snapshot("velocity-0")
    assert fresh["tx_count_1h"] == 0
    for i in range(1, 10):
        snapshot(f"velocity-{i}")
    busy_score, busy = snapshot("velocity-10")
    assert busy["tx_count_1h"] >= 5 and busy["user_tx_count_1m"] >= 5
    assert reset(client, op).status_code == 200
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fs_entity").fetchone()["n"] == 0
        conn.rollback()
    score, snap = snapshot("velocity-after")
    assert snap["tx_count_1h"] == 0 and snap["user_tx_count_1m"] == 0 and snap["device_tx_count_1h"] == 0
    assert (score, snap) == (fresh_score, fresh)
    assert busy_score >= fresh_score


def test_features_for_a_known_customer_are_empty_after_the_reset_at_the_store_level(client, op, shop):
    _, sk, pk = shop
    pay(client, sk, tokenize(client, pk), "store-key-1", customer="cus_fs")
    keys = features.entity_keys("cus_fs", "dev_1", None, "198.51.100.20")
    with connect() as conn:
        assert features.fetch(conn, keys)
        conn.rollback()
    reset(client, op)
    with connect() as conn:
        assert features.fetch(conn, keys) == {}
        conn.rollback()


# ---- chunking, resuming, locking --------------------------------------------------------------------------------
def _bulk(mid: str, n_tx: int, n_other: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO transaction (payment_id, merchant_id, idempotency_key, request_hash, amount, currency, status) "
            "SELECT 'pay_bulk' || g, %s, 'bulk-key-' || g, 'h', 100, 'USD', 'AUTHORIZED' FROM generate_series(1, %s) g", (mid, n_tx))
        conn.execute("INSERT INTO chargeback (payment_id, amount) SELECT 'pay_bulk' || g, 100 FROM generate_series(1, %s) g", (min(n_tx, 300),))
        conn.execute("INSERT INTO outbox (topic, payload) SELECT 'webhook', '{}' FROM generate_series(1, %s)", (n_other,))
        conn.execute("INSERT INTO fs_entity (entity_type, entity_id) SELECT 'user', 'u' || g FROM generate_series(1, %s) g", (n_other,))
        conn.commit()


def test_a_large_reset_runs_in_bounded_chunks(client, op, shop):
    mid, _, _ = shop
    _bulk(mid, 5_300, 12_000)
    sizes = []
    out = maintenance.reset(actor="test", on_chunk=lambda table, n: sizes.append((table, n)))
    assert out["deleted"]["transaction"] == 5_300 and out["deleted"]["outbox"] == 12_000 and out["deleted"]["fs_entity"] == 12_000
    assert max(n for _, n in sizes) <= 5_000 == maintenance.CHUNK and out["max_chunk_rows"] == 5_000
    assert [n for t, n in sizes if t == "outbox"] == [5_000, 5_000, 2_000]
    assert [n for t, n in sizes if t == "transaction"] == [5_000, 300]
    assert out["chunks"] == len(sizes) and sum(counts(list(maintenance.TABLES)).values()) == 0
    assert maintenance.reset(actor="test", chunk=10**9)["max_chunk_rows"] == 0                # a ceiling above CHUNK is clamped; nothing left


def test_an_interrupted_reset_leaves_a_consistent_database_and_resumes(client, op, shop):
    mid, sk, pk = shop
    with connect() as conn:
        demo.run(conn, 60, seed=5)
    _bulk(mid, 400, 300)
    total = counts(list(maintenance.TABLES))
    seen = []

    def boom(table, n):
        seen.append(table)
        if table == "transaction" and seen.count("transaction") == 2:
            raise RuntimeError("the process was killed here")

    with pytest.raises(RuntimeError):
        maintenance.reset(actor="test", chunk=100, on_chunk=boom)
    half = counts(list(maintenance.TABLES))
    assert 0 < half["transaction"] < total["transaction"]
    assert half["outbox"] == half["webhook_event"] == half["fraud_assessment"] == half["three_ds_challenge"] == half["chargeback"] == 0
    with connect() as conn:                                            # no orphan: nothing points at a payment that is gone
        assert conn.execute("SELECT count(*) AS n FROM chargeback c LEFT JOIN transaction t USING (payment_id) WHERE t.id IS NULL").fetchone()["n"] == 0
        a = conn.execute("SELECT detail FROM admin_audit WHERE action='data.reset' ORDER BY id DESC LIMIT 1").fetchone()["detail"]
        conn.rollback()
    assert a["status"] == "interrupted" and a["error"] == "RuntimeError"
    out = maintenance.reset(actor="test", chunk=100)                   # the lock was released; the second run finishes the job
    assert out["deleted"]["transaction"] == half["transaction"]
    assert sum(counts(list(maintenance.TABLES)).values()) == 0
    with connect() as conn:
        assert [r["detail"]["status"] for r in conn.execute("SELECT detail FROM admin_audit WHERE action='data.reset' ORDER BY id")] == ["interrupted", "completed"]
        conn.rollback()


def test_two_resets_cannot_run_together(client, op, shop):
    mid, _, _ = shop
    _bulk(mid, 10, 50)
    holding, release, result = threading.Event(), threading.Event(), {}

    def slow(table, n):
        holding.set()
        assert release.wait(20)

    def first():
        result["first"] = maintenance.reset(actor="a", chunk=10, on_chunk=slow)

    t = threading.Thread(target=first)
    t.start()
    assert holding.wait(20)
    try:
        with pytest.raises(maintenance.ResetBusy):
            maintenance.reset(actor="b")
        r = reset(client, op)
        assert r.status_code == 409 and "already running" in r.json()["detail"]
        assert cli.main(["reset-data", "--yes", "--confirm", PHRASE]) == 1
    finally:
        release.set()
        t.join(30)
    assert result["first"]["deleted"]["outbox"] == 50
    assert reset(client, op).status_code == 200                       # the lock is free again
    with connect() as conn:                                            # the refused attempts left no audit row
        assert [r["actor"] for r in conn.execute("SELECT actor FROM admin_audit WHERE action='data.reset' ORDER BY id")] == ["a", conn.execute(
            "SELECT actor FROM admin_audit WHERE action='data.reset' ORDER BY id DESC LIMIT 1").fetchone()["actor"]]
        conn.rollback()


# ---- who may, the phrase, the audit trail -----------------------------------------------------------------------
def test_only_a_full_credential_may_reset(client, op, shop):
    mid, sk, pk = shop
    read = bearer(client.post("/api/tokens", json={"name": "ro", "scope": "read"}, headers=op).json()["token"])
    metrics = bearer(client.post("/api/tokens", json={"name": "m", "scope": "metrics"}, headers=op).json()["token"])
    full = bearer(client.post("/api/tokens", json={"name": "f", "scope": "full"}, headers=op).json()["token"])
    pay(client, sk, tokenize(client, pk), "permission-1")
    body = {"confirm": PHRASE}
    assert client.post(URL, json=body, headers=CSRF).status_code == 401 and client.get(URL).status_code == 401
    assert client.post(URL, json=body).status_code == 403                                             # no credential and no CSRF header
    assert client.post(URL, json=body, headers=bearer("pbt_nope")).status_code == 401
    for who in (read, metrics, sk, pk):
        assert client.post(URL, json=body, headers=who).status_code == 403
        assert client.get(URL, headers=who).status_code == 403
    assert counts(["transaction"]) == {"transaction": 1}
    r = client.post(URL, json=body, headers=full)
    assert r.status_code == 200 and counts(["transaction"]) == {"transaction": 0}
    assert client.get(URL, headers=full).status_code == 200


def test_a_wrong_phrase_deletes_nothing(client, op, shop):
    mid, sk, pk = shop
    pay(client, sk, tokenize(client, pk), "phrase-key-1")
    for bad in ("", "reset paybroker data", " RESET PAYBROKER DATA", "RESET PAYBROKER DATA ", "RESET", "RESET PAYBROKER DATA!"):
        assert client.post(URL, json={"confirm": bad}, headers=op).status_code == 422, repr(bad)
    assert client.post(URL, json={}, headers=op).status_code == 422                                    # the phrase is required
    assert client.post(URL, json={"confirm": PHRASE, "include_training_data": "maybe"}, headers=op).status_code == 422
    assert client.post(URL, json={"confirm": PHRASE, "surprise": 1}, headers=op).status_code == 422
    assert counts(["transaction"]) == {"transaction": 1} and resets() == 0


def test_cookie_sessions_need_the_csrf_header(client):
    assert client.post("/api/setup", json={"code": core.local.setup_code(), "password": "correct horse battery staple"}, headers=CSRF).status_code == 200
    assert client.post(URL, json={"confirm": PHRASE}).status_code == 403
    assert client.post(URL, json={"confirm": PHRASE}, headers=CSRF).status_code == 200


def test_the_audit_trail_names_who_what_and_when(client, op, shop, monkeypatch):
    ctx = populate(client, op, shop, monkeypatch)
    pre = client.get(URL, headers=ctx["full"]).json()
    assert pre["phrase"] == PHRASE and pre["recent"] == [] and set(pre["counts"]) == set(maintenance.TABLES)
    assert pre["counts"]["transaction"] == counts(["transaction"])["transaction"] > 0
    reset(client, ctx["full"], include_training_data=False)
    reset(client, ctx["full"])
    rows = client.get(URL, headers=ctx["full"]).json()["recent"]
    assert len(rows) == 2 and rows[0]["id"] > rows[1]["id"] and all(r["actor"] and r["at"] for r in rows)
    first, second = rows[1]["detail"], rows[0]["detail"]
    assert first["status"] == "completed" and first["include_training_data"] is False and first["skipped"] == ["training_example"]
    assert first["deleted"]["transaction"] > 0 and second["deleted"]["transaction"] == 0 and second["deleted"]["training_example"] > 0
    blob = json.dumps(rows, default=str)
    for secret in (KEY, "sk_test_", "whsec_", "sk-test-not-real", "pbt_"):
        assert secret not in blob
    assert client.get(URL, headers=op).json()["counts"]["transaction"] == 0


def test_the_queue_survives_a_worker_running_during_the_reset(client, op, shop, monkeypatch):
    """A worker holding a claimed job for a payment that is then wiped must not crash or loop."""
    ctx = populate(client, op, shop, monkeypatch)
    with connect() as conn:
        jobs = worker.claim(conn)
    assert reset(client, ctx["full"]).status_code == 200
    with connect() as conn:
        for job in jobs:
            try:
                worker.handle(conn, job)
            except Exception:  # noqa: BLE001 - run_once catches whatever handle raises; only a hang would be a problem
                conn.rollback()
        assert worker.run_once(conn) == 0


# ---- the command line ------------------------------------------------------------------------------------------
def test_cli_requires_yes_and_the_exact_phrase(client, op, shop, capsys):
    mid, sk, pk = shop
    r = pay(client, sk, tokenize(client, pk), "cli-key-1")
    assert r.status_code == 200, r.text
    assert cli.main(["reset-data"]) == 2
    assert cli.main(["reset-data", "--yes"]) == 2
    assert cli.main(["reset-data", "--confirm", PHRASE]) == 2
    assert cli.main(["reset-data", "--yes", "--confirm", "reset paybroker data"]) == 2
    err = capsys.readouterr().err
    assert "--yes" in err and PHRASE in err
    assert counts(["transaction"]) == {"transaction": 1} and resets() == 0


def test_cli_resets_and_can_keep_the_training_data(client, op, shop, monkeypatch, capsys):
    ctx = populate(client, op, shop, monkeypatch)
    n_train = counts(["training_example"])["training_example"]
    capsys.readouterr()
    assert cli.main(["reset-data", "--yes", "--confirm", PHRASE, "--keep-training-data"]) == 0
    out = capsys.readouterr().out
    assert "transaction:" in out and "deleted" in out and "training_example: kept" in out and "sk_" not in out
    left = counts(["transaction", "training_example", "rule_suggestion", "merchant"])
    assert left["transaction"] == 0 and left["training_example"] == n_train and left["rule_suggestion"] == 0 and left["merchant"] >= 2
    assert cli.main(["reset-data", "--yes", "--confirm", PHRASE, "--keep-rule-suggestions"]) == 0
    assert counts(["training_example"])["training_example"] == 0
    with connect() as conn:
        actors = [r["actor"] for r in conn.execute("SELECT actor FROM admin_audit WHERE action='data.reset' ORDER BY id")]
        conn.rollback()
    assert len(actors) == 2 and all(a.startswith("cli:") for a in actors)
    assert pay(client, ctx["sk"], ctx["tok"], "cli-key-after", customer="cus_cli", ip="198.51.100.151").status_code == 200


def test_the_permission_is_registered_for_admins_only():
    assert "data:reset" in core.PERMISSIONS
    assert core.ROLE_PERMISSIONS["admin"] == core.ALL
    for role in ("operator", "risk_manager", "analyst", "viewer"):
        assert "data:reset" not in core.ROLE_PERMISSIONS[role] and "*" not in core.ROLE_PERMISSIONS[role], role
    assert core.SCOPE_PERMISSIONS["full"] == core.ALL
    for scope in ("read", "metrics", "paybroker.read", "paybroker.metrics"):
        assert "data:reset" not in core.SCOPE_PERMISSIONS[scope], scope
    from paybroker import auth
    assert all("data:reset" not in p for p in auth.MERCHANT_PERMISSIONS.values())


def test_a_merchant_webhook_endpoint_stays_deliverable(client, op, shop, monkeypatch):
    """The webhook URL and its encrypted signing secret are configuration: a payment after the reset still queues a delivery."""
    ctx = populate(client, op, shop, monkeypatch)
    reset(client, ctx["full"])
    seen = []
    monkeypatch.setattr(webhooks, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200))))
    assert pay(client, ctx["sk"], ctx["tok"], "hook-key-after", customer="cus_hook", ip="198.51.100.152").status_code == 200
    with connect() as conn:
        assert worker.run_once(conn) == 1
    assert len(seen) == 1 and seen[0].headers["x-signature"]
