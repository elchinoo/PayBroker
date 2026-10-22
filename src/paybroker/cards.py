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
from .ids import hmac_hex, new_id

TEST_BIN = "999999"
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
_FP_KEY = "card_fingerprint_key"


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


def make_test_number(rng: secrets.SystemRandom | None = None) -> str:
    """A fresh Luhn-valid number in the reserved test BIN range (for simulators)."""
    r = rng or secrets.SystemRandom()
    body = TEST_BIN + "".join(str(r.randrange(10)) for _ in range(9))
    for check in range(10):
        if luhn_ok(body + str(check)):
            return body + str(check)
    raise AssertionError("unreachable")


def _fingerprint_key(conn) -> str:
    key = store.get(conn, _FP_KEY)
    if key is None:
        key = secrets.token_urlsafe(32)
        store.put(conn, _FP_KEY, key)
    return key


def tokenize(conn, merchant_id: str, number: str, exp_month: int, exp_year: int, cvc: str | None = None) -> dict:
    number = re.sub(r"[ -]", "", str(number or ""))
    if not number.isdigit() or not 12 <= len(number) <= 19 or not luhn_ok(number):
        raise CardError("that is not a valid card number")
    if number in TEST_CARDS:
        brand, behavior = TEST_CARDS[number]
    elif number.startswith(TEST_BIN):
        brand, behavior = "visa", "random"
    else:
        raise CardError("this sample only accepts test cards (see the README for the list); it never stores real card numbers")
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
