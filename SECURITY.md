# Security policy

## Reporting a vulnerability
Please **do not open a public issue** for security problems. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on this repository. Include steps to reproduce and the affected version or commit.

## This is a sample
PayBroker simulates the processors, accepts **test cards only** and uses a model trained on **synthetic** data. It is not
PCI-certified and must not be used with real card data. The model below is what it enforces, and where it stops.

## Security model

**Card data**
- The vault accepts only the public processor test numbers and numbers in the reserved test BIN `999999` (the prefixes `99999901`, `99999902` and `99999903` select a simulated behaviour, nothing more). Anything else is
  refused before it is stored, so the sample cannot be used to hold a real card.
- Even test numbers are never stored: the table keeps brand, the 6-digit BIN, the last four, the expiry and a keyed
  HMAC of the number (to recognise the same card across tokens). The CVC is validated and discarded. A test asserts that no
  number or CVC appears in the payment, card, feature-store, webhook and queue tables.
- The AI provider receives derived features and reason codes only: no card data, names or e-mail addresses.

**Two kinds of credential that never cross**
- *Merchant keys* (`sk_test_`, `pk_test_`) are random, shown once and stored as SHA-256 hashes. They carry only
  `payments:write` / `vault:tokenize` and are refused on every operator route and on `/mcp`. A publishable key can only
  tokenize. A merchant sees only its own payments.
- *Operator credentials* (sessions, operator tokens, the static key, single-sign-on tokens) are refused on the payment API,
  so a leaked dashboard token cannot spend. Cookies never authorize a payment.
- Revoking a key or suspending a merchant takes effect immediately in the process that made the change and within 30 s in
  others (the key lookup is cached to keep the payment path off the key table).

**Operators**
- First run: nobody has an account; the server prints a one-time setup code to its log, and whoever presents it sets the admin
  password (salted scrypt, 12 characters minimum). Browser sessions are `HttpOnly` `SameSite` cookies (`Secure` over HTTPS) whose
  SHA-256 is stored, last 12 hours and end on password change or sign-out. Browser writes need a custom header.
- Operator tokens are shown once, hashed, revocable and scoped (`read`, `metrics`, `full`).
- **Single sign-on** (optional, `PAYBROKER_OIDC_ISSUER`): tokens are verified locally against the identity provider's JWKS
  (algorithm allow-list, issuer, audience, expiry); only this app's roles count and they map to permissions in `core.py`.
  Browser sign-in is authorization code + PKCE with state and nonce.
- Failed credentials are rate limited (10 per minute per address, in memory per process; add proxy limits too).
- The payment path authenticates through a pooled connection and answers 503 (not 500) when the database is unreachable.

**Money integrity**
- A unique `(merchant, idempotency_key)` index plus one transaction per authorization means concurrent retries produce one payment
  and a changed body under a reused key is refused. A test fires eight simultaneous requests with one key.
- State changes are checked under a row lock (`FOR UPDATE`): you cannot capture twice, refund more than captured or void a captured payment.
- Fraud decisions are made before any connector is called; a declined payment never reaches a processor.

**The AI cannot act**
- Triage output must match a strict schema (known categories and actions, confidence in 0..1); anything else is rejected and
  retried, then replaced by a rule-based fallback. It is a recommendation shown to a person.
- Suggested rules are validated by the same strict validator as hand-written ones (known fields and operators, bounded depth),
  invalid proposals are dropped, valid ones wait as `pending` and are **validated again** when a person approves them.
- The MCP server is read-only by construction (a test asserts that no tool name suggests a write).

**Secrets and outbound requests**
- AI provider keys and webhook signing secrets are encrypted with a random data key that is stored only wrapped under a key
  derived from the admin password (and optionally a platform secret). A database dump alone reveals none of them. After a
  restart the vault is locked until a sign-in or the platform secret unlocks it; webhooks wait rather than fail while locked.
- Provider and webhook URLs are validated before every call: `https` only (unless private URLs are explicitly allowed), no
  embedded credentials, no loopback, private, link-local or carrier-grade-NAT addresses, no redirects, bounded responses.
  Limitation: the host name is resolved once for the check and again by the HTTP client; run the server with egress rules if that matters.
