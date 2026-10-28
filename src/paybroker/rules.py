"""Tier 1: deterministic rules and filters (target under 5 ms).

Built-in hard checks (blacklists, hard velocity limits, merchant 3-D Secure thresholds) plus operator-defined rules
stored as JSON. A rule is data, never code: nested all/any groups of comparisons over a fixed list of fields, validated
strictly. That matters because the LLM proposes new rules: whatever it writes must pass the same validator, and a person
approves it before it can act.
"""
import threading
import time

from psycopg.types.json import Jsonb

NUMERIC_FIELDS = {
    "amount", "tx_count_1h", "tx_count_24h", "tx_sum_amount_24h_cents", "distinct_cards_used_7d",
    "avg_transaction_amount_30d_cents", "historical_chargeback_count", "ip_tx_count_1h", "device_tx_count_1h",
    "card_tx_count_1h", "device_linked_chargebacks", "checkout_seconds", "account_age_hours", "amount_vs_avg_ratio",
    "user_tx_count_1m", "ip_tx_count_1m", "card_tx_count_1m", "device_tx_count_1m",
}
BOOL_FIELDS = {"impossible_travel_flag", "device_fingerprint_known", "new_device_for_account", "pasted_card_number",
               "country_mismatch", "name_mismatch"}
TEXT_FIELDS = {"currency", "ip_country", "shipping_country", "bin", "brand", "last_ip_country"}
FIELDS = NUMERIC_FIELDS | BOOL_FIELDS | TEXT_FIELDS
OPS = {"==", "!=", ">", ">=", "<", "<=", "in", "not_in"}
MAX_DEPTH, MAX_CONDITIONS = 3, 12
HARD_VELOCITY_PER_MIN = 5
NAME_MISMATCH = "NAME_MISMATCH"   # soft signal (see namematch.py): recorded as a reason, and usable in rules as the field name_mismatch

DEFAULT_RULES = [
    ("new_account_high_amount", "A brand-new account spending a lot: ask for 3-D Secure.", "challenge",
     {"all": [{"field": "amount", "op": ">=", "value": 100000}, {"field": "account_age_hours", "op": "<", "value": 24}]}),
    ("foreign_ip_large_basket", "The IP country differs from the shipping country on a large basket.", "challenge",
     {"all": [{"field": "country_mismatch", "op": "==", "value": True}, {"field": "amount", "op": ">=", "value": 50000}]}),
    ("many_cards_one_account", "Four or more different cards on one account within a week.", "decline",
     {"all": [{"field": "distinct_cards_used_7d", "op": ">=", "value": 4}]}),
    ("name_mismatch_large_basket", "The cardholder name does not look like the account name on a sizeable basket: ask for 3-D Secure.", "challenge",
     {"all": [{"field": "name_mismatch", "op": "==", "value": True}, {"field": "amount", "op": ">=", "value": 30000}]}),
]


class RuleError(ValueError):
    pass


def validate(defn, depth: int = 0, counter: list | None = None) -> None:
    """Raise RuleError unless `defn` is a well-formed rule. Used for operator input and for LLM suggestions."""
    counter = counter if counter is not None else [0]
    if not isinstance(defn, dict) or len(defn) != 1 and not {"field", "op", "value"} <= set(defn):
        raise RuleError("a rule is {all: [...]}, {any: [...]} or {field, op, value}")
    if "all" in defn or "any" in defn:
        key = "all" if "all" in defn else "any"
        if set(defn) != {key} or not isinstance(defn[key], list) or not 1 <= len(defn[key]) <= MAX_CONDITIONS:
            raise RuleError(f"{key} needs a list of 1 to {MAX_CONDITIONS} conditions")
        if depth + 1 > MAX_DEPTH:
            raise RuleError(f"rules nest at most {MAX_DEPTH} levels")
        for sub in defn[key]:
            validate(sub, depth + 1, counter)
        return
    if set(defn) != {"field", "op", "value"}:
        raise RuleError("a condition has exactly field, op and value")
    counter[0] += 1
    if counter[0] > MAX_CONDITIONS:
        raise RuleError(f"a rule has at most {MAX_CONDITIONS} conditions")
    field, op, value = defn["field"], defn["op"], defn["value"]
    if field not in FIELDS:
        raise RuleError(f"unknown field {field!r}")
    if op not in OPS:
        raise RuleError(f"unknown operator {op!r}")
    if op in {"in", "not_in"}:
        if field not in TEXT_FIELDS or not isinstance(value, list) or not 1 <= len(value) <= 50 or not all(isinstance(v, str) and len(v) <= 40 for v in value):
            raise RuleError("in / not_in take a list of up to 50 short texts and only work on text fields")
    elif field in NUMERIC_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 1e12 or op not in {"==", "!=", ">", ">=", "<", "<="}:
            raise RuleError(f"{field} is compared with a number")
    elif field in BOOL_FIELDS:
        if not isinstance(value, bool) or op not in {"==", "!="}:
            raise RuleError(f"{field} is compared with true or false using == or !=")
    elif not isinstance(value, str) or len(value) > 40 or op not in {"==", "!="}:
        raise RuleError(f"{field} is compared with a short text using == or !=")


