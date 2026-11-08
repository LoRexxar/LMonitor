const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.resolve(__dirname, '../../static/portal/js/main.js'), 'utf8');

// Pure event/controlled-HTTP VM unit tests; not browser/layout acceptance.
function setup({fine = true} = {}) {
  let now = 0, next = 0;
  const timers = new Map(), requests = [], ids = new Map();
  let document;
  class Element {
    constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.style = {}; this.dataset = {}; this.attrs = {}; this.children = []; this.listeners = {}; this.isConnected = true; this.innerHTML = ''; this.textContent = ''; }
    set id(v) { this._id = v; ids.set(v, this); } get id() { return this._id; }
    setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return this.attrs[k] || null; }
    appendChild(e) { this.children.push(e); e.parent = this; return e; }
    contains(e) { return this === e || this.children.some(c => c.contains(e)); }
    addEventListener(k, fn) { (this.listeners[k] ||= []).push(fn); }
    fire(k, ev = {}) { for (const fn of this.listeners[k] || []) fn({target: this, pointerType: 'mouse', preventDefault() { this.defaultPrevented = true; }, ...ev}); }
    querySelectorAll(s) { const all = this.children.flatMap(c => [c, ...c.querySelectorAll('*')]); return s === '*' ? all : all.filter(e => s === '[data-nga-article-id]' ? e.attrs['data-nga-article-id'] : s === '[data-nga-preview-toggle]' ? 'data-nga-preview-toggle' in e.attrs : false); }
    querySelector(s) { return this.querySelectorAll(s)[0] || null; }
    getBoundingClientRect() { return {left: 100, top: 80, bottom: 120, width: 400, height: 200}; }
    focus(opts) { document.activeElement = this; this.focusOptions = opts; }
  }
  document = new Element(); document.body = new Element('body'); document.activeElement = document.body;
  document.createElement = tag => new Element(tag);
  document.getElementById = id => ids.get(id) || null;
  const window = new Element(); window.innerWidth = 800; window.innerHeight = 600; window.matchMedia = () => ({matches: fine});
  const context = vm.createContext({document, window, URL, console, AbortController,
    Date: class extends Date { static now() { return now; } },
    setTimeout(fn, ms) { timers.set(++next, {fn, at: now + ms}); return next; }, clearTimeout(id) { timers.delete(id); },
    fetch(url, opts = {}) { return new Promise((resolve, reject) => requests.push({url, opts, resolve, reject})); }});
  vm.runInContext(source, context);
  const list = new Element(); list.id = 'nga-list';
  function row(id) { const e = new Element(); e.setAttribute('data-nga-article-id', id); const button = e.appendChild(new Element('button')); button.setAttribute('data-nga-preview-toggle', ''); const link = e.appendChild(new Element('a')); link.setAttribute('href', `https://bbs.nga.cn/read.php?tid=${id}`); list.appendChild(e); return {e, button, link}; }
  const a = row(1), b = row(2); context.bindNgaHoverTooltips(list);
  function tick(ms) { const end = now + ms; while (true) { const due = [...timers].filter(([, t]) => t.at <= end).sort((x,y) => x[1].at-y[1].at)[0]; if (!due) break; now = due[1].at; timers.delete(due[0]); due[1].fn(); } now = end; }
  const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
  const tip = () => ids.get('nga-hover-tooltip');
  const visible = () => !!tip() && tip().style.display !== 'none';
  const enter = r => { r.e.fire('pointerenter'); r.e.fire('mouseenter'); tick(200); };
  const leave = r => { r.e.fire('pointerleave'); r.e.fire('mouseleave'); };
  const resolve = async (n, content) => { requests[n].resolve({ok: true, json: async () => ({data: {content}})}); await flush(); };
  return {context, document, window, list, a, b, tick, flush, tip, visible, enter, leave, requests, resolve, timers};
}
function descendants(e) { return [e, ...e.children.flatMap(descendants)]; }
const content = s => descendants(s.tip()).map(e => e.textContent).join(' ');

