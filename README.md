<p align="center"><img src="src/paybroker/static/logo.svg" alt="PayBroker logo" width="96" height="96"></p>

<h1 align="center">PayBroker</h1>

An **API-driven payment broker** sample on **Postgres + pgvector**: one payment API in front of several (simulated) payment
processors, with **smart routing**, a **three-tier fraud engine** (rules, an in-process gradient-boosted model, asynchronous LLM
triage), a **card vault** that takes test cards only, **signed webhooks**, a web dashboard, a REST API and an **MCP server**.

> [!IMPORTANT]
> **This is a teaching sample, not a payment system.** The processors are simulations, the vault accepts only the public
> processor test cards (and numbers in the reserved test range `999999`), and the fraud model is trained on **synthetic**
> data. It shows the mechanics (idempotency, routing, scoring, case handling, webhooks, observability) so you can adapt
> them. It is not PCI-certified and must never hold real card data.

## What it does

- **One API** (`/v1`, see [API.md](API.md)): tokenize a test card, authorize, capture, void, refund. Safe to retry with an `idempotency_key`.
- **Smart routing:** each payment goes to the eligible connector with the best score
  `S = W_auth × auth rate − W_cost × fee − W_latency × latency`, using live, smoothed numbers. Hard constraints come first
  (currency, data-residency region, a merchant's maximum fee). On a timeout or 5xx it **cascades** to the next best connector
  within an 800 ms budget; an issuer decline is final and is not retried elsewhere.
- **Three-tier fraud engine**
  1. **Tier 1, rules** (about 0.01 ms): blocked lists, hard velocity limits, merchant 3-D Secure thresholds and JSON rules you
     write. A rule is *data*, never code: nested all/any groups of comparisons, validated strictly.
  2. **Tier 2, model** (about 0.05 ms): a gradient-boosted tree model scores 0 to 1000 from 17 features read from the feature
     store. **Under 300 approve, 300 to 699 ask for 3-D Secure, 700 and above decline and open a case.** Every score comes with reason
     codes. The model is XGBoost trees exported to JSON and walked in pure Python: no native dependency, and a test proves it
     reproduces XGBoost's own predictions exactly.
  3. **Tier 3, an AI agent** (asynchronous): for flagged payments a language model reads derived features and the nearest past
     cases (found with **pgvector**), then writes a category, a confidence, a summary and a recommended action for the human analyst.
     Once a day it also **proposes new rules** from recent confirmed fraud. Its output is validated against a strict schema;
     **suggested rules do nothing until a person approves them**, and the AI never decides a case. Without an AI account a
     built-in rule-based triage takes over.
- **Feature store in Postgres:** per-user, device, card and IP event lists (the last 200 attempts), so velocity and spend are exact
  and need no Redis.
- **Card vault:** keeps the brand, BIN, last four, expiry and a keyed hash of the number. **Never** the number or the CVC.
- **Webhooks:** `payment.authorized`, `.captured`, `.declined`, `.refunded` and others, signed with HMAC-SHA256, retried with
  back-off for about a day, every attempt visible.
- **3-D Secure:** a hosted, simulated issuer page where the tester picks the outcome.
- **Dashboard** at `/`: overview, payments with a full explanation of every decision, fraud cases, rules and suggestions,
  merchants, routing, settings. **MCP server** at `/mcp` with read-only tools so an assistant can investigate but never decide.
- **Prometheus** at `/metrics` (a metrics-only token): the fleet's shared families plus `payment_broker_*`.
- **Sign-in:** standalone (a setup code and an admin password) or **single sign-on** with the ecosystem
  [identity provider](https://github.com/elchinoo/identity), with roles. Built on [agentpg-kit](https://github.com/elchinoo/agentpg-kit).

## Quick start

Requires Docker and [uv](https://docs.astral.sh/uv/) (or any Python 3.11+ environment).

```bash
cp .env.example .env              # then set POSTGRES_PASSWORD; .env is git-ignored
docker compose up -d db
uv venv && uv pip install -e ".[dev]"      # uses the sibling ../agentpg-kit checkout; images use the published kit
set -a; . ./.env; set +a
.venv/bin/paybroker migrate
.venv/bin/paybroker serve         # http://localhost:8000
```

The first start prints a **one-time setup code** to the console. Open http://localhost:8000, enter it and choose an admin
password. Then **Settings > Demo traffic** sends fictional payments through the real pipeline (ordinary customers, issuer
declines, 3-D Secure, card testing, account takeover) so every screen has something to show. From a terminal:
`.venv/bin/paybroker demo -n 150`.

Everything in containers: `docker compose up --build`, then read the setup code from `docker compose logs api`.

## Take a payment

Create a merchant and its keys under **Merchants** (or with the operator API), then:

```bash
# from the customer's browser, with the publishable key: the card number never touches your server
curl -s -X POST localhost:8000/v1/vault/tokens -H "Authorization: Bearer pk_test_..." -H "Content-Type: application/json" \
  -d '{"number":"4242424242424242","exp_month":12,"exp_year":2035,"cvc":"123"}'

# from your server, with the secret key
curl -s -X POST localhost:8000/v1/payments/authorize -H "Authorization: Bearer sk_test_..." -H "Content-Type: application/json" -d '{
  "idempotency_key": "order-1001", "amount": 4999, "currency": "USD",
  "payment_method": {"type": "card", "card_token": "tok_vault_card_..."},
  "customer": {"id": "cus_42", "email": "pat@example.com", "ip_address": "198.51.100.20", "account_age_hours": 4000},
  "telemetry": {"device_fingerprint": "dev_abc", "checkout_duration_ms": 41000, "pasted_card_number": false},
  "shipping_address": {"country": "US"}}'
```

Amounts are in minor units (cents). Test cards that behave differently:

| Number | Behaviour |
|---|---|
| `4242 4242 4242 4242`, `5555 5555 5555 4444`, `3782 822463 10005`, `6011 1111 1111 1117` | approve |
| `4000 0000 0000 0002` | the issuer declines (`do_not_honor`) |
| `4000 0000 0000 9995` | `insufficient_funds` |
| `4100 0000 0000 0019` | the issuer declines as `stolen_card` |
| `4000 0025 0000 3155` | the issuer always demands 3-D Secure |
| `4000 0000 0000 0119` | the first connector answers 5xx; the cascade recovers on the next |
| any Luhn-valid number starting `999999` | random behaviour from each connector's configured rates |

Anything else is refused (HTTP 422): the vault is for test cards only.

`customer.account_age_hours` is optional and matters: it is how old the customer's account is *at the merchant*. Without it the
broker assumes an established account (30 days), because "I have never seen this customer" is not the same as "this is a new
account" and treating it so would put every first payment into 3-D Secure.

## How a decision is made

1. **One database transaction** inserts the payment (the unique `(merchant, idempotency_key)` index makes a concurrent retry
   replay the first result instead of charging twice), reads the feature store, evaluates Tier 1 and Tier 2, routes, and
   commits everything together with the assessment, the case and the queued work.
2. The decision is the stricter of Tier 1's verdict and the score band. A decline never reaches a connector.
3. Flagged payments (score 700 or higher) open a **fraud case** and queue the AI triage. An analyst resolves the case
   (`PERMANENT_BLOCK` also blocks the device, IP and card; `ALLOW_WITH_KYC`; `DISMISS_ALERT`).
4. Chargebacks feed back into the feature store, so a device linked to a chargeback scores higher next time.

The queue is a Postgres table (`outbox`) claimed with `FOR UPDATE SKIP LOCKED`, the Kafka of the reference design; run several
workers with `PAYBROKER_ROLE=worker`.

### Measured, not promised

`make bench` runs 400 authorizations against a local Postgres (connectors instant, one connection): the fraud engine takes
about **1.5 ms** (p95 3 ms) of which Tier 1 is about 0.01 ms and Tier 2 about 0.05 ms, and a full authorization about
**5 ms** (p95 9 ms). Real connectors add their own latency, and a remote database adds a round trip per query.

## Roles and single sign-on

Standalone, the first-run admin has everything. Federated with the identity provider (`PAYBROKER_OIDC_ISSUER`, client id
`paybroker`, redirect `https://<paybroker>/auth/callback`):

| Role | May |
|---|---|
| `admin` | everything |
| `risk_manager` | read, resolve cases, record chargebacks, manage rules and decide on suggestions, use MCP |
| `analyst` | read, resolve cases, record chargebacks, use MCP |
| `operator` | read, manage merchants, keys, webhooks and connectors, use MCP |
| `viewer` | read and use MCP |

Services use `client_credentials` with scope `paybroker.read` or `paybroker.metrics`. Merchant API keys are a different kind
of credential: they can reach only the payment API, never an operator route, and operator credentials cannot make payments.

## Configuration

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Postgres connection string (`DB_EXTERNAL_DSN` and the `PG*` variables also work). |
| `PAYBROKER_ROLE` | `api` (HTTP only), `worker` (background jobs only) or `all` (default). |
| `PAYBROKER_PUBLIC_URL` | Base URL used in 3-D Secure links (default: the request's own address). |
| `PAYBROKER_ENCRYPTION_KEY[_FILE]` | Platform secret that lets the vault unlock itself after a restart (webhook secrets and AI keys need it). |
| `PAYBROKER_API_KEY[_FILE]` | Optional static full-access operator key. Prefer operator tokens. |
| `PAYBROKER_ALLOW_PRIVATE_URLS` | Allow AI provider and webhook URLs on private networks (local testing only). |
| `PAYBROKER_POOL_MAX` | Size of the connection pool used by the payment path (default 20). |
| `PAYBROKER_SIM_LATENCY` | `off` makes the simulated connectors answer instantly (tests and benchmarks). |
| `PAYBROKER_OIDC_*`, `PAYBROKER_LOCAL_LOGIN` | Single sign-on, see above. |

## Deploy on AgentPG

```bash
agentpg sandbox create paybroker --image ghcr.io/elchinoo/paybroker:latest --instance <instance-id> --port 8000 \
  --env PAYBROKER_PUBLIC_URL=https://<sandbox-url>
agentpg extensions create <instance-id> vector
agentpg sandbox logs <sandbox-id>        # the one-time setup code
```

A sandbox that pauses itself answers an empty `503`: `agentpg sandbox resume <sandbox-id>`. After a restart the vault is locked
until you sign in (or `PAYBROKER_ENCRYPTION_KEY` is set), so webhooks wait instead of failing. AgentPG sandboxes speak HTTP and
Postgres only, which is all this needs.

## Limits worth knowing

- The connectors are simulations; the 3-D Secure page lets the tester choose the outcome.
- The model learned from synthetic data. Replace `models/fraud_gbdt.json` (same format; `scripts/train_model.py` shows how to
  export) with a model trained on your own labelled history before drawing any conclusion from a score.
- The geo lookup is a tiny table over documentation IP ranges; pass `telemetry.ip_country` from your edge in real use.
- Per-process, in-memory caches (merchant settings 5 s, API keys 30 s, rules and connectors 5 s): a change reaches other
  processes within seconds, and immediately in the process that made it.
- One currency per payment, no settlement, payouts, disputes workflow or reconciliation.

## Development

```bash
make lint
make test                                     # no database needed: rules, model, routing, signing, validation
make test-db                                  # the whole broker against Postgres (needs POSTGRES_PASSWORD exported)
make train                                    # retrain the model on synthetic data (uv pip install -e ".[train]")
make bench
```

See [SECURITY.md](SECURITY.md) for the security model and its limits, and [API.md](API.md) for every endpoint.
