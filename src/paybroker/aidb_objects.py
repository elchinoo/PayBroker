"""The database objects AIDB mode reads: two schemas of derived views and one table of case narratives. Plain SQL, no AIDB needed, so
they are created on every install (a hook after the migrations, like hardening.py) and cost nothing while AIDB mode is off.

    pb_ai    what the TRIAGE agent may read: `case_features` (one row per fraud assessment, derived numbers and codes only) and
             `case_narrative` (a short derived text per case, the source of the narrative-similarity pipeline).
    pb_ask   what the ANALYST-QUESTIONS agent may read: `payments`, `cases`, `merchants` (documented with COMMENT so a semantic knowledge
             base can find them by meaning). It is a separate schema on purpose: the semantic knowledge base indexes the whole schema it
             is given, so nothing outside these three views is ever described to a model.

What the views leave out is the point: no card token or fingerprint, no customer id, device id or IP address, no idempotency key, no
request body, no webhook URL or secret, no analyst free text (case note, resolver name), no merchant API key. The `features` column is
built from an explicit allow-list of names, so a feature added to the snapshot later stays out until someone lists it here.

The views are owned by the application role and run with its rights (they are not `security_invoker`), which is what lets the purpose
role read them without any privilege on the base tables. They still obey the row-level security of migration 008: a session that is not
the operator's (`app.bypass`) sees no row. Run as the schema owner: the privileges are granted by `paybroker aidb-setup`.
"""
import logging

import psycopg

log = logging.getLogger("paybroker.aidb_objects")

# Names the allow-list of the `features` column (the named features of apgkit/paybroker features.compute, plus name_mismatch).
SAFE_FEATURES = (
    "tx_count_1h", "tx_count_24h", "tx_sum_amount_24h_cents", "distinct_cards_used_7d", "avg_transaction_amount_30d_cents",
    "last_ip_country", "device_fingerprint_known", "historical_chargeback_count", "impossible_travel_flag", "ip_tx_count_1h",
    "device_tx_count_1h", "card_tx_count_1h", "device_linked_chargebacks", "new_device_for_account", "checkout_seconds",
    "pasted_card_number", "country_mismatch", "account_age_hours", "amount_vs_avg_ratio", "user_tx_count_1m", "ip_tx_count_1m",
    "card_tx_count_1m", "device_tx_count_1m", "name_mismatch",
)
_ALLOW = ", ".join("'" + n + "'" for n in SAFE_FEATURES)        # constants only

# Columns of the views, for the documentation, the tests and `paybroker aidb-status`.
VIEWS = {
    "pb_ai.case_features": ("assessment_id", "case_id", "case_status", "analyst_decision", "opened_at", "amount_minor_units", "currency",
                            "fraud_decision", "shipping_country", "ml_score", "rules_triggered", "reason_codes", "features",
                            "feature_vector", "ai_risk_category", "ai_recommended_action", "scored_at"),
    "pb_ask.payments": ("payment_id", "merchant_id", "amount_minor_units", "currency", "status", "capture_method", "fraud_score",
                        "fraud_decision", "psp", "decline_code", "amount_captured", "amount_refunded", "latency_ms", "created_at"),
    "pb_ask.cases": ("case_id", "payment_id", "status", "analyst_decision", "opened_at", "resolved_at", "ml_score", "rules_triggered",
                     "reason_codes", "ai_risk_category", "ai_recommended_action"),
    "pb_ask.merchants": ("merchant_id", "name", "status", "created_at"),
}

