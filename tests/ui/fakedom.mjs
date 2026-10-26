// A tiny fake DOM and a stubbed fetch, enough to run PayBroker's real page modules under node (no browser, no server).
// Each test file runs in its own process (the pages keep module state), imports this first, then the page under test.
//
//   PB_STATIC_DIR=/some/other/static node tests/ui/cases_typing.mjs     runs the same test against another copy of the pages
//   (that is how each regression test was shown to FAIL on the old code and pass on the new).
import path from "node:path";
import { register } from "node:module";
import { pathToFileURL } from "node:url";

const here = path.dirname(new URL(import.meta.url).pathname);
export const STATIC = process.env.PB_STATIC_DIR ? path.resolve(process.env.PB_STATIC_DIR) : path.resolve(here, "../../src/paybroker/static");
const KIT = process.env.PB_KIT_DIR ? path.resolve(process.env.PB_KIT_DIR) : path.resolve(here, "../../../agentpg-kit/src/apgkit/static");
register("data:text/javascript," + encodeURIComponent(`export async function resolve(s, c, n) {
  if (s.startsWith("/kit/")) return { url: ${JSON.stringify(pathToFileURL(KIT).href)} + "/" + s.slice(5), shortCircuit: true };
  if (s.startsWith("/static/")) return { url: ${JSON.stringify(pathToFileURL(STATIC).href)} + "/" + s.slice(8), shortCircuit: true };
  return n(s, c);
}`));

const modals = [];   // open modal dialogs, last = topmost
class Text { constructor(d) { this.nodeType = 3; this.data = String(d); this.parent = null; } get textContent() { return this.data; } }

export class El {
  constructor(tag) {
    this.tag = tag; this.nodeType = 1; this.children = []; this.parent = null; this.attrs = {}; this.dataset = {};
    this._value = ""; this.checked = false; this.disabled = false; this.hidden = false; this.open = false; this.className = "";
    this.listeners = {}; this.files = []; this.classes = new Set(); this.classList = { toggle() {}, add: (c) => this.classes.add(c), remove: (c) => this.classes.delete(c), contains: (c) => this.classes.has(c) }; this.style = {};
  }
  get id() { return this.attrs.id || ""; }
  set id(v) { this.attrs.id = String(v); }
  get value() { return this._value; }
  set value(v) { this._value = String(v); }
  get isConnected() { let e = this; while (e.parent) e = e.parent; return e === document.body; }
  _adopt(k) { if (k.parent) k.parent.children = k.parent.children.filter((c) => c !== k); k.parent = this; return k; }
  append(...kids) {
    for (const k of kids) {
      this.children.push(this._adopt(k));
      if (this.tag === "select" && k.tag === "option" && (this.children.length === 1 || k.selected)) this._value = k._value;   // a select shows its selected (else first) option
    }
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }   // confirmDialog removes its dialog
  replaceChildren(...kids) { for (const c of this.children) c.parent = null; this.children = []; this.append(...kids); }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k === "checked") this.checked = true;
    if (k === "disabled") this.disabled = true;
    if (k === "selected") this.selected = true;
    if (k === "hidden") this.hidden = true;
  }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.replaceChildren(new Text(v)); }
  focus() { document.activeElement = this; }
  // <dialog>: showModal() stacks it  (the browser makes everything outside the topmost modal dialog inert), close() unstacks it.
  showModal() { this.open = true; if (!modals.includes(this)) modals.push(this); this.attrs.open = ""; }
  close() { this.open = false; delete this.attrs.open; const i = modals.indexOf(this); if (i >= 0) modals.splice(i, 1); }
  contains(n) { for (let e = n; e; e = e.parent) if (e === this) return true; return false; }
  matches() { return false; }
  _all(out = []) { for (const c of this.children) if (c.nodeType === 1) { out.push(c); c._all(out); } return out; }
  querySelectorAll(sel) {
    const tests = sel.split(",").map((s) => s.trim()).map((s) => {
      const [tag, pseudo] = s.split(":");
      return (e) => (!tag || e.tag === tag) && (pseudo !== "checked" || e.checked);
    });
    return this._all().filter((e) => tests.some((t) => t(e)));
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

const body = new El("body");
globalThis.document = {
  body, activeElement: null, documentElement: { dataset: { csrf: "X-PayBroker-CSRF" } },
  getElementById: (id) => body._all().find((e) => e.id === id) || null,
  createElement: (t) => new El(t), createElementNS: (_, t) => new El(t), createTextNode: (d) => new Text(d),
  querySelectorAll: (s) => body.querySelectorAll(s),
};
globalThis.window = { dispatchEvent() {}, addEventListener() {} };
globalThis.CustomEvent = class {};
globalThis.location = { origin: "http://x", hash: "", search: "", pathname: "/" };
// No window.confirm/alert/prompt here on purpose: the pages use the kit's confirmDialog/promptDialog (a native modal <dialog> under <body>), and a
// native call would throw. Tests answer the dialog with `dialogs.answer(...)`, or leave `dialogs.auto` on so one is accepted as it opens.

/** The kit's confirm/prompt modal: every dialog that opened is recorded; with `auto` on (default) each is accepted, or cancelled when
 *  `auto = "cancel"`, as soon as it mounts (typing the phrase when one is required). Handlers that `await` a dialog only continue then. */
export const dialogs = { seen: [], auto: "ok", overlay: () => document.body.children.find((c) => c.tag === "dialog" && c.className === "confirm-dialog") || null };
dialogs.button = (label) => dialogs.overlay()?._all().find((e) => e.tag === "button" && e.textContent === label) || null;
dialogs.answer = async (ok, { value } = {}) => {
  const o = dialogs.overlay();
  if (!o) throw new Error("no dialog is open");
  const input = o._all().find((e) => e.tag === "input" || e.tag === "textarea");
  if (ok && input && value !== undefined) { input.value = value; await fire(input, "input"); }
  const btns = o._all().filter((e) => e.tag === "button");
  await fire(ok ? btns[1] : btns[0], "click");   // cancel is first, confirm second
};
/** Escape as the browser delivers it: keydown first, then the native `cancel` of the topmost modal dialog unless the keydown was handled. */
dialogs.escape = async () => {
  const top = modals[modals.length - 1];
  if (!top) throw new Error("no modal dialog is open");
  const ev = await fire(top, "keydown", { key: "Escape" });
  if (!ev.defaultPrevented && modals.includes(top)) await fire(top, "cancel");
};
setInterval(async () => {
  const o = dialogs.overlay();
  if (!o || o._seen) return;
  o._seen = true;
  const text = (id) => o._all().find((e) => e.attrs.id?.startsWith(id))?.textContent;
  const input = o._all().find((e) => e.tag === "input" || e.tag === "textarea");
  const phrase = input?.attrs["aria-label"]?.startsWith("Type ") ? input.attrs["aria-label"].slice(5, -" to confirm".length) : null;
  dialogs.seen.push({ title: text("confirm-title"), message: text("confirm-msg"), danger: o._all().some((e) => e.className === "danger-solid"), phrase, value: phrase ? null : input?.value ?? null });
  if (dialogs.auto === "ok") await dialogs.answer(true, phrase ? { value: phrase } : {}).catch(() => {});
  else if (dialogs.auto === "cancel") await dialogs.answer(false);
}, 1);   // keeps the loop alive while a handler waits on a dialog; done() exits

/** The static page shell: the elements index.html provides. */
export function shell(ids) {
  const make = (id, tag = "div") => { const e = new El(tag); e.setAttribute("id", id); return e; };
  for (const win of ["detail", "form"]) { const w = make(win, "dialog"); w.append(make(`${win}-body`)); body.append(w); }   // the app windows are <dialog>s holding their body
  for (const id of ["toast", ...ids]) body.append(make(id));
}

/** Stub fetch with routes: { "GET /api/rules": body | (init) => body | Response-like {status, body} }. Every call is recorded. */
export const calls = [];
export function route(table) {
  globalThis.fetch = async (p, init = {}) => {
    const key = `${init.method || "GET"} ${p.split("?")[0]}`;
    calls.push({ key, path: p, body: init.body ? JSON.parse(init.body) : undefined });
    const hit = table[key];
    if (hit === undefined) return reply(404, { detail: `no stub for ${key}` });
    const out = typeof hit === "function" ? await hit(init, p) : hit;
    return out && out.__status ? reply(out.__status, out.body) : reply(200, out);
  };
}
const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => body, text: async () => JSON.stringify(body) });
export const failWith = (status, detail) => ({ __status: status, body: { detail } });

