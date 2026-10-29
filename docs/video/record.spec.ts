/**
 * Recorder for docs/VIDEO-SCRIPT.md: performs every on-screen action of the tutorial in real Chrome, at 1920x1080, with a visible
 * cursor, highlighted elements and lower-third callouts, and saves one video. `python3 build.py` runs it with the narration timing.
 *
 * Everything is local and fresh on every take: a throwaway Postgres on 127.0.0.1 and a PayBroker that has never been set up (the
 * script starts from the setup page), no AI account (the built-in rule-based triage), ports from PB_PORT_RANGE (default any;
 * build.py uses 41700-41799). The shop's return page, an on-screen terminal and the signed-webhook receiver are stand-ins
 * the recorder starts itself.
 *
 *   cd docs/video && npm install
 *   python3 build.py                                   # the whole review cut
 *   PACE=0.3 REC_VIDEO=off npx playwright test         # quick rehearsal of the actions only (no narration timing)
 *
 * Environment: NARRATION_JSON and TIMES_JSON (set by build.py: cue times in, scene start times out), PB_PORT_RANGE, PB_PG_BIN,
 * PB_ADMIN_PASSWORD (optional; otherwise a random throwaway password is made for the take and never written anywhere),
 * PACE (1; scales every wait), REC_SHOTS=<dir> (also save a screenshot at every callout, for a visual check), REC_VIDEO=off,
 * HEADED=1.
 * The recorder asserts what the script claims (403 with NEW_ACCOUNT, 402 CARD_DECLINED, REQUIRES_ACTION then the signed return,
 * routing error then approved, signature ok for the three event types, delivered events), so a take that drifts fails.
 * Keys and the signing secret are blurred the moment they are rendered and masked a second later.
 */
import { test, expect, type Locator, type Page } from '@playwright/test';
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import { spawnSync } from 'node:child_process';
import * as http from 'node:http';
import * as path from 'node:path';
import { freePort, startStack, type Stack } from './lib';

const PACE = Number(process.env.PACE || 1);
const OUT = path.join(__dirname, 'out');

// Cards for scenes 6a to 6c: Luhn-valid numbers in the reserved test range that behave like 4000000000000002 (issuer declines),
// 4000002500003155 (always 3-D Secure) and 4000000000000119 (first connector fails) but that the demo traffic of scene 2 never uses,
// so the per-card velocity rule does not trip and every take is the same.
const CARD_DECLINES = '9999990100000070';
const CARD_3DS = '9999990200000079';
const CARD_CASCADE = '9999990300000078';

let stack: Stack;
let PB = '';
let SHOP_PORT = 0;
let HOOK_PORT = 0;
let SHOP = '';

test.beforeAll(async () => {
  stack = await startStack();
  PB = stack.url;
  SHOP_PORT = await freePort();
  HOOK_PORT = await freePort();
  SHOP = `http://localhost:${SHOP_PORT}`;
});
test.afterAll(async () => {
  await stack?.stop();
});

// ---- narration sync ------------------------------------------------------------------------------------------------
// build.py speaks narration.txt first and hands the result over (NARRATION_JSON: {"1": {duration, cues: [{text, start, end}]}}).
// A scene lasts at least LEAD + its narration + TAIL (audio + 0.7 s). Every on-screen action waits for the sentence that
// illustrates it: `await at(page, 'words of the sentence')` blocks until that sentence starts (minus a lead, because the action
// itself takes a moment to show); `atEnd` waits for its end. A late action runs at once and the delay goes to TIMES_JSON ("sync").
const LEAD = 0.3;
const TAIL = 0.4;
type Cue = { text: string; start: number; end: number };
const NARRATION: Record<string, { duration: number; cues: Cue[] }> = process.env.NARRATION_JSON ? JSON.parse(fs.readFileSync(process.env.NARRATION_JSON, 'utf8')) : {};
const T0 = { at: 0 };
const SC = { n: 0, start: 0, targetMs: 0 };
const times: { scene: number; start: number; end: number }[] = [];
const sync: { scene: number; key: string; cueAt: number; actualAt: number; lateBy: number }[] = [];
const rel = () => (Date.now() - T0.at) / 1000;
function begin(n: number) {
  SC.n = n;
  SC.start = Date.now();
  SC.targetMs = (LEAD + (NARRATION[String(n)]?.duration ?? 20) + TAIL) * 1000;
  times.push({ scene: n, start: rel(), end: 0 });
}
async function waitUntil(page: Page, key: string, which: 'start' | 'end', offset: number) {
  const cues = NARRATION[String(SC.n)]?.cues;
  if (!cues) return;
  const cue = cues.find((c) => c.text.includes(key));
  if (!cue) throw new Error(`scene ${SC.n}: no narrated sentence contains "${key}"`);
  const due = (LEAD + cue[which] + offset) * 1000;
  const now = Date.now() - SC.start;
  if (now < due) await page.waitForTimeout(due - now);
  const late = (Date.now() - SC.start - due) / 1000;
  sync.push({ scene: SC.n, key, cueAt: +(LEAD + cue[which]).toFixed(2), actualAt: +((Date.now() - SC.start) / 1000).toFixed(2), lateBy: +late.toFixed(2) });
  if (late > 1) console.log(`scene ${SC.n}: "${key}" is ${late.toFixed(1)} s late`);
}
const at = (page: Page, key: string, lead = 0.4) => waitUntil(page, key, 'start', -lead);
const atEnd = (page: Page, key: string, offset = 0) => waitUntil(page, key, 'end', offset);
/** Time (s) from now to when the sentence containing `key` starts, for animations that run inside one sentence. */
const pause = (page: Page, ms: number) => page.waitForTimeout(Math.round(ms * PACE));
async function endScene(page: Page) {
  const left = SC.targetMs - (Date.now() - SC.start);
  if (left > 0) await page.waitForTimeout(left);
  times[times.length - 1].end = rel();
}

// ---- the on-screen helpers -------------------------------------------------------------------------------------------

