# ruff: noqa: F811
"""Row MACs (agentpg-kit 0.4.2): a database WRITE without the vault data key cannot plant a merchant key, redirect a webhook, add a
connector, a rule or a blocklist entry. Enforced while the vault is unlocked, unchanged while it is locked. Needs a database."""
import pytest
from apgkit.ids import sha256

from paybroker import (
    accounts,
    auth,
    cli,
    core,
    merchants,
    routing,
    rowmac,
    rules,
    settings_store,
    training_data,
    triage_limits,
    vault,
)
from paybroker.db import connect
from tests.conftest import needs_db
from tests.test_db import op, pay, shop, tokenize  # noqa: F401  (fixtures and helpers)

pytestmark = needs_db
PW = "test-vault-password-0123"           # the password wrap made by the autouse fixture
PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 443))]


def _account(client, op, monkeypatch, name="gw", url="https://llm.example/v1") -> int:
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: PUBLIC)
    r = client.post("/api/accounts", json={"name": name, "provider": "custom", "base_url": url, "api_key": "sk-account-key-0123456789",
                                           "chat_model": "m"}, headers=op)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _forge_key(mid: str, raw: str = "sk_test_forged-by-a-database-write-0123456789") -> str:
    """What stolen database credentials can do: a merchant key row with every column valid, and no MAC."""
    with connect() as c:
        c.execute("INSERT INTO merchant_key (merchant_id, kind, name, prefix, key_hash) VALUES (%s,'secret','x',%s,%s)", (mid, raw[:12], sha256(raw)))
        c.commit()
    auth.forget_keys()
    return raw


def _tokenized(client, shop):
    mid, sk, pk = shop
    return pay(client, sk, tokenize(client, pk), "order-rm-0001")


# ---- rows the app writes verify -----------------------------------------------------------------------------------------
def test_rows_written_by_the_app_carry_a_valid_mac(client, shop, op, monkeypatch):
    mid, _, _ = shop
    client.post("/api/rules", json={"name": "big", "description": "", "action": "challenge",
                                    "definition": {"all": [{"field": "amount", "op": ">=", "value": 90000}]}}, headers=op)
    client.post("/api/blacklist", json={"kind": "ip", "value": "198.51.100.99"}, headers=op)
    _account(client, op, monkeypatch)
    assert client.put("/api/admin/triage/settings", json={"rpm": 7}, headers=op).status_code == 200
    assert client.put("/api/admin/training/settings", json={"capture": True}, headers=op).status_code == 200
    with connect() as c:
        for t in rowmac.TABLES:
            rows = c.execute(f"SELECT * FROM {t}" + (f" WHERE {rowmac._SCOPE[t]}" if t in rowmac._SCOPE else "")).fetchall()
            assert rows, t
            assert all(rowmac._good(t, r) for r in rows), t          # seeded connectors and rules too
        assert merchants.get(c, mid)["id"] == mid
        assert "mac" not in merchants.get(c, mid) and all("mac" not in r for r in merchants.listing(c))
    assert _tokenized(client, shop).status_code == 200


def test_every_edit_by_the_app_re_signs(client, shop, op):
    mid, sk, pk = shop
    r = client.patch(f"/api/merchants/{mid}", json={"allowed_origins": ["https://shop.example.com"], "settings": {"max_fee_bps": 500}}, headers=op)
    assert r.status_code == 200, r.text
    key_id = client.get(f"/api/merchants/{mid}", headers=op).json()["keys"][0]["id"]
    assert client.delete(f"/api/merchants/{mid}/keys/{key_id}", headers=op).status_code == 200
    rule = client.post("/api/rules", json={"name": "r1", "description": "", "action": "decline",
                                           "definition": {"all": [{"field": "amount", "op": ">=", "value": 90000}]}}, headers=op).json()["id"]
    assert client.patch(f"/api/rules/{rule}", json={"enabled": False}, headers=op).status_code == 200
    assert client.patch("/api/connectors/stripe", json={"enabled": False}, headers=op).status_code == 200
    with connect() as c:
        for t in rowmac.TABLES:
            scope = f" WHERE {rowmac._SCOPE[t]}" if t in rowmac._SCOPE else ""
            assert all(rowmac._good(t, r) for r in c.execute(f"SELECT * FROM {t}{scope}").fetchall()), t
    listed = client.get(f"/api/merchants/{mid}", headers=op).json()["keys"]
    assert all(k["verified"] for k in listed) and "mac" not in listed[0]


