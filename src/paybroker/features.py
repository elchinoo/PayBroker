"""The feature store: per-entity aggregates in Postgres (table fs_entity, key fs:{entity_type}:{entity_id}).

Replaces the Redis feature store of the reference design so the sample needs nothing but Postgres. Each entity row holds a
bounded event list (the last 200 attempts) plus a few counters; velocity and spend aggregates are computed from it at read
time, so they are exact and need no background aggregation. One query reads the four entities of a payment; one
pipelined round trip writes them back, appending events atomically.
"""
import math
import time

from psycopg.types.json import Jsonb

from . import geo

MAX_EVENTS = 200
MAX_LINKS = 50
H1, H24, D7, D30 = 3600, 86400, 7 * 86400, 30 * 86400

VECTOR_FIELDS = (
    "amount_log", "amount_vs_avg_ratio", "user_tx_1h", "user_tx_24h", "user_sum_24h_log", "distinct_cards_7d",
    "ip_tx_1h", "device_tx_1h", "card_tx_1h", "chargebacks", "device_linked_chargebacks", "impossible_travel",
    "new_device_for_account", "checkout_seconds_log", "pasted_card", "country_mismatch", "account_age_hours_log",
)


DEFAULT_ACCOUNT_AGE_HOURS = 720.0   # 30 days


def entity_keys(user_id, device_id, card_fp, ip) -> list[tuple[str, str]]:
    keys = [("user", user_id), ("device", device_id), ("card", card_fp), ("ip", ip)]
    return [(t, i) for t, i in keys if i]