/** Cursor dot, callout lower third, highlight and key-blur styles, injected into every document. Popovers sit above modal windows. */
function overlay() {
  const boot = () => {
    if (document.getElementById('pb-callout')) return;
    const sheet = new CSSStyleSheet(); // a constructed sheet: PayBroker's content security policy refuses inline <style> elements
    sheet.replaceSync(`
      [popover].pb { position: fixed; inset: auto; margin: 0; padding: 0; border: 0; background: transparent; overflow: visible; }
      #pb-callout { left: 50%; bottom: 56px; transform: translateX(-50%); max-width: 1500px; background: rgba(15,23,42,.94); color: #fff;
        font: 600 36px/1.25 system-ui, sans-serif; padding: 16px 32px; border-radius: 14px; box-shadow: 0 8px 30px rgba(0,0,0,.35); text-align: center; }
      #pb-cursor { width: 30px; height: 30px; border-radius: 50%; background: rgba(245,158,11,.35); border: 3px solid #f59e0b; pointer-events: none; left: -100px; top: -100px; transform: translate(-50%, -50%); }
      #pb-cursor.down { transform: translate(-50%, -50%) scale(.6); background: rgba(245,158,11,.8); }
      .pb-spot { outline: 6px solid #f59e0b !important; outline-offset: 8px; border-radius: 8px; animation: pb-pulse 1.1s ease-in-out infinite; }
      @keyframes pb-pulse { 50% { outline-color: rgba(245,158,11,.35); } }
      #detail .codewrap code { filter: blur(11px); }`); // a key or signing secret is never legible: blurred from the moment it renders
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    const mk = (id: string) => { const d = document.createElement('div'); d.id = id; d.className = 'pb'; d.setAttribute('popover', 'manual'); document.body.appendChild(d); return d; };
    const callout = mk('pb-callout');
    const cursor = mk('pb-cursor');
    const top = () => { for (const el of [callout, cursor]) { try { if (el.matches(':popover-open')) { el.hidePopover(); el.showPopover(); } } catch { /* ignore */ } } };
    cursor.showPopover();
    new MutationObserver(() => setTimeout(top, 0)).observe(document.documentElement, { attributes: true, attributeFilter: ['open'], subtree: true });
    addEventListener('mousemove', (e) => { cursor.style.left = e.clientX + 'px'; cursor.style.top = e.clientY + 'px'; }, true);
    addEventListener('mousedown', () => cursor.classList.add('down'), true);
    addEventListener('mouseup', () => cursor.classList.remove('down'), true);
    let timer: any;
    (window as any).__pbCallout = (text: string | null, ms = 3600) => {
      clearTimeout(timer);
      try { callout.hidePopover(); } catch { /* ignore */ }
      if (!text) return;
      callout.textContent = text;
      callout.showPopover();
      timer = setTimeout(() => { try { callout.hidePopover(); } catch { /* ignore */ } }, ms);
    };
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
}

let shots = 0;
/** A lower-third caption for `sec` seconds (a later one replaces it); does not wait. */
async function callout(page: Page, text: string, sec = 3.6) {
  await page.evaluate(([t, ms]) => (window as any).__pbCallout?.(t, ms), [text, Math.round(sec * 1000)] as const).catch(() => {});
  if (process.env.REC_SHOTS) {
    await page.waitForTimeout(600);
    await page.screenshot({ path: path.join(process.env.REC_SHOTS, `${String(++shots).padStart(2, '0')}-s${SC.n}-${text.slice(0, 24).replace(/\W+/g, '_')}.png`) });
  }
}
const noCallout = (page: Page) => page.evaluate(() => (window as any).__pbCallout?.(null)).catch(() => {});

/** Pulsing outline on an element until the returned function is called. */
async function hl(loc: Locator): Promise<() => Promise<void>> {
  await loc.first().scrollIntoViewIfNeeded();
  await loc.first().evaluate((el) => el.classList.add('pb-spot'));
  return () => loc.first().evaluate((el) => el.classList.remove('pb-spot')).catch(() => {});
}
async function spot(page: Page, loc: Locator, ms: number) {
  const off = await hl(loc);
  await pause(page, ms);
  await off();
}
async function glide(page: Page, loc: Locator) {
  await loc.first().scrollIntoViewIfNeeded();
  const b = await loc.first().boundingBox();
  if (b) await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 24 });
  await pause(page, 250);
}
async function click(page: Page, loc: Locator, after = 600) {
  await glide(page, loc);
  await loc.click();
  await pause(page, after);
}
async function type(page: Page, loc: Locator, text: string, delay = 45) {
  await glide(page, loc);
  await loc.click();
  await loc.pressSequentially(text, { delay: delay * PACE });
  await pause(page, 400);
}
const mask = (loc: Locator) => loc.evaluateAll((els) => els.forEach((e) => (e.textContent = 'shown once (masked)')));

// ---- the stand-ins: shop page, on-screen terminal, cards, and the webhook receiver -----------------------------------------

const TERMINAL_HTML = `<!doctype html><meta charset="utf-8"><title>terminal</title>
<style>
  html, body { margin: 0; height: 100%; background: #0b1220; }
  #t { box-sizing: border-box; height: 100vh; padding: 96px 48px 150px; overflow: hidden; color: #e2e8f0; font: 23px/1.32 ui-monospace, Menlo, Consolas, monospace; white-space: pre-wrap; word-break: break-all; display: flex; flex-direction: column; justify-content: flex-start; }
  #t > div { flex: none; }
  #bar { position: fixed; left: 0; right: 0; top: 0; height: 72px; display: flex; gap: 18px; align-items: center; padding: 0 48px; background: #0f1a2e; border-bottom: 2px solid #1e293b; font: 700 26px system-ui, sans-serif; }
  .tg { padding: 6px 16px; border-radius: 9px; color: #0b1220; } .tg.platform { background: #38bdf8; } .tg.app { background: #4ade80; } .tg.scaf { background: #fbbf24; }
  .tg.cap { background: #fb923c; font-size: 30px; box-shadow: 0 0 0 4px rgba(251,146,60,.35); }
  .p { color: #4ade80; } .c { color: #f8fafc; } .o { color: #94a3b8; margin-bottom: 12px; }
  mark { background: #f59e0b; color: #0b1220; padding: 1px 6px; border-radius: 5px; font-weight: 700; animation: mk 1.1s ease-in-out infinite; }
  @keyframes mk { 50% { background: #fcd34d; box-shadow: 0 0 0 4px rgba(245,158,11,.35); } }
</style><div id="bar"></div><div id="t"></div>
<script>
  const t = document.getElementById('t');
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const line = (cls, text) => { const d = document.createElement('div'); d.className = cls; d.textContent = text; t.appendChild(d); while (t.scrollHeight > t.clientHeight + 2 && t.children.length > 1) t.removeChild(t.firstChild); return d; };
  window.__term = {
    restore(items) { t.textContent = ''; for (const i of items) { if (i.cmd) line('c', '$ ' + i.cmd); if (i.out) line('o', i.out); } },
    async type(cmd, ms) { const d = line('c', '$ '); const step = Math.max(1, Math.ceil(cmd.length / (ms / 22))); for (let i = 0; i < cmd.length; i += step) { d.textContent = '$ ' + cmd.slice(0, i + step); await sleep(ms / Math.ceil(cmd.length / step)); } await sleep(250); },
    out(text) { line('o', text); },
    tags(items) { const b = document.getElementById('bar'); b.textContent = ''; for (const i of items) { const d = document.createElement('div'); d.className = 'tg ' + i.cat; d.textContent = i.text; b.appendChild(d); } },
    mark(patterns) { for (const d of t.querySelectorAll('.o, .c')) { let h = d.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;'); for (const p of patterns) h = h.replace(new RegExp(p, 'g'), (m) => '<mark>' + m + '</mark>'); d.innerHTML = h; } },
    unmark() { for (const d of t.querySelectorAll('.o, .c')) d.textContent = d.textContent; },
  };
</script>`;

const CARD_HTML = `<!doctype html><meta charset="utf-8"><title>card</title>
<style>
  html, body { margin: 0; height: 100%; background: #0b1220; color: #f8fafc; font-family: system-ui, sans-serif; }
  body { display: grid; place-items: center; text-align: center; }
  h1 { font-size: 92px; margin: 0 0 20px; } .sub { font-size: 38px; margin: 0 0 18px; color: #94a3b8; white-space: pre-line; }
  .rv { opacity: 0; transform: translateY(14px); transition: opacity .5s, transform .5s; font-size: 46px; margin: 14px 0; }
  .rv.show { opacity: 1; transform: none; } .rv.q { color: #fcd34d; } .rv.bad { color: #fca5a5; } .rv.a { color: #4ade80; font-weight: 700; } .rv.cmd { font: 40px ui-monospace, Menlo, monospace; color: #e2e8f0; margin: 8px 0; }
  .rv.gone { display: none; }
</style><div id="c"></div>
<script>
  window.__card = (title, sub, items) => {
    const c = document.getElementById('c');
    c.innerHTML = '<h1></h1><p class="sub"></p><div id="items"></div>';
    c.querySelector('h1').textContent = title; c.querySelector('.sub').textContent = sub;
    items.forEach((i, n) => { const d = document.createElement('div'); d.className = 'rv ' + (i.cls || ''); d.textContent = i.text; d.dataset.k = n; c.querySelector('#items').appendChild(d); });
  };
  window.__show = (n) => { const d = document.querySelector('[data-k="' + n + '"]'); if (d) d.classList.add('show'); };
  window.__hide = (n) => { const d = document.querySelector('[data-k="' + n + '"]'); if (d) { d.classList.remove('show'); d.classList.add('gone'); } };
</script>`;


