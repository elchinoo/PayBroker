"""Connections. The implementation (retries for a database that is scaled to zero, the pgvector adapter, the pool) lives in
agentpg-kit; the hot payment path uses `pool()`, everything else a plain connection."""
import psycopg
from psycopg_pool import ConnectionPool

from .core import db as _db


def connect(retries: int = 5, delay: float = 1.0) -> psycopg.Connection:
    return _db.connect(retries, delay)


def pool() -> ConnectionPool:
    return _db.pool()


def close_pool() -> None:
    _db.close_pool()
