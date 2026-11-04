import argparse
import sys

import psycopg
import psycopg.types.json


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="paybroker")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply SQL migrations")
    demo = sub.add_parser("demo", help="create a demo merchant and send simulated traffic through the broker")
    demo.add_argument("-n", type=int, default=200, help="number of payments")
    demo.add_argument("--seed", type=int, default=7)
    rd = sub.add_parser("reset-data", help="wipe the transaction data (payments, fraud, cases, queue, feature store) and keep the configuration")
    rd.add_argument("--yes", action="store_true", help="required: this cannot be undone")
    rd.add_argument("--confirm", default="", help='the exact phrase "RESET PAYBROKER DATA"')
    rd.add_argument("--keep-training-data", action="store_true", help="keep the captured training examples")
    rd.add_argument("--keep-rule-suggestions", action="store_true", help="keep the suggested rules")
    sub.add_parser("serve", help="run the API + MCP server")
    rk = sub.add_parser("rotate-vault-key", help="replace the vault data key and re-encrypt every stored secret (asks for the admin password)")
    rk.add_argument("--yes", action="store_true", help="required: other replicas must be restarted afterwards")
    sub.add_parser("adopt-rowmacs", help="sign existing rows that have no row MAC (trusts the database as it is now; asks for the admin password)")
    sa = sub.add_parser("aidb-setup", help="operator step for AIDB mode: models, pipeline, semantic knowledge base, tools, purposes, agents, grants; "
                                           "seals them (asks for the admin connection string and the admin password; repeating it changes nothing)")
    sa.add_argument("--model", default="", help="use this registered AIDB model for the agents instead of registering the llm-gateway model")
    sa.add_argument("--no-validate", action="store_true", help="do not let AIDB call the gateway while registering the model")
    sa.add_argument("--reseal", action="store_true", help="accept the CURRENT state of definitions whose seal does not match (inspect them first)")
    st = sub.add_parser("aidb-status", help="AIDB mode: what is requested, what is set up, what fails its seal (exit 1 when something is wrong)")
    st.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("reset-admin", help="forget the admin password and delete stored secrets (recovery)")
    sub.add_parser("start", help="container entrypoint: migrate, then serve")
    args = p.parse_args(argv)

    if args.cmd == "migrate":
        from .migrate import run

        applied = run()
        print("applied:", ", ".join(applied) if applied else "nothing (up to date)")
    elif args.cmd == "demo":
        from . import demo as demo_mod
        from .db import connect

        with connect() as conn:
            r = demo_mod.run(conn, args.n, args.seed)
        print(f"merchant {r['merchant_id']}: {r['sent']} payments, outcomes {r['outcomes']}")
        if r.get("secret_key"):
            print(f"secret key (shown once): {r['secret_key']}")
    elif args.cmd == "reset-admin":
        from .core import local
        from .db import connect

        with connect() as conn:
            local.reset_admin(conn)
        print("admin password, vault and stored secrets were cleared; the setup code flow is active again")
    elif args.cmd == "rotate-vault-key":
        return _rotate_vault_key(args)
    elif args.cmd == "adopt-rowmacs":
        return _adopt_rowmacs()
    elif args.cmd == "aidb-setup":
        return _aidb_setup(args)
    elif args.cmd == "aidb-status":
        return _aidb_status(args)
    elif args.cmd == "reset-data":
        return _reset_data(args)
    elif args.cmd == "serve":
        _serve()
    elif args.cmd == "start":
        return _start()
    return 0


def _rotate_vault_key(args) -> int:
    import getpass

    from . import audit, rowmac, vault
    from .core import local
    from .db import connect

    if not args.yes:
        print("refusing to rotate without --yes (other replicas must be restarted afterwards)", file=sys.stderr)
        return 2
    with connect() as conn:
        has_password = local.admin_configured(conn)
        password = getpass.getpass("Admin password: ") if has_password else None
        if has_password:
            if not local.login_password(conn, password or ""):     # also unlocks the vault
                print("wrong admin password", file=sys.stderr)
                return 1
        elif not vault.try_unlock_with_platform_secret(conn):
            print("the vault is locked and there is no platform secret to open it", file=sys.stderr)
            return 1
        try:
            counts = rowmac.rotate_data_key(conn, password=password if has_password else None)      # re-signs every row MAC under the new key
        except (vault.VaultError, vault.VaultLocked) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        who = f"cli:{getpass.getuser()}"
        audit.record(conn, who, "vault.rotate_key", "", {"stores": counts})      # ids and counts only: the ledger never holds a secret
        conn.execute("INSERT INTO admin_audit (actor, action, detail) VALUES (%s,%s,%s)",
                     (who, "vault.rotate_key", psycopg.types.json.Jsonb({"stores": counts})))
        conn.commit()
    for name, n in counts.items():
        print(f"{name}: {n} re-encrypted")
    print("done: restart every other process of this app now (they still hold the old key in memory)")
    return 0


