# ruff: noqa: F811
"""Vault v2 (agentpg-kit 0.4): every ciphertext is bound to its row, old values are upgraded, the data key can be rotated, and
webhook delivery pins the connection to the address it validated. Needs a database (set TEST_DATABASE_URL)."""
import base64
import socket

import httpx
import pytest
from cryptography.fernet import Fernet

from paybroker import audit, cards, core, settings_store, vault, webhooks, worker
from paybroker.db import connect
from tests.conftest import needs_db
from tests.test_db import CSRF, op, pay, shop, tokenize  # noqa: F401  (fixtures)

pytestmark = needs_db
PW = "test-vault-password-0123"           # the password wrap made by the autouse fixture
PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 443))]


def _legacy(plain: str) -> str:
    """A token as kit 0.2.x wrote it: Fernet under the data key, no context."""
    return Fernet(base64.urlsafe_b64encode(core.vault._data_key)).encrypt(plain.encode()).decode()


def _account(client, op, monkeypatch, key="sk-account-key-0123456789"):
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: PUBLIC)
    r = client.post("/api/accounts", json={"name": "gw", "provider": "custom", "base_url": "https://llm.example/v1", "api_key": key,
                                                "chat_model": "m"}, headers=op)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _webhook(client, op, mid, monkeypatch):
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: PUBLIC)
    r = client.put(f"/api/merchants/{mid}/webhook", json={"url": "https://merchant.example/hook"}, headers=op)
    assert r.status_code == 200, r.text
    return r.json()["signing_secret"]


def _tokens(conn):
    return {
        "account": conn.execute("SELECT id, api_key_enc AS v FROM llm_account").fetchone(),
        "merchant": conn.execute("SELECT id, webhook_secret_enc AS v FROM merchant").fetchone(),
        "fp": settings_store.get(conn, "card_fingerprint_key_enc"),
    }


def test_new_values_are_v2_and_bound_to_their_row(client, shop, op, monkeypatch):
    mid, _, pk = shop
    aid = _account(client, op, monkeypatch)
    secret = _webhook(client, op, mid, monkeypatch)
    tokenize(client, pk)
    with connect() as conn:
        t = _tokens(conn)
    assert t["account"]["v"].startswith("v2$") and t["merchant"]["v"].startswith("v2$") and t["fp"].startswith("v2$")
    assert vault.decrypt(t["account"]["v"], vault.llm_key_context(aid)) == "sk-account-key-0123456789"
    assert vault.decrypt(t["merchant"]["v"], vault.webhook_secret_context(mid)) == secret
    assert vault.decrypt(t["fp"], vault.FINGERPRINT_KEY_CONTEXT)
    for token, wrong in ((t["account"]["v"], vault.llm_key_context(aid + 1)), (t["merchant"]["v"], vault.webhook_secret_context("other")),
                         (t["fp"], "app_setting.something_else"), (t["account"]["v"], "")):
        with pytest.raises(vault.VaultError):          # a ciphertext moved to another row or column does not open
            vault.decrypt(token, wrong)


