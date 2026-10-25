import { api } from "/kit/api.js";
import { fmtDate, $, copy, field, fill, h, toast } from "/kit/ui.js";
import { can } from "/kit/session.js";
import { renderMaintenance } from "/static/maintenance.js";
import { renderModel } from "/static/model.js";
import { renderTriageLimits } from "/static/triage.js";

export async function initSettings() {
  const box = $("view-settings");
  fill(box, h("h1", {}, "Settings"), h("div", { id: "set-accounts" }), can("settings:manage") ? h("div", { id: "set-triage", class: "settings-block" }) : null, h("div", { id: "set-model", class: "settings-block" }), h("div", { id: "set-security", class: "settings-block" }), h("div", { id: "set-demo", class: "settings-block" }), can("data:reset") ? h("div", { id: "set-maintenance", class: "settings-block" }) : null);
  renderAccounts();
  if (can("settings:manage")) renderTriageLimits($("set-triage"));
  renderModel($("set-model"));
  renderSecurity();
  renderDemo();
  if (can("data:reset")) renderMaintenance($("set-maintenance"));
}

// ---- LLM accounts ---------------------------------------------------------------------------------
// The block is built once; saving or deleting an account refreshes the table and the selector's options in place, so a
// triage account the person has picked but not saved yet is not reset by an unrelated change.
let accountsNow = null, firstChoice = false;

async function renderAccounts() {
  const box = $("set-accounts");
  let acc;
  try { acc = await api("/api/accounts"); } catch (err) { fill($("acc-table") || box, h("p", { class: "error" }, err.message)); return; }
  if (!$("acc-table")) buildAccounts(box, acc);
  paintAccounts(acc);
}

function buildAccounts(box, acc) {
  const sel = h("select", { id: "triage-select", "aria-label": "Account for AI triage" });
  fill(box,
    h("h2", {}, "AI accounts"),
    h("p", { class: "muted" }, "Tier 3 asks a language model to triage flagged payments and to suggest new rules. It is optional: an account provided by the environment (the shared LLM gateway) is used by default; with no account at all the broker uses a built-in rule-based triage. The model is sent only derived features and reason codes, never card data, names or e-mail addresses. Keys are stored encrypted."),
    h("div", { class: "btns" }, h("button", { class: "primary", type: "button", onclick: () => editAccount(accountsNow, null) }, "Add an account")),
    h("div", { id: "acc-table" }),
    h("div", { class: "card" }, h("div", { class: "grid-inline" }, field("Account for AI triage and rule suggestions", sel),
      h("button", { class: "primary", type: "button", onclick: async () => { try { await api("/api/triage-account", { method: "PUT", json: { account_id: sel.value === "env" ? "env" : sel.value ? Number(sel.value) : null } }); toast("Saved"); } catch (e) { toast(e.message, true); } } }, "Save"))));
  firstChoice = true;
}

