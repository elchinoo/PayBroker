"""Envelope encryption for stored provider keys. The implementation lives in agentpg-kit; this module keeps the old function API
(`vault.encrypt(...)`) bound to this app's single vault."""
from apgkit.vault import VaultError, VaultLocked, is_legacy_token  # noqa: F401

from .core import vault as _v

is_initialized = _v.is_initialized
is_unlocked = _v.is_unlocked
lock = _v.lock
status = _v.status
initialize = _v.initialize
unlock_with_password = _v.unlock_with_password
try_unlock_with_platform_secret = _v.try_unlock_with_platform_secret
change_password = _v.change_password
reset = _v.reset
encrypt = _v.encrypt
decrypt = _v.decrypt
rotate_data_key = _v.rotate_data_key
reencrypt_legacy = _v.reencrypt_legacy

# ---- contexts: every ciphertext is bound to the row and column it lives in -------------------------------------------------
FINGERPRINT_KEY_SETTING = "card_fingerprint_key_enc"
FINGERPRINT_KEY_CONTEXT = f"app_setting.{FINGERPRINT_KEY_SETTING}"


def llm_key_context(account_id: int | str) -> str:
    return f"llm_account.api_key_enc:{account_id}"


def webhook_secret_context(merchant_id: str) -> str:
    return f"merchant.webhook_secret_enc:{merchant_id}"


# Every encrypted column, so a data-key rotation re-encrypts it (and a test fails when a new one is not listed here).
_v.register_store("llm_account.api_key_enc",
                  "SELECT id, api_key_enc AS value FROM llm_account WHERE api_key_enc IS NOT NULL",
                  "UPDATE llm_account SET api_key_enc = %(value)s WHERE id = %(id)s",
                  context="llm_account.api_key_enc:{id}")
_v.register_store("merchant.webhook_secret_enc",
                  "SELECT id, webhook_secret_enc AS value FROM merchant WHERE webhook_secret_enc IS NOT NULL",
                  "UPDATE merchant SET webhook_secret_enc = %(value)s WHERE id = %(id)s",
                  context="merchant.webhook_secret_enc:{id}")
_v.register_store(f"app_setting.{FINGERPRINT_KEY_SETTING}",
                  f"SELECT key, value FROM app_setting WHERE key = '{FINGERPRINT_KEY_SETTING}'",
                  "UPDATE app_setting SET value = %(value)s WHERE key = %(key)s",
                  context=FINGERPRINT_KEY_CONTEXT)


def upgrade_legacy(conn) -> int:
    """Rewrite every value written before contexts existed (Fernet tokens, which carry no context) as a v2 token bound to its
    row. Needs the vault unlocked; values that are already v2 are left alone, so it is safe to run after every unlock. Commits
    nothing: the caller owns the transaction. Returns how many values were upgraded."""
    done = 0
    for st in _v.stores().values():
        for row in conn.execute(st["select"]).fetchall():
            row = dict(row)
            token = row["value"]
            if not token or not is_legacy_token(token):
                continue
            row["value"] = _v.reencrypt_legacy(token, st["context"].format(**{k: v for k, v in row.items() if k != "value"}))
            conn.execute(st["update"], row)
            done += 1
    return done


def upgrade_after_unlock(conn) -> None:
    """Best effort, after a successful unlock: bind values written before contexts existed to their rows. A failure leaves the
    old tokens, which keep decrypting."""
    try:
        if is_unlocked() and upgrade_legacy(conn):
            conn.commit()
        else:
            conn.rollback()
    except Exception:  # noqa: BLE001
        conn.rollback()
