// Audit tab (admin only: `audit:read`): the tamper-evident ledger. The newest entries, and a Verify button that recomputes the whole
// hash chain in the database and says either "chain intact, N entries" or the first broken entry and why. The list and the verdict are
// separate regions: pressing Verify never redraws what you are reading, it only marks the broken row.
import { api } from "/kit/api.js";
import { $, fill, fmtDateTime, h, toast } from "/kit/ui.js";
import { flushCard, headCell } from "/static/common.js";

const SHOWN = 100;
const short = (hash) => (hash || "").slice(0, 12);
const detail = (payload) => {
  const text = JSON.stringify(payload || {});
  return text.length > 180 ? text.slice(0, 177) + "..." : text;
};

export function initAudit() {
  const box = $("view-audit");
  const verdict = h("div", { id: "audit-status", class: "card", role: "status", "aria-live": "polite" }, h("p", { class: "muted" }, "Not verified yet."));
  const body = h("tbody", { id: "audit-body" });
  const button = h("button", { id: "audit-verify", class: "primary", type: "button" }, "Verify the chain");
  let broken = null;

  const paint = (items) => {
    fill(body, items.map((e) => h("tr", { "data-seq": e.seq, class: e.seq === broken ? "broken" : null },
      h("td", {}, e.seq === broken ? h("span", { class: "badge bad" }, `#${e.seq} broken`) : `#${e.seq}`),
      h("td", {}, fmtDateTime(e.ts)), h("td", {}, e.actor), h("td", {}, e.action), h("td", { class: "wrap" }, e.subject),
      h("td", { class: "wrap small-text" }, detail(e.payload)), h("td", { class: "small-text", title: e.hash }, short(e.hash)))));
  };
  const load = async () => {
    const { items } = await api(`/api/audit?limit=${SHOWN}`);
    paint(items);
    return items;
  };

  const show = (v) => {
    broken = v.first_broken;
    const rls = v.row_level_security;
    const rlsLine = rls ? (rls.effective ? "Row-level security is enforced for this connection."
      : rls.enforced ? "Row-level security is set up, but this database role is exempt from it (a superuser or BYPASSRLS role)." : `Row-level security is missing on: ${rls.missing.join(", ")}.`) : "";
    fill(verdict, v.intact
      ? [h("p", {}, h("span", { class: "badge ok" }, "Chain intact"), ` ${Number(v.entries).toLocaleString()} entries, head #${v.head.seq} (${short(v.head.hash)}).`),
        h("p", { class: "hint" }, "Every entry's hash was recomputed from its content and the entry before it. Write the head hash down elsewhere to also catch entries cut off the end.")]
      : [h("p", {}, h("span", { class: "badge bad" }, "Chain broken"), ` First broken entry: #${v.first_broken}.`), h("p", { class: "error", role: "alert" }, v.reason || ""),
        h("p", { class: "hint" }, `${Number(v.entries).toLocaleString()} entries in the ledger. Everything before #${v.first_broken} still verifies.`)],
      rlsLine ? h("p", { class: "hint" }, rlsLine) : null);
  };

  button.addEventListener("click", async () => {
    button.disabled = true;
    fill(verdict, h("p", { class: "muted" }, "Verifying…"));
    try {
      const v = await api("/api/audit/verify");
      show(v);
      await load();                        // re-mark the rows: the entry that failed is highlighted in the list
    } catch (err) {
      fill(verdict, h("p", { class: "error", role: "alert" }, err.message));
      toast(err.message, true);
    }
    button.disabled = false;
  });

  fill(box,
    h("h1", {}, "Audit ledger"),
    h("p", { class: "muted" }, "Every payment decision, capture, refund, 3-D Secure result, rule change, key creation or revocation and blocklist change is written here in the same database transaction as the change itself, chained by SHA-256: each entry's hash covers the entry before it. Nobody can update or delete an entry through SQL; if the table owner does it anyway, Verify names the first entry that no longer matches. Card data, keys and full IPs never enter an entry."),
    h("div", { class: "row" }, button), verdict,
    h("div", { id: "audit-list" }, h("p", { class: "loading" }, "Loading…")));
  load().then(() => {
    fill($("audit-list"), flushCard("Recent entries", h("table", {}, h("thead", {}, h("tr", {}, ...["#", "When", "Who", "Action", "Subject", "Details", "Hash"].map(headCell))), body)));
  }).catch((err) => fill($("audit-list"), h("p", { class: "error" }, err.message)));
}