def _adopt_rowmacs() -> int:
    """Sign every security-relevant row that has no MAC (a database from before row MACs). It TRUSTS THE DATABASE AS IT IS AT THIS MOMENT:
    run it once, after migrating, on a database you believe untampered. Asks for the admin password like rotate-vault-key."""
    import getpass

    from . import audit, rowmac, vault
    from .core import local
    from .db import connect

    with connect() as conn:
        has_password = local.admin_configured(conn)
        if has_password and not vault.is_unlocked():
            password = getpass.getpass("Admin password: ")
            if not local.login_password(conn, password or ""):     # also unlocks the vault
                print("wrong admin password", file=sys.stderr)
                return 1
        elif not vault.is_unlocked() and not vault.try_unlock_with_platform_secret(conn):
            print("the vault is locked and there is no platform secret to open it", file=sys.stderr)
            return 1
        counts = rowmac.adopt(conn)
        who = f"cli:{getpass.getuser()}"
        audit.record(conn, who, "rowmacs.adopt", "", {"signed": counts})
        conn.execute("INSERT INTO admin_audit (actor, action, detail) VALUES (%s,%s,%s)",
                     (who, "rowmacs.adopt", psycopg.types.json.Jsonb({"signed": counts})))
        conn.commit()
    print("signed rows without a MAC: " + ", ".join(f"{t} {n}" for t, n in counts.items()) +
          ". This trusted the database as it was now; rows written by anything else from here on are ignored.")
    return 0


def _unlock_vault(conn) -> bool:
    """Open the vault for this command. In order: already open; the admin password (a standalone install that has one; not under sealed boot, where
    the vault is wrapped under the unseal passphrase instead); the platform secret; under sealed boot (or when no admin password exists) the
    unseal passphrase, asked on a terminal with a hidden prompt through the kit's own `unlock_sealed`. The passphrase is never an argument, an
    environment variable, printed or stored. False says why on stderr."""
    import getpass

    from apgkit import sealed as sealing
    from apgkit.sealed import Protected
    from apgkit.vault import VaultLocked

    from . import vault
    from .core import local
    from .core import vault as core_vault

    if vault.is_unlocked():
        return True
    sealed_boot = sealing.Sealed.from_env().enabled
    if not sealed_boot and local.admin_configured(conn):
        if not local.login_password(conn, getpass.getpass("Admin password: ") or ""):     # also unlocks the vault
            print("wrong admin password", file=sys.stderr)
            return False
        return True
    if vault.try_unlock_with_platform_secret(conn):
        return True
    if not vault.is_initialized(conn):
        print("the vault does not exist yet and there is no platform secret to create it under: start the app (sealed boot creates it at the first "
              "unseal), then run this again", file=sys.stderr)
        return False
    if not sys.stdin.isatty():
        print("the vault is locked and there is no platform secret to open it; run this command in a terminal to enter the unseal passphrase",
              file=sys.stderr)
        return False
    typed = getpass.getpass("Unseal passphrase (the sealed-boot passphrase, not shown, not stored): ") or ""
    if not typed:
        print("no passphrase given", file=sys.stderr)
        return False
    try:
        core_vault.unlock_sealed(conn, Protected(typed))
    except VaultLocked:
        print("that passphrase does not open the vault", file=sys.stderr)
        return False
    finally:
        del typed
    return True


def _aidb_setup(args) -> int:
    """Everything AIDB mode needs that the app's own role must not be able to do. The admin connection string is read from the prompt (or from
    PAYBROKER_AIDB_ADMIN_DSN for this one command), used for this command only and never printed, stored or logged."""
    import getpass
    import os

    import psycopg
    from psycopg.rows import dict_row

    from . import aidb_mode, audit
    from .core import vault as core_vault
    from .db import connect

    dsn = os.environ.get("PAYBROKER_AIDB_ADMIN_DSN") or getpass.getpass("AIDB admin connection string (not shown, not stored): ")
    if not dsn.strip():
        print("no admin connection string given", file=sys.stderr)
        return 2
    with connect() as conn:
        if not _unlock_vault(conn):
            return 1
        role = conn.execute("SELECT current_user AS u").fetchone()["u"]
        try:
            admin = psycopg.connect(dsn.strip(), row_factory=dict_row)
        except psycopg.Error as exc:
            print(f"could not connect with the admin connection string: {type(exc).__name__}", file=sys.stderr)
            return 1
        try:
            steps = aidb_mode.setup(admin, conn, role, vault=core_vault, model=args.model, validate=False if args.no_validate else None,
                                    reseal=args.reseal)
        except (aidb_mode.SetupError, aidb_mode.aidb.AidbError) as exc:
            admin.rollback()
            print(f"aidb-setup stopped: {str(exc).splitlines()[0][:300]}", file=sys.stderr)
            if isinstance(exc, aidb_mode.aidb.AidbTampered):
                print("a sealed definition was changed outside this app; inspect it, then re-run with --reseal to accept the current state", file=sys.stderr)
            return 1
        except psycopg.Error as exc:
            admin.rollback()
            print(f"aidb-setup stopped: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}", file=sys.stderr)
            return 1
        finally:
            admin.close()
        who = f"cli:{getpass.getuser()}"
        changed = sum(1 for s in steps if not s.action.startswith(("unchanged", "existing")))
        audit.record(conn, who, "aidb.setup", "", {"steps": len(steps), "changed": changed, "reseal": bool(args.reseal)})
        conn.execute("INSERT INTO admin_audit (actor, action, detail) VALUES (%s,%s,%s)",
                     (who, "aidb.setup", psycopg.types.json.Jsonb({"steps": len(steps), "changed": changed})))
        conn.commit()
    for s in steps:
        print(s)
    print("done: AIDB mode is set up and sealed. Switch features on with PAYBROKER_RETRIEVAL_BACKEND=aidb, PAYBROKER_TRIAGE_METHOD=aidb_agent, "
          "PAYBROKER_ANALYST_QA=aidb, then restart.")
    return 0