const STILL_HTML = `<!doctype html><meta charset="utf-8"><title>captured</title>
<style>
  html, body { margin: 0; height: 100%; background: #0b1220; overflow: hidden; font-family: system-ui, sans-serif; }
  #bar { position: fixed; left: 0; right: 0; top: 0; height: 80px; display: flex; align-items: center; justify-content: center; gap: 24px; background: #0f1a2e; border-bottom: 2px solid #1e293b; font: 700 34px system-ui; }
  .tg { padding: 6px 20px; border-radius: 10px; color: #0b1220; } .tg.cap { background: #fb923c; box-shadow: 0 0 0 4px rgba(251,146,60,.35); } .tg.platform { background: #38bdf8; }
  #stage { position: absolute; left: 48px; top: 80px; width: 1824px; height: 1000px; overflow: hidden; background: #fff; }
  #w { position: absolute; inset: 0; transform-origin: 50% 50%; } #w img { width: 100%; height: 100%; display: block; }
  #box { position: absolute; border: 6px solid #f59e0b; border-radius: 8px; box-shadow: 0 0 0 9999px rgba(11,18,32,.28); opacity: 0; transition: opacity .4s; }
</style><div id="bar"><span class="tg cap">CAPTURED from the live AgentPG fleet, 28 Oct 2026</span><span class="tg platform">PLATFORM: EDB extensions</span></div>
<div id="stage"><div id="w"><img id="img" alt="captured screenshot"><div id="box"></div></div></div>
<script>
  // a shot: which capture, the zoom origin (% of the image), zoom from/to over ms, and a highlight box (% of the image: l, t, w, h)
  window.__shot = (src, ox, oy, z0, z1, ms, box) => {
    const img = document.getElementById('img'), w = document.getElementById('w'), b = document.getElementById('box');
    if (!img.src.endsWith(src)) img.src = '/live/' + src;
    w.style.transition = 'none'; w.style.transformOrigin = ox + '% ' + oy + '%'; w.style.transform = 'scale(' + z0 + ')';
    void w.offsetWidth;
    w.style.transition = 'transform ' + ms + 'ms linear'; w.style.transform = 'scale(' + z1 + ')';
    if (box) { b.style.left = box[0] + '%'; b.style.top = box[1] + '%'; b.style.width = box[2] + '%'; b.style.height = box[3] + '%'; b.style.opacity = 1; } else b.style.opacity = 0;
  };
</script>`;
const LIVE_STILLS = ['paybroker-insights-top-queries-2026-10-28.jpg', 'paybroker-insights-masking-sql-in-monitor-2026-10-28.jpg', 'paybroker-insights-tuner-advisor-2026-10-28.jpg'];

const DIAGRAM_HTML = `<!doctype html><meta charset="utf-8"><title>diagram</title>
<style>
  html, body { margin: 0; height: 100%; background: #0b1220; color: #f8fafc; font-family: system-ui, sans-serif; overflow: hidden; }
  #d { position: relative; width: 1920px; height: 1080px; }
  .box { position: absolute; box-sizing: border-box; border: 5px solid; border-radius: 20px; padding: 18px 26px; opacity: 0; transition: opacity .5s, box-shadow .3s; }
  .box.show { opacity: 1; } .box.hot { box-shadow: 0 0 0 9px rgba(245,158,11,.55); }
  .tag { display: inline-block; font: 700 24px system-ui; letter-spacing: 2px; padding: 4px 14px; border-radius: 8px; margin-bottom: 10px; color: #0b1220; }
  .platform { border-color: #38bdf8; background: rgba(56,189,248,.10); } .platform .tag, .sw.platform i { background: #38bdf8; }
  .app { border-color: #4ade80; background: rgba(74,222,128,.10); } .app .tag, .sw.app i { background: #4ade80; }
  .scaf { border-color: #fbbf24; background: rgba(251,191,36,.10); } .scaf .tag, .sw.scaf i { background: #fbbf24; }
  h2 { font-size: 40px; margin: 0 0 8px; } p { font-size: 30px; margin: 6px 0; color: #cbd5e1; } .chip { font-size: 30px; margin: 12px 0; color: #e2e8f0; }
  .arrow { position: absolute; height: 6px; background: #64748b; opacity: 0; transition: opacity .5s; } .arrow.show { opacity: 1; }
  #legend { position: absolute; left: 60px; right: 60px; top: 730px; display: flex; gap: 48px; align-items: center; opacity: 0; transition: opacity .5s; font-size: 28px; }
  #legend.show { opacity: 1; } .sw { display: flex; align-items: center; gap: 12px; } .sw i { width: 34px; height: 34px; border-radius: 8px; display: inline-block; }
  #note { position: absolute; left: 0; right: 0; top: 820px; text-align: center; font-size: 40px; color: #94a3b8; opacity: 0; transition: opacity .5s; } #note.show { opacity: 1; }
  .cmd { position: absolute; left: 60px; right: 60px; font: 38px ui-monospace, Menlo, monospace; color: #e2e8f0; opacity: 0; transition: opacity .5s; } .cmd.show { opacity: 1; } .cmd em { color: #94a3b8; font-style: normal; font-size: 28px; }
</style><div id="d">
  <div class="box platform" data-k="0" style="left:60px;top:90px;width:800px;height:600px"><span class="tag">PLATFORM</span><h2>Postgres on AgentPG (EDB)</h2>
    <div class="chip">audit ledger: SHA-256 chain, same transaction</div><div class="chip">row-level security, forced</div><div class="chip">masked views for read-only roles</div><div class="chip">fuzzystrmatch: name-mismatch check</div><div class="chip">EDB insights: edb_stat_monitor, query_advisor</div></div>
  <div class="arrow" data-a="1" style="left:860px;top:386px;width:60px"></div>
  <div class="box app" data-k="1" style="left:920px;top:90px;width:430px;height:600px"><span class="tag">SAMPLE APP</span><h2>PayBroker</h2><p>fraud checks, routing, dashboard, API</p><p>test cards only</p></div>
  <div class="arrow" data-a="2" style="left:1350px;top:386px;width:60px"></div>
  <div class="box scaf" data-k="2" style="left:1410px;top:90px;width:450px;height:190px"><span class="tag">REFERENCE SCAFFOLDING</span><h2>Sign-in</h2><p>any OpenID Connect provider</p></div>
  <div class="box scaf" data-k="3" style="left:1410px;top:300px;width:450px;height:190px"><span class="tag">REFERENCE SCAFFOLDING</span><h2>LLM gateway</h2><p>OpenAI-compatible API</p></div>
  <div class="box scaf" data-k="4" style="left:1410px;top:510px;width:450px;height:180px"><span class="tag">REFERENCE SCAFFOLDING</span><h2>Secret vault</h2><p>shared-kit module</p></div>
  <div id="legend" data-k="6"><div class="sw platform"><i></i>PLATFORM: AgentPG and EDB Postgres</div><div class="sw scaf"><i></i>REFERENCE SCAFFOLDING: swappable</div><div class="sw app"><i></i>SAMPLE APP: PayBroker</div></div>
  <div id="note" data-k="5">Only test cards and simulated processors: nothing moves real money</div>
  <div class="cmd" data-k="7" style="top:810px">$ ecoctl apply --only paybroker --execute <em>   # on AgentPG</em></div>
  <div class="cmd" data-k="8" style="top:870px">$ docker compose up -d db <em>   # or locally, with Docker</em></div>
  <div class="cmd" data-k="9" style="top:930px">$ paybroker migrate &amp;&amp; paybroker serve</div>
  <div class="cmd" data-k="10" style="top:995px;color:#4ade80">docs/GUIDE.md  <em>section 18: audit ledger and row-level security</em></div>
</div>
<script>
  window.__show = (n) => { const d = document.querySelector('[data-k="' + n + '"]'); if (d) d.classList.add('show'); if (n == 1) document.querySelector('[data-a="1"]').classList.add('show'); if (n >= 2 && n <= 4) document.querySelector('[data-a="2"]').classList.add('show'); };
  window.__hot = (...ns) => { document.querySelectorAll('.box').forEach((b) => b.classList.toggle('hot', ns.includes(Number(b.dataset.k)))); };
</script>`;

