"""Read-only views for operators (the dashboard, the MCP tools). Nothing here changes data, and nothing returns a card
number, a CVV, a key or a webhook secret: the broker never holds them in the clear."""
STATUSES = ("PROCESSING", "REQUIRES_ACTION", "AUTHORIZED", "CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED", "VOIDED", "DECLINED", "FAILED")


def overview(conn, days: int = 7) -> dict:
    days = max(1, min(int(days), 90))
    by_status = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, count(*) AS n FROM transaction WHERE created_at > now() - make_interval(days => %s) GROUP BY status", (days,))}
    total = sum(by_status.values())
    money = conn.execute(
        "SELECT coalesce(sum(amount) FILTER (WHERE status IN ('AUTHORIZED','CAPTURED','PARTIALLY_REFUNDED')),0) AS approved_cents, "
        "coalesce(sum(amount_captured),0) AS captured_cents, coalesce(sum(amount_refunded),0) AS refunded_cents, "
        "coalesce(round(avg(latency_ms)),0) AS avg_latency_ms, "
        "coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms),0)::int AS p95_latency_ms "
        "FROM transaction WHERE created_at > now() - make_interval(days => %s)", (days,)).fetchone()
    decisions = {r["fraud_decision"]: r["n"] for r in conn.execute(
        "SELECT fraud_decision, count(*) AS n FROM transaction WHERE created_at > now() - make_interval(days => %s) "
        "GROUP BY fraud_decision", (days,))}
    scores = [r for r in conn.execute(
        "SELECT width_bucket(fraud_score, 0, 1000, 10) AS b, count(*) AS n FROM transaction "
        "WHERE created_at > now() - make_interval(days => %s) GROUP BY b ORDER BY b", (days,))]
    psps = conn.execute(
        "SELECT coalesce(psp_selected,'none') AS psp, count(*) AS n, "
        "count(*) FILTER (WHERE status IN ('AUTHORIZED','CAPTURED','PARTIALLY_REFUNDED','REFUNDED')) AS approved "
        "FROM transaction WHERE created_at > now() - make_interval(days => %s) GROUP BY 1 ORDER BY n DESC", (days,)).fetchall()
    daily = conn.execute(
        "SELECT date_trunc('day', created_at)::date AS day, count(*) AS n, "
        "count(*) FILTER (WHERE status = 'DECLINED') AS declined FROM transaction "
        "WHERE created_at > now() - make_interval(days => %s) GROUP BY 1 ORDER BY 1", (days,)).fetchall()
    open_cases = conn.execute("SELECT count(*) AS n FROM fraud_case WHERE status='open'").fetchone()["n"]
    pending = conn.execute("SELECT count(*) AS n FROM rule_suggestion WHERE status='pending'").fetchone()["n"]
    backlog = conn.execute("SELECT count(*) AS n FROM outbox WHERE done_at IS NULL").fetchone()["n"]
    approved = by_status.get("AUTHORIZED", 0) + by_status.get("CAPTURED", 0) + by_status.get("PARTIALLY_REFUNDED", 0) + by_status.get("REFUNDED", 0)
    return {"days": days, "payments": total, "by_status": by_status, "approval_rate": round(approved / total, 4) if total else None,
            "money_minor_units": {k: int(v) for k, v in money.items() if k.endswith("cents")},
            "latency_ms": {"avg": int(money["avg_latency_ms"]), "p95": int(money["p95_latency_ms"])},
            "fraud_decisions": decisions, "score_histogram": [{"from": (r["b"] - 1) * 100, "count": r["n"]} for r in scores],
            "psp": psps, "daily": daily, "open_cases": open_cases, "pending_rule_suggestions": pending, "outbox_backlog": backlog}


