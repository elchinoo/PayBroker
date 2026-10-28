# PayBroker guide

The complete guide to PayBroker: what it is, how it fits the ecosystem, how to install, configure, use, operate and develop it.
It complements [README.md](../README.md) (the overview and the model and training details), [API.md](../API.md) (every endpoint) and
[SECURITY.md](../SECURITY.md) (the security model). Where this guide and those documents differ, the code is the reference and
the difference is listed under [Known limitations](#16-known-limitations).

> [!IMPORTANT]
> PayBroker is a **teaching sample, not a payment system.** The processors are simulations, the vault accepts only public
> processor test cards (and numbers in the reserved test range `999999`), and the fraud model is trained on synthetic data. Never
> point real card data or real payment traffic at it.

## Contents

1. [Purpose and audience](#1-purpose-and-audience)
2. [How it fits the ecosystem](#2-how-it-fits-the-ecosystem)
3. [Architecture](#3-architecture)
4. [Install](#4-install)
5. [Configuration reference](#5-configuration-reference)
6. [First-run walkthrough](#6-first-run-walkthrough)
7. [Using PayBroker](#7-using-paybroker)
8. [Roles and permissions](#8-roles-and-permissions)
9. [Security notes](#9-security-notes)
10. [Operations](#10-operations)
11. [Troubleshooting](#11-troubleshooting)
12. [Testing and development](#12-testing-and-development)
13. [AIDB and AgentPG integration](#13-aidb-and-agentpg-integration)
14. [Command-line reference](#14-command-line-reference)
15. [Data model at a glance](#15-data-model-at-a-glance)
16. [Known limitations](#16-known-limitations)
17. [EDB and AgentPG capabilities used by this app](#17-edb-and-agentpg-capabilities-used-by-this-app)

---

## 1. Purpose and audience

PayBroker is a **payment broker**: one payment API in front of several (simulated) payment processors. A merchant integrates once
and the broker decides, for every payment, whether it is safe, which processor should handle it, and what to do when a processor
fails. It is built on **Postgres with pgvector**, with no Redis, Kafka or other infrastructure, and it is one of the sample apps in
the EDB AIDB + AgentPG showcase.

What it demonstrates:

- **A payment API with the properties that matter:** idempotency keys, authorize then capture, void, refund, signed webhooks, a
  card vault that never stores a card number, a hosted 3-D Secure step.
- **Smart routing:** each payment goes to the eligible connector with the best score of authorization rate, fee and latency, with a
  cascade to the next connector on a timeout or 5xx.
- **A three-tier fraud engine:** deterministic rules (Tier 1), a gradient-boosted model walked in pure Python (Tier 2), and
  asynchronous LLM triage with nearest past cases found by pgvector (Tier 3). The AI recommends; a person decides.
- **Operations around the model:** a dashboard that explains every decision, a read-only MCP server for assistants, Prometheus
  metrics, optional training-data capture, model upload and rollback, and a data reset.

Who it is for:

| Audience | What they get from it |
|---|---|
| Developers integrating a shop or simulator | A realistic merchant API (`/v1`), test cards with deterministic behaviours, browser tokenization with CORS, a 3-D Secure return, signed webhooks |
| Fraud analysts and risk managers | The dashboard: scores with reason codes, cases with AI triage, rules and suggested rules, a block list, chargebacks |
| Operators | Merchants, keys, webhooks, connectors, AI accounts and limits, models, data reset, metrics |
| People evaluating Postgres + pgvector + AI | A complete small system whose state, queue, feature store and nearest-case search all live in one Postgres |
| AI assistants (through MCP) | Read-only tools to investigate payments and explain a decision |

## 2. How it fits the ecosystem

PayBroker is one app of a fleet that runs on AgentPG, each app in its own sandbox with its own database. The fleet is described in
`ecosystem/ecosystem.yaml` and deployed with `ecoctl`. URLs change when a sandbox is recreated, so nothing here (or in any config)
should hard-code one: use the environment variables ecoctl injects.

```mermaid
flowchart LR
  subgraph ids[Identity and shared services]
    IDP[identity<br/>OIDC provider]
    GW[llm-gateway<br/>aliases, limits, budgets]
  end
  KIT[agentpg-kit<br/>library, linked at build time]
  PB[PayBroker]
  SF[ShopFront<br/>web shop]
  SIM[CityPulse Simulator<br/>cpsim]
  ECO[ecoctl<br/>provisioning]
  PL[PagerLens]
  TD[TrustDesk]
  KIT -. library .-> PB
  PB -- verifies sign-in tokens, JWKS --> IDP
  ECO -- service token paybroker.admin --> PB
  PB -- chat completions, LLM_API_KEY --> GW
  SF -- pk_test / sk_test merchant keys --> PB
  PB -- signed webhooks --> SF
  SIM -- sk_test merchant key --> PB
  PL -. scrapes /healthz /metrics, optional .-> PB
```

### Per project

| Project | Direction | How they interact |
|---|---|---|
| **agentpg-kit** (`apgkit`) | PayBroker uses it | Not a service: a library installed with the app (`agentpg-kit>=0.2.5`; the image pins the `v0.2.5` tag). It provides the database helper (retry for a database that is scaled to zero, pgvector adapter, pool), migrations runner, vault, OIDC token verification and relying party, local admin and sessions, operator tokens, the security middleware (CSP, 64 KB body cap, CSRF header, request metrics), the outbound URL guard, the LLM HTTP client, the shared UI (`/kit/*`) and the Prometheus base metrics. App code is in `src/paybroker/core.py` (the kit instances and the permission model). |
| **identity** | PayBroker trusts it | Optional single sign-on. With `PAYBROKER_OIDC_ISSUER` set, people sign in through identity (authorization code + PKCE, with state and nonce; client id `paybroker`, redirect `https://<paybroker>/auth/callback`). Services and people call the API with identity-issued JWTs, verified locally against the provider's JWKS (RS256, ES256 or EdDSA only; issuer, audience `paybroker`, expiry checked). Only the `paybroker` roles in a token count. Service clients use `client_credentials` with scopes `paybroker.read`, `paybroker.metrics`, `paybroker.admin`. Without the issuer variable PayBroker runs standalone with a setup code and an admin password. |
| **llm-gateway** | PayBroker calls it | The environment contract `LLM_BASE_URL` (`<gateway>/v1`), `LLM_API_KEY` (an `lg_...` client key, not an identity token) and `LLM_CHAT_MODEL` (an alias such as `chat-default`) becomes a built-in, read-only AI account named *Environment (server settings)*. Tier 3 triage and the daily rule suggestions use it by default. PayBroker only calls `POST {LLM_BASE_URL}/chat/completions`. It never sends card data, names or e-mail addresses. The gateway's per-client limit (60 requests a minute) and monthly token budget are why PayBroker throttles its own AI calls (section [7.9](#79-ai-triage-and-its-limits)). |
| **ShopFront** | Calls PayBroker | The shop's browser tokenizes a test card with a **publishable key** from an allowed origin (CORS) and the shop's server authorizes, captures, voids and refunds with a **secret key**. PayBroker calls the shop's webhook (signed) and, after 3-D Secure, redirects the customer to the shop's signed `return_url`. `ecoctl connect` wires the two without copying anything by hand (below). ShopFront's own side is documented in its repository. |
| **CityPulse Simulator** (`cpsim`) | Calls PayBroker | Drives payment workloads with a merchant **secret key** that the simulator keeps in its credential store (an identity scope cannot carry a merchant key). It gets PayBroker's address through the manifest key `uses: [paybroker, identity]`, injected as `PAYBROKER_URL`; recreating PayBroker recreates the simulator so the address follows. Use the `999999xx` test-card prefixes (README) so each simulated customer has its own card; one public number shared by many payments has a huge per-card velocity and is rejected by the fraud engine before it reaches any processor. |
| **ecoctl** | Provisions it | Creates PayBroker's database (extension `vector`) and sandbox, generates `PAYBROKER_ENCRYPTION_KEY`, registers the web client `paybroker` and its five roles in identity, injects the `PAYBROKER_OIDC_*` and `LLM_*` variables, and creates group grants. Its own service client `ecoctl` calls the merchant API with scope `paybroker.admin` (see below). |
| **PagerLens** | Optional observer | PayBroker does not call it. PagerLens's agent can probe `/healthz` and `/readyz` and scrape `/metrics` with a metrics-only token (`paybroker.metrics` scope or a local token of scope `metrics`). The shared metric families (`http_requests_total`, `db_up`, ...) come from the kit so one agent preset reads every app. |
| **TrustDesk**, **CityPulse** | None | No direct interaction. They share the same gateway and the same kit conventions. |

### Authentication of each caller

| Caller | Credential | Reaches | Set up by |
|---|---|---|---|
| Customer's browser | Publishable key `pk_test_...` | `POST /v1/vault/tokens` only, from an allowed origin | A merchant's keys and origins |
| Shop or simulator server | Secret key `sk_test_...` | The whole `/v1` payment API for its own merchant | A merchant's keys |
| Person in the dashboard | Session cookie (local password or SSO) | `/api/*` per role | First-run setup or identity |
| Script or MCP client | Operator token `pbt_...` (scope `read`, `metrics`, `full`) or an identity JWT | `/api/*`, `/mcp`, `/metrics` per scope | Settings > Security, or identity |
| `ecoctl` | Identity service token, scope `paybroker.admin`, audience `paybroker` | The merchant routes of `/api` only | `ecoctl apply` |
| PayBroker to a shop | HMAC-SHA256 signature with the merchant's webhook secret | The shop's webhook URL | Merchant webhook setting |
| PayBroker to the gateway | `LLM_API_KEY` bearer | `{LLM_BASE_URL}/chat/completions` | ecoctl (`llm: true`) |

Merchant keys and operator credentials never cross: a merchant key is refused on every operator route and on `/mcp`, and an
operator credential is refused on the payment API.

### What `ecoctl connect` does on the PayBroker side

The manifest declares `connect: {paybroker: {merchant: "ShopFront"}}` on the shop. After both apps are up (also at the end of
`apply` and `recreate`), ecoctl:

1. Gets an identity service token (`client_credentials`, scope `paybroker.admin`, audience `paybroker`). That scope maps to exactly
   `merchants:manage` and `payments:read` (see [section 8](#8-roles-and-permissions)), and works only on a **service** token.
2. `GET /api/merchants?name=ShopFront`; if the list is empty, `POST /api/merchants {"name": "ShopFront"}` (a 409 means it exists).
3. `PATCH /api/merchants/{id} {"allowed_origins": [<the shop's current URL>]}`. The list is replaced as a whole, so a moved shop works
   again without a visit to PayBroker.
4. Creates two keys named `ecoctl:shopfront:secret` and `ecoctl:shopfront:publishable` (`POST /api/merchants/{id}/keys`, the value is
   returned once and kept in ecoctl's state file) and revokes stale ones with the same name.
5. `PUT /api/merchants/{id}/webhook {"url": "<shop>/webhooks/paybroker"}`. **Every PUT rotates the signing secret**, so ecoctl sends it
   only when the URL changed or the secret is lost.
6. Pushes PayBroker's URL, the two keys and the signing secret to the shop, then runs the shop's connection test.

Every one of these calls leaves an `admin_audit` row whose actor is the service's client id (no row holds a key or a secret). Rotating
the shop's keys and webhook secret is `ecoctl connect --rotate`. Details: `ecosystem/docs/DEPLOY.md`, section 9b.

## 3. Architecture

### Components

```mermaid
flowchart TB
  subgraph clients[Callers]
    BR[Customer browser]
    SRV[Shop / simulator server]
    UI[Dashboard<br/>static pages + /kit]
    AST[Assistant<br/>MCP client]
    PRM[Prometheus / PagerLens agent]
  end

  subgraph app[PayBroker process: FastAPI + uvicorn]
    MW[Middleware<br/>BrowserCors, SecurityGuard, McpGuard]
    V1[Payment API /v1]
    OP[Operator API /api]
    TDS[Hosted 3-D Secure /3ds]
    MCP[MCP server /mcp<br/>read-only]
    PIPE[Pipeline<br/>payments.py]
    T1[Tier 1 rules]
    FS[Feature store]
    T2[Tier 2 model<br/>pure Python trees]
    RT[Routing + simulated PSPs]
    WRK[Worker thread<br/>outbox consumer]
  end

  subgraph pg[Postgres + pgvector]
    TX[(transaction,<br/>fraud_assessment<br/>vector 17, HNSW)]
    FSE[(fs_entity)]
    OB[(outbox)]
    CFG[(merchant, rules,<br/>connectors, settings)]
  end

  IDP[identity]
  GW[llm-gateway]
  HOOK[Merchant webhook]

  BR -- pk_test --> MW
  SRV -- sk_test --> MW
  UI --> MW
  AST --> MW
  PRM --> MW
  MW --> V1 & OP & TDS & MCP
  V1 --> PIPE
  PIPE --> T1 --> T2 --> RT
  PIPE <--> FS
  FS <--> FSE
  PIPE --> TX
  PIPE --> OB
  OP --> CFG
  WRK -- FOR UPDATE SKIP LOCKED --> OB
  WRK -- chat completions --> GW
  WRK -- signed POST --> HOOK
  WRK --> TX
  app -- JWKS --> IDP
```

One process serves the HTTP APIs and (with `PAYBROKER_ROLE=all`, the default) runs one background worker thread. `api` runs HTTP
only (plus a small thread that flushes connector statistics), `worker` is meant for extra processes that only consume the queue
(a worker process still serves the app's HTTP port, because both roles start the same ASGI app; the role only decides whether the
queue consumer thread runs).

### Life of one authorization

```mermaid
sequenceDiagram
  participant S as Shop server
  participant P as PayBroker (one DB transaction)
  participant F as Feature store (Postgres)
  participant C as Connector (simulated)
  participant O as Outbox / worker
  participant M as Merchant webhook
  S->>P: POST /v1/payments/authorize (sk_test, idempotency_key)
  P->>P: INSERT transaction ON CONFLICT (merchant, key) DO NOTHING
  P->>F: read user, device, card, IP documents (one query)
  P->>P: Tier 1 rules, then Tier 2 score (17 features)
  alt score under 300 and no rule
    P->>C: best-ranked connector, cascade on 5xx/timeout (800 ms budget)
    C-->>P: approved or issuer decline
  else 300 to 699 or a challenge rule
    P-->>S: 200 REQUIRES_ACTION + redirect_url (3-D Secure)
  else 700 or more, or a decline rule
    P-->>S: 403 FRAUD_REJECTED (connector never called)
  end
  P->>O: insert webhook event, triage job (score 700 or more), in the same transaction
  P-->>S: response
  O->>M: POST signed event (retried with back-off)
  O->>O: Tier 3 triage, off the payment path
```

The whole authorization (idempotency record, feature read, assessment, case, queued work, feature-store write) commits in one
transaction. A second request with the same `(merchant, idempotency_key)` blocks on the unique index until the first commits, then
replays its stored result; the same key with a different body is `422 IDEMPOTENCY_KEY_REUSED`.

### Modules

| Module | Role |
|---|---|
| `api.py`, `api_admin.py`, `api_training.py` | Routes: merchant API and hosted 3-D Secure; operator API; training and model API |
| `core.py`, `auth.py`, `cors.py`, `origins.py` | Kit instances, permission model, merchant keys, CORS for tokenization, origin validation |
| `payments.py`, `cards.py`, `routing.py`, `psp.py` | Pipeline, test-card vault, ranking and cascade, simulated connectors |
| `rules.py`, `features.py`, `ml.py`, `modelstore.py`, `fraud.py` | Tier 1, feature store, Tier 2, uploaded models, score bands (300 and 700) |
| `triage.py`, `triage_limits.py`, `suggest.py`, `accounts.py`, `chat.py` | Tier 3, its throttle and breaker, rule suggestions, AI accounts, one chat call |
| `worker.py`, `webhooks.py` | Outbox consumer, webhook signing and delivery |
| `training_data.py`, `trainer.py`, `datasets.py`, `synth.py`, `scenarios.py` | Training-data capture, trainer, public-dataset adapters, synthetic generator, scoring scenarios |
| `maintenance.py`, `demo.py`, `insights.py`, `cases.py`, `mcp_server.py`, `metrics.py` | Data reset, demo traffic, read views, analyst actions, MCP tools, Prometheus |
| `hardening.py`, `namematch.py` | Read-only role hardening (masked views, hidden secrets tables), `NAME_MISMATCH` via fuzzystrmatch (section 17) |
| `static/` | The dashboard (plain ES modules, no build step) and the hosted 3-D Secure page |

## 4. Install

PayBroker needs Postgres with the `vector` extension (the compose file and CI use `pgvector/pgvector:pg17`; other versions were not checked) and Python 3.11 or later
(the image uses Python 3.14). The migrations run `CREATE EXTENSION IF NOT EXISTS vector`, so the database role must be allowed to do that,
or the extension must be created first.

### 4.1 Local development with a throwaway Postgres

Requires Docker and [uv](https://docs.astral.sh/uv/). The sibling checkout `../agentpg-kit` is used as an editable dependency
(`[tool.uv.sources]` in `pyproject.toml`).

```bash
cp .env.example .env                       # git-ignored; then set POSTGRES_PASSWORD (python3 -c "import secrets; print(secrets.token_urlsafe(24))")
docker compose up -d db                    # pgvector/pgvector:pg17 on 127.0.0.1:5435, volume pgdata
uv venv && uv pip install -e ".[dev]"
set -a; . ./.env; set +a                   # DATABASE_URL points at 127.0.0.1:5435
.venv/bin/paybroker migrate
.venv/bin/paybroker serve                  # http://localhost:8000 (HOST and PORT override)
```

`.env.example` defines `DATABASE_URL=postgresql://paybroker:${POSTGRES_PASSWORD}@127.0.0.1:5435/paybroker`; the `${...}` expands because the
file is sourced by the shell. The first start prints a one-time setup code (section [6](#6-first-run-walkthrough)). To throw everything away:
`docker compose down -v` removes the database volume.

A database without compose works the same way: any Postgres with pgvector, `DATABASE_URL` pointing at it.

### 4.2 Everything in containers (docker compose)

```bash
cp .env.example .env                       # set POSTGRES_PASSWORD
docker compose up --build                  # db + api
docker compose logs api                    # the setup code
```

The `api` service builds the `Dockerfile`, receives `DATABASE_URL` for the `db` service and runs `paybroker start`, which applies the
migrations and then serves on `127.0.0.1:8000`. The compose file is for local development only.

### 4.3 The container image on its own

`ghcr.io/elchinoo/paybroker:latest` is built by `.github/workflows/build-image.yml` on every push to `main` (after `ruff` and the tests
pass against a Postgres service), on version tags and nightly. The image runs as a non-root user, listens on `PORT` (default 8000) on
`HOST` (default `0.0.0.0`), has a `HEALTHCHECK` on `/healthz`, and starts with `paybroker start` (migrate, then serve). The database URL
arrives at runtime; the image holds no credentials.

### 4.4 Deploy with ecoctl (the fleet)

PayBroker is entry `paybroker` in `ecosystem/ecosystem.yaml`:

```yaml
  paybroker:
    name: PayBroker
    llm: true
    extensions: [vector]
    generate: [PAYBROKER_ENCRYPTION_KEY]
    roles: [admin, risk_manager, analyst, operator, viewer]
    exposes: [paybroker.read, paybroker.metrics, paybroker.admin]
```

From `ecosystem/` (requires the `agentpg` CLI, logged in):

```bash
.venv/bin/ecoctl validate
.venv/bin/ecoctl plan                      # every command and API call; changes nothing
.venv/bin/ecoctl apply --only paybroker --execute    # identity first, the gateway (llm: true), then PayBroker
.venv/bin/ecoctl status                    # health, readiness, the LLM client and one real call
.venv/bin/ecoctl env paybroker             # the environment the sandbox is created with (secrets masked; --reveal prints them)
.venv/bin/ecoctl recreate paybroker        # pull a new image; callers and `uses` apps are recreated too (the URL changes)
.venv/bin/ecoctl connect --dry-run         # what connecting ShopFront to PayBroker would change
```

`ecoctl env paybroker` shows the exact contract; as read from `ecosystem/src/ecoctl/plan.py` it contains `PAYBROKER_OIDC_ISSUER`,
`PAYBROKER_OIDC_CLIENT_ID=paybroker`, `PAYBROKER_OIDC_CLIENT_SECRET`, `PAYBROKER_ENCRYPTION_KEY` (generated), and, because of `llm: true`,
`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_CHAT_MODEL`, `EMBEDDING_MODEL`, `EMBEDDING_DIM`. PayBroker does not read `EMBEDDING_*` (its nearest-case
search uses the payment's own 17-number feature vector, not text embeddings). Note that ecoctl does **not** set `PAYBROKER_PUBLIC_URL`;
PayBroker then derives its address from the request's `Host` and `X-Forwarded-Proto` headers (AgentPG terminates TLS in front of the
sandbox), which is what the 3-D Secure `redirect_url` uses.

To deploy by hand with the `agentpg` CLI (as in the README):

```bash
agentpg sandbox create paybroker --image ghcr.io/elchinoo/paybroker:latest --instance <instance-id> --port 8000 \
  --env PAYBROKER_PUBLIC_URL=https://<sandbox-url>
agentpg extensions create <instance-id> vector
agentpg sandbox logs <sandbox-id>          # the one-time setup code (standalone mode)
```

A sandbox that pauses itself answers an empty `503`: `agentpg sandbox resume <sandbox-id>`.

## 5. Configuration reference

### 5.1 Environment variables

All of these were checked against `src/paybroker/config.py`, `core.py` and the modules named. A variable that can come from a file follows
the kit rule: the variable wins, else the first line of the file named by `<NAME>_FILE`, else the default file.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | none | Postgres connection string. If unset, `DB_EXTERNAL_DSN`, then `PSQL_CONNECTION_STRING`; if all are unset the empty DSN makes libpq use `PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`. |
| `PAYBROKER_ROLE` | `all` | `api` (HTTP and connector-statistics flush only), `worker` (queue consumer; still serves HTTP) or `all`. Any other value means `all`. |
| `PAYBROKER_PUBLIC_URL` | request address | Base URL put in 3-D Secure `redirect_url` and used for the SSO redirect URI. Trailing slash removed. |
| `PAYBROKER_POOL_MAX` | `20` | Maximum size of the connection pool used by the payment path (minimum 2 are opened). |
| `HOST` | `0.0.0.0` | Bind address of `paybroker serve` and `paybroker start`. |
| `PORT` | `8000` | Port of the same. |
| `PAYBROKER_ENCRYPTION_KEY`, `_FILE` (default `/etc/paybroker/encryption_key`) | none | Platform secret that wraps the vault's data key a second time, so the vault unlocks itself after a restart. Without it, webhook secrets and stored AI keys stay locked until an admin signs in. In SSO-only mode it is what creates the vault. |
| `PAYBROKER_API_KEY`, `_FILE` (default `/etc/paybroker/api_key`) | none | Optional static full-access operator key (login `api_key` and bearer). Prefer operator tokens. |
| `PAYBROKER_OIDC_ISSUER` | none | The identity provider's URL. Single sign-on is off when unset. |
| `PAYBROKER_OIDC_CLIENT_ID` | `paybroker` | This app's client id at the provider; also the token audience PayBroker requires. |
| `PAYBROKER_OIDC_CLIENT_SECRET`, `_FILE` | none | The web client's secret. Empty means a public client (PKCE only). |
| `PAYBROKER_LOCAL_LOGIN` | `true` | With SSO on, `false` removes the local admin-password and static-key logins (they remain a break-glass by default). Accepts `1`, `true`, `yes`, `on`. |
| `LLM_BASE_URL` | none | The OpenAI-compatible base URL (the gateway's `<gateway>/v1`). `https://` only (`http://` for `localhost`, `127.0.0.1`, `::1`), no credentials, no query. Empty: no environment AI account. |
| `LLM_API_KEY`, `LLM_API_KEY_FILE` (default `/etc/paybroker/llm_api_key`) | none | The gateway client key. |
| `LLM_CHAT_MODEL` | none | The chat model or gateway alias. Without it the environment account cannot chat. |
| `PAYBROKER_ALLOW_PRIVATE_URLS` | off | `1`/`true`/`yes`/`on` lets webhook URLs and user-added AI provider URLs point at private or loopback addresses (local testing only). Link-local addresses are always refused. |
| `PAYBROKER_SIM_LATENCY` | on | `off`, `0` or `false` makes the simulated connectors answer instantly (tests and benchmarks). |
| `PAYBROKER_CAPTURE_TRAINING_DATA` | off | `1` captures training rows when no stored setting exists (a stored setting wins). |
| `PAYBROKER_TRAINING_RETENTION_DAYS` | `180` | Default retention of training rows (1 to 3650) until set in Settings > Model. |
| `PAYBROKER_TRAINING_MAX_ROWS` | `5000000` | Default row cap (1,000 to 50,000,000) until set in Settings > Model. |
| `PAYBROKER_TRIAGE_RPM` | `20` | AI triage calls per minute per worker process (0 to 600; 0 = unlimited). |
| `PAYBROKER_TRIAGE_DAILY_CAP` | `120` | Calls that reached the model in the last 24 hours (0 to 1,000,000; 0 = unlimited). |
| `PAYBROKER_TRIAGE_GROUP_MINUTES` | `30` | Window in which a case from the same device (else IP) is not sent to the model again (0 to 1,440; 0 = off). |
| `PAYBROKER_TRIAGE_PAUSE_MINUTES` | `10` | Circuit-breaker pause after 3 consecutive gateway 429s (1 to 1,440). |
| `SSL_CERT_FILE` | set in the image | Points psycopg's bundled OpenSSL at the system CA bundle so `sslrootcert=system` works (set by the Dockerfile). |

The triage and training variables only supply the default: a value saved in Settings (stored in `app_setting`) wins. An out-of-range
environment value is ignored and the built-in default is used. Settings that are not environment variables (merchant routing and
risk settings, rules, connectors, AI account choice, active model) live in the database.

Variables `PAYBROKER_TOKEN` and `PAYBROKER_URL` that appear in the Connect tab's snippets are examples for the reader's shell, not server
configuration.

### 5.2 Merchant settings (per merchant, in the dashboard or `PATCH /api/merchants/{id}`)

| Setting | Default | Meaning |
|---|---|---|
| `preferred_psps` | `[]` | Connector names that get a fixed +10 bonus in the routing score |
| `max_fee_bps` | none | Connectors with a higher percentage fee are not eligible (0 to 1000) |
| `weights` | auth 100, cost 30, latency 5 | The `W_auth`, `W_cost`, `W_latency` of the routing score (each 0 to 1000) |
| `require_3ds_over_cents` | none | Amounts at or above this ask for 3-D Secure (reason `MERCHANT_3DS_THRESHOLD`) |
| `allowed_currencies` | all 11 | `USD EUR GBP BRL CAD AUD MXN INR JPY CHF SEK` |
| `region` | none | Two-letter data-residency region: only connectors serving it are eligible |
| `capture_method` | `manual` | `manual` (authorize, then capture) or `automatic`; a request's `capture_method` wins |
| `allowed_origins` | `[]` | Browser origins for publishable-key tokenization and 3-D Secure return URLs (max 20) |

### 5.3 Built-in connectors (simulated)

Seeded on first start: `stripe` (USD EUR GBP CAD AUD MXN JPY; US, EU; 2.10 %, 55 ms), `adyen` (USD EUR GBP CAD AUD SEK CHF INR JPY; US, EU,
IN; 1.80 %, 70 ms), `chase` (USD; US; 1.90 %, 85 ms), `localbr` (BRL; BR; 2.60 %, 80 ms). They can be enabled or disabled under Routing.
BRL is only routed to connectors serving region `BR`.

## 6. First-run walkthrough

1. **Start** PayBroker (section 4). In standalone mode the log contains a block like
   `=== PayBroker first-run setup ===` followed by a code of the form `XXXX-XXXX-XXXX`. It changes on every restart until setup completes.
   In Docker: `docker compose logs api`; on AgentPG: `agentpg sandbox logs <sandbox-id>`.
2. **Open the app** in a browser. You see *Set up PayBroker*: enter the setup code and choose an admin password (12 characters minimum), twice,
   then *Create admin password*. This also creates the vault, so webhook secrets and AI keys can be stored encrypted. You are signed in
   (a session lasts 12 hours).
   With SSO instead (`PAYBROKER_OIDC_ISSUER` set) there is no setup code: you sign in with identity and need a `paybroker` role.
3. **Fill the screens**: Settings > Demo traffic (Payments 120, Seed 7 by default) > *Send*, or the *Generate 120 demo payments* button on an empty Overview (or `paybroker demo -n 150` in a terminal; the traffic is seeded, so the same seed sends the same customers, cards and amounts, though connector outcomes can vary). Overview now shows approvals,
   3-D Secure, fraud rejections and a few open cases.
4. **Look at a decision**: Payments, click a rejected payment: score, reasons, Tier 1 rules that fired, timing, routing attempts, AI triage.
5. **Take a real API payment**: Merchants > *New merchant* > create a secret and a publishable key (each is shown once), then run the curl commands
   from the Connect tab with a test card such as `4242 4242 4242 4242`.
6. **Resolve a case** under Cases (needs a role that can review cases, e.g. `analyst`; standalone admin can).

The standalone admin has every permission. A forgotten admin password is recovered with `paybroker reset-admin` (section 10.4).

## 7. Using PayBroker

### 7.1 The dashboard

Tabs appear only when the signed-in principal has the needed permission.

| Tab | Needs | What it does |
|---|---|---|
| **Overview** | `payments:read` | Period selector (24 hours, 7, 30, 90 days); payments, approval rate, volume, latency (average, p95), score histogram, payments per day, connector split, open cases, pending rule suggestions, queue backlog |
| **Payments** | `payments:read` | Search by payment id, customer id or idempotency key; filter by status, decision, minimum score (300, 500, 700). A row opens the full explanation: payment facts, score gauge, decision and action, reasons, Tier 1 rules and filters, timings per step, the features the model saw, routing attempts, AI triage, the case, the 3-D Secure state, chargebacks, webhook events. With `cases:review` you can *Record a chargeback* on an authorized or captured payment. |
| **Cases** | `payments:read` to read; `cases:review` to decide | Flagged payments (score 700 or more) with the AI summary and recommendation; resolve with *Block permanently* (`PERMANENT_BLOCK`, which also blocks the device, IP and card), *Allow with KYC* or *Dismiss alert*, plus a note |
| **Rules** | `payments:read`; `rules:manage` to change | Tier 1 rules (turn on or off, delete, add with a JSON definition), pending *Suggested rules* (Approve or Reject), and the *Blocklist* (`cases:review` to unblock) |
| **Merchants** | `payments:read`; `merchants:manage` to change | New merchant, then per merchant: API keys (create, revoke), browser origins, routing and risk settings, webhook (save and rotate the secret), recent webhook events, suspend or reactivate |
| **Routing** | `payments:read`; `merchants:manage` to change | Connectors with live (smoothed) authorization rate and latency, fees, attempts; enable or disable a connector |
| **Settings** | `settings:manage` | AI accounts and the account used for triage, AI triage limits, Model (models, training data), Security (admin password, operator tokens), Demo traffic, Maintenance (also needs `data:reset`; model and training blocks also need `model:manage` or `training:export`) |
| **Insights** | `settings:manage` | Database insights (section 17.1): the slowest statements, the tuner's recommendations and the index advisor's suggestions, each with a "Not available on this database" state where an extension is missing |
| **Connect** | `mcp:use` | Copy-paste curl and MCP setup for this server's address |

### 7.2 Merchants, keys, origins and webhooks

A merchant is a business that sends payments. Under Merchants: *New merchant* (a name, 1 to 100 characters; unique), then in its window:

- **Keys.** A *secret* key (`sk_test_` + 43 random characters) for the server and a *publishable* key (`pk_test_...`) for the browser. A key is shown
  once; only its SHA-256 and a 12-character prefix are stored. Revoking takes effect immediately in the process that made the change and within
  30 seconds elsewhere. A key's `last_used_at` is approximate (the lookup is cached for 30 seconds).
- **Browser origins.** One per line, exactly `https://host[:port]` (or `http://localhost[:port]` and `http://127.0.0.1[:port]`). No wildcards,
  paths, credentials or IP addresses (other than 127.0.0.1); up to 20; normalized (lower case, default port dropped). Saving replaces the list.
- **Webhook.** A URL (https, or private/loopback only when `PAYBROKER_ALLOW_PRIVATE_URLS` is set). Saving **generates a new signing secret**
  `whsec_...`, shown once. It needs the vault to be initialized (an admin password set). Clearing the URL removes the secret.
- **Routing and risk.** The settings in section 5.2.

### 7.3 Taking a payment (the `/v1` API)

All bodies are JSON, unknown fields are rejected (422), bodies over 64 KB are refused (413), amounts are integers in minor units, and the
credential is `Authorization: Bearer <key>`. API.md lists every status and error code. The flow:

```bash
# 1. exchange a TEST card for a token (publishable key; from the browser, or a server for a quick test)
curl -s -X POST "$PAYBROKER_URL/v1/vault/tokens" -H "Authorization: Bearer $PB_PUBLISHABLE_KEY" -H "Content-Type: application/json" \
  -d '{"number":"4242424242424242","exp_month":12,"exp_year":2035,"cvc":"123"}'

# 2. authorize with the secret key (safe to repeat with the same idempotency_key)
curl -s -X POST "$PAYBROKER_URL/v1/payments/authorize" -H "Authorization: Bearer $PB_SECRET_KEY" -H "Content-Type: application/json" -d '{
  "idempotency_key": "order-1001", "amount": 4999, "currency": "USD",
  "payment_method": {"type": "card", "card_token": "tok_vault_card_..."},
  "customer": {"id": "cus_42", "email": "pat@example.com", "ip_address": "198.51.100.20", "account_age_hours": 4000},
  "telemetry": {"device_fingerprint": "dev_abc", "checkout_duration_ms": 41000, "pasted_card_number": false},
  "shipping_address": {"country": "US"}}'

# 3. capture, void or refund
curl -s -X POST "$PAYBROKER_URL/v1/payments/pay_.../capture" -H "Authorization: Bearer $PB_SECRET_KEY"
```

| Call | Notes |
|---|---|
| `POST /v1/vault/tokens` | 201 `{token, brand, bin, last4, exp_month, exp_year, fingerprint}`. Test cards only (422 `INVALID_CARD` otherwise). Number and CVC are checked and discarded. |
| `POST /v1/payments/authorize` | `idempotency_key` 8 to 128 characters; `amount` 1 to 99,999,999; `currency` three letters; optional `capture_method`, `telemetry`, `shipping_address`, `return_url`, `merchant_id` (must equal the key's merchant). |
| `GET /v1/payments/{id}`, `GET /v1/payments?limit=&status=` | Your own payments only |
| `POST .../capture {"amount"}`, `.../void`, `.../refund {"amount"}` | `capture` and `void` need `AUTHORIZED`; `refund` needs `CAPTURED` or `PARTIALLY_REFUNDED`; wrong state is 409 `INVALID_STATE`, a bad amount 422 `INVALID_AMOUNT` |

Answers to an authorization: `200` with `AUTHORIZED` or `CAPTURED`; `200` with `REQUIRES_ACTION` and `action_required.redirect_url`; `402 CARD_DECLINED`
with `decline_code` (the issuer's decision, final); `403 FRAUD_REJECTED` with the `fraud_assessment` (the connector was never called);
`422` for `IDEMPOTENCY_KEY_REUSED`, `CURRENCY_NOT_ALLOWED`, `INVALID_CARD_TOKEN`, `NO_ROUTE`, `RETURN_URL_NOT_ALLOWED`, `RETURN_URL_NEEDS_WEBHOOK`;
`429` after too many failed credentials; `502 PSP_UNAVAILABLE` when every eligible connector failed in budget; `503` when the database is unavailable.

`customer.account_age_hours` is optional but matters: without it the broker assumes an established account (720 hours, 30 days), because
"never seen" is not the same as "new account".

### 7.4 Test cards

| Number | Behaviour |
|---|---|
| `4242 4242 4242 4242`, `5555 5555 5555 4444`, `3782 822463 10005`, `6011 1111 1111 1117` | approve |
| `4000 0000 0000 0002` | issuer declines (`do_not_honor`) |
| `4000 0000 0000 9995` | `insufficient_funds` |
| `4100 0000 0000 0019` | issuer declines as `stolen_card` |
| `4000 0025 0000 3155` | the issuer always demands 3-D Secure |
| `4000 0000 0000 0119` | the first connector answers 5xx; the cascade recovers on the next |
| 16-digit Luhn-valid starting `99999901` / `99999902` / `99999903` | decline / always 3-D Secure / first connector 5xx, per customer |
| any other Luhn-valid `999999...` | random behaviour from each connector's configured rates |

Anything else is `422`. Expiry must be in the future and at most 20 years ahead (a two-digit year gets 2000 added).

### 7.5 Browser tokenization (CORS)

The card number goes from the customer's browser straight to PayBroker, so the shop's server only ever sees a token. Only
`POST /v1/vault/tokens` ever gets CORS headers.

- A **preflight** (`OPTIONS`) is answered `204` when any active merchant lists the origin and the requested method is `POST`; otherwise `403 origin_not_allowed`.
- The **call** must use a publishable key whose own merchant lists the browser's `Origin`; a secret key from a browser is refused on purpose. The response then
  carries `Access-Control-Allow-Origin: <that origin>` and `Vary: Origin` (never `*`, never `Allow-Credentials`).
- A call without an `Origin` header (a server, curl) is unaffected.
- No per-origin request rate limit exists; put one at the proxy.

### 7.6 3-D Secure and the signed return

A payment scoring 300 to 699, matching a `challenge` rule, over the merchant's threshold, or using the always-3-D-Secure test card returns
`REQUIRES_ACTION` with `redirect_url = <public url>/3ds/<challenge id>`. The hosted page is a **simulated issuer**: the tester presses *Approve (verification succeeds)* or
*Fail verification*. A challenge lasts 30 minutes (after that, `410 CHALLENGE_EXPIRED` and the payment is declined with `3ds_expired`). On success the payment is routed to a
connector and becomes `AUTHORIZED` or `CAPTURED`; on failure it is `DECLINED` (`3ds_failed`). The `/3ds` and `/api/3ds/*` routes need no credential: anyone holding the
unguessable challenge id can complete it (test mode only). A non-browser caller of `POST /api/3ds/{id}/complete` must still send the `X-PayBroker-CSRF` header (any value), because the guard asks for it on every write under `/api/` that has no `Authorization` header.

If the authorize call carried `return_url` (on one of the merchant's allowed origins; the merchant must have a webhook secret), the page redirects the customer there with
`payment_id`, `status`, `timestamp` and `signature = hex(HMAC-SHA256(webhook secret, timestamp + "." + "payment_id=<id>&status=<STATUS>"))`. Verify it in constant time, reject
timestamps older than five minutes, and treat the webhook (or `GET /v1/payments/{id}`) as the source of truth. If the vault is locked, the page does not redirect at all. The
README has a Node verification function.

### 7.7 Webhooks

Event types: `payment.authorized`, `payment.captured`, `payment.requires_action`, `payment.declined`, `payment.failed`, `payment.voided`, `payment.refunded`. A delivery:

```
POST <merchant url>
Content-Type: application/json
X-Timestamp: <unix seconds>
X-Signature: hex(HMAC-SHA256(secret, timestamp + "." + raw body))
```

The body is `{"id": "evt_...", "type": "...", "created_at": "...", "data": <the payment>}`, serialized with sorted keys and no spaces: verify over the **raw bytes**. Any 2xx is
delivered. Otherwise the delivery is retried after 30 s, 5 min, 30 min, 2 h, 6 h and 15 h (seven attempts, about 23.6 hours in all, then `failed`), and every attempt is visible under Merchants (recent events) and in the payment's detail. Delivery is at least
once: deduplicate on `id`. While the vault is locked, deliveries wait (they are not failed). A merchant with no webhook gets no events.

### 7.8 Routing

Hard constraints first (connector enabled, currency, the merchant's region, `BRL` only on `BR` connectors, the merchant's maximum fee), then for each eligible connector

`S = W_auth x auth rate - W_cost x fee % - W_latency x latency (s) + 10 (if preferred)`

using the live, exponentially smoothed auth rate and latency (smoothing factor 0.05, flushed to `psp_connector` every 5 seconds by the worker). The best three are tried in
order. On a 5xx or timeout (a simulated timeout costs 300 ms) the next is tried while the 800 ms budget lasts; an issuer decline is final and not retried elsewhere. With no
eligible connector the answer is `422 NO_ROUTE`.

### 7.9 The fraud engine

Decision = the stricter of the Tier 1 verdict and the score band: **under 300 approve, 300 to 699 3-D Secure, 700 and above decline and open a case**.

- **Tier 1, rules** (cached 5 seconds): block-list hits (`ip`, `device`, `card_fingerprint`, `bin`, `email_domain`), hard velocity (5 or more attempts in a minute from one IP, card or
  user), the merchant 3-D Secure threshold, the issuer-requires-3-D-Secure card, and JSON rules. A rule is *data*: `{"all": [...]}`, `{"any": [...]}` or
  `{"field", "op", "value"}`, at most 3 levels and 12 conditions, operators `== != > >= < <= in not_in` (`in`/`not_in` on text fields only), over a fixed list of fields that
  `GET /api/rules` returns. Four rules are seeded: `new_account_high_amount` (challenge), `foreign_ip_large_basket` (challenge), `many_cards_one_account` (decline), `name_mismatch_large_basket` (challenge, see below).
- **Tier 1, `NAME_MISMATCH` (fuzzystrmatch)**: when a request carries both `payment_method.cardholder_name` and `customer.name`, Postgres compares them token by token with `levenshtein` (distance 1 for tokens of up to 5 letters, 2 for longer ones), `dmetaphone`, `soundex` and initials. Fewer than half of the tokens of the shorter name matching raises the signal. `Jon Smith` against `John Smyth`, `Smith, John` or `J. Smith` is similar (no signal); `Jon Smith` against `Peter Wong` is a mismatch. The weight is modest: on its own it adds the reason code `NAME_MISMATCH` (shown with its explanation on the payment page, listed in the Tier 1 triggers, and recorded as `name_mismatch` in the feature snapshot) and does not change the verdict. It is also a rule field (`name_mismatch`, boolean), and the seeded rule `name_mismatch_large_basket` asks for 3-D Secure when it is true and the amount is 30,000 minor units or more. Nothing is raised when either name is missing, when no letters can be compared, or when `fuzzystrmatch` is not installed (migration `006` creates it and tolerates failure; the signal checks `pg_extension`). The names are compared and dropped: they are not stored anywhere, the feature vector is unchanged (`vector(17)`). Both fields are optional additions to `POST /v1/payments/authorize` (see [API.md](../API.md)).
- **Tier 2, model**: 70 depth-3 trees over 17 features, score 0 to 1000, with reason codes. Details, training and the scenario harness: README, *Model, training and data*.
- **Tier 3, AI triage**: for a flagged payment a case opens and a `fraud.triage` job is queued. The worker builds a payload of derived features, the model output and the five
  nearest past flagged cases (pgvector cosine distance on the payment's 17-number vector, among assessments scoring 500 or more), asks the language model for a strict
  JSON report (category, confidence 0 to 1, summary, risk factors, recommended action) and validates it. Without an AI account, or when a limit applies, a built-in rule-based
  triage writes the same shape (`source: heuristic`, with a `note`). The report is shown to the analyst; it never changes anything.
- **Rule suggestions**: once a day (if flagged traffic exists) the worker queues a job; an admin or risk manager can also trigger one with `POST /api/suggestions/run`. With
  an AI account the model proposes up to 3 rules; otherwise thresholds are derived from confirmed fraud (analyst blocks and chargebacks, at least 5) against clean approved
  payments (at least 20). Every proposal passes the same validator as a hand-written rule, waits as `pending`, and is validated again on approval. Nothing activates by itself.
- **Analyst actions**: resolving `PERMANENT_BLOCK` blocks the payment's device, IP and card fingerprint. `ALLOW_WITH_KYC` and `DISMISS_ALERT` do not block. A chargeback
  (`POST /api/payments/{id}/chargeback`) feeds the feature store so a device linked to a chargeback scores higher next time.

### 7.10 AI triage and its limits

The gateway enforces 60 requests a minute and a hard monthly token budget per app, so PayBroker never sends every flagged payment to the model. Four limits (Settings > AI triage limits,
`PUT /api/admin/triage/settings`, or the `PAYBROKER_TRIAGE_*` variables as defaults) decide, from cheapest to most specific:

1. **Breaker open** (after 3 consecutive gateway 429s): new cases wait; a case queued over an hour gets the heuristic instead.
2. **Daily cap** reached (default 120 model calls in 24 hours): heuristic at once, with a `note`.
3. **Grouping**: a case whose device (else IP) was already read by the model within the window (default 30 minutes) gets the heuristic, citing the other payment.
4. **Per-minute bucket** (default 20, burst of up to 5, per worker process): no free slot means the job waits 5 to 35 seconds and is retried, at no cost in attempts.

A gateway 429 uses no attempt: the job waits for the `Retry-After` in the error text if present, else 60 seconds doubling to 15 minutes. Other errors get 3 attempts
(after 30 s, 5 min, 30 min), then the heuristic and state `failed`. Each model call is logged (payment row, time, outcome; no identifiers) in `triage_llm_call` for seven days. A data
reset keeps that table so a reset cannot hand back the day's budget. The bucket is per process; the cap, grouping and breaker are shared through the database.

**AI accounts.** With `LLM_BASE_URL` set, Settings > AI accounts shows *Environment (server settings)*, read-only, used by default. You can add accounts (presets: OpenAI, OpenRouter,
Anthropic's OpenAI-compatible endpoint, custom); their keys are stored encrypted, their URLs are checked before every call, and each has a *Test chat* button (`max_tokens=64`) that shows the
provider's failure reason, including a thinking model that spent its budget before answering. The account used for triage is the one chosen, else the environment account if it can chat,
else the only stored account with a chat model.

### 7.11 The operator API

`/api` takes a session cookie, an operator token, an identity JWT or the static key. Any write (not GET, HEAD or OPTIONS) under `/api/` without an `Authorization` header must carry the custom header `X-PayBroker-CSRF` (the dashboard sends it); bearer-token calls do not need it.
API.md has the table of every route and the permission it needs. Highlights: `GET /api/overview`, `GET /api/payments`, `GET /api/payments/{id}`, `POST /api/payments/{id}/chargeback`, cases,
rules, suggestions, blocklist, merchants and keys, connectors, webhook events, accounts, tokens, `POST /api/demo/traffic {count 1..500, seed}`, triage settings, training and model routes,
and the data reset. Operator tokens are created under Settings > Security (scope `read`, `metrics` or `full`, shown once, stored hashed, revocable).

### 7.12 The MCP server

`/mcp` is streamable HTTP, bearer credentials only (no cookies, no merchant keys), permission `mcp:use` (token scope `read` or `full`; `paybroker.read` for services; roles admin,
risk manager, analyst, operator, viewer). The tools only read:

| Tool | Returns |
|---|---|
| `get_overview(days=7)` | Payments, approval rate, fraud decisions, latency, connector split, open cases, queue backlog (1 to 90 days) |
| `search_payments(status, merchant_id, min_score, decision, query, limit=25)` | Matching payments (`query` matches payment id, customer id or idempotency key) |
| `explain_payment(payment_id)` | The request, rules fired, score with reasons and feature snapshot, AI triage, routing outcome, case, 3-D Secure state, webhooks, chargebacks |
| `list_fraud_cases(status="open", limit=50)` | Flagged payments with the AI summary and recommendation |
| `list_fraud_rules()` | Active and disabled Tier 1 rules |
| `list_rule_suggestions(status="pending")` | Rules proposed by the AI or the heuristic |

Which of these AgentPG's restricted MCP could replace (over the masked views of section 17.2, as a read-only user) is in section 17.3; none is removed yet.

Connect from Claude Code (the Connect tab prints this with your address):

```bash
claude mcp add --transport http paybroker https://<paybroker>/mcp --header "Authorization: Bearer $PAYBROKER_TOKEN"
```

Create a `read` token for assistants. An assistant can investigate and explain; resolving a case or approving a rule needs a person in the dashboard or the REST API.

### 7.13 Training data, models and retraining

Covered in the README (*Model, training and data*). In short: capture is off by default and writes one row of 17 derived numbers per scored payment (never a card, e-mail, IP, device id or name);
labels come only from real outcomes (fraud-coded chargebacks, analyst decisions, optional explicit aging); retention (default 180 days) and a row cap apply even while capture is off; the export is
admin-only; `make train` retrains the bundled model from synthetic data and `scripts/train_model.py` can mix an export or a public dataset you download yourself; uploaded models are validated, never
activate by themselves, and roll back by activating an earlier version. The model file and `training_report.json` under `src/paybroker/models/` are what ships.

## 8. Roles and permissions

Permissions are what the code checks; roles (people, from identity) and token scopes (services, local tokens) map onto them in `src/paybroker/core.py`.

| Permission | Meaning |
|---|---|
| `payments:read` | See payments, scores, cases, rules, merchants (without secrets), connectors, webhook events, analytics |
| `cases:review` | Resolve cases, record chargebacks, add and remove block-list entries |
| `rules:manage` | Create, change, delete rules; run and decide suggestions |
| `merchants:manage` | Create and edit merchants, keys, origins, webhooks; enable or disable connectors |
| `settings:manage` | AI accounts and the triage account, triage limits, operator tokens, admin password, vault unlock, demo traffic |
| `training:export` | Training statistics and export (admin only) |
| `model:manage` | Capture and retention settings, label aging, purge, upload, activate and delete models (admin only) |
| `data:reset` | Wipe the transaction data (admin only) |
| `mcp:use` | Use `/mcp` |
| `metrics:scrape` | Read `/metrics` |
| `payments:write`, `vault:tokenize` | Merchant keys only: secret key has both, publishable key only `vault:tokenize` |

| Role | Permissions |
|---|---|
| `admin` | everything |
| `risk_manager` | `payments:read`, `cases:review`, `rules:manage`, `mcp:use` |
| `analyst` | `payments:read`, `cases:review`, `mcp:use` |
| `operator` | `payments:read`, `merchants:manage`, `mcp:use` |
| `viewer` | `payments:read`, `mcp:use` |

| Token scope (local token or service client) | Permissions |
|---|---|
| `read`, `paybroker.read` | `payments:read`, `mcp:use` |
| `metrics`, `paybroker.metrics` | `metrics:scrape` |
| `full` | everything |
| `paybroker.admin` | `merchants:manage`, `payments:read`; honoured only on a **service** token (a person's token that carries it gets only what their roles give) |

Notes:

- The standalone admin (setup code and password) and the static key have every permission. Only `admin` and `full` can reach Settings, `/metrics`, training, models and the reset; the role
  `admin` therefore also covers `/metrics`.
- A person who signs in with SSO but holds no `paybroker` role is turned away with "your account has no role in PayBroker; ask an administrator".
- Role changes in identity apply at the person's next sign-in (their session keeps the roles it was created with).
- Merchant keys can reach only `/v1`; operator credentials cannot make payments.

## 9. Security notes

SECURITY.md is the authoritative model; the points an operator should know:

- **Test cards only.** The vault refuses any number that is not a public processor test number or in the reserved `999999` range. It stores brand, BIN, last four, expiry and a keyed HMAC of the
  number; never the number or the CVC (a test asserts it). PayBroker is not PCI-certified.
- **Keys and secrets.** Merchant keys and operator tokens are random, shown once, stored hashed. Webhook secrets and AI keys are encrypted with a data key wrapped under the admin password (and
  optionally `PAYBROKER_ENCRYPTION_KEY`); a database dump alone reveals none of them. The environment LLM key lives only in the process environment or its file.
- **Outbound requests.** Webhook and user-added AI URLs are validated before every call (https, no credentials, no loopback, private, link-local or carrier-grade-NAT addresses, no redirects,
  bounded responses). The environment LLM URL is operator-set and trusted.
- **AI cannot act.** Strict output schema, rule-based fallback, suggestions need a person, MCP is read-only by construction (a test asserts no tool name suggests a write).
- **Browser surface.** Strict CSP (no inline script or style), `nosniff`, no framing, no referrer, no caching; all text reaches the page through `textContent`; bodies are capped at 64 KB (2 MB for a model upload);
  unknown fields are rejected; writes under `/api/` without an `Authorization` header need the `X-PayBroker-CSRF` header.
- **Rate limits** on failed credentials: 10 per minute per address, in memory, per process. Put proxy-level limits in front, including for tokenization from browsers.
- **Verify what PayBroker sends you.** Check webhook and 3-D Secure return signatures over the raw bytes, in constant time, with a five-minute freshness window; deduplicate on event id.
- **Exports are sensitive.** The training export is derived behavioural data joinable to payments by id: treat the file like the database.
- **Reporting.** Use GitHub private vulnerability reporting, not a public issue.

## 10. Operations

### 10.1 Health, metrics, logs

| Endpoint | Answer |
|---|---|
| `GET /healthz` | `200 {"status": "ok", "version": "..."}`; no database access. The image's `HEALTHCHECK` uses it. |
| `GET /readyz` | `200 {"status": "ready"}` after a `SELECT 1`; `503 {"status": "waking-database"}` otherwise (details are never echoed) |
| `GET /metrics` | Prometheus; needs `metrics:scrape` (a `metrics` token or `paybroker.metrics` service token) |

PayBroker families: `payment_broker_requests_total{status,psp}`, `payment_broker_fraud_score_distribution`, `payment_broker_latency_seconds{step}` (feature_fetch, tier1_rules, ml_inference, psp_roundtrip, total),
`payment_broker_triage_total{source,result}`, `payment_broker_triage_calls_total{outcome}` (`ok`, `error`, `gateway_429`, `throttled`, `capped`, `grouped`, `paused`, `paused_fallback`),
`payment_broker_webhooks_total{result}`, `payment_broker_psp_fallbacks_total`, and the gauges `payment_broker_outbox_backlog`, `payment_broker_open_fraud_cases`, `payment_broker_payments_awaiting_3ds`,
`payment_broker_triage_queue_depth`, `payment_broker_triage_llm_calls_24h`, `payment_broker_triage_circuit_open`. The shared families (`http_requests_total`, `http_request_duration_seconds`, `db_up`,
`app_info`, `process_*`) come from agentpg-kit. The gauges are evaluated at scrape time; they report `db_up 1` when the probe query works.

Insights gauges (from the kit, cached 30 seconds, never failing a scrape): `db_insights_available`, `db_insights_top_query_mean_seconds{queryid}` (the 5 slowest statements with at least 5 calls), `db_insights_statement_calls`, `db_insights_tuner_recommendations`. Without `edb_stat_monitor` the kit falls back to `pg_stat_statements`; with neither, `db_insights_available 0`.

Logs go to stdout/stderr (the setup code, database retry messages, startup failures). On AgentPG: `agentpg sandbox logs <sandbox-id>`.

### 10.2 Backup and restore

All state is in Postgres: back it up with `pg_dump` (for an AgentPG instance, `ecoctl db paybroker` gives the connection details; or the platform's snapshot tools) and restore into a database with the
`vector` extension. The dump contains encrypted webhook secrets and AI keys: to read them after a restore you need the admin password, or `PAYBROKER_ENCRYPTION_KEY` set to the same value as before.
Keep ecoctl's `.ecosystem/state.json` too (it holds the generated encryption key and the database key). Nothing outside the database needs backing up (the bundled model ships in the image).

### 10.3 Vault and secret rotation

| What | How |
|---|---|
| Vault unlock after restart | Sign in once (the password unwraps the key), or set `PAYBROKER_ENCRYPTION_KEY` (with SSO only, the vault is created from it at startup). `GET /api/session` reports `vault: {initialized, unlocked, auto_unlock}`; the dashboard shows a banner while locked; `POST /api/vault/unlock {password}` unlocks without a full re-login. While locked, webhooks wait and the environment AI account keeps working. |
| Admin password | Settings > Security (or `POST /api/admin-password`): needs the current password; signs out every session. |
| Merchant key | Create a new key, switch the shop over, revoke the old one (effective within 30 s everywhere). |
| Webhook signing secret | Saving the webhook URL again generates a new secret; update the receiver at the same time. `ecoctl connect --rotate` does both ends for ShopFront. |
| Operator token | Create a new one under Security, revoke the old. |
| `LLM_API_KEY` | Change the sandbox environment (ecoctl: `ecoctl llm sync`, `apply --rotate`; then `ecoctl recreate paybroker`). |
| Fraud model | Upload a new version, activate it (this instance at once, others within about 30 s); roll back by activating an earlier one or *Use bundled*. |

Changing `PAYBROKER_ENCRYPTION_KEY` after the vault exists: set the new value and restart. The old wrap no longer opens, so the vault stays locked (`GET /api/session` shows `unlocked: false`, `reason: platform_key_mismatch`; webhooks wait and stored AI keys are unusable, the environment AI account is unaffected). Sign in once with the admin password: the kit re-wraps the data key under the new value, nothing stored is lost, and the next restart unlocks by itself. Putting the old value back before that first sign-in also works. Covered by a test in `tests/test_db.py`; there is no command and no data migration.

### 10.4 Recovery commands

| Situation | Command |
|---|---|
| Forgot the admin password | `paybroker reset-admin` (in a console on the host or `agentpg sandbox console <id>`). Clears the admin password, the vault and stored secrets: AI account keys are wiped and every merchant's webhook secret is cleared, so re-save each webhook URL to get a new secret. The setup-code flow is active again. |
| Simulator or test run filled the system with payments | Settings > Maintenance, or `paybroker reset-data --yes --confirm "RESET PAYBROKER DATA"` (keeps configuration; see 10.6) |
| Apply migrations without starting | `paybroker migrate` |

### 10.5 Scaling the worker

Run more consumers with extra processes of the same image and `PAYBROKER_ROLE=worker`. The queue is the `outbox` table, claimed with `FOR UPDATE SKIP LOCKED` and leased for 120 seconds, so a crashed
worker's job comes back. The AI-triage per-minute bucket is per process, so N workers allow N times the rate; the daily cap, grouping and breaker are shared.

### 10.6 Resetting the transaction data

Settings > Maintenance (admin), `POST /api/admin/data/reset` or `paybroker reset-data` delete the transaction data (payments, 3-D Secure challenges, assessments, cases, chargebacks, webhook deliveries, the
queue, the feature store, optionally training rows and rule suggestions) and keep the configuration (merchants, keys, webhooks, card tokens, connectors, rules, block list, settings, AI accounts, models, users,
sessions, tokens, audit). It needs the exact phrase `RESET PAYBROKER DATA`, deletes in chunks of at most 5,000 rows, is safe to interrupt and repeat, holds an advisory lock (a second reset gets 409), and writes
one `admin_audit` row. Pause senders first: payments sent during a reset may survive it. `GET /api/admin/data/reset` previews exact row counts.

### 10.7 Upgrades

A new image runs `paybroker start`, which applies pending migrations (numbered SQL files, recorded in `schema_migrations`) before serving. On AgentPG, `ecoctl recreate paybroker` pulls the latest image; the
database and generated secrets are kept, and the callers (the simulator, ShopFront) are recreated and reconnected because the URL changes. A sandbox can reuse a stale image after pause and resume; recreate is the
reliable way to pull `latest`.

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Empty `503` on the app URL (AgentPG) | The sandbox auto-paused | `agentpg sandbox resume <sandbox-id>`; then sign in or rely on `PAYBROKER_ENCRYPTION_KEY` to unlock the vault |
| `503` on `/v1`, `/readyz` says `waking-database` | The database is scaled to zero or unreachable | Retry; the connection retries with back-off. Check `DATABASE_URL` |
| Container exits with "startup failed while migrating" and mentions `vector` | The extension is not enabled | `agentpg extensions create <instance-id> vector`, or `CREATE EXTENSION vector` as a privileged user |
| No setup code in the log | SSO is enabled (no setup code then), or setup is already complete | Sign in through identity, or use the admin password; `paybroker reset-admin` to start over |
| `422 INVALID_CARD` | Not a test card, bad Luhn, expired | Use a number from section 7.4 |
| `403 origin_not_allowed` on tokenize | Origin not on the merchant's list, a secret key used from a browser, or the merchant is suspended | Add the exact origin under Merchants; use the publishable key |
| `422 RETURN_URL_NEEDS_WEBHOOK` or `RETURN_URL_NOT_ALLOWED` | No webhook secret yet, or the return URL's origin is not allowed, or it carries `payment_id`, `status`, `timestamp` or `signature` | Save a webhook, list the origin, remove the reserved parameters |
| Webhook events stay `pending` | The vault is locked, or the receiver returns non-2xx | Unlock (sign in, or set `PAYBROKER_ENCRYPTION_KEY`); check the receiver; see attempts under Merchants |
| Webhook event `failed` with "URL refused" | The URL is private or loopback | Use a public https URL; for local testing set `PAYBROKER_ALLOW_PRIVATE_URLS=true` |
| Webhook event `failed`: "stored webhook secret cannot be read" | The secret was encrypted under a vault that was reset | Save the webhook URL again to rotate the secret |
| Webhook signature mismatch at the receiver | Parsed and re-serialized body, wrong secret after a rotation, or clock skew over 5 minutes | Verify over the raw bytes with the current secret |
| A whole simulation is rejected by the fraud engine | Many payments share one card number, so per-card velocity trips | Give each customer its own card with the `999999xx` prefixes (section 7.4) |
| Everyday customers asked for 3-D Secure | `account_age_hours` missing or very low, merchant threshold, a rule | Check the payment's reasons; pass `account_age_hours`; review rules |
| `422` "unknown field" | Bodies reject unknown fields | Remove the field; see API.md |
| `413` | Body over 64 KB | Send less (a model upload may be up to 2 MB) |
| `409 INVALID_STATE` | Capture or void on a non-`AUTHORIZED` payment, refund on a non-captured one | Read the payment's status |
| `422 IDEMPOTENCY_KEY_REUSED` | Same key, different body | Use a new key, or resend the identical body |
| `410 CHALLENGE_EXPIRED` | 3-D Secure not completed within 30 minutes | The payment was declined; start again |
| `429` on login or API | 10 failed credentials a minute from one address | Wait a minute |
| `401` on `/mcp`, `403 this credential cannot use MCP` | No token, a merchant key, a cookie, or a token without `mcp:use` | Use a `read` or `full` operator token |
| Case shows "AI triage daily cap reached" or "Grouped with ..." | The throttle did what it is for | Raise the limit under Settings > AI triage limits if your gateway budget allows |
| Triage is paused (`payment_broker_triage_circuit_open 1`) | Three gateway 429s in a row | Fix the gateway budget or rate limit; it resumes after `pause_minutes` |
| *Test chat* fails | Wrong URL, key, model, or a thinking model used its whole budget | Read the reason shown verbatim; pick another model or raise its limit |
| "your account has no role in PayBroker" | The identity user has no `paybroker` role | Add the role or a group grant in identity; sign in again |

## 12. Testing and development

### 12.1 Layout

`src/paybroker/` (the app), `tests/` (pytest and node page tests), `scripts/` (`train_model.py`, `bench.py`), `src/paybroker/models/` (the bundled model and its training report), `src/paybroker/migrations/`
(numbered SQL).

### 12.2 Commands

```bash
make lint                                  # ruff check src tests scripts (line length 110; rules E F I B UP)
make test                                  # no database needed: rules, model, routing, signing, validation, page scripts
make test-db                               # the whole broker against Postgres (docker compose db; needs POSTGRES_PASSWORD exported)
make train                                 # retrain on 1,000,000 synthetic rows (uv pip install -e ".[train]")
make bench                                 # 400 authorizations against a local Postgres
```

`make test-db` drops and recreates a database `paybroker_test` in the compose Postgres and runs pytest with `TEST_DATABASE_URL` pointing at it. The database tests are skipped when `TEST_DATABASE_URL` is
unset. The test fixtures set `PAYBROKER_ROLE=api` (tests drive the queue themselves) and `PAYBROKER_SIM_LATENCY=off`, and truncate every table between tests. CI (`.github/workflows/build-image.yml`) runs ruff
and the whole suite against a `pgvector/pgvector:pg17` service before it builds the image.

The suite holds about 220 test functions: pure logic (rules, model reproduction of XGBoost, scenarios, routing, signing, validation, URL guard), database tests (payments, idempotency under concurrency, keys and
CORS, federation and roles, environment LLM account, triage limits, training capture and export, reset), the training pipeline and dataset adapters, and `tests/ui/*.mjs` page scripts that run in node against a
small fake DOM (they need node and the sibling `agentpg-kit` checkout for the shared UI modules).

### 12.3 Conventions worth knowing

- Money is integer minor units. Timestamps are `timestamptz`.
- Every authorization is one database transaction; do not add work outside it that the decision depends on.
- Rules and models are data validated at the boundary; nothing from a rule, a model file or an LLM is ever executed.
- The pages build DOM with `textContent` only (a test forbids `innerHTML`, native dialogs and friends).
- Per-process caches (merchant 5 s, API keys 30 s, rules and connectors 5 s, active model and capture setting 30 s) mean a change reaches other processes within seconds.
- The library layer is agentpg-kit: shared fixes go there, not in this repository.

### 12.4 Benchmarks

`make bench` (connectors instant, one connection, local Postgres) measures the fraud engine and the full authorization path; the README records the figures it produced. `scripts/bench.py` honours `BENCH_N`.

## 13. AIDB and AgentPG integration

### 13.1 What runs on AgentPG today

- **One AgentPG sandbox and one Postgres instance** per deployment (the container image, port 8000). The only extension required is `vector`; `fuzzystrmatch` is created by migration `006` when the database allows it, and the EDB extensions (Stat Monitor, Tuner, Query Advisor) are read when present (section 17).
- **pgvector is used for real work:** `fraud_assessment.feature_vector vector(17)` holds each payment's feature vector, indexed with HNSW (`vector_cosine_ops`). Tier 3 finds the five nearest past flagged cases with
  `ORDER BY feature_vector <=> $vector` and gives them to the model together with how analysts resolved them.
- **Postgres is also the queue** (`outbox`, `FOR UPDATE SKIP LOCKED`), the **feature store** (`fs_entity`, bounded event lists, exact velocity), the **idempotency store** (unique index) and the **settings and vault store**.
- **Scale-to-zero friendly:** connections retry with back-off, `/readyz` reports `waking-database`, and the payment API answers `503` (not `500`) while the database wakes.
- **AI goes through the LLM gateway** (OpenAI-compatible), not through the database.

### 13.2 AIDB

PayBroker does **not** use AIDB (`aidb.*` pipelines, knowledge bases, agents or the AIDB MCP endpoint) today. The plan, from `ecosystem/docs/03-aidb-integration.md` (section 5.3), is below. It is a plan; nothing in this
repository implements it yet, and the same document says a short spike on a throwaway instance must come first.

| Today | Planned with AIDB |
|---|---|
| Tier 3 triage: the app calls a model with derived features and nearest cases found by a `vector(17)` distance | An agent whose purpose is limited to **derived feature views** (no card hashes, no personal columns, no write on cases), with tools for case lookup and rule proposals written to the suggestions table only. "The AI never decides" would become a grant boundary in Postgres |
| Daily rule suggestions | A scheduled agent run writing to the suggestions table; approval stays human |
| Analyst questions about the data | A semantic knowledge base over payments, cases and merchants for text-to-SQL, with a read-only role |
| Case narrative similarity | A pipeline embedding case summaries for text retrieval beside the numeric nearest neighbour |

Unchanged by the plan: Tier 1 rules, the Tier 2 model (classical ML scoring is not an AIDB feature), routing, the feature store, the outbox, identity, the LLM gateway's budgets.

## 14. Command-line reference

`paybroker <command>` (installed by the package; in a checkout `.venv/bin/paybroker`):

| Command | Does |
|---|---|
| `migrate` | Apply pending SQL migrations (and create the `vector` extension and the kit's shared tables); prints what was applied |
| `serve` | Run the API, MCP and worker (per `PAYBROKER_ROLE`) with uvicorn on `HOST`:`PORT` |
| `start` | Container entrypoint: migrate (with a readable error if it fails), then serve |
| `demo [-n N] [--seed S]` | Create or reuse the merchant "Demo Shop" and send N simulated payments (default 200, seed 7); prints the secret key once, only when the merchant is created |
| `reset-data --yes --confirm "RESET PAYBROKER DATA" [--keep-training-data] [--keep-rule-suggestions]` | Wipe the transaction data, keep the configuration; exit 2 without `--yes` or with the wrong phrase, 1 if another reset runs |
| `reset-admin` | Forget the admin password, the vault and stored secrets (recovery) |

## 15. Data model at a glance

| Table | Holds |
|---|---|
| `merchant`, `merchant_key` | Merchants (settings, webhook URL and encrypted secret, `allowed_origins`), key hashes and prefixes |
| `card` | Vault: token, brand, BIN, last four, expiry, keyed fingerprint, simulated behaviour |
| `transaction` | Payments: status, amounts, idempotency key and request hash, sanitized request, score and decision, connector, codes |
| `three_ds_challenge` | Challenges: status, 30-minute expiry, optional `return_url` |
| `fraud_assessment` | Per payment: Tier 1 triggers, score, feature snapshot, reasons, decision, action, timings, `feature_vector vector(17)` (HNSW), Tier 3 state and analysis, model version |
| `fraud_case`, `chargeback`, `blacklist`, `fraud_rule`, `rule_suggestion` | Analyst workflow and Tier 1 configuration |
| `fs_entity` | Feature store: per user, device, card and IP, the last 200 events |
| `psp_connector` | Simulated connectors and their live statistics |
| `outbox`, `webhook_event` | The queue and webhook deliveries |
| `llm_account`, `triage_llm_call` | AI accounts (keys encrypted) and the 7-day log of model calls |
| `training_example`, `model_version` | Opt-in training rows and uploaded models |
| `admin_audit` | Who changed what (merchant, key, origin, webhook, triage settings, data resets), never a secret |
| `app_setting`, `web_session`, `api_token`, `oidc_login`, `schema_migrations` | Kit tables: settings and vault material, sessions, operator tokens, SSO state, applied migrations |

## 16. Known limitations

Everything here was found while checking this guide against the code. The code-versus-documentation mismatches found at first (daily triage cap, webhook retry schedule) were fixed in the code or in API.md and the README, and are covered by tests. The vault, webhook and demo-script statements were exercised on a throwaway local database; the rest comes from reading the source, tests and the ecosystem manifest and documents.

**Choices worth knowing**

- **Rule-suggestion trigger score.** The worker queues the daily job when any assessment of the last day scored 500 or more, and Tier 3 similarity uses assessments scoring 500 or more, while a case (and Tier 3 triage)
  needs 700 or more. This is how the code is written, not an error.

**Behaviour to be aware of**

- **Changing `PAYBROKER_ENCRYPTION_KEY` later.** The vault stays locked after the restart until an admin signs in with the password, which re-wraps the key under the new value (section 10.3). Until then webhooks wait and stored AI keys cannot be used.
- **Per-process state.** Rate limits for failed credentials, the AI-triage per-minute bucket and several caches are per process, as SECURITY.md says.
- **No browser-origin rate limit.** Tokenization from browsers has only the body cap and the failed-credential limiter.
- **Sandbox pause and the vault.** After a restart or resume with no platform secret the vault is locked until someone signs in; webhooks wait and stored AI keys cannot be used (the environment LLM account is unaffected).
- **ecoctl does not set `PAYBROKER_PUBLIC_URL`.** The public address then comes from request headers; behind a proxy that does not forward `Host` and `X-Forwarded-Proto` correctly, set it explicitly.
- **`EMBEDDING_*` variables are injected but unused** by PayBroker (`EMBEDDING_MODEL` and `EMBEDDING_DIM` from `ecoctl`'s `llm: true`); its nearest-case search is numeric, so no embedding model is needed.
- **Simulated everything.** Connectors, the geo lookup (a small table over documentation IP ranges; pass `telemetry.ip_country` in real use), the 3-D Secure page and the fraud model's training data are simulations or synthetic. One currency per payment; no settlement, payouts, disputes workflow or reconciliation.
- **Dataset adapters** (TabFormer, Sparkov, PaySim) have tests on tiny fixtures but, per the ecosystem state document, were never run on the real files; the real-data fitting step is not built.
- **AI quality is not measured.** The ecosystem documents state that triage quality against real models has not been evaluated; the bundled model's metrics describe the synthetic generator only.
- **AIDB is not integrated** (section 13.2).

**Not verified by this guide**

- The live fleet's current behaviour, URLs or versions (they change on recreate).
- The exact visual layout of the dashboard beyond the labels and element ids read from `src/paybroker/static/*.js` and `index.html`.
- The kit's shared pages (setup, login, vault banner) beyond the element ids used by the ecosystem's end-to-end harness (`#setup-code`, `#setup-pw`, `#setup-pw2`, `#login-pw`).
- Any Postgres version other than the `pgvector/pgvector:pg17` image the compose file and CI use.

## 17. EDB and AgentPG capabilities used by this app

Background and the verified probe results: `ecosystem/docs/06-edb-and-agentpg-capabilities.md`. Needs agentpg-kit 0.3.0 or newer.

### 17.1 Database insights (EDB Stat Monitor, EDB Postgres Tuner, Query Advisor)

- `GET /api/insights?order=total|mean|calls&limit=1..100` (permission `settings:manage`, the same guard as the other admin routes) returns the kit's `insights_payload`: `sources`, `top_queries`, `tuner`, `advisor`.
  Statement text can show the shape of the workload, which is why it is admin only; the statements are the normalised ones (`$1`), never bound values.
- Every part degrades on its own to `{"available": false, "reason": "..."}`: a missing extension, a missing privilege, a timeout. On plain Postgres (no EDB extensions) the page and the API answer normally and say so; `top_queries` then comes from `pg_stat_statements` if it is loaded, or is unavailable. A bad `order` falls back to `total`.
- **Insights tab** (admin only) mounts the kit's `initInsights`: sortable, filterable top statements, tuner recommendations, advisor suggestions, Refresh. Tested in node against the fake DOM (`tests/ui/insights_tab.mjs`).
- `/metrics` exports `db_insights_*` (section 10.1) so PagerLens can alert on a slow-query regression per `queryid`.
- The platform's own `agentpg recommend all <instance>` builds fuller index and statistics recommendations from the same data.

### 17.2 Read-only access hardening (what a read-only user and AgentPG's restricted MCP can see)

AgentPG gives every instance the group role `a1p_readonly` with SELECT on every public table through default privileges; users created with `agentpg db-users create --permission read`
belong to it, and AgentPG's restricted MCP server (`agentpg mcp serve`, `access_mode: restricted`) runs its queries as one. `src/paybroker/hardening.py` runs as an after-migrate hook
(`migrate.run(after_migrate=[...])`, so on every start, `paybroker migrate` and container start) and takes that default away where it matters. It is idempotent, does nothing (and says so in the log) when the role
does not exist, and must run as the owner of the tables (the app's own connection; a non-owner REVOKE does nothing and is logged). It does not affect the app, `a1p_readwrite` members or the platform owner role.

| Table | Read-only role sees | Rules |
|---|---|---|
| `transaction` | `transaction_masked` | `customer_id`, `idempotency_key` and `card_token` become 12-hex hashes (the same hash everywhere, so joins still work); `request` loses `device` and `customer_id` and has the IP's last octet hidden (`203.0.113.xxx`, IPv6: last group); `request_hash`, `auth_code`, `processor_tx_id` are not exposed |
| `card` | `card_masked` | token and fingerprint hashed; BIN, last4, brand kept; expiry not exposed |
| `merchant` | `merchant_masked` | `webhook_url` reduced to `https://host/[path hidden]` (a path or query can carry a token); `webhook_secret_enc` not exposed |
| `three_ds_challenge` | `three_ds_challenge_masked` | the challenge id (a bearer link to the hosted page) hashed; `return_url` reduced to its origin |
| `fs_entity` | `fs_entity_masked` | ids hashed (IPs get the last octet hidden); the `events` and `links` lists removed, counters kept |
| `blacklist` | `blacklist_masked` | IP masked, device and card fingerprint hashed, BIN and email domain as they are |
| `chargeback` | `chargeback_masked` | `customer_id` hashed |
| `fraud_case` | `fraud_case_masked` | `note` redacted, `resolved_by` reduced to an initial |
| `admin_audit` | `admin_audit_masked` | `actor` reduced to an initial |
| `webhook_event` | `webhook_event_masked` | event type, status, attempts, codes; the body (it holds the 3-D Secure link) not exposed |
| `fraud_assessment`, `fraud_rule`, `rule_suggestion`, `psp_connector`, `outbox`, `model_version`, `training_example`, `triage_llm_call`, `schema_migrations` | the table itself | configuration and numbers, no personal data |
| `app_setting` (kit settings and vault store), `web_session`, `api_token`, `oidc_login`, `merchant_key`, `llm_account` | nothing | secrets, hashes or ciphertext: SELECT revoked, **no view** |

PayBroker never stores a card number, CVV, customer email (only its domain, in `request.email_domain`) or a person's name from the payment API, so the masks there are defence in depth. Limits:

- Writes are refused to the role everywhere; the views are `security_barrier`.
- A **new table is readable by the role until it is classified** (default privileges re-grant on creation). `hardening.unclassified()` lists such tables, the hook logs a warning for each, and a test fails if a migration adds a table nobody classified: add it to `MASKED`, `HIDDEN` or `READABLE`.
- The 12-hex hashes are unsalted SHA-256 prefixes: they hide the value from reading, not from a guess-and-compare of a low-entropy value.
- `fraud_assessment.tier3_llm_analysis` and the feature snapshot are readable: they are derived numbers and the model's text, built without identifiers (the triage payload has no customer id or IP).
- Tested with a real read-only login role (`tests/test_edb_features_db.py`): base tables and secrets tables denied, masked views readable, writes refused, exactly the classified set readable.

### 17.3 The PayBroker MCP and AgentPG's restricted MCP

The PayBroker MCP (section 7.12) is unchanged. Candidates to retire in favour of AgentPG's restricted MCP over the masked views (generic SQL, schema and `EXPLAIN` as the read-only user), once it is wired for this app:
`search_payments` (`transaction_masked` plus `card_masked`), `list_fraud_cases` (`fraud_case_masked` plus `fraud_assessment`), `list_fraud_rules` and `list_rule_suggestions` (plain tables, readable as they are),
and `get_overview` (aggregates over `transaction_masked`). `explain_payment` is the one to keep: it joins eight tables and returns the unmasked detail an operator needs, behind `mcp:use`. A reason to keep any of them:
they run under PayBroker's permission scopes and return curated shapes, while the restricted MCP's HTTP mode has no authentication unless `A1P_JWT_SECRET` is set. Nothing was removed.

### 17.4 fuzzystrmatch

`NAME_MISMATCH`, section 7.9. Other extensions the platform offers (`unaccent`, `citext`, `tcn`, `postgis`) are not used by PayBroker.
