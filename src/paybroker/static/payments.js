import { api } from "/kit/api.js";
import { can } from "/kit/session.js";
import { $, badge, fill, fmtDateTime, h, toast } from "/kit/ui.js";
import { ACTION, cents, DECISION, gauge, PAYMENT, pills } from "/static/common.js";

const PAGE = 25;
let wired = false;
let offset = 0;
let rows = [];

export function initPayments() {
  if (!wired) {
    wired = true;
    for (const s of Object.keys(PAYMENT)) $("pay-status").append(h("option", { value: s }, PAYMENT[s][0]));
    let timer;
    $("pay-q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => load(true), 250); });
    for (const id of ["pay-status", "pay-decision", "pay-score"]) $(id).addEventListener("change", () => load(true));
    $("pay-more").addEventListener("click", () => load(false));
  }
  load(true);
}

async function load(reset) {
  if (reset) { offset = 0; rows = []; }
  const p = new URLSearchParams({ limit: PAGE, offset });
  for (const [k, id] of [["q", "pay-q"], ["status", "pay-status"], ["decision", "pay-decision"], ["min_score", "pay-score"]]) if ($(id).value) p.set(k, $(id).value);
  let page;
  try { page = (await api(`/api/payments?${p}`)).items; } catch (err) { toast(err.message, true); return; }
  rows = rows.concat(page);
  offset += page.length;
  fill($("pay-body"), rows.map((r) => h("tr", { class: "clickable", tabindex: "0", onclick: () => openPayment(r.payment_id), onkeydown: (e) => { if (e.key === "Enter") openPayment(r.payment_id); } },
    h("td", {}, h("code", {}, r.payment_id), r.last4 ? h("div", { class: "muted small-text" }, `${r.brand} •••• ${r.last4}`) : null),
    h("td", {}, r.merchant), h("td", { class: "num" }, cents(r.amount, r.currency)),
    h("td", {}, badge(PAYMENT, r.status), r.case_status === "open" ? h("span", { class: "badge bad" }, "case") : null),
    h("td", { class: "num" }, h("span", { class: `badge ${r.fraud_decision === "DECLINE" ? "bad" : r.fraud_decision === "CHALLENGE" ? "warn" : "ok"}` }, String(r.fraud_score))),
    h("td", {}, r.psp_selected || h("span", { class: "muted" }, "none")), h("td", { class: "col-added" }, fmtDateTime(r.created_at)))));
  $("pay-empty").hidden = rows.length > 0;
  $("pay-more").hidden = page.length < PAGE;
  $("pay-total").textContent = rows.length ? `${rows.length} shown` : "";
}

function facts(pairs) {
  return h("dl", { class: "facts compact" }, pairs.filter(([, v]) => v != null && v !== "").flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}

export async function openPayment(id, onChange) {
  const dlg = $("detail");
  fill($("detail-body"), h("p", { class: "loading" }, "Loading…"));
  if (!dlg.open) dlg.showModal();
  let d;
  try { d = await api(`/api/payments/${encodeURIComponent(id)}`); } catch (err) { fill($("detail-body"), h("p", { class: "error" }, err.message)); return; }
  const p = d.payment, f = d.fraud;
  const refresh = () => { openPayment(id, onChange); if (onChange) onChange(); };
  const parts = [
    h("div", { class: "row" }, h("h2", {}, h("code", {}, p.payment_id)), badge(PAYMENT, p.status)),
    h("div", { class: "cols" },
      h("div", { class: "card" }, h("h3", {}, "Payment"), facts([
        ["Merchant", p.merchant], ["Amount", cents(p.amount, p.currency)], ["Captured / refunded", `${cents(p.amount_captured, p.currency)} / ${cents(p.amount_refunded, p.currency)}`],
        ["Card", p.last4 ? `${p.brand} •••• ${p.last4} (BIN ${p.bin}, exp ${p.exp_month}/${p.exp_year})` : ""], ["Customer", p.customer_id],
        ["Capture", p.capture_method], ["Connector", p.psp_selected], ["Authorization code", p.auth_code], ["Decline code", p.decline_code],
        ["Latency", p.latency_ms == null ? "" : `${p.latency_ms} ms`], ["Created", fmtDateTime(p.created_at)],
      ])),
      f ? h("div", { class: "card" }, h("h3", {}, `Fraud engine: ${f.tier2_ml_score} / 1000`), gauge(f.tier2_ml_score),
        h("p", {}, badge(DECISION, f.decision), " ", h("span", { class: "muted" }, `action: ${f.action_taken}`)),
        f.reasons.length ? h("div", {}, h("h4", {}, "Reasons"), pills(f.reasons)) : h("p", { class: "muted" }, "No risk signals worth reporting."),
        f.tier1_rules_triggered.length ? h("div", {}, h("h4", {}, "Tier 1 rules and filters"), pills(f.tier1_rules_triggered)) : null,
        h("h4", {}, "Timing"), facts([["Fraud engine", `${f.timings.fraud_ms} ms`], ["Feature fetch", `${f.timings.feature_fetch_ms} ms`], ["Tier 1 rules", `${f.timings.tier1_ms} ms`],
          ["Tier 2 model", `${f.timings.ml_inference_ms} ms`], ["Connector round trips", f.timings.psp_ms ? `${f.timings.psp_ms} ms` : ""]])) : null),
    f ? h("details", { class: "card" }, h("summary", {}, "Features the model saw"), facts(Object.entries(f.tier2_feature_snapshot).map(([k, v]) => [k, String(v)]))) : null,
  ];
  if (f?.timings?.attempts?.length) {
    parts.push(h("div", { class: "card" }, h("h3", {}, "Routing"), h("ol", { class: "timeline" }, ...f.timings.attempts.map((a) =>
      h("li", {}, `${a.psp}: ${a.result}${a.code ? ` (${a.code})` : ""} · ${a.ms} ms`)))));
  }
  if (f && f.tier3_state !== "none") {
    const t = f.tier3_llm_analysis;
    parts.push(h("div", { class: "card" }, h("h3", {}, "AI triage"),
      t ? h("div", { class: "ai-box" }, h("p", {}, h("strong", {}, `${t.risk_category.replaceAll("_", " ")} · confidence ${Math.round(t.confidence_score * 100)}%`), " ", h("span", { class: "badge plain" }, t.source === "llm" ? `model: ${t.model || "LLM"}` : "rule-based fallback")),
        h("p", {}, t.summary), pills(t.key_risk_factors), h("p", {}, "Recommends: ", h("strong", {}, ACTION[t.recommended_action] || t.recommended_action)),
        t.suggested_rule_updates ? h("p", { class: "muted" }, `Rule idea: ${t.suggested_rule_updates}`) : null,
        t.note ? h("p", { class: "muted" }, t.note) : null)
        : h("p", { class: "muted" }, f.tier3_state === "queued" ? "Queued: a background worker will analyse this shortly." : "No analysis available."),
      h("p", { class: "hint" }, "The AI never sees card data, names or e-mail addresses. Its advice is only a recommendation.")));
  }
  if (d.case) parts.push(caseCard(d.case, refresh));
  if (d.three_ds) parts.push(h("div", { class: "card" }, h("h3", {}, "3-D Secure"), facts([["Challenge", d.three_ds.id], ["Status", d.three_ds.status], ["Expires", fmtDateTime(d.three_ds.expires_at)]]),
    d.three_ds.status === "pending" ? h("p", {}, h("a", { class: "button", href: `/3ds/${d.three_ds.id}`, target: "_blank", rel: "noopener" }, "Open the simulated issuer page")) : null));
  if (d.chargebacks.length) parts.push(h("div", { class: "card" }, h("h3", {}, "Chargebacks"), h("ul", {}, ...d.chargebacks.map((c) => h("li", {}, `${cents(c.amount, p.currency)}: ${c.reason || "no reason"} (${fmtDateTime(c.created_at)})`)))));
  if (d.webhooks.length) parts.push(h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Webhook event", "Type", "Status", "Attempts", "Last HTTP"].map((t) => h("th", {}, t)))),
    h("tbody", {}, ...d.webhooks.map((w) => h("tr", {}, h("td", {}, h("code", {}, w.event_id)), h("td", {}, w.type), h("td", {}, w.status), h("td", {}, w.attempts), h("td", {}, w.last_code ?? "")))))));
  if (can("cases:review") && ["AUTHORIZED", "CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED"].includes(p.status)) {
    parts.push(h("div", { class: "btns" }, h("button", { class: "secondary", type: "button", onclick: async () => {
      const reason = prompt("Chargeback reason (shown to analysts; the whole amount is charged back):", "customer disputes the charge");
      if (reason === null) return;
      try { await api(`/api/payments/${encodeURIComponent(p.payment_id)}/chargeback`, { method: "POST", json: { reason } }); toast("Chargeback recorded; the feature store was updated"); refresh(); } catch (err) { toast(err.message, true); }
    } }, "Record a chargeback")));
  }
  fill($("detail-body"), ...parts);
}

