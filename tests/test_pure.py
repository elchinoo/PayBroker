"""Logic that needs no database: rules, scoring bands, the model, routing, cards, signing, validation of untrusted input."""
import json
import math

import pytest

from paybroker import (
    cards,
    core,
    cors,
    features,
    fraud,
    merchants,
    ml,
    modelstore,
    origins,
    routing,
    rules,
    scenarios,
    suggest,
    training_data,
    triage,
    triage_limits,
    webhooks,
)

CLEAN = [0.0] * len(features.VECTOR_FIELDS)


# ---- rules -------------------------------------------------------------------------------------------------
def cond(field, op, value):
    return {"field": field, "op": op, "value": value}


def test_rule_validation_accepts_nested_groups():
    rules.validate({"all": [cond("amount", ">=", 100), {"any": [cond("pasted_card_number", "==", True), cond("ip_country", "in", ["RU", "NG"])]}]})


@pytest.mark.parametrize("bad", [
    {}, [], "x", {"field": "amount"}, {"all": []}, {"all": "x"}, {"all": [cond("nope", "==", 1)]}, {"all": [cond("amount", "~", 1)]},
    {"all": [cond("amount", ">", "ten")]}, {"all": [cond("pasted_card_number", "==", "yes")]}, {"all": [cond("amount", ">", 1)], "any": []},
    {"all": [{"all": [{"all": [{"all": [cond("amount", ">", 1)]}]}]}]},
    {"all": [cond("amount", ">", i) for i in range(13)]}, {"field": "amount", "op": ">", "value": 1, "extra": 1},
    {"__import__": "os"}, {"all": [cond("amount", "in", "abc")]},
])
def test_rule_validation_rejects_malformed(bad):
    with pytest.raises(rules.RuleError):
        rules.validate(bad)


def test_rule_evaluation():
    ctx = {"amount": 120_000, "account_age_hours": 3.0, "ip_country": "RU", "pasted_card_number": True}
    assert rules.evaluate({"all": [cond("amount", ">=", 100_000), cond("account_age_hours", "<", 24)]}, ctx)
    assert not rules.evaluate({"all": [cond("amount", ">=", 100_000), cond("account_age_hours", ">", 24)]}, ctx)
    assert rules.evaluate({"any": [cond("ip_country", "in", ["NG", "RU"]), cond("amount", "<", 1)]}, ctx)
    assert rules.evaluate(cond("ip_country", "not_in", ["US"]), ctx)


# ---- decisions and the model -----------------------------------------------------------------------------------
@pytest.mark.parametrize("tier1,score,want", [
    (None, 0, "APPROVE"), (None, 299, "APPROVE"), (None, 300, "CHALLENGE"), (None, 699, "CHALLENGE"), (None, 700, "DECLINE"),
    ("CHALLENGE", 10, "CHALLENGE"), ("CHALLENGE", 800, "DECLINE"), ("DECLINE", 0, "DECLINE"), ("APPROVE", 350, "CHALLENGE"),
])
def test_decision_is_the_stricter_of_rules_and_score(tier1, score, want):
    assert fraud.decide(tier1, score) == want


def _vec(**kw):
    v = dict(zip(features.VECTOR_FIELDS, CLEAN, strict=True))
    v.update(kw)
    return [v[f] for f in features.VECTOR_FIELDS]


def test_model_separates_clean_from_risky_and_explains():
    model = ml.load()
    clean = model.score(_vec(amount_log=6.0, amount_vs_avg_ratio=1.0, checkout_seconds_log=4.0, account_age_hours_log=6.0))[0]
    risky_score, contribs = model.score(_vec(amount_log=12.0, amount_vs_avg_ratio=12.0, user_tx_1h=6.0, ip_tx_1h=9.0, card_tx_1h=7.0,
                                             new_device_for_account=1.0, pasted_card=1.0, checkout_seconds_log=1.0, country_mismatch=1.0,
                                             account_age_hours_log=0.2, distinct_cards_7d=5.0))
    assert 0 <= clean < 300 and 700 <= risky_score <= 1000
    assert ml.reasons_from(contribs)


def test_json_tree_walk_reproduces_xgboost_exactly():
    """scripts/train_model.py stores xgboost's own predictions for a few vectors; the pure-Python walk must match them."""
    doc = json.loads(ml.MODEL_PATH.read_text())
    assert doc["reference"], "regenerate the model with scripts/train_model.py"
    model = ml.Model(doc)
    for ref in doc["reference"]:
        assert model.score(ref["x"], explain=False)[0] == ref["score"]


def test_model_rejects_mismatched_feature_list():
    with pytest.raises(ValueError):
        ml.Model({"features": ["x"], "base_margin": 0, "trees": []})


def test_features_vector_has_the_model_width():
    named, vector = features.compute({}, user_id="u", device_id="d", card_fp="c", ip="198.51.100.1", amount=5000, telemetry={},
                                     shipping_country="US", ip_country="US")
    assert len(vector) == len(features.VECTOR_FIELDS) == 17
    assert named["tx_count_1h"] == 0 and named["new_device_for_account"] is False


