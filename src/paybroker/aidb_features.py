"""What AIDB mode does at run time (see aidb_mode.py for the switches and the setup): case narratives and their text retrieval, the triage
agent call, and analyst questions. Every public function here degrades to the `app` behaviour instead of raising into the caller.
"""
import json
import logging
import threading
import time
from dataclasses import dataclass

import psycopg
from apgkit import aidb, retrieval

from . import aidb_mode as mode
from .db import connect

log = logging.getLogger("paybroker.aidb")
MAX_ROWS = 200             # rows of an answer's table returned to the caller
MAX_SQL = 4000
AGENT_TIMEOUT = 90.0       # seconds, wall clock for one agent call (the worker's job lease is 120)


# ---- case narratives: a derived text per case ---------------------------------------------------------------------------------
def narrative(row: dict) -> str:
    """A short text for one pb_ai.case_features row, built from the row alone (so from derived values only). Deterministic: the same row gives
    the same text, and a changed case (a new triage verdict, an analyst decision) changes it."""
    f = row.get("features") or {}
    parts = [f"Payment of {row['amount_minor_units'] / 100:.2f} {row['currency']}, fraud score {row['ml_score']} of 1000, decision {row['fraud_decision']}."]
    signals: list[str] = []
    if f.get("new_device_for_account"):
        signals.append("new device for the account")
    if f.get("impossible_travel_flag"):
        signals.append("impossible travel between countries")
    if f.get("country_mismatch"):
        signals.append(f"IP country {f.get('last_ip_country') or 'unknown'} differs from shipping country {row.get('shipping_country') or 'unknown'}")
    if f.get("pasted_card_number"):
        signals.append("card number pasted at checkout")
    secs = f.get("checkout_seconds")
    if isinstance(secs, (int, float)) and secs < 10:
        signals.append(f"very fast checkout ({secs:g} seconds)")
    for key, label, floor in (("tx_count_1h", "payments from the account in the last hour", 5), ("ip_tx_count_1h", "payments from the IP address in the last hour", 5),
                              ("card_tx_count_1h", "payments with the card in the last hour", 3), ("device_tx_count_1h", "payments from the device in the last hour", 5),
                              ("distinct_cards_used_7d", "different cards on the account in 7 days", 3)):
        if isinstance(f.get(key), (int, float)) and f[key] >= floor:
            signals.append(f"{int(f[key])} {label}")
    if f.get("historical_chargeback_count"):
        signals.append(f"{int(f['historical_chargeback_count'])} earlier chargebacks")
    if f.get("device_linked_chargebacks"):
        signals.append("device linked to a chargeback")
    age = f.get("account_age_hours")
    if isinstance(age, (int, float)) and age < 24:
        signals.append("account younger than a day")
    ratio = f.get("amount_vs_avg_ratio")
    if isinstance(ratio, (int, float)) and ratio >= 3:
        signals.append(f"amount {ratio:g} times the account's usual spend")
    if signals:
        parts.append("Signals: " + "; ".join(signals) + ".")
    if row.get("reason_codes"):
        parts.append("Reason codes: " + ", ".join(row["reason_codes"]) + ".")
    if row.get("rules_triggered"):
        parts.append("Rules triggered: " + ", ".join(row["rules_triggered"]) + ".")
    if row.get("ai_risk_category"):
        parts.append(f"AI triage: {row['ai_risk_category']}" + (f", advice {row['ai_recommended_action']}" if row.get("ai_recommended_action") else "") + ".")
    if row.get("analyst_decision"):
        parts.append(f"Analyst decision: {row['analyst_decision']}.")
    return " ".join(parts)


_UPSERT = ("INSERT INTO pb_ai.case_narrative (case_id, narrative) VALUES (%s, %s) ON CONFLICT (case_id) DO UPDATE "
           "SET narrative = EXCLUDED.narrative, updated_at = now() WHERE pb_ai.case_narrative.narrative IS DISTINCT FROM EXCLUDED.narrative")


def _upsert(conn, row: dict) -> None:
    conn.execute(_UPSERT, (row["case_id"], narrative(row)))