test('leave invalidates a late HTTP response; no resurrection', async () => {
  const s = setup(); s.enter(s.a); s.leave(s.a); s.tick(160); await s.resolve(0, 'late A');
  assert.equal(s.visible(), false); assert.equal(s.requests[0].opts.signal.aborted, true); assert.equal(s.timers.size, 0);
});
test('A/B out-of-order responses cannot replace current post', async () => {
  const s = setup(); s.enter(s.a); s.enter(s.b); await s.resolve(1, 'current B'); await s.resolve(0, 'stale A');
  assert.match(content(s), /current B/); assert.doesNotMatch(content(s), /stale A/);
});
test('hover intent, tooltip crossing and async content preserve leave deadline', async () => {
  const s = setup(); s.a.e.fire('pointerenter'); s.tick(100); assert.equal(s.requests.length, 0); s.tick(100);
  s.leave(s.a); s.tick(50); s.tip().fire('mouseenter'); s.tick(200); assert.equal(s.visible(), true);
  s.tip().fire('mouseleave'); await s.resolve(0, 'readable'); s.tick(160); assert.equal(s.visible(), false);
});
test('explicit toggle pins existing request, switches posts, and closes with focus return', async () => {
  const s = setup(); s.enter(s.a); s.a.button.fire('click'); assert.equal(s.requests.length, 1);
  s.leave(s.a); s.tick(200); assert.equal(s.visible(), true); assert.equal(s.a.button.getAttribute('aria-expanded'), 'true');
  const close = descendants(s.tip()).find(e => e.tagName === 'BUTTON'); assert.equal(s.document.activeElement, close);
  close.fire('click'); assert.equal(s.visible(), false); assert.equal(s.document.activeElement, s.a.button); assert.equal(s.a.button.focusOptions.preventScroll, true);
  await s.resolve(0, 'late'); assert.equal(s.visible(), false);
  s.a.button.fire('click'); s.b.button.fire('click'); assert.equal(s.a.button.getAttribute('aria-expanded'), 'false');
  s.b.button.fire('click'); assert.equal(s.visible(), false);
});
for (const event of ['scroll', 'resize', 'blur', 'Escape', 'outside', 'hidden']) {
  test(`${event} closes and invalidates pending response`, async () => {
    const s = setup(); s.a.button.fire('click');
    if (event === 'Escape') s.document.fire('keydown', {key: 'Escape'});
    else if (event === 'outside') s.document.fire('pointerdown', {target: s.document.body});
    else if (event === 'hidden') { s.document.hidden = true; s.document.fire('visibilitychange'); }
    else s.window.fire(event, {target: s.document});
    assert.equal(s.visible(), false); await s.resolve(0, 'late'); assert.equal(s.visible(), false); assert.equal(s.timers.size, 0);
  });
}
test('internal scroll stays open; external scroll suppresses immediate hover reopening', () => {
  const s = setup(); s.enter(s.a); s.window.fire('scroll', {target: s.tip()}); assert.equal(s.visible(), true);
  s.window.fire('scroll', {target: s.document}); s.enter(s.b); assert.equal(s.visible(), false); assert.equal(s.requests.length, 1);
});
test('touch and synthetic mouse hover do not open; native preview click does; title click untouched', () => {
  const s = setup({fine: false}); s.enter(s.a); assert.equal(s.requests.length, 0);
  s.a.e.fire('pointerenter', {pointerType: 'touch'}); s.tick(200); assert.equal(s.requests.length, 0);
  s.a.link.fire('click'); assert.equal(s.requests.length, 0); assert.equal(s.a.link.getAttribute('href'), 'https://bbs.nga.cn/read.php?tid=1');
  s.a.button.fire('click'); assert.equal(s.visible(), true);
});
test('safe text, 900-character truncation, success cache, failure retry and timeout cleanup', async () => {
  const s = setup(); s.a.button.fire('click'); const raw = '<img src=x onerror=alert(1)>' + 'x'.repeat(1000); await s.resolve(0, raw);
  const text = descendants(s.tip()).find(e => e.textContent.startsWith('<img')); assert.ok(text); assert.equal(text.innerHTML, ''); assert.equal(text.textContent.length, 903);
  s.a.button.fire('click'); s.a.button.fire('click'); await s.flush(); assert.equal(s.requests.length, 1);
  s.b.button.fire('click'); s.requests[1].reject(Error('offline')); await s.flush(); s.b.button.fire('click'); s.b.button.fire('click'); assert.equal(s.requests.length, 3);
  s.tick(10000); assert.equal(s.requests[2].opts.signal.aborted, true); s.requests[2].reject(Error('abort')); await s.flush(); assert.equal(s.timers.size, 0); assert.match(content(s), /暂时不可用/);
});
for (const kind of ['empty', 'filtered', 'skeleton']) {
  test(`${kind} NGA rerender invalidates session`, async () => {
    const s = setup(); s.a.button.fire('click');
    if (kind === 'skeleton') s.context.renderSkeleton('nga-list');
    else { if (kind === 'filtered') vm.runInContext('PORTAL_STATE.query = "absent"', s.context); s.context.renderSimpleList('nga-list', kind === 'empty' ? [] : [{id: 1, title: 'original'}]); }
    assert.equal(s.visible(), false); await s.resolve(0, 'late'); assert.equal(s.visible(), false);
  });
}
test('dismissal blocks stationary-row reopen, clears intent, and permits explicit reopening', () => {
  const s = setup(); s.enter(s.a); s.document.fire('keydown', {key: 'Escape'}); s.enter(s.a);
  assert.equal(s.visible(), false); assert.equal(s.requests.length, 1);
  s.a.button.fire('click'); assert.equal(s.visible(), true);
  s.context.closeNgaPreview(); s.b.e.fire('pointerenter'); s.document.fire('pointerdown', {target: s.document.body}); s.tick(200);
  assert.equal(s.visible(), false); assert.equal(s.requests.length, 2);
});
test('focus crossing cancels hover leave; global and row listeners bind once; detached response ignored', async () => {
  const s = setup(); s.context.bindNgaHoverTooltips(s.list);
  assert.equal(s.window.listeners.scroll.length, 1); assert.equal(s.document.listeners.pointerdown.length, 1); assert.equal(s.a.e.listeners.pointerenter.length, 1);
  s.enter(s.a); s.leave(s.a); s.tip().fire('focusin'); s.tick(200); assert.equal(s.visible(), true);
  s.a.e.isConnected = false; await s.resolve(0, 'detached text'); assert.doesNotMatch(content(s), /detached text/);
});
test('outside dismissal does not move focus; Escape returns to preview trigger', () => {
  const s = setup(); s.a.button.fire('click'); s.document.fire('keydown', {key: 'Escape'}); assert.equal(s.document.activeElement, s.a.button);
  s.b.button.fire('click'); s.document.activeElement = s.a.link; s.document.fire('pointerdown', {target: s.a.link}); assert.equal(s.document.activeElement, s.a.link);
});
test('NGA markup adds native preview control without mutating original payload/link/meta', () => {
  const s = setup(); const row = {id: 7, title: 'Original <title>', url: 'https://bbs.nga.cn/read.php?tid=7', author: 'Author', source: 'NGA', reply_count: 12}; const before = JSON.stringify(row);
  s.context.renderSimpleList('nga-list', [row]); const html = s.list.innerHTML;
  assert.equal(JSON.stringify(row), before); assert.match(html, /href="https:\/\/bbs.nga.cn\/read.php\?tid=7" target="_blank" rel="noreferrer"/);
  assert.match(html, /Original &lt;title&gt;/); assert.match(html, /Author/); assert.match(html, /12 回复/); assert.match(html, /type="button"[^>]*data-nga-preview-toggle/);
  assert.match(html, /aria-haspopup="dialog"/); assert.match(html, /aria-controls="nga-hover-tooltip"/);
});


for (const action of ['close', 'Escape']) {
  test(`${action}: hiding overlay must not open the newly uncovered different row`, () => {
    const s = setup(); s.enter(s.a); s.a.button.fire('click'); s.leave(s.a);
    s.tip().fire('mouseenter');
    s.document.fire('pointermove', {clientX: 230, clientY: 220});
    if (action === 'close') descendants(s.tip()).find(e => e.tagName === 'BUTTON').fire('click');
    else s.document.fire('keydown', {key: 'Escape'});
    // Browser hit-testing changes after hiding the overlay, with no mouse motion.
    s.b.e.fire('pointerenter', {clientX: 230, clientY: 220}); s.tick(350);
    assert.equal(s.visible(), false); assert.equal(s.requests.length, 1);
    // A new intentional hover at a different position is still usable.
    s.b.e.fire('pointerleave'); s.b.e.fire('pointerenter', {clientX: 250, clientY: 220}); s.tick(200);
    assert.equal(s.visible(), true); assert.equal(s.requests.length, 2);
  });
}
