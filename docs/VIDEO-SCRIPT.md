# PayBroker: tutorial video script

## Title

**PayBroker on Postgres and AgentPG: fraud checks, routing and data protection, shown at the database**

## Audience

Customers and internal teams (customer video 1 of 5, problem first: *I run a marketplace; card fraud and false declines cost me money, and I will not ship payment data to three vendors*) who want to see
what a payment broker does, how the fraud checks reach a decision, and which parts are done by the database platform (Postgres on AgentPG with EDB extensions), without needing a payments background. Viewers who may also be developers (they will want the curl commands) and analysts (they will want the case screen). No jargon is used
without a one-sentence explanation in the narration.

## Goal

By the end the viewer can:

1. Say the problem (fraud, false declines, data sent to several vendors) and which parts are PLATFORM (Postgres on AgentPG with its EDB extensions), REFERENCE SCAFFOLDING (sign-in, model gateway, secret vault: swappable) and SAMPLE APP (PayBroker).
2. See the core flow: create a merchant, authorize a payment, 3-D Secure, the processor cascade, signed webhooks, a flagged payment and its case.
3. See the database doing the work: pgvector nearest past fraud cases (HNSW, cosine), the fuzzystrmatch `NAME_MISMATCH` signal, masked views for a read-only role, and the Database insights page (local: `pg_stat_statements`; on AgentPG: a clearly labelled capture of the real EDB extension list and `agentpg recommend` output).
4. Find the three commands (`ecoctl apply`, or `docker compose` locally) and the guide (section 17).

Length: about 8 minutes (about 1000 spoken words, plus pauses for on-screen actions). Voices: Kokoro, US accent, six voices alternating male and female (see `video/README.md`).

**Truthfulness rules.** Everything on screen is a real run against the local throwaway Postgres, except the two terminal cards labelled *CAPTURED from our live AgentPG fleet*
(`videos/live-captures/extensions-installed.txt` and `ecoctl-insights.txt`, the PayBroker blocks; the instance id is dropped). Local Postgres cannot run `edb_stat_monitor`, `edb_pg_tuner` or `query_advisor`:
the video says so. The `a1p_readonly` group role is created in the throwaway cluster by the recorder to mimic what AgentPG provides (`lib.ts`).

## Prerequisites and seed data (reproducible locally)

