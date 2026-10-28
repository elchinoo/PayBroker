"""Connections. The implementation (retries for a database that is scaled to zero, the pgvector adapter, the pool) lives in
agentpg-kit; the hot payment path uses a pooled connection, everything else a plain one.

Row-level security (migration 008) decides what a connection may see from two session settings, and this module is the only
place that sets them:

* `connect()`            a plain connection for the operator side (dashboard routes, worker, CLI, MCP, the hosted 3-D Secure page):
                         `app.bypass = on`, all rows. It is closed after use, so the setting never outlives it.
* `merchant_conn(id)`    a pooled connection for ONE merchant's request: `app.merchant_id = id`, that merchant's rows only.
* `system_conn()`        a pooled connection with the bypass, for the few pooled paths that act before a merchant is known (the
                         API key lookup) or across merchants (the CORS origin check).
* `pool()` itself        an unscoped pooled connection: sees no row at all (fail closed). Useful as a test, never for work.

The settings are session level (they survive the commits and rollbacks a payment makes) and are cleared before a pooled connection
goes back, so the next borrower never inherits a merchant. They are not a secret: any code that can run SQL on this connection could
change them. They stop a missing WHERE clause or a wrong id, not a hostile query (docs/GUIDE.md section 18).
"""
import contextlib
from collections.abc import Iterator

import psycopg
from psycopg_pool import ConnectionPool

from .core import db as _db

_SET = "SELECT set_config('app.merchant_id', %s, false), set_config('app.bypass', %s, false)"


def _scope(conn: psycopg.Connection, merchant_id: str = "", bypass: bool = False) -> None:
    """Set the context in one round trip, outside any transaction, so a later rollback cannot undo it."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        conn.rollback()
    previous, conn.autocommit = conn.autocommit, True
    try:
        conn.execute(_SET, (merchant_id, "on" if bypass else ""))
    finally:
        conn.autocommit = previous


def connect(retries: int = 5, delay: float = 1.0) -> psycopg.Connection:
    """A plain connection with the operator bypass (see the module docstring)."""
    conn = _db.connect(retries, delay)
    try:
        _scope(conn, bypass=True)
    except BaseException:
        conn.close()
        raise
    return conn


def pool() -> ConnectionPool:
    return _db.pool()


@contextlib.contextmanager
def _pooled(merchant_id: str = "", bypass: bool = False) -> Iterator[psycopg.Connection]:
    with pool().connection() as conn:
        _scope(conn, merchant_id, bypass)
        try:
            yield conn
        finally:
            with contextlib.suppress(psycopg.Error):     # a broken connection is discarded by the pool; nothing to clear
                if not conn.closed:
                    _scope(conn)


def merchant_conn(merchant_id: str):
    """A pooled connection that sees only `merchant_id`'s rows. Use as `with merchant_conn(mid) as conn:`."""
    if not merchant_id:
        raise ValueError("a merchant id is required (an empty one would see nothing)")
    return _pooled(merchant_id=merchant_id)


def system_conn():
    """A pooled connection with the operator bypass: only for a path that cannot know the merchant yet (see the module docstring)."""
    return _pooled(bypass=True)


def close_pool() -> None:
    _db.close_pool()
