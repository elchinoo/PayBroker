"""Training data: capture, labels, retention, statistics and export.

Opt-in (off by default). While capture is on, every scored payment leaves ONE row in `training_example`: the 17-number feature
vector the model saw, which model produced which score and decision, and an internal payment reference. It never holds a card,
e-mail, IP address, device id or name; those stay in the feature store and the payment tables under their own rules.

A row's `label` (1 fraud, 0 legitimate) is null until a REAL outcome is known, and is never guessed:

* a fraud-coded chargeback labels the payment 1 (source `chargeback`; the strongest evidence);
* an analyst resolving a flagged case labels it: PERMANENT_BLOCK is confirmed fraud (1), DISMISS_ALERT a false positive (0);
  ALLOW_WITH_KYC leaves it unlabeled because it asserts neither (source `case`);
* an explicit admin action (`label_aging`) may label payments that were approved and captured, and had no dispute after N days,
  as 0 (source `aging`). It never runs by itself.

A stronger source replaces a weaker one (chargeback over case over aging), never the other way round.
"""
import csv
import io
import json
import logging
import os
import time
from collections.abc import Iterator
from datetime import datetime

from . import __version__, features, ml
from . import settings_store as store

log = logging.getLogger("paybroker.training")

DEFAULT_RETENTION_DAYS = 180
DEFAULT_MAX_ROWS = 5_000_000
MAX_RETENTION_DAYS = 3650
MAX_ROWS_LIMIT = 50_000_000
SETTINGS_TTL = 30.0
PURGE_BATCH = 10_000
KEY_CAPTURE, KEY_RETENTION, KEY_MAX_ROWS = "training_capture", "training_retention_days", "training_max_rows"
PRIORITY_SQL = "CASE label_source WHEN 'chargeback' THEN 3 WHEN 'case' THEN 2 WHEN 'aging' THEN 1 ELSE 0 END"
# Network reason codes that mean "the cardholder says they did not make this payment" (fraud), as opposed to a dispute about
# goods, duplicates or credits. Visa category 10.x, Mastercard 4837, 4840, 4863, 4870, 4871.
FRAUD_REASON_CODES = frozenset({"10.1", "10.2", "10.3", "10.4", "10.5", "4837", "4840", "4863", "4870", "4871", "fraud"})
SAMPLE_COLUMNS = ("id", "created_at", "payment_id", "feature_hash", "model_version", "score", "decision", "label", "label_source",
                  "labeled_at")

_cache: dict = {"at": 0.0, "value": None}


class TrainingError(ValueError):
    pass


