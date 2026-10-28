# Video: what the audit ledger and row-level security change (notes for the re-record)

Written alongside the code (GUIDE section 18). Nothing under `docs/video/` was touched and nothing was re-recorded; this is the list of what the
recorder, the script and the narration should look at.

## Scenes and selectors affected

| Scene | What changes | What to do |
|---|---|---|
| All dashboard scenes (2, 3, 5, 7, 11, 13) | The tab bar has one more link for an admin: `#nav a[data-tab="audit"]` (text **Audit**, after **Insights**). The recorder's locators (`#merchants`, `#payments`, `#cases`, `#rules`, `#connect`, `getByRole`) are unaffected; the bar is one item wider, so check it still fits on one line at the recording width. | Look at one frame of each dashboard scene after the change. |
| 2 (demo traffic) | Each demo payment now also writes one `payment.authorize` ledger entry in its own transaction (about 1 ms each). The toast text `Sent 120 demo payments` is the same. | Nothing, unless a timing is tight. |
| 3 (merchant, keys, origin, webhook) | Each click writes a ledger entry (`merchant.create`, `merchant.key_create`, `merchant.update`, `merchant.webhook`). No visible change. | Nothing. |
| 4, 6, 7 (terminal payments) | The API answers are byte for byte the same. Authorize, capture, void, refund and the 3-D Secure completion each leave an entry. Merchant requests do two extra tiny round trips to set the row-level-security context; the `Timing` figures of scene 5 are measured before that and do not change. | Nothing. |
| 7 (case resolved: permanent block) | The Block click writes `case.resolve` and one `blocklist.add` per blocked item. | Nothing. |
| 9, 10, 12 (`psql "$DB"` as the owner, `$RO` as the read-only login) | **Row-level security is `FORCE`d, so an ordinary (non-superuser) owner that sets no context sees no rows.** The recorder's throwaway database is owned by the bootstrap superuser `postgres` (`lib.ts`), which Postgres exempts from policies, so the queries on screen still return the same rows. The read-only login keeps working: policies let a session that inherits `a1p_readonly` read (tested). | If the recording ever uses a non-superuser owner, run `$DB` with `PGOPTIONS='-c app.bypass=on'` (or `SET app.bypass = 'on'` first), or scene 12's "owner sees real ids" shows an empty table. |
| 13 (Database insights) | None. | |
| 14 (closing diagram, "`docs/GUIDE.md` section 17") | A section 18 now exists. | Optional: "sections 17 and 18". |
| 1 (diagram chips) | Optional: a new chip such as "audit ledger + row-level security" in the PLATFORM box. | Optional. |

New selectors if a scene is added (all in `src/paybroker/static/audit.js`): nav link `a[data-tab="audit"]`; heading *Audit ledger*; button `#audit-verify`
(*Verify the chain*); verdict region `#audit-status` (text **Chain intact** and `N entries`, or **Chain broken** and `First broken entry: #N`); list `#audit-body`,
rows `tr[data-seq="N"]`, the failing one also has class `broken` and a badge `#N broken`. API: `GET /api/audit/verify`, `GET /api/audit?limit=`.

## Signature moment: tamper with an audit row live, watch Verify fail at that exact entry

This is the "look at the level of work you should be doing" beat: the database itself notices.
About 45 seconds, as UNDER THE HOOD 5 after the read-only masking scene (12), or before it.

1. **Audit** tab, press **Verify the chain**: green badge, "Chain intact, N entries". Narration: "Every decision this app made is in this ledger, chained: each entry's hash covers the one before it."
2. Terminal, as the owner, pick an entry from the list (say the capture of the 49.99 payment, seq 42) and try to rewrite it:
   `UPDATE audit_ledger SET payload = jsonb_set(payload, '{amount_captured}', '1') WHERE seq = 42;` -> `ERROR: UPDATE on audit_ledger is not allowed: the audit ledger is append-only`.
   (As a non-superuser owner the privilege layer answers first with `permission denied`; as the superuser of the recorder's database the trigger answers, which reads better on screen.)
3. Narration: "The owner can switch that protection off. So I will." Then, on screen:
   `ALTER TABLE audit_ledger DISABLE TRIGGER audit_ledger_no_change;` the same `UPDATE` -> `UPDATE 1`, then `ALTER TABLE audit_ledger ENABLE TRIGGER audit_ledger_no_change;`.
4. Back in the dashboard, press **Verify the chain** again: red badge, **Chain broken**, "First broken entry: #42", and row 42 highlighted in the list. Optionally in the terminal: `SELECT audit_verify();` -> `42`.
5. Callout: "Tampering is not prevented for the owner. It is detected, at the exact entry." Close with: "That is plain Postgres: sha256, a trigger and a function. No extension, no superuser."

Optional second beat (only if the recorder is changed to run PayBroker as an ordinary owner role): `SELECT count(*) FROM transaction;` as that role -> `0`,
then `SET app.bypass = 'on'; SELECT count(*) FROM transaction;` -> the real number, "the database fails closed: a connection that says nothing sees nothing".