- The environment LLM account (`LLM_BASE_URL`, `LLM_API_KEY` or `LLM_API_KEY_FILE`, `LLM_CHAT_MODEL`) is operator-set, so, like the
  other apps, its URL is trusted and skips the private-address check (the shared gateway is another sandbox's URL); it must still be an
  `https` URL without credentials or a query string, with plain `http` accepted for `localhost` only, and redirects are never followed. The key
  lives only in the process environment or the mounted file: it is not stored in the database, never returned by any API (the account is
  read-only and cannot be written or deleted through the API), never logged, and redacted from provider error messages. It needs no vault,
  so triage keeps working while the vault is locked. The model still receives derived features and reason codes only.
- Webhooks are signed `HMAC-SHA256(secret, timestamp.body)`; receivers should check the timestamp to refuse replays.

**Training data and models**
- Capture is **off by default** (an admin setting, or `PAYBROKER_CAPTURE_TRAINING_DATA` as the default). A captured row holds the 17 derived
  feature numbers, the feature-layout hash, the model version, the score, the decision and the internal payment reference, plus a label and
  its source. It never holds a card number, token, fingerprint, e-mail, IP address, device id, customer id or name; a test dumps the table
  after payments with recognisable identities and asserts none of them appears, and that the export has exactly the documented columns.
- Labels come only from outcomes a person or a network recorded (a fraud-coded chargeback, an analyst's case decision) or from an explicit
  admin action (`label-aging`, which runs only when called and labels only approved, captured, undisputed payments). Nothing is guessed in the background.
- **Retention** is enforced for everyone: rows older than the retention (default 180 days) and rows beyond the cap (default 5,000,000,
  oldest first) are purged by the worker every ten minutes in bounded batches, even while capture is off, and on demand.
- **Who may do what:** exporting and reading statistics needs `training:export`; the capture and retention settings, label-aging, purging and
  every model action need `model:manage`. Both exist only in the `admin` role and the `full` token scope; analysts, risk managers, operators,
  viewers, `read` and `metrics` tokens, merchant keys and MCP cannot use them (tests assert each). Exports are logged with the actor and row count.
  An export is still sensitive: it is derived behavioural data about customers, joinable to payments by id. Treat the file like the database.
- **Uploaded models are untrusted input.** They are validated before storage and again when loaded: the exact feature list, at most 500 trees of
  depth 8, at most 2 MB, finite numbers only (`NaN` and `Infinity` are refused), every child after its parent with one parent each (a walk always
  terminates and costs a bounded amount of work), and reference scores that the trees must reproduce. A model is data, never code: it is a JSON
  document walked by a fixed loop, nothing in it is executed. The upload route has its own 2 MB body cap (the 64 KB cap stays everywhere else).
  An upload never activates itself, versions are never overwritten, and the active one cannot be deleted. An invalid row written outside the API is refused at
  load time and the last good model keeps scoring. A model's version is shown to operators, never to merchants.
- A model trained on its own decisions inherits their bias; the README lists the safeguards and the report warns about them.

**Data reset**
- `POST /api/admin/data/reset` and `paybroker reset-data` delete transaction data irreversibly, so the permission `data:reset` belongs to the
  `admin` role and the `full` token scope only (never merchant keys, never `read`/`metrics`). The request must carry the exact phrase
  `RESET PAYBROKER DATA`; a wrong one deletes nothing. It deletes in bounded chunks under an advisory lock, never touches configuration,
  keys, users or secrets, and records who ran it, when and what it deleted in `admin_audit` (counts and options only, no secrets). The
  command line needs `--yes` and the phrase, and is for people who already have database access.

**Hardening**
- Strict `Content-Security-Policy` (no inline script or style), `nosniff`, no framing, no referrer, no caching. All text
  reaches the page through `textContent`. Bodies are size-capped and unknown fields are rejected.
- The container runs as a non-root user and holds no credentials; the database URL arrives at runtime.

## Known limits
- Simulated connectors and a test-mode 3-D Secure page that anyone with the challenge id can complete.
- In-memory rate limits and caches are per process.
- Operator and merchant actions are recorded in `admin_audit` (resets, merchants, keys, origins, webhooks, triage settings) and in the tamper-evident `audit_ledger` (every payment decision and governed change, hash-chained, verifiable: GUIDE section 18). The table owner or a superuser can still tamper with the ledger; that is detected by `audit_verify()`, not prevented, and only an externally stored head hash catches a full rewrite. Row-level security separates merchants in the database but does not bind a superuser or a `BYPASSRLS` role, and the context it relies on is a session setting the application's own role could change (GUIDE section 18.5). The identity provider keeps sign-in audit.
- The sample model is not a measure of real fraud risk, and a model uploaded by an admin is only as good as the data it came from.
- The training-capture setting and the active-model choice are cached per process for 30 seconds; another instance follows within that time.
