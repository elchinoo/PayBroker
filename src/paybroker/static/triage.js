// Settings > AI triage limits: how often the broker may ask the language model, and what it is doing right now.
import { api } from "/kit/api.js";
import { $, field, fill, h, toast } from "/kit/ui.js";

const FIELDS = [
  ["rpm", "tr-rpm", "Calls per minute (0 = unlimited)", 0, 600],
  ["daily_cap", "tr-cap", "Calls per 24 hours (0 = unlimited)", 0, 1000000],
  ["group_minutes", "tr-group", "Group cases of one device or IP for (minutes, 0 = off)", 0, 1440],
  ["pause_minutes", "tr-pause", "Pause after three gateway 429s (minutes)", 1, 1440],
];
const SOURCE = { environment: "from the environment", default: "default", setting: "saved here" };

/** The one-line status under the card: pure, so a test can read it without a page. */
export function usageLine(s) {
  const u = s.usage;
  const cap = s.daily_cap ? ` of ${s.daily_cap.toLocaleString()}` : " (no cap)";
  const circuit = u.circuit.open ? `paused until ${new Date(u.circuit.paused_until * 1000).toLocaleTimeString()}` : "closed";
  return `LLM calls in the last 24 hours: ${u.calls_24h.toLocaleString()}${cap}. Queued triage jobs: ${u.queued.toLocaleString()}. Circuit breaker: ${circuit} (${u.circuit.consecutive_429} consecutive 429s).`;
}

/** The request body for the inputs' current values; a field left blank or not a whole number is reported, not sent. */
export function bodyFrom(values) {
  const body = {};
  for (const [key, , label, lo, hi] of FIELDS) {
    const raw = String(values[key] ?? "").trim();
    const n = Number(raw);
    if (raw === "" || !Number.isInteger(n) || n < lo || n > hi) return { error: `${label}: enter a whole number from ${lo} to ${hi}.` };
    body[key] = n;
  }
  return { body };
}

export async function renderTriageLimits(box) {
  let s;
  try { s = await api("/api/admin/triage/settings"); } catch (err) { fill(box, h("h2", {}, "AI triage limits"), h("p", { class: "error" }, err.message)); return; }
  // The form is built once. Saving or refreshing repaints only the status line and the "where each value comes from" hint, so a
  // value the person is still typing is never replaced.
  const inputs = Object.fromEntries(FIELDS.map(([key, id, , lo, hi]) => [key, h("input", { id, type: "number", min: String(lo), max: String(hi), value: String(s[key]) })]));
  const status = h("p", { id: "tr-usage", role: "status" }), where = h("p", { class: "hint", id: "tr-source" }), err = h("p", { class: "error", role: "alert" });
  const paint = (now) => {
    status.textContent = usageLine(now);
    where.textContent = FIELDS.map(([key, , label]) => `${label.split(" (")[0]}: ${SOURCE[now.sources[key]]}`).join(". ") + ".";
  };
  const refresh = async () => { try { paint(await api("/api/admin/triage/settings")); } catch (e) { toast(e.message, true); } };
  paint(s);
  fill(box, h("h2", {}, "AI triage limits"),
    h("p", { class: "muted" }, "The shared LLM gateway allows 60 requests a minute and a fixed token budget a month. Tier 3 stays well inside both: calls are paced, capped per day, and skipped for cases that share a device or IP with one the model already read. Whatever is not sent to the model gets the built-in rule-based triage, marked as such on the case. With several workers the per-minute limit applies to each one."),
    h("form", { class: "card grid-inline", onsubmit: async (e) => {
      e.preventDefault();
      err.textContent = "";
      const { body, error } = bodyFrom(Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value])));
      if (error) { err.textContent = error; return; }
      try { paint(await api("/api/admin/triage/settings", { method: "PUT", json: body })); toast("Saved"); } catch (ex) { err.textContent = ex.message; }
    } }, ...FIELDS.map(([key, , label]) => field(label, inputs[key])), h("button", { class: "primary", type: "submit" }, "Save")),
    err, where, status,
    h("div", { class: "btns" }, h("button", { class: "secondary small", type: "button", onclick: refresh }, "Refresh usage")));
}
