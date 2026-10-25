import { api } from "/kit/api.js";
import { $, fill, h } from "/kit/ui.js";

const id = location.pathname.split("/").pop();

async function main() {
  const box = $("box");
  let c;
  try { c = await api(`/api/3ds/${encodeURIComponent(id)}`); } catch (err) { fill(box, h("h1", {}, "Challenge not found"), h("p", { class: "error" }, err.message)); return; }
  if (c.status !== "pending") { fill(box, h("h1", {}, "Already completed"), h("p", { class: "muted" }, `This challenge is ${c.status}.`)); return; }
  const done = async (outcome) => {
    try {
      const r = await api(`/api/3ds/${encodeURIComponent(id)}/complete`, { method: "POST", json: { outcome } });
      if (r.return_url) {   // the shop asked to get the customer back; the server signed the link (see API.md)
        fill(box, h("h1", {}, "Returning to the merchant"), h("p", { class: "muted" }, "One moment…"), h("p", {}, h("a", { href: r.return_url }, "Continue")));
        location.assign(r.return_url);
        return;
      }
      fill(box, h("h1", {}, outcome === "succeeded" ? "Verified" : "Not verified"), h("p", {}, `Payment ${r.payment_id}: ${r.status}.`), h("p", { class: "muted" }, "You can close this page. The merchant receives the result by webhook."));
    } catch (err) { fill(box, h("h1", {}, "Could not complete"), h("p", { class: "error" }, err.message)); }
  };
  fill(box,
    h("img", { class: "hero-logo", src: "/static/logo.svg", alt: "", width: "72", height: "72" }),
    h("h1", {}, "Verify this payment"),
    h("p", {}, `${c.merchant} wants to charge ${(c.amount / 100).toFixed(2)} ${c.currency}.`),
    h("p", { class: "warn-text" }, "Simulated issuer page (test mode). A real issuer would ask for a code or biometric check; here you choose the outcome."),
    h("div", { class: "btns" }, h("button", { class: "primary", type: "button", onclick: () => done("succeeded") }, "Approve (verification succeeds)"),
      h("button", { class: "secondary", type: "button", onclick: () => done("failed") }, "Fail verification")));
}
main();
