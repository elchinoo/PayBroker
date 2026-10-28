// Audit tab: lists the newest ledger entries, Verify shows "Chain intact, N entries" or the first broken entry and its reason (and marks that
// row), a failure to verify is shown as an error, and app.js shows the tab only with audit:read. Run: node tests/ui/audit_tab.mjs
import fs from "node:fs";
import { STATIC, calls, check, click, done, find, hasText, live, loginAs, route, settle, shell } from "./fakedom.mjs";

shell(["view-audit"]);
await loginAs(["*"]);
const entry = (seq, action) => ({ seq, ts: "2026-10-28T10:00:00Z", actor: "merchant:mch_x", action, subject: `pay_${seq}`, payload: { amount: 5000, status: "AUTHORIZED" }, prev_hash: "0".repeat(64), hash: String(seq).repeat(64).slice(0, 64) });
const items = [entry(3, "payment.captured"), entry(2, "payment.authorize"), entry(1, "merchant.key_create")];
let verdict = { intact: true, entries: 3, first_broken: null, reason: null, head: { seq: 3, hash: "a".repeat(64) }, row_level_security: { enforced: true, role_bypasses: false, effective: true, tables: [], missing: [] } };
route({ "GET /api/audit": () => ({ items }), "GET /api/audit/verify": () => verdict });
const { initAudit } = await import("/static/audit.js");
initAudit();
await settle(60);
check(hasText("Audit ledger") && hasText("payment.captured") && hasText("payment.authorize") && hasText("merchant.key_create"), "the entries did not render");
check(hasText("Not verified yet"), "before Verify the page must not claim the chain is intact");
const rows = () => live("audit-body")._all().filter((e) => e.tag === "tr");
check(rows().length === 3 && rows()[0].attrs["data-seq"] === "3", "one row per entry, newest first, each with its seq");
const verify = find(live("view-audit"), "audit-verify");
check(!!verify, "no Verify button");

await click(verify);
await settle(60);
check(hasText("Chain intact") && hasText("3 entries") && hasText("Row-level security is enforced"), "an intact chain must say so, with the entry count");
check(rows().every((r) => !r.className.includes("broken")), "nothing should be marked when the chain is intact");

verdict = { ...verdict, intact: false, first_broken: 2, reason: "the entry's content does not match its hash (it was changed after it was written)" };
await click(verify);
await settle(60);
check(hasText("Chain broken") && hasText("First broken entry: #2") && hasText("does not match its hash"), "a broken chain must name the first broken entry and why");
const marked = rows().filter((r) => r.className.includes("broken"));
check(marked.length === 1 && marked[0].attrs["data-seq"] === "2" && marked[0].textContent.includes("#2 broken"), "exactly the broken entry must be marked in the list");
check(calls.filter((c) => c.path.startsWith("/api/audit/verify")).length === 2, "each press of Verify asks the server once");

// the tab is wired in the shell behind audit:read
const js = fs.readFileSync(`${STATIC}/app.js`, "utf8"), html = fs.readFileSync(`${STATIC}/index.html`, "utf8");
check(js.includes('audit: "audit:read"') && js.includes("audit: initAudit") && js.includes('"audit"'), "app.js does not wire the audit tab behind audit:read");
check(html.includes('data-tab="audit"') && html.includes('id="view-audit"'), "index.html lacks the Audit tab or its section");
done("the Audit tab lists entries, verifies the chain, marks the first broken entry, and is wired behind audit:read");
