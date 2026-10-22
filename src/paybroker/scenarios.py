"""The scoring harness: named customer and attack situations as explicit feature vectors, with the band each should land in.

These are the behaviours a merchant would complain about if the model got them wrong. `tests/test_pure.py` asserts every
expectation against the bundled model, and `scripts/train_model.py` prints the table for a freshly trained one, so a retrain
that breaks a scenario is caught before it ships. The vectors are written as named features (the 17 of `VECTOR_FIELDS`),
the same numbers `features.compute` would produce, so each reads like a story.

Bands: below 300 approve, 300 to 699 challenge with 3-D Secure, 700 and above decline (see `fraud.py`).
"""
import math

from .features import VECTOR_FIELDS

# An ordinary customer's first payment in a while: USD 45, an established account (the broker's 30-day default), a normal
# checkout of about a minute, one card, nothing unusual about the device or the place.
ORDINARY = {
    "amount_log": math.log1p(45), "amount_vs_avg_ratio": 1.0, "user_tx_1h": 0, "user_tx_24h": 0, "user_sum_24h_log": 0.0,
    "distinct_cards_7d": 1, "ip_tx_1h": 0, "device_tx_1h": 0, "card_tx_1h": 0, "chargebacks": 0, "device_linked_chargebacks": 0,
    "impossible_travel": 0, "new_device_for_account": 0, "checkout_seconds_log": math.log1p(60), "pasted_card": 0,
    "country_mismatch": 0, "account_age_hours_log": math.log1p(720),
}


def vector(**overrides) -> list[float]:
    unknown = set(overrides) - set(ORDINARY)
    if unknown:
        raise KeyError(f"unknown features: {sorted(unknown)}")
    return [float(({**ORDINARY, **overrides})[name]) for name in VECTOR_FIELDS]


def shopper(prior: int, *, amount: float = 45.0, same_everything: bool = True, **extra) -> list[float]:
    """A regular customer who already paid `prior` times in the last hour. `same_everything`: with one card, one IP and one
    device the four counters rise together (the usual case); otherwise only the account counter does."""
    spend = amount * (prior + 1)
    base = {"amount_log": math.log1p(amount), "user_tx_1h": prior, "user_tx_24h": prior, "user_sum_24h_log": math.log1p(spend),
            "checkout_seconds_log": math.log1p(45 if prior else 60)}
    if same_everything:
        base.update(card_tx_1h=prior, ip_tx_1h=prior, device_tx_1h=prior)
    return vector(**{**base, **extra})


def _scenarios() -> dict[str, tuple[list[float], int, int]]:
    s: dict[str, tuple[list[float], int, int]] = {"ordinary first payment": (vector(), 0, 299)}
    for prior in (1, 2, 3, 4, 5, 6):
        s[f"regular shopper, {prior} earlier payment{'s' if prior > 1 else ''} this hour"] = (shopper(prior), 0, 300 - 1)
    s["regular shopper, 6 earlier, USD 150 basket"] = (shopper(6, amount=150.0, amount_vs_avg_ratio=2.0), 0, 299)
    s["regular shopper, 5 earlier, only the account counter"] = (shopper(5, same_everything=False), 0, 299)
    s["busy day: 8 payments in 24 h, 3 in the last hour"] = (
        vector(user_tx_1h=2, card_tx_1h=2, ip_tx_1h=2, device_tx_1h=2, user_tx_24h=7, user_sum_24h_log=math.log1p(400)), 0, 299)
    s["office network: 15 other customers on the IP"] = (vector(ip_tx_1h=15), 0, 299)
    s["traveller: new device, other country, USD 400"] = (
        vector(new_device_for_account=1, country_mismatch=1, amount_log=math.log1p(400), amount_vs_avg_ratio=2.5), 0, 699)
    s["card testing: 12 cards from one device and IP"] = (
        vector(amount_log=math.log1p(2), ip_tx_1h=14, device_tx_1h=10, card_tx_1h=2, user_tx_1h=7, user_tx_24h=10,
               user_sum_24h_log=math.log1p(20), distinct_cards_7d=8, checkout_seconds_log=math.log1p(3), pasted_card=1,
               account_age_hours_log=math.log1p(8)), 700, 1000)
    s["card testing: one new customer per card, one device and IP"] = (
        vector(amount_log=math.log1p(2), ip_tx_1h=12, device_tx_1h=12, checkout_seconds_log=math.log1p(3), pasted_card=1,
               account_age_hours_log=math.log1p(8)), 700, 1000)
    s["account takeover: new device, travel, big spend, fast"] = (
        vector(new_device_for_account=1, impossible_travel=1, country_mismatch=1, amount_log=math.log1p(900),
               amount_vs_avg_ratio=9.0, checkout_seconds_log=math.log1p(10), pasted_card=1, user_tx_1h=1), 700, 1000)
    s["account takeover: new device and country, spend 6x usual, fast"] = (
        vector(new_device_for_account=1, country_mismatch=1, amount_log=math.log1p(700), amount_vs_avg_ratio=6.0,
               checkout_seconds_log=math.log1p(12), pasted_card=1), 700, 1000)
    s["synthetic identity: new account, 4 cards, linked chargebacks"] = (
        vector(account_age_hours_log=math.log1p(6), distinct_cards_7d=5, device_linked_chargebacks=2, country_mismatch=1,
               amount_log=math.log1p(900), checkout_seconds_log=math.log1p(18)), 700, 1000)
    s["chargeback-linked device"] = (vector(device_linked_chargebacks=3, chargebacks=1, amount_log=math.log1p(250)), 700, 1000)
    return s


SCENARIOS = _scenarios()


def evaluate(model) -> list[dict]:
    """Score every scenario with `model` (an `ml.Model`); each row says whether it landed in its expected range."""
    out = []
    for name, (vec, low, high) in SCENARIOS.items():
        score = model.score(vec, explain=False)[0]
        out.append({"scenario": name, "score": score, "expected": [low, high], "ok": low <= score <= high})
    return out