def touch(conn, *, assessment_id=None, case_id=None) -> None:
    """Write (or refresh) the narrative of one case, in the caller's transaction, when an AIDB feature that reads narratives is on. A failure
    is logged and never reaches the caller (the narrative is derived; the triage or the resolution it rides on must not fail for it)."""
    if not mode.narratives_on():
        return
    try:
        with conn.transaction():
            row = conn.execute("SELECT * FROM pb_ai.case_features WHERE (%s::uuid IS NULL OR assessment_id = %s) AND (%s::bigint IS NULL OR case_id = %s) "
                               "AND case_id IS NOT NULL", (assessment_id, assessment_id, case_id, case_id)).fetchone()
            if row:
                _upsert(conn, row)
    except psycopg.Error as exc:
        log.warning("case narrative not written: %s", str(exc).splitlines()[0][:160])


def backfill_narratives(conn) -> int:
    """Narratives for every case that has none or an outdated one (the operator setup runs this; repeating it changes nothing). Returns the
    number of cases looked at. Caller commits."""
    rows = conn.execute("SELECT * FROM pb_ai.case_features WHERE case_id IS NOT NULL").fetchall()
    for r in rows:
        _upsert(conn, r)
    return len(rows)


# ---- retrieval: numeric neighbours beside narrative neighbours -----------------------------------------------------------------
_selected: dict = {"t": 0.0, "r": None, "backend": ""}
_selected_lock = threading.Lock()


def app_retrieval() -> retrieval.AppRetrieval:
    return retrieval.AppRetrieval("pb_ai.case_narrative", key_column="case_id", text_column="narrative")


def reset_retriever() -> None:
    with _selected_lock:
        _selected.update(t=0.0, r=None, backend="")


def retriever(conn) -> retrieval.Retrieval:
    """The backend PAYBROKER_RETRIEVAL_BACKEND names, when it is usable (AIDB there, set up, seals intact, the knowledge base exists); otherwise
    the app backend, with an ERROR in the log naming why. Chosen once per mode.TTL seconds, not per request."""
    want = mode.retrieval_backend()
    with _selected_lock:
        if _selected["r"] is not None and _selected["backend"] == want and time.monotonic() - _selected["t"] < mode.TTL:
            return _selected["r"]
    app = app_retrieval()
    chosen: retrieval.Retrieval = app
    if want == "aidb":
        ok, why = mode.usable("retrieval", conn)
        if not ok:
            log.error("RETRIEVAL BACKEND FALLBACK: PAYBROKER_RETRIEVAL_BACKEND=aidb but %s; using the app backend", why)
            mode.calls.labels("retrieval", "fallback").inc()
        else:
            chosen = retrieval.select("PAYBROKER", app=app, aidb=retrieval.AidbRetrieval(mode.KB), connect=connect)
    with _selected_lock:
        _selected.update(t=time.monotonic(), r=chosen, backend=want)
    return chosen


def similar(conn, case_id: int, k: int = 5, query: str | None = None) -> dict | None:
    """Past cases like this one, twice: by feature distance (the numeric nearest neighbours, as triage has always used them) and by narrative
    (text retrieval over the case narratives, from the backend the switch names). None when there is no such case."""
    from . import triage
    row = conn.execute("SELECT fa.transaction_id, fa.feature_vector, fc.id AS case_id FROM fraud_case fc "
                       "JOIN fraud_assessment fa ON fa.transaction_id = fc.transaction_id WHERE fc.id = %s", (case_id,)).fetchone()
    if not row:
        return None
    numeric = triage.similar_cases(conn, row["transaction_id"], row["feature_vector"], k) if row["feature_vector"] is not None else []
    text = query
    if not text:
        n = conn.execute("SELECT narrative FROM pb_ai.case_narrative WHERE case_id = %s", (case_id,)).fetchone()
        text = n["narrative"] if n else None
    out = {"case_id": case_id, "numeric": {"method": "feature vector distance (pgvector, cosine)", "items": numeric},
           "narrative": {"backend": "app", "query": text, "items": []}}
    if not text:
        out["narrative"]["note"] = "this case has no narrative yet (narratives are written when an AIDB feature is switched on, or by `paybroker aidb-setup`)"
        return out
    hits, backend, fallback = [], "app", ""
    try:
        r = retriever(conn)
        backend = r.name
        hits = r.retrieve(conn, text, k + 1)
    except Exception as exc:  # noqa: BLE001  (AIDB failing at query time must not take the page down: the app backend answers)
        conn.rollback()
        fallback = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120] if str(exc) else ''}"
        log.error("RETRIEVAL BACKEND FALLBACK: %s failed at query time (%s); using the app backend", backend, fallback)
        mode.calls.labels("retrieval", "error").inc()
        backend = "app"
        hits = app_retrieval().retrieve(conn, text, k + 1)
    keys = [h.key for h in hits if h.key != str(case_id)][:k]
    decisions = {str(r["id"]): r["analyst_decision"] for r in conn.execute(
        "SELECT id, analyst_decision FROM fraud_case WHERE id = ANY(%s)", ([int(x) for x in keys],)).fetchall()} if keys else {}
    items = [{"case_id": int(h.key), "score": round(h.score, 4), "distance": None if h.distance is None else round(h.distance, 4),
              "analyst_decision": decisions.get(h.key), "narrative": h.text} for h in hits if h.key in keys]
    out["narrative"] = {"backend": backend, "query": text, "items": items} | ({"fallback": fallback} if fallback else {})
    return out