export function caseCard(c, onDone) {
  if (c.status === "resolved") {
    return h("div", { class: "card case resolved" }, h("h3", {}, `Case #${c.id}: resolved`), facts([["Decision", ACTION[c.analyst_decision] || c.analyst_decision], ["By", c.resolved_by], ["Note", c.note], ["When", fmtDateTime(c.resolved_at)]]));
  }
  const sel = h("select", { id: `case-dec-${c.id}`, "aria-label": "Decision" }, ...Object.entries(ACTION).map(([v, t]) => h("option", { value: v }, t)));
  const note = h("input", { id: `case-note-${c.id}`, maxlength: "300", placeholder: "Note (optional)", "aria-label": "Note" });
  return h("div", { class: "card case" }, h("h3", {}, `Case #${c.id}: waiting for an analyst`),
    can("cases:review") ? h("div", { class: "grid-inline" }, sel, note, h("button", { class: "primary", type: "button", onclick: async () => {
      if (sel.value === "PERMANENT_BLOCK" && !confirm("Block this device, IP address and card from now on?")) return;
      try {
        const r = await api(`/api/cases/${c.id}/resolve`, { method: "POST", json: { decision: sel.value, note: note.value || null } });
        toast(r.blocked.length ? `Resolved. Blocked: ${r.blocked.join(", ")}` : "Case resolved");
        onDone();
      } catch (err) { toast(err.message, true); }
    } }, "Resolve")) : h("p", { class: "muted" }, "You can read cases but not decide them."));
}
