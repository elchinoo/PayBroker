// Settings > Maintenance (admin only: `data:reset`): wipe the transaction data and start fresh, keeping the configuration.
// The panel is built once. Nothing redraws the form: the live counts, the result and the list of recent resets are separate
// regions, so a refresh never clears the confirmation phrase that is being typed.
import { api } from "/kit/api.js";
import { fill, fmtDateTime, h, toast } from "/kit/ui.js";

const PHRASE = "RESET PAYBROKER DATA";   // the server's own copy (GET) wins; this is only the fallback
const LABEL = {
  transaction: "payments", three_ds_challenge: "3-D Secure challenges", fraud_assessment: "fraud assessments", fraud_case: "fraud cases",
  chargeback: "chargebacks", webhook_event: "webhook deliveries", outbox: "queued events", fs_entity: "feature-store entries (customer, device, card and IP velocity)",
  training_example: "training examples", rule_suggestion: "suggested rules",
};
const num = (n) => Number(n || 0).toLocaleString();

export async function renderMaintenance(box) {
  let info;
  try { info = await api("/api/admin/data/reset"); } catch (err) { fill(box, h("h2", {}, "Maintenance"), h("p", { class: "error" }, err.message)); return; }
  const phrase = info.phrase || PHRASE;
  const countsBox = h("p", { class: "muted", id: "mt-counts" });
  const recentBox = h("div", { id: "mt-recent" });
  const resultBox = h("div", { id: "mt-result", role: "status" });
  const training = h("input", { id: "mt-training", type: "checkbox", checked: true });
  const suggestions = h("input", { id: "mt-suggestions", type: "checkbox", checked: true });
  const typed = h("input", { id: "mt-phrase", autocomplete: "off", spellcheck: "false", placeholder: phrase, "aria-label": `Type ${phrase} to confirm` });
  const button = h("button", { id: "mt-reset", class: "danger", type: "button", disabled: true }, "Reset the data");
  const matches = () => typed.value === phrase;
  typed.addEventListener("input", () => { button.disabled = !matches(); });

  const paint = (data) => {
    countsBox.textContent = `Right now: ${Object.entries(data.counts).filter(([t]) => LABEL[t]).map(([t, n]) => `${num(n)} ${LABEL[t]}`).join(", ")}.`;
    fill(recentBox, data.recent.length ? h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["When", "Who", "Outcome", "Deleted"].map((t) => h("th", {}, t)))),
      h("tbody", {}, data.recent.map((r) => h("tr", {}, h("td", {}, fmtDateTime(r.at)), h("td", {}, r.actor), h("td", {}, outcome(r.detail)),
        h("td", { class: "wrap" }, deletedText(r.detail))))))) : h("p", { class: "hint" }, "No reset has been run yet."));
  };
  const refresh = async () => { try { paint(await api("/api/admin/data/reset")); } catch (e) { toast(e.message, true); } };

  button.addEventListener("click", async () => {
    if (!matches()) return;
    button.disabled = true;
    typed.disabled = training.disabled = suggestions.disabled = true;
    fill(resultBox, h("p", { class: "muted" }, "Resetting… this can take a little while for a large database. Leave this page open."));
    try {
      const r = await api("/api/admin/data/reset", { method: "POST", json: { confirm: typed.value, include_training_data: training.checked, include_rule_suggestions: suggestions.checked } });
      typed.value = "";
      fill(resultBox, resultCard(r));
      toast("The transaction data was reset");
    } catch (err) {
      fill(resultBox, h("p", { class: "error", role: "alert" }, `${err.message}${err.status ? "" : " The server may still be working: check the recent resets below before trying again."}`));
    }
    typed.disabled = training.disabled = suggestions.disabled = false;
    button.disabled = !matches();
    refresh();
  });

  paint(info);
  fill(box, h("h2", {}, "Maintenance"),
    h("div", { class: "card" }, h("h3", {}, "Reset the transaction data"),
      h("p", { class: "muted" }, "Wipes everything the traffic produced and starts fresh. It cannot be undone, and it applies to every instance that shares this database. Pause the senders (the simulator, your own servers) first."),
      h("div", { class: "cols" },
        h("div", {}, h("h4", {}, "Deleted"), h("ul", {}, h("li", {}, "Payments, with their captures, refunds, attempts and idempotency records"), h("li", {}, "3-D Secure challenges"), h("li", {}, "Fraud assessments and fraud cases"),
          h("li", {}, "Chargebacks"), h("li", {}, "Webhook deliveries and the event queue"), h("li", {}, "The feature store: the velocity the fraud model remembers about customers, devices, cards and IPs"),
          h("li", {}, "Training examples and suggested rules (optional, below)"))),
        h("div", {}, h("h4", {}, "Kept"), h("ul", {}, h("li", {}, "Merchants, their API keys (the same keys keep working), webhook endpoints and the card tokens they vaulted"), h("li", {}, "Connectors and routing, fraud rules and the blocked list"),
          h("li", {}, "Settings, AI accounts and their stored keys, uploaded models and the active model"), h("li", {}, "Users, sessions and operator tokens")))),
      countsBox,
      h("div", { class: "checkgrid" },
        h("label", { class: "check" }, training, " Also delete the captured training examples"),
        h("label", { class: "check" }, suggestions, " Also delete the suggested rules (approved rules are kept)")),
      h("div", { class: "field" }, h("label", { for: "mt-phrase" }, `To confirm, type ${phrase}`), typed),
      h("div", { class: "btns" }, button),
      resultBox),
    h("h3", {}, "Recent resets"), recentBox);
}

function outcome(d) {
  const text = d.status === "completed" ? "completed" : d.status === "running" ? "running or interrupted" : d.status === "interrupted" ? "interrupted (run it again to finish)" : d.status || "";
  return text;
}

function deletedText(d) {
  const rows = Object.entries(d.deleted || {}).filter(([t]) => LABEL[t]).map(([t, n]) => `${num(n)} ${LABEL[t].split(" (")[0]}`);
  const kept = [d.include_training_data === false ? "training examples" : "", d.include_rule_suggestions === false ? "suggested rules" : ""].filter(Boolean);
  return rows.length ? rows.join(", ") + (kept.length ? `; kept ${kept.join(" and ")}` : "") : "";
}

function resultCard(r) {
  return h("div", { class: "card", id: "mt-result-card" }, h("h4", {}, `Done in ${r.seconds} s`),
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Deleted"), h("th", { class: "num" }, "Rows"))),
      h("tbody", {}, Object.entries(r.deleted).map(([t, n]) => h("tr", {}, h("td", {}, LABEL[t] || t), h("td", { class: "num" }, num(n)))))),
    r.skipped.length ? h("p", { class: "muted" }, `Kept on request: ${r.skipped.map((t) => LABEL[t] || t).join(", ")}.`) : null,
    h("p", { class: "hint" }, "The configuration was kept. Other running instances use the new state within seconds."));
}
