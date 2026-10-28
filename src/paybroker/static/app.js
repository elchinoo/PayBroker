import { api } from "/kit/api.js";
import { bootSession, can, refreshSession, renderLogin, renderSetup, renderVaultBanner, state } from "/kit/session.js";
import { $ } from "/kit/ui.js";
import { initCases } from "/static/cases.js";
import { initConnect } from "/static/connect.js";
import { initInsightsPage } from "/static/insights.js";
import { initMerchants } from "/static/merchants.js";
import { initOverview } from "/static/overview.js";
import { initPayments } from "/static/payments.js";
import { initRouting } from "/static/routing.js";
import { initRules } from "/static/rules.js";
import { initSettings } from "/static/settings.js";

const TABS = ["overview", "payments", "cases", "rules", "merchants", "routing", "settings", "insights", "connect"];
const INIT = { overview: initOverview, payments: initPayments, cases: initCases, rules: initRules, merchants: initMerchants, routing: initRouting, settings: initSettings, insights: initInsightsPage, connect: initConnect };
const NEEDS = { overview: "payments:read", payments: "payments:read", cases: "payments:read", rules: "payments:read", merchants: "payments:read", routing: "payments:read", settings: "settings:manage", insights: "settings:manage", connect: "mcp:use" };
const SECTIONS = ["auth-card", "vault-banner", "nav", "signout", ...TABS.map((t) => "view-" + t)];

function hideAll() { for (const id of SECTIONS) $(id).hidden = true; }

async function boot(notice = "") {
  hideAll();
  $("auth-card").hidden = false;
  const s = await bootSession($("auth-card"), { text: "Loading PayBroker\u2026" });   // loading state, then error + retry; null on error
  if (!s) return;
  if (s.state === "setup_required") renderSetup(() => boot(), "PayBroker");
  else if (s.state === "logged_out") renderLogin(() => boot(), notice, "PayBroker");
  else showApp();
}

function showApp() {
  $("auth-card").hidden = true;
  for (const a of document.querySelectorAll("#nav a")) a.hidden = !can(NEEDS[a.dataset.tab]);
  $("nav").hidden = false;
  $("signout").hidden = false;
  renderVaultBanner();
  route();
}

function route() {
  if (!state.session || state.session.state !== "logged_in") return;
  const allowed = TABS.filter((t) => can(NEEDS[t]));
  const want = location.hash.slice(1);
  const tab = allowed.includes(want) ? want : (allowed[0] || "overview");
  for (const t of TABS) $("view-" + t).hidden = t !== tab;
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("active", a.dataset.tab === tab);
  INIT[tab]();
}

$("signout").addEventListener("click", async () => {
  let out = {};
  try { out = await api("/api/logout", { method: "POST" }); } catch { /* the cookie is cleared server-side regardless */ }
  if (out.logout_url) { location.assign(out.logout_url); return; }
  boot();
});
for (const id of ["detail", "form"]) $(id + "-close").addEventListener("click", () => $(id).close());

let lostTimer;
window.addEventListener("apg-auth-lost", () => {
  clearTimeout(lostTimer);
  lostTimer = setTimeout(() => boot("Your session ended. Please sign in again."), 50);
});
window.addEventListener("apg-vault-locked", async () => { try { await refreshSession(); renderVaultBanner(); } catch { /* ignore */ } });
window.addEventListener("apg-unlocked", route);
window.addEventListener("hashchange", route);

boot();