function startStandIns(state: { secret: string; lines: string[] }) {
  const shop = http.createServer((req, res) => {
    const url = (req.url || '').split('?')[0];
    if (url === '/return.html') {
      res.writeHead(200, { 'content-type': 'text/html' });
      res.end('<!doctype html><meta charset="utf-8"><title>Acme Shop</title><body style="font:36px system-ui;padding:80px"><h1>Back at Acme Shop</h1><p>Thanks. The webhook confirms the payment.</p>' +
        '<p style="font:24px ui-monospace,monospace;color:#475569;word-break:break-all">The shop can verify this signed link: <span id="u"></span></p><script>u.textContent = location.pathname + location.search</script>');
    } else if (url === '/terminal.html') {
      res.writeHead(200, { 'content-type': 'text/html' });
      res.end(TERMINAL_HTML);
    } else if (url === '/diagram.html') {
      res.writeHead(200, { 'content-type': 'text/html' });
      res.end(DIAGRAM_HTML);
    } else if (url === '/still.html') {
      res.writeHead(200, { 'content-type': 'text/html' });
      res.end(STILL_HTML);
    } else if (url.startsWith('/live/') && LIVE_STILLS.includes(url.slice(6))) {
      res.writeHead(200, { 'content-type': 'image/jpeg' });
      res.end(fs.readFileSync(path.join(LIVE, url.slice(6))));
    } else if (url === '/card.html') {
      res.writeHead(200, { 'content-type': 'text/html' });
      res.end(CARD_HTML);
    } else {
      res.writeHead(404);
      res.end();
    }
  });
  const hook = http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString();
      const ts = String(req.headers['x-timestamp'] || '0');
      const want = crypto.createHmac('sha256', state.secret).update(`${ts}.${raw}`).digest('hex');
      const got = String(req.headers['x-signature'] || '');
      const ok = got.length === want.length && crypto.timingSafeEqual(Buffer.from(got), Buffer.from(want)) && Math.abs(Date.now() / 1000 - Number(ts)) < 300;
      state.lines.push((ok ? 'signature ok   ' : 'SIGNATURE BAD  ') + JSON.parse(raw).type);
      res.writeHead(ok ? 200 : 400);
      res.end();
    });
  });
  return Promise.all([new Promise<void>((r) => shop.listen(SHOP_PORT, '127.0.0.1', r)), new Promise<void>((r) => hook.listen(HOOK_PORT, '127.0.0.1', r))]).then(() => ({
    close: () => { shop.close(); hook.close(); },
  }));
}

// ---- the shop's calls, as the script shows them ----------------------------------------------------------------------------

async function tokenize(pk: string, number: string): Promise<string> {
  const r = await fetch(PB + '/v1/vault/tokens', {
    method: 'POST', headers: { authorization: 'Bearer ' + pk, 'content-type': 'application/json' },
    body: JSON.stringify({ number, exp_month: 12, exp_year: 2035, cvc: '123' }),
  });
  const j: any = await r.json();
  if (!j.token) throw new Error('tokenize failed: ' + r.status);
  return j.token;
}
async function authorize(sk: string, body: Record<string, unknown>): Promise<{ status: number; json: any }> {
  const r = await fetch(PB + '/v1/payments/authorize', { method: 'POST', headers: { authorization: 'Bearer ' + sk, 'content-type': 'application/json' }, body: JSON.stringify({ currency: 'USD', ...body }) });
  return { status: r.status, json: await r.json() };
}
async function action(sk: string, paymentId: string, name: 'capture' | 'refund', body?: Record<string, unknown>): Promise<{ status: number; json: any }> {
  const r = await fetch(`${PB}/v1/payments/${paymentId}/${name}`, { method: 'POST', headers: { authorization: 'Bearer ' + sk, 'content-type': 'application/json' }, body: JSON.stringify(body ?? {}) });
  return { status: r.status, json: await r.json() };
}
/** The curl-helper call as it would be typed: the first keys on one line, one key per line after that, the token swapped for a shell variable. */
function shownAuth(body: Record<string, unknown>, tokenExpr: string): string {
  const e = Object.entries(body);
  const head = e.slice(0, 3).map(([k, v]) => `"${k}":${JSON.stringify(v)}`).join(',');
  const rest = e.slice(3).map(([k, v]) => `"${k}":${JSON.stringify(v)}`);
  const json = [head, ...rest].join(',\n  ').replace(/"card_token":"[^"]*"/, `"card_token":"'"${tokenExpr}"'"`);
  return `auth '{${json}}' | jq`;
}

// ---- real database calls for the "under the hood" scenes ---------------------------------------------------------------------------
/** Runs psql against the throwaway Postgres as `user` (the owner `postgres` or the read-only login role); returns what the terminal shows, errors included. */
function psql(user: 'postgres' | 'pb_owner' | 'reporting_reader' | 'ai_agent', args: string[], input?: string, extraEnv: Record<string, string> = {}): string {
  const c = stack.cluster;
  const r = spawnSync(path.join(c.bin, 'psql'), ['-X', '-h', '127.0.0.1', '-p', String(c.port), '-U', user, '-d', c.dbName, '-P', 'pager=off', '-v', 'VERBOSITY=terse', ...args], { input, encoding: 'utf8', env: { PATH: `${c.bin}:/usr/bin:/bin`, LC_ALL: 'C', ...extraEnv } });
  return `${r.stdout ?? ''}${r.stderr ?? ''}`.replace(/\s+$/, '');
}
/** The same text the terminal shows is the text that runs: `sql` / `$DB` are the app's owner role (an ordinary role, not a superuser), `$RO` the read-only login. None holds a password. */
const ownerSql = (sql: string, extra: string[] = []) => psql('pb_owner', [...extra, '-c', sql]);
/** What the app's own connection does for the operator dashboard: `app.bypass = on` (see row-level security). */
const ownerSqlBypass = (sql: string, extra: string[] = []) => psql('pb_owner', [...extra, '-c', sql], undefined, { PGOPTIONS: '-c app.bypass=on' });
const reader = (sql: string) => psql('reporting_reader', ['-c', sql]);
const LIVE = path.resolve(__dirname, '..', '..', '..', 'videos', 'live-captures');
/** One instance's block of a capture file (`# name (id): command` blocks, or `name (id)` blocks); the instance id is dropped from the heading. */
function captured(file: string, name: string): string {
  const txt = fs.readFileSync(path.join(LIVE, file), 'utf8');
  const blocks = txt.split(/\n\n/);
  const b = blocks.find((x) => new RegExp(`^(# )?${name} \\(pg-[0-9a-f]+\\)`).test(x.trim()));
  if (!b) throw new Error(`${file}: no block for ${name}`);
  return b.trim().split('\n').slice(1).join('\n');
}

// ---- the recording ------------------------------------------------------------------------------------------------------

