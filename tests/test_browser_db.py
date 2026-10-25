"""A merchant's web shop calling PayBroker from the customer's browser: allowed origins, CORS on tokenization only, the 3-D Secure return."""
import hashlib
import hmac
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import needs_db

pytestmark = needs_db

from paybroker import vault  # noqa: E402
from paybroker.db import connect  # noqa: E402
from tests.test_db import CSRF, KEY, bearer, pay, tokenize  # noqa: E402

SHOP = "https://shop.example.com"
CARD = {"number": "4242424242424242", "exp_month": 12, "exp_year": 2035, "cvc": "123"}
TOKENS = "/v1/vault/tokens"


@pytest.fixture
def op(monkeypatch):
    monkeypatch.setenv("PAYBROKER_API_KEY", KEY)
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def shop(client, op):
    """A merchant with a secret and a publishable key. Returns (merchant_id, secret_headers, publishable_headers)."""
    mid = client.post("/api/merchants", json={"name": "Acme Shop"}, headers=op).json()["id"]
    sk = client.post(f"/api/merchants/{mid}/keys", json={"kind": "secret", "name": "server"}, headers=op).json()["key"]
    pk = client.post(f"/api/merchants/{mid}/keys", json={"kind": "publishable", "name": "browser"}, headers=op).json()["key"]
    return mid, bearer(sk), bearer(pk)


def cors_headers(r) -> dict:
    return {k: v for k, v in r.headers.items() if k.lower().startswith("access-control")}


def set_origins(client, op, mid, origins):
    r = client.patch(f"/api/merchants/{mid}", json={"allowed_origins": origins}, headers=op)
    assert r.status_code == 200, r.text
    return r.json()


def preflight(client, origin, path=TOKENS, method="POST"):
    return client.options(path, headers={"Origin": origin, "Access-Control-Request-Method": method,
                                         "Access-Control-Request-Headers": "authorization, content-type"})


# ---- storing and validating the list ------------------------------------------------------------------------------------
def test_allowed_origins_are_validated_stored_shown_and_audited(client, op):
    mid = client.post("/api/merchants", json={"name": "Origins", "allowed_origins": ["HTTPS://Shop.Example.com:443"]}, headers=op).json()["id"]
    assert client.get(f"/api/merchants/{mid}", headers=op).json()["allowed_origins"] == [SHOP]
    other = client.post("/api/merchants", json={"name": "Plain"}, headers=op).json()
    assert other["allowed_origins"] == []
    out = set_origins(client, op, mid, [SHOP, "http://localhost:3000", "http://127.0.0.1:5173", SHOP])
    assert out["allowed_origins"] == [SHOP, "http://localhost:3000", "http://127.0.0.1:5173"]
    for bad in (["*"], ["https://*.example.com"], ["https://shop.example.com/"], ["https://shop.example.com/a"], ["http://shop.example.com"],
                ["shop.example.com"], ["http://10.0.0.1"], [f"https://s{i}.example.com" for i in range(21)]):
        r = client.patch(f"/api/merchants/{mid}", json={"allowed_origins": bad}, headers=op)
        assert r.status_code == 422, bad
    assert client.patch(f"/api/merchants/{mid}", json={"allowed_origins": "https://shop.example.com"}, headers=op).status_code == 422
    assert client.post("/api/merchants", json={"name": "X", "allowed_origins": ["*"]}, headers=op).status_code == 422
    assert client.get(f"/api/merchants/{mid}", headers=op).json()["allowed_origins"] == out["allowed_origins"]   # a refused change changed nothing
    assert client.patch(f"/api/merchants/{mid}", json={"name": "Renamed"}, headers=op).json()["allowed_origins"] == out["allowed_origins"]
    set_origins(client, op, mid, [SHOP, "http://localhost:3000", "http://127.0.0.1:5173"])                         # same list: no new audit row
    set_origins(client, op, mid, [])
    with connect() as conn:
        rows = conn.execute("SELECT actor, detail FROM admin_audit WHERE action='merchant.allowed_origins' ORDER BY id").fetchall()
    assert [r["detail"]["to"] for r in rows] == [[SHOP], [SHOP, "http://localhost:3000", "http://127.0.0.1:5173"], []]
    assert rows[1]["detail"]["from"] == [SHOP] and rows[1]["detail"]["merchant_id"] == mid and rows[0]["actor"]
    read_only = client.post("/api/tokens", json={"name": "ro", "scope": "read"}, headers=op).json()["token"]
    assert client.patch(f"/api/merchants/{mid}", json={"allowed_origins": [SHOP]}, headers=bearer(read_only)).status_code == 403


