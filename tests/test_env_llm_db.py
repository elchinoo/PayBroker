"""The environment LLM account ('env': LLM_BASE_URL, LLM_API_KEY[_FILE], LLM_CHAT_MODEL) next to stored accounts."""
import json
import logging

import httpx
import pytest

from tests.conftest import needs_db

pytestmark = needs_db

from paybroker import accounts, core, vault  # noqa: E402
from paybroker.db import connect  # noqa: E402
from tests.test_db import KEY  # noqa: E402

CSRF = {"X-PayBroker-CSRF": "1"}
GATEWAY_KEY = "gw-" + "secret" * 5  # fake placeholder  # pragma: allowlist secret
STORED_KEY = "sk-" + "stored" * 4  # fake placeholder  # pragma: allowlist secret
CALLS: list[dict] = []


def provider(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    CALLS.append({"host": request.url.host, "auth": request.headers.get("authorization"), "body": body})
    if body["model"] == "thinker":
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "reasoning_content": "hmm"}, "finish_reason": "length"}],
                                         "usage": {"completion_tokens": body["max_tokens"]}})
    if request.headers.get("authorization") not in {f"Bearer {GATEWAY_KEY}", f"Bearer {STORED_KEY}", None}:
        return httpx.Response(401, text="bad key " + str(request.headers.get("authorization")))
    return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}})


@pytest.fixture
def op(monkeypatch):
    monkeypatch.setenv("PAYBROKER_API_KEY", KEY)
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture(autouse=True)
def _llm(monkeypatch):
    for v in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_API_KEY_FILE", "LLM_CHAT_MODEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(core.llm, "_http", lambda: httpx.Client(transport=httpx.MockTransport(provider)))
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    CALLS.clear()


def gateway(monkeypatch, *, url="https://gateway.example/v1/", model="qwen3", key=GATEWAY_KEY):
    monkeypatch.setenv("LLM_BASE_URL", url)
    monkeypatch.setenv("LLM_CHAT_MODEL", model)
    if key:
        monkeypatch.setenv("LLM_API_KEY", key)


def stored(client, op, **over):
    body = {"name": "mine", "provider": "custom", "base_url": "https://llm.example/v1", "api_key": None, "chat_model": "m1"} | over
    return client.post("/api/accounts", json=body, headers=op)


def test_no_environment_means_no_env_account(client, op):
    assert client.get("/api/accounts", headers=op).json()["env"] is None
    assert client.post("/api/accounts/env/test", json={"target": "chat"}, headers=op).status_code == 422
    assert client.put("/api/triage-account", json={"account_id": "env"}, headers=op).status_code == 422
    assert client.get("/api/accounts", headers=op).json()["triage_account"] is None


def test_env_account_is_listed_read_only_and_the_key_is_never_returned(client, op, monkeypatch):
    gateway(monkeypatch)
    out = client.get("/api/accounts", headers=op)
    env = out.json()["env"]
    assert env["id"] == "env" and env["read_only"] and env["has_key"] and env["chat_model"] == "qwen3"
    assert env["base_url"] == "https://gateway.example/v1" and "error" not in env
    assert GATEWAY_KEY not in out.text and "api_key" not in env
    assert out.json()["items"] == []      # it is not a stored row


def test_env_account_is_never_writable_or_deletable(client, op, monkeypatch):
    gateway(monkeypatch)
    body = {"name": "x", "provider": "custom", "base_url": "https://llm.example/v1", "chat_model": "m"}
    assert client.put("/api/accounts/env", json=body, headers=op).status_code in (404, 405, 422)
    assert client.delete("/api/accounts/env", headers=op).status_code in (404, 405, 422)
    assert stored(client, op, name="ENV").status_code == 422          # the name is reserved
    sid = stored(client, op).json()["id"]
    assert client.put(f"/api/accounts/{sid}", json=body | {"name": "env"}, headers=op).status_code == 422
    assert client.get("/api/accounts", headers=op).json()["env"]["base_url"] == "https://gateway.example/v1"
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM llm_account").fetchone()["n"] == 1


def test_env_is_the_default_when_no_account_is_chosen_and_can_be_chosen_explicitly(client, op, monkeypatch):
    a = stored(client, op).json()["id"]
    b = stored(client, op, name="other").json()["id"]
    assert client.get("/api/accounts", headers=op).json()["triage_account"] is None       # two stored accounts, no choice
    gateway(monkeypatch)
    assert client.get("/api/accounts", headers=op).json()["triage_account"] == "env"
    assert client.put("/api/triage-account", json={"account_id": a}, headers=op).status_code == 200
    assert client.get("/api/accounts", headers=op).json()["triage_account"] == a        # an explicit choice wins over the default
    assert client.put("/api/triage-account", json={"account_id": "env"}, headers=op).json() == {"triage_account": "env"}
    assert client.get("/api/accounts", headers=op).json()["triage_account"] == "env"
    assert client.put("/api/triage-account", json={"account_id": None}, headers=op).status_code == 200
    assert client.get("/api/accounts", headers=op).json()["triage_account"] == "env"
    assert client.put("/api/triage-account", json={"account_id": b}, headers=op).status_code == 200
    monkeypatch.delenv("LLM_BASE_URL")                                                   # the choice of a vanished env falls back
    client.put("/api/triage-account", json={"account_id": a}, headers=op)
    with connect() as conn:
        accounts.store.put(conn, accounts.SETTING, "env")
        conn.commit()
        assert accounts.triage_account(conn) is None


