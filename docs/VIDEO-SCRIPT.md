# PayBroker: tutorial video script

## Title

**"I must prove nobody altered the record": PayBroker on EDB Postgres and AgentPG (an audit ledger the database can verify, row-level security, masked views)**

## Audience

Customers who run payments (customer video 1 of 5) and the internal teams who present it. Problem first: *I am running payments and I have to prove that nobody changed a
decision after the fact.* No payments background is needed; every term is explained in one sentence. Developers will want the SQL (it is on screen, in full); auditors will want
the Verify result.

## Goal

By the end the viewer has seen, with real SQL and real results (not described):

1. The problem, and, within the first 30 seconds, that **sign-in, the LLM gateway and the secret vault are swappable reference scaffolding (not the point)**, that **PayBroker is a sample app**, and that
   **EDB Postgres on the AgentPG platform is doing the work** (the three-colour diagram: PLATFORM / REFERENCE SCAFFOLDING / SAMPLE APP).
2. **The audit ledger.** Every decision, capture, refund, rule change and key creation is written in the *same database transaction* as the change, chained by SHA-256. **Verify: intact.**
3. **The tamper.** As the table owner, one historical ledger row is changed with real SQL (the SQL is on screen). **Verify names the first broken entry** (number and reason). The application's role cannot UPDATE or DELETE the ledger
   (`permission denied`), and the owner who grants itself the right is still refused by the trigger until the owner switches it off: *that is detected, not prevented*.
4. **Row-level security.** The same query as two different merchants returns different rows; with nothing set it returns zero rows; an AI role that was given a plain `SELECT` grant on the table also returns zero rows (forced policies). The four rules are named.
5. **Masked views.** The read-only role is denied the base table and reads `*_masked` views. The one real finding: a new database user re-opens grants, so the masking is re-asserted every 5 minutes (kit 0.3.1; its status is public in `/readyz`).
6. **EDB technology on screen**: real screenshots labelled *CAPTURED from the live AgentPG fleet, 28 Oct 2026*: `edb_stat_monitor` top queries, the monitor recording PayBroker's own masking re-assert, `edb_pg_tuner` settings and `query_advisor` suggestions.

Landing moment (customer-story table, goal G10): *"look at this cool thing only Postgres on the platform can do"* (the database names the exact tampered entry), plus *"look how much easier this is"*: no external ledger service, no blockchain, no extra system.

Kept from the earlier cut because it fits the story: fraud scoring with `NAME_MISMATCH` (fuzzystrmatch). Cut: 3-D Secure, the processor cascade, the case screen, signed webhooks, pgvector nearest cases, the rules editor, high availability and AIDB (not shown, not mentioned).

Length: about 5 minutes 56 seconds. Voices: Kokoro, US accent, alternating male and female by topic (see `video/README.md`): `am_liam`, `af_heart`, `am_eric`, `af_heart`, `am_liam`, `af_heart`, `am_eric`, `af_heart`, `am_liam`, `af_heart` (one per scene, never the same voice twice in a row; speed 1.08).

**Truthfulness rules (what is real and what is not).**

- Everything on screen is a real run against a throwaway local PostgreSQL 17.5 cluster (127.0.0.1, a free port, a fresh database per take) and a fresh PayBroker. The app runs as an **ordinary owner role** (`pb_owner`: no superuser, no
  `BYPASSRLS`), so row-level security binds it, as on AgentPG. The cluster is set up by the superuser `postgres`, which is not used on screen.
- Scene 9 shows three **captured** screenshots of the live fleet's PayBroker Insights tab (`videos/live-captures/paybroker-insights-top-queries-2026-10-28.jpg`, `...-masking-sql-in-monitor-...jpg`, `...-tuner-advisor-...jpg`), banner on screen, instance not identified. Nothing on them is retyped: the recorder only zooms and draws highlight boxes. Local Postgres has no `edb_stat_monitor`, `edb_pg_tuner` or `query_advisor`. `extensions-installed.txt` and `ecoctl-insights.txt` are no longer used.
- `a1p_readonly` and the login `reporting_reader` are created by the recorder to mimic AgentPG (`lib.ts`). `ai_agent` is a plain role that is given `SELECT` on `transaction` on screen.
- The `sql` alias on screen is `psql "$DB"` with terse errors (`-v VERBOSITY=terse`); `$DB` is the `pb_owner` connection (no password, loopback, trust authentication).
- The tamper is *really* done: the trigger is switched off by the owner, one row changed, the trigger switched on. The recording database is thrown away afterwards.
- Nothing here claims PCI certification or suitability for real card data. Test cards only.

