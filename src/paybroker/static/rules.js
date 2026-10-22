import { api } from "/kit/api.js";
import { can } from "/kit/session.js";
import { $, field, fill, fmtDate, h, toast } from "/kit/ui.js";

const EXAMPLE = `{"all": [
  {"field": "checkout_seconds", "op": "<", "value": 5},
  {"field": "amount", "op": ">=", "value": 50000}
]}`;

export async function initRules() {
  const box = $("view-rules");
  fill(box, h("p", { class: "loading" }, "Loading…"));
  let rules, sugg, black;
  try { [rules, sugg, black] = await Promise.all([api("/api/rules"), api("/api/suggestions?status=pending"), api("/api/blacklist")]); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  const manage = can("rules:manage"), review = can("cases:review");
  const reload = () => initRules();

  const ruleRows = rules.items.map((r) => h("tr", {},
    h("td", { class: "title" }, r.name, r.description ? h("div", { class: "muted small-text" }, r.description) : null),
    h("td", {}, h("span", { class: `badge ${r.action === "decline" ? "bad" : "warn"}` }, r.action)),
    h("td", { class: "wrap" }, h("code", {}, JSON.stringify(r.definition))),
    h("td", {}, r.created_by), h("td", {}, r.enabled ? "on" : h("span", { class: "muted" }, "off")),
    h("td", { class: "actions" }, manage ? [
      h("button", { class: "secondary small", type: "button", onclick: async () => { try { await api(`/api/rules/${r.id}`, { method: "PATCH", json: { enabled: !r.enabled } }); reload(); } catch (e) { toast(e.message, true); } } }, r.enabled ? "Turn off" : "Turn on"),
      " ", h("button", { class: "danger small", type: "button", onclick: async () => { if (!confirm(`Delete rule "${r.name}"?`)) return; try { await api(`/api/rules/${r.id}`, { method: "DELETE" }); reload(); } catch (e) { toast(e.message, true); } } }, "Delete")] : null)));

  const name = h("input", { id: "rule-name", maxlength: "80", required: true, placeholder: "e.g. rushed_large_basket" });
  const action = h("select", { id: "rule-action" }, h("option", { value: "challenge" }, "Ask for 3-D Secure"), h("option", { value: "decline" }, "Decline"));
  const desc = h("input", { id: "rule-desc", maxlength: "300", placeholder: "Why this rule exists" });
  const def = h("textarea", { id: "rule-def", rows: "5", spellcheck: "false", required: true }, EXAMPLE);
  const err = h("p", { class: "error", role: "alert" });
  const form = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    let definition;
    try { definition = JSON.parse(def.value); } catch { err.textContent = "The definition is not valid JSON."; return; }
    try { await api("/api/rules", { method: "POST", json: { name: name.value, description: desc.value, definition, action: action.value } }); toast("Rule added"); reload(); } catch (ex) { err.textContent = ex.message; }
  } }, h("h3", {}, "Add a rule"),
  h("p", { class: "muted" }, `A rule is data, never code: all/any groups of comparisons (==, !=, >, >=, <, <=, in, not_in). Fields: ${rules.fields.join(", ")}.`),
  h("div", { class: "grid-inline" }, field("Name", name), field("Action", action), field("Description", desc)), field("Definition (JSON)", def), h("button", { class: "primary", type: "submit" }, "Add rule"), err);

  const suggRows = sugg.items.map((s) => h("div", { class: "card" }, h("div", { class: "row" }, h("h3", {}, s.name), h("span", { class: "badge alt" }, s.source === "llm" ? "proposed by the AI" : "from the statistics")),
    h("p", {}, s.rationale || ""), h("code", { class: "json" }, JSON.stringify(s.definition)), h("p", { class: "muted" }, `would ${s.action}; evidence: ${JSON.stringify(s.evidence)}`),
    manage ? h("div", { class: "btns" },
      h("button", { class: "primary", type: "button", onclick: async () => { try { await api(`/api/suggestions/${s.id}/decide`, { method: "POST", json: { approve: true } }); toast("Rule approved and active"); reload(); } catch (e) { toast(e.message, true); } } }, "Approve"),
      h("button", { class: "secondary", type: "button", onclick: async () => { try { await api(`/api/suggestions/${s.id}/decide`, { method: "POST", json: { approve: false } }); reload(); } catch (e) { toast(e.message, true); } } }, "Reject")) : null));

  const kind = h("select", { id: "bl-kind" }, ...["ip", "device", "card_fingerprint", "bin", "email_domain"].map((k) => h("option", { value: k }, k)));
  const value = h("input", { id: "bl-value", maxlength: "200", required: true });
  const blForm = h("form", { class: "grid-inline", onsubmit: async (e) => {
    e.preventDefault();
    try { await api("/api/blacklist", { method: "POST", json: { kind: kind.value, value: value.value, reason: "added by an operator" } }); reload(); } catch (ex) { toast(ex.message, true); }
  } }, field("Kind", kind), field("Value", value), h("button", { class: "primary", type: "submit" }, "Block"));

  fill(box,
    h("h1", {}, "Rules"),
    h("p", { class: "muted" }, "Tier 1 runs in a few milliseconds before the model. A rule can ask for 3-D Secure or decline; the stricter of the rules and the model's score band wins."),
    h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Rule", "Action", "Definition", "Created by", "State", ""].map((t) => h("th", {}, t)))), h("tbody", {}, ruleRows))),
    manage ? form : null,
    h("div", { class: "row" }, h("h2", { class: "section-title" }, "Suggested rules"),
      manage ? h("button", { class: "secondary", type: "button", onclick: async (e) => { e.target.disabled = true; try { const r = await api("/api/suggestions/run", { method: "POST" }); toast(`${r.created} new suggestion(s) (${r.source})`); reload(); } catch (ex) { toast(ex.message, true); e.target.disabled = false; } } }, "Look for new rules") : null),
    h("p", { class: "muted" }, "Proposed from recent confirmed fraud (by the AI if an account is set, else by simple statistics). Nothing becomes active until a person approves it, and every proposal is validated like a hand-written rule."),
    sugg.items.length ? suggRows : h("p", { class: "empty" }, "No suggestions waiting."),
    h("h2", { class: "section-title" }, "Blocked list"),
    review ? h("div", { class: "card" }, blForm) : null,
    black.items.length ? h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Kind", "Value", "Why", "Source", "Added", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, black.items.map((b) => h("tr", {}, h("td", {}, b.kind), h("td", {}, h("code", {}, b.value)), h("td", { class: "wrap" }, b.reason), h("td", {}, b.source), h("td", {}, fmtDate(b.created_at)),
        h("td", { class: "actions" }, review ? h("button", { class: "danger small", type: "button", onclick: async () => { try { await api(`/api/blacklist/${b.id}`, { method: "DELETE" }); reload(); } catch (e) { toast(e.message, true); } } }, "Unblock") : null))))))
      : h("p", { class: "empty" }, "Nothing is blocked."));
}
