import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const context = {window: {}};
vm.runInNewContext(fs.readFileSync(new URL('../../../static/portal/js/gear-catalog.js', import.meta.url), 'utf8'), context);
const {createLoader} = context.window.WowGearCatalog;
let calls = 0;
let time = 0;
let resolve;
const ready = {items: [], snapshot: {state: 'ready'}};
const load = createLoader(() => { calls++; return new Promise(done => { resolve = done; }); }, {now: () => time, ttlMs: 60});
const a = load('head');
const b = load('head');
await Promise.resolve();
assert.equal(calls, 1, '相同坐标并发请求应合并');
resolve(ready);
await Promise.all([a, b]);
await load('head');
assert.equal(calls, 1, '同槽位筛选与翻页复用文件');
time = 61;
const c = load('head');
await Promise.resolve();
resolve(ready);
await c;
assert.equal(calls, 2, '超过刷新间隔重新校验');
let attempts = 0;
const retry = createLoader(async () => {
  attempts++;
  if (attempts === 1) throw Error('临时错误');
  return attempts === 2 ? {snapshot: {state: 'building'}} : ready;
});
await assert.rejects(retry('head'));
await retry('head');
await retry('head');
await retry('head');
assert.equal(attempts, 3, '失败和准备中不应永久缓存');
let boundedCalls = 0;
const bounded = createLoader(async () => { boundedCalls++; return ready; }, {maxEntries: 2});
for (const key of ['head', 'chest', 'legs', 'head']) await bounded(key);
assert.equal(boundedCalls, 4, '缓存必须有界');
console.log('装备目录缓存：并发合并、过期刷新、失败恢复、有界缓存通过。');

// 执行真实页面加载函数，防止翻页时将两代数据拼接，或旧请求覆盖新筛选。
let browserTime = 0;
const elements = new Map();
const pageContext = {
  window: {clearTimeout() {}, setTimeout() {}}, URLSearchParams,
  Date: class extends Date { static now() { return browserTime; } },
  document: {
    querySelector: () => ({dataset: {catalogUrl: '/catalog'}}),
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {value: id === 'gear-source-filter' ? 'all' : '', querySelectorAll: () => []});
      return elements.get(id);
    },
  },
};
vm.runInNewContext(fs.readFileSync(new URL('../../../static/portal/js/gear-catalog.js', import.meta.url), 'utf8'), pageContext);
const builder = fs.readFileSync(new URL('../../../static/portal/js/gear_builder.js', import.meta.url), 'utf8');
vm.runInNewContext(builder.replace('  initialize();', `
  renderCandidates = renderDetail = renderCatalogStatus = toast = () => {};
  refreshCachedEquipmentStats = () => false;
  bootstrap = {};
  globalThis.qa = {loadCandidates, setRequest(value) { requestJson = value; },
    get items() { return candidates; }, get page() { return candidatePage; },
    setSlot(value) { state.selectedSlot = value; }, get slot() { return candidateSlot; }};
`), pageContext);
const rows = Array.from({length: 65}, (_, i) => ({item_id: i, name: `装备${String(i).padStart(2,'0')}`,
  variants: [{id: i, item_level: 700, _filter: {sources: ['raid'], stats: []}}]}));
let generation = 'one';
pageContext.qa.setRequest(async () => ({items: rows, snapshot: {state: 'ready', generation}}));
await pageContext.qa.loadCandidates();
assert.equal(pageContext.qa.items.length, 60);
await pageContext.qa.loadCandidates(false);
assert.equal(pageContext.qa.items.length, 65, '同代分页保持完整结果');
browserTime = 61000;
generation = 'two';
await pageContext.qa.loadCandidates(false);
assert.equal(pageContext.qa.items.length, 60, '换代后从第一页重新开始');
assert.equal(pageContext.qa.page, 1);
let oldRequest;
pageContext.qa.setSlot('chest');
pageContext.qa.setRequest(() => new Promise(done => { oldRequest = done; }));
const oldLoading = pageContext.qa.loadCandidates();
await Promise.resolve();
pageContext.qa.setSlot('head');
await pageContext.qa.loadCandidates();
oldRequest({items: [], snapshot: {state: 'ready', generation: 'old'}});
await oldLoading;
assert.equal(pageContext.qa.slot, 'head');
assert.equal(pageContext.qa.items.length, 60, '迟到的旧槽位请求不能覆盖新槽位');
console.log('装备目录页面：换代分页与迟到请求隔离通过。');
