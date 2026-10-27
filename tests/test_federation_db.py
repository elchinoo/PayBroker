"""PayBroker federated: people and services authenticate with the ecosystem identity provider; roles decide what they may do."""
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from apgkit.auth import Federation, RelyingParty, TokenVerifier
from joserfc import jwt
from joserfc.jwk import RSAKey

from tests.conftest import needs_db

pytestmark = needs_db

from paybroker import api, core  # noqa: E402

ISS, AUD = "https://id.example.org", "paybroker"
KEY = RSAKey.generate_key(2048, {"kid": "k1", "use": "sig", "alg": "RS256"})
CSRF = {"X-PayBroker-CSRF": "1"}


class Provider:
    def __init__(self):
        self.nonce = None
        self.id_claims = {"sub": "usr_1", "name": "Ada", "email": "ada@example.org", "roles": ["paybroker:analyst"]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISS, "jwks_uri": f"{ISS}/jwks", "authorization_endpoint": f"{ISS}/oauth/authorize",
                                             "token_endpoint": f"{ISS}/oauth/token", "end_session_endpoint": f"{ISS}/oauth/logout"})
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [KEY.as_dict(private=False)]})
        if path == "/oauth/token":
            claims = {"iss": ISS, "aud": AUD, "exp": int(time.time()) + 300, "nonce": self.nonce, **self.id_claims}
            return httpx.Response(200, json={"id_token": jwt.encode({"alg": "RS256", "kid": "k1"}, claims, KEY), "access_token": "x"})
        return httpx.Response(404)

    def http(self):
        return lambda: httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=False)


def access(**claims) -> dict:
    base = {"iss": ISS, "aud": AUD, "sub": "usr_1", "exp": int(time.time()) + 600, "sub_type": "user"}
    base.update(claims)
    return {"Authorization": "Bearer " + jwt.encode({"alg": "RS256", "kid": "k1", "typ": "at+jwt"}, base, KEY)}


@pytest.fixture(autouse=True)
def _federated(monkeypatch, client):
    provider = Provider()
    verifier = TokenVerifier(ISS, AUD, http=provider.http())
    monkeypatch.setattr(core.auth, "verifier", verifier)
    monkeypatch.setattr(api, "fed", Federation(ISS, AUD, verifier, RelyingParty(ISS, AUD, None, http=provider.http()), True))
    client.provider = provider
    yield


def test_roles_decide_what_a_person_may_do(client):
    admin, manager = access(sub="u1", roles=["paybroker:admin"]), access(sub="u2", roles=["paybroker:risk_manager"])
    analyst, operator = access(sub="u3", roles=["paybroker:analyst"]), access(sub="u4", roles=["paybroker:operator"])
    viewer, stranger = access(sub="u5", roles=["paybroker:viewer"]), access(sub="u6", roles=["otherapp:admin"])
    rule = {"name": "r1", "definition": {"all": [{"field": "amount", "op": ">", "value": 10}]}, "action": "challenge"}
    for who in (admin, manager, analyst, operator, viewer):
        assert client.get("/api/payments", headers=who).status_code == 200
    assert client.get("/api/payments", headers=stranger).status_code == 403                  # signed in, but no role in this app
    assert client.post("/api/rules", headers=analyst, json=rule).status_code == 403
    assert client.post("/api/rules", headers=viewer, json=rule).status_code == 403
    assert client.post("/api/rules", headers=manager, json=rule).status_code == 201
    assert client.post("/api/merchants", headers=manager, json={"name": "M"}).status_code == 403
    assert client.post("/api/merchants", headers=operator, json={"name": "M"}).status_code == 201
    assert client.post("/api/blacklist", headers=viewer, json={"kind": "ip", "value": "1.2.3.4"}).status_code == 403
    assert client.post("/api/blacklist", headers=analyst, json={"kind": "ip", "value": "1.2.3.4"}).status_code == 201
    assert client.get("/api/accounts", headers=manager).status_code == 403 and client.get("/api/accounts", headers=admin).status_code == 200
    assert client.post("/api/tokens", headers=operator, json={"name": "x", "scope": "full"}).status_code == 403
    assert client.get("/metrics", headers=admin).status_code == 200 and client.get("/metrics", headers=viewer).status_code == 403


