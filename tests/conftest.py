import os

import pytest

os.environ.setdefault("PAYBROKER_ROLE", "api")          # no background worker: tests drive the queue themselves
os.environ.setdefault("PAYBROKER_SIM_LATENCY", "off")   # simulated connectors answer instantly
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL to run DB tests")

LEDGER = ("audit_ledger", "audit_ledger_head")


def reset_ledger(conn) -> None:
    """Start a test from an empty audit chain. This is what the migration forbids, done the only way it can be done: as the owner, with
    the trigger switched off and the privilege granted back, then restored. (A test that left the trigger off is healed here too.)"""
    me = conn.execute("SELECT current_user AS u").fetchone()["u"]
    conn.execute(f'GRANT TRUNCATE ON audit_ledger TO "{me}"')
    conn.execute("ALTER TABLE audit_ledger DISABLE TRIGGER USER")
    conn.execute("TRUNCATE audit_ledger")
    conn.execute("UPDATE audit_ledger_head SET seq = 0, hash = repeat('0', 64)")
    conn.execute("ALTER TABLE audit_ledger ENABLE TRIGGER USER")
    conn.execute(f'REVOKE TRUNCATE ON audit_ledger FROM "{me}"')
    conn.commit()


@pytest.fixture(scope="session")
def _schema():
    if not os.environ.get("TEST_DATABASE_URL"):
        yield
        return
    from paybroker import migrate
    from paybroker.core import db

    with db.connect() as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    migrate.run()
    yield
    db.close_pool()


@pytest.fixture(scope="session")
def client(_schema):
    from fastapi.testclient import TestClient

    from paybroker import api

    with TestClient(api.app) as c:     # the MCP session manager can only start once per process
        yield c


@pytest.fixture(autouse=True)
def _fresh(request, monkeypatch):
    """Every DB test starts from empty tables (with the default connectors and rules) and no cached state."""
    if "client" not in request.fixturenames:
        yield
        return
    from paybroker import api, core, ml, psp, routing, rules, training_data, vault
    from paybroker.db import connect

    for var in ("PAYBROKER_API_KEY", "PAYBROKER_ENCRYPTION_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PAYBROKER_API_KEY_FILE", "/nonexistent/key")
    monkeypatch.setenv("PAYBROKER_ENCRYPTION_KEY_FILE", "/nonexistent/key")
    with connect() as conn:
        tables = [r["tablename"] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'schema_migrations'")
            if r["tablename"] not in LEDGER]
        conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")
        conn.commit()
        reset_ledger(conn)
        routing.seed_connectors(conn)
        rules.seed_defaults(conn)
    for cache in (routing.invalidate, rules.invalidate, ml.invalidate, training_data.invalidate, api.forget_merchants, psp.reset_stats, core.auth.limiter.clear):
        cache()
    core.local._setup_code = None
    request.getfixturevalue("client").cookies.clear()
    vault.lock()
    if request.node.get_closest_marker("no_vault") is None:
        # Tokenizing a card needs the vault (it holds the fingerprint key): most tests run with an initialised, unlocked one.
        with connect() as conn:
            core.vault.initialize(conn, "test-vault-password-0123")
            conn.commit()
    yield
    request.getfixturevalue("client").cookies.clear()
    vault.lock()
