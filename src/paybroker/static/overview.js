import { api } from "/kit/api.js";
import { can } from "/kit/session.js";
import { $, badge, fill, h, toast } from "/kit/ui.js";
import { band, cents, PAYMENT } from "/static/common.js";

let days = 7;
const NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs, text) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  return el;
}

function histogram(buckets) {
  const counts = Array.from({ length: 10 }, (_, i) => buckets.find((b) => b.from === i * 100)?.count || 0);
  const max = Math.max(1, ...counts);
  const svg = svgEl("svg", { viewBox: "0 0 500 170", class: "chart", role: "img", "aria-label": "Fraud score distribution" });
  counts.forEach((c, i) => {
    const hgt = Math.round((c / max) * 120);
    svg.append(svgEl("rect", { class: `bar ${band(i * 100 + 50)}`, x: i * 50 + 4, y: 130 - hgt, width: 42, height: hgt }));
    svg.append(svgEl("text", { x: i * 50 + 25, y: 126 - hgt, "text-anchor": "middle" }, c || ""));
    svg.append(svgEl("text", { x: i * 50 + 25, y: 148, "text-anchor": "middle" }, `${i * 100}`));
  });
  svg.append(svgEl("text", { x: 250, y: 166, "text-anchor": "middle" }, "model score (under 300 approve · 300-699 3-D Secure · 700+ decline)"));
  return svg;
}

function dailyBars(rows) {
  if (!rows.length) return h("p", { class: "muted" }, "No payments in this period.");
  const max = Math.max(1, ...rows.map((r) => r.n));
  const w = Math.max(10, Math.floor(480 / rows.length) - 4);
  const svg = svgEl("svg", { viewBox: "0 0 500 150", class: "chart", role: "img", "aria-label": "Payments per day" });
  rows.forEach((r, i) => {
    const x = i * (w + 4) + 4;
    const total = Math.round((r.n / max) * 100);
    const bad = Math.round((r.declined / max) * 100);
    svg.append(svgEl("rect", { class: "bar", x, y: 110 - total, width: w, height: total }));
    if (bad) svg.append(svgEl("rect", { class: "bar high", x, y: 110 - bad, width: w, height: bad }));
    if (rows.length <= 14) svg.append(svgEl("text", { x: x + w / 2, y: 128, "text-anchor": "middle" }, String(r.day).slice(5)));
  });
  svg.append(svgEl("text", { x: 250, y: 146, "text-anchor": "middle" }, "payments per day (red: declined)"));
  return svg;
}

export async function initOverview() {
  // The title and the period selector are built once; only the data region below them is redrawn when the period changes
  // or demo traffic is sent, so the selector keeps its focus (a keyboard user steps through the periods) and its choice.
  const box = $("view-overview");
  const body = h("div", { id: "ov-body" }, h("p", { class: "loading" }, "Loading…"));
  const period = h("select", { id: "ov-period", "aria-label": "Period", onchange: (e) => { days = Number(e.target.value); paint(); } },
    ...[[1, "Last 24 hours"], [7, "Last 7 days"], [30, "Last 30 days"], [90, "Last 90 days"]].map(([v, t]) => h("option", { value: v, selected: v === days }, t)));
  fill(box, h("div", { class: "row" }, h("h1", {}, "Overview"), period), body);
  let latest = 0;
  await paint();

  async function paint() {
    const mine = ++latest;
    let o;
    try { o = await api(`/api/overview?days=${days}`); } catch (err) { if (mine === latest) fill(body, h("p", { class: "error" }, err.message)); return; }
    if (mine !== latest) return;      // a newer period was chosen while this one loaded
    const stat = (value, label, hot, href) => h("a", { class: `stat${hot ? " hot" : ""}`, href: href || "#overview" }, h("strong", {}, value), h("span", {}, label));
    const parts = [
      h("div", { class: "stats" },
        stat(o.payments.toLocaleString(), "payments", false, "#payments"),
        stat(o.approval_rate == null ? "n/a" : `${(o.approval_rate * 100).toFixed(1)}%`, "approved"),
        stat(`${o.latency_ms.p95} ms`, `p95 latency (avg ${o.latency_ms.avg})`),
        stat(cents(o.money_minor_units.approved_cents), "approved volume"),
        stat(String(o.open_cases), "open fraud cases", o.open_cases > 0, "#cases"),
        stat(String(o.pending_rule_suggestions), "rule suggestions waiting", o.pending_rule_suggestions > 0, "#rules"),
        stat(String(o.outbox_backlog), "events queued")),
    ];
    if (!o.payments) {
      parts.push(h("div", { class: "card" }, h("h3", {}, "No payments yet"),
        h("p", { class: "muted" }, "Create a merchant under Merchants, then call the payment API (see Connect). To see the dashboard with realistic traffic right now, generate a demo run: fictional customers, test cards, and a mix of ordinary payments, issuer declines, 3-D Secure challenges, card testing and account takeover."),
        can("settings:manage") ? h("button", { class: "primary", type: "button", onclick: async (e) => {
          e.target.disabled = true;
          try { const r = await api("/api/demo/traffic", { method: "POST", json: { count: 120, seed: 7 } }); toast(`Sent ${r.sent} demo payments`); paint(); } catch (err) { toast(err.message, true); e.target.disabled = false; }
        } }, "Generate 120 demo payments") : null));
    } else {
      parts.push(h("div", { class: "cols" },
        h("div", { class: "card" }, h("h3", {}, "Fraud score distribution"), histogram(o.score_histogram),
          h("p", { class: "muted" }, `Decisions: ${["APPROVE", "CHALLENGE", "DECLINE"].map((d) => `${o.fraud_decisions[d] || 0} ${d.toLowerCase()}`).join(" · ")}`)),
        h("div", { class: "card" }, h("h3", {}, "Payments per day"), dailyBars(o.daily))),
      h("div", { class: "cols" },
        h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Status"), h("th", { class: "num" }, "Payments"))),
          h("tbody", {}, ...Object.entries(o.by_status).sort((a, b) => b[1] - a[1]).map(([s, n]) => h("tr", {}, h("td", {}, badge(PAYMENT, s)), h("td", { class: "num" }, n.toLocaleString())))))),
        h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Connector"), h("th", { class: "num" }, "Payments"), h("th", { class: "num" }, "Approved"))),
          h("tbody", {}, ...o.psp.map((p) => h("tr", {}, h("td", {}, p.psp), h("td", { class: "num" }, p.n), h("td", { class: "num" }, p.approved))))))));
    }
    fill(body, ...parts);
  }
}