# ---- cards, signing, routing --------------------------------------------------------------------------------------
def test_luhn_and_generated_test_numbers():
    assert cards.luhn_ok("4242424242424242") and not cards.luhn_ok("4242424242424241")
    for _ in range(20):
        n = cards.make_test_number()
        assert n.startswith(cards.TEST_BIN) and cards.luhn_ok(n) and len(n) == 16


def test_behavior_by_reserved_prefix():
    import random
    rng = random.Random(1)
    for prefix, behavior in cards.TEST_PREFIX_BEHAVIOR.items():
        seen = set()
        for _ in range(30):
            n = cards.make_test_number(rng, prefix)
            assert n.startswith(prefix) and len(n) == 16 and cards.luhn_ok(n)
            assert cards.behavior_for(n) == ("visa", behavior)
            seen.add(n)
        assert len(seen) == 30                                   # unique numbers, one behaviour
    assert all(cards.behavior_for(cards.make_test_number(rng))[1] == "random" for _ in range(3000))   # never by chance
    assert cards.behavior_for(cards.make_test_number(rng, "99999904"))[1] == "random"      # an unassigned prefix
    assert cards.behavior_for("4000000000000002") == ("visa", "decline")      # the public numbers are unchanged
    assert cards.behavior_for("4000002500003155") == ("visa", "requires_3ds")
    assert cards.behavior_for("4000000000000119") == ("visa", "psp_error")
    assert cards.behavior_for("4111111111111111") is None                      # a real-looking number is still refused
    assert cards.behavior_for("4999990112345678") is None                      # the prefix must start the number
    assert cards.behavior_for("99999901123") == ("visa", "random")             # the prefix rule is for 16 digits only


def test_webhook_signature_is_stable_and_secret_dependent():
    a = webhooks.sign("whsec_a", "1700000000", '{"x":1}')
    assert a == webhooks.sign("whsec_a", "1700000000", '{"x":1}')
    assert a != webhooks.sign("whsec_b", "1700000000", '{"x":1}') != webhooks.sign("whsec_a", "1700000001", '{"x":1}')
    assert len(a) == 64


def connector(name, cur, regions, bps, fixed=0, auth=0.95, lat=100, enabled=True):
    return {"name": name, "enabled": enabled, "currencies": cur, "regions": regions, "fee_bps": bps, "fixed_fee_cents": fixed,
            "auth_ewma": auth, "latency_ewma": lat}


ROWS = [connector("cheap", ["USD", "EUR"], ["US", "EU"], 150, auth=0.93), connector("good", ["USD", "EUR"], ["US", "EU"], 250, auth=0.97),
        connector("brazil", ["BRL"], ["BR"], 260), connector("off", ["USD"], ["US"], 1, enabled=False)]
DEFAULT = {"weights": {"auth": 100.0, "cost": 30.0, "latency": 5.0}}


def test_routing_prefers_the_higher_score_and_skips_disabled():
    ranked = routing.rank(ROWS, currency="USD", amount=10_000, settings=DEFAULT)
    assert [r["connector"]["name"] for r in ranked] == ["cheap", "good"]   # 93*... - fees: the cheaper connector wins at these weights
    assert "off" not in [r["connector"]["name"] for r in ranked]
    cost_first = routing.rank(ROWS, currency="USD", amount=10_000, settings={"weights": {"auth": 1.0, "cost": 500.0, "latency": 0.0}})
    assert cost_first[0]["connector"]["name"] == "cheap"


def test_routing_hard_constraints():
    assert [r["connector"]["name"] for r in routing.rank(ROWS, currency="BRL", amount=1000, settings=DEFAULT)] == ["brazil"]
    with pytest.raises(routing.NoRoute):
        routing.rank(ROWS, currency="JPY", amount=1000, settings=DEFAULT)
    with pytest.raises(routing.NoRoute):
        routing.rank(ROWS, currency="USD", amount=1000, settings=DEFAULT | {"region": "BR"})
    capped = routing.rank(ROWS, currency="USD", amount=1000, settings=DEFAULT | {"max_fee_bps": 200})
    assert [r["connector"]["name"] for r in capped] == ["cheap"]


def test_fixed_fee_weighs_more_on_small_payments():
    c = connector("x", ["USD"], [], 100, fixed=30)
    assert routing.effective_fee_pct(c, 1000) > routing.effective_fee_pct(c, 100_000)


# ---- untrusted input: merchant settings, LLM output ----------------------------------------------------------------
NAMES = {"adyen", "chase"}


