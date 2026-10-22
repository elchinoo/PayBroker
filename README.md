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
| any 16-digit Luhn-valid number starting `99999901` | the issuer declines (`do_not_honor`) |
| any 16-digit Luhn-valid number starting `99999902` | the issuer always demands 3-D Secure |
| any 16-digit Luhn-valid number starting `99999903` | the first connector answers 5xx; the cascade recovers on the next |
| any other Luhn-valid number starting `999999` | random behaviour from each connector's configured rates |

The three `999999xx` prefixes exist so a simulator can give every customer its own card with the behaviour: one public
number shared by hundreds of payments an hour has a huge per-card velocity and is fraud-rejected before it reaches the issuer,
3-D Secure or connector logic.

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

## Model, training and data

**Techniques.** Tier 2 is a gradient-boosted decision tree ensemble (XGBoost, 70 trees of depth 3, log loss, 17 features) whose
trees are exported to a small JSON file and walked in pure Python. Depth 3 keeps every score explainable: the per-feature
contributions of the walk become the reason codes. The 17 features (`amount_log`, `amount_vs_avg_ratio`, `user_tx_1h`,
`user_tx_24h`, `user_sum_24h_log`, `distinct_cards_7d`, `ip_tx_1h`, `device_tx_1h`, `card_tx_1h`, `chargebacks`,
`device_linked_chargebacks`, `impossible_travel`, `new_device_for_account`, `checkout_seconds_log`, `pasted_card`,
`country_mismatch`, `account_age_hours_log`) come from the feature store and the request; they are the same ones at training
and at scoring time.

**How the bundled model was trained.** `make train` generates **1,000,000 synthetic rows** (`src/paybroker/synth.py`; vectorised,
about a quarter of a second and 70 MB) in the original class mix scaled up: 78 % ordinary traffic (including *busy shoppers*,
who pay several times in an hour with the user, card, IP and device counters rising together, and *shared networks*, many
customers behind one IP), 7 % benign anomalies (travellers), and 7.7 % attacks (account takeover, card testing, synthetic
identities, chargeback-linked devices), a share of them softened so scores spread, plus 2 % label noise. It trains on 80 % and
evaluates on the other 20 %; the whole run takes about a second. On that holdout: PR-AUC 0.776, ROC-AUC 0.879, recall at 1 %
false positives 0.719; 93 % of rows score under 300, 6 % decline, and 98 % of what declines is fraud by construction. The model
file is 42 KB (42,493 bytes, 70 trees, about 1,000 nodes); `src/paybroker/models/training_report.json` holds the numbers, the score distribution and the table below.

**A regular shopper is not challenged.** The previous model had never seen a legitimate customer pay many times in an hour, so
it learned that velocity means fraud: four earlier payments in the hour (same card, IP and device) scored 385 and five scored 428,
a 3-D Secure challenge for a busy afternoon. `src/paybroker/scenarios.py` is the scoring harness: named situations as explicit
feature vectors with the band each must land in, asserted by `tests/test_pure.py` against the bundled model and printed by every
training run. Retraining with the busy-shopper class fixed it:

| Situation | Old model | New model |
|---|---|---|
| ordinary first payment | 29 | 28 |
| regular shopper, 4 earlier payments this hour | 385 | 26 |
| regular shopper, 5 earlier payments this hour | 428 | 26 |
| regular shopper, 6 earlier payments this hour | 428 | 38 |
| busy day: 8 payments in 24 h, 3 in the last hour | 177 | 26 |
| card testing: 12 cards from one device and IP | 990 | 993 |
| card testing: one new customer per card, one device and IP | 914 | 986 |
| account takeover: new device, travel, big spend, fast | 997 | 999 |
| account takeover: new device and country, spend 6x usual, fast | 978 | 978 |
| synthetic identity, linked chargebacks | 994 | 990 |

A sweep of 6 earlier payments over amounts, checkout speeds and counter patterns stays below 300 for any ordinary customer
(a test). A customer who is also unusual (a USD 1,500 basket in 20 seconds on a sixth payment, spend 13 times the usual)
still lands in 3-D Secure or above, as it should.

### Capture the data to retrain or improve the model