# ---- CORS on the tokenization endpoint --------------------------------------------------------------------------------------
def test_a_browser_can_tokenize_from_an_allowed_origin_only(client, op, shop):
    mid, sk, pk = shop
    assert preflight(client, SHOP).status_code == 403 and not cors_headers(preflight(client, SHOP))     # nobody lists it yet
    set_origins(client, op, mid, [SHOP, "http://localhost:3000"])
    pre = preflight(client, SHOP)
    assert pre.status_code == 204 and pre.content == b""
    assert pre.headers["access-control-allow-origin"] == SHOP and pre.headers["vary"] == "Origin"
    assert pre.headers["access-control-allow-methods"] == "POST, OPTIONS" and pre.headers["access-control-max-age"] == "600"
    assert pre.headers["access-control-allow-headers"] == "Authorization, Content-Type, Idempotency-Key"
    assert "access-control-allow-credentials" not in pre.headers and pre.headers["access-control-allow-origin"] != "*"
    assert preflight(client, "http://localhost:3000").headers["access-control-allow-origin"] == "http://localhost:3000"
    assert preflight(client, "https://evil.example.com").status_code == 403
    assert not cors_headers(preflight(client, "https://evil.example.com"))
    assert not cors_headers(preflight(client, "https://shop.example.com:8443"))                          # exact: another port is another origin
    assert not cors_headers(preflight(client, "http://shop.example.com"))
    assert not cors_headers(preflight(client, SHOP, method="DELETE"))

    r = client.post(TOKENS, json=CARD, headers={**pk, "Origin": SHOP})
    assert r.status_code == 201 and r.json()["token"]
    assert r.headers["access-control-allow-origin"] == SHOP and r.headers["vary"] == "Origin" and "access-control-allow-credentials" not in r.headers
    bad_card = client.post(TOKENS, json={**CARD, "number": "4111111111111110"}, headers={**pk, "Origin": SHOP})
    assert bad_card.status_code == 422 and bad_card.headers["access-control-allow-origin"] == SHOP     # the shop's JS can read why
    assert client.post(TOKENS, json={"number": "x"}, headers={**pk, "Origin": SHOP}).headers["access-control-allow-origin"] == SHOP   # body validation too
    r = client.post(TOKENS, json=CARD, headers=pk)                                                       # a server call: no Origin, no CORS
    assert r.status_code == 201 and not cors_headers(r)
    assert not cors_headers(client.post(TOKENS, json=CARD, headers={"Origin": SHOP}))                   # no key: 401, and nothing to read it with
    assert client.post(TOKENS, json=CARD, headers={"Origin": SHOP}).status_code == 401