@pytest.mark.parametrize("bad", [
    {"nope": 1}, {"preferred_psps": ["ghost"]}, {"max_fee_bps": -1}, {"max_fee_bps": True}, {"weights": {"auth": 1}},
    {"weights": {"auth": 1, "cost": 1, "latency": -1}}, {"require_3ds_over_cents": 0}, {"allowed_currencies": []},
    {"allowed_currencies": ["XXX"]}, {"region": "USA"}, {"capture_method": "later"}, "x",
])
def test_merchant_settings_validation(bad):
    with pytest.raises(merchants.MerchantError):
        merchants.clean_settings(bad, NAMES)


def test_merchant_settings_defaults_and_normalisation():
    s = merchants.clean_settings({"region": "us", "preferred_psps": ["adyen"]}, NAMES)
    assert s["region"] == "US" and s["capture_method"] == "manual" and s["weights"]["auth"] == 100.0


def test_llm_rule_suggestions_are_validated_not_trusted():
    good = {"name": "fast checkouts!", "action": "challenge", "rationale": "r", "definition": {"all": [cond("checkout_seconds", "<", 3)]}}
    bad_field = {"name": "x", "action": "decline", "definition": {"all": [cond("is_admin", "==", True)]}}
    bad_action = {"name": "y", "action": "delete_everything", "definition": {"all": [cond("amount", ">", 1)]}}
    out = suggest.parse("Sure! " + json.dumps({"rules": [good, bad_field, bad_action, "junk"]}) + " hope that helps")
    assert [r["name"] for r in out] == ["fast_checkouts_"]
    with pytest.raises(triage.TriageError):
        suggest.parse("no json here")


def test_llm_triage_output_is_validated():
    ok = {"risk_category": "CARD_TESTING", "confidence_score": 0.8, "summary": "s", "key_risk_factors": ["a"], "recommended_action": "PERMANENT_BLOCK"}
    assert triage.parse(json.dumps(ok))["risk_category"] == "CARD_TESTING"
    for broken in ({**ok, "risk_category": "WIZARDRY"}, {**ok, "recommended_action": "NUKE"}, {**ok, "confidence_score": 1.5},
                   {**ok, "confidence_score": True}):
        with pytest.raises(triage.TriageError):
            triage.parse(json.dumps(broken))
    with pytest.raises(triage.TriageError):
        triage.parse("{not json")


def test_heuristic_triage_recognises_card_testing():
    payload = {"transaction": {"amount_minor_units": 300, "currency": "USD", "merchant": "m", "ip_country": "RU", "shipping_country": None},
               "telemetry": {}, "historical_features": {"checkout_seconds": 1.5},
               "ml_output": {"score": 900, "reason_codes": ["HIGH_VELOCITY_IP", "HIGH_VELOCITY_CARD"], "rules_triggered": []},
               "similar_past_cases": []}
    r = triage.heuristic(payload)
    assert r["risk_category"] == "CARD_TESTING" and r["recommended_action"] == "PERMANENT_BLOCK" and 0 <= r["confidence_score"] <= 1


# ---- the scoring harness: named situations as explicit feature vectors -------------------------------------------
@pytest.mark.parametrize("name", list(scenarios.SCENARIOS))
def test_bundled_model_scores_every_scenario_in_its_band(name):
    vec, low, high = scenarios.SCENARIOS[name]
    score = ml.load().score(vec, explain=False)[0]
    assert low <= score <= high, f"{name}: {score} outside {low}..{high}"


def test_a_busy_regular_shopper_is_not_challenged_up_to_six_payments_an_hour():
    """The regression this harness exists for: with an otherwise ordinary customer, 4 earlier payments in the hour used to score
    385 and 5 scored 428 (3-D Secure for a busy afternoon). Sweep amounts, checkout times and counter patterns."""
    model = ml.load()
    for prior in range(0, 7):
        for amount in (5, 20, 45, 100, 250):
            for seconds in (20, 30, 60, 120):
                for same in (True, False):
                    for ratio in (0.5, 1.0, 2.0, 3.0):
                        vec = scenarios.shopper(prior, amount=amount, same_everything=same, amount_vs_avg_ratio=ratio,
                                                checkout_seconds_log=math.log1p(seconds))
                        assert model.score(vec, explain=False)[0] < 300, (prior, amount, seconds, same, ratio)


def test_attack_patterns_stay_declined_and_scenario_vectors_are_explicit():
    model = ml.load()
    for name in ("card testing: 12 cards from one device and IP", "card testing: one new customer per card, one device and IP",
                 "account takeover: new device, travel, big spend, fast", "account takeover: new device and country, spend 6x usual, fast"):
        assert model.score(scenarios.SCENARIOS[name][0], explain=False)[0] >= 700, name
    assert len(scenarios.vector()) == len(features.VECTOR_FIELDS)
    with pytest.raises(KeyError):
        scenarios.vector(no_such_feature=1)
    assert all(r["ok"] for r in scenarios.evaluate(model))


