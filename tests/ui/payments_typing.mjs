// Guard (this page was audited and never had the bug): the search box debounces and reloads only the table body, never the box.
// Run: node tests/ui/payments_typing.mjs
import { calls, check, done, fire, live, loginAs, route, settle, shell, type } from "./fakedom.mjs";

shell(["pay-q", "pay-status", "pay-decision", "pay-score", "pay-body", "pay-empty", "pay-more", "pay-total"]);
await loginAs(["*"]);
route({ "GET /api/payments": { items: [{ payment_id: "pay_1", merchant: "Shop", amount: 100, currency: "USD", status: "CAPTURED", fraud_score: 10, fraud_decision: "APPROVE", created_at: "2026-05-01T10:00:00Z" }] } });
const { initPayments } = await import("/static/payments.js");
initPayments();
await settle();
const box = live("pay-q");
await type(box, "cus_ab");
await new Promise((r) => setTimeout(r, 400));     // the 250 ms debounce fires and reloads the table
await type(box, "cus_abc");
await new Promise((r) => setTimeout(r, 400));
await settle();
check(live("pay-q") === box && box.value === "cus_abc", "the search box was touched by the reload");
check(calls.filter((c) => c.path.includes("q=cus_abc")).length === 1, "the debounced search did not run once for the final text");
check(live("pay-body").textContent.includes("pay_1"), "the table body was not redrawn");
done("typing in the payment search survives the debounced reloads");