# ---- a forged row is ignored while unlocked, accepted while locked ---------------------------------------------------------
def test_a_planted_merchant_key_does_not_authenticate_while_the_vault_is_open(client, shop):
    mid, _, _ = shop
    raw = _forge_key(mid)
    assert client.get("/v1/payments", headers={"Authorization": f"Bearer {raw}"}).status_code == 401
    vault.lock()                                                   # nothing is enforced without the data key: behaviour unchanged
    auth.forget_keys()
    assert client.get("/v1/payments", headers={"Authorization": f"Bearer {raw}"}).status_code == 200


def test_a_planted_merchant_cannot_be_used(client, op):
    with connect() as c:
        c.execute("INSERT INTO merchant (id, name) VALUES ('mch_planted1', 'Planted')")
        c.execute("INSERT INTO merchant_key (merchant_id, kind, prefix, key_hash) VALUES ('mch_planted1','secret','sk_test_plan',%s)", (sha256("sk_test_planted-0123456789abcdef"),))
        c.commit()
        with pytest.raises(merchants.NotFound):
            merchants.get(c, "mch_planted1")
        assert all(m["id"] != "mch_planted1" for m in merchants.listing(c))
        c.rollback()
    assert client.get("/v1/payments", headers={"Authorization": "Bearer sk_test_planted-0123456789abcdef"}).status_code == 401
    assert client.get("/api/merchants/mch_planted1", headers=op).status_code == 404


def test_an_edited_authority_column_is_ignored(client, shop, op, monkeypatch):
    mid, sk, pk = shop
    with connect() as c:
        c.execute("UPDATE merchant SET webhook_url = 'https://attacker.example/steal' WHERE id=%s", (mid,))
        c.commit()
    forget = __import__("paybroker.api", fromlist=["forget_merchants"]).forget_merchants
    forget()
    assert client.get(f"/api/merchants/{mid}", headers=op).status_code == 404
    assert client.get("/v1/payments", headers=sk).status_code == 401           # the merchant itself no longer verifies
    with connect() as c:
        assert merchants.origin_listed(c, "https://shop.example.com") is False
        c.rollback()


def test_a_revoked_key_cannot_be_revived_by_a_write(client, shop, op):
    mid, sk, _ = shop
    with connect() as c:
        key_id = c.execute("SELECT id FROM merchant_key WHERE kind='secret'").fetchone()["id"]
    assert client.delete(f"/api/merchants/{mid}/keys/{key_id}", headers=op).status_code == 200
    with connect() as c:
        c.execute("UPDATE merchant_key SET revoked_at = NULL WHERE id=%s", (key_id,))
        c.commit()
    auth.forget_keys()
    assert client.get("/v1/payments", headers=sk).status_code == 401


def test_a_key_moved_to_another_merchant_is_ignored(client, shop, op):
    mid, sk, _ = shop
    other = client.post("/api/merchants", json={"name": "Other"}, headers=op).json()["id"]
    with connect() as c:
        c.execute("UPDATE merchant_key SET merchant_id=%s WHERE kind='secret'", (other,))
        c.commit()
    auth.forget_keys()
    assert client.get("/v1/payments", headers=sk).status_code == 401


def test_a_planted_connector_is_not_routed_to_and_a_forged_rule_or_block_is_ignored(client, shop):
    with connect() as c:
        c.execute("INSERT INTO psp_connector (name, label, currencies, regions, fee_bps, base_latency_ms, base_auth_rate, auth_ewma, latency_ewma) "
                  "VALUES ('evil','Evil','{USD}','{US}',0,1,1,1,1)")
        c.execute("INSERT INTO fraud_rule (name, definition, action) VALUES ('deny_all', %s, 'decline')",
                  ('{"all": [{"field": "amount", "op": ">=", "value": 1}]}',))
        c.execute("INSERT INTO blacklist (kind, value) VALUES ('ip', '198.51.100.20')")
        c.commit()
        routing.invalidate()
        rules.invalidate()
        assert "evil" not in {r["name"] for r in routing.connectors(c)}
        rl, black = rules._load(c)
        assert "deny_all" not in {r["name"] for r in rl} and ("ip", "198.51.100.20") not in black
    assert _tokenized(client, shop).status_code == 200                 # the forged block on this IP and the decline-all rule did nothing
    vault.lock()                                                       # locked: unchanged, so the same rows act
    routing.invalidate()
    rules.invalidate()
    with connect() as c:
        assert "evil" in {r["name"] for r in routing.connectors(c)}
        rl, black = rules._load(c)
        assert "deny_all" in {r["name"] for r in rl} and ("ip", "198.51.100.20") in black


