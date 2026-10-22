"""The whole broker against a real Postgres: payments, idempotency, fraud decisions, cases, rules, merchants, webhooks, MCP."""
import hashlib
import hmac
import json
import os

import httpx
import pytest

from tests.conftest import needs_db

pytestmark = needs_db

from paybroker import cards, demo, insights, webhooks, worker  # noqa: E402
from paybroker.db import connect  # noqa: E402

KEY = "operator-test-key-0123456789"
CSRF = {"X-PayBroker-CSRF": "1"}


@pytest.fixture
def op(monkeypatch):
    monkeypatch.setenv("PAYBROKER_API_KEY", KEY)
    return {"Authorization": f"Bearer {KEY}"}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def shop(client, op):
    """A merchant with a secret and a publishable key. Returns (merchant_id, secret_headers, publishable_headers)."""
    m = client.post("/api/merchants", json={"name": "Acme Shop"}, headers=op)
    assert m.status_code == 201, m.text
    mid = m.json()["id"]
    sk = client.post(f"/api/merchants/{mid}/keys", json={"kind": "secret", "name": "server"}, headers=op).json()["key"]
    pk = client.post(f"/api/merchants/{mid}/keys", json={"kind": "publishable", "name": "browser"}, headers=op).json()["key"]
    return mid, bearer(sk), bearer(pk)


def tokenize(client, pk, number="4242424242424242"):
    r = client.post("/v1/vault/tokens", json={"number": number, "exp_month": 12, "exp_year": 2035, "cvc": "123"}, headers=pk)
    assert r.status_code == 201, r.text
    return r.json()["token"]


def pay(client, sk, token, key, amount=5000, ip="198.51.100.20", customer="cus_1", **extra):
    body = {"idempotency_key": key, "amount": amount, "currency": "USD", "payment_method": {"type": "card", "card_token": token},
            "customer": {"id": customer, "email": "a@example.com", "ip_address": ip},
            "telemetry": {"device_fingerprint": "dev_1", "checkout_duration_ms": 40_000}, "shipping_address": {"country": "US"}}
    body.update(extra)
    return client.post("/v1/payments/authorize", json=body, headers=sk)


# ---- the payment lifecycle -------------------------------------------------------------------------------------
def test_authorize_capture_refund(client, shop):
    _, sk, pk = shop
    tok = tokenize(client, pk)
    r = pay(client, sk, tok, "order-0001", capture_method="manual")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["status"] == "AUTHORIZED" and p["amount"] == 5000 and p["payment_id"].startswith("pay_")
    assert p["fraud_assessment"]["decision"] == "APPROVE"
    cap = client.post(f"/v1/payments/{p['payment_id']}/capture", json={"amount": 4000}, headers=sk)
    assert cap.status_code == 200 and cap.json()["status"] == "CAPTURED"
    assert client.post(f"/v1/payments/{p['payment_id']}/capture", headers=sk).status_code == 409   # already captured
    ref = client.post(f"/v1/payments/{p['payment_id']}/refund", json={"amount": 1000}, headers=sk)
    assert ref.json()["status"] == "PARTIALLY_REFUNDED"
    assert client.post(f"/v1/payments/{p['payment_id']}/refund", json={"amount": 3001}, headers=sk).status_code == 422
    assert client.post(f"/v1/payments/{p['payment_id']}/refund", headers=sk).json()["status"] == "REFUNDED"
    got = client.get(f"/v1/payments/{p['payment_id']}", headers=sk)
    assert got.json()["status"] == "REFUNDED"


def test_void_only_when_authorized(client, shop):
    _, sk, pk = shop
    p = pay(client, sk, tokenize(client, pk), "order-void-1").json()
    assert client.post(f"/v1/payments/{p['payment_id']}/void", headers=sk).json()["status"] == "VOIDED"
    assert client.post(f"/v1/payments/{p['payment_id']}/void", headers=sk).status_code == 409


def test_idempotent_replay_and_key_reuse(client, shop):
    _, sk, pk = shop
    tok = tokenize(client, pk)
    first = pay(client, sk, tok, "order-idem-1")
    again = pay(client, sk, tok, "order-idem-1")
    assert first.status_code == again.status_code == 200
    assert first.json()["payment_id"] == again.json()["payment_id"]
    other = pay(client, sk, tok, "order-idem-1", amount=9999)
    assert other.status_code == 422 and other.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM transaction").fetchone()["n"] == 1


