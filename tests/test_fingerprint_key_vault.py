# ruff: noqa: F811
"""The card-fingerprint key lives in the database only encrypted by the vault; tokenizing fails closed while the vault is locked."""
import pytest

from paybroker import cards, settings_store, vault
from paybroker.db import connect
from paybroker.ids import hmac_hex
from tests.conftest import needs_db
from tests.test_db import op, shop  # noqa: F401  (fixtures)

pytestmark = needs_db
PAN = "4242424242424242"


def test_key_is_created_encrypted_and_never_plaintext(client):
    with connect() as conn:
        key = cards._fingerprint_key(conn)
        conn.commit()
        assert settings_store.get(conn, "card_fingerprint_key") is None
        enc = settings_store.get(conn, "card_fingerprint_key_enc")
        assert enc and key not in enc and vault.decrypt(enc, vault.FINGERPRINT_KEY_CONTEXT) == key
        assert cards._fingerprint_key(conn) == key                      # stable


def test_plaintext_key_from_an_earlier_version_is_migrated_and_fingerprints_stay_valid(client, shop):
    mid, _, _pk = shop
    with connect() as conn:
        settings_store.put(conn, "card_fingerprint_key", "legacy-fingerprint-key-0123456789")
        conn.commit()
        row = cards.tokenize(conn, mid, PAN, 12, 2035, "123")
        assert settings_store.get(conn, "card_fingerprint_key") is None             # the plaintext row is gone
        assert vault.decrypt(settings_store.get(conn, "card_fingerprint_key_enc"), vault.FINGERPRINT_KEY_CONTEXT) == "legacy-fingerprint-key-0123456789"
        assert row["fingerprint"] == hmac_hex("legacy-fingerprint-key-0123456789", PAN)[:16]


def test_locked_vault_fails_closed_and_stores_no_card(client, shop):
    _, _, pk = shop
    vault.lock()
    r = client.post("/v1/vault/tokens", json={"number": PAN, "exp_month": 12, "exp_year": 2035, "cvc": "123"},
                    headers=pk)
    assert r.status_code == 503 and r.json()["error"]["code"] == "VAULT_UNAVAILABLE"
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM card").fetchone()["n"] == 0
        assert settings_store.get(conn, "card_fingerprint_key") is None and settings_store.get(conn, "card_fingerprint_key_enc") is None
        with pytest.raises(vault.VaultLocked):
            cards._fingerprint_key(conn)


def test_unreadable_stored_key_fails_closed(client):
    with connect() as conn:
        settings_store.put(conn, "card_fingerprint_key_enc", "not-a-valid-token")
        conn.commit()
        with pytest.raises(vault.VaultError):
            cards._fingerprint_key(conn)
