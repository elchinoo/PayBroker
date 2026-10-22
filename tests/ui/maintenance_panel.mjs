// Settings > Maintenance: shown only with `data:reset`; the Reset button stays disabled until the phrase matches exactly; the result
// panel shows the counts; and nothing that refreshes (this panel's lists, the rest of Settings) wipes what is typed.
// Run: node tests/ui/maintenance_panel.mjs
import { allByText, byText, calls, check, click, done, fire, hasText, live, loginAs, route, settle, shell, type } from "./fakedom.mjs";

shell(["view-settings"]);
const PHRASE = "RESET PAYBROKER DATA";
const counts = { transaction: 7612, three_ds_challenge: 3, fraud_assessment: 7612, fraud_case: 7000, chargeback: 0, webhook_event: 5, outbox: 9, fs_entity: 400, training_example: 7612, rule_suggestion: 2 };
let recent = [];
let failNext = null;
const acct = (id) => ({ id, name: `acct${id}`, provider: "custom", base_url: "https://llm.example/v1", chat_model: "m", has_key: false });
let accounts = [acct(1), acct(2)];
route({
  "GET /api/accounts": () => ({ items: accounts, presets: [{ id: "custom", name: "Custom", base_url: "" }], triage_account: null }),
  "DELETE /api/accounts/1": () => { accounts = [acct(2)]; return { deleted: 1 }; },
  "GET /api/admin/models": { bundled: { version: "b", trees: 1, trained_on: "x" }, active: { source: "bundled" }, items: [], limits: { max_trees: 1, max_depth: 1, max_bytes: 1000 } },
  "GET /api/admin/training/stats": { settings: { capture: false, retention_days: 1, max_rows: 1000, capture_source: "default" }, rows: 0, labeled: 0, fraud: 0, fraud_rate: null, label_sources: {}, per_model_version: [] },
  "GET /api/tokens": [], "GET /api/session": { has_admin_password: false },
  "GET /api/admin/data/reset": () => ({ phrase: PHRASE, counts, recent }),
  "POST /api/admin/data/reset": (init) => {
    if (failNext) { const f = failNext; failNext = null; return f; }
    const body = JSON.parse(init.body);
    const deleted = { transaction: 7612, three_ds_challenge: 3, fraud_assessment: 7612, fraud_case: 7000, chargeback: 0, webhook_event: 5, outbox: 9, fs_entity: 400 };
    if (body.include_training_data) deleted.training_example = 7612;
    if (body.include_rule_suggestions) deleted.rule_suggestion = 2;
    recent = [{ id: 1, actor: "Ada Admin", at: "2026-05-01T10:00:00Z", detail: { status: "completed", deleted, include_training_data: body.include_training_data, include_rule_suggestions: body.include_rule_suggestions } }];
    return { deleted, skipped: [], seconds: 1.2, chunks: 4, max_chunk_rows: 5000, audit_id: 1 };
  },
});
const { initSettings } = await import("/static/settings.js");

// 1. without the permission the section does not exist (and nothing asks the server for it)
await loginAs(["payments:read", "settings:manage", "model:manage"]);
await initSettings();
await settle(60);
check(live("set-maintenance") === null && !hasText("Reset the transaction data"), "the Maintenance section is shown without data:reset");
check(!calls.some((c) => c.path.includes("/api/admin/data/reset")), "the page asked for the reset summary without the permission");

// 2. with it: the description, the live counts and the disabled button
await loginAs(["*"]);
await initSettings();
await settle(60);
check(hasText("Reset the transaction data") && hasText("Deleted") && hasText("Kept") && hasText("7,612 payments"), "the description or the live counts are missing");
const button = () => live("mt-reset");
check(button().disabled === true, "the Reset button starts enabled");
for (const [typed, enabled] of [["reset paybroker data", false], ["RESET PAYBROKER", false], [PHRASE + " ", false], [" " + PHRASE, false], [PHRASE, true], [PHRASE + "!", false], [PHRASE, true]]) {
  await type(live("mt-phrase"), typed);
  check(button().disabled === !enabled, `the button should be ${enabled ? "enabled" : "disabled"} for ${JSON.stringify(typed)}`);
}

// 3. nothing that refreshes wipes the phrase, the checkboxes or the result
live("mt-training").checked = false;
await click(allByText(document.body, "button", "Delete")[0]);        // an unrelated refresh elsewhere on the page
await settle();
check(live("mt-phrase").value === PHRASE && button().disabled === false && live("mt-training").checked === false, "an unrelated refresh wiped the confirmation");

// 4. a refused reset keeps the phrase and shows the reason
failNext = { __status: 409, body: { detail: "another reset is already running; wait for it to finish" } };
await click(button());
await settle();
check(hasText("already running"), "the refusal was not shown");
check(live("mt-phrase").value === PHRASE, "the phrase was cleared by a refused reset");

// 5. the reset: the request, the result with the counts, the audit list, the phrase cleared and the button disabled again
const before = calls.length;
await click(button());
await settle();
const post = calls.slice(before).find((c) => c.key === "POST /api/admin/data/reset");
check(post && post.body.confirm === PHRASE && post.body.include_training_data === false && post.body.include_rule_suggestions === true, `wrong request: ${JSON.stringify(post?.body)}`);
check(hasText("Done in 1.2 s") && hasText("7,612") && hasText("payments") && hasText("The configuration was kept"), "the result panel with the counts is missing");
check(hasText("Ada Admin") && hasText("completed"), "the recent resets list was not refreshed");
check(live("mt-phrase").value === "" && button().disabled === true, "the phrase was not cleared after the reset");

// 6. refreshes after the result do not remove it
await click(allByText(document.body, "button", "Delete")[0] || byText(document.body, "button", "Add an account"));
await settle();
check(hasText("Done in 1.2 s"), "the result panel was wiped by a refresh");
done("the Maintenance panel gates the button on the exact phrase, shows the result and is never wiped by a refresh");