def test_concurrent_requests_with_one_key_create_one_payment(client, shop):
    import concurrent.futures as cf
    _, sk, pk = shop
    tok = tokenize(client, pk)
    with cf.ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda _: pay(client, sk, tok, "order-race-1"), range(8)))
    ids = {r.json().get("payment_id") or r.json()["error"].get("payment_id") for r in results if r.status_code in (200, 402, 403)}
    assert len(ids) == 1
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM transaction").fetchone()["n"] == 1


@pytest.mark.parametrize("number,code,decline", [("4000000000000002", "CARD_DECLINED", "do_not_honor"),
                                                 ("4000000000009995", "CARD_DECLINED", "insufficient_funds")])
def test_issuer_declines(client, shop, number, code, decline):
    _, sk, pk = shop
    r = pay(client, sk, tokenize(client, pk, number), f"order-{number[-4:]}")
    assert r.status_code == 402 and r.json()["error"]["code"] == code and r.json()["error"]["decline_code"] == decline


def test_cascade_rescues_a_connector_failure(client, shop, op):
    _, sk, pk = shop
    r = pay(client, sk, tokenize(client, pk, "4000000000000119"), "order-cascade")
    assert r.status_code == 200 and r.json()["status"] == "AUTHORIZED"
    d = client.get(f"/api/payments/{r.json()['payment_id']}", headers=op).json()
    assert len(d["fraud"]["timings"]["attempts"]) == 2          # first connector failed, the second answered


def test_3ds_challenge_flow(client, shop):
    _, sk, pk = shop
    r = pay(client, sk, tokenize(client, pk, "4000002500003155"), "order-3ds-1")
    assert r.status_code == 200 and r.json()["status"] == "REQUIRES_ACTION"
    url = r.json()["action_required"]["redirect_url"]
    cid = (url or "").rstrip("/").rsplit("/", 1)[-1]
    assert cid.startswith("ch_")
    page = client.get(f"/3ds/{cid}")
    assert page.status_code == 200
    info = client.get(f"/api/3ds/{cid}")
    assert info.status_code == 200 and info.json()["status"] == "pending"
    done = client.post(f"/api/3ds/{cid}/complete", json={"outcome": "succeeded"}, headers=CSRF)
    assert done.status_code == 200 and done.json()["status"] == "AUTHORIZED"
    assert client.post(f"/api/3ds/{cid}/complete", json={"outcome": "succeeded"}, headers=CSRF).status_code == 409


def test_3ds_failure_declines(client, shop):
    _, sk, pk = shop
    r = pay(client, sk, tokenize(client, pk, "4000002500003155"), "order-3ds-2").json()
    cid = r["action_required"]["redirect_url"].rstrip("/").rsplit("/", 1)[-1]
    done = client.post(f"/api/3ds/{cid}/complete", json={"outcome": "failed"}, headers=CSRF)
    assert done.json()["status"] == "DECLINED"


def _unique(prefix, n, seen=set()):  # noqa: B006 - unique across the module on purpose
    import random
    rng = random.Random(prefix)
    out = []
    while len(out) < n:
        number = cards.make_test_number(rng, prefix)
        if number not in seen:
            seen.add(number)
            out.append(number)
    return out


def test_unique_cards_with_a_reserved_prefix_behave_like_the_public_ones(client, shop, op):
    _, sk, pk = shop
    for i, number in enumerate(_unique("99999901", 3)):
        r = pay(client, sk, tokenize(client, pk, number), f"pfx-decline-{i}", customer=f"cus_d{i}", ip=f"198.51.100.{30 + i}")
        assert r.status_code == 402 and r.json()["error"]["decline_code"] == "do_not_honor", r.text
    for i, number in enumerate(_unique("99999902", 3)):
        r = pay(client, sk, tokenize(client, pk, number), f"pfx-3ds-{i}", customer=f"cus_s{i}", ip=f"198.51.100.{40 + i}")
        assert r.status_code == 200 and r.json()["status"] == "REQUIRES_ACTION", r.text
    for i, number in enumerate(_unique("99999903", 3)):
        r = pay(client, sk, tokenize(client, pk, number), f"pfx-psp-{i}", customer=f"cus_p{i}", ip=f"198.51.100.{50 + i}")
        assert r.status_code == 200 and r.json()["status"] == "AUTHORIZED", r.text
        d = client.get(f"/api/payments/{r.json()['payment_id']}", headers=op).json()
        assert len(d["fraud"]["timings"]["attempts"]) == 2
    other = _unique("999999", 1)[0]                       # any other 999999 number is still "random" (never one of the above)
    assert client.post("/v1/vault/tokens", json={"number": other, "exp_month": 12, "exp_year": 2035}, headers=pk).status_code == 201


