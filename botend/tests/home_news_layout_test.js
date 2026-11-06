const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readSource} = require('./vm_harness');

// Geometry doubles test scheduling and measurement only, not browser grid placement.
function setup({readyState = 'complete', missing = false, observer = true, raf = true} = {}) {
  const frames = [], observers = [], events = new Map();
  let columns = '500px 500px', gap = '22px', writes = 0, reads = 0;
  const cards = [100.2, 250, 81, 122.1, 70].map(height => {
    const properties = new Map();
    return {height, hidden: false,
      getBoundingClientRect() { reads++; return {height: this.hidden ? 0 : this.height, width: this.hidden ? 0 : 500}; },
      style: {getPropertyValue: key => properties.get(key) || '',
        setProperty(key, value) { writes++; properties.set(key, value); },
        removeProperty(key) { writes++; properties.delete(key); }},
    };
  });
  const classes = new Set();
  const grid = {children: cards, classList: {
    add: key => classes.add(key), remove: key => classes.delete(key), contains: key => classes.has(key),
  }};
  const document = {readyState, getElementById: () => missing ? null : grid,
    addEventListener(name, callback) { events.set(name, callback); }};
  const context = vm.createContext({document,
    getComputedStyle: () => ({gridTemplateColumns: columns, columnGap: gap}),
    ResizeObserver: observer ? class {
      constructor(callback) { this.callback = callback; this.targets = []; observers.push(this); }
      observe(target) { this.targets.push(target); }
    } : undefined,
    requestAnimationFrame: raf ? callback => { frames.push(callback); return frames.length; } : undefined,
  });
  vm.runInContext(readSource('static/portal/js/home-news-layout.js'), context);
  return {cards, classes, grid, frames, observers, events,
    flush() { const batch = frames.splice(0); batch.forEach(callback => callback()); },
    notify() { observers.forEach(item => item.callback([])); },
    columns(value) { columns = value; }, gap(value) { gap = value; },
    get writes() { return writes; }, get reads() { return reads; },
  };
}
const span = card => card.style.getPropertyValue('--news-row-span');

test('natural border-box heights round up with the column gap; DOM remains intact', () => {
  const h = setup(), original = [...h.cards];
  h.flush();
  assert.equal(h.classes.has('is-masonry'), true);
  assert.deepEqual(h.cards.map(span), ['123', '272', '103', '145', '92']);
  assert.deepEqual(h.grid.children, original);
  assert.equal(h.observers.length, 1);
  assert.deepEqual(h.observers[0].targets, [h.grid, ...h.cards]);
  h.gap('17.5px'); h.notify(); h.flush();
  assert.equal(span(h.cards[0]), '118');
});

test('hidden and zero-height cards lose stale spans; restoration is observed', () => {
  const h = setup(); h.flush();
  h.cards[0].hidden = true; h.cards[1].height = 0;
  h.notify(); h.flush();
  assert.equal(span(h.cards[0]), ''); assert.equal(span(h.cards[1]), '');
  h.cards[0].hidden = false; h.cards[0].height = 300.5;
  h.notify(); h.flush();
  assert.equal(span(h.cards[0]), '323');
});

test('computed single column clears masonry and spans, desktop restoration remeasures', () => {
  const h = setup(); h.flush();
  h.columns('720px'); h.notify(); h.flush();
  assert.equal(h.classes.has('is-masonry'), false);
  assert.deepEqual(h.cards.map(span), ['', '', '', '', '']);
  h.cards[0].height = 400; h.columns('410px 410px'); h.notify(); h.flush();
  assert.equal(span(h.cards[0]), '422');
  assert.equal(h.classes.has('is-masonry'), true);
});

test('unsupported observers or animation frames and absent root safely keep grid fallback', () => {
  for (const options of [{observer: false}, {raf: false}, {missing: true}]) {
    const h = setup(options); h.flush();
    assert.equal(h.classes.has('is-masonry'), false);
    assert.equal(h.writes, 0); assert.equal(h.observers.length, 0);
  }
});

test('resize bursts coalesce; observer feedback does not rewrite spans or perpetuate frames', () => {
  const h = setup(); h.notify(); h.notify(); h.notify();
  assert.equal(h.frames.length, 1); h.flush();
  const writes = h.writes, reads = h.reads;
  h.notify(); h.notify(); h.flush();
  assert.equal(h.writes, writes); assert.equal(h.reads, reads + 5);
  assert.equal(h.frames.length, 0);
  h.cards[4].height = 800.3; h.notify(); h.flush();
  assert.equal(span(h.cards[4]), '823'); assert.equal(h.writes, writes + 1);
});

test('loading documents initialize once at DOMContentLoaded', () => {
  const h = setup({readyState: 'loading'});
  assert.equal(h.observers.length, 0);
  const ready = h.events.get('DOMContentLoaded'); assert.equal(typeof ready, 'function');
  ready(); ready(); h.flush();
  assert.equal(h.observers.length, 1);
  assert.equal(h.classes.has('is-masonry'), true);
});
