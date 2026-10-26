import { api } from "/kit/api.js";
import { $, fill, fmtDateTime, h, toast } from "/kit/ui.js";
import { ACTION, cents } from "/static/common.js";
import { caseCard, openPayment } from "/static/payments.js";

let wired = false;

export function initCases() {
  if (!wired) {
    wired = true;
    $("case-status").addEventListener("change", load);
  }
  load();
}

async function load() {
  const box = $("case-list");
  if (!box.children.length) fill(box, h("p", { class: "loading" }, "Loading…"));   // a reload keeps what is on screen until the new list arrives
  let items;
  try { items = (await api(`/api/cases?status=${$("case-status").value}`)).items; } catch (err) { toast(err.message, true); return; }
  if (!items.length) { fill(box, h("p", { class: "empty" }, "Nothing here. Flagged payments appear as soon as a score reaches 700.")); return; }
  fill(box, items.map((c) => h("div", { class: `card case${c.status === "resolved" ? " resolved" : ""}` },
    h("div", { class: "row" },
      h("h2", {}, h("button", { class: "linklike", type: "button", onclick: () => openPayment(c.payment_id, load) }, c.payment_id), `  ${cents(c.amount, c.currency)}`),
      h("span", { class: "badge bad" }, `score ${c.fraud_score}`)),
    h("p", { class: "muted" }, `${fmtDateTime(c.created_at)} · merchant ${c.merchant_id} · ${(c.reasons || []).join(", ") || "no reasons recorded"}`),
    c.ai_summary ? h("div", { class: "ai-box" }, h("strong", {}, `${(c.ai_category || "").replaceAll("_", " ")}: `), c.ai_summary, " ",
      h("em", {}, `Recommends ${ACTION[c.ai_recommendation] || c.ai_recommendation}.`))
      : h("p", { class: "muted" }, c.tier3_state === "queued" ? "AI triage is queued." : "No AI triage yet."),
    caseCard({ id: c.id, status: c.status, analyst_decision: c.analyst_decision, note: c.note, resolved_by: c.resolved_by, resolved_at: null }, load))));
}
