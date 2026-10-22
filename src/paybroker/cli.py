import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="paybroker")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply SQL migrations")
    demo = sub.add_parser("demo", help="create a demo merchant and send simulated traffic through the broker")
    demo.add_argument("-n", type=int, default=200, help="number of payments")
    demo.add_argument("--seed", type=int, default=7)
    sub.add_parser("serve", help="run the API + MCP server")
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
    elif args.cmd == "serve":
        _serve()
    elif args.cmd == "start":
        return _start()
    return 0


def _serve() -> None:
    import os

    import uvicorn

    uvicorn.run("paybroker.api:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))


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
