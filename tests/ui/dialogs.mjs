// The pages ask through the kit's confirmDialog/promptDialog, never window.confirm/prompt: cancelling does nothing, a destructive
// action is flagged danger, the chargeback reason is a required prompt with its default, and a dialog survives a page refresh.
// Run: node tests/ui/dialogs.mjs
import { byText, calls, check, click, dialogs, done, live, loginAs, route, settle, shell, type } from "./fakedom.mjs";

shell(["view-rules", "case-list", "case-status"]);
live("case-status").value = "open";
await loginAs(["*"]);
const rule = { id: 1, name: "big_basket", description: "", definition: { all: [{ field: "amount", op: ">", value: 1 }] }, action: "challenge", enabled: true, created_by: "system" };
route({
  "GET /api/rules": { items: [rule], fields: ["amount"], operators: [">"] },
  "GET /api/suggestions": { items: [] }, "GET /api/blacklist": { items: [] },
  "DELETE /api/rules/1": { deleted: 1 },
  "GET /api/cases": { items: [] },
  "GET /api/payments/pay_c": { payment: { payment_id: "pay_c", status: "CAPTURED", amount: 500, currency: "USD", merchant: "Shop" }, fraud: null, case: null, three_ds: null, webhooks: [], chargebacks: [] },
  "POST /api/payments/pay_c/chargeback": { ok: true },
});
const hit = (key) => calls.filter((c) => c.key === key);

// 1. a destructive confirm: cancel does nothing; accept performs it; it is flagged danger and states the consequence
const { initRules } = await import("/static/rules.js");
await initRules();
await settle();
dialogs.auto = "cancel";
await click(byText(document.body, "button", "Delete"));
await settle();
check(hit("DELETE /api/rules/1").length === 0, "cancelling the confirmation still deleted the rule");
check(dialogs.seen.at(-1)?.danger === true && dialogs.seen.at(-1).message.includes("big_basket"), `rule delete dialog: ${JSON.stringify(dialogs.seen.at(-1))}`);
check(dialogs.overlay() === null, "the dialog was left open");
dialogs.auto = "ok";
await click(byText(document.body, "button", "Delete"));
await settle();
check(hit("DELETE /api/rules/1").length === 1, "accepting the confirmation did not delete the rule");

// 2. the chargeback reason is a required prompt with its default; blank cannot be confirmed; cancel records nothing
const { openPayment } = await import("/static/payments.js");
await openPayment("pay_c");
dialogs.auto = "off";
const asked = click(byText(document.body, "button", "Record a chargeback"));
await settle();
check(dialogs.overlay() && dialogs.button("Record chargeback"), "the chargeback prompt did not open");
const input = dialogs.overlay()._all().find((e) => e.tag === "input");
check(input.value === "customer disputes the charge", `the prompt lost its default reason: ${JSON.stringify(input.value)}`);
await type(input, "   ");
check(dialogs.button("Record chargeback").disabled === true, "a blank reason can be confirmed");
await type(input, "card stolen");
check(dialogs.button("Record chargeback").disabled === false, "a reason cannot be confirmed");
await dialogs.answer(true);
await asked;
await settle();
check(hit("POST /api/payments/pay_c/chargeback")[0]?.body.reason === "card stolen", `chargeback body: ${JSON.stringify(hit("POST /api/payments/pay_c/chargeback")[0]?.body)}`);

await openPayment("pay_c");
const again = click(byText(document.body, "button", "Record a chargeback"));
await settle();
await dialogs.answer(false);
await again;
await settle();
check(hit("POST /api/payments/pay_c/chargeback").length === 1, "cancelling the prompt still recorded a chargeback");
done("destructive actions and the chargeback reason go through the kit dialogs and cancelling does nothing");
