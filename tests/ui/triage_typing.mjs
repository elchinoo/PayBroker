// Regression guard for Settings > AI triage limits: refreshing the usage line or saving repaints only the status text, never the
// inputs, so a half-typed value survives. Also checks the two pure helpers.  Run: node tests/ui/triage_typing.mjs
import { calls, byText, check, click, done, fire, hasText, live, loginAs, route, settle, shell, type, up } from "./fakedom.mjs";

shell(["view-settings", "set-triage"]);
await loginAs(["*"]);
const usage = (n) => ({ calls_24h: n, queued: 12, circuit: { open: false, paused_until: null, consecutive_429: 1 } });
let current = { rpm: 20, daily_cap: 500, group_minutes: 30, pause_minutes: 10, sources: { rpm: "default", daily_cap: "setting", group_minutes: "default", pause_minutes: "environment" }, usage: usage(7) };
route({
  "GET /api/admin/triage/settings": () => current,
  "PUT /api/admin/triage/settings": (init) => { current = { ...current, ...JSON.parse(init.body), usage: usage(9) }; return current; },
});
const { renderTriageLimits, usageLine, bodyFrom } = await import("/static/triage.js");
await renderTriageLimits(live("set-triage"));
await settle();

check(live("tr-rpm")?.value === "20" && live("tr-cap")?.value === "500", "the stored values were not shown");
check(hasText("LLM calls in the last 24 hours: 7 of 500") && hasText("Queued triage jobs: 12") && hasText("Circuit breaker: closed"), "the usage line is missing");
check(hasText("Calls per 24 hours: saved here"), "where a value comes from is not shown");

// type into two fields, then refresh the usage: neither value may be replaced
await type(live("tr-rpm"), "45");
await type(live("tr-group"), "1");
current = { ...current, usage: usage(8) };
await click(byText(document.body, "button", "Refresh usage"));
await settle();
check(hasText("24 hours: 8 of 500"), "the usage line did not refresh");
check(live("tr-rpm")?.value === "45" && live("tr-group")?.value === "1", `a refresh wiped what was typed: ${live("tr-rpm")?.value}/${live("tr-group")?.value}`);

// an invalid value is reported and nothing is sent
const before = calls.length;
await type(live("tr-pause"), "0");
await fire(up(live("tr-pause"), "form"), "submit");
await settle();
check(hasText("Pause after three gateway 429s") && calls.length === before, "an out-of-range value was sent");

// a valid save sends the CSRF-protected PUT with all four numbers, updates the status, and keeps the inputs
await type(live("tr-pause"), "15");
await fire(up(live("tr-pause"), "form"), "submit");
await settle();
const put = calls.find((c) => c.key === "PUT /api/admin/triage/settings");
check(put && JSON.stringify(put.body) === JSON.stringify({ rpm: 45, daily_cap: 500, group_minutes: 1, pause_minutes: 15 }), `wrong body ${JSON.stringify(put?.body)}`);
check(hasText("24 hours: 9 of 500") && live("tr-rpm")?.value === "45", "the save redrew the form or missed the new usage");

// pure helpers
check(usageLine({ ...current, daily_cap: 0 }).includes("(no cap)"), "no cap is not described");
check(usageLine({ ...current, usage: { calls_24h: 3, queued: 0, circuit: { open: true, paused_until: 1900000000, consecutive_429: 3 } } }).includes("paused until"), "an open breaker is not described");
check(bodyFrom({ rpm: "1.5", daily_cap: 1, group_minutes: 1, pause_minutes: 1 }).error && bodyFrom({ rpm: "", daily_cap: 1, group_minutes: 1, pause_minutes: 1 }).error, "bad numbers accepted");
done("typing in the triage limits survives a refresh and a save");