def test_the_bundled_model_is_valid_small_and_flags_its_provenance():
    doc = json.loads(ml.MODEL_PATH.read_text())
    info = ml.validate(doc)
    assert info["trees"] == 70 and info["max_depth"] <= 3 and ml.MODEL_PATH.stat().st_size < 100_000
    assert isinstance(doc["trained_on"], dict) and "synthetic" in doc["trained_on"]["summary"]
    assert doc["features"] == list(features.VECTOR_FIELDS)


# ---- uploaded models: validation ---------------------------------------------------------------------------------
def _leaf_model(value: float, **over) -> dict:
    doc = {"version": "leaf", "features": list(features.VECTOR_FIELDS), "base_margin": 0.0,
           "trees": [{"f": [0], "t": [0.0], "l": [-1], "r": [-1], "v": [value], "e": [value]}]}
    doc.update(over)
    return doc


def _stump(left=1, right=2, feat=0, thr=1.0) -> dict:
    return {"f": [feat, 0, 0], "t": [thr, 0, 0], "l": [left, -1, -1], "r": [right, -1, -1], "v": [0, -1.0, 1.0], "e": [0, -1.0, 1.0]}


def test_validate_accepts_a_minimal_and_a_real_model():
    assert ml.validate(_leaf_model(0.5)) == {"trees": 1, "nodes": 1, "max_depth": 0}
    assert ml.Model(_leaf_model(20.0)).score([0.0] * 17, explain=False)[0] == 1000
    assert ml.Model(_leaf_model(-20.0)).score([0.0] * 17, explain=False)[0] == 0
    assert ml.Model(_leaf_model(1e4, base_margin=1e4)).score([0.0] * 17)[0] == 1000     # no overflow in exp()


def _bad(**kw):
    doc = _leaf_model(1.0)
    doc.update(kw)
    return doc


@pytest.mark.parametrize("doc,why", [
    ([], "not an object"), ({}, "no features"), (_bad(features=list(features.VECTOR_FIELDS)[::-1]), "feature order"),
    (_bad(features=list(features.VECTOR_FIELDS)[:-1]), "feature count"), (_bad(features=list(features.VECTOR_FIELDS) + ["x"]), "extra feature"),
    (_bad(base_margin="0"), "margin type"), (_bad(base_margin=float("inf")), "infinite margin"), (_bad(base_margin=True), "bool margin"),
    (_bad(trees=[]), "no trees"), (_bad(trees="x"), "trees type"), (_bad(trees=[_leaf_model(1.0)["trees"][0]] * (ml.MAX_TREES + 1)), "too many trees"),
    (_bad(trees=[{"f": [0]}]), "missing lists"),
    (_bad(trees=[{"f": [0], "t": [0], "l": [-1], "r": [-1], "v": [1, 2], "e": [1]}]), "ragged lists"),
    (_bad(trees=[{"f": [0], "t": [0], "l": [-1], "r": [-1], "v": [float("nan")], "e": [1]}]), "nan leaf"),
    (_bad(trees=[{"f": [0], "t": [0], "l": [-1], "r": [-1], "v": [1e9], "e": [1]}]), "huge leaf"),
    (_bad(trees=[_stump(left=0, right=2)]), "child points to the root"), (_bad(trees=[_stump(left=1, right=1)]), "shared child"),
    (_bad(trees=[{**_stump(), "l": [1, 0, -1], "r": [2, 1, -1]}]), "cycle"),
    (_bad(trees=[_stump(left=1, right=9)]), "child out of range"), (_bad(trees=[_stump(feat=17)]), "feature index out of range"),
    (_bad(trees=[_stump(feat=-1)]), "negative feature"), (_bad(trees=[_stump(feat=1.5)]), "float feature"),
    (_bad(trees=[_stump(thr=float("nan"))]), "nan threshold"), (_bad(trees=[_stump(thr="x")]), "string threshold"),
    (_bad(trees=[{**_stump(), "l": [1, 2, -1], "r": [2, 2, -1]}]), "two parents"),
    (_bad(version=7), "version type"),
])
def test_validate_refuses_malformed_models(doc, why):
    with pytest.raises(ml.ModelError):
        ml.validate(doc)


def _chain(depth: int) -> dict:
    """A tree that goes `depth` levels down its left side (every right child is a leaf)."""
    t = {k: [] for k in "ftlrve"}

    def node(d: int) -> int:
        i = len(t["f"])
        for k in t:
            t[k].append(0)
        if d == 0:
            t["l"][i] = t["r"][i] = -1
            return i
        t["t"][i] = 1.0
        t["l"][i] = node(d - 1)
        t["r"][i] = node(0)
        return i
    node(depth)
    return t


def test_validate_bounds_the_depth_of_a_tree():
    assert ml.validate(_bad(trees=[_chain(ml.MAX_DEPTH)]))["max_depth"] == ml.MAX_DEPTH
    with pytest.raises(ml.ModelError, match="deeper"):
        ml.validate(_bad(trees=[_chain(ml.MAX_DEPTH + 1)]))


