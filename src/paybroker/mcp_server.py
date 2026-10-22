"""MCP surface for operators and analyst agents: read-only. Anything that changes data (resolving a case, approving a rule)
needs a person in the UI or the REST API, so an agent can investigate but never decide."""
from mcp.server.mcpserver import MCPServer

from . import insights
from .db import connect

mcp = MCPServer(
    "paybroker",
    instructions=(
        "PayBroker is a payment broker with a three-tier fraud engine (rules, a gradient-boosted model scoring 0-1000, and "
        "asynchronous LLM triage). These tools only read: use them to investigate payments and flagged cases, and to explain "
        "why a payment was approved, challenged or declined. Scores: under 300 approve, 300-699 3-D Secure, 700 and above decline."
    ),
)


@mcp.tool()
def get_overview(days: int = 7) -> dict:
    """Payments, approval rate, fraud decisions, latency, connector split, open cases and queue backlog for the last N days (1-90)."""
    with connect() as conn:
        return insights.overview(conn, days)


@mcp.tool()
def search_payments(status: str | None = None, merchant_id: str | None = None, min_score: int | None = None, decision: str | None = None,
                    query: str | None = None, limit: int = 25) -> list[dict]:
    """Find payments. status: one of AUTHORIZED, CAPTURED, DECLINED, REQUIRES_ACTION...; decision: APPROVE, CHALLENGE or DECLINE;
    min_score 0-1000; query matches the payment id, customer id or idempotency key."""
    with connect() as conn:
        return insights.search_payments(conn, status=status, merchant_id=merchant_id, min_score=min_score, decision=decision,
                                        q=query, limit=limit)


@mcp.tool()
def explain_payment(payment_id: str) -> dict:
    """Everything about one payment: the request, the rules that fired, the model score with its reasons and feature snapshot,
    the LLM triage (if done), the routing outcome, any case, 3-D Secure state, webhooks and chargebacks."""
    with connect() as conn:
        return insights.payment_detail(conn, payment_id) or {"error": "payment not found"}


@mcp.tool()
def list_fraud_cases(status: str = "open", limit: int = 50) -> list[dict]:
    """Flagged transactions awaiting (or past) an analyst decision, with the AI triage summary and recommendation."""
    with connect() as conn:
        return insights.list_cases(conn, None if status == "all" else status, limit)


@mcp.tool()
def list_fraud_rules() -> list[dict]:
    """The active and disabled Tier 1 rules (JSON definitions)."""
    with connect() as conn:
        return insights.list_rules(conn)


@mcp.tool()
def list_rule_suggestions(status: str = "pending") -> list[dict]:
    """Rules proposed by the LLM or the heuristic; they only take effect when a person approves them."""
    with connect() as conn:
        return insights.list_suggestions(conn, None if status == "all" else status)
