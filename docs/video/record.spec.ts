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
  h2 { font-size: 40px; margin: 0 0 8px; } p { font-size: 30px; margin: 6px 0; color: #cbd5e1; } .chip { font-size: 32px; margin: 12px 0; color: #e2e8f0; }
  .arrow { position: absolute; height: 6px; background: #64748b; opacity: 0; transition: opacity .5s; } .arrow.show { opacity: 1; }
  #legend { position: absolute; left: 60px; right: 60px; top: 730px; display: flex; gap: 48px; align-items: center; opacity: 0; transition: opacity .5s; font-size: 28px; }
  #legend.show { opacity: 1; } .sw { display: flex; align-items: center; gap: 12px; } .sw i { width: 34px; height: 34px; border-radius: 8px; display: inline-block; }
  #note { position: absolute; left: 0; right: 0; top: 820px; text-align: center; font-size: 40px; color: #94a3b8; opacity: 0; transition: opacity .5s; } #note.show { opacity: 1; }
  .cmd { position: absolute; left: 60px; right: 60px; font: 38px ui-monospace, Menlo, monospace; color: #e2e8f0; opacity: 0; transition: opacity .5s; } .cmd.show { opacity: 1; } .cmd em { color: #94a3b8; font-style: normal; font-size: 28px; }
</style><div id="d">
  <div class="box platform" data-k="0" style="left:60px;top:90px;width:800px;height:600px"><span class="tag">PLATFORM</span><h2>Postgres on AgentPG (EDB)</h2>
    <div class="chip">pgvector: nearest past fraud cases</div><div class="chip">fuzzystrmatch: name-mismatch check</div><div class="chip">read-only role + masked views</div><div class="chip">EDB insights: edb_stat_monitor, query_advisor</div></div>
  <div class="arrow" data-a="1" style="left:860px;top:386px;width:60px"></div>
  <div class="box app" data-k="1" style="left:920px;top:90px;width:430px;height:600px"><span class="tag">SAMPLE APP</span><h2>PayBroker</h2><p>fraud checks, routing, dashboard, API</p><p>test cards only</p></div>
  <div class="arrow" data-a="2" style="left:1350px;top:386px;width:60px"></div>
  <div class="box scaf" data-k="2" style="left:1410px;top:90px;width:450px;height:190px"><span class="tag">REFERENCE SCAFFOLDING</span><h2>Sign-in</h2><p>any OpenID Connect provider</p></div>
  <div class="box scaf" data-k="3" style="left:1410px;top:300px;width:450px;height:190px"><span class="tag">REFERENCE SCAFFOLDING</span><h2>Model gateway</h2><p>OpenAI-compatible API</p></div>
  <div class="box scaf" data-k="4" style="left:1410px;top:510px;width:450px;height:180px"><span class="tag">REFERENCE SCAFFOLDING</span><h2>Secret vault</h2><p>shared-kit module</p></div>
  <div id="legend" data-k="6"><div class="sw platform"><i></i>PLATFORM: AgentPG and EDB Postgres</div><div class="sw scaf"><i></i>REFERENCE SCAFFOLDING: swappable</div><div class="sw app"><i></i>SAMPLE APP: PayBroker</div></div>
  <div id="note" data-k="5">Only test cards and simulated processors: nothing moves real money</div>
  <div class="cmd" data-k="7" style="top:810px">$ ecoctl apply --only paybroker --execute <em>   # on AgentPG</em></div>
  <div class="cmd" data-k="8" style="top:870px">$ docker compose up -d db <em>   # or locally, with Docker</em></div>
  <div class="cmd" data-k="9" style="top:930px">$ paybroker migrate &amp;&amp; paybroker serve</div>
  <div class="cmd" data-k="10" style="top:995px;color:#4ade80">docs/GUIDE.md  <em>section 17: EDB and AgentPG capabilities</em></div>
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
function psql(user: 'postgres' | 'reporting_reader', args: string[], input?: string): string {
  const c = stack.cluster;
  const r = spawnSync(path.join(c.bin, 'psql'), ['-X', '-h', '127.0.0.1', '-p', String(c.port), '-U', user, '-d', c.dbName, '-P', 'pager=off', ...args], { input, encoding: 'utf8', env: { PATH: `${c.bin}:/usr/bin:/bin`, LC_ALL: 'C' } });
  return `${r.stdout ?? ''}${r.stderr ?? ''}`.replace(/\s+$/, '');
}
/** The same text the terminal shows is the text that runs: `$DB` / `$RO` stand for the connection strings (they hold no password). */
const owner = (sql: string) => psql('postgres', ['-c', sql]);
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
    // ---- scene 1: the problem, then what is platform, sample app and scaffolding ---------------------------------------------
    begin(1);
    await card('A marketplace taking cards', 'I run a marketplace', [
      { text: 'Card fraud costs me money', cls: 'bad' }, { text: 'False declines cost me sales', cls: 'bad' }, { text: 'My payment data goes to three vendors', cls: 'bad' },
    ]);
    await at(page, 'Card fraud and false declines', 0.2);
    await show(0);
    await pause(page, 1300);
    await show(1);
    await at(page, 'And I will not ship', 0.2);
    await show(2);
    await at(page, 'So here is how Postgres on AgentPG', 0.2);
    await page.goto(SHOP + '/diagram.html');
    await show(6);
    await show(0);
    await at(page, 'PayBroker is the sample app', 0.3);
    await show(1);
    await at(page, 'Three things here are scaffolding', 0.3);
    await show(2);
    await pause(page, 700);
    await show(3);
    await pause(page, 700);
    await show(4);
    await at(page, 'Sign-in speaks standard', 0.2);
    await hot(2);
    await at(page, 'The gateway speaks', 0.2);
    await hot(3);
    await at(page, 'The vault is a small module', 0.2);
    await hot(4);
    await at(page, 'It uses only test cards', 0.2);
    await hot();
    await show(5);
    await endScene(page);

    // ---- scene 2: setup and demo traffic -------------------------------------------------------------------------------------
    begin(2);
    await at(page, 'First, setup', 0.1);
    await page.goto(PB + '/');
    await expect(page.getByRole('heading', { name: 'Set up PayBroker' })).toBeVisible();
    await callout(page, 'One-time setup code from the server log', 3.0);
    await type(page, page.locator('#setup-code'), stack.setupCode, 25);
    await type(page, page.locator('#setup-pw'), adminPassword, 12);
    await type(page, page.locator('#setup-pw2'), adminPassword, 12);
    await click(page, page.getByRole('button', { name: 'Create admin password' }), 300);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
    const demoBtn = page.getByRole('button', { name: 'Generate 120 demo payments' });
    await expect(demoBtn).toBeVisible();
    await at(page, 'Now the dashboard', 0.1);
    await callout(page, 'Empty overview: no payments yet', 2.6);
    await at(page, 'One click loads', 0.2);
    const offBtn = await hl(demoBtn);
    await callout(page, '120 fictional payments: honest ones and fraud attempts', 4.2);
    await glide(page, demoBtn);
    await at(page, 'I click, and each payment', 0.2);
    await offBtn();
    await demoBtn.click();
    const waitStart = Date.now();
    await expect(page.locator('#toast')).toContainText('Sent 120 demo payments', { timeout: 90_000 });
    await expect(page.getByRole('heading', { name: 'Fraud score distribution' })).toBeVisible({ timeout: 30_000 });
    console.log(`demo traffic took ${((Date.now() - waitStart) / 1000).toFixed(1)} s`);
    await at(page, 'Done.', 0.2);
    await spot(page, page.locator('a.stat', { hasText: 'approved' }).first(), 1800);
    await callout(page, 'under 300 approve | 300-699 verify | 700+ decline', 8);
    await at(page, 'Under three hundred', 0.2);
    await spot(page, page.locator('.card', { has: page.getByRole('heading', { name: 'Fraud score distribution' }) }), 6000);
    await endScene(page);

    // ---- scene 3: merchant, keys, origin, webhook -----------------------------------------------------------------
    begin(3);
    await at(page, 'Next, a shop', 0.2);
    await page.goto(PB + '/#merchants');
    await at(page, 'I create a merchant called', 0.2);
    await click(page, page.getByRole('button', { name: 'New merchant' }), 200);
    await type(page, page.getByLabel('Name'), 'Acme Shop', 25);
    await click(page, page.getByRole('button', { name: 'Create', exact: true }), 200);
    await expect(detail.getByRole('heading', { name: 'Acme Shop' })).toBeVisible();
    await at(page, 'The secret key stays', 0.3);
    await callout(page, 'Secret key: server only', 3.0);
    await detail.locator('#key-kind').selectOption('secret');
    await type(page, detail.locator('#key-name'), 'checkout server', 20);
    await click(page, detail.getByRole('button', { name: 'Create key' }), 100);
    let code = detail.locator('.codewrap code').last();
    await expect(code).toContainText('sk_test_');
    sk = (await code.innerText()).trim();
    await at(page, 'Each key is shown once', 0.2);
    await callout(page, 'Shown once', 2.4);
    await pause(page, 700);
    await mask(detail.locator('.codewrap code'));
    await at(page, 'The publishable key can only', 0.4);
    await callout(page, 'Publishable key: browser safe', 3.0);
    await detail.locator('#key-kind').selectOption('publishable');
    await type(page, detail.locator('#key-name'), 'checkout page', 20);
    await click(page, detail.getByRole('button', { name: 'Create key' }), 100);
    code = detail.locator('.codewrap code').last();
    await expect(code).toContainText('pk_test_');
    pk = (await code.innerText()).trim();
    await pause(page, 600);
    await mask(detail.locator('.codewrap code'));
    await at(page, 'Then the allowed browser origin', 0.3);
    await type(page, detail.getByLabel('Allowed browser origins'), SHOP, 8);
    await click(page, detail.getByRole('button', { name: 'Save origins' }), 100);
    await expect(page.locator('#toast')).toContainText('Allowed origins saved');
    await callout(page, 'Allowed origin: only this site may call from a browser', 2.8);
    await type(page, detail.locator('#wh-url'), `http://127.0.0.1:${HOOK_PORT}/hook`, 8);
    await at(page, 'The webhook signing secret', 0.2);
    await click(page, detail.getByRole('button', { name: 'Save', exact: true }), 100);
    code = detail.locator('.codewrap code').last();
    await expect(code).toContainText('whsec_');
    await code.scrollIntoViewIfNeeded(); // the secret appears in the keys block at the top of the window: scroll back to it (blurred)
    hooks.secret = (await code.innerText()).trim();
    await callout(page, 'Signing secret: shown once', 2.6);
    await pause(page, 1200);
    await mask(detail.locator('.codewrap code'));
    await endScene(page);

    // ---- scene 4: tokenize and authorize, twice ---------------------------------------------------------------------
    const card4242 = await tokenize(pk, '4242424242424242');
    const body1001 = {
      idempotency_key: 'order-1001', amount: 4999, currency: 'USD', payment_method: { type: 'card', card_token: card4242 },
      customer: customer('cus_42', 'pat@example.com', '198.51.100.20'),
      telemetry: { device_fingerprint: 'dev_abc', checkout_duration_ms: 41000, pasted_card_number: false }, shipping_address: { country: 'US' },
    };
    begin(4);
    await at(page, 'Now a payment', 0.2);
    await term.fresh([{ cat: 'app', text: 'SAMPLE APP: the shop calls PayBroker' }]);
    await callout(page, 'Token, not card number', 3);
    await term.run('TOKEN=$(tok 4242424242424242); echo $TOKEN', async () => card4242, 1.6);
    await at(page, "So the shop's server never sees", 0.2);
    await term.mark(['tok_vault_card_\\w+']);
    await at(page, 'The server then authorizes', 0.2);
    await term.unmark();
    let first!: { status: number; json: any };
    await term.run(shownAuth(body1001, '$TOKEN'), async () => {
      first = await authorize(sk, body1001);
      expect(first.status).toBe(200);
      expect(first.json.status).toBe('AUTHORIZED');
      ids['1001'] = first.json.payment_id;
      return JSON.stringify(first.json, null, 2);
    }, 2.6);
    await term.mark(['"idempotency_key":"order-1001"']);
    await at(page, 'The answer says authorized', 0.2);
    await term.unmark();
    await term.mark(['"status": "AUTHORIZED"', '"score": \\d+', '"psp_name": "[^"]+"']);
    await at(page, 'Now suppose the network', 0.2);
    await callout(page, 'Same idempotency key = same payment', 6);
    await term.unmark();
    await term.run(shownAuth(body1001, '$TOKEN'), async () => {
      const again = await authorize(sk, body1001);
      expect(again.json.payment_id).toBe(first.json.payment_id);
      return JSON.stringify(again.json, null, 2);
    }, 2.0);
    await at(page, 'It gets the same payment back', 0.2);
    await callout(page, `Same payment_id as before: ${ids['1001']}`, 5);
    await term.mark(['"payment_id": "[^"]+"']);
    await endScene(page);

    // ---- scene 5: the payment in the dashboard ----------------------------------------------------------------------
    begin(5);
    await at(page, 'In the dashboard', 0.2);
    await openPayment(ids['1001'], 15);
    await callout(page, 'Score 0-1000', 3);
    const offScore = await hl(detail.getByRole('heading', { name: /Fraud engine:/ }));
    await at(page, 'Below, the checks', 0.2);
    await offScore();
    await callout(page, 'Rules + model + routing, explained', 4);
    const offT = await hl(detail.getByRole('heading', { name: 'Timing' }));
    await at(page, 'And last, the processor', 0.2);
    await offT();
    await spot(page, detail.getByRole('heading', { name: 'Routing' }), 2400);
    await click(page, page.locator('#detail-close'), 200);
    await endScene(page);

    // ---- scene 6: card declined, 3-D Secure, cascade -----------------------------------------------------------------
    begin(6);
    const call = async (key: string, number: string, who: ReturnType<typeof customer>, extra: Record<string, unknown> = {}) => {
      const tok = await tokenize(pk, number);
      const body = { idempotency_key: key, amount: 4999, currency: 'USD', payment_method: { type: 'card', card_token: tok }, customer: who, ...extra };
      return { body, shown: shownAuth(body, `$(tok ${number})`), run: () => authorize(sk, body) };
    };
    const a = await call('order-1002', CARD_DECLINES, customer('cus_43', 'lee@example.com', '198.51.100.21'));
    const b = await call('order-1003', CARD_3DS, customer('cus_44', 'kim@example.com', '198.51.100.22'), { return_url: SHOP + '/return.html' });
    const c = await call('order-1004', CARD_CASCADE, customer('cus_45', 'sam@example.com', '198.51.100.23'));
    await at(page, 'Now, test cards', 0.2);
    await term.show();
    await at(page, 'The first one makes', 0.2);
    await callout(page, '402: bank declined', 5);
    await term.run(a.shown, async () => {
      const r = await a.run();
      expect(r.status, JSON.stringify(r.json)).toBe(402);
      expect(r.json.error.code).toBe('CARD_DECLINED');
      return `HTTP ${r.status}\n` + JSON.stringify(r.json, null, 2);
    }, 2.0);
    await at(page, 'Four oh two', 0.2);
    await term.mark(['HTTP 402', '"code": "CARD_DECLINED"']);
    await at(page, 'The second card demands', 0.2);
    await term.unmark();
    await callout(page, '3-D Secure: verify the customer', 6);
    let redirect = '';
    await term.run(b.shown, async () => {
      const r = await b.run();
      expect(r.status).toBe(200);
      expect(r.json.status).toBe('REQUIRES_ACTION');
      redirect = r.json.action_required.redirect_url;
      expect(redirect).toContain('/3ds/');
      return JSON.stringify(r.json, null, 2);
    }, 2.0);
    await at(page, 'The answer holds a page', 0.2);
    await term.mark(['"status": "REQUIRES_ACTION"', '"redirect_url": "[^"]+"']);
    await at(page, 'I open it, and approve', 0.3);
    await page.goto(redirect);
    await expect(page.getByRole('heading', { name: 'Verify this payment' })).toBeVisible();
    await pause(page, 400);
    await click(page, page.getByRole('button', { name: 'Approve (verification succeeds)' }), 200);
    await page.waitForURL(new RegExp(`^${SHOP.replace(/[.]/g, '\\.')}/return\\.html\\?payment_id=.*signature=`));
    await expect(page.getByRole('heading', { name: 'Back at Acme Shop' })).toBeVisible();
    await at(page, 'The customer is back', 0.3);
    await callout(page, 'Signed link: the shop can verify it', 3.4);
    await hl(page.locator('#u'));
    await at(page, 'The third card', 0.2);
    await term.show();
    await callout(page, 'Cascade: processor 1 failed, processor 2 approved', 6);
    await term.run(c.shown, async () => {
      const r = await c.run();
      expect(r.status).toBe(200);
      ids['1004'] = r.json.payment_id;
      return JSON.stringify(r.json, null, 2);
    }, 2.0);
    await at(page, 'PayBroker tries the next', 0.2);
    await openPayment(ids['1004'], 15);
    const routing = detail.locator('ol.timeline');
    await expect(routing).toContainText('error');
    await expect(routing).toContainText('approved');
    await at(page, 'The routing shows an error', 0.4);
    await spot(page, routing, 2600);
    await click(page, page.locator('#detail-close'), 200);
    await endScene(page);

    // ---- scene 7: the risky payment and the case -------------------------------------------------------------------
    begin(7);
    const tokRisky = await tokenize(pk, '5555555555554444');
    const risky = {
      idempotency_key: 'order-risky-1', amount: 450000, currency: 'USD', payment_method: { type: 'card', card_token: tokRisky },
      customer: { id: 'cus_risky', email: 'x@example.com', ip_address: '203.0.113.9', account_age_hours: 1 },
      telemetry: { device_fingerprint: 'dev_risky_z', checkout_duration_ms: 900, pasted_card_number: true }, shipping_address: { country: 'NG' },
    };
    await at(page, 'Now, the payment my marketplace fears', 0.2);
    await term.show();
    await callout(page, 'A brand-new account, a fast checkout, a pasted card, a large basket', 5);
    await atEnd(page, 'A brand-new account');
    let rr!: { status: number; json: any };
    await term.run(shownAuth(risky, '$(tok 5555555555554444)'), async () => {
      rr = await authorize(sk, risky);
      expect(rr.status).toBe(403);
      expect(rr.json.error.code).toBe('FRAUD_REJECTED');
      expect(rr.json.error.fraud_assessment.reasons).toContain('NEW_ACCOUNT');
      ids.risky = rr.json.error.payment_id;
      return `HTTP ${rr.status}\n` + JSON.stringify(rr.json, null, 2);
    }, 1.8);
    await at(page, 'Four oh three', 0.2);
    await callout(page, '403: rejected before any processor', 4);
    await term.mark(['HTTP 403', '"code": "FRAUD_REJECTED"', '"score": \\d+']);
    await at(page, 'A case opens', 0.2);
    await page.goto(PB + '/#cases');
    const item = page.locator('#case-list .case', { hasText: ids.risky });
    await expect(item).toBeVisible();
    await expect(item).toContainText('NEW_ACCOUNT');
    const offItem = await hl(item);
    await at(page, 'The summary comes from', 0.3);
    await offItem();
    await glide(page, item.getByRole('button', { name: ids.risky }));
    await item.getByRole('button', { name: ids.risky }).click();
    await expect(detail).toContainText('rule-based fallback');
    const offAi = await hl(detail.locator('.ai-box'));
    await at(page, 'The AI only advises', 0.2);
    await callout(page, 'AI advises, a person decides', 3);
    await offAi();
    const decision = detail.getByLabel('Decision');
    await glide(page, decision);
    await at(page, 'I choose permanent block', 0.2);
    await decision.selectOption('PERMANENT_BLOCK');
    await pause(page, 300);
    await click(page, detail.getByRole('button', { name: 'Resolve' }), 200);
    await expect(page.getByRole('alertdialog')).toBeVisible();
    await pause(page, 400);
    await click(page, page.getByRole('alertdialog').getByRole('button', { name: 'Block' }), 200);
    await expect(page.locator('#toast')).toContainText('Resolved. Blocked:');
    await callout(page, 'Blocked: device, ip, card', 3.4);
    await endScene(page);

    // ---- scene 8: signed webhooks -------------------------------------------------------------------------------------
    begin(8);
    const want = ['payment.authorized', 'payment.requires_action', 'payment.declined'];
    const deadline = Date.now() + 120_000;
    while (!want.every((t) => hooks.lines.some((l) => l.endsWith(t))) && Date.now() < deadline) await page.waitForTimeout(1000);
    for (const t of want) expect(hooks.lines).toContain('signature ok   ' + t);
    expect(hooks.lines.filter((l) => l.startsWith('SIGNATURE BAD'))).toHaveLength(0);
    await at(page, 'The shop also needs', 0.2);
    if (await detail.isVisible()) await click(page, page.locator('#detail-close'), 200);
    await page.goto(PB + '/#merchants');
    await click(page, page.locator('#view-merchants tbody tr', { hasText: 'Acme Shop' }), 200);
    const offUrl = await hl(detail.locator('#wh-url'));
    await at(page, "Here is the shop's receiver", 0.2);
    await offUrl();
    await term.fresh([{ cat: 'app', text: 'SAMPLE APP: the shop\'s webhook receiver' }]);
    await callout(page, 'Signed events: HMAC-SHA256', 3);
    await term.run('WHSEC=whsec_********** python3 receiver.py', async () => [...new Set(hooks.lines)].join('\n'), 1.4);
    await at(page, 'It checks every signature', 0.2);
    await term.mark(['signature ok']);
    await at(page, 'On the merchant', 0.2);
    await page.goto(PB + '/#merchants');
    await click(page, page.locator('#view-merchants tbody tr', { hasText: 'Acme Shop' }), 200);
    const ev = detail.getByRole('region', { name: 'Recent webhook events' });
    await expect(ev).toContainText('delivered');
    const offEv = await hl(ev);
    await at(page, 'If the shop is down', 0.2);
    await callout(page, 'Retries with back-off', 3.4);
    await pause(page, 2400);
    await offEv();
    await endScene(page);

    // ---- scene 9: under the hood, nearest past cases (pgvector, HNSW) ---------------------------------------------------
    begin(9);
    const txId = psql('postgres', ['-At', '-c', `SELECT id FROM transaction WHERE payment_id = '${ids.risky}'`]);
    const vec = psql('postgres', ['-At', '-c', `SELECT feature_vector FROM fraud_assessment WHERE transaction_id = '${txId}'`]);
    expect(vec).toMatch(/^\[[-0-9.e, ]+\]$/);
    expect(vec.split(',').length).toBe(17);
    await at(page, 'Now, under the hood', 0.2);
    if (await detail.isVisible()) await click(page, page.locator('#detail-close'), 200);
    await page.goto(PB + '/#cases');
    await callout(page, 'Have we seen payments like this one before?', 6);
    await at(page, 'Postgres answers it', 0.3);
    await term.fresh([{ cat: 'platform', text: 'PLATFORM: pgvector on Postgres' }, { cat: 'app', text: 'SAMPLE APP: the query and the data' }]);
    const dimsSql = `SELECT vector_dims(feature_vector) AS dims, left(feature_vector::text, 54) || '...' AS feature_vector FROM fraud_assessment LIMIT 1`;
    await term.run(`psql "$DB" -c "${dimsSql}"`, async () => owner(dimsSql), 2.0);
    await at(page, 'This index finds', 0.2);
    const idxSql = `SELECT indexdef FROM pg_indexes WHERE indexname = 'fa_vec_idx'`;
    await term.run(`psql "$DB" -At -c "${idxSql}"`, async () => psql('postgres', ['-At', '-c', idxSql]), 1.6);
    await term.mark(['hnsw', 'vector_cosine_ops']);
    await at(page, 'And here is the real query', 0.2);
    await term.unmark();
    const nearest = `SELECT t.payment_id, fa.tier2_ml_score AS score, fc.analyst_decision,
       fa.tier3_llm_analysis->>'risk_category' AS category,
       round((1 - (fa.feature_vector <=> :'v'))::numeric, 3) AS similarity
FROM fraud_assessment fa JOIN transaction t ON t.id = fa.transaction_id
LEFT JOIN fraud_case fc ON fc.transaction_id = fa.transaction_id
WHERE fa.transaction_id <> :'tx' AND fa.tier2_ml_score >= 500 AND fa.feature_vector IS NOT NULL
ORDER BY fa.feature_vector <=> :'v' LIMIT 5;`;
    await term.run(`psql "$DB" -v v="$VEC" -v tx="$TX" <<'SQL'\n${nearest}\nSQL`, async () => {
      const out = psql('postgres', ['-v', `v=${vec}`, '-v', `tx=${txId}`], nearest);
      expect(out).toContain('(5 rows)');
      return out;
    }, 3.2);
    await term.mark(['<=>', 'hnsw']);
    await at(page, 'Each row shows', 0.2);
    await term.unmark();
    await term.mark(['similarity', 'analyst_decision']);
    await at(page, 'Those five go to the triage', 0.2);
    await callout(page, 'Nearest flagged cases go to the AI triage', 4);
    await endScene(page);

    // ---- scene 10: fuzzystrmatch, the NAME_MISMATCH signal ---------------------------------------------------------------
    begin(10);
    await at(page, 'Next problem', 0.2);
    await term.fresh([{ cat: 'platform', text: 'PLATFORM: fuzzystrmatch extension' }, { cat: 'app', text: 'SAMPLE APP: the signal and the rule' }]);
    await callout(page, 'Does the cardholder name match the account name?', 6);
    await at(page, 'Here are two real calls', 0.2);
    const fuzzSql = `SELECT levenshtein('smith', 'smyth') AS edits, dmetaphone('smith') = dmetaphone('smyth') AS sound_alike, soundex('john') = soundex('jon') AS soundex_same`;
    await term.run(`psql "$DB" -c "${fuzzSql}"`, async () => {
      const out = owner(fuzzSql);
      expect(out).toMatch(/\n\s*1 \| t\s+\| t/);
      return out;
    }, 2.4);
    await at(page, 'Smith and Smyth are one letter', 0.2);
    await term.mark(['levenshtein', 'edits']);
    const tokName = await tokenize(pk, '4242424242424242');
    const bodyName = {
      idempotency_key: 'order-name-1', amount: 34900, currency: 'USD', payment_method: { type: 'card', card_token: tokName, cardholder_name: 'Jon Smith' },
      customer: { ...customer('cus_77', 'peter@example.com', '198.51.100.30'), name: 'Peter Wong' },
    };
    await at(page, 'The card says Jon Smith', 0.3);
    await term.unmark();
    const nameFilter = '{status, reasons: .fraud_assessment.reasons, action_required}';
    const shownName = shownAuth(bodyName, '$(tok 4242424242424242)').replace(/\| jq$/, `| jq '${nameFilter}'`);
    let nm!: { status: number; json: any };
    await term.run(shownName, async () => {
      nm = await authorize(sk, bodyName);
      expect(nm.status, JSON.stringify(nm.json)).toBe(200);
      const reasons: string[] = nm.json.fraud_assessment?.reasons ?? [];
      expect(reasons).toContain('NAME_MISMATCH');
      ids.name = nm.json.payment_id;
      const o = nm.json;
      return JSON.stringify({ status: o.status, reasons, action_required: o.action_required ? { type: o.action_required.type, redirect_url: '...' } : null }, null, 2);
    }, 3.0);
    await at(page, 'The reason code is name mismatch', 0.2);
    await callout(page, 'NAME_MISMATCH: reason code', 4);
    await term.mark(['"NAME_MISMATCH"', '"status": "[A-Z_]+"']);
    await at(page, 'In the dashboard', 0.2);
    await openPayment(ids.name, 15);
    const nmEl = detail.getByText('NAME_MISMATCH').first();
    await expect(nmEl).toBeVisible();
    await spot(page, nmEl, 2600);
    await click(page, page.locator('#detail-close'), 200);
    await endScene(page);

    // ---- scene 11: the four built-in rules ---------------------------------------------------------------------------------
    begin(11);
    await at(page, 'That step came', 0.2);
    await page.goto(PB + '/#rules');
    await expect(page.getByRole('heading', { name: 'Add a rule' })).toBeVisible();
    await at(page, 'There are four built-in', 0.2);
    await callout(page, 'Four built-in rules', 3.4);
    const offRules = await hl(page.locator('#view-rules table').first());
    await expect(page.locator('#view-rules')).toContainText('name_mismatch_large_basket');
    await at(page, 'The new one asks', 0.2);
    await offRules();
    await spot(page, page.locator('#view-rules tr', { hasText: 'name_mismatch_large_basket' }).first(), 4000);
    await at(page, 'Rules are plain data', 0.2);
    await spot(page, page.locator('#rule-def'), 4000);
    await endScene(page);

    // ---- scene 12: masking for the read-only role ------------------------------------------------------------------------------
    begin(12);
    const ownerSql = (t: string) => `SELECT payment_id, customer_id, request->>'ip' AS ip FROM ${t} ORDER BY created_at DESC LIMIT 3`;
    await at(page, 'Next problem', 0.2);
    await term.fresh([{ cat: 'platform', text: 'PLATFORM: a1p_readonly role (db-users --permission read)' }, { cat: 'app', text: 'SAMPLE APP: masked views' }]);
    await callout(page, 'Who can read the customer data?', 6);
    await at(page, "Here is the app's own connection", 0.2);
    await term.run(`psql "$DB" -c "${ownerSql('transaction')}"`, async () => {
      const out = owner(ownerSql('transaction'));
      expect(out).toContain('cus_');
      return out;
    }, 2.4);
    await at(page, 'It sees the real customer id', 0.2);
    await term.mark(['cus_\\w+', '\\d+\\.\\d+\\.\\d+\\.\\d+']);
    await at(page, 'Now the read-only role', 0.2);
    await term.unmark();
    await callout(page, 'Read-only role (db-users create --permission read)', 5);
    await at(page, 'The same query on the table', 0.2);
    await term.run(`psql "$RO" -c "${ownerSql('transaction')}"`, async () => {
      const out = reader(ownerSql('transaction'));
      expect(out).toContain('permission denied');
      return out;
    }, 2.4);
    await term.mark(['permission denied']);
    await at(page, 'But the masked view', 0.2);
    await term.unmark();
    await term.run(`psql "$RO" -c "${ownerSql('transaction_masked')}"`, async () => {
      const out = reader(ownerSql('transaction_masked'));
      expect(out).toContain('xxx');
      return out;
    }, 2.4);
    await at(page, 'Identifiers become short hashes', 0.2);
    await term.mark(['[0-9a-f]{12}', '\\d+\\.\\d+\\.\\d+\\.xxx']);
    await at(page, 'And a write is refused', 0.2);
    await term.unmark();
    const writeSql = `UPDATE transaction_masked SET fraud_score = 0`;
    await term.run(`psql "$RO" -c "${writeSql}"`, async () => {
      const out = reader(writeSql);
      expect(out).toMatch(/permission denied|cannot update|not allowed/);
      return out;
    }, 1.6);
    await term.mark(['ERROR[^\\n]*']);
    await endScene(page);

    // ---- scene 13: database insights: local (pg_stat_statements) and the captured AgentPG output -------------------------------
    begin(13);
    await at(page, 'Last problem', 0.2);
    await page.goto(PB + '/#insights');
    await at(page, 'That is the Database insights tab', 0.2);
    await expect(page.getByRole('heading', { name: 'Database insights' })).toBeVisible();
    await page.waitForTimeout(1500);
    await spot(page, page.getByRole('heading', { name: 'Database insights' }), 1500);
    await at(page, 'On my laptop', 0.2);
    await callout(page, 'Local Postgres: pg_stat_statements only', 6);
    await at(page, 'On AgentPG, the same page', 0.2);
    await callout(page, 'On AgentPG: + edb_stat_monitor, edb_pg_tuner, query_advisor', 6);
    await page.mouse.wheel(0, 700);
    await at(page, 'So this is real output', 0.2);
    await term.fresh([{ cat: 'cap', text: 'CAPTURED from our live AgentPG fleet: not run in this video' }, { cat: 'platform', text: 'PLATFORM: EDB extensions' }]);
    await term.run('agentpg extensions list $PAYBROKER_INSTANCE', async () => captured('extensions-installed.txt', 'paybroker'), 1.8);
    await at(page, 'The PayBroker instance has', 0.2);
    await term.mark(['edb_stat_monitor', 'query_advisor', 'vector', 'fuzzystrmatch']);
    await at(page, 'Its advisor suggested', 0.2);
    await term.unmark();
    await term.run('ecoctl insights   # runs: agentpg recommend', async () => captured('ecoctl-insights.txt', 'paybroker'), 1.6);
    await term.mark(['CREATE INDEX', 'CREATE STATISTICS']);
    await endScene(page);

    // ---- scene 14: try it yourself, and the legend again ------------------------------------------------------------------------
    begin(14);
    await page.goto(SHOP + '/diagram.html');
    for (const k of [6, 0, 1, 2, 3, 4]) await show(k);
    await at(page, 'On AgentPG, one command', 0.2);
    await show(7);
    await at(page, 'Or run it on your laptop', 0.2);
    await show(8);
    await pause(page, 900);
    await show(9);
    await at(page, 'Remember the three colours', 0.1);
    await hot(0, 1, 2, 3, 4);
    await at(page, 'Platform is Postgres', 0.1);
    await hot(0);
    await at(page, 'Scaffolding is swappable', 0.1);
    await hot(2, 3, 4);
    await at(page, 'PayBroker is the sample', 0.1);
    await hot(1);
    await at(page, 'The guide is in the docs', 0.1);
    await hot();
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