SQL = f"""
CREATE SCHEMA IF NOT EXISTS pb_ai;
CREATE SCHEMA IF NOT EXISTS pb_ask;

CREATE OR REPLACE VIEW pb_ai.case_features AS
SELECT fa.id AS assessment_id, fc.id AS case_id, fc.status AS case_status, fc.analyst_decision, fc.created_at AS opened_at,
       t.amount AS amount_minor_units, t.currency, t.fraud_decision, t.request->>'shipping_country' AS shipping_country,
       fa.tier2_ml_score AS ml_score, fa.tier1_rules_triggered AS rules_triggered, fa.reasons AS reason_codes,
       COALESCE((SELECT jsonb_object_agg(e.key, e.value) FROM jsonb_each(fa.tier2_feature_snapshot) AS e
                 WHERE e.key IN ({_ALLOW})), '{{}}'::jsonb) AS features,
       fa.feature_vector, fa.tier3_llm_analysis->>'risk_category' AS ai_risk_category,
       fa.tier3_llm_analysis->>'recommended_action' AS ai_recommended_action, fa.created_at AS scored_at
FROM fraud_assessment fa JOIN transaction t ON t.id = fa.transaction_id LEFT JOIN fraud_case fc ON fc.transaction_id = fa.transaction_id;

COMMENT ON VIEW pb_ai.case_features IS 'Derived fraud-scoring features per assessed payment: numbers, codes and countries only. No card, customer, device or IP identifiers.';

-- A short derived text per case (built from case_features by paybroker.aidb_features, never from raw payment data): the source of the
-- narrative-similarity pipeline. Deleting a case removes its narrative (the reset of the transaction data needs no extra step).
CREATE TABLE IF NOT EXISTS pb_ai.case_narrative (
    case_id    bigint PRIMARY KEY REFERENCES fraud_case(id) ON DELETE CASCADE,
    narrative  text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE VIEW pb_ask.payments AS
SELECT t.payment_id, t.merchant_id, t.amount AS amount_minor_units, t.currency, t.status, t.capture_method, t.fraud_score,
       t.fraud_decision, t.psp_selected AS psp, t.decline_code, t.amount_captured, t.amount_refunded, t.latency_ms, t.created_at
FROM transaction t;

CREATE OR REPLACE VIEW pb_ask.cases AS
SELECT fc.id AS case_id, t.payment_id, fc.status, fc.analyst_decision, fc.created_at AS opened_at, fc.resolved_at,
       fa.tier2_ml_score AS ml_score, fa.tier1_rules_triggered AS rules_triggered, fa.reasons AS reason_codes,
       fa.tier3_llm_analysis->>'risk_category' AS ai_risk_category, fa.tier3_llm_analysis->>'recommended_action' AS ai_recommended_action
FROM fraud_case fc JOIN transaction t ON t.id = fc.transaction_id LEFT JOIN fraud_assessment fa ON fa.transaction_id = fc.transaction_id;

CREATE OR REPLACE VIEW pb_ask.merchants AS
SELECT m.id AS merchant_id, m.name, m.status, m.created_at FROM merchant m;

COMMENT ON VIEW pb_ask.payments IS 'One row per payment attempt: amount, currency, status, the fraud score and decision, which payment service provider (PSP) handled it, and timing. No customer, card or device identifiers.';
COMMENT ON COLUMN pb_ask.payments.payment_id IS 'Payment identifier, joins to pb_ask.cases.payment_id.';
COMMENT ON COLUMN pb_ask.payments.merchant_id IS 'The merchant that took the payment, joins to pb_ask.merchants.merchant_id.';
COMMENT ON COLUMN pb_ask.payments.amount_minor_units IS 'Payment amount in minor currency units (cents): 12345 is 123.45.';
COMMENT ON COLUMN pb_ask.payments.status IS 'Lifecycle state: PROCESSING, REQUIRES_ACTION, AUTHORIZED, CAPTURED, PARTIALLY_REFUNDED, REFUNDED, VOIDED, DECLINED, FAILED.';
COMMENT ON COLUMN pb_ask.payments.fraud_score IS 'Tier 2 machine-learning fraud score from 0 (safe) to 1000 (almost certainly fraud).';
COMMENT ON COLUMN pb_ask.payments.fraud_decision IS 'What the fraud engine decided: APPROVE, CHALLENGE (3-D Secure step-up) or DECLINE.';
COMMENT ON COLUMN pb_ask.payments.psp IS 'The payment service provider (connector) that handled the authorization.';
COMMENT ON COLUMN pb_ask.payments.decline_code IS 'Why the payment was declined, when it was.';
COMMENT ON COLUMN pb_ask.payments.amount_captured IS 'Amount captured so far, minor units.';
COMMENT ON COLUMN pb_ask.payments.amount_refunded IS 'Amount refunded so far, minor units.';
COMMENT ON COLUMN pb_ask.payments.latency_ms IS 'End-to-end authorization time in milliseconds.';
COMMENT ON COLUMN pb_ask.payments.created_at IS 'When the payment attempt was made.';

COMMENT ON VIEW pb_ask.cases IS 'Fraud cases opened for payments the model scored 700 or more: status, the analyst decision, the rules and reason codes that fired, and the AI triage advice.';
COMMENT ON COLUMN pb_ask.cases.case_id IS 'Fraud case identifier.';
COMMENT ON COLUMN pb_ask.cases.payment_id IS 'The flagged payment, joins to pb_ask.payments.payment_id.';
COMMENT ON COLUMN pb_ask.cases.status IS 'open while waiting for an analyst, resolved once decided.';
COMMENT ON COLUMN pb_ask.cases.analyst_decision IS 'What the analyst decided: PERMANENT_BLOCK (confirmed fraud), ALLOW_WITH_KYC or DISMISS_ALERT (false positive).';
COMMENT ON COLUMN pb_ask.cases.ml_score IS 'Tier 2 fraud score of the flagged payment, 0 to 1000.';
COMMENT ON COLUMN pb_ask.cases.rules_triggered IS 'Tier 1 rules that fired.';
COMMENT ON COLUMN pb_ask.cases.reason_codes IS 'Reason codes behind the model score, for example NEW_DEVICE_FOR_ACCOUNT or HIGH_VELOCITY_IP.';
COMMENT ON COLUMN pb_ask.cases.ai_risk_category IS 'Advisory category from AI triage: ACCOUNT_TAKEOVER, CARD_TESTING, SYNTHETIC_IDENTITY or BENIGN_ANOMALY.';
COMMENT ON COLUMN pb_ask.cases.ai_recommended_action IS 'Advisory action from AI triage; the analyst decides.';

COMMENT ON VIEW pb_ask.merchants IS 'The merchants (shops) that send payments to the broker. No API keys, webhook URLs or secrets.';
COMMENT ON COLUMN pb_ask.merchants.merchant_id IS 'Merchant identifier.';
COMMENT ON COLUMN pb_ask.merchants.name IS 'Merchant display name.';
COMMENT ON COLUMN pb_ask.merchants.status IS 'active or suspended.';
"""

