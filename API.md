# PayBroker API

Two APIs share one server. The **payment API** (`/v1`) is for merchants and takes merchant keys. The **operator API**
(`/api`) is for the dashboard, scripts and agents and takes sessions, operator tokens or single-sign-on tokens. A credential
of one kind is refused on the other.

All bodies are JSON and **unknown fields are rejected (422)**. Amounts are integers in minor units (cents). Bodies over 64 KB
are refused (413). Send `Authorization: Bearer <key>`.

## Payment API (`/v1`)

| Key | Prefix | Can |
|---|---|---|
| Secret | `sk_test_` | everything below (server side only) |
| Publishable | `pk_test_` | only `POST /v1/vault/tokens` (safe in a browser, from the merchant's allowed origins: see *Browser access*) |

### `POST /v1/vault/tokens` → 201
```json
{"number": "4242424242424242", "exp_month": 12, "exp_year": 2035, "cvc": "123"}
```
Returns `{token, brand, bin, last4, exp_month, exp_year, fingerprint}`. Only the public processor test cards and numbers
starting `999999` are accepted (16-digit numbers starting `99999901` always decline, `99999902` always need 3-D Secure,
`99999903` fail on the first connector and recover; see the README table); anything else is `422 INVALID_CARD`. The number and CVC are checked and discarded.

### Browser access (CORS) for `POST /v1/vault/tokens`
A shop's page on another origin can call this one endpoint from the customer's browser, so card numbers never reach the shop's server.
Nothing else in `/v1` (authorize, capture, void, refund, the reads) or anywhere else on the server ever gets CORS headers.

* Each merchant lists its **allowed origins** under Merchants (or `PATCH /api/merchants/{id}` `{"allowed_origins": [...]}`, which
  replaces the list; `[]` turns browser access off). An origin is exactly `https://host[:port]`, or `http://localhost[:port]` and
  `http://127.0.0.1[:port]` for local development. No wildcards, no paths, no credentials, no IP addresses, at most 20; they are
  normalized (lower case, default port dropped) and anything else is `422`. Every change writes an `admin_audit` row
  (`merchant.allowed_origins`: merchant, old and new list).
* **Preflight** (`OPTIONS /v1/vault/tokens`, no credential is sent): answered `204` with `Access-Control-Allow-Origin` (the request's
  own origin, never `*`), `Access-Control-Allow-Methods: POST, OPTIONS`, `Access-Control-Allow-Headers: Authorization, Content-Type,
  Idempotency-Key`, `Access-Control-Max-Age: 600` and `Vary: Origin` when **any active merchant** lists the origin and the requested
  method is `POST`; otherwise `403 origin_not_allowed` with no CORS headers.
* **The call itself**: when the request has an `Origin`, the key's **own merchant** must list it and the key must be publishable;
  otherwise `403 {"error": {"code": "origin_not_allowed"}}` with no CORS headers (a secret key from a browser is refused on purpose).
  The response then carries `Access-Control-Allow-Origin: <that origin>` and `Vary: Origin`, errors included, so the page can read
  them. `Access-Control-Allow-Credentials` is never sent: the key travels in `Authorization`, not in cookies. A call without an
  `Origin` (your server, curl) is unaffected.
* The usual limits apply: 64 KB bodies, and the failed-credential limiter (429). There is no per-origin request rate limit yet.

### `POST /v1/payments/authorize`
```json
{
  "idempotency_key": "order-1001",
  "amount": 4999, "currency": "USD",
  "capture_method": "manual",
  "payment_method": {"type": "card", "card_token": "tok_vault_card_...", "cvv_check": true, "cardholder_name": "Pat Doe"},
  "customer": {"id": "cus_42", "name": "Patricia Doe", "email": "pat@example.com", "phone": "+15550100", "ip_address": "198.51.100.20", "account_age_hours": 4000},
  "telemetry": {"device_fingerprint": "dev_abc", "user_agent": "...", "checkout_duration_ms": 41000, "pasted_card_number": false, "ip_country": "US"},
  "shipping_address": {"line1": "...", "city": "...", "state": "...", "postal_code": "...", "country": "US"}
}
```
`idempotency_key` (8 to 128 characters) is required. **A retry with the same key and the same body returns the first result;
the same key with a different body is `422 IDEMPOTENCY_KEY_REUSED`.** Concurrent retries create one payment.
`capture_method` is `manual` (authorize now, capture later) or `automatic`; the merchant's default applies when omitted.
`return_url` (optional, up to 500 characters) is where the hosted 3-D Secure page sends the customer afterwards: see *3-D Secure*.
`account_age_hours` is how old the customer's account is at your site; when absent an established account is assumed.
`payment_method.cardholder_name` and `customer.name` (both optional, up to 100 characters) feed the `NAME_MISMATCH` risk signal when **both** are present: Postgres `fuzzystrmatch` compares them (spelling variants such as `Jon Smith` and `John Smyth` are similar) and a clear mismatch adds the reason `NAME_MISMATCH`, and asks for 3-D Secure on baskets of 30,000 minor units or more. The names are compared and dropped; they are not stored.

| Status | Body | Meaning |
|---|---|---|
| 200 | the payment (`status` `AUTHORIZED` or `CAPTURED`) | approved |
| 200 | `status: REQUIRES_ACTION` and `action_required: {type: "3DS_CHALLENGE", redirect_url}` | send the customer to `redirect_url`; the result arrives by webhook (and by the signed return, if you passed `return_url`) |
| 402 | `error.code: CARD_DECLINED`, `decline_code` | the issuer declined (final, not retried on another connector) |
| 403 | `error.code: FRAUD_REJECTED`, `fraud_assessment` | declined by the fraud engine; the connector was never called |
| 403 | `error.code: MERCHANT_MISMATCH` | `merchant_id` in the body is not this key's merchant |
| 422 | `IDEMPOTENCY_KEY_REUSED`, `CURRENCY_NOT_ALLOWED`, `INVALID_CARD_TOKEN`, `NO_ROUTE`, `RETURN_URL_NOT_ALLOWED`, `RETURN_URL_NEEDS_WEBHOOK`, validation errors | fix the request |
| 429 | | too many failed credentials from this address |
| 502 | `PSP_UNAVAILABLE` | every eligible connector failed within the budget |
| 503 | | the database is unavailable (it may be waking up); retry |

A payment looks like:
```json
{"payment_id": "pay_...", "status": "AUTHORIZED", "amount": 4999, "currency": "USD", "created_at": "...",
 "processor_response": {"psp_name": "stripe", "authorization_code": "AUTH_...", "network_transaction_id": "ntx_..."},
 "fraud_assessment": {"score": 41, "decision": "APPROVE", "action_taken": "NONE", "execution_time_ms": 2, "reasons": []}}
```
`reasons` lists the reason codes when the score is 300 or more or a rule fired.

### Other payment endpoints
| Call | Notes |
|---|---|
| `GET /v1/payments/{payment_id}` | your own payments only (404 otherwise) |
| `GET /v1/payments?limit=&status=` | newest first, up to 100 |
| `POST /v1/payments/{id}/capture` `{"amount": 4000}` | `AUTHORIZED` only; the amount is optional (default all), at most the authorized amount |
| `POST /v1/payments/{id}/void` | `AUTHORIZED` only |
| `POST /v1/payments/{id}/refund` `{"amount": 1000}` | `CAPTURED` or `PARTIALLY_REFUNDED`; at most what is left |

Wrong state is `409 INVALID_STATE`; a bad amount `422 INVALID_AMOUNT`.

Statuses: `PROCESSING`, `REQUIRES_ACTION`, `AUTHORIZED`, `CAPTURED`, `PARTIALLY_REFUNDED`, `REFUNDED`, `VOIDED`, `DECLINED`, `FAILED`.

### 3-D Secure (simulated issuer)
`GET /3ds/{challenge_id}` is the hosted page. `GET /api/3ds/{id}` and `POST /api/3ds/{id}/complete {"outcome": "succeeded"}|{"outcome": "failed"}`
are what it calls. **Test mode only**: anyone holding the unguessable challenge id can complete it, because a real issuer's
result would come from the card network instead. A challenge lasts 30 minutes.

**How a shop sends the customer there and back.**
1. Authorize with `return_url`, a URL on one of the merchant's allowed origins (`https`, or `http://localhost`; no credentials, no
   fragment, and none of the query parameters `payment_id`, `status`, `timestamp`, `signature`, which the page adds). It is refused
   with `422 RETURN_URL_NOT_ALLOWED` otherwise, and with `422 RETURN_URL_NEEDS_WEBHOOK` when the merchant has no webhook (the
   redirect is signed with the webhook secret). Without `return_url` nothing changes: the page shows the result and the shop
   learns it by webhook.