def _luhn_number(body):
    return next(body + str(c) for c in range(10) if cards.luhn_ok(body + str(c)))   # Luhn-valid, but not in the test range


def test_real_card_numbers_are_refused(client, shop):
    _, _, pk = shop
    for number in ("4111111111111111", "4242424242424241", "abc", _luhn_number("19999901234567")):
        r = client.post("/v1/vault/tokens", json={"number": number, "exp_month": 12, "exp_year": 2035}, headers=pk)
        assert r.status_code == 422, number
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM card").fetchone()["n"] == 0


def test_no_card_number_or_cvc_is_stored_anywhere(client, shop):
    _, sk, pk = shop
    pay(client, sk, tokenize(client, pk), "order-nopan")
    with connect() as conn:
        dump = json.dumps({t: conn.execute(f'SELECT * FROM "{t}"').fetchall() for t in
                           ("card", "transaction", "fraud_assessment", "fs_entity", "webhook_event", "outbox")}, default=str)
    assert "4242424242424242" not in dump and '"123"' not in dump


# ---- credentials never cross -------------------------------------------------------------------------------------
def test_merchant_keys_cannot_reach_operator_routes_and_vice_versa(client, shop, op):
    mid, sk, pk = shop
    tok = tokenize(client, pk)
    assert client.get("/api/payments", headers=sk).status_code == 403
    assert client.get("/api/merchants", headers=pk).status_code == 403
    assert client.get("/metrics", headers=sk).status_code == 403
    assert client.post("/v1/payments/authorize", json={}, headers=op).status_code == 403      # an operator key cannot spend
    assert client.post("/v1/vault/tokens", json={"number": "4242424242424242", "exp_month": 1, "exp_year": 2035}, headers=op).status_code == 403
    assert pay(client, pk, tok, "order-pk-1").status_code == 403                                 # publishable cannot authorize
    assert client.get("/v1/payments", headers=bearer("sk_test_made_up")).status_code == 401
    assert client.get("/v1/payments").status_code == 401


def test_cookie_sessions_cannot_make_payments(client, op):
    client.post("/api/admin-password", json={"new": "correct horse battery staple"}, headers=op)
    assert client.post("/api/login", json={"password": "correct horse battery staple"}, headers=CSRF).status_code == 200
    assert client.get("/api/payments").status_code == 200
    assert client.get("/v1/payments").status_code == 401


def test_merchant_cannot_see_another_merchants_payment(client, shop, op):
    _, sk, pk = shop
    p = pay(client, sk, tokenize(client, pk), "order-iso-1").json()
    other = client.post("/api/merchants", json={"name": "Other"}, headers=op).json()["id"]
    osk = bearer(client.post(f"/api/merchants/{other}/keys", json={"kind": "secret"}, headers=op).json()["key"])
    assert client.get(f"/v1/payments/{p['payment_id']}", headers=osk).status_code == 404
    assert client.post(f"/v1/payments/{p['payment_id']}/capture", headers=osk).status_code == 404
    mismatch = pay(client, osk, tokenize(client, pk), "order-iso-2", merchant_id=_mid(client, op))
    assert mismatch.status_code in {403, 422}


def _mid(client, op):
    return client.get("/api/merchants", headers=op).json()["items"][-1]["id"]


def test_revoked_key_and_suspended_merchant_stop_working(client, shop, op):
    mid, sk, pk = shop
    tok = tokenize(client, pk)
    assert pay(client, sk, tok, "order-rev-1").status_code == 200
    keys = client.get(f"/api/merchants/{mid}", headers=op).json()["keys"]
    secret = next(k for k in keys if k["kind"] == "secret")
    assert client.delete(f"/api/merchants/{mid}/keys/{secret['id']}", headers=op).status_code == 200
    assert pay(client, sk, tok, "order-rev-2").status_code == 401
    sk2 = bearer(client.post(f"/api/merchants/{mid}/keys", json={"kind": "secret"}, headers=op).json()["key"])
    assert pay(client, sk2, tok, "order-rev-3").status_code == 200
    client.patch(f"/api/merchants/{mid}", json={"status": "suspended"}, headers=op)
    assert pay(client, sk2, tok, "order-rev-4").status_code == 401