function paintAccounts(acc) {
  accountsNow = acc;
  const sel = $("triage-select");
  const keep = firstChoice ? String(acc.triage_account ?? "") : sel.value;      // after the first paint the person's own choice wins
  firstChoice = false;
  const options = [h("option", { value: "" }, "None (use the built-in rule-based triage)"),
    ...[...(acc.env && acc.env.chat_model && !acc.env.error ? [acc.env] : []), ...acc.items.filter((a) => a.chat_model)].map((a) => h("option", { value: a.id }, `${a.name} · ${a.chat_model}`))];
  fill(sel, options);
  sel.value = options.some((o) => o.value === keep) ? keep : String(acc.triage_account ?? "");
  const test = (a) => async (e) => {
    const out = $(`acc-test-${a.id}`);
    e.target.disabled = true;
    out.className = "muted";
    out.textContent = "Testing…";
    try { const r = await api(`/api/accounts/${a.id}/test`, { method: "POST", json: { target: "chat" } }); out.className = "ok"; out.textContent = `Chat works (${r.ms} ms). Reply: "${r.reply}"`; }
    catch (err) { out.className = "error"; out.textContent = err.message; }   // the provider's own reason, verbatim
    finally { e.target.disabled = false; }
  };
  const testCell = (a) => h("td", {}, a.chat_model && !a.error ? h("button", { class: "secondary small", type: "button", onclick: test(a) }, "Test chat") : null, " ", h("span", { id: `acc-test-${a.id}`, role: "status" }));
  const envRow = acc.env ? [h("tr", {}, h("td", { class: "title" }, acc.env.name), h("td", {}, h("span", { class: "badge plain" }, "from environment")),
    h("td", {}, acc.env.chat_model || h("span", { class: "muted" }, "none (set LLM_CHAT_MODEL)")),
    h("td", {}, acc.env.has_key ? "set by the server" : h("span", { class: "muted" }, "no key")),
    h("td", { class: "actions" }, testCell(acc.env), acc.env.error ? h("span", { class: "error" }, acc.env.error) : null))] : [];
  const list = [...envRow, ...acc.items.map((a) => h("tr", {}, h("td", { class: "title" }, a.name), h("td", {}, a.provider), h("td", {}, a.chat_model || h("span", { class: "muted" }, "none")),
    h("td", {}, a.has_key ? `…${a.api_key_last4}` : h("span", { class: "muted" }, "no key")),
    h("td", { class: "actions" }, testCell(a), " ", h("button", { class: "secondary small", type: "button", onclick: () => editAccount(acc, a) }, "Edit"), " ",
      h("button", { class: "danger small", type: "button", onclick: async () => { if (!confirm(`Delete "${a.name}"?`)) return; try { await api(`/api/accounts/${a.id}`, { method: "DELETE" }); renderAccounts(); } catch (e) { toast(e.message, true); } } }, "Delete"))))];
  fill($("acc-table"), list.length ? h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Name", "Provider", "Chat model", "Key", ""].map((t) => h("th", {}, t)))), h("tbody", {}, list))) : h("p", { class: "hint" }, "No accounts yet."));
}

function editAccount(acc, a) {
  const preset = h("select", { id: "acc-preset" }, ...acc.presets.map((p) => h("option", { value: p.id }, p.name)));
  const name = h("input", { id: "acc-name", maxlength: "60", required: true, value: a?.name || "" });
  const url = h("input", { id: "acc-url", type: "url", maxlength: "300", required: true, value: a?.base_url || acc.presets[0].base_url });
  const model = h("input", { id: "acc-model", maxlength: "100", value: a?.chat_model || "", placeholder: "e.g. gpt-4o-mini" });
  const key = h("input", { id: "acc-key", type: "password", autocomplete: "off", maxlength: "500", placeholder: a?.has_key ? "leave blank to keep the stored key" : "" });
  const clear = h("input", { id: "acc-clear", type: "checkbox" });
  const err = h("p", { class: "error", role: "alert" });
  if (a) preset.value = a.provider;
  preset.addEventListener("change", () => { const p = acc.presets.find((x) => x.id === preset.value); if (p?.base_url) url.value = p.base_url; });
  fill($("form-body"), h("h2", {}, a ? "Edit account" : "Add an AI account"), h("form", { onsubmit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    const body = { name: name.value, provider: preset.value, base_url: url.value, api_key: key.value || null, clear_key: clear.checked, chat_model: model.value || null };
    try { await api(a ? `/api/accounts/${a.id}` : "/api/accounts", { method: a ? "PUT" : "POST", json: body }); key.value = ""; $("form").close(); toast("Saved"); renderAccounts(); } catch (ex) { err.textContent = ex.message; }
  } }, field("Provider", preset), field("Name", name), field("Base URL (OpenAI-compatible)", url), field("Chat model", model), field("API key", key),
  a?.has_key ? h("label", { class: "check" }, clear, " Remove the stored key") : null, h("button", { class: "primary", type: "submit" }, "Save"), err));
  $("form").showModal();
}