2. If the answer is `REQUIRES_ACTION`, redirect the customer's browser to `action_required.redirect_url`.
3. After the customer completes the challenge, the page redirects to your `return_url` with four query parameters appended:
   `payment_id`, `status` (`AUTHORIZED`, `CAPTURED`, `DECLINED` or `FAILED`), `timestamp` (unix seconds) and `signature`.
4. **Verify before trusting it**: `signature = hex(HMAC-SHA256(webhook secret, timestamp + "." + "payment_id=<id>&status=<STATUS>"))`,
   the webhook scheme over that fixed string. Compare in constant time, reject a timestamp older than five minutes, and treat the
   webhook (or `GET /v1/payments/{id}` with your secret key) as the source of truth. If the secret cannot be read at that moment
   (the vault is locked) the page does not redirect at all.

### Using PayBroker from a web shop
The five steps, with the exact calls (the full walkthrough, with code, is in the README):
1. Under Merchants: list the shop's origin (`https://shop.example.com`), set a webhook URL (keep the signing secret), create a
   publishable key for the page and a secret key for the server.
2. In the browser, tokenize with the publishable key: `fetch("https://<broker>/v1/vault/tokens", {method: "POST", headers: {"Authorization": "Bearer pk_test_...", "Content-Type": "application/json"}, body: JSON.stringify({number, exp_month, exp_year, cvc})})` gives `{token, ...}`. Send only the `token` to the shop's server.
3. On the server, `POST /v1/payments/authorize` with the secret key, an `idempotency_key`, the token and, for 3-D Secure, `return_url`.
4. On `REQUIRES_ACTION`, redirect the customer to `action_required.redirect_url`; verify the signed return as above.
5. Verify every webhook (below), then capture or void.