Everything below runs on one laptop with Docker, [uv](https://docs.astral.sh/uv/), `curl`, `jq` and Python 3. Nothing touches a live service or the
fleet. Do **not** record against a deployed sandbox: its URL changes on every recreate.

### 1. Start PayBroker on a throwaway database

From the `paybroker` directory (README, *Quick start*):

```bash
cp .env.example .env                       # then set POSTGRES_PASSWORD to a throwaway value; .env is git-ignored
docker compose down -v                     # a clean start: removes the old database volume
docker compose up -d db
uv venv && uv pip install -e ".[dev]"
set -a; . ./.env; set +a
export PAYBROKER_PUBLIC_URL=http://localhost:8000
export PAYBROKER_ALLOW_PRIVATE_URLS=true   # the webhook receiver below is on 127.0.0.1; this is local testing only
.venv/bin/paybroker migrate
.venv/bin/paybroker serve                  # keep this terminal visible: it prints the one-time setup code
```

Do not set `LLM_BASE_URL`. With no AI account the built-in rule-based triage writes the case summary, which is what the script narrates. (If you prefer to
show a model, point `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_CHAT_MODEL` at any OpenAI-compatible endpoint you control, and change the scene 7 narration: the
AI box then says `model: ...` instead of `rule-based fallback`.)

Leave the simulated connector latency on (the default) so the routing numbers look real.

### 2. Two helper servers (second and third terminal)

The shop's page and its webhook receiver are stand-ins on `localhost`:

```bash
# shop-return: where the customer lands after 3-D Secure (scene 6); it prints the signed link it was opened with
mkdir -p /tmp/shop && printf '<h1>Back at Acme Shop</h1><p>Thanks. The webhook confirms the payment.</p><p id=u></p><script>u.textContent = location.pathname + location.search</script>' > /tmp/shop/return.html
python3 -m http.server 3000 --directory /tmp/shop
```

```python
# receiver.py: the shop's webhook endpoint (scene 8). Start it AFTER scene 3, with the signing secret PayBroker shows once:
#   WHSEC=whsec_... python3 receiver.py
import hashlib, hmac, json, os, time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ["WHSEC"].encode()

class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))              # the RAW body, not re-serialized JSON
        ts = self.headers["X-Timestamp"]
        want = hmac.new(SECRET, f"{ts}.{raw.decode()}".encode(), hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(want, self.headers.get("X-Signature", "")) and abs(time.time() - int(ts)) < 300
        print(("signature ok   " if ok else "SIGNATURE BAD  ") + json.loads(raw)["type"], flush=True)
        self.send_response(200 if ok else 400)
        self.end_headers()
    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", 4010), Hook).serve_forever()
```

### 3. Seed data

| What | How | Used in |
|---|---|---|
| Admin account | Open http://localhost:8000, enter the setup code from the server log, choose a throwaway admin password of 12 or more characters | scene 2 |
| Demo traffic: 120 fictional payments, seed 7 | The *Generate 120 demo payments* button on the empty Overview (this is scene 2). Equivalent from a terminal: `.venv/bin/paybroker demo -n 120 --seed 7` | scenes 2 and 7 |
| Merchant "Acme Shop", two keys, origin `http://localhost:3000`, webhook `http://127.0.0.1:4010/hook` | Created **on camera** in scene 3 | scenes 4 to 12 |
| Test cards | `4242424242424242` (approves, scene 4) and `5555555555554444` (approves; the risky payment) are public numbers from the README table that the demo traffic never uses. For scene 6 use the reserved-range numbers `9999990100000070` (issuer declines), `9999990200000079` (always 3-D Secure) and `9999990300000078` (first connector fails): they behave exactly like `4000000000000002`, `4000002500003155` and `4000000000000119`, but the demo traffic of scene 2 sends those three public numbers many times and a shared card trips the per-card velocity rule (a `403 FRAUD_REJECTED` with `HARD_VELOCITY_CARD` instead of the behaviour you want to show) | scenes 4 to 7 |
| A risky payment | The payload in scene 7: a one-hour-old account, a 0.9 s checkout, a pasted card, shipping to `NG`, amount 450000. Checked on an empty database in this script's own order (admin, 120 demo payments, merchant, scenes 4 and 6, then this call): `403 FRAUD_REJECTED`, score 976, reasons `NEW_ACCOUNT_HIGH_AMOUNT`, `FOREIGN_IP_LARGE_BASKET`, `FAST_CHECKOUT`, `NEW_ACCOUNT`, `LARGE_AMOUNT`, `PASTED_CARD_NUMBER`. The customer, device and card are used by nothing else, so earlier traffic does not change it. The ecosystem's end-to-end test sends the same payload | scene 7 |

The demo traffic is seeded (same customers and amounts for the same seed), but connector latency and a few outcomes involve randomness, so exact
counts on the Overview vary between takes (one rehearsal: 75 authorized, 7 verify, 25 fraud-rejected, 13 declined). Do not narrate exact numbers from the Overview other than "about a hundred and twenty payments".

Shell variables used in the terminal scenes (set once, off camera, after the keys exist; the keys are shown once in scene 3, so copy them then):

```bash
export PB=http://localhost:8000
export PK=pk_test_...     # the publishable key from scene 3
export SK=sk_test_...     # the secret key from scene 3
tok() { curl -s -X POST $PB/v1/vault/tokens -H "Authorization: Bearer $PK" -H "Content-Type: application/json" \
        -d "{\"number\":\"$1\",\"exp_month\":12,\"exp_year\":2035,\"cvc\":\"123\"}" | jq -r .token; }
auth() { curl -s -X POST $PB/v1/payments/authorize -H "Authorization: Bearer $SK" -H "Content-Type: application/json" -d "$1"; }
```

Keep the keys out of the recording: show them for one second in scene 3 using throwaway values from a database that is deleted afterwards, or blur them in
post.

## Scenes

Timestamps are approximate. Narration is plain speech at about 140 words a minute; pauses for the actions are included in each scene's time. On-screen
callouts are short captions lower-third.

| # | Time | ON-SCREEN ACTION | NARRATION | ON-SCREEN CALLOUTS |
|---|---|---|---|---|
| 1 | 0:00 to 0:48 | Card: the problem (three red lines: fraud, false declines, data to three vendors). Then the labelled diagram: PLATFORM (Postgres on AgentPG, EDB), SAMPLE APP (PayBroker), three REFERENCE SCAFFOLDING boxes (sign-in, model gateway, secret vault), legend. Each scaffolding box lights up as it is mentioned. | "I run a marketplace. Card fraud and false declines cost me money. And I will not ship payment data to three vendors. So here is how Postgres on AgentPG does it. That is the platform part, with its EDB extensions. PayBroker is the sample app on top. Three things here are scaffolding you can swap: sign-in, the model gateway, and the secret vault. Sign-in speaks standard OpenID Connect. Any provider works, as long as it sends the roles our apps expect. The gateway speaks an OpenAI-compatible API. Both are set by configuration. The vault is a small module of the shared kit. It is replaceable, but not by a setting. It uses only test cards, so nothing here moves real money." | Problem, platform, sample app, swappable scaffolding, test cards only. |
| 2 | 0:49 to 1:22 | Setup page (one-time code, admin password), empty Overview, **Generate 120 demo payments**, score bands. | "First, setup. I paste the one-time code, and choose an admin password. Now the dashboard. One click loads a hundred and twenty fictional payments, honest ones and fraud attempts. I click, and each payment is scored. [pause 7] Done. Under three hundred, a payment is approved. Up to six ninety-nine, the customer must verify. Seven hundred or more is declined." | One-time setup code from the server log; 120 fictional payments; score bands. |
| 3 | 1:23 to 1:47 | **Merchants**, **New merchant** `Acme Shop`, secret key, publishable key, allowed origin, webhook URL; keys and secret blurred then masked. | "Next, a shop. I create a merchant called Acme Shop, and then two keys. The secret key stays on the shop's server. Each key is shown once, so it is hidden right after. The publishable key can only swap a card for a token. So it is safe in a web page. Then the allowed browser origin, and a webhook address. The webhook signing secret appears once, too." | Secret key: server only; Shown once; Publishable key: browser safe; Allowed origin; Signing secret. |
| 4 | 1:48 to 2:24 | Terminal: tokenize, authorize `order-1001`, repeat with the same idempotency key. | "Now a payment. The customer's browser trades the test card for a token. So the shop's server never sees the card number. The server then authorizes forty-nine ninety-nine dollars, with an idempotency key. [pause 1.5] The answer says authorized, with a fraud score, and the processor that was used. Now suppose the network drops, and the shop sends the same request again. [pause 1.5] It gets the same payment back. Look at the payment id. It is identical. Never a second charge." | Token, not card number; Same idempotency key = same payment; the payment id. |
| 5 | 2:25 to 2:38 | Payments: open the payment, score, Timing, Routing. | "In the dashboard, the payment opens with its score, out of one thousand. Below, the checks that ran, and the time spent in each step. And last, the processor that handled it. Every decision is explained." | Score 0-1000; Rules + model + routing, explained. |
| 6 | 2:39 to 3:27 | Terminal: declined card (402), 3-D Secure card (open the page, approve, signed return), cascade card (routing error then approved). | "Now, test cards that trigger specific behaviours. The first one makes the customer's bank decline. [pause 2.0] Four oh two, card declined. The second card demands three-D Secure, where the customer proves who they are. [pause 2.0] The answer holds a page to send the customer to. I open it, and approve. [pause 1.5] The customer is back at the shop, with a signed link. The third card makes the first processor fail. [pause 2.0] PayBroker tries the next processor. So the payment still succeeds. The routing shows an error, then approved." | 402: bank declined; 3-D Secure; Signed link; Cascade. |
| 7 | 3:28 to 3:57 | Terminal: the risky payment (403). Cases: open the case, the AI triage box, **Permanent block**, **Block**. | "Now, the payment my marketplace fears. A brand-new account, a very fast checkout, a pasted card number, and a large basket. [pause 2.0] Four oh three, rejected. No processor ever saw it. A case opens for an analyst. The summary comes from the built-in fallback, because no language model is configured. The AI only advises. A person decides. I choose permanent block." | 403: rejected before any processor; AI advises, a person decides; Blocked. |
| 8 | 3:58 to 4:17 | Merchant window (webhook URL), terminal with the receiver output, the recent webhook events table. | "The shop also needs to hear about every payment. PayBroker sends it signed events, on a webhook. Here is the shop's receiver. It checks every signature, and says signature okay. On the merchant, every delivery is listed. If the shop is down, PayBroker retries, with growing delays." | Signed events: HMAC-SHA256; Retries with back-off. |
| 9 | 4:18 to 5:00 | UNDER THE HOOD 1. Cases view, then a terminal with PLATFORM and SAMPLE APP tags: `vector_dims` of a real feature vector, the real `fa_vec_idx` HNSW definition from `pg_indexes`, the nearest-cases query of `triage.py` run with `psql` variables. | "Now, under the hood. Here is a problem the analyst has. Have we seen payments like this one before? Postgres answers it, with pgvector. Every payment is stored as a list of seventeen numbers. This index finds the nearest neighbours fast. It is HNSW, with cosine distance. [pause 1.5] And here is the real query from the app. It sorts past payments by distance, and keeps the five nearest flagged ones. [pause 2.0] Each row shows how close it is, and its risk category. Those five go to the triage. No second system, and no copy of the data." | Have we seen payments like this one before?; Nearest flagged cases go to the AI triage. |
| 10 | 5:01 to 5:44 | UNDER THE HOOD 2. `levenshtein`, `dmetaphone` and `soundex` calls; an authorize call with cardholder `Jon Smith` and account `Peter Wong` (amount 34900): `NAME_MISMATCH` and 3-D Secure; the payment page shows the reason. | "Next problem. Fraudsters pay with cards that are not theirs. So the cardholder name should look like the account name. Postgres can compare them, with the fuzzystrmatch extension. Here are two real calls. Smith and Smyth are one letter apart, and they sound the same. [pause 1.5] Now a payment. The card says Jon Smith. The account says Peter Wong. The basket is large. [pause 2.0] The reason code is name mismatch. And the customer is asked to verify. In the dashboard, the same reason code appears, with its explanation." | Does the cardholder name match the account name?; NAME_MISMATCH: reason code. |
| 11 | 5:45 to 6:03 | Rules tab: four built-in rules, `name_mismatch_large_basket` highlighted, the rule editor. | "That step came from a rule. There are four built-in rules now. The new one asks for three-D Secure when the names do not match, on a large basket. Rules are plain data. And the editor only accepts known fields, so a typo is refused." | Four built-in rules. |
| 12 | 6:04 to 6:44 | UNDER THE HOOD 3. Terminal: the owner reads `transaction` (real ids), the read-only login role (member of `a1p_readonly`) is denied on `transaction`, reads `transaction_masked` (hashes, masked address), and a write is refused. | "Next problem. I do not want everyone who reads the database to see customer data. Here is the app's own connection. It sees the real customer id, and the real address. [pause 1.5] Now the read-only role. It is the kind AgentPG makes for read-only users, and for its restricted MCP. The same query on the table is denied. [pause 1.5] But the masked view is allowed. Identifiers become short hashes. The address loses its last number. [pause 1.5] And a write is refused. The database enforces all of it." | Who can read the customer data?; Read-only role: like agentpg db-users create --permission read. |
| 13 | 6:45 to 7:36 | UNDER THE HOOD 4. Database insights tab on local Postgres (`pg_stat_statements` only); terminal cards labelled **CAPTURED from our live AgentPG fleet**: `agentpg extensions list` and `ecoctl insights` (PayBroker blocks). | "Last problem. How is the database doing, and what should I tune? That is the Database insights tab. On my laptop, plain Postgres, it shows the slowest statements, from pg_stat_statements. That is all a laptop can show. On AgentPG, the same page also reads edb_stat_monitor, edb_pg_tuner, and query_advisor, when they are installed. I cannot run them here. So this is real output, captured from our live AgentPG fleet. [pause 1.0] The PayBroker instance has edb_stat_monitor and query_advisor, next to pgvector and fuzzystrmatch. [pause 1.5] Its advisor suggested two indexes, and one statistics object. It only suggests. A person decides." | Local Postgres: pg_stat_statements only; On AgentPG: + edb_stat_monitor, edb_pg_tuner, query_advisor. |
| 14 | 7:37 to 8:03 | The labelled diagram again, with the three commands (`ecoctl apply --only paybroker --execute`, `docker compose up -d db`, `paybroker migrate && paybroker serve`) and `docs/GUIDE.md` section 17; the colours are highlighted as the legend is repeated. | "Try it yourself. On AgentPG, one command deploys it. Or run it on your laptop, with Docker. Remember the three colours. Platform is Postgres on AgentPG. Scaffolding is swappable. PayBroker is the sample. The guide is in the docs folder. Section seventeen lists every EDB feature this app uses. Next, see how ShopFront uses PayBroker." | (none: the diagram legend) |

(Times are approximate; the build prints the real ones.)

### Commands used on screen

Scene 4 (the authorize call, key `order-1001`; run it twice):

```bash
auth '{"idempotency_key":"order-1001","amount":4999,"currency":"USD",
  "payment_method":{"type":"card","card_token":"'"$TOKEN"'"},
  "customer":{"id":"cus_42","email":"pat@example.com","ip_address":"198.51.100.20","account_age_hours":4000},
  "telemetry":{"device_fingerprint":"dev_abc","checkout_duration_ms":41000,"pasted_card_number":false},
  "shipping_address":{"country":"US"}}' | jq
```

Scene 6 (the three variations; same body, other `idempotency_key` and token; the 3-D Secure one adds `return_url`):

```bash
auth '{"idempotency_key":"order-1002","amount":4999,"currency":"USD","payment_method":{"type":"card","card_token":"'"$(tok 9999990100000070)"'"},
  "customer":{"id":"cus_43","email":"lee@example.com","ip_address":"198.51.100.21","account_age_hours":4000}}' | jq
auth '{"idempotency_key":"order-1003","amount":4999,"currency":"USD","payment_method":{"type":"card","card_token":"'"$(tok 9999990200000079)"'"},
  "customer":{"id":"cus_44","email":"kim@example.com","ip_address":"198.51.100.22","account_age_hours":4000},
  "return_url":"http://localhost:3000/return.html"}' | jq
auth '{"idempotency_key":"order-1004","amount":4999,"currency":"USD","payment_method":{"type":"card","card_token":"'"$(tok 9999990300000078)"'"},
  "customer":{"id":"cus_45","email":"sam@example.com","ip_address":"198.51.100.23","account_age_hours":4000}}' | jq
```

Scene 7 (the risky payment):

```bash
auth '{"idempotency_key":"order-risky-1","amount":450000,"currency":"USD","payment_method":{"type":"card","card_token":"'"$(tok 5555555555554444)"'"},
  "customer":{"id":"cus_risky","email":"x@example.com","ip_address":"203.0.113.9","account_age_hours":1},
  "telemetry":{"device_fingerprint":"dev_risky_z","checkout_duration_ms":900,"pasted_card_number":true},
  "shipping_address":{"country":"NG"}}' | jq
```

Scenes 9, 10 and 12 (the database; the recorder runs exactly the text shown, with `$DB` and `$RO` standing for the local connection strings, which hold no password; `$VEC` and `$TX` are the risky payment's feature vector and transaction id):

```bash
psql "$DB" -c "SELECT vector_dims(feature_vector) AS dims, left(feature_vector::text, 54) || '...' AS feature_vector FROM fraud_assessment LIMIT 1"
psql "$DB" -At -c "SELECT indexdef FROM pg_indexes WHERE indexname = 'fa_vec_idx'"      # CREATE INDEX fa_vec_idx ... USING hnsw (feature_vector vector_cosine_ops)
psql "$DB" -v v="$VEC" -v tx="$TX" <<'SQL'                                              # the query of src/paybroker/triage.py (similar_cases), parameters as psql variables
SELECT t.payment_id, fa.tier2_ml_score AS score, fc.analyst_decision, fa.tier3_llm_analysis->>'risk_category' AS category,
       round((1 - (fa.feature_vector <=> :'v'))::numeric, 3) AS similarity
FROM fraud_assessment fa JOIN transaction t ON t.id = fa.transaction_id
LEFT JOIN fraud_case fc ON fc.transaction_id = fa.transaction_id
WHERE fa.transaction_id <> :'tx' AND fa.tier2_ml_score >= 500 AND fa.feature_vector IS NOT NULL
ORDER BY fa.feature_vector <=> :'v' LIMIT 5;
SQL
psql "$DB" -c "SELECT levenshtein('smith', 'smyth') AS edits, dmetaphone('smith') = dmetaphone('smyth') AS sound_alike, soundex('john') = soundex('jon') AS soundex_same"
# scene 10: authorize with payment_method.cardholder_name "Jon Smith", customer.name "Peter Wong", amount 34900 -> NAME_MISMATCH + REQUIRES_ACTION
psql "$DB" -c "SELECT payment_id, customer_id, request->>'ip' AS ip FROM transaction ORDER BY created_at DESC LIMIT 3"     # the owner
psql "$RO" -c "<same, FROM transaction>"          # login role reporting_reader (member of a1p_readonly): permission denied
psql "$RO" -c "<same, FROM transaction_masked>"   # 12-hex hashes, address with the last octet hidden
psql "$RO" -c "UPDATE transaction_masked SET fraud_score = 0"                            # refused
```

Scene 13 is the Database insights tab of the running app, then two cards copied from `videos/live-captures/` (PayBroker blocks), under the banner *CAPTURED from our live AgentPG fleet: not run in this video*.

### Things to check before pressing record

- Start every take from an empty database (`docker compose down -v`): the risky payment is deterministic only on a clean database in the order of this script. A second take on the same
  database reuses idempotency keys, the blocklist already holds the card from the first take (the answer is still `403`, with `BLACK_LISTED_CARD` among the reasons), and the demo button is gone.
- Use the scene 6 cards from the seed table. The public numbers of the README table are also used by the demo traffic, so right after scene 2 they can answer `403` (`HARD_VELOCITY_CARD`).
- The 3-D Secure card returns `200 REQUIRES_ACTION` only because its issuer behaviour forces a challenge; it does not depend on the score.
- The signed return only happens because Acme Shop has a webhook secret and `http://localhost:3000` is in its allowed origins. Both are done in scene 3.
- The server terminal must show no vault banner problems: the vault unlocks when you create the admin password. If you restart `paybroker serve` mid-recording, sign in
  again (the vault is locked until a sign-in), otherwise webhooks wait.

## Call to action (as spoken in scene 14)

Recap: one API; smart routing across processors with a cascade on failure; three layers of fraud checks (rules, a model, AI triage) that each leave reasons you can read; a person who
makes every decision; signed webhooks and a read-only MCP server.

Call to action: run it locally with `docker compose up -d db`, `paybroker migrate`, `paybroker serve`; read `docs/GUIDE.md` for configuration, security and operations; then watch the ShopFront
video to see a real web shop paying through PayBroker.

## Recording notes

### Capture settings

Browser window 1920 by 1080 at 100 % zoom (dark or light theme, but the same for the whole video); terminal font at least 18 pt; record at 1080p, 30 fps; no cursor trails. Keep the server
terminal out of frame except for the setup code in scene 1. Narration can be recorded afterwards over a silent screen capture; the scene times above leave room for it.

### What the Playwright recorder does

(The selector table below dates from the app-first cut; the scene numbers there are the old ones. The current scene list is the table above; new selectors: `#view-rules tr` with `name_mismatch_large_basket`, heading *Database insights*, `detail.getByText('NAME_MISMATCH')`.)

`docs/video/record.spec.ts` (with `playwright.config.ts` and `package.json` beside it) performs every on-screen action of the table above in the installed Google Chrome, viewport and video 1920 by 1080,
a pause of 0.5 to 3.5 seconds after each step, a visible cursor, a pulsing outline on the element being discussed and the callouts as lower thirds (each at least three seconds). It writes
`docs/video/out/paybroker-tutorial.webm` (git-ignored). The header comment of the file lists the environment. In short:

```bash
docker compose down -v && docker compose up -d db && .venv/bin/paybroker migrate     # a database that has never been set up
PAYBROKER_PUBLIC_URL=http://localhost:8000 PAYBROKER_ALLOW_PRIVATE_URLS=true .venv/bin/paybroker serve > /tmp/pb.log 2>&1 &
cd docs/video && npm install && npx playwright install ffmpeg      # once; the ffmpeg download is what encodes the video
PB_URL=http://localhost:8000 PB_LOG=/tmp/pb.log PB_ADMIN_PASSWORD='<12+ characters, throwaway>' npx playwright test
```

It starts its own stand-ins (the shop's return page and an on-screen terminal on port 3000, the signed-webhook receiver on 4010), so the two helper servers above are not needed for it. The terminal scenes (4, 6, 7, 8, 9, 10, 12, 13) are drawn on that
terminal page: the recorder types the command, makes the real call from Node and prints the real answer. The recorder asserts what the script claims (for example `403` with `NEW_ACCOUNT`, `402 CARD_DECLINED`,
`REQUIRES_ACTION` then the signed return, the routing list showing `error` then `approved`, `signature ok` for the three event types, `delivered` events), so a take that drifts from the script fails instead of recording something false.
Keys and the signing secret are shown for about a second and then masked in the page. `PACE=0.3` makes a quick rehearsal, `REC_VIDEO=off` skips encoding, `REC_SHOTS=<dir>` saves a screenshot at every callout,
(The optional `BLOCKLIST_BEAT` of the earlier cut was removed with the rewrite.)

The selectors it uses (all verified against this version):

| Step | Selectors and labels |
|---|---|
| Scene 1 | heading *Set up PayBroker*; `#setup-code`, `#setup-pw`, `#setup-pw2`; button *Create admin password*; heading *Overview* |
| Scene 2 | button *Generate 120 demo payments*; the `#toast` text `Sent 120 demo payments`; `a.stat` *approved*; heading *Fraud score distribution*; region *Payments by connector* |
| Scene 3 | `#merchants`; *New merchant*; label *Name*; *Create* (exact); `#key-kind`, `#key-name`, *Create key*; key in `#detail .codewrap code` (last); label *Allowed browser origins*, *Save origins*, toast `Allowed origins saved`; `#wh-url`, *Save* (exact), secret in the last `.codewrap code` |
| Scene 5 | `#pay-q`; `#pay-body tr` with the id; `#detail` text `Fraud engine: N / 1000`; heading *Timing*; summary *Features the model saw*; heading *Routing*; `#detail-close` |
| Scene 6 | answer fields `status` and `action_required.redirect_url` (`http://.../3ds/<id>`); heading *Verify this payment*; button *Approve (verification succeeds)*; return to `/return.html?payment_id=...&signature=...`; the routing list is `#detail ol.timeline` |
| Scene 7 | `#case-list .case` with the payment id and `NEW_ACCOUNT`; the id button opens `#detail` with the *rule-based fallback* badge in `.ai-box`; label *Decision* = `PERMANENT_BLOCK`; *Resolve*; `alertdialog` button *Block*; toast `Resolved. Blocked:`; `#rules`, region *Blocklist* |
| Scene 8 | heading *Add a rule*; `#rule-name`, `#rule-action` = `challenge`, `#rule-def`, *Add rule*; heading *Suggested rules* |
| Scene 9 | region *Recent webhook events* in the Acme Shop window, rows `delivered` |
| Scene 10 | `#connect`, the last `#view-connect pre.code` (the MCP command) |

Sign-in is not needed: the setup in scene 1 signs in. If the recorder is changed to start later, post `{"password": ...}` to `/api/login` with the header `X-PayBroker-CSRF: 1` (the ecosystem's e2e harness does this with `loginViaApi`).
Never put the password in a file, and delete the throwaway database (`docker compose down -v`) after recording.

### Editing

Cut the wait after the demo-traffic click (scene 2) and the receiver restart (scene 3). Blur or mask every key and secret. Put the callouts in the lower third for at least three seconds each. End on the closing
card for five seconds. Subtitles: use the narration column as the transcript.

### Accuracy check list (for the reviewer)

- Score bands: under 300 approve, 300 to 699 verify with 3-D Secure, 700 and above decline and open a case.
- The cascade is shown by the card `9999990300000078` (it behaves like `4000000000000119`): the first connector answers 5xx, the next approves, within an 800 ms budget.
- The AI box reads *rule-based fallback* only when no AI account is configured (or a throttle applied); with the shared LLM gateway configured it names the model.
- The webhook retry description is deliberately general ("growing delays"). The exact schedule is 30 s, 5 min, 30 min, 2 h, 6 h and 15 h between seven attempts (about 23.6 hours); see `docs/GUIDE.md`, section 7.7.
- Never say the sample is PCI-certified or suitable for real card data.