def test_parse_refuses_nan_infinity_garbage_and_oversize():
    for raw in (b'{"base_margin": NaN}', b"Infinity", b"not json", b"\xff\xfe", b"[" * 100_000, b"x" * (ml.MAX_MODEL_BYTES + 1)):
        with pytest.raises(ml.ModelError):
            ml.parse(raw)
    assert ml.parse(json.dumps(_leaf_model(1.0)))["version"] == "leaf"


def test_upload_envelope_and_limits():
    doc, notes, version, activate = modelstore.parse_upload(json.dumps({"model": _leaf_model(1.0), "notes": " hi ", "version": "v2",
                                                                          "activate": True}).encode())
    assert (notes, version, activate) == ("hi", "v2", True) and doc["trees"]
    for body in (b"[]", b'{"model": 1}', b'{"model": {}, "x": 1}', b'{"trees": []}', b"{}",
                 json.dumps({"model": _leaf_model(1.0), "version": "../etc/passwd"}).encode(),
                 json.dumps({"model": _leaf_model(1.0), "version": "a" * 65}).encode(),
                 json.dumps({"model": _leaf_model(1.0), "notes": "n" * 1001}).encode()):
        with pytest.raises(modelstore.ModelStoreError) as exc:
            modelstore.parse_upload(body)
        assert exc.value.status == 422
    with pytest.raises(modelstore.ModelStoreError) as exc:
        modelstore.parse_upload(b"x" * (ml.MAX_MODEL_BYTES + 20_000))
    assert exc.value.status == 413
    bad_ref = _leaf_model(1.0, reference=[{"x": [0.0] * 17, "score": 5}])
    with pytest.raises(modelstore.ModelStoreError):
        modelstore.parse_upload(json.dumps(bad_ref).encode())


# ---- training data helpers and permissions -------------------------------------------------------------------------
@pytest.mark.parametrize("code,reason,want", [
    ("10.4", "", True), ("4837", "x", True), ("10.1", "", True), ("13.1", "fraud", False), ("4853", "", False), (None, "fraud", True),
    (None, "Fraud", True), (None, "item not received, not fraud", False), (None, "", False), ("", "stolen card", False),
])
def test_only_fraud_coded_chargebacks_are_fraud_labels(code, reason, want):
    assert training_data.is_fraud_chargeback(code, reason) is want


def test_new_permissions_belong_to_the_admin_role_alone():
    for perm in ("training:export", "model:manage"):
        assert perm in core.PERMISSIONS
        for role, perms in core.ROLE_PERMISSIONS.items():
            assert (perm in perms or "*" in perms) == (role == "admin"), (perm, role)
        assert not any(perm in p for p in core.SCOPE_PERMISSIONS.values() if "*" not in p)


def test_feature_hash_identifies_the_layout():
    assert len(features.FEATURE_HASH) == 12 and features.FEATURE_HASH == training_data.features.FEATURE_HASH
    assert training_data.parse_since("2026-01-31").year == 2026 and training_data.parse_since(None) is None
    with pytest.raises(training_data.TrainingError):
        training_data.parse_since("last tuesday")


def test_llm_environment_contract_matches_trustdesk(monkeypatch, tmp_path):
    from paybroker import accounts
    from paybroker.config import get_settings

    for v in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_API_KEY_FILE", "LLM_CHAT_MODEL"):
        monkeypatch.delenv(v, raising=False)
    assert accounts.env_account() is None and get_settings().llm_api_key == ""
    f = tmp_path / "k"
    f.write_text("file-key\nignored\n")
    monkeypatch.setenv("LLM_BASE_URL", "https://gw.example/v1/")
    monkeypatch.setenv("LLM_CHAT_MODEL", "qwen3")
    monkeypatch.setenv("LLM_API_KEY_FILE", str(f))
    s = get_settings()
    assert (s.llm_base_url, s.llm_chat_model, s.llm_api_key) == ("https://gw.example/v1", "qwen3", "file-key")
    monkeypatch.setenv("LLM_API_KEY", "env-key")        # the variable wins over the file
    assert get_settings().llm_api_key == "env-key"
    env = accounts.env_account()
    assert env["id"] == "env" and env["has_key"] and "env-key" not in str(env) and "file-key" not in str(env)


# ---- Tier 3 throttle: bucket, cap, grouping, backoff (clock and lookups injected; nothing sleeps) ------------------
class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_bucket_paces_calls_refills_with_time_and_zero_means_unlimited():
    clock = Clock()
    b = triage_limits.Bucket(clock)
    assert all(b.take(0) == 0.0 for _ in range(100))                           # unlimited
    taken = [b.take(20) for _ in range(triage_limits.BURST + 1)]
    assert taken[:triage_limits.BURST] == [0.0] * triage_limits.BURST and taken[-1] == pytest.approx(3.0)   # 20/min is one slot per 3 s
    clock.now += 3.0
    assert b.take(20) == 0.0 and b.take(20) > 0                                # exactly one slot came back
    clock.now += 600                                                           # idle: the bucket fills to its burst, never beyond
    assert sum(b.take(20) == 0.0 for _ in range(20)) == triage_limits.BURST


