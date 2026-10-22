"""Measure the fraud pipeline and the full authorization path against a real database.

    make bench        (needs DATABASE_URL; run `paybroker migrate` first; uses a throwaway merchant)

Connector latency is switched off so the numbers are the broker's own: feature fetch, Tier 1 rules, Tier 2 model, and the
single database transaction that persists everything. The reference design budgets 5 ms for rules and 35 ms for the model.
"""
import os
import statistics
import sys
import time
from pathlib import Path

os.environ["PAYBROKER_SIM_LATENCY"] = "off"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paybroker import cards, merchants, payments, routing, rules  # noqa: E402
from paybroker.db import connect  # noqa: E402

N = int(os.environ.get("BENCH_N", "400"))


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


def main() -> None:
    with connect() as conn:
        routing.seed_connectors(conn)
        rules.seed_defaults(conn)
        m = merchants.create(conn, f"bench-{int(time.time())}")
        tokens = [cards.tokenize(conn, m["id"], cards.make_test_number(), 12, 2035, "123")["token"] for _ in range(N)]
        fraud_ms, feat_ms, rule_ms, ml_ms, total = [], [], [], [], []
        for i in range(N):
            body = {"idempotency_key": f"bench-{time.time_ns()}-{i}", "amount": 2500 + i, "currency": "USD",
                    "payment_method": {"type": "card", "card_token": tokens[i], "cvv_check": True},
                    "customer": {"id": f"bench_cus_{i}", "email": "bench@example.com", "ip_address": f"198.51.100.{i % 200 + 1}"},
                    "telemetry": {"device_fingerprint": f"bench_dev_{i}", "checkout_duration_ms": 30_000}, "shipping_address": {"country": "US"}}
            t0 = time.perf_counter()
            payments.authorize(conn, m, body, "http://localhost")
            total.append((time.perf_counter() - t0) * 1000)
            t = conn.execute("SELECT timings FROM fraud_assessment ORDER BY created_at DESC LIMIT 1").fetchone()["timings"]
            fraud_ms.append(t["fraud_ms"])
            feat_ms.append(t["feature_fetch_ms"])
            rule_ms.append(t["tier1_ms"])
            ml_ms.append(t["ml_inference_ms"])
    print(f"{N} payments, one connection, connectors instant")
    for name, v in (("feature fetch", feat_ms), ("tier 1 rules", rule_ms), ("tier 2 model", ml_ms), ("fraud engine (all)", fraud_ms), ("full authorize", total)):
        print(f"  {name:20s} p50 {statistics.median(v):7.2f} ms   p95 {pct(v, 0.95):7.2f} ms   max {max(v):7.2f} ms")


if __name__ == "__main__":
    main()
