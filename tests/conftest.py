import os

import pytest

os.environ.setdefault("PAYBROKER_ROLE", "api")          # no background worker: tests drive the queue themselves
os.environ.setdefault("PAYBROKER_SIM_LATENCY", "off")   # simulated connectors answer instantly
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL to run DB tests")


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
    from paybroker import api, core, psp, routing, rules, vault
    from paybroker.db import connect

    for var in ("PAYBROKER_API_KEY", "PAYBROKER_ENCRYPTION_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PAYBROKER_API_KEY_FILE", "/nonexistent/key")
    monkeypatch.setenv("PAYBROKER_ENCRYPTION_KEY_FILE", "/nonexistent/key")
    with connect() as conn:
        tables = [r["tablename"] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'schema_migrations'")]
        conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")
        conn.commit()
        routing.seed_connectors(conn)
        rules.seed_defaults(conn)
    for cache in (routing.invalidate, rules.invalidate, api.forget_merchants, psp.reset_stats, core.auth.limiter.clear):
        cache()
    core.local._setup_code = None
    request.getfixturevalue("client").cookies.clear()
    vault.lock()
    yield
    request.getfixturevalue("client").cookies.clear()
    vault.lock()