test('PayBroker tutorial video', async ({ page }) => {
  test.setTimeout(30 * 60_000);
  const adminPassword = process.env.PB_ADMIN_PASSWORD || crypto.randomBytes(9).toString('hex'); // throwaway, per take
  const hooks = { secret: 'unset', lines: [] as string[] };
  const standIns = await startStandIns(hooks);
  await page.addInitScript(overlay);
  fs.mkdirSync(OUT, { recursive: true });
  T0.at = Date.now();

  // the on-screen terminal: history is kept here so a visit to the dashboard in between does not lose it
  const history: { cmd?: string; out?: string }[] = [];
  let barTags: { cat: string; text: string }[] = [];
  const term = {
    async show() {
      if (!page.url().startsWith(SHOP + '/terminal.html')) {
        await page.goto(SHOP + '/terminal.html');
        await page.evaluate((h) => (window as any).__term.restore(h), history);
        await page.evaluate((t) => (window as any).__term.tags(t), barTags);
      }
    },
    /** Types `shown` (fast for long commands: at most `typeSec`), then prints `out` once `getOut` is ready. */
    async run(shown: string, getOut: () => Promise<string>, typeSec = 2.4) {
      await term.show();
      await page.evaluate(([c, ms]) => (window as any).__term.type(c, ms), [shown, Math.round(Math.min(typeSec * 1000, shown.length * 38) * PACE)] as const);
      const out = await getOut();
      await page.evaluate((o) => (window as any).__term.out(o), out);
      history.push({ cmd: shown, out });
    },
    /** A fresh screen with these category tags in the top bar (cat: platform | app | scaf | cap). */
    async fresh(tags: { cat: string; text: string }[]) {
      history.length = 0;
      barTags = tags;
      if (page.url().startsWith(SHOP + '/terminal.html')) {
        await page.evaluate(([t]) => { (window as any).__term.restore([]); (window as any).__term.tags(t); }, [tags] as const);
      } else await term.show();
    },
    mark: (patterns: string[]) => page.evaluate((p) => (window as any).__term.mark(p), patterns),
    unmark: () => page.evaluate(() => (window as any).__term.unmark()),
  };
  /** A dark card whose items appear one by one (`show(i)`), driven by the narration. */
  const card = async (title: string, sub: string, items: { text: string; cls?: string }[]) => {
    await page.goto(SHOP + '/card.html');
    await page.evaluate(([t, s, i]) => (window as any).__card(t, s, i), [title, sub, items] as const);
  };
  const show = (n: number) => page.evaluate((k) => (window as any).__show(k), n);
  const hide = (n: number) => page.evaluate((k) => (window as any).__hide(k), n);
  const hot = (...n: number[]) => page.evaluate((k) => (window as any).__hot(...k), n);

  let sk = '';
  let pk = '';
  const ids: Record<string, string> = {};
  const cardAt = rel();
  const customer = (id: string, email: string, ip: string) => ({ id, email, ip_address: ip, account_age_hours: 4000 });
  const openPayment = async (id: string, searchDelay = 45) => {
    await page.goto(PB + '/#payments');
    await type(page, page.locator('#pay-q'), id, searchDelay);
    const row = page.locator('#pay-body tr', { hasText: id });
    await expect(row).toBeVisible();
    await glide(page, row);
    await row.click();
    await expect(page.locator('#detail')).toContainText(/Fraud engine: \d+ \/ 1000/);
  };
  const detail = page.locator('#detail');

  try {
    // ---- scene 1: the problem, then what is platform, sample app and scaffolding (swappable) ----------------------------------
    begin(1);
    await card('Prove nobody altered the record', 'I am running payments', [
      { text: 'A decision must never change after the fact', cls: 'q' }, { text: 'Not by an engineer', cls: 'bad' }, { text: 'Not by a database admin', cls: 'bad' }, { text: 'Not by me', cls: 'bad' },
    ]);
    await at(page, 'And I have to prove', 0.2);
    await show(0);
    await at(page, 'Not my engineers', 0.2);
    await show(1);
    await at(page, 'Not my database admin', 0.2);
    await show(2);
    await at(page, 'Not me.', 0.2);
    await show(3);
    await at(page, 'Here is how EDB Postgres', 0.3);
    await page.goto(SHOP + '/diagram.html');
    await show(6);
    await show(0);
    await at(page, 'This platform does the work', 0.2);
    await hot(0);
    await at(page, 'PayBroker is only a sample app', 0.3);
    await show(1);
    await hot(1);
    await at(page, 'Sign-in, the LLM gateway', 0.3);
    await show(2);
    await pause(page, 600);
    await show(3);
    await pause(page, 600);
    await show(4);
    await hot(2, 3, 4);
    await at(page, 'They are swappable', 0.2);
    await at(page, 'It uses test cards only', 0.2);
    await hot();
    await show(5);
    await endScene(page);

    // ---- scene 2: setup, demo traffic (each decision goes into the ledger), a merchant -----------------------------------------
    begin(2);
    await at(page, 'First, a quick setup', 0.1);
    await page.goto(PB + '/');
    await expect(page.getByRole('heading', { name: 'Set up PayBroker' })).toBeVisible();
    await callout(page, 'One-time setup code from the server log', 2.4);
    await type(page, page.locator('#setup-code'), stack.setupCode, 12);
    await type(page, page.locator('#setup-pw'), adminPassword, 6);
    await type(page, page.locator('#setup-pw2'), adminPassword, 6);
    await click(page, page.getByRole('button', { name: 'Create admin password' }), 300);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
    const demoBtn = page.getByRole('button', { name: 'Generate 120 demo payments' });
    await expect(demoBtn).toBeVisible();
    await at(page, 'One click sends', 0.3);
    const offBtn = await hl(demoBtn);
    await glide(page, demoBtn);
    await offBtn(); // the click happens as the sentence that names it plays: 120 payments take ~8 s, and "Done." must not come before they finish
    await demoBtn.click();
    await callout(page, 'Every decision is also written to the audit ledger', 3.2);
    const waitStart = Date.now();
    await expect(page.locator('#toast')).toContainText('Sent 120 demo payments', { timeout: 90_000 });
    await expect(page.getByRole('heading', { name: 'Fraud score distribution' })).toBeVisible({ timeout: 30_000 });
    console.log(`demo traffic took ${((Date.now() - waitStart) / 1000).toFixed(1)} s`);
    await at(page, 'Done.', 0.2);
    await page.goto(PB + '/#merchants');
    await at(page, 'I create Acme Shop', 0.3);
    await click(page, page.getByRole('button', { name: 'New merchant' }), 100);
    await type(page, page.getByLabel('Name'), 'Acme Shop', 15);
    await click(page, page.getByRole('button', { name: 'Create', exact: true }), 200);
    await expect(detail.getByRole('heading', { name: 'Acme Shop' })).toBeVisible();
    await detail.locator('#key-kind').selectOption('secret');
    await type(page, detail.locator('#key-name'), 'checkout server', 10);
    await click(page, detail.getByRole('button', { name: 'Create key' }), 100);
    let code = detail.locator('.codewrap code').last();
    await expect(code).toContainText('sk_test_');
    sk = (await code.innerText()).trim();
    await mask(detail.locator('.codewrap code'));
    await at(page, 'Then a publishable key', 0.3);
    await detail.locator('#key-kind').selectOption('publishable');
    await type(page, detail.locator('#key-name'), 'checkout page', 10);
    await click(page, detail.getByRole('button', { name: 'Create key' }), 100);
    code = detail.locator('.codewrap code').last();
    await expect(code).toContainText('pk_test_');
    pk = (await code.innerText()).trim();
    await at(page, 'Each key is shown once', 0.2);
    await callout(page, 'Shown once, then hidden', 2.6);
    await pause(page, 500);
    await mask(detail.locator('.codewrap code'));
    await endScene(page);

    // ---- scene 3: Acme Shop's payments from the terminal: authorize, capture, refund, and a name mismatch ----------------------
    const cardOk = await tokenize(pk, '4242424242424242');
    const body1001 = {
      idempotency_key: 'order-1001', amount: 4999, currency: 'USD', capture_method: 'manual', payment_method: { type: 'card', card_token: cardOk },
      customer: customer('cus_42', 'pat@example.com', '198.51.100.20'),
      telemetry: { device_fingerprint: 'dev_abc', checkout_duration_ms: 41000, pasted_card_number: false }, shipping_address: { country: 'US' },
    };
    const tokName = await tokenize(pk, '4242424242424242');
    const bodyName = {
      idempotency_key: 'order-name-1', amount: 34900, currency: 'USD', payment_method: { type: 'card', card_token: tokName, cardholder_name: 'Jon Smith' },
      customer: { ...customer('cus_77', 'peter@example.com', '198.51.100.30'), name: 'Peter Wong' },
    };
    begin(3);
    await at(page, 'Now Acme Shop takes a payment', 0.3);
    await term.fresh([{ cat: 'app', text: 'SAMPLE APP: the shop calls PayBroker' }]);
    await term.run('TOKEN=$(tok 4242424242424242); echo $TOKEN', async () => cardOk, 1.2);
    await term.mark(['tok_vault_card_\\w+']);
    await callout(page, 'A token, not the card number', 3.4);
    await at(page, 'The server authorizes', 0.2);
    let first!: { status: number; json: any };
    await term.run(shownAuth(body1001, '$TOKEN').replace(/\| jq$/, "| jq '{status, payment_id, score: .fraud_assessment.score}'"), async () => {
      first = await authorize(sk, body1001);
      expect(first.status).toBe(200);
      expect(first.json.status).toBe('AUTHORIZED');
      ids['1001'] = first.json.payment_id;
      return JSON.stringify({ status: first.json.status, payment_id: first.json.payment_id, score: first.json.fraud_assessment.score }, null, 2);
    }, 1.8);
    await term.mark(['"status": "AUTHORIZED"', '"score": \\d+']);
    await callout(page, 'Authorized, with a fraud score out of 1000', 3.4);
    await at(page, 'Then it captures', 0.2);
    await term.unmark();
    await term.run(`capture ${ids['1001']} | jq '{status, amount_captured}'`, async () => {
      const r = await action(sk, ids['1001'], 'capture');
      expect(r.status).toBe(200);
      expect(r.json.amount_captured).toBe(4999);
      return JSON.stringify({ status: r.json.status, amount_captured: r.json.amount_captured }, null, 2);
    }, 1.2);
    await term.mark(['"amount_captured": \\d+']);
    await callout(page, 'Captured: 49.99', 2.6);
    await at(page, 'And refunds ten dollars', 0.2);
    await term.run(`refund ${ids['1001']} '{"amount":1000}' | jq '{status, amount_refunded}'`, async () => {
      const r = await action(sk, ids['1001'], 'refund', { amount: 1000 });
      expect(r.status).toBe(200);
      expect(r.json.amount_refunded).toBe(1000);
      return JSON.stringify({ status: r.json.status, amount_refunded: r.json.amount_refunded }, null, 2);
    }, 1.2);
    await term.mark(['"amount_refunded": \\d+']);
    await callout(page, 'Refunded: 10.00', 2.6);
    await at(page, 'The card says Jon Smith', 0.2);
    await term.fresh([{ cat: 'platform', text: 'PLATFORM: fuzzystrmatch extension' }, { cat: 'app', text: 'SAMPLE APP: the shop calls PayBroker' }]);
    await callout(page, 'Card says Jon Smith, account says Peter Wong', 4.5);
    const nameFilter = '{status, reasons: .fraud_assessment.reasons}';
    let nm!: { status: number; json: any };
    await term.run(shownAuth(bodyName, '$(tok 4242424242424242)').replace(/\| jq$/, `| jq '${nameFilter}'`), async () => {
      nm = await authorize(sk, bodyName);
      expect(nm.status, JSON.stringify(nm.json)).toBe(200);
      const reasons: string[] = nm.json.fraud_assessment?.reasons ?? [];
      expect(reasons).toContain('NAME_MISMATCH');
      ids.name = nm.json.payment_id;
      return JSON.stringify({ status: nm.json.status, reasons }, null, 2);
    }, 2.2);
    await at(page, 'Postgres compares the names', 0.2);
    await term.mark(['"NAME_MISMATCH"', '"status": "[A-Z_]+"']);
    await callout(page, 'NAME_MISMATCH: reason code from fuzzystrmatch', 4.5);
    await endScene(page);

    // ---- scene 4: the audit ledger: Audit tab, Verify, and the same check in SQL -------------------------------------------------
    begin(4);
    await at(page, 'Every one of those wrote', 0.3);
    await page.goto(PB + '/#audit');
    await expect(page.getByRole('heading', { name: 'Audit ledger' })).toBeVisible();
    await expect(page.locator('#audit-body tr').first()).toBeVisible();
    const offList = await hl(page.locator('#audit-body'));
    await at(page, 'Also rule changes', 0.2);
    await callout(page, 'decision, capture, refund, rule change, key creation', 3.4);
    await at(page, 'Each entry is written in the same', 0.2);
    await offList();
    await callout(page, 'Same database transaction as the change itself', 4.2);
    await spot(page, page.locator('#audit-body tr', { hasText: 'payment.refunded' }).first(), 2200);
    await at(page, 'And each entry carries', 0.2);
    await term.fresh([{ cat: 'platform', text: 'PLATFORM: plain Postgres: sha256(), a trigger, a function' }, { cat: 'app', text: 'SAMPLE APP: the ledger  |  sql = psql "$DB" as the table owner' }]);
    const chainSql = `SELECT seq, action, left(prev_hash, 10) AS prev_hash, left(hash, 10) AS hash FROM audit_ledger ORDER BY seq DESC LIMIT 4`;
    await term.run(`sql -c "${chainSql}"`, async () => ownerSql(chainSql), 2.4);
    await term.mark(['\\b[0-9a-f]{10}\\b']);
    await callout(page, 'Each prev_hash is the hash of the entry above', 4);
    await at(page, 'Now I press Verify', 0.2);
    await term.unmark();
    await page.goto(PB + '/#audit');
    await expect(page.locator('#audit-body tr').first()).toBeVisible();
    await click(page, page.locator('#audit-verify'), 100);
    await expect(page.locator('#audit-status')).toContainText('Chain intact', { timeout: 30_000 });
    await spot(page, page.locator('#audit-status'), 1800);
    await at(page, 'The database recomputes every hash', 0.1);
    await callout(page, 'Chain intact: every hash recomputed in the database', 4.5);
    for (let i = 0; i < 5; i++) { await page.mouse.wheel(0, 380); await pause(page, 700); }
    await at(page, 'Here is the same check in plain SQL', 0.2);
    await term.show();
    const verSql = `SELECT * FROM audit_verify_detail()`;
    await term.run(`sql -x -c "${verSql}"`, async () => {
      const out = ownerSql(verSql, ['-x']);
      expect(out).toMatch(/first_broken\s*\|\s*\n/);
      return out;
    }, 1.6);
    await term.mark(['first_broken[^\\n]*', 'entries[^\\n]*']);
    await at(page, 'A function, a trigger', 0.2);
    await callout(page, 'No external ledger service', 4);
    const trgSql = `SELECT tgname AS trigger, tgenabled AS enabled FROM pg_trigger WHERE tgrelid = 'audit_ledger'::regclass AND NOT tgisinternal ORDER BY 1`;
    await term.run(`sql -c "${trgSql}"`, async () => {
      const out = ownerSql(trgSql);
      expect(out).toContain('audit_ledger_no_change');
      return out;
    }, 2.2);
    await term.mark(['audit_ledger_no_change']);
    await endScene(page);

    // ---- scene 5: tamper, as the table owner -----------------------------------------------------------------------------------
    begin(5);
    const capSeq = Number(ownerSql(`SELECT seq FROM audit_ledger WHERE action = 'payment.captured' ORDER BY seq LIMIT 1`, ['-At']));
    expect(capSeq).toBeGreaterThan(100);
    const showRow = `SELECT seq, action, payload->'amount_captured' AS captured FROM audit_ledger WHERE seq = ${capSeq}`;
    const upd = `UPDATE audit_ledger SET payload['amount_captured'] = '1' WHERE seq = ${capSeq}`;
    const del = `DELETE FROM audit_ledger WHERE seq = ${capSeq}`;
    await at(page, 'Now I try to cheat', 0.2);
    await term.fresh([{ cat: 'platform', text: 'PLATFORM: Postgres as the table owner (local test database)  |  sql = psql "$DB"' }, { cat: 'app', text: 'SAMPLE APP: the audit ledger' }]);
    await at(page, 'This entry is the capture', 0.1);
    await term.run(`sql -c "${showRow}"`, async () => ownerSql(showRow), 1.6);
    await term.mark([`\\b${capSeq}\\b`, 'payment.captured']);
    await at(page, 'First, as the app', 0.2);
    await term.unmark();
    await callout(page, 'Round 1: the app\'s own database role', 3.4);
    await term.run(`sql -c "${upd}"`, async () => {
      const out = ownerSql(upd);
      expect(out).toContain('permission denied');
      return out;
    }, 2.0);
    await at(page, 'Permission denied.', 0.1);
    await term.mark(['ERROR[^\\n]*']);
    await at(page, 'And deleting it', 0.1);
    await term.unmark();
    await term.run(`sql -c "${del}"`, async () => {
      const out = ownerSql(del);
      expect(out).toContain('permission denied');
      return out;
    }, 1.2);
    await term.mark(['ERROR[^\\n]*']);
    await at(page, 'The owner can grant itself', 0.1);
    await term.unmark();
    await callout(page, 'Round 2: the table owner', 3.4);
    const grant = `GRANT UPDATE ON audit_ledger TO CURRENT_USER`;
    await term.run(`sql -c "${grant}"`, async () => ownerSql(grant), 1.0);
    await term.run(`sql -c "${upd}"`, async () => {
      const out = ownerSql(upd);
      expect(out).toContain('append-only');
      return out;
    }, 1.4);
    await at(page, 'The trigger still refuses', 0.1);
    await term.mark(['ERROR[^\\n]*']);
    await at(page, 'But the owner can switch', 0.1);
    await term.unmark();
    await callout(page, 'The owner switches the trigger off, edits, switches it on', 5);
    const off = `ALTER TABLE audit_ledger DISABLE TRIGGER audit_ledger_no_change`;
    const on = `ALTER TABLE audit_ledger ENABLE TRIGGER audit_ledger_no_change`;
    await term.run(`sql -c "${off}"`, async () => ownerSql(off), 0.9);
    await term.run(`sql -c "${upd}"`, async () => {
      const out = ownerSql(upd);
      expect(out).toContain('UPDATE 1');
      return out;
    }, 1.2);
    await term.mark(['UPDATE 1']);
    await term.run(`sql -c "${on}"`, async () => ownerSql(on), 0.9);
    await at(page, 'The capture now says one cent', 0.1);
    await term.unmark();
    await term.run(`sql -c "${showRow}"`, async () => {
      const out = ownerSql(showRow);
      expect(out).toMatch(/\|\s+1\s*\n/);
      return out;
    }, 1.2);
    await term.mark(['\\s1\\s*(?=\\n|$)']);
    await endScene(page);

    // ---- scene 6: Verify names the first broken entry ------------------------------------------------------------------------------
    begin(6);
    await at(page, 'Now, back in the dashboard', 0.2);
    await page.goto(PB + '/#audit');
    await expect(page.locator('#audit-body tr').first()).toBeVisible();
    await at(page, 'I verify the chain again', 0.3);
    await click(page, page.locator('#audit-verify'), 100);
    await expect(page.locator('#audit-status')).toContainText('Chain broken', { timeout: 30_000 });
    await expect(page.locator('#audit-status')).toContainText(`First broken entry: #${capSeq}`);
    await expect(page.locator(`#audit-body tr[data-seq="${capSeq}"]`)).toHaveClass(/broken/);
    await at(page, 'Chain broken.', 0.1);
    await callout(page, `Chain broken. First broken entry: #${capSeq}`, 5);
    await spot(page, page.locator('#audit-status'), 2200);
    await at(page, 'Everything before it', 0.2);
    await spot(page, page.locator(`#audit-body tr[data-seq="${capSeq}"]`), 2400);
    await at(page, 'The same answer comes', 0.2);
    await term.fresh([{ cat: 'platform', text: 'PLATFORM: audit_verify_detail(), a SQL function' }, { cat: 'app', text: 'SAMPLE APP: the audit ledger' }]);
    const verSql2 = `SELECT * FROM audit_verify_detail()`;
    await term.run(`sql -x -c "${verSql2}"`, async () => {
      const out = ownerSql(verSql2, ['-x']);
      expect(out).toMatch(new RegExp(`first_broken\\s*\\|\\s*${capSeq}\\b`));
      return out;
    }, 1.6);
    await term.mark([`first_broken[^\\n]*`, 'reason[^\\n]*']);
    await at(page, 'Look at that.', 0.1);
    await callout(page, 'No blockchain. No ledger service. Just Postgres.', 6);
    await endScene(page);

    // ---- scene 7: row-level security: the same query as two merchants, nothing set, and a plain-grant AI role -----------------------------------
    begin(7);
    const acme = ownerSqlBypass(`SELECT id FROM merchant WHERE name = 'Acme Shop'`, ['-At']);
    const other = ownerSqlBypass(`SELECT id FROM merchant WHERE name = 'Demo Shop'`, ['-At']);
    expect(acme).toMatch(/^mch_/);
    expect(other).toMatch(/^mch_/);
    psql('postgres', ['-c', `CREATE ROLE ai_agent LOGIN NOSUPERUSER; GRANT USAGE ON SCHEMA public TO ai_agent`]);
    const rlsQ = `SELECT merchant_id, count(*) AS payments, min(payment_id) AS first_payment FROM transaction GROUP BY 1`;
    const asMerchant = (m: string) => psql('pb_owner', ['-c', `SET app.merchant_id = '${m}'`, '-c', rlsQ, '-v', 'VERBOSITY=terse']);
    await at(page, 'Next problem', 0.2);
    await term.fresh([{ cat: 'platform', text: 'PLATFORM: row-level security, FORCEd, in Postgres' }, { cat: 'app', text: 'SAMPLE APP: sets app.merchant_id per request' }]);
    await callout(page, "Merchant A must never see merchant B's rows", 4.5);
    await at(page, 'Row-level security does that', 0.2);
    const polSql = `SELECT tablename, policyname, cmd FROM pg_policies WHERE tablename IN ('transaction', 'fraud_assessment', 'fraud_case') ORDER BY 1, 2`;
    await term.run(`sql -c "${polSql}"`, async () => {
      const out = ownerSql(polSql);
      expect(out).toContain('pb_scope');
      return out;
    }, 2.4);
    await at(page, 'A merchant sees only its own rows', 0.1);
    await term.mark(['pb_scope']);
    await callout(page, 'Rule 1: a merchant sees only its own rows', 3);
    await at(page, 'Child rows follow', 0.1);
    await term.unmark();
    await term.mark(['fraud_assessment']);
    await callout(page, 'Rule 2: child rows follow their payment', 3);
    await at(page, 'Cases and chargebacks', 0.1);
    await term.unmark();
    await term.mark(['fraud_case[^\\n]*']);
    await callout(page, 'Rule 3: cases and chargebacks belong to the operator', 3.4);
    await at(page, 'And read-only auditors', 0.1);
    await term.unmark();
    await term.mark(['pb_readonly']);
    await callout(page, 'Rule 4: read-only auditors may look, never change', 3.6);
    await at(page, 'First, as Acme Shop', 0.2);
    await term.run(`sql -c "SET app.merchant_id = '$ACME'" -c "${rlsQ}"`, async () => {
      const out = asMerchant(acme);
      expect(out).toContain('(1 row)');
      expect(out).toContain(acme);
      return out;
    }, 2.2);
    await term.mark([acme]);
    await at(page, 'Now the same query', 0.2);
    await term.unmark();
    await term.run(`sql -c "SET app.merchant_id = '$OTHER'" -c "${rlsQ}"`, async () => {
      const out = asMerchant(other);
      expect(out).toContain('(1 row)');
      expect(out).toContain(other);
      expect(out).not.toContain(acme);
      return out;
    }, 2.2);
    await term.mark([other]);
    await at(page, 'Different rows', 0.2);
    await callout(page, 'Same query, same table, different rows', 3.4);
    await term.unmark();
    await term.run(`sql -c "${rlsQ}"`, async () => {
      const out = ownerSql(rlsQ);
      expect(out).toContain('(0 rows)');
      return out;
    }, 1.6);
    await term.mark(['\\(0 rows\\)']);
    await at(page, 'It fails closed', 0.1);
    await callout(page, 'Nothing set = zero rows, even for the table owner', 4);
    await at(page, 'Last, an AI agent', 0.2);
    await term.unmark();
    const grantAi = `GRANT SELECT ON transaction TO ai_agent`;
    await term.run(`sql -c "${grantAi}"`, async () => ownerSql(grantAi), 1.4);
    await atEnd(page, 'I give its role', -0.1);
    const cnt = `SELECT count(*) FROM transaction`;
    await term.run(`psql "$AI" -c "${cnt}"`, async () => {
      const out = psql('ai_agent', ['-c', cnt]);
      expect(out).toMatch(/\n\s*0\n/);
      return out;
    }, 1.4);
    await term.mark(['\\b0\\b(?=\\n)']);
    await callout(page, 'SELECT granted, zero rows: the policy is forced', 4);
    await at(page, 'A grant alone opens nothing', 0.1);
    await callout(page, 'A grant alone opens nothing', 3.5);
    await endScene(page);

    // ---- scene 8: masked views for the read-only role, and the 5-minute re-assert ------------------------------------------------------
    begin(8);
    const maskQ = (t: string) => `SELECT payment_id, customer_id, request->>'ip' AS ip FROM ${t} ORDER BY created_at DESC LIMIT 3`;
    await at(page, 'Next problem', 0.2);
    await term.fresh([{ cat: 'platform', text: 'PLATFORM: a1p_readonly role (agentpg db-users --permission read)' }, { cat: 'app', text: 'SAMPLE APP: masked views' }]);
    await callout(page, 'Analyst or AI agent: not the customer details', 4);
    await at(page, "The app's own connection", 0.2);
    await term.run(`PGOPTIONS='-c app.bypass=on' psql "$DB" -c "${maskQ('transaction')}"`, async () => {
      const out = ownerSqlBypass(maskQ('transaction'));
      expect(out).toContain('cus_');
      return out;
    }, 2.2);
    await term.mark(['cus_\\w+', '\\d+\\.\\d+\\.\\d+\\.\\d+']);
    await at(page, 'The read-only role is denied', 0.2);
    await term.unmark();
    await term.run(`psql "$RO" -c "${maskQ('transaction')}"`, async () => {
      const out = reader(maskQ('transaction'));
      expect(out).toContain('permission denied');
      return out;
    }, 1.8);
    await term.mark(['permission denied']);
    await at(page, 'But the masked view', 0.2);
    await term.unmark();
    await term.run(`psql "$RO" -c "${maskQ('transaction_masked')}"`, async () => {
      const out = reader(maskQ('transaction_masked'));
      expect(out).toContain('xxx');
      return out;
    }, 2.2);
    await term.mark(['[0-9a-f]{12}', '\\d+\\.\\d+\\.\\d+\\.xxx']);
    await at(page, 'Now, one real finding', 0.2);
    await term.unmark();
    await callout(page, 'New database user = grants re-open. PayBroker re-asserts every 5 minutes', 6.5);
    await at(page, 'Its status is public', 0.2);
    await term.run(`curl -s $PB/readyz | jq .masking`, async () => {
      const r: any = await (await fetch(PB + '/readyz')).json();
      expect(r.masking).toBeTruthy();
      return JSON.stringify(r.masking, null, 2);
    }, 1.4);
    await term.mark(['"drift": [^\\n]*', '"ok": [^\\n]*']);
    await endScene(page);

    // ---- scene 9: EDB tech from the live fleet: real screenshots of the Insights tab, captured 28 Oct 2026 (not run in this video) ----------------
    begin(9);
    const W = 1481, H = 812; // the screenshots' size; boxes below are read off them in pixels and converted to percent of the image
    const pct = (x0: number, y0: number, x1: number, y1: number) => [(x0 / W) * 100, (y0 / H) * 100, ((x1 - x0) / W) * 100, ((y1 - y0) / H) * 100];
    const shot = (img: string, ox: number, oy: number, z0: number, z1: number, ms: number, box?: number[]) =>
      page.evaluate(([i, a, b, c, d, e, f]) => (window as any).__shot(i, a, b, c, d, e, f), [img, ox, oy, z0, z1, ms, box ?? null] as const);
    const TOP = 'paybroker-insights-top-queries-2026-10-28.jpg', MASK = 'paybroker-insights-masking-sql-in-monitor-2026-10-28.jpg', TUNE = 'paybroker-insights-tuner-advisor-2026-10-28.jpg';
    await at(page, 'The platform adds one more thing', 0.2);
    await page.goto(PB + '/#insights');
    await expect(page.getByRole('heading', { name: 'Database insights' })).toBeVisible();
    await callout(page, 'This laptop: pg_stat_statements only', 5);
    await page.waitForTimeout(1500);
    for (let i = 0; i < 4; i++) { await page.mouse.wheel(0, 250); await pause(page, 700); }
    await at(page, 'On AgentPG, the same page reads', 0.2);
    await callout(page, 'On AgentPG: + edb_stat_monitor, edb_pg_tuner, query_advisor', 5.5);
    await at(page, 'These are real screenshots', 0.2);
    await page.goto(SHOP + '/still.html');
    await shot(TOP, 50, 20, 1.0, 1.08, 8000);
    await at(page, 'First, edb_stat_monitor', 0.2);
    await shot(TOP, 40, 38, 1.0, 1.35, 7500, pct(255, 264, 870, 306));
    await at(page, 'Further down', 0.3);
    await shot(MASK, 35, 12, 1.0, 1.7, 8000, pct(270, 80, 700, 106));
    await at(page, 'And it creates a masked view', 0.2);
    await shot(MASK, 35, 26, 1.0, 1.7, 5500, pct(270, 160, 700, 262));
    await at(page, 'Then edb_pg_tuner', 0.2);
    await shot(TUNE, 40, 45, 1.0, 1.5, 6000, pct(264, 284, 1204, 425));
    await at(page, 'And query_advisor suggests', 0.2);
    await shot(TUNE, 45, 80, 1.0, 1.45, 8500, pct(264, 570, 1204, 712));
    await endScene(page);

    // ---- scene 10: close, and the legend again ------------------------------------------------------------------------------------------
    begin(10);
    await page.goto(SHOP + '/diagram.html');
    for (const k of [6, 0, 1, 2, 3, 4]) await show(k);
    await at(page, 'The platform, EDB Postgres', 0.1);
    await hot(0);
    await at(page, 'The scaffolding is swappable', 0.1);
    await hot(2, 3, 4);
    await at(page, 'PayBroker is the sample', 0.1);
    await hot(1);
    await at(page, 'Try it yourself', 0.1);
    await hot();
    await at(page, 'One command on AgentPG', 0.1);
    await show(7);
    await at(page, 'Or Docker on your laptop', 0.1);
    await show(8);
    await at(page, 'The guide, section eighteen', 0.1);
    await show(10);
    await endScene(page);
  } finally {
    standIns.close();
    if (process.env.TIMES_JSON) {
      fs.mkdirSync(path.dirname(process.env.TIMES_JSON), { recursive: true });
      fs.writeFileSync(process.env.TIMES_JSON, JSON.stringify({ cardAt, scenes: times, sync }, null, 2));
    }
    await page.close();
  }
});
