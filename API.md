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
| Publishable | `pk_test_` | only `POST /v1/vault/tokens` (safe in a browser) |

### `POST /v1/vault/tokens` → 201
```json
{"number": "4242424242424242", "exp_month": 12, "exp_year": 2035, "cvc": "123"}
```
Returns `{token, brand, bin, last4, exp_month, exp_year, fingerprint}`. Only the public processor test cards and numbers
starting `999999` are accepted; anything else is `422 INVALID_CARD`. The number and CVC are checked and discarded.

### `POST /v1/payments/authorize`
```json
{
  "idempotency_key": "order-1001",
  "amount": 4999, "currency": "USD",
  "capture_method": "manual",
  "payment_method": {"type": "card", "card_token": "tok_vault_card_...", "cvv_check": true},
  "customer": {"id": "cus_42", "email": "pat@example.com", "phone": "+15550100", "ip_address": "198.51.100.20", "account_age_hours": 4000},
  "telemetry": {"device_fingerprint": "dev_abc", "user_agent": "...", "checkout_duration_ms": 41000, "pasted_card_number": false, "ip_country": "US"},
  "shipping_address": {"line1": "...", "city": "...", "state": "...", "postal_code": "...", "country": "US"}
}
```
`idempotency_key` (8 to 128 characters) is required. **A retry with the same key and the same body returns the first result;
the same key with a different body is `422 IDEMPOTENCY_KEY_REUSED`.** Concurrent retries create one payment.
`capture_method` is `manual` (authorize now, capture later) or `automatic`; the merchant's default applies when omitted.
`account_age_hours` is how old the customer's account is at your site; when absent an established account is assumed.

| Status | Body | Meaning |
|---|---|---|
| 200 | the payment (`status` `AUTHORIZED` or `CAPTURED`) | approved |
| 200 | `status: REQUIRES_ACTION` and `action_required: {type: "3DS_CHALLENGE", redirect_url}` | send the customer to `redirect_url`; the result arrives by webhook |
| 402 | `error.code: CARD_DECLINED`, `decline_code` | the issuer declined (final, not retried on another connector) |
| 403 | `error.code: FRAUD_REJECTED`, `fraud_assessment` | declined by the fraud engine; the connector was never called |
| 403 | `error.code: MERCHANT_MISMATCH` | `merchant_id` in the body is not this key's merchant |
| 422 | `IDEMPOTENCY_KEY_REUSED`, `CURRENCY_NOT_ALLOWED`, `INVALID_CARD_TOKEN`, `NO_ROUTE`, validation errors | fix the request |
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
`GET /3ds/{challenge_id}` is the hosted page. `GET /api/3ds/{id}` and `POST /api/3ds/{id}/complete {"outcome": "succeeded"|"failed"}`
are what it calls. **Test mode only**: anyone holding the unguessable challenge id can complete it, because a real issuer's
result would come from the card network instead. A challenge lasts 30 minutes.

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
delivery is retried after 5 s, 30 s, 5 min, 30 min, 2 h and 6 h (six attempts), and each attempt is visible in the dashboard.
Deliveries are at least once: use `id` to deduplicate.

## Operator API (`/api`)

Permissions (what a role or token scope grants): `payments:read`, `cases:review`, `rules:manage`, `merchants:manage`,
`settings:manage`, `mcp:use`, `metrics:scrape`. Token scopes: `read` (`payments:read`, `mcp:use`), `metrics`, `full`.
Browser sessions send a custom `X-PayBroker-CSRF` header on writes; bearer-token calls don't need it.

| Call | Needs | Notes |
|---|---|---|
| `GET /api/session` | none | what the UI should show; the caller's roles and permissions |
| `GET /api/overview?days=` | `payments:read` | counts, approval rate, volume, latency, score histogram, connectors, queue |
| `GET /api/payments?q=&status=&decision=&min_score=&merchant_id=&limit=&offset=` | `payments:read` | |
| `GET /api/payments/{id}` | `payments:read` | the request, rules fired, score and reasons, features, timings and routing attempts, AI triage, case, 3-D Secure, webhooks, chargebacks |
| `POST /api/payments/{id}/chargeback` `{amount?, reason}` | `cases:review` | updates the feature store |
| `GET /api/cases?status=open\|resolved\|all` | `payments:read` | with the AI summary and recommendation |
| `POST /api/cases/{id}/resolve` `{decision, note}` | `cases:review` | `PERMANENT_BLOCK`, `ALLOW_WITH_KYC`, `DISMISS_ALERT` |
| `GET /api/rules`, `POST /api/rules`, `PATCH /api/rules/{id}` `{enabled}`, `DELETE /api/rules/{id}` | read / `rules:manage` | the definition is validated; the fields and operators are returned by the GET |
| `GET /api/suggestions`, `POST /api/suggestions/run`, `POST /api/suggestions/{id}/decide` `{approve}` | read / `rules:manage` | nothing activates until approved |
| `GET/POST/DELETE /api/blacklist` | read / `cases:review` | kinds: `ip`, `device`, `card_fingerprint`, `bin`, `email_domain` |
| `GET/POST /api/merchants`, `GET/PATCH /api/merchants/{id}`, `PUT .../webhook`, `POST/DELETE .../keys` | read / `merchants:manage` | keys and the webhook secret are returned once |
| `GET /api/connectors`, `PATCH /api/connectors/{name}` `{enabled}` | read / `merchants:manage` | |
| `GET /api/webhook-events` | `payments:read` | |
| `GET/POST/PUT/DELETE /api/accounts`, `PUT /api/triage-account` | `settings:manage` | AI accounts; keys are never returned |
| `GET/POST/DELETE /api/tokens` | `settings:manage` | operator tokens, shown once |
| `POST /api/demo/traffic` `{count, seed}` | `settings:manage` | fictional payments through the real pipeline |
| `GET /metrics` | `metrics:scrape` | Prometheus |

Merchant settings (`PATCH /api/merchants/{id}` with `settings`): `preferred_psps`, `max_fee_bps`, `weights {auth, cost, latency}`,
`require_3ds_over_cents`, `allowed_currencies`, `region` (data residency), `capture_method`.

Fraud rules are `{"all": [...]}`, `{"any": [...]}` or `{"field", "op", "value"}`, nested at most 3 levels and 12 conditions, with
operators `== != > >= < <= in not_in`. `GET /api/rules` lists the allowed fields.

## MCP (`/mcp`)

Streamable HTTP; bearer tokens only (no cookies, no merchant keys), `mcp:use` required. Read-only tools:
`get_overview`, `search_payments`, `explain_payment`, `list_fraud_cases`, `list_fraud_rules`, `list_rule_suggestions`.
