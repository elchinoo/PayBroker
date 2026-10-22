// Regression: on Settings, deleting an AI account redrew the whole accounts block (the triage selector snapped back), and every
// action in the Model section (activate, delete, purge, label, save) redrew the whole section: half-written upload notes, a version
// name, a retention value were thrown away.  Run: node tests/ui/settings_typing.mjs
import { allByText, byText, check, click, done, failWith, fire, hasText, live, loginAs, route, settle, shell, type } from "./fakedom.mjs";

shell(["view-settings"]);
await loginAs(["*"]);
const acct = (id, name) => ({ id, name, provider: "custom", base_url: "https://llm.example/v1", chat_model: `m${id}`, has_key: false });
let accounts = [acct(1, "first"), acct(2, "second")];
const env = { id: "env", name: "Environment (server settings)", provider: "env", base_url: "https://gw.example/v1", chat_model: "qwen3", has_key: true, api_key_last4: null, read_only: true };
const thinking = "the model used all 64 completion tokens before answering (finish_reason=length): raise max tokens or use a model without a thinking phase";
route({
  "GET /api/accounts": () => ({ items: accounts, env, presets: [{ id: "custom", name: "Custom", base_url: "" }], triage_account: null }),
  "DELETE /api/accounts/1": () => { accounts = [acct(2, "second")]; return { deleted: 1 }; },
  "POST /api/accounts/env/test": () => failWith(502, thinking),
  "GET /api/admin/models": { bundled: { version: "bundled-1", trees: 40, trained_on: "synthetic" }, active: { source: "bundled" },
    items: [{ id: 5, version: "v2", notes: "", uploaded_by: "ada", uploaded_at: "2026-05-01T10:00:00Z", tree_count: 3, size_bytes: 2048, active: false }],
    limits: { max_trees: 100, max_depth: 6, max_bytes: 1048576 } },
  "POST /api/admin/models/5/activate": { ok: true },
  "GET /api/admin/training/stats": { settings: { capture: false, retention_days: 180, max_rows: 5000000, capture_source: "default" }, rows: 0, labeled: 0, fraud: 0,
    fraud_rate: null, label_sources: {}, per_model_version: [] },
  "POST /api/admin/training/purge": { expired: 0, over_cap: 0 },
  "GET /api/tokens": [],
  "GET /api/session": { has_admin_password: false },
  "GET /api/admin/data/reset": { phrase: "RESET PAYBROKER DATA", counts: {}, recent: [] },
});
const { initSettings } = await import("/static/settings.js");
await initSettings();
await settle(60);

// the environment account: listed read-only as "from environment", offered for triage, and a failed test shows the provider's reason verbatim
check(hasText("from environment") && live("triage-select").children.some((o) => o.value === "env"), "the environment account is not offered");
check(allByText(document.body, "button", "Delete").length === 3, "only the two stored accounts and the model row may have a Delete button");
await click(allByText(document.body, "button", "Test chat")[0]);
await settle();
check(hasText(thinking), "the improved test error was not shown verbatim");

// accounts: pick the second account for triage (not saved yet), then delete the first one
live("triage-select").value = "2";
await fire(live("triage-select"), "change");
await click(allByText(document.body, "button", "Delete")[0]);
await settle();
check(live("triage-select")?.value === "2", `the triage choice snapped back (now ${JSON.stringify(live("triage-select")?.value)})`);

// Model section: half-fill the upload form and the retention value, then act on the list and on the statistics
await type(live("model-notes"), "retrained on March traffic");
await type(live("model-version"), "v3-rc1");
await type(live("tr-retention"), "30");
await click(byText(document.body, "button", "Activate"));
await settle();
await click(byText(document.body, "button", "Purge now"));
await settle();
const kept = [live("model-notes")?.value, live("model-version")?.value, live("tr-retention")?.value];
check(JSON.stringify(kept) === JSON.stringify(["retrained on March traffic", "v3-rc1", "30"]), `the Model forms were wiped: ${JSON.stringify(kept)}`);
done("the triage choice and the Model section forms survive their own refreshes");