def test_only_the_admin_role_may_reset_the_data(client):
    from paybroker.db import connect
    url, phrase = "/api/admin/data/reset", {"confirm": "RESET PAYBROKER DATA"}
    with connect() as conn:
        conn.execute("INSERT INTO outbox (topic, payload) VALUES ('webhook', '{}')")
        conn.commit()
    for role in ("operator", "risk_manager", "analyst", "viewer"):
        who = access(sub=f"u_{role}", roles=[f"paybroker:{role}"])
        assert client.post(url, headers=who, json=phrase).status_code == 403, role
        assert client.get(url, headers=who).status_code == 403, role
    assert client.post(url, headers=access(roles=["otherapp:admin"]), json=phrase).status_code == 403
    admin = access(sub="u_admin", name="Ada Admin", roles=["paybroker:admin"])
    assert client.post(url, headers=admin, json={"confirm": "nope"}).status_code == 422
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM outbox").fetchone()["n"] == 1
        conn.rollback()
    r = client.post(url, headers=admin, json=phrase)
    assert r.status_code == 200 and r.json()["deleted"]["outbox"] == 1
    assert client.get(url, headers=admin).json()["recent"][0]["actor"] == "Ada Admin"


def test_the_case_resolver_is_named_in_the_record(client):
    from paybroker import demo
    from paybroker.db import connect
    with connect() as conn:
        demo.run(conn, 150, seed=3)
    analyst, viewer = access(name="Grace Hopper", roles=["paybroker:analyst"]), access(sub="u2", roles=["paybroker:viewer"])
    case = client.get("/api/cases", headers=analyst).json()["items"][0]
    assert client.post(f"/api/cases/{case['id']}/resolve", headers=viewer, json={"decision": "DISMISS_ALERT"}).status_code == 403
    assert client.post(f"/api/cases/{case['id']}/resolve", headers=analyst, json={"decision": "DISMISS_ALERT"}).status_code == 200
    got = [c for c in client.get("/api/cases?status=resolved", headers=analyst).json()["items"] if c["id"] == case["id"]][0]
    assert got["resolved_by"] == "Grace Hopper"


def test_a_service_token_is_never_a_merchant_and_a_merchant_key_is_never_an_operator(client):
    svc = access(sub="dashboard-bot", sub_type="service", scope="paybroker.read", client_id="dashboard-bot")
    assert client.get("/api/payments", headers=svc).status_code == 200
    assert client.post("/api/rules", headers=svc, json={}).status_code == 403
    assert client.get("/metrics", headers=svc).status_code == 403
    assert client.post("/v1/payments/authorize", headers=svc, json={}).status_code == 403    # operators, human or service, cannot spend
    metrics = access(sub="prom", sub_type="service", scope="paybroker.metrics")
    assert client.get("/metrics", headers=metrics).status_code == 200 and client.get("/api/payments", headers=metrics).status_code == 403
    admin = access(roles=["paybroker:admin"])
    mid = client.post("/api/merchants", headers=admin, json={"name": "Shop"}).json()["id"]
    sk = {"Authorization": "Bearer " + client.post(f"/api/merchants/{mid}/keys", headers=admin, json={"kind": "secret"}).json()["key"]}
    assert client.get("/api/payments", headers=sk).status_code == 403


def test_tokens_for_another_app_or_with_a_bad_signature_are_refused(client):
    assert client.get("/api/payments", headers=access(aud="some-other-app", roles=["paybroker:admin"])).status_code == 401
    assert client.get("/api/payments", headers=access(exp=int(time.time()) - 3600, roles=["paybroker:admin"])).status_code == 401
    assert client.get("/api/payments", headers=access(iss="https://evil.example.org", roles=["paybroker:admin"])).status_code == 401
    other = RSAKey.generate_key(2048, {"kid": "k1", "use": "sig", "alg": "RS256"})
    forged = {"Authorization": "Bearer " + jwt.encode({"alg": "RS256", "kid": "k1"}, {"iss": ISS, "aud": AUD, "sub": "x", "exp": int(time.time()) + 60,
                                                                                      "roles": ["paybroker:admin"]}, other)}
    assert client.get("/api/payments", headers=forged).status_code == 401


