import { h } from "/kit/ui.js";

export const cents = (amount, currency) => (amount == null ? "" : `${(Number(amount) / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency || ""}`.trim());

export const PAYMENT = {
  PROCESSING: ["Processing", "plain"], REQUIRES_ACTION: ["3-D Secure", "warn"], AUTHORIZED: ["Authorized", "ok"], CAPTURED: ["Captured", "ok"],
  PARTIALLY_REFUNDED: ["Part refunded", "alt"], REFUNDED: ["Refunded", "alt"], VOIDED: ["Voided", "plain"], DECLINED: ["Declined", "bad"], FAILED: ["Failed", "bad"],
};
export const DECISION = { APPROVE: ["Approve", "ok"], CHALLENGE: ["3-D Secure", "warn"], DECLINE: ["Decline", "bad"] };
export const ACTION = { PERMANENT_BLOCK: "Permanent block", ALLOW_WITH_KYC: "Allow with KYC", DISMISS_ALERT: "Dismiss alert" };

export const band = (score) => (score >= 700 ? "high" : score >= 300 ? "mid" : "low");

// A 0-1000 bar split at the 300 and 700 thresholds, with a marker at the score.
export function gauge(score) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 1000 24");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Fraud score ${score} of 1000`);
  const line = document.createElementNS(ns, "line");
  for (const [k, v] of Object.entries({ x1: score, x2: score, y1: 0, y2: 24 })) line.setAttribute(k, v);
  svg.append(line);
  return h("div", { class: "gauge" }, svg);
}

export function pills(items) {
  return h("div", { class: "pill-list" }, (items || []).map((i) => h("span", { class: "badge plain" }, i)));
}