def test_bucket_follows_a_changed_setting_at_once():
    clock = Clock()
    b = triage_limits.Bucket(clock)
    assert sum(b.take(60) == 0.0 for _ in range(10)) == triage_limits.BURST
    assert b.take(1) > 0 and b.take(1) == pytest.approx(60.0, abs=1)           # lowered: the new rate applies, no leftover credit
    assert b.take(600) == 0.0                                                  # raised: a full bucket


LIMITS = triage_limits.Limits(rpm=20, daily_cap=500, group_minutes=30, pause_minutes=10)


def verdict(limits=LIMITS, *, now=1000.0, paused_until=0.0, job_age=0.0, used=0, grouped=None, wait=0.0):
    calls = []
    v = triage_limits.decide(limits, now=now, paused_until=paused_until, job_age=job_age,
                             used_24h=lambda: calls.append("used") or used, grouped=lambda: calls.append("group") or grouped,
                             take=lambda: calls.append("take") or wait, rand=lambda: 0.0)
    return v, calls


def test_decision_goes_to_the_model_when_nothing_stands_in_the_way():
    v, calls = verdict()
    assert (v.action, v.outcome) == ("llm", "llm") and calls == ["used", "group", "take"]


def test_daily_cap_gives_the_heuristic_at_once_and_zero_disables_it():
    v, calls = verdict(used=500)
    assert (v.action, v.outcome) == ("heuristic", "capped") and "daily cap reached" in v.note and calls == ["used"]
    assert verdict(used=499)[0].action == "llm"
    off, calls = verdict(triage_limits.Limits(20, 0, 30, 10), used=10**9)
    assert off.action == "llm" and "used" not in calls                          # no cap: the count is not even queried


def test_grouping_uses_the_heuristic_and_names_the_other_payment_and_zero_is_off():
    v, calls = verdict(grouped=("pay_abc", "device"))
    assert (v.action, v.outcome) == ("heuristic", "grouped") and "pay_abc" in v.note and "device" in v.note and "30 minutes" in v.note
    assert "take" not in calls                                                  # a grouped case never spends a rate-limit slot
    off, calls = verdict(triage_limits.Limits(20, 500, 0, 10), grouped=("pay_abc", "device"))
    assert off.action == "llm" and "group" not in calls


def test_no_free_slot_defers_with_a_short_jittered_delay():
    v, _ = verdict(wait=3.0)
    assert (v.action, v.outcome, v.delay) == ("defer", "throttled", triage_limits.THROTTLE_MIN_DELAY)    # 3 s wait is raised to the minimum
    assert verdict(wait=40.0)[0].delay == 40.0
    jittered = triage_limits.decide(LIMITS, now=0, paused_until=0, job_age=0, used_24h=lambda: 0, grouped=lambda: None,
                                    take=lambda: 1.0, rand=lambda: 1.0)
    assert jittered.delay == triage_limits.THROTTLE_MIN_DELAY + triage_limits.THROTTLE_JITTER


def test_open_breaker_keeps_new_cases_queued_but_gives_an_old_backlog_the_heuristic():
    v, calls = verdict(now=1000.0, paused_until=1600.0, job_age=60)
    assert (v.action, v.outcome, v.delay) == ("defer", "paused", 600.0) and calls == []
    stale, _ = verdict(now=1000.0, paused_until=1600.0, job_age=triage_limits.STALE_BACKLOG + 1)
    assert (stale.action, stale.outcome) == ("heuristic", "paused_fallback") and "paused" in stale.note
    assert verdict(now=1600.0, paused_until=1600.0)[0].action == "llm"          # the pause is over


def test_gateway_status_and_retry_after_are_read_from_the_error_text():
    assert triage_limits.http_status("chat endpoint returned HTTP 429: slow down") == 429
    assert triage_limits.http_status("chat endpoint returned HTTP 502: bad gateway") == 502
    assert triage_limits.http_status("the model did not return a JSON object") is None
    assert triage_limits.http_status("") is None
    for text, secs in (("HTTP 429: {'retry_after': 12}", 12.0), ("HTTP 429: Retry-After: 30", 30.0), ("HTTP 429: rate limited, retry after 7 seconds", 7.0),
                       ("HTTP 429: try again in 45s", 45.0), ("HTTP 429: Retry-After: 999999", float(triage_limits.RETRY_AFTER_MAX))):
        assert triage_limits.retry_after(text) == secs, text
    assert triage_limits.retry_after("HTTP 429: monthly token budget exhausted") is None


def test_backoff_honours_the_gateway_else_doubles_from_a_minute_up_to_fifteen():
    assert [triage_limits.backoff_seconds(n) for n in range(1, 7)] == [60, 120, 240, 480, 900, 900]
    assert triage_limits.backoff_seconds(5, asked=12.0) == 12.0
    assert triage_limits.backoff_seconds(10**6) == 900.0