def test_every_registered_store_has_a_context_and_every_encrypted_column_is_registered():
    stores = core.vault.stores()
    assert set(stores) == {"llm_account.api_key_enc", "merchant.webhook_secret_enc", "app_setting.card_fingerprint_key_enc"}
    assert all(s["context"] for s in stores.values())
    with connect() as conn:
        cols = {f'{r["table_name"]}.{r["column_name"]}' for r in conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public' AND column_name LIKE '%\\_enc'")}
    assert cols == {"llm_account.api_key_enc", "merchant.webhook_secret_enc"}        # app_setting rows are found by key, not column


def test_values_written_before_contexts_still_work_and_are_upgraded_after_unlock(client, shop, op, monkeypatch):
    mid, _, pk = shop
    aid = _account(client, op, monkeypatch)
    _webhook(client, op, mid, monkeypatch)
    tokenize(client, pk)
    with connect() as conn:
        fp = vault.decrypt(settings_store.get(conn, "card_fingerprint_key_enc"), vault.FINGERPRINT_KEY_CONTEXT)
        conn.execute("UPDATE llm_account SET api_key_enc=%s WHERE id=%s", (_legacy("old-account-key-0123456789"), aid))
        conn.execute("UPDATE merchant SET webhook_secret_enc=%s WHERE id=%s", (_legacy("whsec_old"), mid))
        settings_store.put(conn, "card_fingerprint_key_enc", _legacy(fp))
        conn.commit()
        t = _tokens(conn)
        assert not t["account"]["v"].startswith("v2$")
        # legacy tokens carry no context and still decrypt with the context a v2 caller passes
        assert vault.decrypt(t["account"]["v"], vault.llm_key_context(aid)) == "old-account-key-0123456789"
        assert cards._fingerprint_key(conn) == fp                         # read-through: the fingerprint row is upgraded
        conn.commit()
        assert settings_store.get(conn, "card_fingerprint_key_enc").startswith("v2$")
        assert vault.upgrade_legacy(conn) == 2                           # account key and webhook secret
        conn.commit()
        t = _tokens(conn)
        assert t["account"]["v"].startswith("v2$") and t["merchant"]["v"].startswith("v2$")
        assert vault.decrypt(t["account"]["v"], vault.llm_key_context(aid)) == "old-account-key-0123456789"
        assert vault.decrypt(t["merchant"]["v"], vault.webhook_secret_context(mid)) == "whsec_old"
        assert vault.upgrade_legacy(conn) == 0                           # idempotent


def test_signing_in_upgrades_old_values(client, shop, op, monkeypatch):
    mid, _, _ = shop
    client.post("/api/admin-password", json={"new": "correct horse battery staple"}, headers=op)
    _webhook(client, op, mid, monkeypatch)
    with connect() as conn:
        conn.execute("UPDATE merchant SET webhook_secret_enc=%s WHERE id=%s", (_legacy("whsec_old"), mid))
        conn.commit()
    vault.lock()
    assert client.post("/api/login", json={"password": "correct horse battery staple"}, headers=CSRF).status_code == 200
    with connect() as conn:
        assert _tokens(conn)["merchant"]["v"].startswith("v2$")


def test_rotating_the_data_key_reencrypts_everything_and_leaves_the_ledger_intact(client, shop, op, monkeypatch):
    mid, sk, pk = shop
    aid = _account(client, op, monkeypatch)
    secret = _webhook(client, op, mid, monkeypatch)
    tok = tokenize(client, pk)
    assert pay(client, sk, tok, "order-before").status_code == 200
    with connect() as conn:
        before = _tokens(conn)
        fp = vault.decrypt(before["fp"], vault.FINGERPRINT_KEY_CONTEXT)
        conn.execute("UPDATE llm_account SET api_key_enc=%s WHERE id=%s", (_legacy("old-account-key-0123456789"), aid))   # a legacy value too
        conn.commit()
        entries = audit.verify(conn)["entries"]
    assert client.post("/api/vault/rotate-key", json={"password": PW}, headers=op).status_code == 422              # no confirm flag
    assert client.post("/api/vault/rotate-key", json={"password": "wrong", "confirm": True}, headers=op).status_code == 403
    r = client.post("/api/vault/rotate-key", json={"password": PW, "confirm": True}, headers=op)
    assert r.status_code == 200, r.text
    assert r.json()["rotated"] == {"llm_account.api_key_enc": 1, "merchant.webhook_secret_enc": 1,
                                   "app_setting.card_fingerprint_key_enc": 1} and r.json()["restart_other_replicas"]
    with connect() as conn:
        after = _tokens(conn)
        assert after["account"]["v"] != before["account"]["v"] and after["account"]["v"].startswith("v2$")
        assert vault.decrypt(after["account"]["v"], vault.llm_key_context(aid)) == "old-account-key-0123456789"
        assert vault.decrypt(after["merchant"]["v"], vault.webhook_secret_context(mid)) == secret
        assert vault.decrypt(after["fp"], vault.FINGERPRINT_KEY_CONTEXT) == fp                  # fingerprints stay valid
        verdict = audit.verify(conn)
        assert verdict["intact"] and verdict["entries"] == entries + 1                          # one entry added, none touched
        last = audit.recent(conn, 1)[0]
        assert last["action"] == "vault.rotate_key" and last["payload"] == {"stores": r.json()["rotated"]}
        assert conn.execute("SELECT count(*) AS n FROM admin_audit WHERE action='vault.rotate_key'").fetchone()["n"] == 1
    # the old password wrap was rewritten too: lock, then unlock with the same password
    vault.lock()
    with connect() as conn:
        assert vault.unlock_with_password(conn, PW)
    assert pay(client, sk, tok, "order-after").status_code == 200
    # the ledger holds no secret or ciphertext: nothing it stores changed with the key
    with connect() as conn:
        dump = " ".join(str(r) for r in conn.execute("SELECT payload FROM audit_ledger").fetchall())
    assert secret not in dump and "v2$" not in dump and "whsec_" not in dump


def test_rotation_needs_a_permission_the_confirm_flag_and_an_unlocked_vault(client, shop, op):
    assert client.post("/api/vault/rotate-key", json={"password": PW, "confirm": True}, headers=CSRF).status_code == 401
    read = client.post("/api/tokens", json={"name": "ro", "scope": "read"}, headers=op).json()["token"]
    assert client.post("/api/vault/rotate-key", json={"password": PW, "confirm": True},
                       headers={"Authorization": f"Bearer {read}"}).status_code == 403
    vault.lock()
    assert client.post("/api/vault/rotate-key", json={"password": PW, "confirm": True}, headers=op).status_code == 409


def test_a_locked_vault_still_fails_closed_for_card_fingerprints(client, shop):
    _, _, pk = shop
    vault.lock()
    r = client.post("/v1/vault/tokens", json={"number": "4242424242424242", "exp_month": 12, "exp_year": 2035, "cvc": "123"}, headers=pk)
    assert r.status_code == 503 and r.json()["error"]["code"] == "VAULT_UNAVAILABLE"


# ---- webhook delivery: the connection is pinned to the validated address --------------------------------------------------
def _deliver(conn):
    conn.execute("UPDATE outbox SET available_at = now()")
    conn.commit()
    return worker.run_once(conn)


def test_delivery_connects_to_the_validated_address_with_the_merchants_host_and_sni(client, shop, op, monkeypatch):
    mid, sk, pk = shop
    _webhook(client, op, mid, monkeypatch)
    seen = []
    monkeypatch.setattr(webhooks, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: (seen.append(r), httpx.Response(200))[1])))
    assert pay(client, sk, tokenize(client, pk), "order-pin").status_code == 200
    with connect() as conn:
        assert _deliver(conn) == 1
        assert conn.execute("SELECT status FROM webhook_event").fetchone()["status"] == "delivered"
    req = seen[-1]
    assert req.url.host == "93.184.216.34" and req.headers["host"] == "merchant.example"          # literal IP, the merchant's Host header
    assert req.extensions["sni_hostname"] == "merchant.example" and req.url.path == "/hook"


