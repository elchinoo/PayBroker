"""The card vault. It tokenizes TEST cards only and keeps metadata only.

Never stored: the card number, the CVC, anything that would let the number be rebuilt. Stored: brand, the 6-digit
BIN, last four, expiry, a keyed hash of the number (so the same card can be recognised across tokens) and, for test
cards, how the simulated PSPs should treat it. Numbers that are not on the test list (or in the reserved test BIN
range 999999) are refused, so this sample cannot be used to hold real card data.
"""
import re
import secrets
from datetime import date

from . import settings_store as store
from . import vault
from .ids import hmac_hex, new_id

TEST_BIN = "999999"
# Behaviour by prefix inside the reserved range: a 16-digit PAN starting with one of these 8 digits behaves like the matching
# public number above, so a simulator can give EVERY customer a unique card that still declines / needs 3-D Secure / hits a
# connector failure. (One shared public number used by many payments trips the per-card velocity rules and is fraud-rejected
# before it ever reaches the behaviour.) Every other 999999 number keeps the "random" behaviour.
TEST_PREFIX_BEHAVIOR = {
    "99999901": "decline",         # issuer says do_not_honor
    "99999902": "requires_3ds",    # the issuer always demands 3-D Secure
    "99999903": "psp_error",       # the first connector answers 5xx; the cascade recovers
}
# Public, well-known processor test numbers and how the simulated PSPs respond to them.
TEST_CARDS = {
    "4242424242424242": ("visa", "approve"),
    "5555555555554444": ("mastercard", "approve"),
    "378282246310005": ("amex", "approve"),
    "6011111111111117": ("discover", "approve"),
    "4000000000000002": ("visa", "decline"),               # issuer says do_not_honor
    "4000000000009995": ("visa", "insufficient_funds"),
    "4000000000000119": ("visa", "psp_error"),             # the first connector answers 5xx; the cascade recovers
    "4000002500003155": ("visa", "requires_3ds"),          # the issuer always demands 3-D Secure
    "4100000000000019": ("visa", "stolen_card"),           # issuer declines as stolen
}
_FP_KEY = "card_fingerprint_key"            # the plaintext row of earlier versions: migrated and deleted on first unlock
_FP_KEY_ENC = "card_fingerprint_key_enc"    # the key, encrypted by the vault


class CardError(ValueError):
    pass


def luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def behavior_for(number: str) -> tuple[str, str] | None:
    """(brand, behaviour) for a test PAN, or None if it is not a test card. Exact public numbers win; then the reserved
    prefixes (16 digits only); then any other 999999 number is `random`."""
    if number in TEST_CARDS:
        return TEST_CARDS[number]
    if number.startswith(TEST_BIN):
        return "visa", (TEST_PREFIX_BEHAVIOR.get(number[:8], "random") if len(number) == 16 else "random")
    return None


def make_test_number(rng: secrets.SystemRandom | None = None, prefix: str = TEST_BIN) -> str:
    """A fresh Luhn-valid 16-digit number in the reserved test BIN range (for simulators). `prefix` is `999999` (random
    behaviour) or one of the TEST_PREFIX_BEHAVIOR prefixes."""
    r = rng or secrets.SystemRandom()
    if not prefix.startswith(TEST_BIN) or len(prefix) > 15:
        raise ValueError("prefix must start with the reserved test BIN")
    while True:
        body = prefix + "".join(str(r.randrange(10)) for _ in range(15 - len(prefix)))
        for check in range(10):
            if luhn_ok(body + str(check)):
                number = body + str(check)
                break
        if prefix != TEST_BIN or number[:8] not in TEST_PREFIX_BEHAVIOR:   # an ordinary card never lands on a behaviour prefix
            return number


def _fingerprint_key(conn) -> str:
    """The HMAC key behind card fingerprints, kept in the database only encrypted by the vault.

    Fails closed: while the vault is locked (or the stored key cannot be read) this raises VaultLocked / VaultError and no card is
    tokenized, because a fingerprint made with a different key would silently break blacklists and velocity rules. A plaintext key left
    by an earlier version is encrypted and its row deleted in the same transaction (the key value is unchanged, so existing
    fingerprints stay valid). Two racing first callers agree on the winner."""
    if not vault.is_unlocked() and not vault.try_unlock_with_platform_secret(conn):
        raise vault.VaultLocked("Stored secrets are locked. An administrator must unlock the vault before cards can be tokenized.")
    enc = store.get(conn, _FP_KEY_ENC)
    if enc is None:
        plain = store.get(conn, _FP_KEY) or secrets.token_urlsafe(32)
        conn.execute("INSERT INTO app_setting (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (_FP_KEY_ENC, vault.encrypt(plain)))
        store.delete(conn, _FP_KEY)
        enc = store.get(conn, _FP_KEY_ENC)
    return vault.decrypt(enc)


def tokenize(conn, merchant_id: str, number: str, exp_month: int, exp_year: int, cvc: str | None = None) -> dict:
    number = re.sub(r"[ -]", "", str(number or ""))
    if not number.isdigit() or not 12 <= len(number) <= 19 or not luhn_ok(number):
        raise CardError("that is not a valid card number")
    found = behavior_for(number)
    if found is None:
        raise CardError("this sample only accepts test cards (see the README for the list); it never stores real card numbers")
    brand, behavior = found
    if cvc is not None and not re.fullmatch(r"\d{3,4}", str(cvc)):
        raise CardError("cvc must be 3 or 4 digits")  # checked, then discarded: it is never stored
    if isinstance(exp_year, int) and exp_year < 100:
        exp_year += 2000
    today = date.today()
    if not (isinstance(exp_month, int) and 1 <= exp_month <= 12 and isinstance(exp_year, int) and exp_year <= today.year + 20):
        raise CardError("expiry is not valid")
    if (exp_year, exp_month) < (today.year, today.month):
        raise CardError("the card has expired")
    token = new_id("tok_vault_card_", 14)
    row = conn.execute(
        "INSERT INTO card (token, merchant_id, brand, bin, last4, exp_month, exp_year, fingerprint, sim_behavior) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING token, brand, bin, last4, exp_month, exp_year, fingerprint",
        (token, merchant_id, brand, number[:6], number[-4:], exp_month, exp_year,
         hmac_hex(_fingerprint_key(conn), number), behavior)).fetchone()
    conn.commit()
    return {"token": row["token"], "brand": row["brand"], "bin": row["bin"], "last4": row["last4"],
            "exp_month": row["exp_month"], "exp_year": row["exp_year"], "fingerprint": row["fingerprint"][:16]}


def get(conn, merchant_id: str, token: str) -> dict | None:
    return conn.execute("SELECT token, brand, bin, last4, exp_month, exp_year, fingerprint, sim_behavior FROM card "
                        "WHERE token=%s AND merchant_id=%s", (token, merchant_id)).fetchone()