# ---- the triage agent ---------------------------------------------------------------------------------------------------------
@dataclass
class AgentCall:
    text: str | None
    conversation_id: str | None
    error: str = ""
    ms: int = 0


def call_agent(conn, agent: str, prompt: str, conversation_id: str | None = None) -> AgentCall:
    """One `aidb.agent_converse`, bounded in time. Never raises: a failure is in `error` (first line only, no SQL, no values)."""
    t0 = time.monotonic()
    try:
        conn.commit()                                        # nothing of the caller's stays open across a call that can take a minute
        reply = aidb.converse(conn, agent, prompt, conversation_id=conversation_id, timeout=AGENT_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return AgentCall(None, None, f"{type(exc).__name__}", int((time.monotonic() - t0) * 1000))
    ms = int((time.monotonic() - t0) * 1000)
    if reply.error or reply.message is None:
        return AgentCall(None, reply.conversation_id, (reply.error or "the agent returned no message").strip().splitlines()[0][:160], ms)
    return AgentCall(reply.message, reply.conversation_id, "", ms)


def triage_prompt(assessment_id) -> str:
    return (f"Triage the assessed payment with assessment_id {assessment_id}. Use the tools pb_get_case and pb_nearest_cases with that "
            "assessment_id, then answer with the JSON object only.")


# ---- analyst questions --------------------------------------------------------------------------------------------------------
_ask_slots = threading.BoundedSemaphore(2)       # each question can cost a model call and a minute of a database session


class AskRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def executed(conn, conversation_id: str | None, since) -> list[dict]:
    """What the agent actually did in this call, from AIDB's action log (the runtime role reads its own rows there, nothing else): one dict per
    tool call in order: {"tool", "sql" (run_sql_query only), "ok", "rows" (run_sql_query only), "error"}. Empty when the log cannot be read.
    AIDB's built-in `sleep` is logged as `sleep`/`wake_up` instead of `tool_call`/`tool_response`; both shapes are read."""
    if not conversation_id:
        return []
    try:
        rows = conn.execute("SELECT action_type, request_payload, response_payload FROM aidb_internal.action_log "
                            "WHERE conversation_id = %s::uuid AND action_type IN ('tool_call', 'tool_response', 'sleep', 'wake_up') AND created_at >= %s "
                            "ORDER BY created_at, id", (conversation_id, since)).fetchall()
        conn.commit()
    except psycopg.Error as exc:
        conn.rollback()
        log.warning("the agent's action log could not be read: %s", str(exc).splitlines()[0][:120])
        return []
    calls: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        if r["action_type"] in ("tool_call", "sleep"):
            p = r["request_payload"] or {}
            cid = p.get("call_id") or f"n{len(order)}"
            calls[cid] = {"tool": p.get("tool_name"), "sql": (p.get("arguments") or {}).get("query") if p.get("tool_name") == "run_sql_query" else None,
                          "ok": None, "rows": None, "error": ""}
            order.append(cid)
        else:
            p = r["response_payload"] or {}
            c = calls.get(p.get("call_id"))
            if c is None:
                continue
            if p.get("error"):
                c["ok"], c["error"] = False, str(p["error"]).splitlines()[0][:300]
            else:
                c["ok"] = True
                if c["tool"] == "run_sql_query":
                    try:
                        parsed = json.loads(p.get("contents") or "[]")
                        c["rows"] = parsed if isinstance(parsed, list) else [parsed]
                    except ValueError:
                        c["rows"], c["error"] = None, "the result could not be parsed"
    return [calls[c] for c in order]


def denied_calls(ran: list[dict]) -> int:
    """How many of the agent's tool calls the database or AIDB refused (each is one `denied` decision in the governance trail)."""
    return sum(1 for c in ran if c["ok"] is False)


RUN_THE_QUERY = ("You gave an answer without running a query, so there are no rows behind it. Run your read-only SELECT now with the run_sql_query "
                 "tool, then answer from the rows it returns. Do not write the SQL as your answer.")


def ask(conn, question: str, actor: str, conversation_id: str | None = None) -> dict:
    """Answer an analyst's question with the AIDB agent (semantic search over the three analyst views, then read-only SQL under a purpose that
    can read those views and nothing else). The SQL and the rows in the response are what really ran, read back from AIDB's action log, never
    what the model says it ran. A model that writes SQL as its answer without running it is told once to run it; if it still does not, the
    response says `executed: false` with no answer, and the model's text is returned only as `unverified_text`. Every question is recorded in
    the audit ledger (a short hash and the length of the question, never its text: an analyst may type an identifier)."""
    from . import audit
    if mode.analyst_qa() != "aidb":
        raise AskRefused(503, "analyst questions are switched off (set PAYBROKER_ANALYST_QA=aidb after `paybroker aidb-setup`)")
    ok, why = mode.usable("ask", conn)
    if not ok:
        mode.calls.labels("ask", "refused").inc()
        raise AskRefused(503, f"analyst questions are not available: {why}")
    if not _ask_slots.acquire(blocking=False):
        raise AskRefused(429, "two questions are already being answered; try again in a moment")
    retried = False
    try:
        since = conn.execute("SELECT clock_timestamp() - interval '2 seconds' AS t").fetchone()["t"]
        call = call_agent(conn, mode.ASK_AGENT, question, conversation_id)
        ran = executed(conn, call.conversation_id, since) if call.text else []
        if call.text and not any(c["tool"] == "run_sql_query" for c in ran):
            retried = True                          # one bounded retry: the model answered (often with SQL as text) but never used the tool
            nudge = RUN_THE_QUERY if call.conversation_id else f"{question}\n\n{RUN_THE_QUERY}"
            again = call_agent(conn, mode.ASK_AGENT, nudge, call.conversation_id)
            call = AgentCall(again.text or call.text, again.conversation_id or call.conversation_id, again.error if not again.text else "",
                             call.ms + again.ms)
            ran = executed(conn, call.conversation_id, since)
    finally:
        _ask_slots.release()
    sql_runs = [c for c in ran if c["tool"] == "run_sql_query"]
    good = [c for c in sql_runs if c["ok"] and c["rows"] is not None]
    last = good[-1] if good else (sql_runs[-1] if sql_runs else None)
    rows = (last["rows"] or [])[:MAX_ROWS] if last else []
    outcome = "error" if not call.text else "no_query" if last is None else "sql_error" if not last["ok"] else "ok"
    mode.calls.labels("ask", outcome).inc()
    audit.record(conn, actor, "analyst.ask", "", {"agent": mode.ASK_AGENT, "question": audit.short_hash(question), "chars": len(question),
                                                   "outcome": outcome, "conversation_id": call.conversation_id, "ms": call.ms, "steps": len(ran),
                                                   "rows": len(rows), "retried": retried, "denied_calls": denied_calls(ran),
                                                   **({"error": call.error} if call.error else {})})
    conn.commit()
    if not call.text:
        raise AskRefused(502, f"the agent could not answer: {call.error}")
    out = {"executed": last is not None, "retried": retried, "conversation_id": call.conversation_id, "agent": mode.ASK_AGENT, "backend": "aidb",
           "steps": [{"tool": c["tool"], "ok": c["ok"]} for c in ran]}
    if last is None:
        # The model produced text but no query ran: that text is NOT an answer. It is returned for the analyst to read, labelled as unverified.
        return out | {"answer": None, "sql": None, "rows": [], "row_count": 0, "truncated": False,
                      "sql_error": "the agent did not run a query, so there is no answer from the data; rephrase the question or try again",
                      "unverified_text": call.text}
    return out | {"answer": call.text, "sql": last["sql"][:MAX_SQL] if last["sql"] else None, "rows": rows, "row_count": len(last["rows"] or []),
                  "truncated": len(last["rows"] or []) > MAX_ROWS, "sql_error": last["error"] if not last["ok"] else ""}