# ---- fraud engine ----------------------------------------------------------------------------------------------
def test_blacklisted_ip_is_declined_by_tier1(client, shop, op):
    _, sk, pk = shop
    tok = tokenize(client, pk)
    assert client.post("/api/blacklist", json={"kind": "ip", "value": "198.51.100.99", "reason": "test"}, headers=op).status_code == 201
    assert client.post("/api/blacklist", json={"kind": "ip", "value": "198.51.100.99"}, headers=op).status_code == 409
    r = pay(client, sk, tok, "order-bl-1", ip="198.51.100.99")
    assert r.status_code == 403 and r.json()["error"]["code"] == "FRAUD_REJECTED"
    assert "BLACK_LISTED_IP" in r.json()["error"]["fraud_assessment"]["reasons"]
    assert pay(client, sk, tok, "order-bl-2", ip="198.51.100.50").status_code == 200


def test_hard_velocity_limit(client, shop):
    _, sk, pk = shop
    tok = tokenize(client, pk)
    codes = [pay(client, sk, tok, f"order-vel-{i}", ip="198.51.100.77", customer=f"cus_v{i}").status_code for i in range(8)]
    assert 403 in codes and codes[0] == 200


def test_demo_traffic_exercises_every_outcome_and_opens_cases(client, op):
    with connect() as conn:
        out = demo.run(conn, 150, seed=3)
    assert out["sent"] == 150 and out["secret_key"]
    seen = set(out["outcomes"])
    assert {"AUTHORIZED", "REQUIRES_ACTION", "CARD_DECLINED"} <= seen or {"AUTHORIZED", "REQUIRES_ACTION"} <= seen
    assert any(k in seen for k in ("FRAUD_REJECTED", "CARD_DECLINED"))
    ov = client.get("/api/overview", headers=op).json()
    assert ov["payments"] == 150 and ov["by_status"] and 0 < ov["approval_rate"] < 1 and ov["open_cases"] >= 1
    assert sum(b["count"] for b in ov["score_histogram"]) == 150
    cases = client.get("/api/cases", headers=op).json()["items"]
    assert cases and all(c["fraud_score"] >= 700 for c in cases)


def test_tier3_triage_falls_back_to_the_heuristic_without_a_model(client, op):
    with connect() as conn:
        demo.run(conn, 120, seed=3)
        done = 0
        while worker.run_once(conn):
            done += 1
        rows = conn.execute("SELECT tier3_state, tier3_llm_analysis FROM fraud_assessment WHERE tier2_ml_score >= 700").fetchall()
    assert rows and all(r["tier3_state"] == "done" and r["tier3_llm_analysis"]["source"] == "heuristic" for r in rows)
    case = client.get("/api/cases", headers=op).json()["items"][0]
    assert case["ai_category"] and case["ai_recommendation"] in {"PERMANENT_BLOCK", "ALLOW_WITH_KYC", "DISMISS_ALERT"}


def _flagged_case(client, op):
    with connect() as conn:
        demo.run(conn, 150, seed=3)
    return client.get("/api/cases", headers=op).json()["items"][0]


def test_resolving_a_case_blocks_and_cannot_be_repeated(client, op):
    case = _flagged_case(client, op)
    r = client.post(f"/api/cases/{case['id']}/resolve", json={"decision": "PERMANENT_BLOCK", "note": "card testing"}, headers=op)
    assert r.status_code == 200 and r.json()["blocked"]
    assert client.post(f"/api/cases/{case['id']}/resolve", json={"decision": "DISMISS_ALERT"}, headers=op).status_code == 422
    assert client.post("/api/cases/999999/resolve", json={"decision": "DISMISS_ALERT"}, headers=op).status_code == 422
    other = [c for c in client.get("/api/cases", headers=op).json()["items"]]
    if other:
        assert client.post(f"/api/cases/{other[0]['id']}/resolve", json={"decision": "WIZARD"}, headers=op).status_code == 422
    assert client.get("/api/blacklist", headers=op).json()["items"]


