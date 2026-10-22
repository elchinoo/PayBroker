// Regression: resolving one case reloads the case list; a note being typed on another case (or on the case inside the payment
// window, when a chargeback is recorded) must still be there afterwards.   Run: node tests/ui/cases_typing.mjs
import { allByText, byText, check, click, done, fire, live, loginAs, route, settle, shell, type } from "./fakedom.mjs";

shell(["case-list", "case-status"]);
live("case-status").value = "open";
await loginAs(["*"]);
const caseRow = (id, pid) => ({ id, status: "open", analyst_decision: null, note: null, resolved_by: null, created_at: "2026-05-01T10:00:00Z", payment_id: pid, amount: 12345, currency: "USD", fraud_score: 800, merchant_id: "mch_1", reasons: ["velocity"], tier3_state: "done", ai_summary: null });
route({
  "GET /api/cases": { items: [caseRow(1, "pay_a"), caseRow(2, "pay_b")] },
  "POST /api/cases/2/resolve": { case_id: 2, decision: "DISMISS_ALERT", blocked: [] },
  "GET /api/payments/pay_c": { payment: { payment_id: "pay_c", status: "CAPTURED", amount: 500, currency: "USD", merchant: "Shop" }, fraud: null, case: { id: 3, status: "open" }, three_ds: null, webhooks: [], chargebacks: [] },
  "POST /api/payments/pay_c/chargeback": { ok: true },
});

const { initCases } = await import("/static/cases.js");
await initCases();
await settle();

// 1. a note on case 1 is half written; the analyst resolves case 2
await type(live("case-note-1"), "same ring as last week, block");
live("case-dec-1").value = "PERMANENT_BLOCK";
await fire(live("case-dec-1"), "change");
await click(allByText(document.body, "button", "Resolve")[1]);
await settle();
check(live("case-note-1")?.value === "same ring as last week, block", `the note on the other case was lost (now: ${JSON.stringify(live("case-note-1")?.value)})`);
check(live("case-dec-1")?.value === "PERMANENT_BLOCK", "the decision picked on the other case was reset");

// 2. the same case inside the payment window: a chargeback is recorded while a note is half written
const { openPayment } = await import("/static/payments.js");
await openPayment("pay_c");
await type(live("case-note-3"), "waiting for the issuer to call back");
await click(byText(document.body, "button", "Record a chargeback"));
await settle();
check(live("case-note-3")?.value === "waiting for the issuer to call back", `the note in the payment window was lost (now: ${JSON.stringify(live("case-note-3")?.value)})`);
done("a note typed on a case survives the reload of the list and of the payment window");