## Prerequisites (reproducible locally)

The recorder needs no hand setup: `python3 docs/video/build.py --work <dir>` starts everything (see `docs/video/README.md`). To follow along by hand with Docker, use the README quick start; then, as the owner role:

```bash
psql "$DB" -c "SELECT * FROM audit_verify_detail()"       # entries, first_broken, reason, head_seq, head_hash
```

An ordinary (non-superuser) database owner is needed for the row-level-security scenes; with a superuser every policy is skipped (Postgres exempts superusers), and `GET /api/audit/verify` says so (`row_level_security.effective`).

## Scenes

Timestamps are the real ones of the review cut (the build prints them). Narration is mirrored from `docs/video/narration.txt` by the build: edit that file, never this column.

| # | Time | ON-SCREEN ACTION | NARRATION | ON-SCREEN CALLOUTS |
|---|---|---|---|---|
| 1 | 0:00 | Card: "Prove nobody altered the record" with three red lines (not by an engineer, a DBA, me). Then the labelled diagram: PLATFORM (EDB Postgres on AgentPG: audit ledger, row-level security, masked views, fuzzystrmatch, EDB insights), SAMPLE APP (PayBroker), three REFERENCE SCAFFOLDING boxes (sign-in, LLM gateway, secret vault) highlighted as "swappable"; legend; "test cards only". | "I am running payments. And I have to prove that nobody changed a decision after the fact. Not my engineers. Not my database admin. Not me. Here is how EDB Postgres on AgentPG does that. This platform does the work you are about to see. PayBroker is only a sample app. Sign-in, the LLM gateway, and the secret vault are reference scaffolding. They are swappable, and they are not the point. It uses test cards only. Nothing here moves real money." | none |
| 2 | 0:30 | Setup page, **Generate 120 demo payments**, **Merchants**: Acme Shop, secret key, publishable key (blurred, then masked). | "First, a quick setup. I paste the one-time code, and choose a password. One click sends a hundred and twenty fictional payments. Each one is scored. And each decision goes into a ledger. [pause 4.0] Done. Now a shop. [pause 1.0] I create Acme Shop, with a secret key. [pause 3.0] Then a publishable key. [pause 2.5] Each key is shown once, then hidden." | One-time setup code from the server log; Every decision is also written to the audit ledger; Shown once, then hidden |
| 3 | 0:57 | Terminal: token; authorize 49.99 (manual capture); capture; refund 10.00; then a payment whose card says Jon Smith and account says Peter Wong: `NAME_MISMATCH`. | "Now Acme Shop takes a payment. The customer's card becomes a token, so the shop never sees the number. The server authorizes forty-nine ninety-nine dollars. [pause 1.5] Authorized. Then it captures the money. [pause 1.5] And refunds ten dollars. [pause 1.5] One more. The card says Jon Smith. The account says Peter Wong. The basket is large. [pause 1.5] Postgres compares the names with an extension, fuzzystrmatch. The reason is name mismatch, and the customer is asked to verify." | Card says Jon Smith, account says Peter Wong; NAME_MISMATCH: reason code from fuzzystrmatch |
| 4 | 1:31 | **Audit** tab: the entries; terminal: the last four entries with `prev_hash` and `hash`; **Verify the chain**: *Chain intact*; terminal: `SELECT * FROM audit_verify_detail()`. | "Every one of those wrote a ledger entry. Decisions, captures, refunds. Also rule changes, and key creation. Each entry is written in the same database transaction as the change itself. So a decision cannot exist without its entry. And each entry carries the SHA-256 hash of the entry before it. That is a chain. Now I press Verify. [pause 2.0] The database recomputes every hash. Chain intact. Here is the same check in plain SQL. [pause 2.5] A function, a trigger, and a hash. There is no external ledger service to run." | decision, capture, refund, rule change, key creation; Same database transaction as the change itself; Each prev_hash is the hash of the entry above; No external ledger service |
| 5 | 2:07 | Terminal (table owner): show the capture entry; `UPDATE` as the app role (permission denied); `DELETE` (permission denied); owner `GRANT UPDATE` then `UPDATE` (trigger: append-only); `ALTER TABLE ... DISABLE TRIGGER`, `UPDATE 1`, `ENABLE TRIGGER`; the entry now says 1. | "Now I try to cheat. This entry is the capture of forty-nine ninety-nine. [pause 1.5] I will change it to one cent. First, as the app's own database role. [pause 1.5] Permission denied. And deleting it? Also denied. [pause 1.5] But I am also the table owner. The owner can grant itself the right to update. [pause 3.0] The trigger still refuses. The table is append only. [pause 1.0] But the owner can switch the trigger off. So I do. [pause 4.5] The capture now says one cent. No error. No alarm. [pause 1.0]" | Round 1: the app's own database role; Round 2: the table owner; The owner switches the trigger off, edits, switches it on |
| 6 | 2:47 | **Audit** tab: **Verify the chain**: *Chain broken. First broken entry: #N* with the reason, the row highlighted; terminal: `audit_verify_detail()` shows `first_broken` and `reason`. | "Now, back in the dashboard. I verify the chain again. [pause 2.5] Chain broken. The first broken entry is named, with its number, and the reason. The content no longer matches its hash. Everything before it still verifies. The same answer comes in plain SQL. [pause 2.5] Look at that. Nobody can stop the owner. But nobody can change history without leaving proof. No blockchain, no ledger service, no extra system. Just Postgres." | Chain broken. First broken entry: #N; No blockchain. No ledger service. Just Postgres. |
| 7 | 3:17 | Terminal: one query as Acme Shop (one row), as Demo Shop (a different row), with nothing set (0 rows); `GRANT SELECT ... TO ai_agent` and `SELECT count(*)` as `ai_agent` (0). | "Next problem. Acme Shop must never see another merchant's payments. Even if a developer forgets the filter. Row-level security does that, inside the database. There are four rules. A merchant sees only its own rows. Child rows follow their payment. Cases and chargebacks belong to the operator. And read-only auditors may look, never change. Here is one query. First, as Acme Shop. [pause 2.0] One row. Now the same query, as another merchant. [pause 2.0] Different rows. And with nothing set at all? [pause 2.0] Zero rows. It fails closed, even for the table owner. Last, an AI agent. I give its role a plain select grant on the table. [pause 2.5] Still zero rows. The policy is forced. A grant alone opens nothing." | Four rules: own rows, children follow the payment, operator only, read-only auditors; SELECT granted, zero rows: the policy is forced |
| 8 | 4:10 | Terminal: the app's connection reads `transaction` (real ids, full IP); `reporting_reader` is denied the table; the same query on `transaction_masked` (hashes, `.xxx`); `curl /readyz \| jq .masking`. | "Next problem. An analyst, or an AI agent, needs to read the data. But not the customer details. The app's own connection sees the real customer id, and the real address. [pause 1.5] The read-only role is denied the base table. [pause 1.5] But the masked view is allowed. Identifiers become hashes. Addresses lose the last number. [pause 1.5] Now, one real finding. When someone creates a new database user, grants on the base tables open again. So PayBroker re-asserts the masking every five minutes. Its status is public, here. [pause 2.0] Remember this. I will show it again, recorded by the platform itself." | Analyst or AI agent: not the customer details; New database user = grants re-open. PayBroker re-asserts every 5 minutes |
| 9 | 4:51 | Insights tab (local: `pg_stat_statements` only); then three real screenshots of the live PayBroker Insights tab, full screen under the banner *CAPTURED from the live AgentPG fleet, 28 Oct 2026*, each with a slow zoom and a highlight box: `edb_stat_monitor` top queries (with the "statement texts are hidden: needs pg_read_all_stats" line), further down the monitor\'s record of PayBroker\'s own masking statements (`REVOKE SELECT ... FROM "a1p_readonly"`, `CREATE OR REPLACE VIEW "admin_audit_masked"`: the 5-minute re-assert), then `edb_pg_tuner` settings and `query_advisor` index suggestions. | "The platform adds one more thing. Insights. On my laptop, plain Postgres shows only pg_stat_statements. On AgentPG, the same page reads edb_stat_monitor, edb_pg_tuner, and query_advisor. These are real screenshots from the live fleet, 28 October. [pause 0.5] First, edb_stat_monitor: the slowest statements. [pause 1.0] Further down, the platform's own monitor recorded PayBroker re-asserting its masking. It revokes select on the base tables. [pause 0.5] And it creates a masked view again. [pause 1.0] Then edb_pg_tuner suggests memory settings. [pause 1.0] And query_advisor suggests indexes. It only suggests. A person decides. [pause 0.5]" | This laptop: pg_stat_statements only; On AgentPG: + edb_stat_monitor, edb_pg_tuner, query_advisor |
| 10 | 5:36 | The labelled diagram again with the commands (`ecoctl apply`, `docker compose`, `paybroker migrate && serve`) and the guide section. | "So, the problem was proof. Nobody altered the record, and the database can show it. The platform, EDB Postgres on AgentPG, did the work. The scaffolding is swappable. PayBroker is the sample. Try it yourself. One command on AgentPG. Or Docker on your laptop. The guide, section eighteen, has the tamper test." | none |