# The schema hook creates the table with its foreign key; a schema rebuilt underneath it (tests drop and recreate `public`) can leave the
# table without the key, so make sure it is there.
_FK = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'pb_ai.case_narrative'::regclass AND contype = 'f') THEN
        DELETE FROM pb_ai.case_narrative WHERE case_id NOT IN (SELECT id FROM fraud_case);      -- orphans of a rebuilt schema (derived data)
        ALTER TABLE pb_ai.case_narrative ADD FOREIGN KEY (case_id) REFERENCES fraud_case(id) ON DELETE CASCADE;
    END IF;
END $$;
"""


def apply(conn: psycopg.Connection) -> bool:
    """The after-migrate hook. Idempotent. A role that may not create schemas leaves AIDB mode unavailable (it is off by default) and
    must not stop the app: that is logged, `paybroker aidb-setup` and /readyz say so. Returns whether the objects are in place."""
    try:
        with conn.transaction():
            conn.execute(SQL)
            conn.execute(_FK)
        return True
    except psycopg.errors.InsufficientPrivilege as exc:
        log.warning("AIDB views not created (%s); AIDB mode stays unavailable until a role that may create schemas runs `paybroker migrate`",
                    str(exc).splitlines()[0][:160])
    except psycopg.Error as exc:
        log.warning("AIDB views not created: %s", str(exc).splitlines()[0][:200])
    return False


def present(conn: psycopg.Connection) -> bool:
    row = conn.execute("SELECT to_regclass('pb_ai.case_features') IS NOT NULL AND to_regclass('pb_ask.payments') IS NOT NULL "
                       "AND to_regclass('pb_ai.case_narrative') IS NOT NULL AS ok").fetchone()
    return bool(row and row["ok"])
