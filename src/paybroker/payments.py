"""The payment pipeline: idempotency, Tier 1 rules, feature fetch, Tier 2 score, decision, routing, persistence, events.

Everything for one authorization happens in ONE database transaction. That is what makes idempotency simple and safe: a
second request with the same key blocks on the unique index until the first commits, then replays its stored result.
If anything fails the transaction rolls back and the key is free to retry.
"""
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from . import cards, features, fraud, ml, namematch, origins, routing, rules, training_data, webhooks
from . import metrics as m
from .ids import new_id


class PaymentError(Exception):
    """A business error with an HTTP status and a stable code the caller can act on."""

    def __init__(self, status: int, code: str, message: str, payment_id: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.payment_id = status, code, message, payment_id

    def body(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.payment_id:
            err["payment_id"] = self.payment_id
        return {"error": err}


def request_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


# ---- rendering (one place defines what a payment looks like) --------------------------------------------------
def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def render(tx: dict, fa: dict | None, challenge: dict | None, public_url: str) -> dict:
    out = {"payment_id": tx["payment_id"], "status": tx["status"], "amount": tx["amount"], "currency": tx["currency"],
           "created_at": _iso(tx["created_at"])}
    if tx["psp_selected"]:
        out["processor_response"] = {"psp_name": tx["psp_selected"], "authorization_code": tx["auth_code"],
                                     "network_transaction_id": tx["processor_tx_id"]}
        if tx["decline_code"]:
            out["processor_response"]["decline_code"] = tx["decline_code"]
    if challenge and tx["status"] == "REQUIRES_ACTION":
        out["action_required"] = {"type": "3DS_CHALLENGE", "redirect_url": f"{public_url}/3ds/{challenge['id']}"}
    if fa:
        f = {"score": fa["tier2_ml_score"], "decision": fa["decision"], "action_taken": fa["action_taken"],
             "execution_time_ms": int(round((fa["timings"] or {}).get("fraud_ms", 0)))}
        if fa["reasons"]:
            f["reasons"] = fa["reasons"]
        out["fraud_assessment"] = f
    if tx["status"] in {"CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED"} or tx["amount_captured"]:
        out["amount_captured"], out["amount_refunded"] = tx["amount_captured"], tx["amount_refunded"]
    return out


def authorize_response(tx: dict, fa: dict | None, challenge: dict | None, public_url: str) -> tuple[int, dict]:
    """Map a stored payment to the HTTP answer of POST /v1/payments/authorize."""
    body = render(tx, fa, challenge, public_url)
    if tx["status"] == "DECLINED" and tx["decline_code"] == "fraud":
        return 403, {"error": {"code": "FRAUD_REJECTED", "message": "Transaction declined due to security policy.",
                               "payment_id": tx["payment_id"], "fraud_assessment": body["fraud_assessment"]}}
    if tx["status"] == "DECLINED":
        return 402, {"error": {"code": "CARD_DECLINED", "message": "The card was declined.", "payment_id": tx["payment_id"],
                               "decline_code": tx["decline_code"], "fraud_assessment": body.get("fraud_assessment")}}
    if tx["status"] == "FAILED":
        return 502, {"error": {"code": "PSP_UNAVAILABLE", "message": "No payment processor could complete the request.",
                               "payment_id": tx["payment_id"]}}
    return 200, body


def load(conn, payment_id: str, merchant_id: str | None = None) -> tuple[dict, dict | None, dict | None]:
    tx = conn.execute("SELECT * FROM transaction WHERE payment_id=%s" + (" AND merchant_id=%s" if merchant_id else ""),
                      (payment_id, merchant_id) if merchant_id else (payment_id,)).fetchone()
    if not tx:
        raise PaymentError(404, "PAYMENT_NOT_FOUND", "no such payment")
    fa = conn.execute("SELECT * FROM fraud_assessment WHERE transaction_id=%s", (tx["id"],)).fetchone()
    ch = conn.execute("SELECT * FROM three_ds_challenge WHERE transaction_id=%s AND status='pending' ORDER BY created_at DESC LIMIT 1",
                      (tx["id"],)).fetchone()
    return tx, fa, ch


# ---- events -------------------------------------------------------------------------------------------------------
def emit(conn, merchant: dict, event_type: str, payload: dict) -> None:
    """Queue a signed webhook for the merchant (a no-op if it has no webhook configured)."""
    if not merchant.get("webhook_url"):
        return
    event_id = new_id("evt_", 16)
    body = {"id": event_id, "type": event_type, "created_at": _iso(datetime.now(UTC)), "data": payload}
    conn.execute("INSERT INTO webhook_event (event_id, merchant_id, type, body) VALUES (%s,%s,%s,%s)",
                 (event_id, merchant["id"], event_type, Jsonb(body)))
    conn.execute("INSERT INTO outbox (topic, payload) VALUES ('webhook', %s)", (Jsonb({"event_id": event_id}),))


# ---- the authorization pipeline -------------------------------------------------------------------------------
def check_return_url(merchant: dict, body: dict) -> None:
    """A return_url (where the hosted 3-D Secure page sends the customer back) must be on one of the merchant's allowed origins, and
    the merchant needs a webhook secret: the redirect is signed with it, so the shop can tell it from a link the customer typed."""
    url = body.get("return_url")
    if not url:
        return
    if not merchant.get("has_webhook_secret"):
        raise PaymentError(422, "RETURN_URL_NEEDS_WEBHOOK", "return_url needs a webhook configured: the redirect is signed with its secret")
    try:
        origins.clean_return_url(url, merchant.get("allowed_origins") or [])
    except origins.OriginError as exc:
        raise PaymentError(422, "RETURN_URL_NOT_ALLOWED", str(exc)) from exc


def authorize(conn: psycopg.Connection, merchant: dict, body: dict, public_url: str) -> tuple[int, dict]:
    """Run one authorization. `body` is the validated request; returns (http_status, response_body)."""
    check_return_url(merchant, body)
    t_start = time.perf_counter()
    rhash = request_hash(body)
    payment_id = new_id("pay_", 10)
    s = merchant["settings"]
    req = {"customer_id": body["customer"]["id"], "ip": body["customer"]["ip_address"],
           "email_domain": body["customer"]["email"].rsplit("@", 1)[-1].lower() if body["customer"].get("email") else None,
           "device": (body.get("telemetry") or {}).get("device_fingerprint"),
           "ip_country": (body.get("telemetry") or {}).get("ip_country"),
           "shipping_country": ((body.get("shipping_address") or {}).get("country") or "").upper() or None,
           "account_age_hours": body["customer"].get("account_age_hours"),
           "telemetry": {k: v for k, v in (body.get("telemetry") or {}).items() if k in {"checkout_duration_ms", "pasted_card_number"}}}
    capture = body.get("capture_method") or s.get("capture_method", "manual")
    row = conn.execute(
        "INSERT INTO transaction (payment_id, merchant_id, idempotency_key, request_hash, amount, currency, status, customer_id, "
        "card_token, capture_method, request) VALUES (%s,%s,%s,%s,%s,%s,'PROCESSING',%s,%s,%s,%s) "
        "ON CONFLICT (merchant_id, idempotency_key) DO NOTHING RETURNING id",
        (payment_id, merchant["id"], body["idempotency_key"], rhash, body["amount"], body["currency"], req["customer_id"],
         body["payment_method"]["card_token"], capture, Jsonb(req))).fetchone()
    if row is None:   # a committed transaction already holds this key: replay it (or refuse a different payload)
        conn.rollback()
        prior = conn.execute("SELECT payment_id, request_hash FROM transaction WHERE merchant_id=%s AND idempotency_key=%s",
                             (merchant["id"], body["idempotency_key"])).fetchone()
        if prior["request_hash"] != rhash:
            raise PaymentError(422, "IDEMPOTENCY_KEY_REUSED", "this idempotency_key was already used with a different request",
                               prior["payment_id"])
        tx, fa, ch = load(conn, prior["payment_id"], merchant["id"])
        conn.rollback()
        status, out = authorize_response(tx, fa, ch, public_url)
        return status, out
    tx_id = row["id"]

    if body["currency"] not in s["allowed_currencies"]:
        conn.rollback()
        raise PaymentError(422, "CURRENCY_NOT_ALLOWED", f"this merchant does not accept {body['currency']}")
    card = cards.get(conn, merchant["id"], body["payment_method"]["card_token"])
    if card is None:
        conn.rollback()
        raise PaymentError(422, "INVALID_CARD_TOKEN", "unknown card token for this merchant")

    # -- feature fetch
    t0 = time.perf_counter()
    ip_country, ip_asn = features.country_for(req["ip"], req["ip_country"])
    keys = features.entity_keys(req["customer_id"], req["device"], card["fingerprint"], req["ip"])
    entities = features.fetch(conn, keys)
    named, vector = features.compute(entities, user_id=req["customer_id"], device_id=req["device"], card_fp=card["fingerprint"],
                                     ip=req["ip"], amount=body["amount"], telemetry=req["telemetry"],
                                     shipping_country=req["shipping_country"], ip_country=ip_country,
                                     account_age_hours=req["account_age_hours"])
    # NAME_MISMATCH needs both names; they are compared and dropped (never stored). No key at all when it cannot be computed.
    name_mismatch = namematch.mismatch(conn, (body.get("payment_method") or {}).get("cardholder_name"), body["customer"].get("name"))
    if name_mismatch is not None:
        named["name_mismatch"] = name_mismatch
    t_features = _ms(t0)

    # -- tier 1
    t0 = time.perf_counter()
    ctx = {**named, "amount": body["amount"], "currency": body["currency"], "ip_country": ip_country,
           "shipping_country": req["shipping_country"], "bin": card["bin"], "brand": card["brand"]}
    t1 = rules.run(conn, ctx, ip=req["ip"], device=req["device"], card_fp=card["fingerprint"], bin_=card["bin"],
                   email_domain=req["email_domain"], merchant_settings=s, card_behavior=card["sim_behavior"])
    t_rules = _ms(t0)

    # -- tier 2
    t0 = time.perf_counter()
    model = ml.current(conn)
    score, contribs = model.score(vector)
    t_ml = _ms(t0)
    reasons = list(dict.fromkeys(t1.reasons + ml.reasons_from(contribs))) if (t1.decision or t1.triggered or score >= 300) else []
    fraud_ms = _ms(t_start)
    decision = fraud.decide(t1.decision, score)

    # -- act on the decision
    tx_fields: dict = {"fraud_score": score, "fraud_decision": decision}
    action, challenge, t_psp, attempts = "NONE", None, 0.0, []
    if decision == "DECLINE":
        status, action = "DECLINED", "DECLINE"
        tx_fields["decline_code"] = "fraud"
        result = "D"
    elif decision == "CHALLENGE":
        status, action = "REQUIRES_ACTION", "REQUIRE_3DS2"
        challenge = {"id": new_id("ch_", 16)}
        conn.execute("INSERT INTO three_ds_challenge (id, transaction_id, expires_at, return_url) VALUES (%s,%s,%s,%s)",
                     (challenge["id"], tx_id, datetime.now(UTC) + timedelta(minutes=30), body.get("return_url")))
        result = "C"
    else:
        status, result, t_psp, attempts = _route_and_authorize(conn, tx_id, merchant, card, body, capture, tx_fields)

    # -- persist, learn, emit
    fa_row = conn.execute(
        "INSERT INTO fraud_assessment (transaction_id, tier1_rules_triggered, tier2_ml_score, tier2_feature_snapshot, reasons, "
        "decision, action_taken, timings, feature_vector, tier3_state, model_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (tx_id, t1.triggered, score, Jsonb(named), reasons, decision, action,
         Jsonb({"fraud_ms": round(fraud_ms, 2), "feature_fetch_ms": round(t_features, 2), "tier1_ms": round(t_rules, 2),
                "ml_inference_ms": round(t_ml, 2), "psp_ms": round(t_psp, 1), "attempts": attempts}),
         vector, "queued" if score >= 700 else "none", model.version[:64])).fetchone()
    if score >= 700:   # flagged: open a case and let the asynchronous LLM agent triage it
        conn.execute("INSERT INTO fraud_case (transaction_id) VALUES (%s) ON CONFLICT DO NOTHING", (tx_id,))
        conn.execute("INSERT INTO outbox (topic, payload) VALUES ('fraud.triage', %s)", (Jsonb({"transaction_id": str(tx_id)}),))
    # Opt-in training capture rides in the feature store's own pipelined round trip (no extra database call).
    extra = ([training_data.capture_statement(payment_id, vector, model.version, score, decision)]
             if training_data.capture_enabled(conn) else None)
    features.record(conn, user_id=req["customer_id"], device_id=req["device"], card_fp=card["fingerprint"], ip=req["ip"],
                    amount=body["amount"], result=result, ip_country=ip_country, ip_asn=ip_asn, extra=extra)
    total_ms = int(_ms(t_start))
    sets = ", ".join(f"{k}=%s" for k in tx_fields)
    conn.execute(f"UPDATE transaction SET status=%s, latency_ms=%s, updated_at=now(), {sets} WHERE id=%s",
                 (status, total_ms, *tx_fields.values(), tx_id))
    tx = conn.execute("SELECT * FROM transaction WHERE id=%s", (tx_id,)).fetchone()
    http_status, out = authorize_response(tx, fa_row, challenge, public_url)
    event = {"AUTHORIZED": "payment.authorized", "CAPTURED": "payment.captured", "REQUIRES_ACTION": "payment.requires_action",
             "DECLINED": "payment.declined", "FAILED": "payment.failed"}.get(status)
    if event:
        emit(conn, merchant, event, render(tx, fa_row, challenge, public_url))
    conn.commit()

    m.FRAUD_SCORE.observe(score)
    m.STEP.labels("feature_fetch").observe(t_features / 1000)
    m.STEP.labels("tier1_rules").observe(t_rules / 1000)
    m.STEP.labels("ml_inference").observe(t_ml / 1000)
    if t_psp:
        m.STEP.labels("psp_roundtrip").observe(t_psp / 1000)
    m.STEP.labels("total").observe(total_ms / 1000)
    m.PAYMENTS.labels(status.lower(), tx["psp_selected"] or "none").inc()
    if len(attempts) > 1 and status in {"AUTHORIZED", "CAPTURED"}:
        m.FALLBACKS.inc()
    return http_status, out


def _route_and_authorize(conn, tx_id, merchant, card, body, capture, tx_fields) -> tuple[str, str, float, list]:
    """Pick connectors, call them with cascade, and fill tx_fields. Returns (status, feature result, psp ms, attempts)."""
    try:
        ranked = routing.rank(routing.connectors(conn), currency=body["currency"], amount=body["amount"], settings=merchant["settings"])
    except routing.NoRoute as exc:
        conn.rollback()
        raise PaymentError(422, "NO_ROUTE", str(exc)) from exc
    t0 = time.perf_counter()
    outcome, name, attempts = routing.execute(ranked, card, body["amount"], body["currency"])
    t_psp = _ms(t0)
    if outcome is None or outcome.kind in {"error", "timeout"}:
        tx_fields["decline_code"] = "psp_unavailable"
        tx_fields["psp_selected"] = name
        return "FAILED", "D", t_psp, attempts
    tx_fields["psp_selected"] = name
    if outcome.kind == "declined":
        tx_fields["decline_code"] = outcome.code
        return "DECLINED", "D", t_psp, attempts
    tx_fields.update(processor_tx_id=outcome.network_tx_id, auth_code=outcome.auth_code)
    if capture == "automatic":
        tx_fields["amount_captured"] = body["amount"]
        return "CAPTURED", "A", t_psp, attempts
    return "AUTHORIZED", "A", t_psp, attempts


# ---- 3-D Secure completion (simulated issuer) ---------------------------------------------------------------------
def complete_3ds(conn, challenge_id: str, outcome: str, public_url: str) -> dict:
    """The cardholder finished the challenge. In this sample the hosted page asks the tester to pick the outcome; a real
    deployment receives the ACS result instead."""
    ch = conn.execute("SELECT * FROM three_ds_challenge WHERE id=%s FOR UPDATE", (challenge_id,)).fetchone()
    if not ch:
        raise PaymentError(404, "CHALLENGE_NOT_FOUND", "no such challenge")
    if ch["status"] != "pending":
        raise PaymentError(409, "CHALLENGE_CLOSED", f"this challenge is already {ch['status']}")
    if ch["expires_at"] < datetime.now(UTC):
        conn.execute("UPDATE three_ds_challenge SET status='expired' WHERE id=%s", (challenge_id,))
        conn.execute("UPDATE transaction SET status='DECLINED', decline_code='3ds_expired', updated_at=now() WHERE id=%s", (ch["transaction_id"],))
        conn.commit()
        raise PaymentError(410, "CHALLENGE_EXPIRED", "the challenge expired; the payment was declined")
    tx = conn.execute("SELECT * FROM transaction WHERE id=%s FOR UPDATE", (ch["transaction_id"],)).fetchone()
    merchant = conn.execute("SELECT id, name, status, webhook_url, webhook_secret_enc, settings FROM merchant WHERE id=%s", (tx["merchant_id"],)).fetchone()
    fa = conn.execute("SELECT * FROM fraud_assessment WHERE transaction_id=%s", (tx["id"],)).fetchone()
    fields: dict = {}
    if outcome != "succeeded":
        conn.execute("UPDATE three_ds_challenge SET status='failed' WHERE id=%s", (challenge_id,))
        status, fields["decline_code"] = "DECLINED", "3ds_failed"
    else:
        conn.execute("UPDATE three_ds_challenge SET status='succeeded' WHERE id=%s", (challenge_id,))
        card = cards.get(conn, tx["merchant_id"], tx["card_token"])
        body = {"amount": tx["amount"], "currency": tx["currency"]}
        status, _, _, attempts = _route_and_authorize(conn, tx["id"], merchant | {"settings": merchant["settings"]}, card, body,
                                                      tx["capture_method"], fields)
        conn.execute("UPDATE fraud_assessment SET action_taken='3DS_PASSED', timings = timings || %s WHERE id=%s",
                     (Jsonb({"attempts_after_3ds": attempts}), fa["id"]))
    sets = ", ".join(f"{k}=%s" for k in fields)
    conn.execute(f"UPDATE transaction SET status=%s, updated_at=now(){', ' + sets if sets else ''} WHERE id=%s",
                 (status, *fields.values(), tx["id"]))
    tx = conn.execute("SELECT * FROM transaction WHERE id=%s", (tx["id"],)).fetchone()
    fa = conn.execute("SELECT * FROM fraud_assessment WHERE id=%s", (fa["id"],)).fetchone()
    event = {"AUTHORIZED": "payment.authorized", "CAPTURED": "payment.captured", "DECLINED": "payment.declined",
             "FAILED": "payment.failed"}.get(status)
    out = render(tx, fa, None, public_url)
    if event:
        emit(conn, merchant, event, out)
    conn.commit()
    m.PAYMENTS.labels(status.lower(), tx["psp_selected"] or "none").inc()
    if ch["return_url"] and merchant["webhook_secret_enc"]:   # the page redirects the customer here; unsigned redirects are never made
        link = webhooks.signed_return_url(merchant["webhook_secret_enc"], ch["return_url"], out["payment_id"], out["status"])
        if link:
            out["return_url"] = link
    return out


# ---- capture, void, refund ----------------------------------------------------------------------------------------
def _locked(conn, merchant_id: str, payment_id: str) -> dict:
    tx = conn.execute("SELECT * FROM transaction WHERE payment_id=%s AND merchant_id=%s FOR UPDATE", (payment_id, merchant_id)).fetchone()
    if not tx:
        raise PaymentError(404, "PAYMENT_NOT_FOUND", "no such payment")
    return tx


def _finish(conn, merchant: dict, tx_id, event: str, public_url: str) -> dict:
    tx = conn.execute("SELECT * FROM transaction WHERE id=%s", (tx_id,)).fetchone()
    fa = conn.execute("SELECT * FROM fraud_assessment WHERE transaction_id=%s", (tx_id,)).fetchone()
    out = render(tx, fa, None, public_url)
    emit(conn, merchant, event, out)
    conn.commit()
    return out


def capture(conn, merchant: dict, payment_id: str, amount: int | None, public_url: str) -> dict:
    tx = _locked(conn, merchant["id"], payment_id)
    if tx["status"] != "AUTHORIZED":
        conn.rollback()
        raise PaymentError(409, "INVALID_STATE", f"only an AUTHORIZED payment can be captured (this one is {tx['status']})", payment_id)
    amount = amount or tx["amount"]
    if not 1 <= amount <= tx["amount"]:
        conn.rollback()
        raise PaymentError(422, "INVALID_AMOUNT", "capture amount must be between 1 and the authorized amount", payment_id)
    conn.execute("UPDATE transaction SET status='CAPTURED', amount_captured=%s, updated_at=now() WHERE id=%s", (amount, tx["id"]))
    return _finish(conn, merchant, tx["id"], "payment.captured", public_url)


def void(conn, merchant: dict, payment_id: str, public_url: str) -> dict:
    tx = _locked(conn, merchant["id"], payment_id)
    if tx["status"] != "AUTHORIZED":
        conn.rollback()
        raise PaymentError(409, "INVALID_STATE", f"only an AUTHORIZED payment can be voided (this one is {tx['status']})", payment_id)
    conn.execute("UPDATE transaction SET status='VOIDED', updated_at=now() WHERE id=%s", (tx["id"],))
    return _finish(conn, merchant, tx["id"], "payment.voided", public_url)


def refund(conn, merchant: dict, payment_id: str, amount: int | None, public_url: str) -> dict:
    tx = _locked(conn, merchant["id"], payment_id)
    if tx["status"] not in {"CAPTURED", "PARTIALLY_REFUNDED"}:
        conn.rollback()
        raise PaymentError(409, "INVALID_STATE", f"only a captured payment can be refunded (this one is {tx['status']})", payment_id)
    left = tx["amount_captured"] - tx["amount_refunded"]
    amount = amount or left
    if not 1 <= amount <= left:
        conn.rollback()
        raise PaymentError(422, "INVALID_AMOUNT", f"refund amount must be between 1 and {left}", payment_id)
    status = "REFUNDED" if amount == left else "PARTIALLY_REFUNDED"
    conn.execute("UPDATE transaction SET status=%s, amount_refunded=amount_refunded+%s, updated_at=now() WHERE id=%s", (status, amount, tx["id"]))
    return _finish(conn, merchant, tx["id"], "payment.refunded", public_url)