### Commands used on screen

The recorder runs exactly the text it types (`sql` is `psql "$DB"` as the owner role; `$ACME` and `$OTHER` are merchant ids; `$AI` the `ai_agent` login; `$RO` the read-only login). Scene 5 is the tamper (`<seq>` is the first `payment.captured` entry):

```sql
SELECT seq, action, payload->'amount_captured' AS captured FROM audit_ledger WHERE seq = <seq>;
UPDATE audit_ledger SET payload['amount_captured'] = '1' WHERE seq = <seq>;     -- ERROR: permission denied for table audit_ledger
DELETE FROM audit_ledger WHERE seq = <seq>;                                       -- ERROR: permission denied for table audit_ledger
GRANT UPDATE ON audit_ledger TO CURRENT_USER;                                     -- the owner can do this
UPDATE audit_ledger SET payload['amount_captured'] = '1' WHERE seq = <seq>;     -- ERROR: UPDATE on audit_ledger is not allowed: the audit ledger is append-only
ALTER TABLE audit_ledger DISABLE TRIGGER audit_ledger_no_change;                  -- the owner can do this too
UPDATE audit_ledger SET payload['amount_captured'] = '1' WHERE seq = <seq>;     -- UPDATE 1
ALTER TABLE audit_ledger ENABLE TRIGGER audit_ledger_no_change;
SELECT * FROM audit_verify_detail();                                              -- first_broken = <seq>, reason = the entry's content does not match its hash
```

