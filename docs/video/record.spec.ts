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
 * BLOCKLIST_BEAT=1 (the optional "same card again" beat of scene 7), HEADED=1.
 * The recorder asserts what the script claims (403 with NEW_ACCOUNT, 402 CARD_DECLINED, REQUIRES_ACTION then the signed return,
 * routing error then approved, signature ok for the three event types, delivered events), so a take that drifts fails.
 * Keys and the signing secret are blurred the moment they are rendered and masked a second later.
 */
import { test, expect, type Locator, type Page } from '@playwright/test';
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
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
  #t { box-sizing: border-box; height: 100vh; padding: 36px 48px 150px; overflow: hidden; color: #e2e8f0; font: 23px/1.32 ui-monospace, Menlo, Consolas, monospace; white-space: pre-wrap; word-break: break-all; display: flex; flex-direction: column; justify-content: flex-start; }
  #t > div { flex: none; }
  .p { color: #4ade80; } .c { color: #f8fafc; } .o { color: #94a3b8; margin-bottom: 12px; }
  mark { background: #f59e0b; color: #0b1220; padding: 1px 6px; border-radius: 5px; font-weight: 700; animation: mk 1.1s ease-in-out infinite; }
  @keyframes mk { 50% { background: #fcd34d; box-shadow: 0 0 0 4px rgba(245,158,11,.35); } }
</style><div id="t"></div>
<script>
  const t = document.getElementById('t');
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const line = (cls, text) => { const d = document.createElement('div'); d.className = cls; d.textContent = text; t.appendChild(d); while (t.scrollHeight > t.clientHeight + 2 && t.children.length > 1) t.removeChild(t.firstChild); return d; };
  window.__term = {
    restore(items) { t.textContent = ''; for (const i of items) { if (i.cmd) line('c', '$ ' + i.cmd); if (i.out) line('o', i.out); } },
    async type(cmd, ms) { const d = line('c', '$ '); const step = Math.max(1, Math.ceil(cmd.length / (ms / 22))); for (let i = 0; i < cmd.length; i += step) { d.textContent = '$ ' + cmd.slice(0, i + step); await sleep(ms / Math.ceil(cmd.length / step)); } await sleep(250); },
    out(text) { line('o', text); },
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
  .rv.show { opacity: 1; transform: none; } .rv.q { color: #fcd34d; } .rv.a { color: #4ade80; font-weight: 700; } .rv.cmd { font: 40px ui-monospace, Menlo, monospace; color: #e2e8f0; margin: 8px 0; }
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
  const term = {
    async show() {
      if (!page.url().startsWith(SHOP + '/terminal.html')) {
        await page.goto(SHOP + '/terminal.html');
        await page.evaluate((h) => (window as any).__term.restore(h), history);
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

  try {
    // ---- scene 1: title card, then the setup page -------------------------------------------------------------------
    begin(1);
    await card('PayBroker', 'One payment API in front of many processors', [
      { text: 'Is this payment safe?', cls: 'q' }, { text: 'Which processor should handle it?', cls: 'q' }, { text: 'What if that processor fails?', cls: 'q' },
      { text: 'One API: smart routing + fraud checks', cls: 'a' }, { text: 'A sample application on Postgres + pgvector, running on AgentPG', cls: '' },
      { text: 'Test cards and simulated processors only: nothing moves real money', cls: '' },
    ]);
    await at(page, 'Is this payment safe', 0.2);
    await show(0);
    await pause(page, 1500);
    await show(1);
    await pause(page, 1500);
    await show(2);
    await at(page, 'PayBroker answers all three', 0.3);
    await hide(0); await hide(1); await hide(2);
    await show(3);
    await at(page, 'It is a sample application', 0.3);
    await show(4);
    await at(page, 'It uses only test cards', 0.3);
    await show(5);
    await at(page, "Let's set it up", 0.2);
    await page.goto(PB + '/');
    await expect(page.getByRole('heading', { name: 'Set up PayBroker' })).toBeVisible();
    await callout(page, 'One-time setup code from the server log', 3.4);
    await type(page, page.locator('#setup-code'), stack.setupCode, 40);
    await at(page, 'Then I choose', 0.3);
    await type(page, page.locator('#setup-pw'), adminPassword, 30);
    await type(page, page.locator('#setup-pw2'), adminPassword, 30);
    await click(page, page.getByRole('button', { name: 'Create admin password' }), 300);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
    await endScene(page);

    // ---- scene 2: demo traffic, the dashboard -------------------------------------------------------------------------
    begin(2);
    const demoBtn = page.getByRole('button', { name: 'Generate 120 demo payments' });
    await expect(demoBtn).toBeVisible();
    await at(page, 'This is the dashboard', 0.1);
    await callout(page, 'Empty overview: no payments yet', 3.6);
    await glide(page, page.locator('a.stat').first());
    const offStats = await hl(page.locator('a.stat').first().locator('xpath=..'));
    await at(page, 'One click loads', 0.2);
    await callout(page, '120 fictional payments', 4.2);
    await offStats();
    const offBtn = await hl(demoBtn);
    await at(page, 'Ordinary customers', 0.2);
    await callout(page, 'Customers, declines, 3-D Secure, card testing, takeover', 7);
    await glide(page, demoBtn);
    await at(page, 'I click, and the payments', 0.2);
    await offBtn();
    await demoBtn.click();
    const waitStart = Date.now();
    await expect(page.locator('#toast')).toContainText('Sent 120 demo payments', { timeout: 90_000 });
    await expect(page.getByRole('heading', { name: 'Fraud score distribution' })).toBeVisible({ timeout: 30_000 });
    console.log(`demo traffic took ${((Date.now() - waitStart) / 1000).toFixed(1)} s`);
    await at(page, 'Done.', 0.2);
    await spot(page, page.locator('a.stat', { hasText: 'approved' }).first(), 3000);
    await at(page, 'Payments are spread', 0.3);
    const offConn = await hl(page.getByRole('region', { name: 'Payments by connector' }));
    await at(page, 'This chart is the fraud score', 0.3);
    await offConn();
    await callout(page, 'under 300 approve | 300-699 verify | 700+ decline', 8);
    await spot(page, page.locator('.card', { has: page.getByRole('heading', { name: 'Fraud score distribution' }) }), 7000);
    await endScene(page);

    // ---- scene 3: merchant, keys, origin, webhook -----------------------------------------------------------------
    begin(3);
    const detail = page.locator('#detail');
    await at(page, 'Now let', 0.2);
    await page.goto(PB + '/#merchants');
    await at(page, 'I open Merchants', 0.2);
    await click(page, page.getByRole('button', { name: 'New merchant' }), 300);
    await type(page, page.getByLabel('Name'), 'Acme Shop');
    await click(page, page.getByRole('button', { name: 'Create', exact: true }), 300);
    await expect(detail.getByRole('heading', { name: 'Acme Shop' })).toBeVisible();
    await at(page, 'Then come two keys', 0.3);
    await callout(page, 'Secret key: server only', 3.6);
    await detail.locator('#key-kind').selectOption('secret');
    await at(page, 'I create it', 0.3);
    await type(page, detail.locator('#key-name'), 'checkout server');
    await click(page, detail.getByRole('button', { name: 'Create key' }), 200);
    let code = detail.locator('.codewrap code').last();
    await expect(code).toContainText('sk_test_');
    sk = (await code.innerText()).trim();
    await callout(page, 'Shown once', 3);
    await pause(page, 1000);
    await mask(detail.locator('.codewrap code'));
    await at(page, 'The publishable key is different', 0.3);
    await callout(page, 'Publishable key: browser safe', 3.6);
    await detail.locator('#key-kind').selectOption('publishable');
    await type(page, detail.locator('#key-name'), 'checkout page');
    await at(page, 'That makes it safe', 0.5);
    await click(page, detail.getByRole('button', { name: 'Create key' }), 200);
    code = detail.locator('.codewrap code').last();
    await expect(code).toContainText('pk_test_');
    pk = (await code.innerText()).trim();
    await pause(page, 900);
    await mask(detail.locator('.codewrap code'));
    await at(page, 'Next, the shop', 0.3);
    await type(page, detail.getByLabel('Allowed browser origins'), SHOP, 35);
    await at(page, 'Only that site', 0.3);
    await click(page, detail.getByRole('button', { name: 'Save origins' }), 200);
    await expect(page.locator('#toast')).toContainText('Allowed origins saved');
    await callout(page, 'Allowed origin: only this site may call from a browser', 3.6);
    await at(page, 'Then a webhook address', 0.3);
    await type(page, detail.locator('#wh-url'), `http://127.0.0.1:${HOOK_PORT}/hook`, 30);
    await at(page, 'The signing secret appears', 0.2);
    await click(page, detail.getByRole('button', { name: 'Save', exact: true }), 200);
    code = detail.locator('.codewrap code').last();
    await expect(code).toContainText('whsec_');
    await code.scrollIntoViewIfNeeded(); // the secret appears in the keys block at the top of the window: scroll back to it (blurred)
    hooks.secret = (await code.innerText()).trim();
    await callout(page, 'Signing secret: shown once', 3.2);
    await pause(page, 1500);
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
    await at(page, 'Time for a payment', 0.2);
    await term.show();
    await callout(page, 'Token, not card number', 4);
    await term.run('TOKEN=$(tok 4242424242424242); echo $TOKEN', async () => card4242, 1.6);
    await at(page, 'It uses the publishable key', 0.2);
    await callout(page, 'Publishable key: card in, token out', 3.6);
    await term.mark(['tok_vault_card_\\w+']);
    await at(page, 'Now the server authorizes', 0.2);
    await term.unmark();
    let first!: { status: number; json: any };
    await term.run(shownAuth(body1001, '$TOKEN'), async () => {
      first = await authorize(sk, body1001);
      expect(first.status).toBe(200);
      expect(first.json.status).toBe('AUTHORIZED');
      ids['1001'] = first.json.payment_id;
      return JSON.stringify(first.json, null, 2);
    }, 2.6);
    await at(page, 'It adds an idempotency key', 0.2);
    await term.mark(['"idempotency_key":"order-1001"']);
    await at(page, 'The answer says authorized', 0.2);
    await term.unmark();
    await term.mark(['"status": "AUTHORIZED"', '"score": \\d+', '"psp_name": "[^"]+"']);
    await at(page, 'Now suppose the network', 0.2);
    await callout(page, 'Same idempotency key = same payment', 7);
    await term.unmark();
    await term.run(shownAuth(body1001, '$TOKEN'), async () => {
      const again = await authorize(sk, body1001);
      expect(again.json.payment_id).toBe(first.json.payment_id);
      return JSON.stringify(again.json, null, 2);
    }, 2.0);
    await at(page, 'It gets the same payment back', 0.2);
    await callout(page, `Same payment_id as before: ${ids['1001']}`, 6);
    await term.mark(['"payment_id": "[^"]+"']);
    await endScene(page);

    // ---- scene 5: the payment in the dashboard ----------------------------------------------------------------------
    begin(5);
    await at(page, 'Back in the dashboard', 0.2);
    await openPayment(ids['1001']);
    await at(page, 'At the top', 0.2);
    await callout(page, 'Score 0-1000', 3.4);
    const offScore = await hl(detail.getByRole('heading', { name: /Fraud engine:/ }));
    await at(page, 'This payment is clean', 0.2);
    await offScore();
    const clean = detail.getByText('No risk signals worth reporting');
    if (await clean.count()) await hl(clean).then((off) => pause(page, 2000).then(off));
    await at(page, 'Below, the checks', 0.2);
    await callout(page, 'Rules + model + routing, explained', 8);
    const t1 = detail.getByRole('heading', { name: 'Tier 1 rules and filters' });
    if (await t1.count()) await spot(page, t1, 2000);
    await at(page, 'Here is the time spent', 0.2);
    const offT = await hl(detail.getByRole('heading', { name: 'Timing' }));
    await at(page, 'Next, the details', 0.2);
    await offT();
    await click(page, detail.getByText('Features the model saw'), 300);
    await at(page, 'And last', 0.2);
    await spot(page, detail.getByRole('heading', { name: 'Routing' }), 2800);
    await at(page, 'Every decision is explained', 0.2);
    await click(page, page.locator('#detail-close'), 300);
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
    await at(page, 'Next, test cards', 0.2);
    await term.show();
    await at(page, 'The first one makes', 0.2);
    await callout(page, '402: bank declined', 6);
    await term.run(a.shown, async () => {
      const r = await a.run();
      expect(r.status, JSON.stringify(r.json)).toBe(402);
      expect(r.json.error.code).toBe('CARD_DECLINED');
      return `HTTP ${r.status}\n` + JSON.stringify(r.json, null, 2);
    }, 2.2);
    await at(page, 'The answer is four oh two', 0.2);
    await term.mark(['HTTP 402', '"code": "CARD_DECLINED"']);
    await at(page, 'The second card demands', 0.2);
    await term.unmark();
    await callout(page, '3-D Secure: verify the customer', 8);
    let redirect = '';
    await term.run(b.shown, async () => {
      const r = await b.run();
      expect(r.status).toBe(200);
      expect(r.json.status).toBe('REQUIRES_ACTION');
      redirect = r.json.action_required.redirect_url;
      expect(redirect).toContain('/3ds/');
      return JSON.stringify(r.json, null, 2);
    }, 2.2);
    await at(page, 'The answer holds a page', 0.2);
    await term.mark(['"status": "REQUIRES_ACTION"', '"redirect_url": "[^"]+"']);
    await at(page, 'I open it', 0.3);
    await page.goto(redirect);
    await expect(page.getByRole('heading', { name: 'Verify this payment' })).toBeVisible();
    await pause(page, 600);
    await click(page, page.getByRole('button', { name: 'Approve (verification succeeds)' }), 200);
    await page.waitForURL(new RegExp(`^${SHOP.replace(/[.]/g, '\\.')}/return\\.html\\?payment_id=.*signature=`));
    await expect(page.getByRole('heading', { name: 'Back at Acme Shop' })).toBeVisible();
    await at(page, 'The customer is back', 0.3);
    await callout(page, 'Signed link: the shop can verify it', 5);
    await hl(page.locator('#u'));
    await at(page, 'The third card', 0.2);
    await term.show();
    await callout(page, 'Cascade: processor 1 failed, processor 2 approved', 9);
    await term.run(c.shown, async () => {
      const r = await c.run();
      expect(r.status).toBe(200);
      ids['1004'] = r.json.payment_id;
      return JSON.stringify(r.json, null, 2);
    }, 2.2);
    await at(page, 'PayBroker tries the next', 0.2);
    await openPayment(ids['1004']);
    const routing = detail.locator('ol.timeline');
    await expect(routing).toContainText('error');
    await expect(routing).toContainText('approved');
    await at(page, 'First an error', 0.4);
    await spot(page, routing, 3000);
    await click(page, page.locator('#detail-close'), 300);
    await endScene(page);

    // ---- scene 7: the risky payment and the case -------------------------------------------------------------------
    begin(7);
    const tokRisky = await tokenize(pk, '5555555555554444');
    const risky = {
      idempotency_key: 'order-risky-1', amount: 450000, currency: 'USD', payment_method: { type: 'card', card_token: tokRisky },
      customer: { id: 'cus_risky', email: 'x@example.com', ip_address: '203.0.113.9', account_age_hours: 1 },
      telemetry: { device_fingerprint: 'dev_risky_z', checkout_duration_ms: 900, pasted_card_number: true }, shipping_address: { country: 'NG' },
    };
    await at(page, 'Now a risky payment', 0.2);
    await term.show();
    await callout(page, 'A brand-new account, a fast checkout, a pasted card, a large basket', 6);
    await at(page, "Let's send it", 0.2);
    let rr!: { status: number; json: any };
    await term.run(shownAuth(risky, '$(tok 5555555555554444)'), async () => {
      rr = await authorize(sk, risky);
      expect(rr.status).toBe(403);
      expect(rr.json.error.code).toBe('FRAUD_REJECTED');
      expect(rr.json.error.fraud_assessment.reasons).toContain('NEW_ACCOUNT');
      ids.risky = rr.json.error.payment_id;
      return `HTTP ${rr.status}\n` + JSON.stringify(rr.json, null, 2);
    }, 2.0);
    await at(page, 'Four oh three', 0.2);
    await callout(page, '403: rejected before any processor', 5);
    await term.mark(['HTTP 403', '"code": "FRAUD_REJECTED"', '"score": \\d+']);
    await at(page, 'A case opens', 0.2);
    await page.goto(PB + '/#cases');
    const item = page.locator('#case-list .case', { hasText: ids.risky });
    await expect(item).toBeVisible();
    await expect(item).toContainText('NEW_ACCOUNT');
    await at(page, 'Here it is', 0.2);
    const offItem = await hl(item);
    await at(page, 'The analyst reads', 0.3);
    await offItem();
    await glide(page, item.getByRole('button', { name: ids.risky }));
    await item.getByRole('button', { name: ids.risky }).click();
    await expect(detail).toContainText('rule-based fallback');
    const offReasons = await hl(detail.getByRole('heading', { name: 'Reasons' }));
    await at(page, 'Here it comes from', 0.2);
    await offReasons();
    const offAi = await hl(detail.locator('.ai-box'));
    await at(page, 'The AI only advises', 0.2);
    await callout(page, 'AI advises, a person decides', 4);
    await offAi();
    const decision = detail.getByLabel('Decision');
    await glide(page, decision);
    const offDec = await hl(decision);
    await at(page, 'I choose permanent block', 0.2);
    await offDec();
    await decision.selectOption('PERMANENT_BLOCK');
    await pause(page, 500);
    await click(page, detail.getByRole('button', { name: 'Resolve' }), 200);
    await expect(page.getByRole('alertdialog')).toBeVisible();
    await pause(page, 600);
    await click(page, page.getByRole('alertdialog').getByRole('button', { name: 'Block' }), 200);
    await expect(page.locator('#toast')).toContainText('Resolved. Blocked:');
    await at(page, 'The device, the address', 0.4);
    await callout(page, 'Blocked: device, ip, card', 4);
    await at(page, 'In Rules', 0.2);
    await click(page, page.locator('#detail-close'), 200);
    await page.goto(PB + '/#rules');
    await spot(page, page.getByRole('region', { name: 'Blocklist' }), 3500);
    if (process.env.BLOCKLIST_BEAT) {
      const again = await authorize(sk, { ...risky, idempotency_key: 'order-risky-2', payment_method: { type: 'card', card_token: await tokenize(pk, '5555555555554444') } });
      expect(again.status).toBe(403);
      expect(again.json.error.fraud_assessment.reasons).toContain('BLACK_LISTED_CARD');
    }
    await endScene(page);

    // ---- scene 8: a rule ----------------------------------------------------------------------------------------------
    begin(8);
    await at(page, 'Now, the rules', 0.2);
    await page.goto(PB + '/#rules');
    await expect(page.getByRole('heading', { name: 'Add a rule' })).toBeVisible();
    await at(page, 'There are three built-in', 0.2);
    await callout(page, 'Rules = data, validated', 4);
    const offRules = await hl(page.locator('#view-rules table').first());
    await at(page, 'I add one', 0.2);
    await offRules();
    await type(page, page.locator('#rule-name'), 'rushed_large_basket', 40);
    await page.locator('#rule-action').selectOption('challenge');
    await pause(page, 500);
    await at(page, 'The editor only accepts', 0.2);
    await spot(page, page.locator('#rule-def'), 2500);
    await click(page, page.getByRole('button', { name: 'Add rule' }), 300);
    await expect(page.locator('#view-rules')).toContainText('rushed_large_basket');
    await at(page, 'The system can also propose', 0.2);
    await callout(page, 'Suggestions wait for a person', 4);
    const offSug = await hl(page.getByRole('heading', { name: 'Suggested rules' }));
    await at(page, 'But a suggestion', 0.2);
    await pause(page, 2500);
    await offSug();
    await endScene(page);

    // ---- scene 9: signed webhooks -------------------------------------------------------------------------------------
    begin(9);
    const want = ['payment.authorized', 'payment.requires_action', 'payment.declined'];
    const deadline = Date.now() + 120_000;
    while (!want.every((t) => hooks.lines.some((l) => l.endsWith(t))) && Date.now() < deadline) await page.waitForTimeout(1000);
    for (const t of want) expect(hooks.lines).toContain('signature ok   ' + t);
    expect(hooks.lines.filter((l) => l.startsWith('SIGNATURE BAD'))).toHaveLength(0);
    await at(page, 'Remember the webhook', 0.2);
    await term.show();
    await callout(page, 'Signed events: HMAC-SHA256', 4);
    await term.run('WHSEC=whsec_********** python3 receiver.py', async () => [...new Set(hooks.lines)].join('\n'), 1.4);
    await at(page, 'It checks the signature', 0.2);
    await term.mark(['signature ok']);
    await at(page, 'On the merchant', 0.2);
    await page.goto(PB + '/#merchants');
    await click(page, page.locator('#view-merchants tbody tr', { hasText: 'Acme Shop' }), 300);
    const ev = detail.getByRole('region', { name: 'Recent webhook events' });
    await expect(ev).toContainText('delivered');
    const offEv = await hl(ev);
    await at(page, 'With its attempts', 0.2);
    await offEv();
    const offAtt = await hl(ev.getByRole('columnheader', { name: 'Attempts' }));
    await pause(page, 1500);
    await offAtt();
    const offHttp = await hl(ev.getByRole('columnheader', { name: 'Last HTTP' }));
    await at(page, 'If the shop is down', 0.2);
    await offHttp();
    await callout(page, 'Retries with back-off', 4);
    const offEv2 = await hl(ev);
    await at(page, 'So no event is lost', 0.5);
    await pause(page, 1500);
    await offEv2();
    await endScene(page);

    // ---- scene 10: connect and closing card ---------------------------------------------------------------------------
    begin(10);
    await click(page, page.locator('#detail-close'), 200);
    await page.goto(PB + '/#connect');
    const mcp = page.locator('#view-connect pre.code').last();
    await expect(mcp).toContainText('/mcp');
    await at(page, 'Everything you saw', 0.1);
    const offMcp = await hl(mcp);
    await at(page, 'It can search payments', 0.2);
    await callout(page, 'MCP: read-only', 4);
    await at(page, 'To recap', 0.2);
    await offMcp();
    await card('Recap', 'PayBroker', [
      { text: 'One API' }, { text: 'Smart routing' }, { text: 'Rules, model, AI triage' }, { text: 'A person decides', cls: 'a' },
      { text: 'docker compose up -d db', cls: 'cmd' }, { text: 'paybroker migrate', cls: 'cmd' }, { text: 'paybroker serve', cls: 'cmd' }, { text: 'docs/GUIDE.md', cls: 'a' },
    ]);
    await at(page, 'One API.', 0.1);
    await show(0);
    await at(page, 'Smart routing', 0.1);
    await show(1);
    await at(page, 'Three layers', 0.1);
    await show(2);
    await at(page, 'And a person who decides', 0.1);
    await show(3);
    await at(page, 'Try it yourself', 0.1);
    await hide(0); await hide(1); await hide(2); await hide(3);
    await show(4);
    await pause(page, 1100);
    await show(5);
    await pause(page, 1100);
    await show(6);
    await at(page, 'The full guide', 0.1);
    await show(7);
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