def evaluate(defn: dict, ctx: dict) -> bool:
    if "all" in defn:
        return all(evaluate(s, ctx) for s in defn["all"])
    if "any" in defn:
        return any(evaluate(s, ctx) for s in defn["any"])
    have, op, want = ctx.get(defn["field"]), defn["op"], defn["value"]
    if have is None:
        return op in {"!=", "not_in"}
    if op == "in":
        return have in want
    if op == "not_in":
        return have not in want
    if op == "==":
        return have == want
    if op == "!=":
        return have != want
    return {">": have > want, ">=": have >= want, "<": have < want, "<=": have <= want}[op]


class Tier1:
    def __init__(self, decision: str | None = None, triggered: list[str] | None = None, reasons: list[str] | None = None):
        self.decision, self.triggered, self.reasons = decision, triggered or [], reasons or []


_cache: dict = {"at": 0.0, "rules": [], "black": set()}
_lock = threading.Lock()
TTL = 5.0


def invalidate() -> None:
    with _lock:
        _cache["at"] = 0.0


def _load(conn) -> tuple[list[dict], set]:
    """Rules and blacklist, cached for a few seconds so Tier 1 stays well under its 5 ms budget."""
    with _lock:
        if time.monotonic() - _cache["at"] < TTL:
            return _cache["rules"], _cache["black"]
    rules = conn.execute("SELECT id, name, definition, action FROM fraud_rule WHERE enabled ORDER BY id").fetchall()
    black = {(r["kind"], r["value"]) for r in conn.execute("SELECT kind, value FROM blacklist")}
    conn.commit()
    with _lock:
        _cache.update(at=time.monotonic(), rules=rules, black=black)
    return rules, black


def seed_defaults(conn) -> None:
    for name, desc, action, defn in DEFAULT_RULES:
        conn.execute("INSERT INTO fraud_rule (name, description, definition, action, created_by) VALUES (%s,%s,%s,%s,'system') "
                     "ON CONFLICT (name) DO NOTHING", (name, desc, Jsonb(defn), action))
    conn.commit()
    invalidate()


def run(conn, ctx: dict, *, ip: str | None, device: str | None, card_fp: str | None, bin_: str, email_domain: str | None,
        merchant_settings: dict, card_behavior: str) -> Tier1:
    rules, black = _load(conn)
    declines: list[str] = []
    challenges: list[str] = []
    reasons: list[str] = []
    for kind, value, code in (("ip", ip, "BLACK_LISTED_IP"), ("device", device, "BLACK_LISTED_FINGERPRINT"),
                              ("card_fingerprint", card_fp, "BLACK_LISTED_CARD"), ("bin", bin_, "BLACKLISTED_BIN_RANGE"),
                              ("email_domain", email_domain, "BLACK_LISTED_EMAIL_DOMAIN")):
        if value and (kind, value) in black:
            declines.append(code)
            reasons.append(code)
    for key, code in (("ip_tx_count_1m", "HARD_VELOCITY_IP"), ("card_tx_count_1m", "HARD_VELOCITY_CARD"),
                      ("user_tx_count_1m", "HARD_VELOCITY_USER")):
        if ctx.get(key, 0) >= HARD_VELOCITY_PER_MIN:
            declines.append(code)
            reasons.append(code)
    for rule in rules:
        try:
            if evaluate(rule["definition"], ctx):
                (declines if rule["action"] == "decline" else challenges).append(f"rule:{rule['name']}")
        except (TypeError, KeyError):
            continue  # a stored rule that no longer fits is skipped, never fatal
    over = merchant_settings.get("require_3ds_over_cents")
    if over and ctx["amount"] >= over:
        challenges.append("MERCHANT_3DS_THRESHOLD")
    if card_behavior == "requires_3ds":
        challenges.append("ISSUER_REQUIRES_3DS")
    soft = [NAME_MISMATCH] if ctx.get("name_mismatch") else []   # modest weight: a reason on its own, a verdict only through a rule
    if declines:
        return Tier1("DECLINE", declines + challenges + soft, (reasons or [c.removeprefix("rule:").upper() for c in declines]) + soft)
    if challenges:
        return Tier1("CHALLENGE", challenges + soft, [c.removeprefix("rule:").upper() for c in challenges] + soft)
    return Tier1(None, soft, soft)
