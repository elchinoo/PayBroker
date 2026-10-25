import { api } from "/kit/api.js";
import { can } from "/kit/session.js";
import { $, copy, field, fill, fmtDate, fmtDateTime, h, toast } from "/kit/ui.js";

export async function initMerchants() {
  const box = $("view-merchants");
  fill(box, h("p", { class: "loading" }, "Loading…"));
  let list;
  try { list = await api("/api/merchants"); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  fill(box,
    h("div", { class: "row" }, h("h1", {}, "Merchants"), can("merchants:manage") ? h("button", { class: "primary", type: "button", onclick: newMerchant }, "New merchant") : null),
    h("p", { class: "muted" }, "A merchant is a business that sends payments. Each has secret keys for its server (authorize, capture, refund), publishable keys for the browser (only exchange a test card for a token), routing preferences and an optional signed webhook."),
    h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Merchant", "Id", "Status", "Webhook", "Payments", "Created"].map((t) => h("th", {}, t)))),
      h("tbody", {}, list.items.map((m) => h("tr", { class: "clickable", tabindex: "0", onclick: () => openMerchant(m.id), onkeydown: (e) => { if (e.key === "Enter") openMerchant(m.id); } },
        h("td", { class: "title" }, m.name), h("td", {}, h("code", {}, m.id)), h("td", {}, h("span", { class: `badge ${m.status === "active" ? "ok" : "bad"}` }, m.status)),
        h("td", { class: "wrap" }, m.webhook_url || h("span", { class: "muted" }, "none")), h("td", { class: "num" }, m.payments), h("td", {}, fmtDate(m.created_at)))))),
      list.items.length ? null : h("p", { class: "empty" }, "No merchants yet.")));
}

function newMerchant() {
  const name = h("input", { id: "m-name", maxlength: "100", required: true, placeholder: "e.g. Acme Shop" });
  const err = h("p", { class: "error", role: "alert" });
  fill($("form-body"), h("h2", {}, "New merchant"), h("form", { onsubmit: async (e) => {
    e.preventDefault();
    try { const m = await api("/api/merchants", { method: "POST", json: { name: name.value } }); $("form").close(); await initMerchants(); openMerchant(m.id); } catch (ex) { err.textContent = ex.message; }
  } }, field("Name", name), h("button", { class: "primary", type: "submit" }, "Create"), err));
  $("form").showModal();
  name.focus();
}

const num = (v) => (v === "" || v == null ? null : Number(v));

