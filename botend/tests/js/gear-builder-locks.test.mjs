import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// 执行真实状态变更函数，仅替换显示、存储和网络边界。
const source = fs.readFileSync(new URL('../../../static/portal/js/gear_builder.js', import.meta.url), 'utf8');
const element = {querySelector: () => null, querySelectorAll: () => []};
const context = {
    structuredClone,
    document: {querySelector: () => ({dataset: {}}), getElementById: () => element},
};
const marker = '  initialize();';
assert.ok(source.includes(marker), '必须找到配装器初始化入口');
vm.runInNewContext(source.replace(marker, `
  const renderCachedDetail = renderDetail;
  persist = renderAll = renderDetail = openDetail = loadEnhancements = toast = () => {};
  bootstrap = {slots: [], rules: {}};
  globalThis.qa = {
    get state() { return state; },
    setState(value) { state = normalizeState(value); },
    setRequest(value) { requestJson = value; },
    setCandidates(value) { candidates = value; },
    normalizeState, toggleSlotLock, addItem, applyEnhancement, switchVariant,
    changeCraftedStat, removeEnhancement, compactShareState, hydrateSharePayload,
    resolveCraftedEntry, replaceLoadout, totalsAndEffects, refreshCachedEquipmentStats, renderCachedDetail,
  };
`), context);
const api = context.qa;
const plain = value => JSON.parse(JSON.stringify(value));
const item = {item_id: 1, name: '原装备'};
const variant = {id: 1, key: '原变体', type: 'drop_equipment', stats: {strength: 100}, metadata: {}, socket_count: 1};
const entry = () => ({item, variant, gems: [{item: {item_id: 3}, variant: {id: 3, key: '宝石'}}], selectedStats: [], addedSocket: false});

assert.deepEqual(plain(api.normalizeState({lockedSlots: ['head', 'head', '未知部位']}).lockedSlots), ['head']);
assert.deepEqual(plain(api.normalizeState({}).lockedSlots), [], '旧配装默认不锁定');

api.setState({lockedSlots: ['head'], equipment: {}});
await api.addItem(item, variant);
assert.deepEqual(plain(api.state.equipment), {}, '空部位锁定后不能放入装备');

api.setState({lockedSlots: ['head'], equipment: {head: entry()}});
api.setCandidates([{...item, variants: [variant, {...variant, id: 2}]}]);
const locked = plain(api.state);
await api.addItem({...item, item_id: 2}, {...variant, id: 2});
await api.applyEnhancement('gem', item, variant);
await api.switchVariant(2);
await api.changeCraftedStat(0, 'crit');
api.removeEnhancement('gem:0');
assert.deepEqual(plain(api.state), locked, '锁定必须阻止装备、品级、制造绿字和强化变更');
assert.throws(() => api.replaceLoadout(api.normalizeState({})), /解锁/, '载入其他配装不能绕过锁定');

api.toggleSlotLock('head');
await api.addItem({...item, item_id: 2}, {...variant, id: 2});
assert.equal(api.state.equipment.head.item.item_id, 2, '解锁后可正常换装');

api.setState({className: 'Warrior', specName: 'Arms', selectedSlot: 'main_hand', lockedSlots: ['off_hand'], equipment: {off_hand: entry()}});
const twoHanded = {...variant, metadata: {two_handed: true}};
await api.addItem(item, twoHanded);
assert.ok(api.state.equipment.off_hand, '双手武器不能移除锁定的副手');
assert.equal(api.state.equipment.main_hand, undefined);
api.toggleSlotLock('off_hand');
await api.addItem(item, twoHanded);
assert.equal(api.state.equipment.off_hand, undefined, '副手解锁后可正常装备双手武器');

api.setState({lockedSlots: ['head', 'neck'], equipment: {head: entry()}});
const share = api.compactShareState(api.state);
api.setRequest(async () => ({equipment: {head: entry()}}));
const restored = await api.hydrateSharePayload(share);
assert.deepEqual(plain(restored.lockedSlots), ['head', 'neck'], '保存和分享须保留已装备及空部位锁定');
const legacy = await api.hydrateSharePayload({...share, l: undefined});
assert.deepEqual(plain(legacy.lockedSlots), [], '旧分享格式保持兼容');

