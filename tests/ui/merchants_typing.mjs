// Regression: revoking a key (or suspending the merchant) reloaded the whole merchant window: the webhook URL and the routing
// settings being edited were lost, and so was a freshly created key, which is shown only once. The allowed-origins box is covered too.  Run: node tests/ui/merchants_typing.mjs
import { allByText, byText, calls, check, click, done, failWith, fire, hasText, live, loginAs, route, settle, shell, type, up } from "./fakedom.mjs";

shell(["view-merchants"]);
await loginAs(["*"]);
let revoked = false;
const merchant = () => ({ id: "mch_1", name: "Acme", status: "active", webhook_url: null, created_at: "2026-05-01T10:00:00Z", allowed_origins: ["https://shop.example.com"],
  settings: { preferred_psps: [], max_fee_bps: null, weights: { auth: 100, cost: 30, latency: 5 }, require_3ds_over_cents: null, region: null, capture_method: "manual", allowed_currencies: ["USD"] },
  keys: [{ id: 1, kind: "secret", name: "a", prefix: "sk_test_aaaa", last_used_at: null, revoked_at: null }, { id: 2, kind: "secret", name: "b", prefix: "sk_test_bbbb", last_used_at: null, revoked_at: revoked ? "2026-05-02T00:00:00Z" : null }] });
route({
  "GET /api/merchants": { items: [{ id: "mch_1", name: "Acme", status: "active", webhook_url: null, payments: 0, created_at: "2026-05-01T10:00:00Z" }] },
  "GET /api/merchants/mch_1": () => merchant(),
  "GET /api/connectors": { items: [{ name: "stripe" }] },
  "GET /api/webhook-events": { items: [] },
  "POST /api/merchants/mch_1/keys": { id: 3, key: "sk_test_ONLY_SHOWN_ONCE", kind: "secret" },
  "DELETE /api/merchants/mch_1/keys/2": () => { revoked = true; return { revoked: 2 }; },
  "PATCH /api/merchants/mch_1": (init) => { const b = JSON.parse(init.body); return b.allowed_origins?.includes("https://bad.example.com/x") ? failWith(422, "https://bad.example.com/x: an origin has no path") : { id: "mch_1", allowed_origins: b.allowed_origins }; },
});
const { initMerchants } = await import("/static/merchants.js");
await initMerchants();
await settle();
await click(document.body._all().find((e) => e.tag === "tr" && e.className === "clickable"));
await settle();

await type(live("wh-url"), "https://shop.example.com/paybroker");
await type(live("ms-wa"), "250");
await type(live("ms-region"), "EU");
check(live("ms-origins")?.value === "https://shop.example.com", `the saved origins are not shown (${JSON.stringify(live("ms-origins")?.value)})`);
await type(live("ms-origins"), "https://shop.example.com\nhttp://localhost:3000");
await type(live("key-name"), "checkout server");
await fire(up(live("key-name"), "form"), "submit");
await settle();
check(hasText("sk_test_ONLY_SHOWN_ONCE"), "the new key was not shown");
await click(allByText(document.body, "button", "Revoke")[1]);        // revoke key 2
await settle();
check(live("wh-url")?.value === "https://shop.example.com/paybroker", `the webhook URL was lost (${JSON.stringify(live("wh-url")?.value)})`);
check(live("ms-wa")?.value === "250" && live("ms-region")?.value === "EU", "the routing settings being edited were lost");
check(live("ms-origins")?.value === "https://shop.example.com\nhttp://localhost:3000", "the origins being edited were lost");
check(hasText("sk_test_ONLY_SHOWN_ONCE"), "the key that is shown once was wiped by the revoke");
check(hasText("revoked"), "the key table was not refreshed");

await click(byText(document.body, "button", "Suspend"));
await settle();
check(live("wh-url")?.value === "https://shop.example.com/paybroker" && live("ms-wa")?.value === "250", "suspending the merchant wiped the forms");
check(live("ms-origins")?.value === "https://shop.example.com\nhttp://localhost:3000", "suspending the merchant wiped the origins");

await fire(up(live("ms-origins"), "form"), "submit");                 // the whole text box is saved as a list, one origin per line
await settle();
const saved = calls.filter((c) => c.key === "PATCH /api/merchants/mch_1").at(-1);
check(JSON.stringify(saved?.body) === JSON.stringify({ allowed_origins: ["https://shop.example.com", "http://localhost:3000"] }), `wrong save ${JSON.stringify(saved?.body)}`);
await type(live("ms-origins"), "https://bad.example.com/x");
await fire(up(live("ms-origins"), "form"), "submit");
await settle();
check(hasText("an origin has no path") && live("ms-origins")?.value === "https://bad.example.com/x", "a refused list must show why and keep what was typed");
done("revoking a key or suspending keeps the forms, the origins box and the one-time key");
