"""Demo traffic: a believable mix of customers, issuers and fraud patterns sent through the real pipeline.

Deterministic for a given seed. Every identity is fictional (`cus_demo_07`, `shopper07@example.com`, documentation IP ranges),
and every card is a public processor test number or one in the reserved test BIN range.
"""
import random
import secrets
from collections import Counter

from . import cards, merchants, payments

DEMO_MERCHANT = "Demo Shop"
HOMES = {"US": ("198.51.100", "USD"), "GB": ("203.0.113", "GBP"), "DE": ("192.0.2", "EUR")}   # country: (documentation IP range, currency)
RISKY_IPS = ["100.66.", "100.67."]   # the demo "high risk" ranges (see geo.py)


def _merchant(conn) -> tuple[dict, str | None]:
    row = conn.execute("SELECT id FROM merchant WHERE name=%s ORDER BY created_at LIMIT 1", (DEMO_MERCHANT,)).fetchone()
    if row:
        return merchants.get(conn, row["id"]), None
    m = merchants.create(conn, DEMO_MERCHANT)
    _, key = merchants.new_key(conn, m["id"], "secret", "demo")
    return m, key


def _token(conn, merchant_id: str, number: str) -> str:
    return cards.tokenize(conn, merchant_id, number, 12, 2035, "123")["token"]


def run(conn, count: int, seed: int = 7, public_url: str = "http://localhost:8000") -> dict:
    rng = random.Random(seed)
    run_id = secrets.token_hex(4)
    mer, secret_key = _merchant(conn)
    mid = mer["id"]
    customers = []
    for i in range(1, max(25, count * 2 // 3) + 1):
        country = rng.choice(list(HOMES))
        base, currency = HOMES[country]
        customers.append({"id": f"cus_demo_{i:03d}", "email": f"shopper{i:03d}@example.com", "ip": f"{base}.{rng.randint(2, 250)}",
                          "country": country, "currency": currency, "device": f"dev_demo_{i:03d}", "token": _token(conn, mid, cards.make_test_number(rng)),   # one card each: a shared card would look like card testing
                          "typical": rng.choice([1500, 3200, 6400, 9900, 14000])})
    outcomes: Counter = Counter()
    sent = 0
    for n in range(count):
        roll = rng.random()
        c = rng.choice(customers)
        body = {"idempotency_key": f"demo-{run_id}-{n:04d}", "currency": c["currency"],
                "customer": {"id": c["id"], "email": c["email"], "ip_address": c["ip"]},
                "payment_method": {"type": "card", "card_token": c["token"], "cvv_check": True},
                "telemetry": {"device_fingerprint": c["device"], "checkout_duration_ms": rng.randint(25_000, 120_000),
                              "user_agent": "Mozilla/5.0 (demo)", "pasted_card_number": False},
                "shipping_address": {"country": c["country"]}, "amount": max(500, int(c["typical"] * rng.uniform(0.5, 1.6)))}
        if roll < 0.08:        # the issuer declines
            body["payment_method"]["card_token"] = _token(conn, mid, rng.choice(["4000000000000002", "4000000000009995", "4100000000000019"]))
        elif roll < 0.13:      # the issuer demands 3-D Secure
            body["payment_method"]["card_token"] = _token(conn, mid, "4000002500003155")
        elif roll < 0.19:      # the first connector fails; the cascade recovers
            body["payment_method"]["card_token"] = _token(conn, mid, "4000000000000119")
        elif roll < 0.25:      # a brand-new account, a big basket, a hurried checkout
            body["customer"] = {"id": f"cus_demo_new_{run_id}_{n}", "email": f"newbie{n}@example.com", "ip_address": c["ip"],
                                "account_age_hours": rng.uniform(0.2, 6)}
            body["telemetry"] |= {"device_fingerprint": f"dev_demo_new_{run_id}_{n}", "checkout_duration_ms": rng.randint(6_000, 14_000)}
            body["amount"] = rng.randint(90_000, 250_000)
        elif roll < 0.33:      # card testing: a burst of small payments from one risky IP with fresh cards
            ip = f"{rng.choice(RISKY_IPS)}{rng.randint(0, 3)}.{rng.randint(2, 250)}"
            body["customer"] = {"id": f"cus_demo_probe_{run_id}_{n % 3}", "email": "probe@example.com", "ip_address": ip,
                                "account_age_hours": rng.uniform(0.1, 3)}
            body["payment_method"]["card_token"] = _token(conn, mid, cards.make_test_number())
            body["telemetry"] |= {"device_fingerprint": f"dev_demo_probe_{run_id}", "checkout_duration_ms": rng.randint(900, 2500),
                                  "pasted_card_number": True}
            body["amount"] = rng.randint(100, 900)
        elif roll < 0.39:      # account takeover: a known customer, a new device abroad, a pasted card, a big order
            body["customer"]["ip_address"] = f"{rng.choice(RISKY_IPS)}{rng.randint(0, 3)}.{rng.randint(2, 250)}"
            body["telemetry"] |= {"device_fingerprint": f"dev_demo_stolen_{run_id}_{n}", "pasted_card_number": True,
                                  "checkout_duration_ms": rng.randint(4_000, 9_000)}
            body["amount"] = rng.randint(120_000, 350_000)
            body["shipping_address"] = {"country": "NG"}
        try:
            _, out = payments.authorize(conn, mer, body, public_url)
            outcomes[out["status"] if "status" in out else out["error"]["code"]] += 1
        except payments.PaymentError as exc:
            outcomes[exc.code] += 1
        sent += 1
    return {"merchant_id": mid, "sent": sent, "outcomes": dict(outcomes), "secret_key": secret_key}
