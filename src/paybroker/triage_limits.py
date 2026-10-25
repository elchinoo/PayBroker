"""Throttle for Tier 3: how many LLM triage calls PayBroker lets itself make, and what a case gets when the answer is "not now".

The shared LLM gateway enforces a request rate (60 per minute) and a hard monthly token budget per app. A card-testing flood
flags thousands of payments an hour, and without a limit here every one of them became a gateway call. Four settings keep the
broker well inside both, and each one degrades to the deterministic heuristic triage instead of failing:

* `triage_rpm`           token bucket, calls per minute per worker process (0 = unlimited). No free slot: the job waits.
* `triage_daily_cap`     calls that used the LLM in the last 24 hours (0 = unlimited). At the cap: heuristic, at once.
* `triage_group_minutes` a case whose device (else IP) was already triaged by the LLM that recently gets the heuristic (0 = off).
* `triage_pause_minutes` after 3 gateway 429s in a row, LLM triage pauses this long (circuit breaker).

The stored value wins and the environment (PAYBROKER_TRIAGE_*) supplies the default, as for the training-data switch. The
decision itself is a pure function (`decide`) with the clock and the lookups injected, so it is tested without sleeping.
"""
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from . import settings_store as store

KEYS = {"rpm": "triage_rpm", "daily_cap": "triage_daily_cap", "group_minutes": "triage_group_minutes", "pause_minutes": "triage_pause_minutes"}
ENV = {"rpm": "PAYBROKER_TRIAGE_RPM", "daily_cap": "PAYBROKER_TRIAGE_DAILY_CAP", "group_minutes": "PAYBROKER_TRIAGE_GROUP_MINUTES",
       "pause_minutes": "PAYBROKER_TRIAGE_PAUSE_MINUTES"}
DEFAULTS = {"rpm": 20, "daily_cap": 120, "group_minutes": 30, "pause_minutes": 10}
BOUNDS = {"rpm": (0, 600), "daily_cap": (0, 1_000_000), "group_minutes": (0, 1440), "pause_minutes": (1, 1440)}
KEY_STREAK, KEY_PAUSED_UNTIL = "triage_429_streak", "triage_paused_until"     # breaker state: shared by every worker, survives restarts

BURST = 5                       # the bucket never holds more than this many calls, so a restart cannot spend a minute's allowance at once
BREAKER_AFTER = 3               # consecutive gateway 429s that open the breaker
STALE_BACKLOG = 3600            # while paused, a job older than this gets the heuristic instead of waiting
BACKOFF_FIRST, BACKOFF_MAX = 60, 900
RETRY_AFTER_MAX = 3600
THROTTLE_MIN_DELAY, THROTTLE_JITTER = 5.0, 30.0
CALL_KEEP_DAYS = 7              # triage_llm_call rows older than this are purged

LLM_OUTCOMES = ("ok", "error")  # the rows that count against the cap: the call reached the model (a 429 never did)


class LimitsError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    rpm: int
    daily_cap: int
    group_minutes: int
    pause_minutes: int


@dataclass(frozen=True)
class Verdict:
    action: str             # llm | heuristic | defer
    outcome: str            # the metric label: llm, capped, grouped, paused, paused_fallback, throttled
    note: str = ""          # stored with a heuristic result, so the analyst sees why there is no AI analysis
    delay: float = 0.0      # defer: seconds until the job may run again


