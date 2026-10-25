// Settings > Model: which fraud model scores, uploading and rolling back models, and the opt-in training data.
import { api } from "/kit/api.js";
import { can } from "/kit/session.js";
import { fmtDate, $, field, fill, h, toast, confirmDialog } from "/kit/ui.js";

const num = (n) => (n == null ? "" : Number(n).toLocaleString());
const pct = (r) => (r == null ? "n/a" : `${(r * 100).toFixed(2)}%`);
const kb = (n) => `${Math.max(1, Math.round(n / 1024))} KB`;
const summary = (t) => (t && typeof t === "object" ? t.summary || "" : t || "");

export async function renderModel(box) {
  if (!can("model:manage") && !can("training:export")) { fill(box); return; }
  let models, stats;
  try { [models, stats] = await Promise.all([api("/api/admin/models"), api("/api/admin/training/stats")]); } catch (err) { fill(box, h("h2", {}, "Model"), h("p", { class: "error" }, err.message)); return; }
  // The model list and the training statistics are regions that are refreshed in place after an action. The upload form and
  // the training settings and export forms are built once: activating a model or purging rows never clears a half-filled form
  // (notes, a version name, the chosen file, a retention value).
  const listBox = h("div", { id: "model-list" }), statsBox = h("div", { id: "training-stats" }), hintBox = h("p", { class: "hint", id: "training-hint" });
  const refreshModels = async () => {
    try { models = await api("/api/admin/models"); } catch (e) { toast(e.message, true); return; }
    fill(listBox, modelList(models, refreshModels));
  };
  const refreshStats = async () => {
    try { stats = await api("/api/admin/training/stats"); } catch (e) { toast(e.message, true); return; }
    paintStats(statsBox, hintBox, stats);
  };
  fill(listBox, modelList(models, refreshModels));
  paintStats(statsBox, hintBox, stats);
  fill(box, h("h2", {}, "Model"), h("p", { class: "muted" }, "Tier 2 scores every payment with a gradient-boosted tree model. The bundled one is trained on synthetic data only. You can upload a model trained elsewhere and switch to it, or back, without rebuilding the image; other instances follow within about 30 seconds."),
    listBox, uploadForm(models, refreshModels), trainingData(stats, statsBox, hintBox, refreshStats));
}

// ---- models -----------------------------------------------------------------------------------------
function modelList(models, again) {
  const act = async (id, msg) => { try { await api(id == null ? "/api/admin/models/use-bundled" : `/api/admin/models/${id}/activate`, { method: "POST" }); toast(msg); again(); } catch (e) { toast(e.message, true); } };
  const bundledActive = models.active.source === "bundled";
  const rows = [h("tr", {}, h("td", { class: "title" }, models.bundled.version), h("td", {}, "bundled with the app"), h("td", {}, models.bundled.trees), h("td", {}, "n/a"),
    h("td", {}, h("span", { class: `badge ${bundledActive ? "ok" : "plain"}` }, bundledActive ? "active" : "inactive")),
    h("td", { class: "actions" }, bundledActive ? null : h("button", { class: "secondary small", type: "button", onclick: () => act(null, "Using the bundled model") }, "Use this")))];
  for (const m of models.items) {
    rows.push(h("tr", {}, h("td", { class: "title" }, m.version, m.notes ? h("div", { class: "hint" }, m.notes) : null), h("td", {}, `${m.uploaded_by}, ${fmtDate(m.uploaded_at)}`), h("td", {}, m.tree_count), h("td", {}, kb(m.size_bytes)),
      h("td", {}, h("span", { class: `badge ${m.active ? "ok" : "plain"}` }, m.active ? "active" : "inactive")),
      h("td", { class: "actions" }, m.active ? null : [
        h("button", { class: "secondary small", type: "button", onclick: () => act(m.id, `Activated ${m.version}`) }, "Activate"), " ",
        h("button", { class: "danger small", type: "button", onclick: async () => { if (!(await confirmDialog(`Delete model ${m.version}? This cannot be undone.`, { title: "Delete model", confirmLabel: "Delete", danger: true }))) return; try { await api(`/api/admin/models/${m.id}`, { method: "DELETE" }); again(); } catch (e) { toast(e.message, true); } } }, "Delete")])));
  }
  return h("div", {}, h("h3", {}, "Models"),
    h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Version", "Uploaded", "Trees", "Size", "State", ""].map((t) => h("th", {}, t)))), h("tbody", {}, rows))),
    h("p", { class: "hint" }, `Rolling back is activating an earlier version. Bundled model: ${summary(models.bundled.trained_on)}`));
}

function uploadForm(models, again) {
  const file = h("input", { id: "model-file", type: "file", accept: ".json,application/json" });
  const notes = h("input", { id: "model-notes", maxlength: "1000", placeholder: "what changed, what it was trained on" });
  const version = h("input", { id: "model-version", maxlength: "64", placeholder: "optional; default is the file's own version" });
  const activate = h("input", { id: "model-activate", type: "checkbox" });
  const err = h("p", { class: "error", role: "alert" });
  return h("div", {}, h("h3", {}, "Upload a model"),
    h("p", { class: "muted" }, `A JSON file in the format scripts/train_model.py writes: the 17 features in order and up to ${models.limits.max_trees} trees of depth ${models.limits.max_depth}, at most ${kb(models.limits.max_bytes)}. It is checked before it is stored and never activates by itself unless you tick the box.`),
    h("form", { class: "card", onsubmit: async (e) => {
      e.preventDefault();
      err.textContent = "";
      const f = file.files[0];
      if (!f) { err.textContent = "Choose a model file."; return; }
      if (f.size > models.limits.max_bytes) { err.textContent = `The file is larger than ${kb(models.limits.max_bytes)}.`; return; }
      let doc;
      try { doc = JSON.parse(await f.text()); } catch { err.textContent = "That file is not valid JSON."; return; }
      try {
        const r = await api("/api/admin/models", { method: "POST", json: { model: doc, notes: notes.value, version: version.value || null, activate: activate.checked } });
        toast(`Uploaded ${r.version}${r.active ? " and activated it" : ""}`);
        file.value = notes.value = version.value = "";
        activate.checked = false;
        again();
      } catch (ex) { err.textContent = ex.message; }
    } }, field("Model file", file), field("Version name", version), field("Notes", notes), h("label", { class: "check" }, activate, " Activate right after upload"),
    h("button", { class: "primary", type: "submit" }, "Upload"), err));
}