def test_single_sign_on_login_creates_a_role_bound_session(client):
    r = client.get("/auth/login", params={"next": "/#cases"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"{ISS}/oauth/authorize?")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["http://testserver/auth/callback"] and q["client_id"] == [AUD]
    client.provider.nonce = q["nonce"][0]
    cb = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert cb.status_code == 303 and cb.headers["location"] == "/#cases" and "pb_session" in cb.cookies
    s = client.get("/api/session").json()
    assert s["state"] == "logged_in" and s["user"]["name"] == "Ada" and s["user"]["roles"] == ["paybroker:analyst"] and s["user"]["source"] == "oidc"
    assert "cases:review" in s["user"]["permissions"] and "rules:manage" not in s["user"]["permissions"]
    assert s["sso"]["login_url"] == "/auth/login"
    assert client.get("/api/payments").status_code == 200
    assert client.post("/api/rules", headers=CSRF, json={"name": "x", "definition": {"all": [{"field": "amount", "op": ">", "value": 1}]},
                                                       "action": "decline"}).status_code == 403
    out = client.post("/api/logout", headers=CSRF).json()
    assert out["logout_url"].startswith(f"{ISS}/oauth/logout?") and "id_token_hint=" in out["logout_url"]
    assert client.get("/api/payments").status_code == 401


def test_sso_refuses_people_without_a_role_here(client):
    client.provider.id_claims = {"sub": "usr_9", "name": "Nobody", "roles": ["otherapp:admin"]}
    r = client.get("/auth/login", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    client.provider.nonce = q["nonce"][0]
    cb = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert cb.headers["location"].startswith("/?sso_error=") and "pb_session" not in cb.cookies
    assert client.get("/api/payments").status_code == 401
    again = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert again.headers["location"].startswith("/?sso_error=")                                  # the state was single-use


def test_mcp_needs_the_mcp_permission(client):
    h = {"Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert client.post("/mcp", headers={**h, **access(roles=["paybroker:viewer"])}, json=body).status_code != 403
    assert client.post("/mcp", headers={**h, **access(sub="m", sub_type="service", scope="paybroker.metrics")}, json=body).status_code == 403
    assert client.post("/mcp", headers={**h, **access(sub="x", roles=["otherapp:admin"])}, json=body).status_code == 403


def test_sso_endpoints_are_404_when_federation_is_off(client, monkeypatch):
    monkeypatch.setattr(api, "fed", Federation("", AUD, None, None, True))
    assert client.get("/auth/login", follow_redirects=False).status_code == 404
    assert client.get("/api/session").json()["sso"] is None


# ---- provisioning from another service (ecoctl): the paybroker.admin scope ------------------------------------------------
def test_a_service_with_the_admin_scope_provisions_merchants_and_nothing_else(client, monkeypatch):
    from paybroker.db import connect
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    client.post("/api/admin-password", json={"new": "correct horse battery staple"}, headers=access(roles=["paybroker:admin"]))
    eco = access(sub="ecoctl", sub_type="service", scope="paybroker.admin", client_id="ecoctl", name="Ecosystem CLI")
    assert client.get("/api/merchants", headers=eco).json()["items"] == []
    r = client.post("/api/merchants", headers=eco, json={"name": "ShopFront"})
    assert r.status_code == 201
    mid = r.json()["id"]
    assert client.post("/api/merchants", headers=eco, json={"name": "ShopFront"}).status_code == 409           # find-or-create is safe
    found = client.get("/api/merchants", headers=eco, params={"name": "ShopFront"}).json()["items"]
    assert [m["id"] for m in found] == [mid]
    assert client.get("/api/merchants", headers=eco, params={"name": "Shop"}).json()["items"] == []            # exact, not a prefix
    assert client.get(f"/api/merchants/{mid}", headers=eco).json()["name"] == "ShopFront"
    r = client.patch(f"/api/merchants/{mid}", headers=eco, json={"allowed_origins": ["https://shop.example.com"]})
    assert r.status_code == 200 and r.json()["allowed_origins"] == ["https://shop.example.com"]
    r = client.put(f"/api/merchants/{mid}/webhook", headers=eco, json={"url": "https://shop.example.com/hook"})
    assert r.status_code == 200 and r.json()["signing_secret"].startswith("whsec_")
    secret_key = client.post(f"/api/merchants/{mid}/keys", headers=eco, json={"kind": "secret", "name": "shopfront"})
    assert secret_key.status_code == 201 and secret_key.json()["key"].startswith("sk_test_") and secret_key.json()["kind"] == "secret"
    pub = client.post(f"/api/merchants/{mid}/keys", headers=eco, json={"kind": "publishable", "name": "shopfront"}).json()
    assert pub["key"].startswith("pk_test_")
    listed = client.get(f"/api/merchants/{mid}", headers=eco).json()["keys"]
    assert {k["name"] for k in listed} == {"shopfront"} and {k["kind"] for k in listed} == {"secret", "publishable"}
    assert all(set(k) >= {"id", "kind", "name", "prefix", "revoked_at"} and "key" not in k and "key_hash" not in k for k in listed)
    shown = [secret_key.json()["key"], pub["key"], r.json()["signing_secret"]]
    assert not any(s in str(client.get(f"/api/merchants/{mid}", headers=eco).json()) for s in shown)
    assert client.delete(f"/api/merchants/{mid}/keys/{pub['id']}", headers=eco).json() == {"revoked": pub["id"]}
    assert client.delete(f"/api/merchants/{mid}/keys/{pub['id']}", headers=eco).status_code == 404
    # nothing beyond merchants: no reset, settings, model, rules, tokens, cases, training or metrics
    assert client.post("/api/admin/data/reset", headers=eco, json={"confirm": "RESET PAYBROKER DATA"}).status_code == 403
    assert client.get("/api/admin/data/reset", headers=eco).status_code == 403
    assert client.get("/api/accounts", headers=eco).status_code == 403
    assert client.get("/api/admin/triage/settings", headers=eco).status_code == 403
    assert client.get("/api/tokens", headers=eco).status_code == 403
    assert client.post("/api/rules", headers=eco, json={}).status_code == 403
    assert client.post("/api/blacklist", headers=eco, json={"kind": "ip", "value": "1.2.3.4"}).status_code == 403
    assert client.get("/metrics", headers=eco).status_code == 403
    assert client.post("/api/demo/traffic", headers=eco, json={}).status_code == 403
    for path in ("/api/admin/models", "/api/admin/training/settings", "/api/admin/training/stats", "/api/admin/training/export"):
        assert client.get(path, headers=eco).status_code == 403, path
    assert client.post("/api/admin/models/use-bundled", headers=eco).status_code == 403
    assert client.post("/api/admin/training/purge", headers=eco).status_code == 403
    assert client.post("/v1/payments/authorize", headers=eco, json={}).status_code == 403
    # the trail: who, what, never a secret
    with connect() as conn:
        rows = conn.execute("SELECT actor, action, detail FROM admin_audit ORDER BY id").fetchall()
        conn.rollback()
    mine = [(r["actor"], r["action"]) for r in rows if r["actor"] == "ecoctl"]
    assert mine == [("ecoctl", a) for a in ("merchant.create", "merchant.allowed_origins", "merchant.webhook", "merchant.key_create",
                                            "merchant.key_create", "merchant.key_revoke")]
    assert not any(s in str(rows) for s in shown)
    assert [r["detail"]["key_id"] for r in rows if r["action"] == "merchant.key_revoke"] == [pub["id"]]


def test_a_read_only_service_cannot_manage_merchants_and_a_person_cannot_use_the_admin_scope(client):
    ro = access(sub="dashboard-bot", sub_type="service", scope="paybroker.read", client_id="dashboard-bot")
    mid = client.post("/api/merchants", headers=access(roles=["paybroker:admin"]), json={"name": "Shop"}).json()["id"]
    assert client.get("/api/merchants", headers=ro).status_code == 200
    assert client.post("/api/merchants", headers=ro, json={"name": "X"}).status_code == 403
    assert client.patch(f"/api/merchants/{mid}", headers=ro, json={"allowed_origins": []}).status_code == 403
    assert client.put(f"/api/merchants/{mid}/webhook", headers=ro, json={"url": None}).status_code == 403
    assert client.post(f"/api/merchants/{mid}/keys", headers=ro, json={"kind": "secret"}).status_code == 403
    assert client.delete(f"/api/merchants/{mid}/keys/1", headers=ro).status_code == 403
    # the metrics scope and a stranger do not either
    assert client.post("/api/merchants", headers=access(sub="p", sub_type="service", scope="paybroker.metrics"), json={"name": "X"}).status_code == 403
    # a person's token that carries the scope gets only what their roles give
    person = access(sub="u9", sub_type="user", scope="paybroker.admin", roles=["paybroker:analyst"])
    assert client.get("/api/payments", headers=person).status_code == 200
    assert client.post("/api/merchants", headers=person, json={"name": "X"}).status_code == 403
    assert client.post("/api/merchants", headers=access(sub="u9", scope="paybroker.admin"), json={"name": "X"}).status_code == 403
    assert client.post("/api/merchants", headers=access(sub="u9", roles=["paybroker:operator"], scope="paybroker.admin"),
                       json={"name": "X"}).status_code == 201                                                  # the role still counts


def test_merchant_names_are_unique_for_new_merchants_and_ecoctl_scope_is_exactly_two_permissions(client):
    assert core.SCOPE_PERMISSIONS["paybroker.admin"] == frozenset({"merchants:manage", "payments:read"})
    admin = access(roles=["paybroker:admin"])
    assert client.post("/api/merchants", headers=admin, json={"name": " Shop "}).status_code == 201
    assert client.post("/api/merchants", headers=admin, json={"name": "Shop"}).status_code == 409