def test_settings_come_from_the_store_then_the_environment_then_the_default_and_bad_values_fall_back(monkeypatch):
    for var in triage_limits.ENV.values():
        monkeypatch.delenv(var, raising=False)
    assert {f: triage_limits._stored({}, f) for f in triage_limits.KEYS} == triage_limits.DEFAULTS
    monkeypatch.setenv("PAYBROKER_TRIAGE_RPM", "7")
    monkeypatch.setenv("PAYBROKER_TRIAGE_DAILY_CAP", "oops")
    monkeypatch.setenv("PAYBROKER_TRIAGE_PAUSE_MINUTES", "0")                   # below the bound: the default
    assert triage_limits._stored({}, "rpm") == 7 and triage_limits._stored({}, "daily_cap") == triage_limits.DEFAULTS["daily_cap"] and triage_limits._stored({}, "pause_minutes") == 10
    assert triage_limits._stored({"triage_rpm": "0"}, "rpm") == 0               # a stored 0 (unlimited) wins over the environment
    assert triage_limits._stored({"triage_rpm": "9999"}, "rpm") == 7            # a stored value out of bounds is ignored


# ---- browser origins, CORS and the 3-D Secure return --------------------------------------------------------------
@pytest.mark.parametrize("raw,clean", [
    ("https://shop.example.com", "https://shop.example.com"), ("HTTPS://Shop.Example.COM", "https://shop.example.com"),
    ("https://shop.example.com:443", "https://shop.example.com"), ("https://shop.example.com:8443", "https://shop.example.com:8443"),
    ("http://localhost", "http://localhost"), ("http://localhost:3000", "http://localhost:3000"), ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
    ("https://localhost:3000", "https://localhost:3000"), ("  https://a.example.org  ", "https://a.example.org")])
def test_origins_are_normalized(raw, clean):
    assert origins.clean_origin(raw) == clean


@pytest.mark.parametrize("raw", [
    "*", "https://*.example.com", "https://shop.example.com/", "https://shop.example.com/path", "https://shop.example.com?x=1",
    "https://shop.example.com#f", "http://shop.example.com", "http://192.168.0.1", "https://203.0.113.9", "ftp://shop.example.com",
    "shop.example.com", "//shop.example.com", "https://user:pw@shop.example.com", "https://shop.example.com:99999", "https://shop.example.com:0",
    "https://shop example.com", "https://exa_mple.com", "https://-bad.example.com", "https://intranet", "https://shop.exämple.com", "", "   ",
    "https://" + "a" * 250 + ".com", "null", None, 5, "https://shop.example.com\x00"])
def test_bad_origins_are_refused(raw):
    with pytest.raises(origins.OriginError):
        origins.clean_origin(raw)


def test_origin_lists_are_deduplicated_and_bounded():
    assert origins.clean_origins(["https://a.example.com", "HTTPS://A.EXAMPLE.COM:443", "http://localhost:3000"]) == ["https://a.example.com", "http://localhost:3000"]
    assert origins.clean_origins([]) == []
    with pytest.raises(origins.OriginError):
        origins.clean_origins("https://a.example.com")
    with pytest.raises(origins.OriginError):
        origins.clean_origins([f"https://s{i}.example.com" for i in range(origins.MAX_ORIGINS + 1)])
    with pytest.raises(merchants.MerchantError, match="wildcards"):
        merchants.clean_origins(["https://*.example.com"])


def test_return_urls_must_sit_on_an_allowed_origin():
    allowed = ["https://shop.example.com", "http://localhost:3000"]
    ok = "https://shop.example.com/checkout/return?order=17"
    assert origins.clean_return_url(ok, allowed) == ok
    assert origins.clean_return_url("http://localhost:3000/back", allowed)
    for bad in ["https://evil.example.com/x", "http://shop.example.com/x", "https://shop.example.com:444/x", "https://shop.example.com/x#frag",
                "https://u:p@shop.example.com/x", "/relative", "javascript:alert(1)", "https://shop.example.com/x?status=ok",
                "https://shop.example.com/x?a=1&signature=z", "https://shop.example.com/a b", "x" * 501, "", None]:
        with pytest.raises(origins.OriginError):
            origins.clean_return_url(bad, allowed)


def test_the_cors_origin_lookup_is_cached_briefly():
    calls = []
    look = cors.OriginLookup(lambda o: calls.append(o) or o.endswith(".ok"))
    assert look("a.ok") and look("a.ok") and not look("b.no") and not look("b.no")
    assert calls == ["a.ok", "b.no"]
    look.clear()
    assert look("a.ok") and calls == ["a.ok", "b.no", "a.ok"]