# ---- token bucket ---------------------------------------------------------------------------------------------------
class Bucket:
    """Calls per minute, refilled continuously. Per process: with N workers the effective rate is N times `rpm`."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock, self.rpm, self.tokens, self.at = clock, 0, 0.0, clock()

    def capacity(self) -> float:
        return float(max(1, min(self.rpm, BURST)))

    def take(self, rpm: int) -> float:
        """0.0 when a slot was taken (or `rpm` is 0: unlimited), else the seconds until one is free."""
        if rpm <= 0:
            return 0.0
        now = self.clock()
        if rpm != self.rpm:     # a changed setting applies at once; a raised one starts with a full bucket
            raised = rpm > self.rpm
            self.rpm = rpm
            self.tokens = self.capacity() if raised else min(self.tokens, self.capacity())
        self.tokens = min(self.capacity(), self.tokens + (now - self.at) * rpm / 60.0)
        self.at = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        return (1.0 - self.tokens) * 60.0 / rpm


BUCKET = Bucket()


# ---- the decision ---------------------------------------------------------------------------------------------------
def decide(limits: Limits, *, now: float, paused_until: float, job_age: float, used_24h: Callable[[], int],
           grouped: Callable[[], tuple[str, str] | None], take: Callable[[], float],
           rand: Callable[[], float] = random.random) -> Verdict:
    """What to do with one case that is about to be sent to the model. The checks run from cheapest to most specific, and the
    bucket is last so a slot is only spent on a call that will really be made."""
    if now < paused_until:
        if job_age > STALE_BACKLOG:
            return Verdict("heuristic", "paused_fallback",
                           "AI triage is paused after gateway rate limits and this case waited over an hour; this is the deterministic fallback.")
        return Verdict("defer", "paused", "AI triage paused after gateway rate limits", max(1.0, paused_until - now))
    if limits.daily_cap > 0:
        used = used_24h()
        if used >= limits.daily_cap:
            return Verdict("heuristic", "capped", f"AI triage daily cap reached ({used} of {limits.daily_cap} calls in 24 hours).")
    if limits.group_minutes > 0:
        hit = grouped()
        if hit:
            ref, kind = hit
            return Verdict("heuristic", "grouped", f"Grouped with {ref}: the same {kind} was already triaged by the LLM within {limits.group_minutes} minutes.")
    wait = take()
    if wait > 0:
        return Verdict("defer", "throttled", "AI triage rate limit: waiting for a free slot",
                       max(wait, THROTTLE_MIN_DELAY) + rand() * THROTTLE_JITTER)
    return Verdict("llm", "llm")


# ---- gateway errors -------------------------------------------------------------------------------------------------
_STATUS = re.compile(r"returned HTTP (\d{3})")
_RETRY_AFTER = re.compile(r"retry[-_ ]?after\D{0,12}(\d+(?:\.\d+)?)|try again in (\d+(?:\.\d+)?) ?s", re.IGNORECASE)


def http_status(message: str) -> int | None:
    """apgkit raises LLMError(f"chat endpoint returned HTTP {status}: {body}") and exposes no status attribute, so read it from there."""
    m = _STATUS.search(message or "")
    return int(m.group(1)) if m else None


def retry_after(message: str) -> float | None:
    """Seconds the gateway asked us to wait, when its error text says so (the header itself is not kept by the client)."""
    m = _RETRY_AFTER.search(message or "")
    if not m:
        return None
    return min(float(m.group(1) or m.group(2)), RETRY_AFTER_MAX) or None


def backoff_seconds(streak: int, asked: float | None = None) -> float:
    """How long a job waits after its `streak`-th consecutive 429: what the gateway asked for, else 60 s doubling up to 15 min."""
    if asked:
        return asked
    return float(min(BACKOFF_MAX, BACKOFF_FIRST * 2 ** max(0, min(streak, 10) - 1)))


# ---- settings -------------------------------------------------------------------------------------------------------
def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return v if lo <= v <= hi else default


def _stored(rows: dict, field: str) -> int:
    key = KEYS[field]
    lo, hi = BOUNDS[field]
    if key in rows:
        try:
            v = int(rows[key])
            if lo <= v <= hi:
                return v
        except ValueError:
            pass
    return _env_int(ENV[field], DEFAULTS[field], lo, hi)


def _rows(conn, *keys: str) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_setting WHERE key = ANY(%s)", (list(keys),))}


def limits(conn) -> Limits:
    rows = _rows(conn, *KEYS.values())
    return Limits(**{f: _stored(rows, f) for f in KEYS})


def settings(conn) -> dict:
    """The limits, where each came from (setting | environment | default), and what is in use right now."""
    rows = _rows(conn, *KEYS.values())
    out = {f: _stored(rows, f) for f in KEYS}
    out["sources"] = {f: "setting" if KEYS[f] in rows else ("environment" if os.environ.get(ENV[f], "") != "" else "default") for f in KEYS}
    return {**out, "usage": usage(conn)}


def update_settings(conn, *, actor: str, **values: int | None) -> dict:
    """Validate, store, and leave one admin_audit row (what changed, from what to what). Raises LimitsError."""
    changes = {f: v for f, v in values.items() if v is not None}
    for f, v in changes.items():
        lo, hi = BOUNDS[f]
        if not lo <= v <= hi:
            raise LimitsError(f"{f} must be between {lo} and {hi}")
    before = limits(conn)
    for f, v in changes.items():
        store.put(conn, KEYS[f], str(v))
    conn.execute("INSERT INTO admin_audit (actor, action, detail) VALUES (%s,%s,%s)", (
        actor, "triage.settings", Jsonb({f: {"from": getattr(before, f), "to": v} for f, v in changes.items()})))
    conn.commit()
    return settings(conn)


# ---- state: usage, breaker, calls -----------------------------------------------------------------------------------
def breaker(conn, now: float | None = None) -> dict:
    rows = _rows(conn, KEY_STREAK, KEY_PAUSED_UNTIL)
    until = float(rows.get(KEY_PAUSED_UNTIL) or 0)
    now = time.time() if now is None else now
    return {"open": now < until, "paused_until": until if now < until else None, "consecutive_429": int(rows.get(KEY_STREAK) or 0)}


def calls_24h(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM triage_llm_call WHERE at > now() - interval '24 hours' AND outcome = ANY(%s)",
                        (list(LLM_OUTCOMES),)).fetchone()["n"]


def queued(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM outbox WHERE topic='fraud.triage' AND done_at IS NULL").fetchone()["n"]


def usage(conn) -> dict:
    return {"calls_24h": calls_24h(conn), "queued": queued(conn), "circuit": breaker(conn)}


def record_call(conn, transaction_id: str, outcome: str) -> None:
    conn.execute("INSERT INTO triage_llm_call (transaction_id, outcome) VALUES (%s,%s)", (transaction_id, outcome))


def grouped_with(conn, tx: dict, minutes: int) -> tuple[str, str] | None:
    """The payment reference of another case with the same device (else the same IP) that the LLM triaged within `minutes`.
    Uses the identifiers already on the payment row; nothing new is stored."""
    req = tx["request"] or {}
    kind, value = ("device", req.get("device")) if req.get("device") else ("IP", req.get("ip"))
    if not value:
        return None
    row = conn.execute(
        "SELECT t.payment_id FROM triage_llm_call c JOIN transaction t ON t.id = c.transaction_id WHERE c.outcome = 'ok' "
        "AND c.at > now() - %s * interval '1 minute' AND c.transaction_id <> %s AND t.request->>%s = %s ORDER BY c.at DESC LIMIT 1",
        (minutes, tx["id"], "device" if kind == "device" else "ip", value)).fetchone()
    return (row["payment_id"], kind) if row else None


def gate(conn, tx: dict, job_age: float, now: float | None = None) -> Verdict:
    lim = limits(conn)
    now = time.time() if now is None else now
    return decide(lim, now=now, paused_until=breaker(conn, now)["paused_until"] or 0.0, job_age=job_age,
                  used_24h=lambda: calls_24h(conn), grouped=lambda: grouped_with(conn, tx, lim.group_minutes),
                  take=lambda: BUCKET.take(lim.rpm))


def on_rate_limited(conn, message: str, now: float | None = None) -> float:
    """A gateway 429 (rate limit or budget): count it, open the breaker on the third in a row, return how long this job waits."""
    now = time.time() if now is None else now
    streak = breaker(conn, now)["consecutive_429"] + 1
    delay = backoff_seconds(streak, retry_after(message))
    store.put(conn, KEY_STREAK, str(streak))
    if streak >= BREAKER_AFTER:
        pause = limits(conn).pause_minutes * 60
        store.put(conn, KEY_PAUSED_UNTIL, str(now + pause))
        delay = max(delay, pause)
    conn.commit()
    return delay


def on_success(conn) -> None:
    """A call went through: the streak ends. Writes only when there was a streak, so the healthy path costs one read."""
    if breaker(conn)["consecutive_429"]:
        store.put(conn, KEY_STREAK, "0")
        conn.commit()


def purge(conn) -> int:
    n = conn.execute("DELETE FROM triage_llm_call WHERE at < now() - %s * interval '1 day'", (CALL_KEEP_DAYS,)).rowcount
    conn.commit()
    return n