### Webhooks
Set a URL under Merchants (or `PUT /api/merchants/{id}/webhook`); the signing secret is shown once.
```
POST <your url>
X-Timestamp: 1767225600
X-Signature: hex( HMAC-SHA256(secret, timestamp + "." + raw body) )
```
Verify over the **raw** body, compare in constant time, and reject timestamps older than five minutes. The body is
`{"id": "evt_...", "type": "payment.authorized", "created_at": "...", "data": <the payment>}`. Types: `payment.authorized`,
`.captured`, `.requires_action`, `.declined`, `.failed`, `.voided`, `.refunded`. Any 2xx counts as delivered; otherwise the
delivery is retried after 30 s, 5 min, 30 min, 2 h, 6 h and 15 h (seven attempts, about 23.6 hours in all), and each attempt is visible in the dashboard.
Deliveries are at least once: use `id` to deduplicate.

## Operator API (`/api`)

Permissions (what a role or token scope grants): `payments:read`, `cases:review`, `rules:manage`, `merchants:manage`,
`settings:manage`, `training:export`, `model:manage`, `data:reset`, `mcp:use`, `metrics:scrape`. Three of them (`training:export`,
`model:manage` and `data:reset`) belong to the `admin` role and the `full` scope only. Token scopes: `read` (`payments:read`, `mcp:use`), `metrics`, `full`.
A service token with scope `paybroker.admin` holds `merchants:manage` and `payments:read` and nothing else (see *Provisioning from another service*).
Browser sessions send a custom `X-PayBroker-CSRF` header on writes; bearer-token calls don't need it.