def test_an_edited_connector_rule_or_block_is_ignored(client, shop):
    with connect() as c:
        c.execute("UPDATE psp_connector SET fee_bps = 0 WHERE name='stripe'")
        c.execute("UPDATE fraud_rule SET action='decline' WHERE name='new_account_high_amount'")
        c.commit()
        routing.invalidate()
        rules.invalidate()
        assert "stripe" not in {r["name"] for r in routing.connectors(c)}
        assert "new_account_high_amount" not in {r["name"] for r in rules._load(c)[0]}
        c.rollback()


def test_the_tamper_is_counted_and_listed_as_unverified(client, shop, op):
    with connect() as c:
        c.execute("INSERT INTO blacklist (kind, value) VALUES ('ip', '203.0.113.9')")
        c.commit()
    items = client.get("/api/blacklist", headers=op).json()["items"]
    assert [i["verified"] for i in items if i["value"] == "203.0.113.9"] == [False]
    assert 'payment_broker_tampered_rows_total{table="blacklist"}' in client.get("/metrics", headers=op).text


def test_the_built_in_seed_rows_are_signed_after_an_unlock_but_only_when_unchanged(client):
    with connect() as c:
        c.execute("UPDATE psp_connector SET mac = NULL")
        c.execute("UPDATE fraud_rule SET mac = NULL")
        c.execute("UPDATE psp_connector SET fee_bps = 1 WHERE name='adyen'")          # edited: never healed
        c.commit()
        routing.invalidate()
        rules.invalidate()
        names = {r["name"] for r in routing.connectors(c)}
        assert {"stripe", "chase", "localbr"} <= names and "adyen" not in names
        assert {r["name"] for r in rules._load(c)[0]} == {n for n, *_ in rules.DEFAULT_RULES}


# ---- adopt-rowmacs ---------------------------------------------------------------------------------------------------------
def test_adopt_signs_legacy_rows_and_trusts_what_is_there(client, shop, op):
    mid, sk, _ = shop
    with connect() as c:
        c.execute("UPDATE merchant SET mac = NULL")
        c.execute("UPDATE merchant_key SET mac = NULL")
        c.execute("INSERT INTO blacklist (kind, value) VALUES ('ip', '203.0.113.77')")
        c.commit()
    auth.forget_keys()
    assert client.get("/v1/payments", headers=sk).status_code == 401           # unsigned = ignored once the vault is in use
    with connect() as c:
        counts = rowmac.adopt(c)
    assert counts["merchant"] == 1 and counts["merchant_key"] == 2 and counts["blacklist"] == 1
    auth.forget_keys()
    from paybroker import api
    api.forget_merchants()
    assert client.get("/v1/payments", headers=sk).status_code == 200


def test_adopt_needs_an_open_vault(client):
    vault.lock()
    with connect() as c, pytest.raises(RuntimeError):
        rowmac.adopt(c)


def test_the_adopt_command_asks_for_the_password_and_signs(client, shop, op, monkeypatch, capsys):
    mid, sk, _ = shop
    assert client.post("/api/admin-password", json={"new": PW}, headers=op).status_code in (200, 204)
    with connect() as c:
        c.execute("UPDATE merchant SET mac = NULL")
        c.commit()
    vault.lock()
    monkeypatch.setattr("getpass.getpass", lambda *_: PW)
    assert cli.main(["adopt-rowmacs"]) == 0
    assert "merchant 1" in capsys.readouterr().out
    with connect() as c:
        assert rowmac._good("merchant", c.execute("SELECT * FROM merchant").fetchone())
    monkeypatch.setattr("getpass.getpass", lambda *_: "wrong password")
    vault.lock()
    assert cli.main(["adopt-rowmacs"]) == 1