export async function loginAs(permissions) {
  const { state } = await import("/kit/session.js");
  state.session = { state: "logged_in", user: { permissions } };
}

export const find = (root, id) => (root.attrs?.id === id ? root : (root.children || []).map((c) => find(c, id)).find(Boolean));
export const live = (id) => document.getElementById(id);                  // the element currently in the document with this id
export const text = (el) => el.textContent;
export const byLabel = (label) => document.body._all().find((e) => e.attrs["aria-label"] === label) || null;
export const hasText = (needle) => document.body.textContent.includes(needle);
export const up = (el, tag) => { let e = el; while (e && e.tag !== tag) e = e.parent; return e; };
export function byText(root, tag, label) {
  return root._all().find((e) => e.tag === tag && e.textContent === label) || null;
}
export const allByText = (root, tag, label) => root._all().filter((e) => e.tag === tag && e.textContent === label);

/** Dispatch a DOM event to the element's listeners (and await them). */
export async function fire(el, type, extra = {}) {
  const ev = { type, target: el, key: "", preventDefault() { this.defaultPrevented = true; }, defaultPrevented: false, ...extra };
  const top = modals[modals.length - 1];
  if (top && !top.contains(el) && el !== top) return ev;   // inert: input outside the topmost modal dialog is not delivered
  for (const f of el.listeners[type] || []) await f(ev);
  return ev;
}
export const click = (el) => fire(el, "click");
export async function type(el, value) { el.value = value; await fire(el, "input"); }
/** Let pending promises and 0-delay timers run (an async refresh the page does not await). */
export const settle = async (ms = 30) => { for (let i = 0; i < 3; i++) await new Promise((r) => setTimeout(r, ms)); };

let failures = 0;
export function check(cond, message) {
  if (cond) return;
  failures++;
  console.error("FAIL: " + message);
}
export function done(okMessage) {
  if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
  console.log("ok: " + okMessage);
  process.exit(0);
}