| Call | Needs | Notes |
|---|---|---|
| `GET /api/session` | none | what the UI should show; the caller's roles and permissions |
| `GET /api/overview?days=` | `payments:read` | counts, approval rate, volume, latency, score histogram, connectors, queue |
| `GET /api/payments?q=&status=&decision=&min_score=&merchant_id=&limit=&offset=` | `payments:read` | |
| `GET /api/payments/{id}` | `payments:read` | the request, rules fired, score and reasons, features, timings and routing attempts, AI triage, case, 3-D Secure, webhooks, chargebacks |
| `POST /api/payments/{id}/chargeback` `{amount?, reason, reason_code?}` | `cases:review` | updates the feature store; a fraud `reason_code` (10.1 to 10.5, 4837, 4840, 4863, 4870, 4871) labels the payment's training row as fraud |
| `GET /api/cases?status=open\|resolved\|all` | `payments:read` | with the AI summary and recommendation |
| `POST /api/cases/{id}/resolve` `{decision, note}` | `cases:review` | `PERMANENT_BLOCK`, `ALLOW_WITH_KYC`, `DISMISS_ALERT` |
| `GET /api/rules`, `POST /api/rules`, `PATCH /api/rules/{id}` `{enabled}`, `DELETE /api/rules/{id}` | read / `rules:manage` | the definition is validated; the fields and operators are returned by the GET |
| `GET /api/suggestions`, `POST /api/suggestions/run`, `POST /api/suggestions/{id}/decide` `{approve}` | read / `rules:manage` | nothing activates until approved |
| `GET/POST/DELETE /api/blacklist` | read / `cases:review` | kinds: `ip`, `device`, `card_fingerprint`, `bin`, `email_domain` |
| `GET /api/merchants?name=` | `payments:read` | `name` keeps only the merchant with exactly that name |
| `GET/POST /api/merchants`, `GET/PATCH /api/merchants/{id}`, `PUT .../webhook`, `POST/DELETE .../keys` | read / `merchants:manage` | keys and the webhook secret are returned once |
| `GET /api/connectors`, `PATCH /api/connectors/{name}` `{enabled}` | read / `merchants:manage` | |
| `GET /api/webhook-events` | `payments:read` | |
| `GET/POST/PUT/DELETE /api/accounts`, `PUT /api/triage-account` | `settings:manage` | AI accounts; keys are never returned |
| `GET/POST/DELETE /api/tokens` | `settings:manage` | operator tokens, shown once |
| `GET /api/insights?order=total\|mean\|calls&limit=20` | `settings:manage` | database insights: top statements, tuner recommendations, index advisor suggestions; each part is `{available: false, reason}` where the extension or privilege is missing |
| `POST /api/demo/traffic` `{count, seed}` | `settings:manage` | fictional payments through the real pipeline |
| `GET /api/admin/triage/settings`, `PUT` `{rpm?, daily_cap?, group_minutes?, pause_minutes?}` | `settings:manage` | limits on LLM triage: calls per minute 0 to 600 (default 20, 0 = unlimited, per worker process), calls per 24 h 0 to 1,000,000 (default 120, 0 = unlimited), grouping window 0 to 1,440 minutes (default 30, 0 = off), breaker pause 1 to 1,440 minutes (default 10); a stored value wins over `PAYBROKER_TRIAGE_*`; PUT answers 422 out of bounds and writes an `admin_audit` row (`triage.settings`, old and new values). Both return the limits, `sources` (`setting`, `environment` or `default` for each) and `usage`: `{calls_24h, queued, circuit: {open, paused_until, consecutive_429}}` |
| `GET /api/admin/training/settings`, `PUT` `{capture?, retention_days?, max_rows?}` | `training:export` or `model:manage` / `model:manage` | capture on or off (default off), retention 1 to 3650 days (default 180), row cap 1,000 to 50,000,000 (default 5,000,000) |
| `GET /api/admin/training/stats` | `training:export` | rows, labeled, fraud, fraud rate, label sources, per model version, feature hash |
| `GET /api/admin/training/export?format=csv\|ndjson&labeled=&since=&limit=` | `training:export` | streamed; CSV with `#` header comments (features, hash, model versions), NDJSON with a `_meta` first line; rows of an older feature layout are skipped |
| `POST /api/admin/training/label-aging` `{days}` | `model:manage` | labels approved, captured, undisputed payments older than `days` as legitimate; returns `{labeled_legitimate}` |
| `POST /api/admin/training/purge` | `model:manage` | applies retention and the row cap now; returns the counts |
| `GET /api/admin/models` | `model:manage` | active model, the bundled one, uploaded versions and the limits |
| `POST /api/admin/models?notes=&version=&activate=` | `model:manage` | body: the model file, or `{model, notes?, version?, activate?}`; up to 2 MB; 422 with the reason when invalid, 409 when the version exists |
| `GET /api/admin/models/{id}` | `model:manage` | the model document |
| `POST /api/admin/models/{id}/activate`, `POST /api/admin/models/use-bundled` | `model:manage` | switch (or roll back) the scoring model; other instances follow within about 30 s |
| `DELETE /api/admin/models/{id}` | `model:manage` | refused (409) while active |
| `GET /api/admin/data/reset` | `data:reset` | what a reset would delete right now (exact row counts per table), the confirmation phrase, and the last 10 resets (who, when, outcome, counts) |
| `POST /api/admin/data/reset` `{confirm, include_training_data?, include_rule_suggestions?}` | `data:reset` | wipes the transaction data and keeps the configuration (see below); `confirm` must be exactly `RESET PAYBROKER DATA` (else 422, nothing is deleted); both flags default to `true`; 409 while another reset runs; returns `{deleted: {table: rows}, skipped, seconds, chunks, max_chunk_rows, audit_id}` |
| `GET /metrics` | `metrics:scrape` | Prometheus |