def test_setting_the_first_admin_password_signs_what_a_vaultless_install_already_has(client, shop, op):
    # a fresh install: merchant created before any password exists (no vault), then the admin sets one
    with connect() as c:
        c.execute("UPDATE merchant SET mac = NULL")
        c.commit()
    vault.lock()
    with connect() as c:
        core.vault.reset(c)
        c.commit()
    assert client.post("/api/admin-password", json={"new": PW}, headers=op).status_code in (200, 204)
    with connect() as c:
        assert rowmac._good("merchant", c.execute("SELECT * FROM merchant").fetchone())


# ---- rotation --------------------------------------------------------------------------------------------------------------
def test_a_data_key_rotation_re_signs_valid_rows_and_leaves_forged_ones_unsigned(client, shop, op):
    mid, sk, _ = shop
    raw = _forge_key(mid)
    with connect() as c:
        rowmac.rotate_data_key(c, password=PW)
    auth.forget_keys()
    with connect() as c:
        rows = {r["key_hash"] == sha256(raw): rowmac._good("merchant_key", r) for r in c.execute("SELECT * FROM merchant_key").fetchall()}
        assert rows == {False: True, True: False}                       # the app's keys verify under the new key, the forged one still does not
        assert all(rowmac._good(t, r) for t in ("merchant", "psp_connector", "fraud_rule") for r in c.execute(f"SELECT * FROM {t}").fetchall())
    assert client.get("/v1/payments", headers=sk).status_code == 200
    assert client.get("/v1/payments", headers={"Authorization": f"Bearer {raw}"}).status_code == 401


def test_a_rotation_that_the_vault_refuses_changes_nothing(client, shop):
    with connect() as c:
        before = c.execute("SELECT mac FROM merchant").fetchone()["mac"]
        with pytest.raises(vault.VaultError):
            rowmac.rotate_data_key(c, password="not the password")
    with connect() as c:
        assert c.execute("SELECT mac FROM merchant").fetchone()["mac"] == before


# ---- sealed boot -----------------------------------------------------------------------------------------------------------
@pytest.mark.no_vault
def test_a_sealed_first_start_does_not_sign_existing_rows(client, shop):
    """Sealed boot creates the vault at the first unseal: rows already there stay unsigned (a database whose vault records were wiped
    cannot launder planted rows); `adopt-rowmacs` is the deliberate step."""
    from apgkit.sealed import Protected
    with connect() as c:
        c.execute("INSERT INTO blacklist (kind, value) VALUES ('ip', '203.0.113.50')")
        c.commit()
        assert core.vault.unlock_sealed(c, Protected("sealed-passphrase-0123456789")) == "initialized"
        c.commit()
        row = c.execute("SELECT * FROM blacklist WHERE value='203.0.113.50'").fetchone()
        assert row["mac"] is None and rowmac.verify_row("blacklist", row) is False
        rowmac.adopt(c)
        assert rowmac.verify_row("blacklist", c.execute("SELECT * FROM blacklist WHERE value='203.0.113.50'").fetchone())


# ---- LLM accounts, behaviour settings and the webhook secret -----------------------------------------------------------------
def test_a_redirected_llm_account_is_not_used_while_unlocked_and_is_used_while_locked(client, op, monkeypatch):
    aid = _account(client, op, monkeypatch)
    with connect() as c:
        assert accounts.chat_config_for(c, aid).base_url == "https://llm.example/v1"
        c.execute("UPDATE llm_account SET base_url='https://attacker.example/v1' WHERE id=%s", (aid,))
        c.commit()
        with pytest.raises(accounts.AccountError):
            accounts.chat_config_for(c, aid)                          # treated as missing
        assert accounts.triage_account(c) is None
        assert accounts.list_accounts(c)[0]["verified"] is False and "mac" not in accounts.list_accounts(c)[0]
        c.rollback()
    assert 'payment_broker_tampered_rows_total{table="llm_account"}' in client.get("/metrics", headers=op).text
    assert client.get("/api/accounts", headers=op).json()["items"][0]["verified"] is False
    vault.lock()                                                      # locked: nothing enforced, unchanged behaviour
    with connect() as c:
        assert accounts.chat_config_for(c, aid).base_url == "https://attacker.example/v1"
        c.rollback()


