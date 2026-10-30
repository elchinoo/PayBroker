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
    elif args.cmd == "reset-data":
        return _reset_data(args)
    elif args.cmd == "serve":
        _serve()
    elif args.cmd == "start":
        return _start()
    return 0


def _rotate_vault_key(args) -> int:
    import getpass

    from . import audit, vault
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
        conn.rollback()
        try:
            counts = vault.rotate_data_key(conn, password=password if has_password else None)
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
