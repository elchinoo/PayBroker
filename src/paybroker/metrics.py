"""Prometheus metrics. The shared family names (http_requests_total, http_request_duration_seconds, db_up, app_info,
process_*) come from agentpg-kit, so one PagerLens agent preset reads every app; the broker's own are added here."""
from prometheus_client import Counter, Histogram

from . import triage_limits
from .core import db, metrics

APP = metrics.app
REGISTRY = metrics.registry
observe = metrics.observe

PAYMENTS = Counter("payment_broker_requests_total", "Payment attempts by outcome and connector", ["status", "psp"], registry=REGISTRY)
FRAUD_SCORE = Histogram("payment_broker_fraud_score_distribution", "Tier 2 model score (0-1000)", registry=REGISTRY,
                        buckets=(50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000))
STEP = Histogram("payment_broker_latency_seconds", "Latency by pipeline step", ["step"], registry=REGISTRY,
                 buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.035, 0.05, 0.1, 0.18, 0.3, 0.8, 2))
TRIAGE = Counter("payment_broker_triage_total", "Tier 3 triage runs", ["source", "result"], registry=REGISTRY)
TRIAGE_CALLS = Counter("payment_broker_triage_calls_total", "Tier 3 triage decisions by outcome (llm, error, gateway_429, throttled, capped, grouped, paused, paused_fallback)",
                       ["outcome"], registry=REGISTRY)
WEBHOOKS = Counter("payment_broker_webhooks_total", "Webhook deliveries", ["result"], registry=REGISTRY)
FALLBACKS = Counter("payment_broker_psp_fallbacks_total", "Payments rescued by cascading to another connector", registry=REGISTRY)


def _probe() -> dict[str, float]:
    """Evaluated at scrape time: database reachable, work waiting, cases open."""
    with db.connect(retries=1) as conn:
        backlog = conn.execute("SELECT count(*) AS n FROM outbox WHERE done_at IS NULL").fetchone()["n"]
        cases = conn.execute("SELECT count(*) AS n FROM fraud_case WHERE status='open'").fetchone()["n"]
        use = triage_limits.usage(conn)
        held = conn.execute("SELECT count(*) AS n FROM transaction WHERE status='REQUIRES_ACTION'").fetchone()["n"]
    return {"db_up": 1, "payment_broker_outbox_backlog": backlog, "payment_broker_open_fraud_cases": cases,
            "payment_broker_payments_awaiting_3ds": held,
            "payment_broker_triage_queue_depth": use["queued"], "payment_broker_triage_llm_calls_24h": use["calls_24h"],
            "payment_broker_triage_circuit_open": int(use["circuit"]["open"])}


metrics.add_db_collector(_probe)
metrics.add_insights_collector(db.connect)   # db_insights_* gauges (EDB Stat Monitor, EDB Postgres Tuner); a scrape never fails


def render() -> tuple[bytes, str]:
    return metrics.render()
