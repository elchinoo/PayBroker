import { $, copy, fill, h } from "/kit/ui.js";

const SNIPPET = (url) => `export PAYBROKER_URL=${url}
export PB_PUBLISHABLE_KEY=pk_test_...   # browser-safe: can only exchange a test card for a token
export PB_SECRET_KEY=sk_test_...        # server only

# 1. exchange a TEST card for a token (from the customer's browser; the number never touches your server)
curl -s -X POST "$PAYBROKER_URL/v1/vault/tokens" -H "Authorization: Bearer $PB_PUBLISHABLE_KEY" -H "Content-Type: application/json" \\
  -d '{"number":"4242424242424242","exp_month":12,"exp_year":2035,"cvc":"123"}'

# 2. authorize (safe to repeat with the same idempotency_key)
curl -s -X POST "$PAYBROKER_URL/v1/payments/authorize" -H "Authorization: Bearer $PB_SECRET_KEY" -H "Content-Type: application/json" -d '{
  "idempotency_key": "order-1001", "amount": 4999, "currency": "USD",
  "payment_method": {"type": "card", "card_token": "tok_vault_card_..."},
  "customer": {"id": "cus_42", "email": "pat@example.com", "ip_address": "198.51.100.20", "account_age_hours": 4000},
  "telemetry": {"device_fingerprint": "dev_abc", "checkout_duration_ms": 41000, "pasted_card_number": false},
  "shipping_address": {"country": "US"}}'

# 3. capture, void or refund
curl -s -X POST "$PAYBROKER_URL/v1/payments/pay_.../capture" -H "Authorization: Bearer $PB_SECRET_KEY"`;

const MCP = (url) => `claude mcp add --transport http paybroker ${url}/mcp --header "Authorization: Bearer $PAYBROKER_TOKEN"
# PAYBROKER_TOKEN: a read-only operator token (Settings, Operator tokens). The tools only read.`;

export function initConnect() {
  const box = $("view-connect");
  const url = location.origin;
  fill(box,
    h("h1", {}, "Connect"),
    h("h2", { class: "section-title" }, "Take a payment"),
    h("p", { class: "muted" }, "Create a merchant, then its keys under Merchants. The vault accepts only the public processor test cards (4242 4242 4242 4242 approves; 4000 0000 0000 0002 is declined by the issuer; 4000 0025 0000 3155 demands 3-D Secure; 4000 0000 0000 0119 makes the first connector fail so the cascade recovers) and numbers in the reserved test range 999999. It never stores a real card number, and never stores any card number or CVC at all."),
    h("div", { class: "codewrap" }, h("pre", { class: "code" }, h("code", { id: "pay-snippet" }, SNIPPET(url))), h("button", { class: "secondary small", type: "button", onclick: () => copy(SNIPPET(url)) }, "Copy")),
    h("p", { class: "muted" }, "Responses: 200 with the payment; 402 CARD_DECLINED; 403 FRAUD_REJECTED (with the score and reasons); 200 REQUIRES_ACTION with a 3-D Secure redirect_url; 422 for bad input or a reused idempotency key; 502 if no connector could answer. Webhooks (payment.authorized, .captured, .declined, .refunded, ...) are signed; see Merchants."),
    h("h2", { class: "section-title" }, "Investigate with an AI assistant"),
    h("p", { class: "muted" }, "PayBroker is also an MCP server with read-only tools: overview, search payments, explain a payment, list fraud cases, list rules and suggestions. An assistant can investigate; only a person can decide."),
    h("div", { class: "codewrap" }, h("pre", { class: "code" }, h("code", {}, MCP(url))), h("button", { class: "secondary small", type: "button", onclick: () => copy(MCP(url)) }, "Copy")),
    h("p", { class: "muted" }, "Prometheus: scrape ", h("code", {}, `${url}/metrics`), " with a metrics-only token. The standard families (http_requests_total, db_up, ...) plus payment_broker_* are exposed."));
}