Off by default. **Settings > Model > Training data** (or `PUT /api/admin/training/settings`, or the environment variable
`PAYBROKER_CAPTURE_TRAINING_DATA=1` as the default when nothing is stored; a stored setting wins) switches it on. For every
scored payment the broker then writes **one row** to `training_example`, in the payment's own database round trip (no extra call;
see *Measured* below): the 17 numbers, the feature-layout hash, the model version, the score, the decision and the payment
reference. **No card, e-mail, IP address, device id or name** is ever in it.

A row's `label` is null until a real outcome is known, and is never guessed:

| Outcome | Label | Source |
|---|---|---|
| fraud-coded chargeback (`reason_code` 10.1 to 10.5, 4837, 4840, 4863, 4870, 4871, or the reason exactly `fraud`) | 1 | `chargeback` |
| analyst resolves a case `PERMANENT_BLOCK` | 1 | `case` |
| analyst resolves a case `DISMISS_ALERT` (false positive) | 0 | `case` |
| analyst resolves a case `ALLOW_WITH_KYC` | none: it asserts neither | |
| a chargeback that is not fraud-coded | none: it proves nothing about fraud | |
| admin runs `POST /api/admin/training/label-aging {days: N}` | 0 for approved and captured payments, at least N days old, with no dispute and no open case | `aging` |

A stronger source replaces a weaker one (chargeback over case over aging), never the other way round. Aging never runs by
itself, and it never labels a declined or uncaptured payment (silence about a payment that could not be disputed proves nothing).

**Retention:** rows older than `retention_days` (default 180) are purged, and rows above `max_rows` (default 5,000,000) are purged
oldest first; the worker does it every ten minutes in bounded batches (even while capture is off), or call
`POST /api/admin/training/purge`.

**Export** (admin only, permission `training:export`): `GET /api/admin/training/export?format=csv|ndjson&labeled=true&since=2026-01-01`
streams the rows from a server-side cursor, so memory stays flat. The CSV starts with `#` comment lines (the feature list and
order, the feature hash, the model versions, the filters); the NDJSON starts with a `{"_meta": ...}` line. `GET /api/admin/training/stats`
counts rows, labels, the fraud rate and per-model-version results. Settings > Model does all of this from the browser.

### Retrain

```bash
uv pip install -e ".[train]"
make train                                   # 1,000,000 synthetic rows -> the bundled model + training_report.json
python scripts/train_model.py --synthetic-rows 2000000 --out /tmp/model.json
python scripts/train_model.py --from-export training.csv --mix-synthetic 0.3 --out my_model.json
python scripts/train_model.py --dataset tabformer --path card_transaction.v1.csv --mix-synthetic 0.2 --out tf_model.json
```

`--synthetic-rows N` (default 1,000,000), `--from-export FILE` (CSV or NDJSON; labeled rows only, `--label-sources` filters),
`--mix-synthetic F` (the synthetic share of all rows when a real source is given), `--dataset tabformer|sparkov|paysim --path FILE`,
`--fill class|neutral`, `--max-real-rows` (a memory cap: fraud is kept, legitimate rows are down-sampled and weighted),
`--rounds`, `--max-depth`, `--holdout`, `--out`, `--report`, `--version`, `--dry-run`; `--help` lists them all. The holdout is 20 %,
**time-ordered** (the newest rows) for exports and datasets and stratified-random for synthetic rows. The report gives PR-AUC,
ROC-AUC, recall at 1 % false positives and the score distribution per source, and warns about the traps below.

**Public datasets** go through adapters (`src/paybroker/datasets.py`), which read in chunks with per-entity rolling state, so the
24 million rows of TabFormer never sit in memory. **No dataset is included or downloaded by this project**; you choose and download
them. The adapters read columns by header name and map as many of the 17 features as the data supports:

