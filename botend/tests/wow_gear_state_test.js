const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../static/shared/js/wow-gear-state.js'), 'utf8'), context);

test('刷新全部槽位的已有数值，保留选择和外部装备', async () => {
  const gear = {item: {item_id: 123}, variant: {id: 1, key: 'hero-1', stats: {strength: 10}},
    selectedStats: ['crit', 'haste'], gems: [], addedSocket: true};
  const state = {className: 'Warrior', specName: 'Fury', batchKey: 'stable', lockedSlots: ['head'],
    equipment: {head: gear, feet: {external: true, item: {item_id: 999}}}};
  let request;
  const next = await context.WowGearState.refresh(state, async (url, options) => {
    request = JSON.parse(options.body);
    assert.equal(url, '/portal/api/gear-builder/resolve-share/');
    return {equipment: {head: {...gear, variant: {...gear.variant, stats: {strength: 20}}}}};
  });
  assert.equal(request.current, true);
  assert.equal(request.e.length, 1);
  assert.deepEqual(request.e[0][3], ['crit', 'haste']);
  assert.equal(next.equipment.head.variant.stats.strength, 20);
  assert.equal(state.equipment.head.variant.stats.strength, 10);
  assert.equal(next.equipment.feet, state.equipment.feet);
  assert.equal(next.lockedSlots, state.lockedSlots);
});

test('缺失引用和刷新失败不会清空已有装备', async () => {
  const state = {batchKey: 'stable', equipment: {head: {external: true}}};
  const next = await context.WowGearState.refresh(state, () => { throw Error('不应请求'); });
  assert.equal(next, state);
});

test('没有旧批次号也能按 ID 恢复当前数据', async () => {
  const state = {className: 'Warrior', specName: 'Fury', equipment: {
    head: {item: {item_id: 123}, variant: {key: 'hero-1'}}}};
  const next = await context.WowGearState.refresh(state, async (url, options) => {
    assert.equal(JSON.parse(options.body).b, '');
    return {batch_key: 'current', equipment: {}};
  });
  assert.equal(next.batchKey, 'current');
  assert.equal(next.equipment.head, state.equipment.head);
});
