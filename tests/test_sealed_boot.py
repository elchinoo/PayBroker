"""Sealed boot (agentpg-kit 0.4.1): with APGKIT_SEALED set the app answers 503 until the operator unseals it, then works.

The app is imported in a child process: sealing is decided when the module is imported, and the MCP session manager can only
start once per process (the shared `client` fixture already used it)."""
import json
import os
import subprocess
import sys
import textwrap

from apgkit import sealed as sealing

from tests.conftest import needs_db

PASS = "correct-horse-battery-staple-9"

CHILD = textwrap.dedent("""
    import json, os
    from apgkit import sealed as sealing
    from fastapi.testclient import TestClient
    from paybroker import api

    out = {}
    with TestClient(api.app) as c:
        out["sealed_route"] = c.get("/api/session").status_code
        out["health"] = c.get("/healthz").status_code
        out["state"] = c.get("/_seal/status").json()["state"]
        if os.environ.get("CHILD_UNSEAL") == "1":
            key = c.get("/_seal/key").json()["public_key"]
            r = c.post("/_seal/unseal", json=sealing.make_envelope(key, os.environ["CHILD_PASS"]))
            out["unseal"], out["body"] = r.status_code, r.text[:300]
            out["state_after"] = c.get("/_seal/status").json()["state"]
            out["route_after"] = c.get("/api/session").status_code
            out["vault_after"] = c.get("/api/session").json().get("vault", {}).get("unlocked")
    print("RESULT" + json.dumps(out))
""")


def _run(blob: str, passphrase: str = "", unseal: bool = False) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "DB_EXTERNAL_DSN", "PSQL_CONNECTION_STRING",
                                                          "PAYBROKER_ENCRYPTION_KEY", "PAYBROKER_API_KEY"}}
    env.update({"APGKIT_SEALED": blob, "CHILD_UNSEAL": "1" if unseal else "0", "CHILD_PASS": passphrase,
                "PAYBROKER_ENCRYPTION_KEY_FILE": "/nonexistent/key", "PAYBROKER_API_KEY_FILE": "/nonexistent/key",
                "PGCONNECT_TIMEOUT": "2"})
    p = subprocess.run([sys.executable, "-I", "-c", CHILD], env=env, capture_output=True, text=True, timeout=120, cwd=os.getcwd())
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT")), None)
    assert line, p.stderr[-2000:]
    return json.loads(line[len("RESULT"):])


def test_sealed_app_serves_only_the_gate_and_health():
    out = _run(sealing.seal({"DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none"}, PASS))
    assert out == {"sealed_route": 503, "health": 200, "state": "sealed"}


@needs_db
def test_unseal_migrates_opens_the_vault_and_serves(_schema):
    from paybroker.core import local
    from paybroker.db import connect

    with connect() as conn:       # a vault left by an earlier test belongs to another passphrase: start this one from none
        local.reset_admin(conn)
    blob = sealing.seal({"DATABASE_URL": os.environ["TEST_DATABASE_URL"]}, PASS)
    out = _run(blob, PASS, unseal=True)
    assert out["sealed_route"] == 503 and out["health"] == 200
    assert out["unseal"] == 200 and out["state_after"] == "unsealed", out
    assert out["route_after"] == 200
    assert out["vault_after"] is True
