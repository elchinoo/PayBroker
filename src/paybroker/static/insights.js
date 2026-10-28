import { api } from "/kit/api.js";
import { initInsights } from "/kit/insights.js";
import { $, fill, h } from "/kit/ui.js";

// Settings-level page (needs settings:manage, like GET /api/insights): the kit renders the top statements, the tuner's
// recommendations and the index advisor's suggestions, each with an honest "Not available" state on a plain Postgres.
export function initInsightsPage() {
  const body = h("div", { id: "insights-body" });
  fill($("view-insights"),
    h("h1", {}, "Database insights"),
    h("p", { class: "muted" }, "What EDB's extensions on AgentPG know about PayBroker's own queries: EDB Stat Monitor (slowest statements), EDB Postgres Tuner (settings) and Query Advisor (indexes that could help). Read-only; statements are shown without their values. On a database without these extensions the page says so."),
    body);
  return initInsights(body, api);
}
