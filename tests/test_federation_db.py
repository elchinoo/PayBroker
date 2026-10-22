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
