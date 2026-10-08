// 执行真实页面加载函数，验证同槽位换装复用、迟到隔离和准备状态恢复。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const elements = new Map();
let timer;
const context = {
  window: {clearTimeout() { timer = null; }, setTimeout(fn) { timer = fn; return 1; }}, URLSearchParams,
  document: {querySelector: () => ({dataset: {enhancementsUrl: '/enhancements'}}),
    getElementById(id) { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); }},
};
vm.runInNewContext(fs.readFileSync(new URL('../../../static/portal/js/gear-catalog.js', import.meta.url), 'utf8'), context);
const source = fs.readFileSync(new URL('../../../static/portal/js/gear_builder.js', import.meta.url), 'utf8');
vm.runInNewContext(source.replace('  initialize();', `
  refreshCachedEnhancementText = () => false;
  sortedGems = rows => rows;
  socketRule = () => null;
  socketCapacity = () => 0;
  syncSlotLocks = () => {};
  renderOptionGroup = (el, rows, kind, message) => { el.textContent = rows.length ? rows.map(row => row.name).join(',') : message; };
  state.mode = 'enhancement';
  globalThis.qa = {loadEnhancements, setRequest(fn) { requestJson = fn; },
    setIdentity(cls, spec) { state.className = cls; state.specName = spec; },
    setVariant(id) { state.equipment[state.selectedSlot] = {variant: {id, type: 'crafted_equipment'}}; },
    get groups() { return enhancementGroups; }};
`), context);
const payload = name => ({snapshot: {state: 'ready'}, groups: {gems: [], enchants: [], embellishments: []},
  embellishment_options: {a: {name: name + '甲'}, b: {name: name + '乙'}}, embellishments_by_equipment: {'1': ['a'], '2': ['b']}});
let calls = 0;
context.qa.setRequest(async url => { calls++; assert.match(url, /snapshot=1/); assert.doesNotMatch(url, /variant_id/); return payload('战士'); });
context.qa.setVariant(1);
await context.qa.loadEnhancements();
assert.equal(context.qa.groups.embellishments[0].name, '战士甲');
context.qa.setVariant(2);
await context.qa.loadEnhancements();
assert.equal(context.qa.groups.embellishments[0].name, '战士乙');
assert.equal(calls, 1, '同槽位换装备不得重新请求');
let oldRequest;
context.qa.setIdentity('Paladin', 'Holy');
context.qa.setRequest(() => new Promise(done => { oldRequest = done; }));
const old = context.qa.loadEnhancements();
await Promise.resolve();
context.qa.setIdentity('Mage', 'Frost');
context.qa.setRequest(async () => payload('法师'));
await context.qa.loadEnhancements();
oldRequest(payload('圣骑士'));
await old;
assert.equal(context.qa.groups.embellishments[0].name, '法师乙', '旧职业请求不得覆盖新选择');
context.qa.setIdentity('Warrior', 'Arms');
let attempts = 0;
context.qa.setRequest(async () => ++attempts === 1 ? {snapshot: {state: 'building'}} : payload('武器'));
await context.qa.loadEnhancements();
assert.match(elements.get('gear-gem-list').textContent, /准备/);
assert.equal(typeof timer, 'function');
await context.qa.loadEnhancements(true);
assert.equal(context.qa.groups.embellishments[0].name, '武器乙');
assert.equal(attempts, 2, '准备状态不能永久缓存');
console.log('增强目录：换装零新增请求、迟到请求隔离、准备状态恢复通过。');