# ---- settings -------------------------------------------------------------------------------------------------------
def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def settings(conn, *, fresh: bool = False) -> dict:
    """capture / retention_days / max_rows. The stored value wins; the environment supplies the default; capture is off unless
    one of them says on. Cached for 30 s per process (so the payment path does not query for it), refreshed on every change."""
    if not fresh and _cache["value"] is not None and time.monotonic() - _cache["at"] < SETTINGS_TTL:
        return _cache["value"]
    rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_setting WHERE key = ANY(%s)",
                                                       ([KEY_CAPTURE, KEY_RETENTION, KEY_MAX_ROWS],))}
    capture = _truthy(rows[KEY_CAPTURE]) if KEY_CAPTURE in rows else _truthy(os.environ.get("PAYBROKER_CAPTURE_TRAINING_DATA"))
    out = {
        "capture": capture,
        "capture_source": "setting" if KEY_CAPTURE in rows else ("environment" if capture else "default"),
        "retention_days": int(rows.get(KEY_RETENTION) or _env_int("PAYBROKER_TRAINING_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)),
        "max_rows": int(rows.get(KEY_MAX_ROWS) or _env_int("PAYBROKER_TRAINING_MAX_ROWS", DEFAULT_MAX_ROWS)),
    }
    _cache.update(at=time.monotonic(), value=out)
    return out


def invalidate() -> None:
    _cache.update(at=0.0, value=None)


def capture_enabled(conn) -> bool:
    """Cheap on the payment path: a fresh cache answers without touching the database; a stale one is refreshed inside a
    savepoint, so even a failure there cannot abort the payment's own transaction (capture then stays off for 30 s)."""
    if _cache["value"] is not None and time.monotonic() - _cache["at"] < SETTINGS_TTL:
        return _cache["value"]["capture"]
    try:
        with conn.transaction():
            return settings(conn)["capture"]
    except Exception:  # noqa: BLE001
        _cache.update(at=time.monotonic(), value={"capture": False, "capture_source": "default",
                                                  "retention_days": DEFAULT_RETENTION_DAYS, "max_rows": DEFAULT_MAX_ROWS})
        return False


def update_settings(conn, *, capture: bool | None = None, retention_days: int | None = None, max_rows: int | None = None) -> dict:
    if retention_days is not None and not 1 <= retention_days <= MAX_RETENTION_DAYS:
        raise TrainingError(f"retention_days must be between 1 and {MAX_RETENTION_DAYS}")
    if max_rows is not None and not 1000 <= max_rows <= MAX_ROWS_LIMIT:
        raise TrainingError(f"max_rows must be between 1000 and {MAX_ROWS_LIMIT}")
    if capture is not None:
        store.put(conn, KEY_CAPTURE, "1" if capture else "0")
    if retention_days is not None:
        store.put(conn, KEY_RETENTION, str(retention_days))
    if max_rows is not None:
        store.put(conn, KEY_MAX_ROWS, str(max_rows))
    conn.commit()
    invalidate()
    return settings(conn, fresh=True)


# ---- capture --------------------------------------------------------------------------------------------------------
_INSERT = ("INSERT INTO training_example (payment_id, features, feature_hash, model_version, score, decision) "
           "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (payment_id) DO NOTHING")


def capture_statement(payment_id: str, vector: list[float], model_version: str, score: int, decision: str) -> tuple[str, tuple]:
    """The INSERT (and its parameters) for one scored payment, to ride in the payment's own pipeline: no extra round trip."""
    return _INSERT, (payment_id, [float(v) for v in vector], features.FEATURE_HASH, model_version[:64], int(score), decision)


# ---- labels ---------------------------------------------------------------------------------------------------------
def is_fraud_chargeback(reason_code: str | None, reason: str | None = None) -> bool:
    """True for a dispute the network codes as fraud. Free text alone only counts when it is exactly the word fraud, so
    "item not received, not fraud" never labels a payment."""
    code = (reason_code or "").strip().lower()
    if code:
        return code in FRAUD_REASON_CODES
    return (reason or "").strip().lower() == "fraud"


def set_label(conn, payment_id: str, label: int, source: str) -> bool:
    """Record a real outcome for a payment's training row. Returns True when a row changed. Does not commit."""
    if label not in (0, 1) or source not in {"chargeback", "case", "aging"}:
        raise TrainingError("bad label")
    rank = {"chargeback": 3, "case": 2, "aging": 1}[source]
    row = conn.execute(
        f"UPDATE training_example SET label=%s, label_source=%s, labeled_at=now() WHERE payment_id=%s AND {PRIORITY_SQL} <= %s "
        "RETURNING id", (label, source, payment_id, rank)).fetchone()
    return row is not None


def label_aging(conn, days: int, batch: int = 20_000) -> dict:
    """Label as 0 the payments that were approved and captured, still have no label, no chargeback and no open case, and are at
    least `days` old. Declined or never-captured payments cannot be disputed, so silence about them proves nothing and they are
    never labeled. Run only by an explicit admin action. Bias note: the result covers only approved traffic."""
    if not 1 <= days <= 3650:
        raise TrainingError("days must be between 1 and 3650")
    total = 0
    while True:
        rows = conn.execute(
            "UPDATE training_example te SET label=0, label_source='aging', labeled_at=now() WHERE te.id IN ("
            "SELECT te2.id FROM training_example te2 JOIN transaction t ON t.payment_id=te2.payment_id "
            "WHERE te2.label IS NULL AND te2.created_at < now() - make_interval(days => %s) "
            "AND t.status IN ('CAPTURED','PARTIALLY_REFUNDED','REFUNDED') "
            "AND NOT EXISTS (SELECT 1 FROM chargeback c WHERE c.payment_id=te2.payment_id) "
            "AND NOT EXISTS (SELECT 1 FROM fraud_case fc WHERE fc.transaction_id=t.id AND fc.status='open') "
            "ORDER BY te2.id LIMIT %s)", (days, batch))
        conn.commit()
        total += rows.rowcount
        if rows.rowcount < batch:
            break
    return {"labeled_legitimate": total, "days": days}


# ---- retention ------------------------------------------------------------------------------------------------------
def purge(conn, *, retention_days: int | None = None, max_rows: int | None = None, max_batches: int = 20,
          batch: int = PURGE_BATCH) -> dict:
    """Delete rows older than the retention, then the oldest rows above the hard cap. Bounded work per call (`max_batches`
    batches of `batch` rows) so the worker never holds the table for long; the next run continues."""
    cfg = settings(conn, fresh=True)
    retention_days = cfg["retention_days"] if retention_days is None else retention_days
    max_rows = cfg["max_rows"] if max_rows is None else max_rows
    expired = capped = batches = 0
    while batches < max_batches:
        n = conn.execute(
            "DELETE FROM training_example WHERE id IN (SELECT id FROM training_example WHERE created_at < now() - make_interval(days => %s) "
            "ORDER BY id LIMIT %s)", (retention_days, batch)).rowcount
        conn.commit()
        batches += 1
        expired += n
        if n < batch:
            break
    if batches < max_batches:
        excess = conn.execute("SELECT count(*) AS n FROM training_example").fetchone()["n"] - max_rows
        while excess > 0 and batches < max_batches:
            n = conn.execute("DELETE FROM training_example WHERE id IN (SELECT id FROM training_example ORDER BY id LIMIT %s)",
                             (min(batch, excess),)).rowcount
            conn.commit()
            batches += 1
            capped += n
            excess -= n
            if n == 0:
                break
    conn.commit()
    return {"expired": expired, "over_cap": capped, "batches": batches, "retention_days": retention_days, "max_rows": max_rows}


# ---- statistics -----------------------------------------------------------------------------------------------------
def stats(conn) -> dict:
    total = conn.execute(
        "SELECT count(*) AS rows, count(label) AS labeled, count(*) FILTER (WHERE label=1) AS fraud, min(created_at) AS oldest, "
        "max(created_at) AS newest FROM training_example").fetchone()
    by_model = conn.execute(
        "SELECT model_version, count(*) AS rows, count(label) AS labeled, count(*) FILTER (WHERE label=1) AS fraud, "
        "round(avg(score)) AS avg_score FROM training_example GROUP BY model_version ORDER BY max(id) DESC").fetchall()
    by_source = {r["label_source"]: r["n"] for r in conn.execute(
        "SELECT label_source, count(*) AS n FROM training_example WHERE label IS NOT NULL GROUP BY label_source")}
    conn.rollback()

    def rate(fraud, labeled):
        return round(fraud / labeled, 5) if labeled else None
    return {"rows": total["rows"], "labeled": total["labeled"], "fraud": total["fraud"],
            "fraud_rate": rate(total["fraud"], total["labeled"]), "oldest": total["oldest"], "newest": total["newest"],
            "label_sources": by_source,
            "per_model_version": [{"model_version": r["model_version"], "rows": r["rows"], "labeled": r["labeled"], "fraud": r["fraud"],
                                   "fraud_rate": rate(r["fraud"], r["labeled"]), "avg_score": int(r["avg_score"])} for r in by_model],
            "feature_hash": features.FEATURE_HASH, "features": list(features.VECTOR_FIELDS)}


# ---- export ---------------------------------------------------------------------------------------------------------
def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrainingError("since must be an ISO date or time, for example 2026-01-31 or 2026-01-31T12:00:00Z") from exc
    return dt


def _where(labeled: bool, since: datetime | None) -> tuple[str, list]:
    clauses, args = ["feature_hash = %s"], [features.FEATURE_HASH]
    if labeled:
        clauses.append("label IS NOT NULL")
    if since is not None:
        clauses.append("created_at >= %s")
        args.append(since)
    return " AND ".join(clauses), args


def export_meta(conn, labeled: bool, since: datetime | None, actor: str) -> dict:
    where, args = _where(labeled, since)
    versions = [r["model_version"] for r in conn.execute(
        f"SELECT model_version, max(id) AS m FROM training_example WHERE {where} GROUP BY model_version ORDER BY m", args)]
    conn.rollback()
    return {"format": "paybroker-training-export/1", "paybroker_version": __version__, "feature_hash": features.FEATURE_HASH,
            "features": list(features.VECTOR_FIELDS), "model_versions": versions, "active_model": ml.current().version,
            "labeled_only": labeled, "since": since.isoformat() if since else None,
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"), "exported_by": actor,
            "notes": "label: 1 fraud, 0 legitimate, empty when unknown. Rows scored by a model carry its decisions: training on "
                     "them alone teaches the model its own bias. No card, e-mail, IP, device id or name is included."}


def _fmt(v: float) -> str:
    return f"{v:.7g}"


def export_rows(conn, fmt: str, labeled: bool, since: datetime | None, actor: str, limit: int | None = None,
                itersize: int = 2000) -> Iterator[bytes]:
    """Stream the export as bytes: a server-side cursor, so memory stays flat however many rows there are."""
    meta = export_meta(conn, labeled, since, actor)
    names = list(features.VECTOR_FIELDS)
    where, args = _where(labeled, since)
    sql = (f"SELECT {', '.join(SAMPLE_COLUMNS)}, features FROM training_example WHERE {where} ORDER BY id"
           + (f" LIMIT {int(limit)}" if limit else ""))
    if fmt == "csv":
        yield "".join(f"# {line}\n" for line in (
            f"paybroker training export, format {meta['format']}, paybroker {meta['paybroker_version']}",
            f"feature_hash: {meta['feature_hash']}", f"features: {', '.join(names)}",
            f"model_versions: {', '.join(meta['model_versions']) or 'none'} (active now: {meta['active_model']})",
            f"labeled_only: {str(labeled).lower()}; since: {meta['since'] or 'beginning'}; exported_at: {meta['exported_at']}",
            "label: 1 fraud, 0 legitimate, empty when unknown. Scores and decisions come from the model that scored the payment.",
            "no card, e-mail, IP, device id or name is included")).encode()
    else:
        yield (json.dumps({"_meta": meta}, separators=(",", ":")) + "\n").encode()
    count = 0
    t0 = time.monotonic()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    if fmt == "csv":
        writer.writerow([*SAMPLE_COLUMNS, *names])
        yield buf.getvalue().encode()
        buf.seek(0)
        buf.truncate()
    with conn.cursor(name="training_export") as cur:
        cur.itersize = itersize
        cur.execute(sql, args)
        for row in cur:
            base = [row[c] for c in SAMPLE_COLUMNS]
            base[1] = base[1].isoformat() if base[1] else ""
            base[9] = base[9].isoformat() if base[9] else ""
            vec = row["features"]
            if fmt == "csv":
                writer.writerow([("" if v is None else v) for v in base] + [_fmt(v) for v in vec])
                if buf.tell() > 64 * 1024:
                    yield buf.getvalue().encode()
                    buf.seek(0)
                    buf.truncate()
            else:
                doc = dict(zip(SAMPLE_COLUMNS, base, strict=True))
                doc["features"] = {n: float(_fmt(v)) for n, v in zip(names, vec, strict=True)}
                yield (json.dumps(doc, separators=(",", ":")) + "\n").encode()
            count += 1
    if fmt == "csv" and buf.tell():
        yield buf.getvalue().encode()
    conn.rollback()
    log.info("training export by %s: %d rows as %s in %.1fs", actor, count, fmt, time.monotonic() - t0)
