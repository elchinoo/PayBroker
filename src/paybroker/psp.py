"""Simulated PSP connectors. There is no real acquirer behind them: each one behaves according to its row in psp_connector
(latency, auth rate, error and timeout rates) and to the TEST card used (see cards.py), so routing and failover can be
exercised deterministically. A real connector would implement the same `authorize` signature."""
import os
import random
import threading
import time
from dataclasses import dataclass

from .ids import new_id

TIMEOUT_MS = 300
CASCADE_BUDGET_MS = 800


@dataclass
class Outcome:
    kind: str                   # approved | declined | error | timeout
    code: str = ""
    auth_code: str = ""
    network_tx_id: str = ""
    latency_ms: float = 0.0


def _sleep(ms: float) -> None:
    if os.environ.get("PAYBROKER_SIM_LATENCY", "on").lower() not in {"off", "0", "false"}:
        time.sleep(ms / 1000.0)


def authorize(connector: dict, card: dict, amount: int, currency: str, attempt: int, rng: random.Random | None = None) -> Outcome:
    r = rng or random
    behavior = card["sim_behavior"]
    latency = max(5.0, r.gauss(connector["base_latency_ms"], connector["base_latency_ms"] * 0.25))
    if behavior == "random":
        if r.random() < float(connector["timeout_rate"]):
            _sleep(TIMEOUT_MS)
            return Outcome("timeout", "psp_timeout", latency_ms=TIMEOUT_MS)
        if r.random() < float(connector["error_rate"]):
            _sleep(latency)
            return Outcome("error", "psp_5xx", latency_ms=latency)
    elif behavior == "psp_error" and attempt == 0:
        _sleep(latency)
        return Outcome("error", "psp_5xx", latency_ms=latency)
    _sleep(latency)
    if behavior in {"decline", "insufficient_funds", "stolen_card"}:
        code = {"decline": "do_not_honor", "insufficient_funds": "insufficient_funds", "stolen_card": "stolen_card"}[behavior]
        return Outcome("declined", code, latency_ms=latency)
    if behavior == "random" and r.random() > float(connector["base_auth_rate"]):
        return Outcome("declined", r.choice(["do_not_honor", "insufficient_funds", "card_velocity_exceeded"]), latency_ms=latency)
    return Outcome("approved", "", auth_code="AUTH_" + new_id("", 6).upper(), network_tx_id=new_id("ntx_", 10), latency_ms=latency)


# ---- live connector statistics, kept in memory and flushed to the database by the worker -----------------------
_lock = threading.Lock()
_stats: dict[str, dict] = {}
ALPHA = 0.05


def load_stats(conn) -> None:
    with _lock:
        for r in conn.execute("SELECT name, auth_ewma, latency_ewma, attempts, successes FROM psp_connector"):
            _stats.setdefault(r["name"], {"auth": float(r["auth_ewma"]), "lat": float(r["latency_ewma"]),
                                          "attempts": 0, "successes": 0, "dirty": False})


def observe(name: str, success: bool, latency_ms: float) -> None:
    """Fold one attempt in. Declines are issuer decisions, not connector failures, so callers pass success=True for them."""
    with _lock:
        s = _stats.setdefault(name, {"auth": 0.9, "lat": latency_ms, "attempts": 0, "successes": 0, "dirty": False})
        s["auth"] += ALPHA * ((1.0 if success else 0.0) - s["auth"])
        s["lat"] += ALPHA * (latency_ms - s["lat"])
        s["attempts"] += 1
        s["successes"] += 1 if success else 0
        s["dirty"] = True


def live(name: str, default_auth: float, default_lat: float) -> tuple[float, float]:
    with _lock:
        s = _stats.get(name)
        return (s["auth"], s["lat"]) if s else (default_auth, default_lat)


def flush(conn) -> int:
    with _lock:
        dirty = {n: dict(s) for n, s in _stats.items() if s["dirty"]}
        for s in _stats.values():
            if s["dirty"]:
                s["attempts"] = s["successes"] = 0
                s["dirty"] = False
    for name, s in dirty.items():
        conn.execute("UPDATE psp_connector SET auth_ewma=%s, latency_ewma=%s, attempts=attempts+%s, successes=successes+%s "
                     "WHERE name=%s", (round(max(0.0, min(1.0, s["auth"])), 5), round(s["lat"], 2), s["attempts"], s["successes"], name))
    conn.commit()
    return len(dirty)


def reset_stats() -> None:
    with _lock:
        _stats.clear()