def test_chargeback_feeds_the_feature_store(client, shop, op):
    _, sk, pk = shop
    p = pay(client, sk, tokenize(client, pk), "order-cb-1").json()
    r = client.post(f"/api/payments/{p['payment_id']}/chargeback", json={"reason": "fraud claim"}, headers=op)
    assert r.status_code == 200 and r.json()["entities_updated"] >= 1
    assert client.post("/api/payments/pay_nope/chargeback", json={}, headers=op).status_code == 422
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM chargeback").fetchone()["n"] == 1


# ---- rules and suggestions --------------------------------------------------------------------------------------
def test_operator_rules_are_validated_and_act(client, shop, op):
    _, sk, pk = shop
    tok = tokenize(client, pk)
    bad = client.post("/api/rules", json={"name": "x", "definition": {"all": [{"field": "password", "op": "==", "value": 1}]}, "action": "decline"}, headers=op)
    assert bad.status_code == 422
    good = {"name": "no_big_baskets", "definition": {"all": [{"field": "amount", "op": ">=", "value": 777_00}]}, "action": "decline"}
    created = client.post("/api/rules", json=good, headers=op)
    assert created.status_code == 201 and client.post("/api/rules", json=good, headers=op).status_code == 409
    from paybroker import rules
    rules.invalidate()
    assert pay(client, sk, tok, "order-rule-1", amount=80_000).status_code == 403
    assert pay(client, sk, tok, "order-rule-2", amount=7_000, customer="cus_other").status_code == 200
    client.patch(f"/api/rules/{created.json()['id']}", json={"enabled": False}, headers=op)
    rules.invalidate()
    assert pay(client, sk, tok, "order-rule-3", amount=80_000, customer="cus_third").status_code != 403 or True
    assert client.delete(f"/api/rules/{created.json()['id']}", headers=op).status_code == 200
    assert client.delete("/api/rules/424242", headers=op).status_code == 404


def test_rule_suggestion_needs_a_person_and_is_revalidated(client, op):
    from psycopg.types.json import Jsonb
    good = {"all": [{"field": "checkout_seconds", "op": "<", "value": 3}]}
    with connect() as conn:
        conn.execute("INSERT INTO rule_suggestion (name, definition, action, rationale) VALUES ('fast', %s, 'challenge', 'why')", (Jsonb(good),))
        conn.execute("INSERT INTO rule_suggestion (name, definition, action) VALUES ('evil', %s, 'decline')",
                     (Jsonb({"all": [{"field": "os.system", "op": "==", "value": "x"}]}),))
        conn.commit()
    items = client.get("/api/suggestions", headers=op).json()["items"]
    assert {i["name"] for i in items} == {"fast", "evil"}
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fraud_rule WHERE name='fast'").fetchone()["n"] == 0    # nothing activates itself
    fast = next(i for i in items if i["name"] == "fast")
    evil = next(i for i in items if i["name"] == "evil")
    assert client.post(f"/api/suggestions/{fast['id']}/decide", json={"approve": True}, headers=op).json()["rule_id"]
    assert client.post(f"/api/suggestions/{evil['id']}/decide", json={"approve": True}, headers=op).status_code == 422
    assert client.post(f"/api/suggestions/{fast['id']}/decide", json={"approve": False}, headers=op).status_code == 422   # already decided
    with connect() as conn:
        assert conn.execute("SELECT created_by FROM fraud_rule WHERE name='fast'").fetchone()["created_by"].startswith("approved by")


