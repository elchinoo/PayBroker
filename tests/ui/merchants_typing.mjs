// Regression: revoking a key (or suspending the merchant) reloaded the whole merchant window: the webhook URL and the routing
// settings being edited were lost, and so was a freshly created key, which is shown only once.  Run: node tests/ui/merchants_typing.mjs
import { allByText, byText, check, click, done, fire, hasText, live, loginAs, route, settle, shell, type, up } from "./fakedom.mjs";

shell(["view-merchants"]);
await loginAs(["*"]);
let revoked = false;
const merchant = () => ({ id: "mch_1", name: "Acme", status: "active", webhook_url: null, created_at: "2026-05-01T10:00:00Z",
  settings: { preferred_psps: [], max_fee_bps: null, weights: { auth: 100, cost: 30, latency: 5 }, require_3ds_over_cents: null, region: null, capture_method: "manual", allowed_currencies: ["USD"] },
  keys: [{ id: 1, kind: "secret", name: "a", prefix: "sk_test_aaaa", last_used_at: null, revoked_at: null }, { id: 2, kind: "secret", name: "b", prefix: "sk_test_bbbb", last_used_at: null, revoked_at: revoked ? "2026-05-02T00:00:00Z" : null }] });
route({
  "GET /api/merchants": { items: [{ id: "mch_1", name: "Acme", status: "active", webhook_url: null, payments: 0, created_at: "2026-05-01T10:00:00Z" }] },
  "GET /api/merchants/mch_1": () => merchant(),
  "GET /api/connectors": { items: [{ name: "stripe" }] },
  "GET /api/webhook-events": { items: [] },
  "POST /api/merchants/mch_1/keys": { id: 3, key: "sk_test_ONLY_SHOWN_ONCE", kind: "secret" },
  "DELETE /api/merchants/mch_1/keys/2": () => { revoked = true; return { revoked: 2 }; },
  "PATCH /api/merchants/mch_1": { id: "mch_1" },
});
const { initMerchants } = await import("/static/merchants.js");
await initMerchants();
await settle();
await click(document.body._all().find((e) => e.tag === "tr" && e.className === "clickable"));
await settle();

await type(live("wh-url"), "https://shop.example.com/paybroker");
await type(live("ms-wa"), "250");
await type(live("ms-region"), "EU");
await type(live("key-name"), "checkout server");
await fire(up(live("key-name"), "form"), "submit");
await settle();
check(hasText("sk_test_ONLY_SHOWN_ONCE"), "the new key was not shown");
await click(allByText(document.body, "button", "Revoke")[1]);        // revoke key 2
await settle();
check(live("wh-url")?.value === "https://shop.example.com/paybroker", `the webhook URL was lost (${JSON.stringify(live("wh-url")?.value)})`);
check(live("ms-wa")?.value === "250" && live("ms-region")?.value === "EU", "the routing settings being edited were lost");
check(hasText("sk_test_ONLY_SHOWN_ONCE"), "the key that is shown once was wiped by the revoke");
check(hasText("revoked"), "the key table was not refreshed");

await click(byText(document.body, "button", "Suspend"));
await settle();
check(live("wh-url")?.value === "https://shop.example.com/paybroker" && live("ms-wa")?.value === "250", "suspending the merchant wiped the forms");
done("revoking a key or suspending keeps the forms and the one-time key");