def _aidb_status(args) -> int:
    import json

    from . import aidb_mode
    from .db import connect

    with connect() as conn:
        _unlock_vault(conn)                       # without it the seals cannot be checked: the report then says so
        s = aidb_mode.inspect(conn)
        conn.rollback()
    problems = s["state"] != "ready" or any(not f["ready"] for f in s["features"].values())
    if args.json:
        print(json.dumps(s, indent=2, default=str))
        return 1 if problems else 0
    print("requested:", ", ".join(f"{k}={v}" for k, v in s["requested"].items()))
    print(f"AIDB: {'available, version ' + str(s['aidb']['version']) if s.get('aidb', {}).get('available') else 'not available'}; state: {s['state']}"
          + (f" ({s['reason']})" if s["reason"] else ""))
    for t in s["tampered"]:
        print(f"  TAMPERED {t['kind']} {t['name']}: {t['problem']}")
    for name, f in s["features"].items():
        print(f"  {name:<10} {'ready' if f['ready'] else 'not ready: ' + f['reason']}")
    if s["state"] != "unavailable":
        with connect() as conn:
            g = aidb_mode.governance(conn)
            conn.rollback()
        if g["available"]:
            print("  governance " + (", ".join(f"{a}: " + "/".join(f"{n} {st}" for st, n in sorted(c.items())) for a, c in sorted(g["counts"].items())) or "no decisions in 24 hours"))
        else:
            print(f"  governance not available: {g['reason']}")
    return 1 if problems else 0


def _reset_data(args) -> int:
    import getpass

    from . import maintenance

    if not args.yes:
        print("refusing to delete without --yes (this cannot be undone)", file=sys.stderr)
        return 2
    try:
        maintenance.check_phrase(args.confirm)
    except maintenance.ResetError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        out = maintenance.reset(actor=f"cli:{getpass.getuser()}", include_training_data=not args.keep_training_data,
                                include_rule_suggestions=not args.keep_rule_suggestions)
    except maintenance.ResetBusy as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for table, n in out["deleted"].items():
        print(f"{table}: {n} deleted")
    for table in out["skipped"]:
        print(f"{table}: kept")
    print(f"done in {out['seconds']} s; the configuration (merchants, keys, webhooks, connectors, rules, settings, models, users) was kept")
    return 0


def _serve() -> None:
    import os

    import uvicorn

    from .config import bind_host

    # Forwarded headers are trusted only from the proxy in front (never "*": any caller could then choose its own address).
    uvicorn.run("paybroker.api:app", host=bind_host(), port=int(os.environ.get("PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips=os.environ.get("PAYBROKER_FORWARDED_ALLOW_IPS", "127.0.0.1"))


def _start() -> int:
    from apgkit import sealed as sealing

    if sealing.Sealed.from_env().enabled:      # sealed boot: no database URL until the operator unseals; the API migrates then
        print("sealed boot: migrations run after unseal")
        _serve()
        return 0
    from .migrate import run

    try:
        applied = run()   # the connection retries with backoff, so a waking database is tolerated
    except Exception as exc:  # noqa: BLE001 - surface a readable cause, then fail the container
        print(f"startup failed while migrating: {exc}\n"
              "Check that the database is reachable. If the error mentions the vector extension, enable it first, e.g. on AgentPG:\n"
              "  agentpg extensions create <instance-id> vector", file=sys.stderr)
        return 1
    print("migrations applied:", ", ".join(applied) if applied else "none (up to date)")
    _serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