def test_the_real_request_is_checked_against_the_keys_own_merchant(client, op, shop):
    mid, sk, pk = shop
    other = client.post("/api/merchants", json={"name": "Other", "allowed_origins": ["https://other.example.com"]}, headers=op).json()["id"]
    set_origins(client, op, mid, [SHOP])
    assert preflight(client, "https://other.example.com").status_code == 204     # a preflight cannot tell whose key will follow
    r = client.post(TOKENS, json=CARD, headers={**pk, "Origin": "https://other.example.com"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "origin_not_allowed" and not cors_headers(r)
    r = client.post(TOKENS, json=CARD, headers={**pk, "Origin": "null"})
    assert r.status_code == 403 and not cors_headers(r)
    set_origins(client, op, mid, [])                                              # removing the origin applies at once
    r = client.post(TOKENS, json=CARD, headers={**pk, "Origin": SHOP})
    assert r.status_code == 403 and not cors_headers(r)
    assert client.post(TOKENS, json=CARD, headers=pk).status_code == 201          # server-side use is unaffected
    assert other != mid


def test_a_secret_key_is_refused_from_a_browser(client, op, shop):
    mid, sk, pk = shop
    set_origins(client, op, mid, [SHOP])
    r = client.post(TOKENS, json=CARD, headers={**sk, "Origin": SHOP})
    assert r.status_code == 403 and r.json()["error"]["code"] == "origin_not_allowed" and not cors_headers(r)
    assert client.post(TOKENS, json=CARD, headers=sk).status_code == 201


def test_a_suspended_merchants_origins_stop_working(client, op, shop):
    mid, sk, pk = shop
    set_origins(client, op, mid, [SHOP])
    assert preflight(client, SHOP).status_code == 204
    client.patch(f"/api/merchants/{mid}", json={"status": "suspended"}, headers=op)
    assert preflight(client, SHOP).status_code == 403
    assert client.post(TOKENS, json=CARD, headers={**pk, "Origin": SHOP}).status_code == 401


def test_secret_key_endpoints_never_get_cors_headers(client, op, shop):
    mid, sk, pk = shop
    set_origins(client, op, mid, [SHOP])
    token = tokenize(client, pk)
    origin = {"Origin": SHOP}
    r = pay(client, {**sk, **origin}, token, "order-cors-1")
    assert r.status_code == 200 and not cors_headers(r) and "vary" not in r.headers
    pid = r.json()["payment_id"]
    for method, path, kw in [("get", f"/v1/payments/{pid}", {}), ("get", "/v1/payments", {}), ("post", f"/v1/payments/{pid}/capture", {"json": {}}),
                             ("post", f"/v1/payments/{pid}/refund", {"json": {}}), ("post", f"/v1/payments/{pid}/void", {"json": {}}),
                             ("post", "/v1/payments/authorize", {"json": {}}), ("get", "/healthz", {}), ("get", "/api/session", {})]:
        r = getattr(client, method)(path, headers={**sk, **origin}, **kw)
        assert not cors_headers(r), (method, path)
        pre = client.options(path, headers={**origin, "Access-Control-Request-Method": method.upper()})
        assert not cors_headers(pre) and pre.status_code in {403, 404, 405}, (method, path, pre.status_code)
    assert not cors_headers(client.get(f"/api/merchants/{mid}", headers={**op, **origin}))
    assert client.options("/api/merchants", headers={**origin, "Access-Control-Request-Method": "PATCH"}).status_code in {403, 404, 405}


# ---- the 3-D Secure return ---------------------------------------------------------------------------------------------------
@pytest.fixture
def signing(client, op, shop, monkeypatch):
    """The shop with a webhook (so there is a signing secret) and the origin listed. Returns (merchant id, sk, pk, secret)."""
    mid, sk, pk = shop
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    client.post("/api/admin-password", json={"new": "correct horse battery staple"}, headers=op)
    secret = client.put(f"/api/merchants/{mid}/webhook", json={"url": "https://shop.example.com/hook"}, headers=op).json()["signing_secret"]
    set_origins(client, op, mid, [SHOP])
    return mid, sk, pk, secret


def challenge_id(r) -> str:
    return r.json()["action_required"]["redirect_url"].rsplit("/", 1)[-1]


def test_3ds_sends_the_customer_back_with_a_signed_status(client, signing):
    mid, sk, pk, secret = signing
    back = f"{SHOP}/checkout/return?order=1001"
    r = pay(client, sk, tokenize(client, pk, "4000002500003155"), "order-3ds-back-1", return_url=back)
    assert r.status_code == 200 and r.json()["status"] == "REQUIRES_ACTION"
    assert "return_url" not in r.json()
    with connect() as conn:
        assert conn.execute("SELECT return_url FROM three_ds_challenge").fetchone()["return_url"] == back
    done = client.post(f"/api/3ds/{challenge_id(r)}/complete", json={"outcome": "succeeded"}, headers=CSRF).json()
    assert done["status"] == "AUTHORIZED"
    link = urlsplit(done["return_url"])
    q = {k: v[0] for k, v in parse_qs(link.query).items()}
    assert f"{link.scheme}://{link.netloc}{link.path}" == f"{SHOP}/checkout/return" and q["order"] == "1001"
    assert q["payment_id"] == done["payment_id"] and q["status"] == "AUTHORIZED"
    expect = hmac.new(secret.encode(), f"{q['timestamp']}.payment_id={q['payment_id']}&status=AUTHORIZED".encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(q["signature"], expect)
    with connect() as conn:
        body = conn.execute("SELECT body FROM webhook_event ORDER BY created_at DESC LIMIT 1").fetchone()["body"]
    assert "return_url" not in str(body) and "signature" not in str(body)

    r = pay(client, sk, tokenize(client, pk, "4000002500003155"), "order-3ds-back-2", return_url=back)
    failed = client.post(f"/api/3ds/{challenge_id(r)}/complete", json={"outcome": "failed"}, headers=CSRF).json()
    assert failed["status"] == "DECLINED" and "status=DECLINED" in failed["return_url"]


def test_3ds_without_a_return_url_behaves_as_before(client, signing):
    mid, sk, pk, _ = signing
    r = pay(client, sk, tokenize(client, pk, "4000002500003155"), "order-3ds-plain")
    done = client.post(f"/api/3ds/{challenge_id(r)}/complete", json={"outcome": "succeeded"}, headers=CSRF).json()
    assert done["status"] == "AUTHORIZED" and "return_url" not in done


def test_a_return_url_must_be_on_an_allowed_origin_and_the_merchant_needs_a_webhook(client, op, shop, signing):
    mid, sk, pk, _ = signing
    token = tokenize(client, pk, "4000002500003155")
    for i, url in enumerate(["https://evil.example.com/steal", "http://shop.example.com/x", f"{SHOP}/x#frag", "javascript:alert(1)",
                             f"{SHOP}/x?signature=forged", "//shop.example.com/x"]):
        r = pay(client, sk, token, f"order-bad-{i}", return_url=url)
        assert r.status_code == 422 and r.json()["error"]["code"] == "RETURN_URL_NOT_ALLOWED", url
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM transaction").fetchone()["n"] == 0           # refused before anything was stored
    client.put(f"/api/merchants/{mid}/webhook", json={"url": None}, headers=op)
    r = pay(client, sk, token, "order-nohook", return_url=f"{SHOP}/x")
    assert r.status_code == 422 and r.json()["error"]["code"] == "RETURN_URL_NEEDS_WEBHOOK"


def test_no_redirect_is_made_when_the_secret_cannot_be_read(client, signing):
    mid, sk, pk, _ = signing
    r = pay(client, sk, tokenize(client, pk, "4000002500003155"), "order-locked", return_url=f"{SHOP}/back")
    vault.lock()
    done = client.post(f"/api/3ds/{challenge_id(r)}/complete", json={"outcome": "succeeded"}, headers=CSRF)
    assert done.status_code == 200 and "return_url" not in done.json()


# ---- upgrade safety ----------------------------------------------------------------------------------------------------------
def test_migration_005_applies_on_top_of_a_database_that_holds_merchants_and_challenges():
    """001 to 004 plus data, then 005: nothing is lost, existing merchants get an empty list, and re-running 005 is harmless."""
    from paybroker import migrate
    sql = {p.name: p.read_text() for p in sorted(Path(migrate.MIGRATIONS).glob("*.sql"))}
    with connect() as conn:
        conn.execute("DROP SCHEMA IF EXISTS mig_upgrade5 CASCADE")
        conn.execute("CREATE SCHEMA mig_upgrade5")
        conn.commit()
        conn.execute("SET search_path = mig_upgrade5, public")
        for name in ("001_init.sql", "002_training.sql", "003_admin_audit.sql", "004_triage_calls.sql"):
            conn.execute(sql[name])
        conn.execute("INSERT INTO merchant (id, name, webhook_url) VALUES ('mch_upgrade', 'Old Shop', 'https://old.example.com/hook')")
        tx = conn.execute("INSERT INTO transaction (payment_id, merchant_id, idempotency_key, request_hash, amount, currency, status) "
                          "VALUES ('pay_old','mch_upgrade','k','h',100,'USD','REQUIRES_ACTION') RETURNING id").fetchone()["id"]
        conn.execute("INSERT INTO three_ds_challenge (id, transaction_id, expires_at) VALUES ('ch_old', %s, now() + interval '1 hour')", (tx,))
        conn.execute(sql["005_browser_origins.sql"])
        conn.execute(sql["005_browser_origins.sql"])
        m = conn.execute("SELECT name, webhook_url, allowed_origins FROM merchant").fetchone()
        assert m == {"name": "Old Shop", "webhook_url": "https://old.example.com/hook", "allowed_origins": []}
        assert conn.execute("SELECT return_url, status FROM three_ds_challenge").fetchone() == {"return_url": None, "status": "pending"}
        conn.execute("UPDATE merchant SET allowed_origins = %s", (["https://shop.example.com"],))
        assert conn.execute("SELECT 1 FROM merchant WHERE %s = ANY(allowed_origins)", ("https://shop.example.com",)).fetchone()
        conn.rollback()
        conn.execute("SET search_path = public")
        conn.execute("DROP SCHEMA mig_upgrade5 CASCADE")
        conn.commit()
