"""Tiny plain-SQL migration runner (just Postgres: no ORM). The shared tables (settings, sessions, API tokens, OIDC state)
come from agentpg-kit. After every run the read-only role is hardened (hardening.py)."""
from pathlib import Path

from apgkit import migrate as kit_migrate
from apgkit.auth.schema import SQL as AUTH_SQL

from . import hardening
from .core import db

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def _auth_tables(conn) -> None:
    conn.execute(AUTH_SQL)      # before hardening, so web_session / api_token / oidc_login exist when they are taken from the role


def run() -> list[str]:
    return kit_migrate.run(db, MIGRATIONS, extensions=("vector",), after_migrate=[_auth_tables, hardening.apply])