Scene 7 (the same query three times, then the AI role):

```sql
SET app.merchant_id = '<Acme Shop id>';   SELECT merchant_id, count(*) AS payments, min(payment_id) AS first_payment FROM transaction GROUP BY 1;   -- 1 row
SET app.merchant_id = '<Demo Shop id>';   SELECT ...same...;                                                                                         -- a different row
SELECT ...same..., with nothing set;                                                                                                                 -- (0 rows)
GRANT SELECT ON transaction TO ai_agent;   -- then as ai_agent: SELECT count(*) FROM transaction;                                                      -- 0
```

Scene 8: `PGOPTIONS='-c app.bypass=on' psql "$DB" -c "SELECT payment_id, customer_id, request->>'ip' AS ip FROM transaction ..."` (what the operator side of the app sets), `psql "$RO"` on `transaction` (denied) and on `transaction_masked`, `curl -s $PB/readyz | jq .masking`.

Scene 3 uses the shop's `auth`, `capture` and `refund` helpers (curl wrappers with the secret key in a shell variable; keys are never printed).

### Things to check before pressing record

- A fresh database per take: the demo traffic, velocity rules and the blocklist depend on it. `build.py` does that.
- Scene 5 must run after scene 4 (a chain that already verifies) and nothing after scene 6 writes the ledger; the take leaves a broken ledger in a database that is thrown away.
- Port 8000 is held by nginx on the author's Mac: the recorder uses `PB_PORT_RANGE` (default 41700-41799).

## Call to action (as spoken in scene 10)

The problem was proof; the platform (EDB Postgres on AgentPG) did the work; the scaffolding is swappable; PayBroker is the sample. One command on AgentPG (`ecoctl apply`) or Docker on a laptop; `docs/GUIDE.md` section 18 has the tamper test.

## Recording notes

See `docs/video/README.md` for the build (one command), the voice plan, pacing rules and options. The recorder is `docs/video/record.spec.ts`; it asserts what this script claims (the tampered entry is named by `first_broken`, the two merchants see different rows, the nothing-set session sees `(0 rows)`, the AI role counts 0, the read-only role is denied the base table), so a take that drifts from the script fails instead of recording something false.

### Accuracy check list (for the reviewer)

- The ledger hash: `hash(n) = sha256(hash(n-1) || [seq, ts, actor, action, subject, payload]::jsonb::text)` (GUIDE section 18.2). The owner can switch the trigger off: that is not prevented, it is detected, at the first entry that no longer matches.
- Row-level security is `FORCE`d, so it binds the (ordinary) owner role; a superuser or a `BYPASSRLS` role skips it by Postgres design.
- Masking is re-asserted every 5 minutes (GUIDE section 17.2); between a new grant and the next tick a base table can be readable for up to 5 minutes. The video says that plainly.
- Local Postgres cannot run `edb_stat_monitor`, `edb_pg_tuner` or `query_advisor`; the card is a labelled capture.
- Never say the sample is PCI-certified or suitable for real card data.