@pytest.mark.parametrize("column,value", [("provider", "openai"), ("chat_model", "other-model"), ("api_key_enc", None)])
def test_any_edited_signed_account_column_is_ignored(client, op, monkeypatch, column, value):
    aid = _account(client, op, monkeypatch)
    with connect() as c:
        c.execute(f"UPDATE llm_account SET {column}=%s WHERE id=%s", (value, aid))        # noqa: S608
        c.commit()
        with pytest.raises(accounts.AccountError):
            accounts.chat_config_for(c, aid)
        c.rollback()


def test_a_planted_llm_account_is_ignored_and_a_swapped_one_too(client, op, monkeypatch):
    a, b = _account(client, op, monkeypatch, "one"), _account(client, op, monkeypatch, "two", "https://other.example/v1")
    with connect() as c:
        c.execute("INSERT INTO llm_account (name, provider, base_url, chat_model) VALUES ('planted','custom','https://attacker.example/v1','m')")
        planted = c.execute("SELECT id FROM llm_account WHERE name='planted'").fetchone()["id"]
        c.execute("UPDATE llm_account SET mac = (SELECT mac FROM llm_account WHERE id=%s) WHERE id=%s", (a, b))      # a MAC copied from another row
        c.commit()
        with pytest.raises(accounts.AccountError):
            accounts.chat_config_for(c, planted)
        with pytest.raises(accounts.AccountError):
            accounts.chat_config_for(c, b)
        assert accounts.chat_config_for(c, a).label == "one"
        c.rollback()


def test_editing_an_unverified_account_does_not_legitimise_it(client, op, monkeypatch):
    aid = _account(client, op, monkeypatch)
    with connect() as c:
        c.execute("UPDATE llm_account SET base_url='https://attacker.example/v1' WHERE id=%s", (aid,))
        c.commit()
    r = client.put(f"/api/accounts/{aid}", json={"name": "gw", "provider": "custom", "base_url": "https://llm2.example/v1", "chat_model": "m"}, headers=op)
    assert r.status_code == 200
    with connect() as c:
        assert rowmac._good("llm_account", c.execute("SELECT * FROM llm_account WHERE id=%s", (aid,)).fetchone()) is False
        c.rollback()


def test_a_cleared_or_replaced_webhook_secret_makes_the_merchant_unverified(client, shop, op, monkeypatch):
    mid, sk, _ = shop
    monkeypatch.setattr("paybroker.netguard.socket.getaddrinfo", lambda *a, **k: PUBLIC)
    assert client.put(f"/api/merchants/{mid}/webhook", json={"url": "https://merchant.example/hook"}, headers=op).status_code == 200
    with connect() as c:
        assert rowmac._good("merchant", c.execute("SELECT * FROM merchant").fetchone())
        c.execute("UPDATE merchant SET webhook_secret_enc = NULL WHERE id=%s", (mid,))
        c.commit()
        assert rowmac._good("merchant", c.execute("SELECT * FROM merchant").fetchone()) is False
        c.rollback()


def test_a_forged_edited_or_swapped_setting_is_ignored_while_unlocked_and_accepted_while_locked(client, op):
    assert client.put("/api/admin/triage/settings", json={"rpm": 33, "daily_cap": 44}, headers=op).status_code == 200
    with connect() as c:
        assert triage_limits.limits(c).rpm == 33
        c.execute("UPDATE app_setting SET value='0' WHERE key='triage_rpm'")                  # edited
        c.execute("UPDATE app_setting SET mac = (SELECT mac FROM app_setting WHERE key='triage_daily_cap') WHERE key='triage_group_minutes'")
        c.execute("INSERT INTO app_setting (key, value) VALUES ('triage_pause_minutes', '1440')")                      # planted
        c.execute("INSERT INTO app_setting (key, value) VALUES ('training_capture', '1')")
        c.commit()
        lim = triage_limits.limits(c)
        assert (lim.rpm, lim.pause_minutes) == (20, 10)                                       # the defaults, as if never set
        assert lim.daily_cap == 44                                                            # the untouched one still counts
        assert training_data.settings(c, fresh=True)["capture"] is False
        assert settings_store.get(c, "triage_rpm") is None
        c.rollback()
    assert 'payment_broker_tampered_rows_total{table="app_setting"}' in client.get("/metrics", headers=op).text
    vault.lock()
    with connect() as c:
        assert triage_limits.limits(c).rpm == 0 and triage_limits.limits(c).pause_minutes == 1440
        assert training_data.settings(c, fresh=True)["capture"] is True
        c.rollback()


