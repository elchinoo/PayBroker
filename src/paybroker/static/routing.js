import { api } from "/kit/api.js";
import { can } from "/kit/session.js";
import { $, fill, h, toast } from "/kit/ui.js";
import { flushCard, headCell } from "/static/common.js";

export async function initRouting() {
  const box = $("view-routing");
  fill(box, h("p", { class: "loading" }, "Loading…"));
  let list;
  try { list = (await api("/api/connectors")).items; } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  fill(box,
    h("h1", {}, "Routing"),
    h("p", { class: "muted" }, "Each payment goes to the eligible connector with the highest score S = W_auth × authorization rate − W_cost × fee − W_latency × latency, using the live (smoothed) numbers below. If a connector times out or fails with a 5xx, the next best is tried within an 800 ms budget; an issuer decline is final. All connectors here are simulations."),
    flushCard("Connectors", h("table", {}, h("thead", {}, h("tr", {}, ...["Connector", "Currencies", "Regions", "Fee", "Auth rate (live)", "Latency (live)", "Attempts", "On"].map(headCell))),
      h("tbody", {}, list.map((c) => h("tr", {}, h("td", { class: "title" }, c.label, h("div", { class: "muted small-text" }, c.name)), h("td", { class: "wrap" }, c.currencies.join(" ")), h("td", {}, c.regions.join(" ")),
        h("td", {}, `${(c.fee_bps / 100).toFixed(2)}%${c.fixed_fee_cents ? ` + ${c.fixed_fee_cents}c` : ""}`), h("td", {}, `${(Number(c.auth_ewma) * 100).toFixed(1)}%`), h("td", {}, `${Number(c.latency_ewma).toFixed(0)} ms`),
        h("td", {}, `${c.successes}/${c.attempts}`),
        h("td", { class: "actions" }, can("merchants:manage") ? h("button", { class: c.enabled ? "secondary small" : "primary small", type: "button", onclick: async () => {
          try { await api(`/api/connectors/${c.name}`, { method: "PATCH", json: { enabled: !c.enabled } }); initRouting(); } catch (e) { toast(e.message, true); }
        } }, c.enabled ? "Disable" : "Enable") : (c.enabled ? "yes" : "no"))))))),
    h("p", { class: "hint" }, "Per-merchant preferences (preferred connectors, maximum fee, weights, data-residency region, 3-D Secure threshold) are under Merchants."));
}