api.setState({equipment: {head: {...entry(), variant: {...variant, type: 'crafted_equipment'}, selectedStats: ['crit', 'haste'], resolvedStats: null}}});
let finishRequest;
api.setRequest(() => new Promise(resolve => { finishRequest = resolve; }));
const pending = api.resolveCraftedEntry(api.state.equipment.head);
api.toggleSlotLock('head');
const snapshot = plain(api.state.equipment.head);
finishRequest({resolved_stats: {strength: 999}, effects: []});
await pending;
assert.deepEqual(plain(api.state.equipment.head), snapshot, '锁定前发出的异步响应不能改写锁定快照');

api.setState({lockedSlots: ['head', 'shoulders'], equipment: {
    head: {...entry(), resolvedStats: {strength: 150, crit: 30},
        gems: [{variant: {stats: {crit: 7}}}],
        enchant: {variant: {stats: {crit: 11}}},
        embellishment: {variant: {stats: {strength: 5}}}},
    neck: {...entry(), variant: {...variant, stats: {strength: 50, haste: 20}}, gems: []},
}});
let split = api.totalsAndEffects();
assert.deepEqual(plain(split.lockedTotals), {strength: 155, crit: 48}, '锁定汇总包含制造解析属性、宝石、附魔和美化');
assert.deepEqual(plain(split.unlockedTotals), {strength: 50, haste: 20}, '未锁定部位单独汇总');
const beforeUnlock = plain(split.totals);
assert.deepEqual(beforeUnlock, {strength: 205, crit: 48, haste: 20});
api.toggleSlotLock('head');
split = api.totalsAndEffects();
assert.deepEqual(plain(split.lockedTotals), {}, '锁定空部位不贡献属性');
assert.deepEqual(plain(split.unlockedTotals), beforeUnlock, '解锁后属性从锁定转入未锁定');
assert.deepEqual(plain(split.totals), beforeUnlock, '锁定状态不改变当前属性');
api.toggleSlotLock('head');
api.toggleSlotLock('neck');
split = api.totalsAndEffects();
assert.deepEqual(plain(split.lockedTotals), beforeUnlock, '全部锁定时所有属性归入锁定');
assert.deepEqual(plain(split.unlockedTotals), {});
api.setState({});
assert.deepEqual(plain(api.totalsAndEffects().totals), {}, '空配装属性为零');

const staleEntry = {...entry(), variant: {...variant, item_level: 334, stats: {crit: 61, haste: 140}},
    resolvedStats: {crit: 80, haste: 121}};
api.setState({lockedSlots: ['head'], equipment: {head: staleEntry}});
const freshVariant = {...variant, item_level: 334,
    stats: {strength: 189, stamina: 3910, armor: 326, crit: 61, haste: 140}, tooltip: '完整属性提示'};
assert.equal(api.refreshCachedEquipmentStats([{...item, variants: [{...freshVariant, id: 9}]}]), false, '不能用其他变体补属性');
assert.equal(api.refreshCachedEquipmentStats([{...item, variants: [{...freshVariant, item_level: 331}]}]), false, '不能用其他装等补属性');
assert.equal(api.refreshCachedEquipmentStats([{...item, variants: [freshVariant]}]), true);
assert.deepEqual(plain(api.state.equipment.head.variant.stats), freshVariant.stats, '旧配装补齐主属性、耐力和护甲');
assert.deepEqual(plain(api.state.equipment.head.resolvedStats), {crit: 80, haste: 121, strength: 189, stamina: 3910, armor: 326}, '保留制造绿字并补齐基础属性');
assert.deepEqual(plain(api.state.lockedSlots), ['head'], '数据补齐保留部位锁定');
assert.deepEqual(plain(api.state.equipment.head.gems), plain(staleEntry.gems), '数据补齐保留强化');
assert.equal(api.refreshCachedEquipmentStats([{...item, variants: [freshVariant]}]), false, '完整配装无需重复补齐');
assert.equal(api.totalsAndEffects().lockedTotals.strength, 189, '补齐后锁定属性汇总同步更新');
api.renderCachedDetail();
assert.match(element.innerHTML, /力量<\/span><span>189<\/span>/, '右侧详情显示主属性');
assert.match(element.innerHTML, /耐力<\/span><span>3,910<\/span>/, '右侧详情显示耐力');
assert.match(element.innerHTML, /护甲<\/span><span>326<\/span>/, '右侧详情显示护甲');

console.log('配装回归验证通过：锁定限制、属性拆分与旧配装基础属性补齐。');
