"""Tiny plain-SQL migration runner (just Postgres: no ORM). The shared tables (settings, sessions, API tokens, OIDC state)
come from agentpg-kit."""
from pathlib import Path

from apgkit import migrate as kit_migrate
from apgkit.auth.schema import SQL as AUTH_SQL

from .core import db

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def run() -> list[str]:
    applied = kit_migrate.run(db, MIGRATIONS, extensions=("vector",))
    with db.connect() as conn:
        conn.execute(AUTH_SQL)
        conn.commit()
    return applied