### Reset the data (`POST /api/admin/data/reset`)

Deletes, children first, in chunks of at most 5,000 rows per transaction: the outbox queue, `webhook_event`, `three_ds_challenge`,
`fraud_assessment`, `fraud_case`, `chargeback`, `transaction` (payments, with their captures, refunds, attempts, idempotency records and
card references, which are columns of the payment row), `fs_entity` (the feature store: the velocity the model remembers), and, unless
switched off, `training_example` and `rule_suggestion`. Everything else is kept: merchants and their keys, webhook endpoints, card tokens,
connectors, fraud rules, the blocked list, settings, AI accounts, uploaded models, users, sessions and operator tokens. A key that worked
before keeps working. The run is safe to interrupt and to repeat (a second call continues), takes a database advisory lock so two resets
cannot overlap, and writes one `admin_audit` row (actor, `data.reset`, detail with the options and counts, time). Payments sent while it
runs may survive it, so pause the senders first. Only the `admin` role and `full` tokens may call it; merchant keys, other roles and the
`read` and `metrics` scopes get 403, no credential 401.

`allowed_origins` (list of browser origins, see *Browser access*) is a field of the merchant itself, beside `name` and `status`, in
`POST /api/merchants` and `PATCH /api/merchants/{id}`, and part of every merchant the operator API returns.

Merchant settings (`PATCH /api/merchants/{id}` with `settings`): `preferred_psps`, `max_fee_bps`, `weights {auth, cost, latency}`,
`require_3ds_over_cents`, `allowed_currencies`, `region` (data residency), `capture_method`.

Fraud rules are `{"all": [...]}`, `{"any": [...]}` or `{"field", "op", "value"}`, nested at most 3 levels and 12 conditions, with
operators `== != > >= < <= in not_in`. `GET /api/rules` lists the allowed fields.

