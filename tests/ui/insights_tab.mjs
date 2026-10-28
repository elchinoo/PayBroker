// Insights tab: the page mounts the kit's initInsights into #view-insights, asks GET /api/insights once, renders the sources that are
// available and an honest "Not available" for the others (plain Postgres), and Refresh asks again. app.js shows the tab only with
// settings:manage. Run: node tests/ui/insights_tab.mjs
import fs from "node:fs";
import { STATIC, calls, check, click, done, hasText, live, loginAs, route, settle, shell } from "./fakedom.mjs";

shell(["view-insights"]);
await loginAs(["*"]);
const none = { available: false, reason: "extension edb_pg_tuner is not installed" };
let payload = {
  sources: {},
  top_queries: { available: true, source: "pg_stat_statements", since: null, queries: [{ queryid: "1", query: "SELECT * FROM transaction WHERE payment_id = $1", calls: 10, total_ms: 50, mean_ms: 5, max_ms: 9, rows: 10 }] },
  tuner: none,
  advisor: { available: false, reason: "extension query_advisor is not installed" },
};
route({ "GET /api/insights": () => payload });
const { initInsightsPage } = await import("/static/insights.js");
await initInsightsPage();
await settle(60);
check(hasText("Database insights") && hasText("EDB Stat Monitor"), "the page heading or the intro is missing");
check(hasText("SELECT * FROM transaction WHERE payment_id = $1"), "the top statements did not render");
check(hasText("Not available on this database") && hasText("edb_pg_tuner is not installed"), "an unavailable source must say so, with the reason");
check(calls.filter((c) => c.path.startsWith("/api/insights")).length === 1, "the page should ask for the insights exactly once");
const refresh = [live("view-insights")].flatMap((r) => r._all()).find((e) => e.tag === "button" && e.textContent.startsWith("Refresh"));
check(!!refresh, "no Refresh button");
payload = { ...payload, tuner: { available: true, recommendations: [{ setting: "shared_buffers", value: "186 MB", raw: "" }] } };
await click(refresh);
await settle(60);
check(hasText("shared_buffers") && calls.filter((c) => c.path.startsWith("/api/insights")).length === 2, "Refresh did not reload the page");

// the tab is wired in the shell: nav link, section, and the permission that gates it
const js = fs.readFileSync(`${STATIC}/app.js`, "utf8"), html = fs.readFileSync(`${STATIC}/index.html`, "utf8");
check(js.includes('insights: "settings:manage"') && js.includes("insights: initInsightsPage") && js.includes('"insights"'), "app.js does not wire the insights tab behind settings:manage");
check(html.includes('data-tab="insights"') && html.includes('id="view-insights"'), "index.html lacks the Insights tab or its section");
done("the Insights tab renders available and unavailable sources, refreshes, and is wired behind settings:manage");