def fetch(conn, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    if not keys:
        return {}
    clause = " OR ".join("(entity_type=%s AND entity_id=%s)" for _ in keys)
    rows = conn.execute(f"SELECT entity_type, entity_id, features FROM fs_entity WHERE {clause}",
                        [v for k in keys for v in k]).fetchall()
    return {(r["entity_type"], r["entity_id"]): r["features"] for r in rows}


def _window(doc: dict | None, now: float, seconds: int) -> list[dict]:
    return [e for e in (doc or {}).get("events", []) if now - e["t"] <= seconds]


def compute(entities: dict, *, user_id, device_id, card_fp, ip, amount: int, telemetry: dict, shipping_country: str | None,
            ip_country: str | None, account_age_hours: float | None = None, now: float | None = None) -> tuple[dict, list[float]]:
    """Named features (the reference schema plus the extras the model uses) and the model's input vector."""
    now = now or time.time()
    user = entities.get(("user", user_id)) if user_id else None
    device = entities.get(("device", device_id)) if device_id else None
    card = entities.get(("card", card_fp)) if card_fp else None
    ipdoc = entities.get(("ip", ip)) if ip else None

    u1, u24, u7 = _window(user, now, H1), _window(user, now, H24), _window(user, now, D7)
    u30 = _window(user, now, D30)
    ok30 = [e["a"] for e in u30 if e.get("r") == "A"]
    avg30 = sum(ok30) / len(ok30) if ok30 else 0
    sum24 = sum(e["a"] for e in u24 if e.get("r") != "D")
    cards7 = len({e["c"] for e in u7 if e.get("c")})
    first_seen = (user or {}).get("first_seen")
    # The merchant knows how old the customer's account is; the broker only knows since when it has seen them. Without the
    # merchant's answer, assume an established account: treating "never seen here" as "brand new" would put every first
    # payment of every customer into 3-D Secure.
    observed = (now - first_seen) / 3600 if first_seen else 0.0
    age_hours = account_age_hours if account_age_hours is not None else max(observed, DEFAULT_ACCOUNT_AGE_HOURS)
    known_devices = (user or {}).get("links", [])
    device_known = bool(device_id and device_id in known_devices)
    new_device = bool(user and known_devices and device_id and not device_known)
    last_country, last_seen = (user or {}).get("last_ip_country"), (user or {}).get("last_seen")
    travel = bool(last_country and ip_country and last_country != ip_country and last_seen and now - last_seen < 7200)
    chargebacks = int((user or {}).get("chargebacks", 0)) + int((card or {}).get("chargebacks", 0))
    linked = int((device or {}).get("linked_chargebacks", 0))
    checkout_ms = telemetry.get("checkout_duration_ms")
    checkout_s = (checkout_ms / 1000.0) if isinstance(checkout_ms, (int, float)) else 30.0
    mismatch = bool(ip_country and shipping_country and ip_country != shipping_country)
    ratio = min(amount / avg30, 20.0) if avg30 else 1.0

    named = {
        "tx_count_1h": len(u1), "tx_count_24h": len(u24), "tx_sum_amount_24h_cents": sum24,
        "distinct_cards_used_7d": cards7, "avg_transaction_amount_30d_cents": int(avg30),
        "last_ip_country": last_country, "last_ip_asn": (user or {}).get("last_ip_asn"),
        "device_fingerprint_known": device_known, "historical_chargeback_count": chargebacks,
        "impossible_travel_flag": travel,
        "ip_tx_count_1h": len(_window(ipdoc, now, H1)), "device_tx_count_1h": len(_window(device, now, H1)),
        "card_tx_count_1h": len(_window(card, now, H1)), "device_linked_chargebacks": linked,
        "new_device_for_account": new_device, "checkout_seconds": round(checkout_s, 1),
        "pasted_card_number": bool(telemetry.get("pasted_card_number")), "country_mismatch": mismatch,
        "account_age_hours": round(age_hours, 1), "amount_vs_avg_ratio": round(ratio, 2),
        "user_tx_count_1m": len(_window(user, now, 60)), "ip_tx_count_1m": len(_window(ipdoc, now, 60)),
        "card_tx_count_1m": len(_window(card, now, 60)), "device_tx_count_1m": len(_window(device, now, 60)),
    }
    vector = [
        math.log1p(amount / 100.0), ratio, float(len(u1)), float(len(u24)), math.log1p(sum24 / 100.0), float(cards7),
        float(named["ip_tx_count_1h"]), float(named["device_tx_count_1h"]), float(named["card_tx_count_1h"]),
        float(chargebacks), float(linked), float(travel), float(new_device), math.log1p(checkout_s),
        float(named["pasted_card_number"]), float(mismatch), math.log1p(age_hours),
    ]
    return named, vector


_UPSERT = """
INSERT INTO fs_entity (entity_type, entity_id, features)
VALUES (%(t)s, %(i)s, %(init)s)
ON CONFLICT (entity_type, entity_id) DO UPDATE SET
  features = fs_entity.features || jsonb_build_object(
    'events', (SELECT coalesce(jsonb_agg(e ORDER BY ord), '[]'::jsonb) FROM (
                 SELECT e, ord FROM jsonb_array_elements(coalesce(fs_entity.features->'events', '[]'::jsonb) || %(ev)s::jsonb)
                   WITH ORDINALITY AS x(e, ord) ORDER BY ord DESC LIMIT %(max_events)s) s),
    'links', (SELECT coalesce(jsonb_agg(to_jsonb(l)), '[]'::jsonb) FROM (
                 SELECT DISTINCT l FROM jsonb_array_elements_text(
                   coalesce(fs_entity.features->'links', '[]'::jsonb) || %(link)s::jsonb) AS y(l) LIMIT %(max_links)s) s2)
  ) || %(scalars)s::jsonb,
  updated_at = now()
"""


def record(conn, *, user_id, device_id, card_fp, ip, amount: int, result: str, ip_country: str | None, ip_asn: str | None,
           now: float | None = None) -> None:
    """Append this attempt to every entity it touched. `result` is A (authorized), C (challenged) or D (declined).
    One pipelined round trip; the event list is appended and trimmed inside the database so concurrent attempts
    against the same IP or card (the card-testing case) are never lost."""
    now = now or time.time()
    event = {"t": now, "a": amount, "r": result, "c": (card_fp or "")[:12], "u": user_id, "co": ip_country}
    plan = [
        ("user", user_id, [device_id] if device_id else [], {"last_ip_country": ip_country, "last_ip_asn": ip_asn, "last_seen": now}),
        ("device", device_id, [user_id] if user_id else [], {"last_seen": now}),
        ("card", card_fp, [], {"last_seen": now}),
        ("ip", ip, [], {"last_seen": now, "country": ip_country}),
    ]
    with conn.pipeline():
        for etype, eid, links, scalars in plan:
            if not eid:
                continue
            scalars = {k: v for k, v in scalars.items() if v is not None}
            conn.execute(_UPSERT, {
                "t": etype, "i": eid, "init": Jsonb({"first_seen": now, "events": [], "links": [], **scalars}),
                "ev": Jsonb([event]), "link": Jsonb(links), "scalars": Jsonb(scalars),
                "max_events": MAX_EVENTS, "max_links": MAX_LINKS})


def add_chargeback(conn, *, user_id, card_fp) -> int:
    """A chargeback taints the user, the card and every device the user ever used (graph linkage)."""
    n = 0
    user = conn.execute("SELECT features FROM fs_entity WHERE entity_type='user' AND entity_id=%s", (user_id,)).fetchone() if user_id else None
    for etype, eid in (("user", user_id), ("card", card_fp)):
        if eid:
            conn.execute(
                "INSERT INTO fs_entity (entity_type, entity_id, features) VALUES (%s,%s,'{\"chargebacks\":1}') "
                "ON CONFLICT (entity_type, entity_id) DO UPDATE SET features = fs_entity.features || jsonb_build_object("
                "'chargebacks', coalesce((fs_entity.features->>'chargebacks')::int, 0) + 1), updated_at=now()", (etype, eid))
            n += 1
    for dev in (user["features"].get("links", []) if user else []):
        conn.execute(
            "UPDATE fs_entity SET features = features || jsonb_build_object('linked_chargebacks', "
            "coalesce((features->>'linked_chargebacks')::int, 0) + 1), updated_at=now() "
            "WHERE entity_type='device' AND entity_id=%s", (dev,))
        n += 1
    conn.commit()
    return n


def country_for(ip: str | None, override: str | None) -> tuple[str | None, str | None]:
    if override and len(override) == 2 and override.isalpha():
        _, asn = geo.lookup(ip or "")
        return override.upper(), asn
    return geo.lookup(ip or "")