async function openMerchant(id) {
  fill($("detail-body"), h("p", { class: "loading" }, "Loading…"));
  if (!$("detail").open) $("detail").showModal();
  let m, conns, events;
  try { [m, conns, events] = await Promise.all([api(`/api/merchants/${id}`), api("/api/connectors"), api(`/api/webhook-events?merchant_id=${id}&limit=15`)]); } catch (err) { fill($("detail-body"), h("p", { class: "error" }, err.message)); return; }
  const manage = can("merchants:manage");
  const s = m.settings;
  // Only regions are redrawn after an action (the header, the key table). The settings and webhook forms, a key that was just
  // revealed (it is shown once) and anything typed stay exactly as they are.
  const head = h("div", {});
  const keyRows = h("tbody", {});
  let current = m;
  const refreshMerchant = async () => {
    try { current = { ...(await api(`/api/merchants/${id}`)) }; } catch (ex) { toast(ex.message, true); return false; }
    paintHead();
    paintKeys();
    return true;
  };
  const reveal = h("div", { hidden: true });
  const show = (title, secret) => { reveal.replaceChildren(h("p", { class: "warn-text" }, `${title} Copy it now: it is shown only once and only a hash (or an encrypted copy) is stored.`),
    h("div", { class: "codewrap" }, h("pre", { class: "code" }, h("code", {}, secret)), h("button", { class: "secondary small", type: "button", onclick: () => copy(secret, "Copied") }, "Copy"))); reveal.hidden = false; };

  // routing and risk settings
  const prefs = h("div", { class: "checkgrid" }, conns.items.map((c) => h("label", {}, h("input", { type: "checkbox", value: c.name, checked: s.preferred_psps.includes(c.name) }), c.name)));
  const fee = h("input", { id: "ms-fee", type: "number", min: "0", max: "1000", value: s.max_fee_bps ?? "", placeholder: "no limit" });
  const wa = h("input", { id: "ms-wa", type: "number", min: "0", max: "1000", value: s.weights.auth });
  const wc = h("input", { id: "ms-wc", type: "number", min: "0", max: "1000", value: s.weights.cost });
  const wl = h("input", { id: "ms-wl", type: "number", min: "0", max: "1000", value: s.weights.latency });
  const t3 = h("input", { id: "ms-3ds", type: "number", min: "1", value: s.require_3ds_over_cents ?? "", placeholder: "never" });
  const region = h("input", { id: "ms-region", maxlength: "2", value: s.region ?? "", placeholder: "any" });
  const cap = h("select", { id: "ms-cap" }, h("option", { value: "manual", selected: s.capture_method === "manual" }, "Manual (authorize, then capture)"), h("option", { value: "automatic", selected: s.capture_method === "automatic" }, "Automatic"));
  const cur = h("input", { id: "ms-cur", value: s.allowed_currencies.join(" "), "aria-label": "Allowed currencies" });
  const serr = h("p", { class: "error", role: "alert" });
  const settingsForm = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    serr.textContent = "";
    const settings = { preferred_psps: [...prefs.querySelectorAll("input:checked")].map((i) => i.value), max_fee_bps: num(fee.value),
      weights: { auth: Number(wa.value), cost: Number(wc.value), latency: Number(wl.value) }, require_3ds_over_cents: num(t3.value),
      region: region.value.trim() || null, capture_method: cap.value, allowed_currencies: cur.value.split(/[\s,]+/).filter(Boolean).map((c) => c.toUpperCase()) };
    try { await api(`/api/merchants/${id}`, { method: "PATCH", json: { settings } }); toast("Settings saved"); } catch (ex) { serr.textContent = ex.message; }
  } }, h("h3", {}, "Routing and risk"), field("Preferred connectors (a small bonus in the score)", prefs),
  h("div", { class: "grid3" }, field("Maximum fee (basis points)", fee), field("Data-residency region", region), field("Ask for 3-D Secure at or above (cents)", t3)),
  h("div", { class: "grid3" }, field("Weight: authorization rate", wa), field("Weight: cost", wc), field("Weight: latency", wl)),
  h("div", { class: "grid2" }, field("Capture", cap), field("Allowed currencies", cur)),
  manage ? h("button", { class: "primary", type: "submit" }, "Save settings") : null, serr);
  if (!manage) for (const el of settingsForm.querySelectorAll("input,select")) el.disabled = true;

  // browser origins (CORS for the publishable key, and where 3-D Secure may send the customer back)
  const origins = h("textarea", { id: "ms-origins", rows: "4", maxlength: "2000", placeholder: "https://shop.example.com\nhttp://localhost:3000", spellcheck: "false", "aria-label": "Allowed browser origins" });
  origins.value = (m.allowed_origins || []).join("\n");
  const oerr = h("p", { class: "error", role: "alert" });
  const originsForm = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    oerr.textContent = "";
    const allowed_origins = origins.value.split(/[\s,]+/).filter(Boolean);
    try { const r = await api(`/api/merchants/${id}`, { method: "PATCH", json: { allowed_origins } }); origins.value = r.allowed_origins.join("\n"); toast("Allowed origins saved"); } catch (ex) { oerr.textContent = ex.message; }
  } }, h("h3", {}, "Browser origins"),
  h("p", { class: "muted" }, "Websites that may call POST /v1/vault/tokens from a customer's browser with a publishable key, one per line: https://host[:port] (http only for localhost). No wildcards, no paths. Only the publishable-key tokenization call accepts browsers; a payment's 3-D Secure return_url must also be on one of these origins."),
  field("Allowed origins", origins), manage ? h("button", { class: "primary", type: "submit" }, "Save origins") : null, oerr);
  if (!manage) origins.disabled = true;

  // keys
  const kname = h("input", { id: "key-name", maxlength: "60", placeholder: "e.g. checkout server" });
  const kkind = h("select", { id: "key-kind" }, h("option", { value: "secret" }, "Secret key (server)"), h("option", { value: "publishable" }, "Publishable key (browser)"));
  const keys = h("div", { class: "card" }, h("h3", {}, "API keys"),
    manage ? h("form", { class: "grid-inline", onsubmit: async (e) => {
      e.preventDefault();
      try { const k = await api(`/api/merchants/${id}/keys`, { method: "POST", json: { kind: kkind.value, name: kname.value } }); kname.value = ""; show(`New ${k.kind} key.`, k.key); } catch (ex) { toast(ex.message, true); }
    } }, field("Name", kname), field("Kind", kkind), h("button", { class: "primary", type: "submit" }, "Create key")) : null,
    reveal,
    h("table", {}, h("thead", {}, h("tr", {}, ...["Key", "Kind", "Name", "Last used", ""].map((t) => h("th", {}, t)))), keyRows));
  function paintKeys() {
    fill(keyRows, current.keys.map((k) => h("tr", {}, h("td", {}, h("code", {}, `${k.prefix}…`)), h("td", {}, k.kind), h("td", {}, k.name), h("td", {}, fmtDate(k.last_used_at)),
      h("td", { class: "actions" }, k.revoked_at ? h("span", { class: "muted" }, "revoked") : manage ? h("button", { class: "danger small", type: "button", onclick: async () => {
        if (!confirm("Revoke this key? Servers using it stop working within seconds.")) return;
        try { await api(`/api/merchants/${id}/keys/${k.id}`, { method: "DELETE" }); await refreshMerchant(); } catch (ex) { toast(ex.message, true); }
      } }, "Revoke") : null))));
  }
  function paintHead() {
    fill(head,
      h("div", { class: "row" }, h("h2", {}, current.name), h("span", { class: `badge ${current.status === "active" ? "ok" : "bad"}` }, current.status)),
      h("p", { class: "muted" }, h("code", {}, current.id), ` · created ${fmtDateTime(current.created_at)}`),
      manage ? h("div", { class: "btns" }, h("button", { class: "secondary", type: "button", onclick: async () => {
        const next = current.status === "active" ? "suspended" : "active";
        if (next === "suspended" && !confirm("Suspend this merchant? Its keys stop working immediately.")) return;
        try { await api(`/api/merchants/${id}`, { method: "PATCH", json: { status: next } }); await refreshMerchant(); initMerchants(); } catch (ex) { toast(ex.message, true); }
      } }, current.status === "active" ? "Suspend" : "Reactivate")) : null);
  }

  // webhook
  const url = h("input", { id: "wh-url", type: "url", maxlength: "500", value: m.webhook_url || "", placeholder: "https://example.com/paybroker/webhooks" });
  const whForm = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    try { const r = await api(`/api/merchants/${id}/webhook`, { method: "PUT", json: { url: url.value.trim() || null } }); if (r.signing_secret) show("Webhook signing secret.", r.signing_secret); else toast("Webhook removed"); } catch (ex) { toast(ex.message, true); }
  } }, h("h3", {}, "Webhook"),
  h("p", { class: "muted" }, "Events are POSTed as JSON with X-Timestamp and X-Signature = HMAC-SHA256(secret, timestamp + \".\" + raw body). Saving generates a new signing secret. Delivery retries with back-off for a day."),
  field("URL", url), manage ? h("button", { class: "primary", type: "submit" }, m.webhook_url ? "Save and rotate the secret" : "Save") : null);
  if (!manage) url.disabled = true;

  paintHead();
  paintKeys();
  fill($("detail-body"),
    head,
    keys, originsForm, settingsForm, whForm,
    events.items.length ? h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Recent event", "Type", "Status", "Attempts", "Last HTTP", "When"].map((t) => h("th", {}, t)))),
      h("tbody", {}, events.items.map((w) => h("tr", {}, h("td", {}, h("code", {}, w.event_id)), h("td", {}, w.type), h("td", {}, w.status), h("td", {}, w.attempts), h("td", {}, w.last_code ?? ""), h("td", {}, fmtDateTime(w.created_at))))))) : null);
}
