"""Logic that needs no database: rules, scoring bands, the model, routing, cards, signing, validation of untrusted input."""
import json

import pytest

from paybroker import cards, features, fraud, merchants, ml, routing, rules, suggest, triage, webhooks

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
