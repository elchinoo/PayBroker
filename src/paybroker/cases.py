"""Analyst actions: resolve flagged cases (blocking what they decide to block) and record chargebacks."""
from . import features, rules, training_data

DECISIONS = ("PERMANENT_BLOCK", "ALLOW_WITH_KYC", "DISMISS_ALERT")


class CaseError(ValueError):
    pass


def _block(conn, kind: str, value: str | None, reason: str, source: str) -> bool:
    if not value:
        return False
    row = conn.execute("INSERT INTO blacklist (kind, value, reason, source) VALUES (%s,%s,%s,%s) ON CONFLICT (kind, value) DO NOTHING "
                       "RETURNING id", (kind, value[:200], reason[:200], source)).fetchone()
    return row is not None


def resolve(conn, case_id: int, decision: str, note: str | None, actor: str) -> dict:
    if decision not in DECISIONS:
        raise CaseError(f"decision must be one of {', '.join(DECISIONS)}")
    case = conn.execute("SELECT c.*, t.request, t.card_token, t.merchant_id, t.payment_id FROM fraud_case c "
                        "JOIN transaction t ON t.id=c.transaction_id WHERE c.id=%s FOR UPDATE OF c", (case_id,)).fetchone()
    if not case:
        raise CaseError("no such case")
    if case["status"] == "resolved":
        raise CaseError("this case is already resolved")
    blocked: list[str] = []
    if decision == "PERMANENT_BLOCK":
        card = conn.execute("SELECT fingerprint FROM card WHERE token=%s", (case["card_token"],)).fetchone()
        why = f"case {case_id} ({case['payment_id']})"
        for kind, value in (("device", case["request"].get("device")), ("ip", case["request"].get("ip")),
                            ("card_fingerprint", card["fingerprint"] if card else None)):
            if _block(conn, kind, value, why, "case"):
                blocked.append(kind)
        rules.invalidate()
    # A real outcome for the model's training data: confirmed fraud or a false positive (ALLOW_WITH_KYC asserts neither).
    label = {"PERMANENT_BLOCK": 1, "DISMISS_ALERT": 0}.get(decision)
    if label is not None:
        training_data.set_label(conn, case["payment_id"], label, "case")
    conn.execute("UPDATE fraud_case SET status='resolved', analyst_decision=%s, note=%s, resolved_by=%s, resolved_at=now() WHERE id=%s",
                 (decision, (note or "")[:1000] or None, actor[:60], case_id))
    conn.commit()
    return {"case_id": case_id, "decision": decision, "blocked": blocked}


def record_chargeback(conn, payment_id: str, amount: int | None, reason: str, reason_code: str | None = None) -> dict:
    tx = conn.execute("SELECT t.*, c.fingerprint FROM transaction t LEFT JOIN card c ON c.token=t.card_token WHERE t.payment_id=%s",
                      (payment_id,)).fetchone()
    if not tx:
        raise CaseError("no such payment")
    if tx["status"] not in {"CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED", "AUTHORIZED"}:
        raise CaseError("only a payment that was authorized or captured can be charged back")
    amount = amount or tx["amount"]
    if not 1 <= amount <= tx["amount"]:
        raise CaseError("the chargeback amount must be between 1 and the payment amount")
    conn.execute("INSERT INTO chargeback (payment_id, customer_id, amount, reason, reason_code) VALUES (%s,%s,%s,%s,%s)",
                 (payment_id, tx["customer_id"], amount, reason[:200], (reason_code or "").strip()[:20] or None))
    if training_data.is_fraud_chargeback(reason_code, reason):      # a fraud-coded dispute is the strongest label there is
        training_data.set_label(conn, payment_id, 1, "chargeback")
    touched = features.add_chargeback(conn, user_id=tx["customer_id"], card_fp=tx["fingerprint"])
    return {"payment_id": payment_id, "amount": amount, "entities_updated": touched}
