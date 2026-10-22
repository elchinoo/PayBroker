// Regression: toggling, deleting or unblocking reloaded the whole Rules page and threw away a half-written rule and a half-typed
// block.  Run: node tests/ui/rules_typing.mjs
import { allByText, byText, check, click, done, live, loginAs, route, settle, shell, type } from "./fakedom.mjs";

shell(["view-rules"]);
await loginAs(["*"]);
const rule = (id, enabled) => ({ id, name: `rule_${id}`, description: "", definition: { all: [{ field: "amount", op: ">", value: 1 }] }, action: "challenge", enabled, created_by: "system" });
let blocked = [{ id: 7, kind: "ip", value: "203.0.113.9", reason: "", source: "manual", created_at: "2026-05-01T10:00:00Z" }];
route({
  "GET /api/rules": () => ({ items: [rule(1, true), rule(2, true)], fields: ["amount"], operators: ["=="] }),
  "GET /api/suggestions": { items: [] },
  "GET /api/blacklist": () => ({ items: blocked }),
  "PATCH /api/rules/1": { id: 1, enabled: false },
  "DELETE /api/blacklist/7": () => { blocked = []; return { deleted: 7 }; },
});
const { initRules } = await import("/static/rules.js");
await initRules();
await settle();

await type(live("rule-name"), "rushed_big_basket");
await type(live("rule-desc"), "checkout under 5 s and over 500 USD");
await type(live("rule-def"), '{"all": [{"field": "checkout_seconds", "op": "<", "value": 5}]}');
await type(live("bl-value"), "198.51.100.77");
await click(allByText(document.body, "button", "Turn off")[0]);
await settle();
const kept = () => [live("rule-name")?.value, live("rule-desc")?.value, live("rule-def")?.value, live("bl-value")?.value];
const want = ["rushed_big_basket", "checkout under 5 s and over 500 USD", '{"all": [{"field": "checkout_seconds", "op": "<", "value": 5}]}', "198.51.100.77"];
check(JSON.stringify(kept()) === JSON.stringify(want), `toggling a rule wiped the forms: ${JSON.stringify(kept())}`);

await click(byText(document.body, "button", "Unblock"));
await settle();
check(JSON.stringify(kept()) === JSON.stringify(want), `unblocking an entry wiped the forms: ${JSON.stringify(kept())}`);
check(document.body.textContent.includes("Nothing is blocked."), "the blocked list was not refreshed");
done("the rule and block forms keep what is typed while the lists refresh");