def _asgi(app, method, path, headers):
    import asyncio

    out = {"start": None, "body": b""}

    async def run():
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            if m["type"] == "http.response.start":
                out["start"] = m
            else:
                out["body"] += m.get("body", b"")

        await app({"type": "http", "method": method, "path": path, "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                   "state": {}}, receive, send)

    asyncio.run(run())
    return out["start"]["status"], {k.decode(): v.decode() for k, v in out["start"]["headers"]}, out["body"]


def test_cors_middleware_preflight_and_response_headers():
    verdict = {"origin": None}

    async def inner(scope, receive, send):
        if verdict["origin"]:
            scope["state"][cors.STATE_KEY] = verdict["origin"]
        await send({"type": "http.response.start", "status": 201, "headers": [(b"content-type", b"application/json"), (b"vary", b"Accept")]})
        await send({"type": "http.response.body", "body": b"{}"})

    app = cors.BrowserCors(inner, lambda o: o == "https://shop.example.com")
    pre = {"origin": "https://shop.example.com", "access-control-request-method": "POST"}
    status, h, _ = _asgi(app, "OPTIONS", "/v1/vault/tokens", pre)
    assert status == 204 and h["access-control-allow-origin"] == "https://shop.example.com" and h["vary"] == "Origin"
    assert h["access-control-allow-methods"] == "POST, OPTIONS" and h["access-control-max-age"] == "600"
    assert h["access-control-allow-headers"] == "Authorization, Content-Type, Idempotency-Key" and "access-control-allow-credentials" not in h
    for bad in ({**pre, "origin": "https://evil.example.com"}, {**pre, "access-control-request-method": "DELETE"}):
        status, h, body = _asgi(app, "OPTIONS", "/v1/vault/tokens", bad)
        assert status == 403 and not [k for k in h if k.startswith("access-control") or k == "vary"] and b"origin_not_allowed" in body
    for path in ("/v1/payments/authorize", "/v1/payments/pay_1/capture", "/v1/payments/pay_1/refund", "/api/merchants"):    # never CORS
        status, h, _ = _asgi(app, "OPTIONS", path, pre)
        assert status == 201 and not [k for k in h if k.startswith("access-control")]
        status, h, _ = _asgi(app, "POST", path, {"origin": "https://shop.example.com"})
        assert not [k for k in h if k.startswith("access-control")]
    status, h, _ = _asgi(app, "POST", "/v1/vault/tokens", {"origin": "https://shop.example.com"})        # the route did not approve the origin
    assert status == 201 and not [k for k in h if k.startswith("access-control")] and h["vary"] == "Accept"
    verdict["origin"] = "https://shop.example.com"
    status, h, _ = _asgi(app, "POST", "/v1/vault/tokens", {"origin": "https://shop.example.com"})
    assert h["access-control-allow-origin"] == "https://shop.example.com" and h["vary"] == "Accept, Origin"
    assert "access-control-allow-credentials" not in h and "access-control-allow-methods" not in h
    status, h, _ = _asgi(app, "POST", "/v1/vault/tokens", {"origin": "https://other.example.com"})       # approval is for one exact origin
    assert not [k for k in h if k.startswith("access-control")]
    status, h, _ = _asgi(app, "POST", "/v1/vault/tokens", {})                                            # no Origin: untouched
    assert status == 201 and not [k for k in h if k.startswith("access-control")]


def test_a_failing_origin_lookup_answers_the_preflight_without_headers():
    def boom(_):
        raise RuntimeError("database asleep")

    status, h, _ = _asgi(cors.BrowserCors(None, boom), "OPTIONS", "/v1/vault/tokens",
                         {"origin": "https://shop.example.com", "access-control-request-method": "POST"})
    assert status == 403 and not [k for k in h if k.startswith("access-control")]


def test_the_signed_return_url_uses_the_webhook_scheme(monkeypatch):
    import hashlib
    import hmac
    from urllib.parse import parse_qs, urlsplit

    monkeypatch.setattr("paybroker.webhooks.vault.decrypt", lambda enc: "whsec_test")
    url = webhooks.signed_return_url("enc", "https://shop.example.com/return?order=17", "pay_abc", "AUTHORIZED", now=1_767_225_600)
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith("https://shop.example.com/return?order=17&payment_id=pay_abc&status=AUTHORIZED&timestamp=1767225600&signature=")
    assert q["order"] == "17" and not urlsplit(url).fragment
    expect = hmac.new(b"whsec_test", b"1767225600.payment_id=pay_abc&status=AUTHORIZED", hashlib.sha256).hexdigest()
    assert q["signature"] == expect and webhooks.sign("whsec_test", "1767225600", "payment_id=pay_abc&status=AUTHORIZED") == expect

    def locked(_):
        raise webhooks.vault.VaultLocked("locked")

    monkeypatch.setattr("paybroker.webhooks.vault.decrypt", locked)
    assert webhooks.signed_return_url("enc", "https://shop.example.com/r", "pay_abc", "AUTHORIZED") is None   # never an unsigned redirect
