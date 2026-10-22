// Regression: choosing a period redrew the whole overview, including the period selector itself: a new element, so keyboard
// focus was lost after every step.  Run: node tests/ui/overview_focus.mjs
import { byLabel, calls, check, done, fire, live, loginAs, route, settle, shell } from "./fakedom.mjs";

shell(["view-overview"]);
await loginAs(["*"]);
const overview = { days: 7, payments: 3, by_status: { CAPTURED: 3 }, approval_rate: 1, money_minor_units: { approved_cents: 300 }, latency_ms: { avg: 5, p95: 9 },
  fraud_decisions: { APPROVE: 3 }, score_histogram: [{ from: 0, count: 3 }], psp: [{ psp: "stripe", n: 3, approved: 3 }], daily: [{ day: "2026-05-01", n: 3, declined: 0 }],
  open_cases: 0, pending_rule_suggestions: 0, outbox_backlog: 0 };
route({ "GET /api/overview": overview });
const { initOverview } = await import("/static/overview.js");
await initOverview();
await settle();

const period = byLabel("Period");
period.focus();
period.value = "30";
await fire(period, "change", { target: period });
await settle();
const after = byLabel("Period");
check(after === period, "the period selector was replaced by the redraw");
check(document.activeElement === after, "the period selector lost the keyboard focus");
check(after.value === "30", "the chosen period was not kept");
check(calls.some((c) => c.path.endsWith("days=30")), "the new period was not requested");
check(live("view-overview").textContent.includes("3"), "the data region was not drawn");
done("the period selector keeps its element, focus and choice");