| Dataset | Columns (as documented) | Real features | Completed from the generator |
|---|---|---|---|
| IBM TabFormer (about 24 M card transactions) | `User, Card, Year, Month, Day, Time, Amount, Use Chip, Merchant Name, Merchant City, Merchant State, Zip, MCC, Errors?, Is Fraud?` | amount, ratio to the user's 30-day average, user counts 1 h and 24 h, 24 h spend, distinct cards 7 d, card count 1 h; impossible travel *approximately* (two in-person payments within 2 h in different state and city: the file has no coordinates). Refunds (negative amounts) are skipped | IP and device velocity, chargebacks, device chargebacks, new device, checkout time, pasted card, country mismatch, account age |
| Sparkov / Kaggle (about 1.3 M) | `trans_date_trans_time, cc_num, merchant, category, amt, gender, lat, long, city_pop, job, dob, trans_num, unix_time, merch_lat, merch_long, is_fraud` (the files also carry `first, last, street, city, state, zip`, never read) | amount, ratio, user and card counts, 24 h spend, distinct cards (always 1: one card per customer), impossible travel from the merchant coordinates of consecutive payments (over 900 km/h) | IP and device velocity, chargebacks, new device, checkout time, pasted card, country mismatch, account age |
| PaySim (about 6.3 M) | `step, type, amount, nameOrig, oldbalanceOrg, newbalanceOrig, nameDest, oldbalanceDest, newbalanceDest, isFraud, isFlaggedFraud` | amount, ratio, user counts 1 h and 24 h, 24 h spend (`nameOrig` rarely repeats, so most are 0) | everything else |

> [!WARNING]
> **PaySim's balance columns leak the label** (its fraud empties the origin account, so `oldbalanceOrg`, `newbalanceOrig`,
> `oldbalanceDest`, `newbalanceDest` give it away, and `isFlaggedFraud` is a rule's output). The adapter never reads them; a test
> proves that changing them changes nothing. `type` is not one of the 17 features and PaySim's fraud is only `TRANSFER` and `CASH_OUT`.

Features a dataset cannot provide are drawn from the synthetic generator for the row's class, and the model file records it in
`trained_on` (sources, rows, which features were real, approximate or synthetic, how many rows were skipped, the fill mode).
**This flatters the model:** the completed columns are conditioned on the label, so they are informative by construction. The report
therefore scores the holdout again with the completed columns replaced by ordinary-traffic draws (`dataset:NAME:real_features_only`):
that is what the real columns alone achieve. `--fill neutral` completes from ordinary traffic for every row instead.

### Upload a model and roll back, without rebuilding the image

**Settings > Model** (or `POST /api/admin/models`, permission `model:manage`, admin only) takes a model JSON in the format the training
script writes. It is **validated before it is stored**: the 17 features exactly and in order, at most 500 trees of depth 8, at most
2 MB, every number finite, every child after its parent with exactly one parent (so a walk always ends), and the file's own reference
scores must be reproduced by its trees. It is stored in the database with its version, notes and uploader and **never activates by itself**
unless you ask. Activating one (or `POST /api/admin/models/use-bundled`) takes effect at once in that instance and within about 30
seconds in the others; **rollback is activating an earlier version**. The bundled file is always the fallback. Every score carries
its model version into `fraud_assessment.model_version` and the training row; the dashboard shows it in a payment's detail, and merchant
API responses are unchanged.

### What to keep in mind

- **Synthetic is not real.** The bundled model and every metric above describe the generator, not fraud. Retrain on your own labelled
  history before drawing any conclusion from a score.
- **Labels arrive late and are biased.** A chargeback shows up weeks after the payment; payments the model declined never get a
  chargeback, so the labelled set is mostly payments the model approved. Aging labels cover approved payments only and may include fraud
  that has not been disputed yet.
- **Feedback loop.** A model trained on rows scored (and filtered) by a model learns to imitate that model's decisions. Mix other data
  in (`--mix-synthetic`, a public dataset), prefer `chargeback` and `case` labels (`--label-sources chargeback,case`), compare against
  the live model on a time-ordered holdout, and activate a new model only after reviewing the report. Roll back if approvals or declines move unexpectedly.
- **Privacy.** Training rows hold derived numbers and an internal payment reference, nothing else; retention and a row cap bound them;
  only an admin can export or change any of it. See [SECURITY.md](SECURITY.md).
- **Measured, not promised.** Capture rides in the same pipelined round trip the feature store already makes, so it adds no
  database call. `scripts/bench.py` (1,000 payments per run, one connection, connectors instant, local Postgres), median of six
  interleaved runs, full authorization p50 / p95: before this feature **5.68 / 8.86 ms**, after with capture off **5.53 / 9.14 ms**,
  capture on **5.83 / 9.79 ms** (the fraud engine alone, which finishes before the insert: 1.58, 1.66, 1.75 ms). Run-to-run noise on a
  laptop is larger than the effect, so a second measurement alternated capture off and on payment by payment inside one process
  (3 x 1,500 payments each): the difference is **+0.02 to +0.09 ms at p50** and +0.02 to +0.15 ms on the mean, about 1 %.