def test_a_name_that_resolves_differently_after_configuration_is_refused_at_delivery(client, shop, op, monkeypatch):
    """DNS rebinding: the URL passed when it was saved; at delivery its name now points inside. Nothing is sent, the attempt is counted."""
    mid, sk, pk = shop
    _webhook(client, op, mid, monkeypatch)
    sent = []
    monkeypatch.setattr(webhooks, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: (sent.append(r), httpx.Response(200))[1])))
    assert pay(client, sk, tokenize(client, pk), "order-rebind").status_code == 200
    inside = [(10, 1, 6, "", ("::ffff:10.0.0.5", 443, 0, 0))]
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: inside)
    with connect() as conn:
        assert _deliver(conn) == 1
        ev = conn.execute("SELECT status, attempts, last_code FROM webhook_event").fetchone()
    assert not sent and ev["status"] == "pending" and ev["attempts"] == 1 and ev["last_code"] is None


IPV6_FORMS = ("::ffff:10.0.0.5", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "::10.0.0.5", "64:ff9b::a00:5", "64:ff9b::7f00:1",
              "2002:0a00:0005::1", "2002:7f00:0001::1", "2002:a9fe:a9fe::1", "2001:0:4136:e378:8000:63bf:f5ff:fffa", "fec0::1", "fe80::1",
              "fd00::1", "::1")


@pytest.mark.parametrize("address", IPV6_FORMS)
def test_webhook_urls_that_resolve_to_ipv6_forms_of_private_addresses_are_refused(client, shop, op, monkeypatch, address):
    mid, _, _ = shop
    monkeypatch.delenv("PAYBROKER_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: [(socket.AF_INET6, 1, 6, "", (address, 443, 0, 0))])
    r = client.put(f"/api/merchants/{mid}/webhook", json={"url": "https://sneaky.example/hook"}, headers=op)
    assert r.status_code == 422, address
    literal = f"https://[{address}]/hook"
    assert client.put(f"/api/merchants/{mid}/webhook", json={"url": literal}, headers=op).status_code == 422, literal