def test_a_forged_triage_account_choice_is_ignored(client, op, monkeypatch):
    a = _account(client, op, monkeypatch, "one")
    _account(client, op, monkeypatch, "two")
    with connect() as c:
        c.execute("INSERT INTO app_setting (key, value) VALUES ('triage_account', %s)", (str(a),))
        c.commit()
        assert accounts.triage_account(c) is None             # two accounts and no signed choice
        accounts.set_triage_account(c, a)
        assert accounts.triage_account(c) == a
        c.rollback()


def test_settings_outside_the_authority_set_are_left_alone(client, op):
    with connect() as c:
        settings_store.put(c, "last_rule_synthesis", "123")
        c.commit()
        assert c.execute("SELECT mac FROM app_setting WHERE key='last_rule_synthesis'").fetchone()["mac"] is None
        assert settings_store.get(c, "last_rule_synthesis") == "123"
        assert c.execute("SELECT count(*) AS n FROM app_setting WHERE mac IS NOT NULL AND key <> ALL(%s)",
                         (sorted(rowmac.AUTHORITY_KEYS),)).fetchone()["n"] == 0
        c.rollback()


def test_adopt_signs_unsigned_accounts_and_authority_settings_only(client, op, monkeypatch):
    aid = _account(client, op, monkeypatch)
    client.put("/api/admin/triage/settings", json={"rpm": 5}, headers=op)
    with connect() as c:
        c.execute("UPDATE llm_account SET mac = NULL")
        c.execute("UPDATE app_setting SET mac = NULL")
        c.commit()
        with pytest.raises(accounts.AccountError):
            accounts.chat_config_for(c, aid)
        counts = rowmac.adopt(c)
        assert counts["llm_account"] == 1 and counts["app_setting"] == 1             # the one authority key; the kit's rows are not signed
        assert accounts.chat_config_for(c, aid).base_url == "https://llm.example/v1" and triage_limits.limits(c).rpm == 5
        assert c.execute("SELECT count(*) AS n FROM app_setting WHERE mac IS NOT NULL").fetchone()["n"] == 1


def test_adopt_upgrades_merchants_signed_before_the_webhook_secret_was_covered(client, shop, op):
    mid, sk, _ = shop
    from paybroker import api
    with connect() as c:
        row = c.execute("SELECT * FROM merchant WHERE id=%s", (mid,)).fetchone()
        old = core.vault.row_mac("merchant", *(rowmac._canon(row[col], k) for col, k in rowmac._LEGACY_FIELDS["merchant"]))
        c.execute("UPDATE merchant SET mac=%s WHERE id=%s", (old, mid))
        c.commit()
        assert rowmac._good("merchant", c.execute("SELECT * FROM merchant").fetchone()) is False
        c.execute("UPDATE merchant SET status='suspended' WHERE id=%s", (mid,))          # an edited row under the old MAC stays unverified
        c.commit()
        rowmac.adopt(c)
        assert rowmac._good("merchant", c.execute("SELECT * FROM merchant").fetchone()) is False
        c.execute("UPDATE merchant SET status='active', mac=%s WHERE id=%s", (old, mid))
        c.commit()
        rowmac.adopt(c)
        assert rowmac._good("merchant", c.execute("SELECT * FROM merchant").fetchone())
    auth.forget_keys()
    api.forget_merchants()
    assert client.get("/v1/payments", headers=sk).status_code == 200


def test_a_rotation_re_signs_accounts_and_settings(client, op, monkeypatch):
    aid = _account(client, op, monkeypatch)
    client.put("/api/admin/triage/settings", json={"rpm": 9}, headers=op)
    with connect() as c:
        c.execute("INSERT INTO app_setting (key, value) VALUES ('training_max_rows', '99999')")      # forged: stays unsigned
        c.commit()
        rowmac.rotate_data_key(c, password=PW)
        assert accounts.chat_config_for(c, aid).api_key() == "sk-account-key-0123456789"
        assert triage_limits.limits(c).rpm == 9
        assert settings_store.get(c, "training_max_rows") is None
        c.rollback()