### Provisioning from another service

For a deploy tool that connects a shop to PayBroker with no copied keys. Get a service token from the identity provider
(`client_credentials`, audience `paybroker`, scope `paybroker.admin`). The scope maps to `merchants:manage` and `payments:read` only;
`data:reset`, `settings:manage`, rules, cases, training and model routes answer 403, and so do the payment routes (`/v1`). Only a
service token (`sub_type: service`) can hold it; on a person's token the scope is ignored. A token with only `paybroker.read` cannot
change merchants (403). Make it safe to repeat:

| Call | Answer |
|---|---|
| `GET /api/merchants?name=ShopFront` | `{"items": [{id, name, status, webhook_url, allowed_origins, created_at, payments}], "defaults": {...}}`, empty `items` when none |
| `POST /api/merchants` `{"name", "allowed_origins"?, "settings"?}` | 201 the merchant (`id`, `name`, `status`, `webhook_url`, `has_webhook_secret`, `settings`, `allowed_origins`, `created_at`); **409** when a merchant of that name exists (find first, create on an empty list) |
| `GET /api/merchants/{id}` | the merchant plus `keys: [{id, kind, name, prefix, created_at, last_used_at, revoked_at}]`: `prefix` is the first 12 characters of the key (`sk_test_` and 4 more), the secret is never returned again; revoked keys stay listed with `revoked_at` |
| `PATCH /api/merchants/{id}` `{"allowed_origins": [...]}` | the merchant; the list is replaced as a whole |
| `PUT /api/merchants/{id}/webhook` `{"url"}` | `{"merchant": {...}, "signing_secret": "whsec_..."}`: a new secret every time, returned once (422 until the vault has an admin password, or when the URL is refused) |
| `POST /api/merchants/{id}/keys` `{"kind": "secret"\|"publishable", "name"?}` | 201 `{"id", "key", "kind"}`: the key is returned once; `name` (up to 60 characters) lets the caller find the keys it made |
| `DELETE /api/merchants/{id}/keys/{key_id}` | `{"revoked": key_id}`; 404 when unknown or already revoked |

The merchant name is unique among merchants created through the API (older rows may repeat a name). Each change writes an
`admin_audit` row whose actor is the service's client id: `merchant.create`, `merchant.allowed_origins` (from, to),
`merchant.webhook` (from, to, `secret_rotated`), `merchant.key_create` and `merchant.key_revoke` (merchant, key id, kind, name,
prefix). No row holds a key or signing secret.

```bash
TOKEN=$(curl -s "$IDP/oauth/token" -d grant_type=client_credentials -d client_id=ecoctl -d client_secret="$SECRET" \
        -d audience=paybroker -d scope=paybroker.admin | jq -r .access_token)
H="Authorization: Bearer $TOKEN"
MID=$(curl -s -H "$H" "$PB/api/merchants?name=ShopFront" | jq -r '.items[0].id // empty')
[ -n "$MID" ] || MID=$(curl -s -H "$H" -H 'content-type: application/json' -d '{"name":"ShopFront"}' "$PB/api/merchants" | jq -r .id)
curl -s -X PATCH -H "$H" -H 'content-type: application/json' -d '{"allowed_origins":["https://shop.example.com"]}' "$PB/api/merchants/$MID"
curl -s -X PUT -H "$H" -H 'content-type: application/json' -d '{"url":"https://shop.example.com/webhooks/paybroker"}' "$PB/api/merchants/$MID/webhook"   # signing_secret, once
curl -s -H "$H" -H 'content-type: application/json' -d '{"kind":"secret","name":"shopfront"}' "$PB/api/merchants/$MID/keys"                               # key, once
```

## MCP (`/mcp`)

Streamable HTTP; bearer tokens only (no cookies, no merchant keys), `mcp:use` required. Read-only tools:
`get_overview`, `search_payments`, `explain_payment`, `list_fraud_cases`, `list_fraud_rules`, `list_rule_suggestions`.
