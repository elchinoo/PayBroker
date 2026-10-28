"""Background worker: the Kafka consumer of the reference design, on Postgres.

The `outbox` table is the queue. Rows are claimed with FOR UPDATE SKIP LOCKED and leased by pushing `available_at` forward,
so several workers can run and a crashed one's work comes back. Handlers: webhook delivery, Tier 3 triage and rule
synthesis. Connector statistics are flushed to the database on the same loop.
"""
import threading
import time
from datetime import UTC, datetime

from . import metrics as m
from . import psp, suggest, training_data, triage, triage_limits, webhooks
from . import settings_store as store
from .db import connect
from .llm_http import LLMError

LEASE_SECONDS = 120
MAX_ATTEMPTS = {"webhook": webhooks.ATTEMPTS, "fraud.triage": 3, "fraud.rules": 2}
RETRY_SECONDS = {"fraud.triage": [30, 300, 1800], "fraud.rules": [300, 3600]}
SYNTH_EVERY = 24 * 3600
PURGE_EVERY = 600


def claim(conn, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "UPDATE outbox SET attempts = attempts + 1, available_at = now() + %s * interval '1 second' WHERE id IN ("
        "SELECT id FROM outbox WHERE done_at IS NULL AND available_at <= now() ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED) "
        "RETURNING id, topic, payload, attempts, created_at", (LEASE_SECONDS, limit)).fetchall()
    conn.commit()
    return rows


def _age(job: dict) -> float:
    """Seconds since the job was queued (0 when unknown)."""
    created = job.get("created_at")
    return max(0.0, (datetime.now(UTC) - created).total_seconds()) if created else 0.0


def _finish(conn, job: dict, error: str | None = None, retry_in: float | None = None, free_attempt: bool = False) -> None:
    if retry_in is not None:
        conn.execute("UPDATE outbox SET available_at = now() + %s * interval '1 second', last_error=%s, attempts = attempts - %s "
                     "WHERE id=%s", (retry_in, error, 1 if free_attempt else 0, job["id"]))
    else:
        conn.execute("UPDATE outbox SET done_at = now(), last_error=%s WHERE id=%s", (error, job["id"]))
    conn.commit()


def handle(conn, job: dict) -> str:
    topic, payload, attempts = job["topic"], job["payload"], job["attempts"]
    if topic == "webhook":
        outcome, detail = webhooks.deliver(conn, payload["event_id"])
        m.WEBHOOKS.labels(outcome).inc()
        if outcome == "locked":
            _finish(conn, job, detail, retry_in=60, free_attempt=True)     # waiting for an unlock is not a failed attempt
        elif outcome == "retry":
            _finish(conn, job, detail, retry_in=webhooks.BACKOFF[min(attempts - 1, len(webhooks.BACKOFF) - 1)])
        else:
            _finish(conn, job, None if outcome in {"delivered", "skipped"} else detail)
        return outcome
    if topic == "fraud.triage":
        try:
            outcome = triage.run(conn, payload["transaction_id"], _age(job))
        except triage.Deferred as wait:
            _finish(conn, job, wait.reason, retry_in=wait.delay, free_attempt=True)     # throttled or paused: not a failed attempt
            return "deferred"
        except LLMError as exc:
            if triage_limits.http_status(str(exc)) == 429:      # the gateway's rate limit or token budget, not a fault of this job
                _finish(conn, job, str(exc), retry_in=triage_limits.on_rate_limited(conn, str(exc)), free_attempt=True)
                return "rate_limited"
            if attempts >= MAX_ATTEMPTS[topic]:
                triage.give_up(conn, payload["transaction_id"], str(exc))
                _finish(conn, job, str(exc))
                return "gave_up"
            _finish(conn, job, str(exc), retry_in=RETRY_SECONDS[topic][min(attempts - 1, 2)])
            return "retry"
        if outcome == "locked":
            _finish(conn, job, "vault locked", retry_in=60, free_attempt=True)
        else:
            _finish(conn, job)
        return outcome
    if topic == "fraud.rules":
        res = suggest.run(conn)
        if res["source"] == "locked":
            _finish(conn, job, "vault locked", retry_in=60, free_attempt=True)
        else:
            _finish(conn, job)
        return res["source"]
    _finish(conn, job, "unknown topic")
    return "unknown"


def run_once(conn) -> int:
    jobs = claim(conn)
    for job in jobs:
        try:
            handle(conn, job)
        except Exception as exc:  # noqa: BLE001 - one bad job must never stop the queue
            conn.rollback()
            if job["attempts"] >= MAX_ATTEMPTS.get(job["topic"], 3):
                _finish(conn, job, f"gave up: {type(exc).__name__}")
            else:
                _finish(conn, job, f"{type(exc).__name__}", retry_in=60)
    return len(jobs)


def maybe_schedule_rule_synthesis(conn) -> bool:
    """Once a day, queue a rule-synthesis job if there has been any flagged traffic."""
    last = store.get(conn, "last_rule_synthesis")
    if last and time.time() - int(last) < SYNTH_EVERY:
        return False
    flagged = conn.execute("SELECT count(*) AS n FROM fraud_assessment WHERE tier2_ml_score >= 500 AND created_at > now() - interval '1 day'").fetchone()["n"]
    if not flagged:
        return False
    store.put(conn, "last_rule_synthesis", str(int(time.time())))   # claim the slot first so workers do not double-queue
    conn.execute("INSERT INTO outbox (topic, payload) VALUES ('fraud.rules', '{}')")
    conn.commit()
    return True


def loop(stop: threading.Event, interval: float = 1.0) -> None:
    last_flush = last_sched = 0.0
    last_purge = float("-inf")      # the first pass purges at once
    while not stop.is_set():
        try:
            with connect(retries=1) as conn:
                psp.load_stats(conn)
                n = run_once(conn)
                if time.monotonic() - last_flush > 5:
                    psp.flush(conn)
                    last_flush = time.monotonic()
                if time.monotonic() - last_sched > 600:
                    maybe_schedule_rule_synthesis(conn)
                    last_sched = time.monotonic()
                if time.monotonic() - last_purge > PURGE_EVERY:
                    triage_limits.purge(conn)
                    training_data.purge(conn)       # retention and the row cap hold even while capture is switched off
                    last_purge = time.monotonic()
        except Exception:  # noqa: BLE001 - the database may be asleep; try again
            n = 0
        stop.wait(0.05 if n else interval)


def flush_loop(stop: threading.Event) -> None:
    """Role `api` runs no queue worker but still has to persist its connector statistics."""
    while not stop.wait(5.0):
        try:
            with connect(retries=1) as conn:
                psp.flush(conn)
        except Exception:  # noqa: BLE001
            pass