// ---- training data ------------------------------------------------------------------------------------
function paintStats(statsBox, hintBox, stats) {
  const s = stats.settings;
  const byModel = stats.per_model_version.map((m) => h("tr", {}, h("td", { class: "title" }, m.model_version), h("td", {}, num(m.rows)), h("td", {}, num(m.labeled)), h("td", {}, pct(m.fraud_rate)), h("td", {}, m.avg_score)));
  hintBox.textContent = s.capture_source === "environment" ? "Capture is on because PAYBROKER_CAPTURE_TRAINING_DATA is set; saving here overrides it." : "";
  fill(statsBox, h("div", { class: "card" }, h("p", {}, `${num(stats.rows)} rows, ${num(stats.labeled)} labeled (${num(stats.fraud)} fraud, fraud rate ${pct(stats.fraud_rate)}). Label sources: ${Object.entries(stats.label_sources).map(([k, v]) => `${k} ${num(v)}`).join(", ") || "none yet"}.`),
    stats.per_model_version.length ? h("table", {}, h("thead", {}, h("tr", {}, ...["Model version", "Rows", "Labeled", "Fraud rate", "Average score"].map((t) => h("th", {}, t)))), h("tbody", {}, byModel)) : null));
}

function trainingData(stats, statsBox, hintBox, again) {
  const s = stats.settings;
  const capture = h("input", { id: "tr-capture", type: "checkbox", checked: s.capture });
  const retention = h("input", { id: "tr-retention", type: "number", min: "1", max: "3650", value: String(s.retention_days) });
  const maxRows = h("input", { id: "tr-max", type: "number", min: "1000", max: "50000000", value: String(s.max_rows) });
  const labeledOnly = h("input", { id: "tr-labeled", type: "checkbox", checked: true });
  const since = h("input", { id: "tr-since", type: "date" });
  const days = h("input", { id: "tr-days", type: "number", min: "1", max: "3650", value: "90" });
  const exportLink = (fmt) => {
    const a = h("a", { class: "button secondary", href: "#", download: "" }, `Export ${fmt.toUpperCase()}`);
    a.addEventListener("click", () => { a.href = `/api/admin/training/export?format=${fmt}&labeled=${labeledOnly.checked}${since.value ? `&since=${since.value}` : ""}`; });
    return a;
  };
  return h("div", {}, h("h3", {}, "Training data"),
    h("p", { class: "muted" }, "Off by default. When on, every scored payment leaves one row: the 17 derived features, the model version, the score and decision, and the payment reference. Never a card, e-mail, IP address, device id or name. Labels come only from real outcomes: a fraud-coded chargeback, or an analyst resolving a case. Export the rows to retrain or improve the model."),
    h("form", { class: "card grid-inline", onsubmit: async (e) => {
      e.preventDefault();
      try { await api("/api/admin/training/settings", { method: "PUT", json: { capture: capture.checked, retention_days: Number(retention.value), max_rows: Number(maxRows.value) } }); toast("Saved"); again(); } catch (ex) { toast(ex.message, true); }
    } }, h("label", { class: "check" }, capture, " Capture training data"), field("Keep rows for (days)", retention), field("Row cap (oldest are purged first)", maxRows), h("button", { class: "primary", type: "submit" }, "Save")),
    hintBox,
    statsBox,
    h("div", { class: "card grid-inline" }, h("label", { class: "check" }, labeledOnly, " Labeled rows only"), field("Since", since), exportLink("csv"), exportLink("ndjson")),
    h("p", { class: "hint" }, "Only an admin can export. The file lists the feature names and versions in its header. A model trained on its own past decisions inherits their bias: mix in other data and read the README before trusting it."),
    h("form", { class: "card grid-inline", onsubmit: async (e) => {
      e.preventDefault();
      if (!(await confirmDialog(`Label every approved, captured payment older than ${days.value} days with no dispute and no open case as legitimate? This is a guess about silence, covers approved payments only, and cannot be undone.`, { title: "Label as legitimate", confirmLabel: "Label", danger: true }))) return;
      try { const r = await api("/api/admin/training/label-aging", { method: "POST", json: { days: Number(days.value) } }); toast(`${num(r.labeled_legitimate)} rows labeled legitimate`); again(); } catch (ex) { toast(ex.message, true); }
    } }, field("Label by aging: no dispute after (days)", days), h("button", { class: "secondary", type: "submit" }, "Label as legitimate")),
    h("div", { class: "btns" }, h("button", { class: "secondary", type: "button", onclick: async () => {
      try { const r = await api("/api/admin/training/purge", { method: "POST" }); toast(`Purged ${num(r.expired + r.over_cap)} rows`); again(); } catch (ex) { toast(ex.message, true); }
    } }, "Purge now")));
}
