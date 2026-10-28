"""NAME_MISMATCH: a risk signal for a cardholder (billing) name that does not look like the customer's account name.

Uses Postgres' `fuzzystrmatch` (levenshtein, dmetaphone, soundex), so the comparison runs next to the data and tolerates spelling
variants ("Jon Smith" / "John Smyth", "Smith, John", "J. Smith"). Names are compared token by token: a token of the shorter name
matches when it is within a small edit distance of a token of the other name, sounds the same (double metaphone or soundex), or is an
initial of it. The signal is raised only when fewer than half of those tokens match. Nothing is stored: the names are used for this
comparison and dropped. No signal (None) when either name is missing or fuzzystrmatch is not installed.
"""
import re
import time

import psycopg

_TOKEN = re.compile(r"[^\W\d_]+")
MAX_TOKENS, MAX_LEN = 8, 40
MIN_MATCH_RATIO = 0.5

_SQL = """
SELECT count(*) FILTER (WHERE ok) AS matched, count(*) AS total FROM (
  SELECT EXISTS (
    SELECT 1 FROM unnest(%(b)s::text[]) AS b(t)
    WHERE (length(a.t) = 1 AND left(b.t, 1) = a.t)
       OR (length(b.t) = 1 AND left(a.t, 1) = b.t)
       OR levenshtein(a.t, b.t) <= CASE WHEN greatest(length(a.t), length(b.t)) <= 5 THEN 1 ELSE 2 END
       OR (dmetaphone(a.t) <> '' AND dmetaphone(a.t) = dmetaphone(b.t))
       OR (soundex(a.t) <> '' AND soundex(a.t) = soundex(b.t))
  ) AS ok FROM unnest(%(a)s::text[]) AS a(t)) x
"""

_avail: dict = {"at": 0.0, "ok": False}
TTL = 60.0


def tokens(name: str | None) -> list[str]:
    return [t[:MAX_LEN] for t in _TOKEN.findall((name or "").lower())][:MAX_TOKENS]


def available(conn: psycopg.Connection, refresh: bool = False) -> bool:
    """Is fuzzystrmatch installed (cached for a minute, so the payment path does not ask every time)?"""
    if refresh or time.monotonic() - _avail["at"] > TTL:
        ok = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'fuzzystrmatch'").fetchone() is not None
        _avail.update(at=time.monotonic(), ok=ok)
    return _avail["ok"]


def compare(conn: psycopg.Connection, name_a: str | None, name_b: str | None) -> dict | None:
    """{"mismatch": bool, "matched": n, "total": n} or None when the signal cannot be computed."""
    a, b = tokens(name_a), tokens(name_b)
    if not a or not b or not available(conn):
        return None
    if len(a) > len(b):
        a, b = b, a                                # compare the tokens of the shorter name against the longer one
    try:
        with conn.transaction():                   # a savepoint: a failure here must never poison the payment's transaction
            row = conn.execute(_SQL, {"a": a, "b": b}).fetchone()
    except psycopg.Error:
        _avail.update(at=time.monotonic(), ok=False)
        return None
    matched, total = int(row["matched"]), int(row["total"])
    return {"mismatch": matched / total < MIN_MATCH_RATIO, "matched": matched, "total": total}


def mismatch(conn: psycopg.Connection, name_a: str | None, name_b: str | None) -> bool | None:
    res = compare(conn, name_a, name_b)
    return None if res is None else res["mismatch"]
