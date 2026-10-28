# PayBroker: tutorial video script

## Title

**PayBroker in six minutes: one payment API, smart routing and fraud checks you can explain**

## Audience

Customers and internal teams who want to see what a payment broker does and how the fraud checks reach a decision, without needing a payments
background. Viewers who may also be developers (they will want the curl commands) and analysts (they will want the case screen). No jargon is used
without a one-sentence explanation in the narration.

## Goal

By the end the viewer can:

1. Say what PayBroker does (one API in front of several processors, with routing and three layers of fraud checks).
2. Create a merchant, its keys, a browser origin and a webhook.
3. Send a test payment, read the decision and see why it was made.
4. Handle a flagged payment as an analyst, knowing the AI only advises.
5. Find the documentation and run it locally.

Length: about 6 minutes 30 seconds (about 740 spoken words at 140 words a minute, plus pauses for on-screen actions).

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
# receiver.py: the shop's webhook endpoint (scene 9). Start it AFTER scene 3, with the signing secret PayBroker shows once:
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
| Admin account | Open http://localhost:8000, enter the setup code from the server log, choose a throwaway admin password of 12 or more characters | scene 1 |
| Demo traffic: 120 fictional payments, seed 7 | The *Generate 120 demo payments* button on the empty Overview (this is scene 2). Equivalent from a terminal: `.venv/bin/paybroker demo -n 120 --seed 7` | scenes 2 and 7 |
| Merchant "Acme Shop", two keys, origin `http://localhost:3000`, webhook `http://127.0.0.1:4010/hook` | Created **on camera** in scene 3 | scenes 4 to 9 |
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
| 1 | 0:00 to 0:28 | Title card (5 s). Cut to a browser at http://localhost:8000 showing *Set up PayBroker*. Paste the setup code from the server terminal into **Setup code**, type the admin password in both password fields, click **Create admin password**. Land on the empty **Overview**. | "When a shop takes a card payment, three questions come up. Is this payment safe? Which processor should handle it? And what if that processor fails? PayBroker answers all three, behind one API. It is a sample application, built on Postgres, and running on AgentPG. It uses only test cards and simulated processors. So nothing here moves real money. Let's set it up. The server printed a one-time code, so I paste it here. Then I choose an admin password, and create the account." | "PayBroker: one API, many processors" then "Postgres + pgvector, test cards only" |
| 2 | 0:28 to 1:05 | On the empty Overview, click **Generate 120 demo payments**, wait for the toast *Sent 120 demo payments* (cut the wait). Hover the approval rate, then the score chart, then the connector table. | "This is the dashboard, and right now it is empty. One click loads a hundred and twenty fictional payments. Ordinary customers, bank declines, and three-D Secure checks. Plus card testing, and account takeover. I click, and the payments are scored one by one. [pause 7] Done. The overview shows how many were approved, and how long each payment took. Payments are spread across the simulated processors. A score weighs approval rate, fee, and speed. This chart is the fraud score. Under three hundred, a payment is approved. From three hundred to six ninety-nine, the customer must verify. Seven hundred or more is declined." | "120 fictional payments" ; on the chart: "under 300 approve | 300-699 verify | 700+ decline" |
| 3 | 1:05 to 1:55 | Click **Merchants**, **New merchant**, type `Acme Shop` in **Name**, click **Create**. In the window: choose **Secret key (server)**, name `checkout server`, **Create key**; show the key for one second, then **Publishable key (browser)**, name `checkout page`, **Create key**. In **Allowed origins** type `http://localhost:3000`, click **Save origins**. In **URL** type `http://127.0.0.1:4010/hook`, click **Save**: the signing secret appears. Copy it. (Cut away: start `receiver.py` with `WHSEC` set.) | "Now let's connect a shop. A merchant is a business that sends payments. I open Merchants, and create one called Acme Shop. Then come two keys. The secret key stays on the shop's server. I create it. Each key is shown once, so it is hidden right after. The publishable key is different. It can only swap a card for a token. That makes it safe to put in a web page. Next, the shop's web address, as an allowed browser origin. Only that site can call PayBroker from a browser. Then a webhook address. That is where PayBroker sends signed events. The signing secret appears once, so I copy it." | "Secret key: server only" ; "Publishable key: browser safe" ; "Shown once" |
| 4 | 1:55 to 2:45 | Switch to the terminal (large font). Run `TOKEN=$(tok 4242424242424242); echo $TOKEN`. Then run the authorize call (below the table). Show the JSON: `status`, `fraud_assessment.score`, `processor_response.psp_name`. Run the **same** authorize command again and point at the identical `payment_id`. | "Time for a payment. First, the customer's browser trades the test card for a token. It uses the publishable key. So the shop's server never sees the card number. Now the server authorizes forty-nine ninety-nine dollars, using the secret key. It adds an idempotency key. That is a unique label for the order. [pause 1.5] The answer says authorized, with a fraud score, and the processor that was used. Now suppose the network drops, and the shop sends the same request again. [pause 1.5] It gets the same payment back. Look at the payment id. It is identical. Never a second charge." | "Token, not card number" ; "Same idempotency key = same payment" |
| 5 | 2:45 to 3:20 | Back in the browser: **Payments**, type the payment id (or `order-1001`) in the search box, click the row. Pause on **Fraud engine: N / 1000** (an approved payment says *No risk signals worth reporting*; the **Tier 1 rules and filters** list appears only when a rule fired, as in scene 7), point at **Timing**, **Features the model saw** (click to open), **Routing**. Close the window. | "Back in the dashboard, Payments shows it. I search for the payment, and open it. At the top, the fraud score, out of one thousand. This payment is clean, so no risk signals are reported. Below, the checks that ran. Tier one is a list of fixed rules. Tier two is a small model, running inside PayBroker itself. Here is the time spent in each step. A few milliseconds in total. Next, the details the model looked at. And last, the processor that handled it. Every decision is explained. So an analyst never has to guess." | "Score 0-1000" ; "Rules + model + routing, explained" |
| 6 | 3:20 to 4:15 | Terminal. (a) `auth` with a token for `9999990100000070` (see the seed table) and key `order-1002`: show `402 CARD_DECLINED`. (b) `auth` with a token for `9999990200000079`, key `order-1003` and `"return_url":"http://localhost:3000/return.html"`: show `REQUIRES_ACTION` and `action_required.redirect_url`. Open the URL in the browser, click **Approve (verification succeeds)**: the browser lands on `localhost:3000/return.html?payment_id=...&signature=...`; point at the signed link the shop page prints (a page capture has no address bar). (c) `auth` with a token for `9999990300000078`, key `order-1004`: it succeeds. In **Payments** open it and show **Routing**: first connector `error`, second `approved`. | "Next, test cards that trigger specific behaviours. The first one makes the customer's bank decline. [pause 2.0] The answer is four oh two, card declined. Clear, and easy to handle. The second card demands three-D Secure. That is an extra step, where the customer proves who they are. [pause 2.0] The answer holds a page to send the customer to. I open it, and approve the verification. [pause 1.5] The customer is back at the shop, with a signed link the shop can verify. The third card makes the first processor fail. [pause 2.0] PayBroker tries the next processor straight away. So the payment still succeeds. In Payments, the routing shows it. First an error, then approved." | "402: bank declined" ; "3-D Secure: verify the customer" ; "Cascade: processor 1 failed, processor 2 approved" |
| 7 | 4:15 to 5:05 | Terminal: send the risky payment (below the table) with a token for `5555555555554444`, key `order-risky-1`: show `403 FRAUD_REJECTED` and the reasons. Browser: **Cases** (about 25 cases from the demo traffic are listed; the newest is first, find this payment id), click the payment id to open it, read the **AI triage** box (the badge says *rule-based fallback* there, not in the list), choose **Block permanently** in the **Decision** list, click **Resolve**, confirm **Block**: the toast lists what was blocked. Close the window, click **Rules** and scroll to the **Blocklist** table (the heading above it reads *Blocked list*). | "Now a risky payment. A brand-new account, a very fast checkout, and a pasted card number. The basket is large, too. Let's send it. [pause 2.0] Four oh three, rejected. The score is high, so no processor ever saw it. A case opens for an analyst. Here it is, at the top of Cases. The analyst reads the summary. Here it comes from the built-in fallback, because no language model is configured. The AI only advises. A person decides. I choose permanent block, and resolve the case. [pause 1.5] The device, the address, and the card are blocked from now on. In Rules, the blocklist shows them." | "403: rejected before any processor" ; "AI advises, a person decides" ; "Blocked: device, ip, card" |
| 8 | 5:05 to 5:35 | **Rules** tab. Point at the four built-in rules (a fourth, `name_mismatch_large_basket`, was added with the NAME_MISMATCH signal; the recorded narration and `video/record.spec.ts` still say "three" and need the word changed at the next recording). In **Add a rule** type name `rushed_large_basket`, action **Ask for 3-D Secure**, keep the example definition (checkout under 5 seconds and amount at least 50000), click **Add rule**. Scroll to **Suggested rules** and point at *No suggestions waiting* (or at a pending one, if the day's job has run). | "Now, the rules behind those decisions. There are four built-in rules. Rules are plain data, not code. I add one. It asks for three-D Secure when a large basket is paid in under five seconds. The editor only accepts known fields and values. So a typo is refused, instead of silently ignored. The system can also propose new rules, from confirmed fraud. But a suggestion does nothing until a person approves it." | "Rules = data, validated" ; "Suggestions wait for a person" |
| 9 | 5:35 to 6:00 | Terminal with `receiver.py` output: show the lines `signature ok   payment.authorized`, `payment.requires_action`, `payment.declined`. Browser: **Merchants**, **Acme Shop**, scroll to **Recent webhook events**: type, status `delivered`, attempts, last HTTP 200. | "Remember the webhook address? Here is the shop's receiver. It checks the signature on every event. And each one says signature okay. On the merchant, every delivery is listed. With its attempts, and the response code. If the shop is down, PayBroker keeps retrying, with growing delays. So no event is lost." | "Signed events: HMAC-SHA256" ; "Retries with back-off" |
| 10 | 6:00 to 6:30 | **Connect** tab: show the MCP command. Cut to a recap slide with four bullets, then the closing card with the commands `docker compose up -d db`, `paybroker migrate`, `paybroker serve` and the path `docs/GUIDE.md`. | "Everything you saw is also open to an AI assistant, through a read-only connector called MCP. It can search payments, and explain a decision. But it can never resolve a case. To recap. One API. Smart routing. Three layers of fraud checks. And a person who decides. Try it yourself. The quick start takes a few minutes, with Docker. The full guide is in the docs folder of the repository. Next, see how ShopFront uses PayBroker." | Recap: "One API | Smart routing | Rules, model, AI triage | A person decides" ; closing: "docs/GUIDE.md" |

(Word count of the narration column: about 740 words, which is about 5 minutes 20 seconds of speech; the rest of the 6:30 is action time.)

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

Optional extra beat at the end of scene 7 (adds about 8 seconds; trim the narration of scene 8 if needed): send the same card again with a new key to show the
block list at work. The answer is `403` and the reasons contain `BLACK_LISTED_CARD` (the card fingerprint was blocked when the case was resolved).

### Things to check before pressing record

- Start every take from an empty database (`docker compose down -v`): the risky payment is deterministic only on a clean database in the order of this script. A second take on the same
  database reuses idempotency keys, the blocklist already holds the card from the first take (the answer is still `403`, with `BLACK_LISTED_CARD` among the reasons), and the demo button is gone.
- Use the scene 6 cards from the seed table. The public numbers of the README table are also used by the demo traffic, so right after scene 2 they can answer `403` (`HARD_VELOCITY_CARD`).
- The 3-D Secure card returns `200 REQUIRES_ACTION` only because its issuer behaviour forces a challenge; it does not depend on the score.
- The signed return only happens because Acme Shop has a webhook secret and `http://localhost:3000` is in its allowed origins. Both are done in scene 3.
- The server terminal must show no vault banner problems: the vault unlocks when you create the admin password. If you restart `paybroker serve` mid-recording, sign in
  again (the vault is locked until a sign-in), otherwise webhooks wait.

## Recap and call to action (as spoken in scene 10)

Recap: one API; smart routing across processors with a cascade on failure; three layers of fraud checks (rules, a model, AI triage) that each leave reasons you can read; a person who
makes every decision; signed webhooks and a read-only MCP server.

Call to action: run it locally with `docker compose up -d db`, `paybroker migrate`, `paybroker serve`; read `docs/GUIDE.md` for configuration, security and operations; then watch the ShopFront
video to see a real web shop paying through PayBroker.

## Recording notes

### Capture settings

Browser window 1920 by 1080 at 100 % zoom (dark or light theme, but the same for the whole video); terminal font at least 18 pt; record at 1080p, 30 fps; no cursor trails. Keep the server
terminal out of frame except for the setup code in scene 1. Narration can be recorded afterwards over a silent screen capture; the scene times above leave room for it.

### What the Playwright recorder does

`docs/video/record.spec.ts` (with `playwright.config.ts` and `package.json` beside it) performs every on-screen action of the table above in the installed Google Chrome, viewport and video 1920 by 1080,
a pause of 0.5 to 3.5 seconds after each step, a visible cursor, a pulsing outline on the element being discussed and the callouts as lower thirds (each at least three seconds). It writes
`docs/video/out/paybroker-tutorial.webm` (git-ignored). The header comment of the file lists the environment. In short:

```bash
docker compose down -v && docker compose up -d db && .venv/bin/paybroker migrate     # a database that has never been set up
PAYBROKER_PUBLIC_URL=http://localhost:8000 PAYBROKER_ALLOW_PRIVATE_URLS=true .venv/bin/paybroker serve > /tmp/pb.log 2>&1 &
cd docs/video && npm install && npx playwright install ffmpeg      # once; the ffmpeg download is what encodes the video
PB_URL=http://localhost:8000 PB_LOG=/tmp/pb.log PB_ADMIN_PASSWORD='<12+ characters, throwaway>' npx playwright test
```

It starts its own stand-ins (the shop's return page and an on-screen terminal on port 3000, the signed-webhook receiver on 4010), so the two helper servers above are not needed for it. The terminal scenes (4, 6, 7, 9) are drawn on that
terminal page: the recorder types the command, makes the real call from Node and prints the real answer. The recorder asserts what the script claims (for example `403` with `NEW_ACCOUNT`, `402 CARD_DECLINED`,
`REQUIRES_ACTION` then the signed return, the routing list showing `error` then `approved`, `signature ok` for the three event types, `delivered` events), so a take that drifts from the script fails instead of recording something false.
Keys and the signing secret are shown for about a second and then masked in the page. `PACE=0.3` makes a quick rehearsal, `REC_VIDEO=off` skips encoding, `REC_SHOTS=<dir>` saves a screenshot at every callout,
`BLOCKLIST_BEAT=1` adds the optional beat of scene 7.

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