// ---- security -----------------------------------------------------------------------------------
async function renderSecurity() {
  const box = $("set-security");
  let tokens, session;
  try { [tokens, session] = await Promise.all([api("/api/tokens"), api("/api/session")]); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  const reveal = h("div", { hidden: true });
  const tName = h("input", { id: "tk-name", maxlength: "60", required: true, placeholder: "e.g. Claude Code on my laptop" });
  const tScope = h("select", { id: "tk-scope" }, h("option", { value: "read" }, "Read-only (dashboards and MCP; recommended)"), h("option", { value: "metrics" }, "Metrics only (scrape /metrics)"), h("option", { value: "full" }, "Full operator access"));
  const tErr = h("p", { class: "error", role: "alert" });
  const table = h("div", {});
  async function renderTable() {
    const list = await api("/api/tokens");
    fill(table, list.length ? h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Name", "Token", "Access", "Created", "Last used", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, list.map((t) => h("tr", {}, h("td", { class: "title" }, t.name), h("td", {}, h("code", {}, `${t.prefix}…`)), h("td", {}, t.scope), h("td", {}, fmtDate(t.created_at)), h("td", {}, fmtDate(t.last_used_at)),
        h("td", { class: "actions" }, t.revoked_at ? h("span", { class: "muted" }, "revoked") : h("button", { class: "danger small", type: "button", onclick: async () => {
          if (!confirm(`Revoke "${t.name}"? Clients using it stop working immediately.`)) return;
          try { await api(`/api/tokens/${t.id}`, { method: "DELETE" }); toast("Token revoked"); renderTable(); } catch (err) { toast(err.message, true); }
        } }, "Revoke"))))))) : h("p", { class: "hint" }, "No tokens yet."));
  }
  const tokenForm = h("form", { class: "grid-inline", onsubmit: async (e) => {
    e.preventDefault();
    tErr.textContent = "";
    try {
      const t = await api("/api/tokens", { method: "POST", json: { name: tName.value, scope: tScope.value } });
      tName.value = "";
      reveal.replaceChildren(h("p", { class: "warn-text" }, "Copy this token now. It is shown only once; only a hash is stored."),
        h("div", { class: "codewrap" }, h("pre", { class: "code" }, h("code", {}, t.token)), h("button", { class: "secondary small", type: "button", onclick: () => copy(t.token, "Token copied") }, "Copy")));
      reveal.hidden = false;
      renderTable();
    } catch (ex) { tErr.textContent = ex.message; }
  } }, field("Name", tName), field("Access", tScope), h("button", { class: "primary", type: "submit" }, "Create token"));

  const has = session.has_admin_password;
  const cur = h("input", { id: "pw-cur", type: "password", autocomplete: "current-password" });
  const nw = h("input", { id: "pw-new", type: "password", autocomplete: "new-password", minlength: "12", required: true });
  const nw2 = h("input", { id: "pw-new2", type: "password", autocomplete: "new-password", minlength: "12", required: true });
  const pErr = h("p", { class: "error", role: "alert" });
  const pwForm = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    pErr.textContent = "";
    if (nw.value !== nw2.value) { pErr.textContent = "The new passwords do not match."; return; }
    try {
      await api("/api/admin-password", { method: "POST", json: { current: has ? cur.value : null, new: nw.value } });
      cur.value = nw.value = nw2.value = "";
      toast("Password saved. You were signed out everywhere.");
      window.dispatchEvent(new CustomEvent("apg-auth-lost"));
    } catch (ex) { pErr.textContent = ex.message; }
  } }, has ? field("Current password", cur) : null, field(has ? "New password (12+ characters)" : "Admin password (12+ characters)", nw), field("Repeat", nw2),
  h("button", { class: "primary", type: "submit" }, has ? "Change password" : "Set admin password"), pErr);

  fill(box, h("h2", {}, "Security"),
    h("h3", {}, "Admin password"),
    h("p", { class: "muted" }, has ? "Changing it signs every browser out and re-protects the stored keys and webhook secrets." : "No admin password is set. Set one to store AI keys and webhook secrets encrypted, and to sign in without the server's API key."),
    h("div", { class: "card" }, pwForm),
    h("h3", {}, "Operator tokens"),
    h("p", { class: "muted" }, "For dashboards, MCP clients and Prometheus. Merchants use their own keys under Merchants; those can never reach this API."),
    h("div", { class: "card" }, tokenForm, tErr), reveal, table);
  renderTable();
}

// ---- demo traffic ---------------------------------------------------------------------------------
function renderDemo() {
  const count = h("input", { id: "demo-count", type: "number", min: "1", max: "500", value: "120" });
  const seed = h("input", { id: "demo-seed", type: "number", min: "0", value: "7" });
  const out = h("p", { class: "muted", role: "status" });
  fill($("set-demo"), h("h2", {}, "Demo traffic"),
    h("p", { class: "muted" }, "Sends fictional payments through the real pipeline for a merchant called Demo Shop: ordinary customers, issuer declines, 3-D Secure, card testing and account takeover. Everything is a test card and a documentation IP address."),
    h("form", { class: "card grid-inline", onsubmit: async (e) => {
      e.preventDefault();
      out.textContent = "Sending…";
      try { const r = await api("/api/demo/traffic", { method: "POST", json: { count: Number(count.value), seed: Number(seed.value) } }); out.textContent = `Sent ${r.sent}: ${Object.entries(r.outcomes).map(([k, v]) => `${v} ${k}`).join(", ")}.${r.secret_key ? ` Demo Shop secret key (shown once): ${r.secret_key}` : ""}`; } catch (ex) { out.textContent = ex.message; }
    } }, field("Payments", count), field("Seed (same seed, same traffic)", seed), h("button", { class: "primary", type: "submit" }, "Send")), out);
}
