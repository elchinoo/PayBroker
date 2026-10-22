"""Smart routing: hard constraints first, then the cost and performance score S_r, then cascade on connector failure.

    S_r = W_auth * AuthRate_r  -  W_cost * Fee_r  -  W_lat * Latency_r        (the highest S_r among eligible connectors wins)

AuthRate is the connector's observed authorization rate (an EWMA of the last attempts), Fee its effective percentage for
this payment (percentage fee plus the fixed fee spread over the amount) and Latency its observed seconds per attempt.
"""
import threading
import time

from . import psp

PREFERRED_BONUS = 10.0
_cache: dict = {"at": 0.0, "rows": []}
_lock = threading.Lock()
TTL = 5.0


class NoRoute(Exception):
    pass


def invalidate() -> None:
    with _lock:
        _cache["at"] = 0.0


def connectors(conn) -> list[dict]:
    with _lock:
        if time.monotonic() - _cache["at"] < TTL:
            return _cache["rows"]
    rows = conn.execute("SELECT * FROM psp_connector ORDER BY name").fetchall()
    conn.commit()
    with _lock:
        _cache.update(at=time.monotonic(), rows=rows)
    return rows


def effective_fee_pct(c: dict, amount: int) -> float:
    return c["fee_bps"] / 100.0 + (c["fixed_fee_cents"] / amount * 100.0 if amount else 0.0)


def rank(rows: list[dict], *, currency: str, amount: int, settings: dict) -> list[dict]:
    """Eligible connectors, best first, each with its score and the numbers behind it. Raises NoRoute if none qualify."""
    region = settings.get("region")
    w = settings.get("weights") or {"auth": 100.0, "cost": 30.0, "latency": 5.0}
    max_fee = settings.get("max_fee_bps")
    prefs = set(settings.get("preferred_psps") or [])
    out = []
    for c in rows:
        if not c["enabled"] or currency not in c["currencies"]:
            continue
        if region and region not in c["regions"]:            # data residency: only connectors that serve that region
            continue
        if currency == "BRL" and "BR" not in c["regions"]:   # BRL settles only through local Brazilian acquirers
            continue
        if max_fee is not None and c["fee_bps"] > max_fee:
            continue
        auth, lat_ms = psp.live(c["name"], float(c["auth_ewma"]), float(c["latency_ewma"]))
        fee = effective_fee_pct(c, amount)
        score = w["auth"] * auth - w["cost"] * fee - w["latency"] * (lat_ms / 1000.0) + (PREFERRED_BONUS if c["name"] in prefs else 0.0)
        out.append({"connector": c, "score": round(score, 3), "auth_rate": round(auth, 4), "fee_pct": round(fee, 3),
                    "latency_ms": round(lat_ms, 1)})
    if not out:
        raise NoRoute(f"no connector can process {currency}" + (f" in region {region}" if region else ""))
    return sorted(out, key=lambda r: -r["score"])


def execute(ranked: list[dict], card: dict, amount: int, currency: str) -> tuple[psp.Outcome | None, str | None, list[dict]]:
    """Try the best connector, cascade to the next on a 5xx or timeout while the 800 ms budget lasts.
    Returns (outcome, connector_name, attempts). Issuer declines are final: they are not retried elsewhere."""
    started = time.monotonic()
    attempts: list[dict] = []
    last = None
    for i, r in enumerate(ranked[:3]):
        if i and (time.monotonic() - started) * 1000 > psp.CASCADE_BUDGET_MS:
            break
        name = r["connector"]["name"]
        outcome = psp.authorize(r["connector"], card, amount, currency, i)
        psp.observe(name, outcome.kind in {"approved", "declined"}, outcome.latency_ms)
        attempts.append({"psp": name, "result": outcome.kind, "code": outcome.code, "ms": round(outcome.latency_ms, 1)})
        last = (outcome, name)
        if outcome.kind in {"approved", "declined"}:
            return outcome, name, attempts
    return (last[0], last[1], attempts) if last else (None, None, attempts)


SEED = [
    ("stripe", "Stripe (simulated)", ["USD", "EUR", "GBP", "CAD", "AUD", "MXN", "JPY"], ["US", "EU"], 210, 0, 55, 0.955),
    ("adyen", "Adyen (simulated)", ["USD", "EUR", "GBP", "CAD", "AUD", "SEK", "CHF", "INR", "JPY"], ["US", "EU", "IN"], 180, 0, 70, 0.948),
    ("chase", "Chase Paymentech (simulated)", ["USD"], ["US"], 190, 0, 85, 0.940),
    ("localbr", "Brazilian local acquirer (simulated)", ["BRL"], ["BR"], 260, 0, 80, 0.930),
]


def seed_connectors(conn) -> None:
    for name, label, cur, regions, bps, fixed, lat, auth in SEED:
        conn.execute("INSERT INTO psp_connector (name, label, currencies, regions, fee_bps, fixed_fee_cents, base_latency_ms, "
                     "base_auth_rate, auth_ewma, latency_ewma) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                     (name, label, cur, regions, bps, fixed, lat, auth, auth, lat))
    conn.commit()
    invalidate()