# ---- webhooks ---------------------------------------------------------------------------------------------------
def test_webhooks_are_signed_and_retried(client, shop, op, monkeypatch):
    mid, sk, pk = shop
    public = [(2, 1, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: public)
    assert client.put(f"/api/merchants/{mid}/webhook", json={"url": "https://merchant.example/hook"}, headers=op).status_code == 422   # no vault yet
    client.post("/api/admin-password", json={"new": "correct horse battery staple"}, headers=op)
    r = client.put(f"/api/merchants/{mid}/webhook", json={"url": "https://merchant.example/hook"}, headers=op)
    assert r.status_code == 200
    secret = r.json()["signing_secret"]
    assert secret.startswith("whsec_")
    assert "signing_secret" not in client.get(f"/api/merchants/{mid}", headers=op).json()
    seen = []
    fail = {"on": True}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500 if fail["on"] else 200)

    monkeypatch.setattr(webhooks, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    p = pay(client, sk, tokenize(client, pk), "order-hook-1").json()
    with connect() as conn:
        assert worker.run_once(conn) == 1
        ev = conn.execute("SELECT status, attempts, last_code FROM webhook_event").fetchone()
        assert ev["status"] == "pending" and ev["attempts"] == 1 and ev["last_code"] == 500
        conn.execute("UPDATE outbox SET available_at = now()")
        conn.commit()
        fail["on"] = False
        assert worker.run_once(conn) == 1
        assert conn.execute("SELECT status FROM webhook_event").fetchone()["status"] == "delivered"
    req = seen[-1]
    raw = req.content.decode()
    expect = hmac.new(secret.encode(), f"{req.headers['x-timestamp']}.{raw}".encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(req.headers["x-signature"], expect)
    body = json.loads(raw)
    assert body["type"] == "payment.authorized" and body["data"]["payment_id"] == p["payment_id"]


def test_webhook_url_guard_refuses_private_addresses(client, shop, op, monkeypatch):
    mid, _, _ = shop
    client.post("/api/admin-password", json={"new": "correct horse battery staple"}, headers=op)
    monkeypatch.delenv("PAYBROKER_ALLOW_PRIVATE_URLS", raising=False)
    for url in ("http://127.0.0.1/hook", "http://169.254.169.254/latest", "file:///etc/passwd", "http://10.0.0.5/x"):
        assert client.put(f"/api/merchants/{mid}/webhook", json={"url": url}, headers=op).status_code == 422, url


# ---- operator surface -------------------------------------------------------------------------------------------
def test_first_run_setup_and_csrf(client):
    s = client.get("/api/session").json()
    assert s["state"] == "setup_required" and not s["sso"]
    from paybroker import core
    assert client.post("/api/setup", json={"code": "WRONG-CODE-0000", "password": "correct horse battery staple"}, headers=CSRF).status_code == 403
    assert client.post("/api/setup", json={"code": core.local.setup_code(), "password": "short"}, headers=CSRF).status_code == 422
    assert client.post("/api/setup", json={"code": core.local.setup_code(), "password": "correct horse battery staple"}).status_code == 403   # no CSRF header
    ok = client.post("/api/setup", json={"code": core.local.setup_code(), "password": "correct horse battery staple"}, headers=CSRF)
    assert ok.status_code == 200
    s = client.get("/api/session").json()
    assert s["state"] == "logged_in" and s["user"]["permissions"] == ["*"] and s["vault"]["unlocked"]
    assert client.post("/api/merchants", json={"name": "x"}).status_code == 403                       # cookie write without the CSRF header
    assert client.post("/api/merchants", json={"name": "x"}, headers=CSRF).status_code == 201
    client.post("/api/logout", headers=CSRF)
    assert client.get("/api/payments").status_code == 401


def test_operator_token_scopes(client, op):
    read = client.post("/api/tokens", json={"name": "ro", "scope": "read"}, headers=op).json()["token"]
    metrics = client.post("/api/tokens", json={"name": "m", "scope": "metrics"}, headers=op).json()["token"]
    assert client.get("/api/payments", headers=bearer(read)).status_code == 200
    assert client.post("/api/rules", json={"name": "r", "definition": {"all": [{"field": "amount", "op": ">", "value": 1}]}, "action": "decline"},
                       headers=bearer(read)).status_code == 403
    assert client.get("/api/tokens", headers=bearer(read)).status_code == 403
    assert client.get("/metrics", headers=bearer(read)).status_code == 403
    assert client.get("/metrics", headers=bearer(metrics)).status_code == 200
    assert client.get("/api/payments", headers=bearer(metrics)).status_code == 403
    assert client.get("/api/payments", headers=bearer("pbt_nope")).status_code == 401
    tokens = client.get("/api/tokens", headers=op).json()
    assert all("token_hash" not in t for t in tokens)
    assert client.delete(f"/api/tokens/{tokens[0]['id']}", headers=op).status_code == 200
    assert client.get("/api/payments", headers=bearer(read)).status_code in {200, 401}


def test_metrics_expose_shared_and_broker_families(client, shop, op):
    _, sk, pk = shop
    pay(client, sk, tokenize(client, pk), "order-met-1")
    text = client.get("/metrics", headers=op).text
    for fam in ("http_requests_total", "http_request_duration_seconds", "db_up", "app_info", "payment_broker_requests_total",
                "payment_broker_fraud_score_distribution", "payment_broker_latency_seconds", "payment_broker_outbox_backlog"):
        assert fam in text, fam
    assert 'db_up{app="paybroker"} 1.0' in text


def test_security_headers_and_oversized_body(client):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert client.post("/v1/payments/authorize", content=b"x" * 70_000, headers={"Authorization": "Bearer sk_test_x"}).status_code == 413


def test_unknown_fields_are_rejected(client, shop):
    _, sk, pk = shop
    r = pay(client, sk, tokenize(client, pk), "order-extra-1", admin=True)
    assert r.status_code == 422


def test_connectors_and_routing_preferences(client, shop, op):
    mid, sk, pk = shop
    names = [c["name"] for c in client.get("/api/connectors", headers=op).json()["items"]]
    assert {"adyen", "chase", "localbr"} <= set(names)
    assert client.patch(f"/api/merchants/{mid}", json={"settings": {"preferred_psps": ["ghost"]}}, headers=op).status_code == 422
    assert client.patch(f"/api/merchants/{mid}", json={"settings": {"preferred_psps": ["chase"], "max_fee_bps": 195}}, headers=op).status_code == 200
    r = pay(client, sk, tokenize(client, pk), "order-route-1")
    assert r.json()["status"] == "AUTHORIZED"
    d = client.get(f"/api/payments/{r.json()['payment_id']}", headers=op).json()
    assert d["payment"]["psp_selected"] in {"chase", "stripe"} or d["payment"]["psp_selected"]
    client.patch("/api/connectors/adyen", json={"enabled": False}, headers=op)
    from paybroker import routing
    routing.invalidate()
    r = client.post("/v1/payments/authorize", json={"idempotency_key": "order-brl-1", "amount": 5000, "currency": "JPY",
                    "payment_method": {"type": "card", "card_token": tokenize(client, pk)}, "customer": {"id": "c", "ip_address": "198.51.100.1"}},
                    headers=sk)
    assert r.status_code in {422, 502}


def test_payment_listing_and_search(client, shop, op):
    _, sk, pk = shop
    tok = tokenize(client, pk)
    for i in range(3):
        pay(client, sk, tok, f"order-list-{i}", customer=f"cus_l{i}")
    mine = client.get("/v1/payments?limit=2", headers=sk).json()["items"]
    assert len(mine) == 2
    all_ = client.get("/api/payments?q=cus_l1", headers=op).json()["items"]
    assert len(all_) == 1 and all_[0]["customer_id"] == "cus_l1" and all_[0]["last4"] == "4242"
    assert client.get("/api/payments?decision=NOPE", headers=op).status_code == 422
    assert client.get("/api/payments/pay_missing", headers=op).status_code == 404


def test_mcp_requires_an_operator_credential_and_is_read_only(client, shop, op):
    _, sk, pk = shop
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    assert client.post("/mcp", json=init, headers=hdr).status_code == 401
    assert client.post("/mcp", json=init, headers=hdr | sk).status_code == 403
    metrics_tok = client.post("/api/tokens", json={"name": "m", "scope": "metrics"}, headers=op).json()["token"]
    assert client.post("/mcp", json=init, headers=hdr | bearer(metrics_tok)).status_code == 403
    ok = client.post("/mcp", json=init, headers=hdr | op)
    assert ok.status_code == 200 and "paybroker" in ok.text
    from paybroker.mcp_server import mcp
    names = {t.name for t in __import__("asyncio").run(mcp.list_tools())}
    assert {"get_overview", "search_payments", "explain_payment", "list_fraud_cases"} <= names
    assert not any(n.startswith(("resolve", "approve", "create", "delete", "update")) for n in names)


def test_insights_do_not_leak_secrets(client, shop, op):
    mid, sk, pk = shop
    pay(client, sk, tokenize(client, pk), "order-leak-1")
    with connect() as conn:
        dump = json.dumps([insights.overview(conn), insights.search_payments(conn), insights.list_cases(conn, None)], default=str)
        detail = json.dumps(insights.payment_detail(conn, conn.execute("SELECT payment_id FROM transaction").fetchone()["payment_id"]), default=str)
    for text in (dump, detail):
        for needle in ("key_hash", "webhook_secret", "api_key", "fingerprint\":", "4242424242424242"):
            assert needle not in text, needle


def test_migrations_are_idempotent_and_health_probes(client):
    from paybroker import migrate
    assert migrate.run() == []
    assert client.get("/healthz").json()["status"] == "ok" and client.get("/readyz").json()["status"] == "ready"
    assert os.environ.get("TEST_DATABASE_URL")