def test_env_without_a_chat_model_or_with_a_bad_url_is_not_used(client, op, monkeypatch):
    gateway(monkeypatch, model="")
    assert client.get("/api/accounts", headers=op).json()["triage_account"] is None
    r = client.post("/api/accounts/env/test", json={"target": "chat"}, headers=op)
    assert r.status_code == 422 and "LLM_CHAT_MODEL" in r.json()["detail"]
    for url in ("http://gateway.example/v1", "https://user:pw@gateway.example/v1", "ftp://gateway.example", "https://gateway.example/v1?x=1"):
        gateway(monkeypatch, url=url)
        env = client.get("/api/accounts", headers=op).json()["env"]
        assert env["error"] and client.get("/api/accounts", headers=op).json()["triage_account"] is None
        assert client.post("/api/accounts/env/test", json={"target": "chat"}, headers=op).status_code == 422
    assert CALLS == []


def test_plain_http_is_accepted_for_localhost_only(client, op, monkeypatch):
    for url in ("http://localhost:4000/v1", "http://127.0.0.1:4000/v1", "http://[::1]:4000/v1"):
        gateway(monkeypatch, url=url)
        assert "error" not in client.get("/api/accounts", headers=op).json()["env"], url
    assert client.post("/api/accounts/env/test", json={"target": "chat"}, headers=op).status_code == 200


def test_env_account_test_makes_one_small_call_with_the_key_and_no_vault(client, op, monkeypatch):
    gateway(monkeypatch)
    r = client.post("/api/accounts/env/test", json={"target": "chat"}, headers=op)
    assert r.status_code == 200 and r.json()["reply"] == "OK"
    call = CALLS[-1]
    assert call["host"] == "gateway.example" and call["auth"] == f"Bearer {GATEWAY_KEY}"
    assert call["body"]["max_tokens"] == 64 and call["body"]["model"] == "qwen3"
    assert GATEWAY_KEY not in r.text


def test_env_key_can_come_from_a_file(client, op, monkeypatch, tmp_path):
    f = tmp_path / "llm_key"
    f.write_text(GATEWAY_KEY + "\n")
    gateway(monkeypatch, key="")
    monkeypatch.setenv("LLM_API_KEY_FILE", str(f))
    assert client.get("/api/accounts", headers=op).json()["env"]["has_key"] is True
    assert client.post("/api/accounts/env/test", json={"target": "chat"}, headers=op).status_code == 200
    assert CALLS[-1]["auth"] == f"Bearer {GATEWAY_KEY}"


def test_env_account_works_while_the_vault_is_locked_but_stored_keys_do_not(client, op, monkeypatch):
    client.post("/api/admin-password", json={"new": "correct horse battery staple"}, headers=op)
    sid = stored(client, op, api_key=STORED_KEY).json()["id"]
    gateway(monkeypatch)
    vault.lock()
    assert client.post("/api/accounts/env/test", json={"target": "chat"}, headers=op).status_code == 200
    r = client.post(f"/api/accounts/{sid}/test", json={"target": "chat"}, headers=op)
    assert r.status_code == 423 and "locked" in r.json()["detail"].lower()
    with connect() as conn:
        cfg = accounts.chat_config_for(conn, "env")
    assert cfg.trusted and cfg.api_key() == GATEWAY_KEY


def test_a_thinking_model_that_runs_out_of_tokens_gets_the_real_reason(client, op, monkeypatch):
    gateway(monkeypatch, model="thinker")
    r = client.post("/api/accounts/env/test", json={"target": "chat"}, headers=op)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "all 64 completion tokens" in detail and "finish_reason=length" in detail and "thinking phase" in detail
    assert "empty answer" not in detail and GATEWAY_KEY not in r.text


def test_the_secret_never_reaches_a_response_or_a_log(client, op, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    gateway(monkeypatch, key=GATEWAY_KEY + "-wrong")                    # the provider echoes whatever key it was given
    r = client.post("/api/accounts/env/test", json={"target": "chat"}, headers=op)
    assert r.status_code == 502 and "401" in r.json()["detail"] and GATEWAY_KEY not in r.text
    blob = r.text + client.get("/api/accounts", headers=op).text + caplog.text
    assert GATEWAY_KEY not in blob


def test_stored_accounts_are_unaffected(client, op, monkeypatch):
    client.post("/api/admin-password", json={"new": "correct horse battery staple"}, headers=op)
    sid = stored(client, op, api_key=STORED_KEY).json()["id"]
    assert client.get("/api/accounts", headers=op).json()["triage_account"] == sid       # the only usable account, no env
    r = client.post(f"/api/accounts/{sid}/test", json={"target": "chat"}, headers=op)
    assert r.status_code == 200 and CALLS[-1]["auth"] == f"Bearer {STORED_KEY}" and CALLS[-1]["body"]["max_tokens"] == 64
    gateway(monkeypatch)
    items = client.get("/api/accounts", headers=op).json()["items"]
    assert [i["id"] for i in items] == [sid] and items[0]["has_key"] and STORED_KEY not in json.dumps(items)
    assert client.put(f"/api/accounts/{sid}", json={"name": "renamed", "provider": "custom", "base_url": "https://llm.example/v1",
                                                    "chat_model": "m2"}, headers=op).status_code == 200
    assert client.delete(f"/api/accounts/{sid}", headers=op).status_code == 200


def test_triage_and_suggestions_use_the_env_account_by_default(client, op, monkeypatch):
    from paybroker import suggest

    gateway(monkeypatch)
    seen = []

    def fake(cfg, messages, max_tokens=700, temperature=0.1):
        seen.append(cfg)
        return {"text": "{}", "usage": None}

    monkeypatch.setattr(suggest, "chat", fake)
    with connect() as conn:
        suggest.run(conn)
    assert seen and seen[0].trusted and seen[0].label == "Environment"