## Reset the data and start fresh

Settings > Maintenance (admin only), `POST /api/admin/data/reset` and `paybroker reset-data` wipe the **transaction data** and keep the
**configuration**, for example after a simulator bug filled the system with payments.

```bash
paybroker reset-data --yes --confirm "RESET PAYBROKER DATA" [--keep-training-data] [--keep-rule-suggestions]
```

| Deleted | Kept |
|---|---|
| `transaction`: payments, with their captures, refunds, attempts, idempotency records and card references (all columns of the payment row) | `merchant`, `merchant_key` (a key that worked keeps working), webhook URL and secret |
| `three_ds_challenge`, `fraud_assessment`, `fraud_case` (with its decision trail), `chargeback` | `card`: vaulted test-card tokens (metadata only; the merchant may hold them, and they feed no score) |
| `webhook_event` (deliveries) and `outbox` (the queue) | `psp_connector` (routing configuration; their live auth and latency averages are not reset) |
| `fs_entity`: the feature store (a customer with ten payments scores like a new one afterwards) | `fraud_rule` and `blacklist`, including entries a case or chargeback once added (remove them under Rules if wanted) |
| `training_example` (unless `include_training_data: false` / `--keep-training-data`) | `model_version` (uploaded models and the active one), `llm_account`, `app_setting` (capture and retention settings, vault) |
| `rule_suggestion` (unless `include_rule_suggestions: false` / `--keep-rule-suggestions`) | `web_session`, `api_token`, `oidc_login`, `admin_audit`, `schema_migrations` |

It deletes children first, at most 5,000 rows per transaction, so it is safe to interrupt and to run again (it continues); a database
advisory lock stops two resets from overlapping (409 over the API, exit code 1 on the command line). Each run adds a row to
`admin_audit` (who, when, options, rows deleted per table), shown under Settings > Maintenance. Nothing process-local depends on the deleted data (the merchant, key, rule,
connector and training-setting caches hold configuration, which is kept), so other running instances need no restart: their caches expire within
5 to 30 seconds anyway. Payments sent while a reset runs may survive it; pause the senders first. Sequences are not rewound and the
space is returned to the table by autovacuum.

## Roles and single sign-on

Standalone, the first-run admin has everything. Federated with the identity provider (`PAYBROKER_OIDC_ISSUER`, client id
`paybroker`, redirect `https://<paybroker>/auth/callback`):

| Role | May |
|---|---|
| `admin` | everything, including exporting training data (`training:export`), managing models and capture (`model:manage`) and resetting the transaction data (`data:reset`), which no other role has |
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
| `PAYBROKER_CAPTURE_TRAINING_DATA` | `1` captures training data when no stored setting says otherwise (default off). |
| `PAYBROKER_TRAINING_RETENTION_DAYS`, `PAYBROKER_TRAINING_MAX_ROWS` | Defaults for the retention (180 days) and row cap (5,000,000) until set in Settings > Model. |
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
- The model learned from synthetic data. Capture your own labelled history, retrain or upload a model (see *Model, training and
  data*) before drawing any conclusion from a score.
- The geo lookup is a tiny table over documentation IP ranges; pass `telemetry.ip_country` from your edge in real use.
- Per-process, in-memory caches (merchant settings 5 s, API keys 30 s, rules and connectors 5 s): a change reaches other
  processes within seconds, and immediately in the process that made it.
- One currency per payment, no settlement, payouts, disputes workflow or reconciliation.

## Development

```bash
make lint
make test                                     # no database needed: rules, model, routing, signing, validation
make test-db                                  # the whole broker against Postgres (needs POSTGRES_PASSWORD exported)
make train                                    # retrain on 1,000,000 synthetic rows (uv pip install -e ".[train]"); see *Model, training and data*
make bench
```

See [SECURITY.md](SECURITY.md) for the security model and its limits, and [API.md](API.md) for every endpoint.