def search_payments(conn, *, status: str | None = None, merchant_id: str | None = None, min_score: int | None = None,
                    decision: str | None = None, q: str | None = None, limit: int = 25, offset: int = 0) -> list[dict]:
    limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
    like = f"%{q.strip()}%" if q and q.strip() else None
    return conn.execute(
        "SELECT t.payment_id, t.merchant_id, m.name AS merchant, t.status, t.amount, t.currency, t.fraud_score, t.fraud_decision, "
        "t.psp_selected, t.decline_code, t.customer_id, t.latency_ms, t.created_at, c.brand, c.last4, "
        "fa.tier3_state, fc.status AS case_status FROM transaction t JOIN merchant m ON m.id=t.merchant_id "
        "LEFT JOIN card c ON c.token=t.card_token LEFT JOIN fraud_assessment fa ON fa.transaction_id=t.id "
        "LEFT JOIN fraud_case fc ON fc.transaction_id=t.id "
        "WHERE (%(status)s::text IS NULL OR t.status=%(status)s) AND (%(m)s::text IS NULL OR t.merchant_id=%(m)s) "
        "AND (%(s)s::int IS NULL OR t.fraud_score >= %(s)s) AND (%(d)s::text IS NULL OR t.fraud_decision=%(d)s) "
        "AND (%(q)s::text IS NULL OR t.payment_id ILIKE %(q)s OR t.customer_id ILIKE %(q)s OR t.idempotency_key ILIKE %(q)s) "
        "ORDER BY t.created_at DESC LIMIT %(l)s OFFSET %(o)s",
        {"status": status, "m": merchant_id, "s": min_score, "d": decision, "q": like, "l": limit, "o": offset}).fetchall()


def payment_detail(conn, payment_id: str) -> dict | None:
    tx = conn.execute(
        "SELECT t.payment_id, t.merchant_id, m.name AS merchant, t.status, t.amount, t.currency, t.amount_captured, t.amount_refunded, "
        "t.customer_id, t.capture_method, t.fraud_score, t.fraud_decision, t.psp_selected, t.processor_tx_id, t.auth_code, "
        "t.decline_code, t.latency_ms, t.request, t.created_at, t.updated_at, c.brand, c.bin, c.last4, c.exp_month, c.exp_year, t.id "
        "FROM transaction t JOIN merchant m ON m.id=t.merchant_id LEFT JOIN card c ON c.token=t.card_token WHERE t.payment_id=%s",
        (payment_id,)).fetchone()
    if not tx:
        return None
    tid = tx.pop("id")
    fa = conn.execute(
        "SELECT tier1_rules_triggered, tier2_ml_score, tier2_feature_snapshot, reasons, decision, action_taken, timings, tier3_state, "
        "tier3_llm_analysis, model_version, created_at FROM fraud_assessment WHERE transaction_id=%s", (tid,)).fetchone()
    case = conn.execute("SELECT id, status, analyst_decision, note, resolved_by, resolved_at, created_at FROM fraud_case "
                        "WHERE transaction_id=%s", (tid,)).fetchone()
    challenge = conn.execute("SELECT id, status, expires_at FROM three_ds_challenge WHERE transaction_id=%s ORDER BY created_at DESC LIMIT 1",
                             (tid,)).fetchone()
    events = conn.execute("SELECT event_id, type, status, attempts, last_code, created_at, delivered_at FROM webhook_event "
                          "WHERE body->'data'->>'payment_id' = %s ORDER BY created_at", (payment_id,)).fetchall()
    chargebacks = conn.execute("SELECT amount, reason, reason_code, created_at FROM chargeback WHERE payment_id=%s ORDER BY id", (payment_id,)).fetchall()
    return {"payment": tx, "fraud": fa, "case": case, "three_ds": challenge, "webhooks": events, "chargebacks": chargebacks}


def list_cases(conn, status: str | None = "open", limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    return conn.execute(
        "SELECT fc.id, fc.status, fc.analyst_decision, fc.note, fc.resolved_by, fc.created_at, t.payment_id, t.amount, t.currency, "
        "t.fraud_score, t.merchant_id, fa.reasons, fa.tier3_state, fa.tier3_llm_analysis->>'summary' AS ai_summary, "
        "fa.tier3_llm_analysis->>'recommended_action' AS ai_recommendation, fa.tier3_llm_analysis->>'risk_category' AS ai_category "
        "FROM fraud_case fc JOIN transaction t ON t.id=fc.transaction_id LEFT JOIN fraud_assessment fa ON fa.transaction_id=t.id "
        "WHERE (%s::text IS NULL OR fc.status=%s) ORDER BY fc.created_at DESC LIMIT %s", (status, status, limit)).fetchall()


def list_rules(conn) -> list[dict]:
    return conn.execute("SELECT id, name, description, definition, action, enabled, created_by, created_at FROM fraud_rule ORDER BY id").fetchall()


def list_suggestions(conn, status: str | None = "pending") -> list[dict]:
    return conn.execute("SELECT id, name, definition, action, rationale, evidence, source, status, created_at, decided_at FROM rule_suggestion "
                        "WHERE (%s::text IS NULL OR status=%s) ORDER BY id DESC LIMIT 100", (status, status)).fetchall()
