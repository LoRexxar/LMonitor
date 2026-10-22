import harness from '../vm_harness.js';
const {gearRenderer} = harness;
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// 执行真实状态变更函数，仅替换显示、存储和网络边界。
const source = fs.readFileSync(new URL('../../../static/portal/js/gear_builder.js', import.meta.url), 'utf8');
const service = fs.readFileSync(new URL('../../services/gear_builder.py', import.meta.url), 'utf8');
const slotKeys = [...service.match(/EQUIPMENT_SLOTS = \(([\s\S]*?)\n\)/)[1].matchAll(/\('([^']+)',/g)].map(match => match[1]);
const element = {querySelector: () => null, querySelectorAll: () => []};
const context = {
    slotKeys,
    structuredClone,
    atob, TextDecoder,
    window: {confirm(message) { context.confirmMessage = message; return false; },
        WowItemTooltip: {renderContent(_container, content) { context.tooltipContent = content; }}},
    document: {querySelector: () => ({dataset: {}}), getElementById: () => element},
};
const marker = '  initialize();';
assert.ok(source.includes(marker), '必须找到配装器初始化入口');
vm.createContext(context);
vm.runInContext(fs.readFileSync(new URL('../../../static/shared/js/wow-gear-sources.js', import.meta.url), 'utf8'), context);
vm.runInNewContext(source.replace(marker, `
  const renderCachedDetail = renderDetail;
  persist = renderAll = renderDetail = openDetail = loadEnhancements = toast = () => {};
  bootstrap = {slots: slotKeys.map(key => ({key})), rules: {}};
  globalThis.qa = {
    get state() { return state; },
    setState(value) { state = normalizeState(value); },
    setRequest(value) { requestJson = value; },
    setCandidates(value) { candidates = value; },
    normalizeState, toggleSlotLock, addItem, applyEnhancement, setGemCount, optionRow, switchVariant,
    variantLabel, simcEquipmentLine, loadSavedLoadout, loadOnlineLoadout,
    changeCraftedStat, removeEnhancement, compactShareState, hydrateSharePayload,
    resolveCraftedEntry, replaceLoadout, totalsAndEffects, refreshCachedEquipmentStats, renderCachedDetail, refreshCachedEnhancementText,
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
api.setRequest(async () => { throw new Error('取消载入不应发送请求'); });
for (const load of [api.loadSavedLoadout, api.loadOnlineLoadout]) {
    context.confirmMessage = '';
    await load({id: 1, name: '另一方案', code: 'j' + Buffer.from(JSON.stringify({equipment: {}})).toString('base64url')});
    assert.match(context.confirmMessage, /未保存的修改会丢失/, '载入前说明覆盖损失');
    assert.deepEqual(plain(api.state), locked, '取消后装备与锁定均不变');
}
const newPlan = api.normalizeState({lockedSlots: ['neck'], equipment: {neck: entry()}});
api.replaceLoadout(newPlan);
assert.deepEqual(plain(api.state), plain(newPlan), '载入新方案不校验旧方案锁定，锁定随新方案载入');
api.setState(locked);

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

const staleDescription = '升级：勇士 6/6\n腕部 板甲\n静态属性说明：183护甲\n+83 [力量 or 智力]\n耐久: 50\n50 需要等级 90';
api.state.equipment.head.item.description = staleDescription;
api.state.equipment.head.item.text_schema_version = 2;
api.state.equipment.head.variant.text_schema_version = 2;
const previousStats = plain(api.state.equipment.head.variant.stats);
const previousGems = plain(api.state.equipment.head.gems);
const cleanVariant = {...freshVariant, text_schema_version: 2, effects: [], tooltip: '当前装等属性提示'};
assert.equal(api.refreshCachedEquipmentStats([{...item, description: '', text_schema_version: 2, variants: [cleanVariant]}]), true,
    '已标记为规范化的旧配装也必须清除脏描述');
assert.equal(api.state.equipment.head.item.description, '');
assert.deepEqual(plain(api.state.equipment.head.variant.stats), previousStats);
assert.deepEqual(plain(api.state.equipment.head.gems), previousGems);
assert.deepEqual(plain(api.state.lockedSlots), ['head']);
api.renderCachedDetail();
assert.match(element.innerHTML, /装备描述/, '没有独立风味文字的装备也显示完整 Tooltip');
assert.equal(context.tooltipContent.description, cleanVariant.tooltip, '右侧描述使用变体 Tooltip，不能使用旧英文说明');
assert.doesNotMatch(context.tooltipContent.description, /静态属性说明/, '不重复旧属性说明');
assert.match(element.innerHTML, /力量<\/span><span>189<\/span>/, '描述清理不影响属性区');

console.log('配装回归验证通过：锁定限制、属性拆分与旧配装基础属性补齐。');
api.setState({lockedSlots: ['head'], equipment: {head: {...entry(), gems: [{
    item: {item_id: 3, name: '旧宝石', description: '旧的整段提示'},
    variant: {id: 3, stats: {haste: 17}, effects: [{description_zh: '+17 急速'}]},
}]}}});
assert.equal(api.refreshCachedEnhancementText({gems: [{item_id: 3, description: '', text_schema_version: 2,
    variants: [{id: 3, effects: [], tooltip: '+17 急速', text_schema_version: 2}]}]}), true);
assert.deepEqual(plain(api.state.equipment.head.gems[0].variant.stats), {haste: 17});
assert.deepEqual(plain(api.state.equipment.head.gems[0].variant.effects), []);
assert.equal(api.state.equipment.head.gems[0].item.description, '');
assert.deepEqual(plain(api.state.lockedSlots), ['head'], '强化文本迁移保留装备锁定');

// 晋升毒液石切换必须使用新数值，保存和导出都引用真实八阶变体。
const venomItem = {item_id: 280799, name: '露瑟拉玛，圣光之裁', name_en: 'Luselama'};
const sixth = {id: 100, key: 'myth-6', type: 'drop_equipment', item_level: 334, track: 'myth', track_label: '神话',
    track_rank: 6, track_max_rank: 6, socket_count: 1, stats: {strength: 180}, effects: [{description_zh: '旧效果'}], bonus_ids: [12854]};
const eighth = {...sixth, id: 101, key: 'myth-6-venomstone', item_level: 340, track_rank: 8,
    stats: {strength: 199}, effects: [{description_zh: '装备：获得774急速。'}], bonus_ids: [12856], metadata: {venomstone: {item_id: 280562}}};
api.setState({className: 'Warrior', specName: 'Fury', selectedSlot: 'main_hand', equipment: {
    main_hand: {item: venomItem, variant: sixth, resolvedStats: {strength: 180}, resolvedEffects: sixth.effects,
        gems: [{item: {item_id: 12345}}], enchant: {item: {enchantment_id: 777}}, selectedStats: []},
}});
api.setCandidates([{...venomItem, variants: [eighth, sixth]}]);
await api.switchVariant(101);
const upgraded = api.state.equipment.main_hand;
assert.equal(upgraded.resolvedStats, null, '八阶不能沿用旧属性缓存');
assert.equal(upgraded.resolvedEffects, null, '八阶不能沿用旧特效缓存');
assert.equal(upgraded.variant.stats.strength, 199);
assert.equal(upgraded.gems[0].item.item_id, 12345, '品级切换保留宝石');
assert.match(api.variantLabel(upgraded.variant), /神话 8\/6 · 340 · 晋升毒液石/);
assert.match(api.simcEquipmentLine('main_hand', upgraded), /ilevel=340,bonus_id=12856,gem_id=12345,enchant_id=777/);
assert.match(JSON.stringify(api.compactShareState(api.state)), /myth-6-venomstone/, '分享保存真实八阶引用');
await api.switchVariant(100);
assert.equal(api.state.equipment.main_hand.variant.item_level, 334);
assert.equal(api.state.equipment.main_hand.variant.stats.strength, 180);

// 后端修正原档位后，已保存配装也必须更新同一变体的特效缓存。
api.state.equipment.main_hand.variant.effects = [{description_zh: '装备：获得1127急速。'}];
api.state.equipment.main_hand.resolvedEffects = [{description_zh: '装备：获得1127急速。'}];
const correctedSixth = {...sixth, text_schema_version: 2, effects: [{description_zh: '装备：获得756急速。'}]};
assert.equal(api.refreshCachedEquipmentStats([{...venomItem, text_schema_version: 2, variants: [correctedSixth]}]), true);
assert.equal(api.state.equipment.main_hand.variant.id, 100, '修正数值不更换已选装备');
assert.equal(api.state.equipment.main_hand.variant.item_level, 334);
assert.match(api.state.equipment.main_hand.variant.effects[0].description_zh, /756急速/);
assert.match(api.state.equipment.main_hand.resolvedEffects[0].description_zh, /756急速/);

// 双孔项链可镶嵌相同宝石，数量在属性、保存和导出中都必须保留。
const neckItem = {item_id: 268265, name: '亚基克星藏骨匣'};
const neckVariant = {id: 46820, key: 'neck-myth', type: 'drop_equipment', socket_count: 2, stats: {haste: 100}};
const repeatGem = {item_id: 20001, name: '测试急速宝石'};
const repeatVariant = {id: 20001, key: 'gem-repeat', type: 'gem', stats: {haste: 17}};
const otherGem = {item_id: 20002, name: '测试精通宝石'};
const otherVariant = {id: 20002, key: 'gem-other', type: 'gem', stats: {mastery: 17}};
api.setState({selectedSlot: 'neck', equipment: {neck: {item: neckItem, variant: neckVariant, gems: []}}});
assert.doesNotMatch(api.optionRow({...repeatGem, variants: [repeatVariant]}, 'gem'), /gear-gem-count/, '未勾选宝石保持原复选框布局');
api.setGemCount(repeatGem, repeatVariant, 2);
assert.deepEqual(plain(api.state.equipment.neck.gems.map(gem => gem.item.item_id)), [20001, 20001]);
assert.equal(api.totalsAndEffects().totals.haste, 134, '两颗相同宝石的属性分别累加');
assert.match(api.optionRow({...repeatGem, variants: [repeatVariant]}, 'gem'), /value="2" selected/, '数量控件回显两颗');
assert.match(api.optionRow({...repeatGem, variants: [repeatVariant]}, 'gem'), /type="checkbox"[^>]* checked/, '双槽也保留已勾选的复选框');
for (const kind of ['enchant', 'embellishment']) {
  assert.doesNotMatch(api.optionRow({...repeatGem, variants: [repeatVariant]}, kind), /gear-gem-count|gear-option-row--quantity/, '双槽装备的附魔与美化不使用数量样式');
}
assert.match(api.simcEquipmentLine('neck', api.state.equipment.neck), /gem_id=20001\/20001/, '导出保留重复宝石');
const repeatedShare = api.compactShareState(api.state);
assert.deepEqual(plain(repeatedShare.e[0][5]), [[20001, 'gem-repeat'], [20001, 'gem-repeat']], '分享引用不去重');
api.setState(plain(api.state));
assert.equal(api.state.equipment.neck.gems.length, 2, '保存后恢复仍保留两颗');
api.setGemCount(otherGem, otherVariant, 1);
assert.equal(api.state.equipment.neck.gems.length, 2, '满槽时不能挤掉其他宝石或超额镶嵌');
api.setGemCount(repeatGem, repeatVariant, 1);
api.setGemCount(otherGem, otherVariant, 1);
api.setGemCount(repeatGem, repeatVariant, 2);
assert.deepEqual(plain(api.state.equipment.neck.gems.map(gem => gem.item.item_id)), [20001, 20002], '增加数量不得覆盖另一颗宝石');
api.setGemCount(repeatGem, repeatVariant, 0);
assert.deepEqual(plain(api.state.equipment.neck.gems.map(gem => gem.item.item_id)), [20002], '清除只移除所选宝石');
api.toggleSlotLock('neck');
api.setGemCount(otherGem, otherVariant, 0);
assert.equal(api.state.equipment.neck.gems.length, 1, '锁定阻止数量变更');
api.toggleSlotLock('neck');
api.setGemCount(otherGem, otherVariant, 0);
const uniqueVariant = {...repeatVariant, unique_group: '唯一宝石测试', max_equipped: 1};
api.setGemCount(repeatGem, uniqueVariant, 2);
assert.equal(api.state.equipment.neck.gems.length, 0, '装备唯一宝石不能重复镶嵌');
api.state.equipment.head = {item, variant, gems: [{item: otherGem, variant: {...otherVariant, unique_group: '唯一宝石测试'}}]};
api.setGemCount(repeatGem, uniqueVariant, 1);
assert.equal(api.state.equipment.neck.gems.length, 0, '唯一分组限制包含其他装备上的宝石');
api.state.equipment.neck.variant.socket_count = 1;
assert.match(api.optionRow({...otherGem, variants: [otherVariant]}, 'gem'), /type="checkbox"/, '单孔装备保留复选操作');
api.state.equipment.neck.gems = [{item: otherGem, variant: otherVariant}];
for (const capacity of [0, 1, 3]) {
  api.state.equipment.neck.variant.socket_count = capacity;
  assert.doesNotMatch(api.optionRow({...otherGem, variants: [otherVariant]}, 'gem'), /gear-gem-count|gear-option-row--quantity/, '非双槽装备即使已选宝石也保持原样式');
}
console.log('重复宝石数量、插槽上限、单颗移除、锁定、属性及分享导出验证通过。');

// Cache correction is independent of lock mutation scenarios above.

await (async function neckSocketCacheCorrection() {
const context = gearRenderer('globalThis.qa = {refreshCachedEquipmentStats, setState: (value) => {state = value;}};');
const variant = {id: 1, item_level: 340, socket_count: 2, socket_types: ['prismatic', 'prismatic'], text_schema_version: 2, stats: {haste: 100}, effects: [], tooltip: ''};
const entry = {item: {item_id: 240950, text_schema_version: 2, description: ''}, variant, gems: [{id: 11}, {id: 12}], selectedStats: ['mastery', 'versatility'], resolvedStats: {mastery: 50, versatility: 50}};
context.qa.setState({className: 'Warrior', specName: 'Fury', equipment: {neck: entry}, lockedSlots: ['neck']});
const fresh = {...variant, socket_count: 1, socket_types: ['prismatic']};
assert.equal(context.qa.refreshCachedEquipmentStats([{...entry.item, variants: [fresh]}]), true);
assert.equal(entry.variant.socket_count, 1);
assert.deepEqual(entry.variant.socket_types, ['prismatic']);
assert.deepEqual(entry.gems, [{id: 11}], '移除超过真实孔数的宝石');
assert.deepEqual(entry.selectedStats, ['mastery', 'versatility']);
assert.deepEqual(entry.resolvedStats, {mastery: 50, versatility: 50});
assert.equal(context.qa.refreshCachedEquipmentStats([{...entry.item, variants: [fresh]}]), false, '再次刷新幂等');
entry.item.item_id = 268265;
entry.variant = {...variant};
entry.gems = [{id: 11}, {id: 12}];
assert.equal(context.qa.refreshCachedEquipmentStats([{...entry.item, variants: [{...variant}]}]), false);
assert.equal(entry.gems.length, 2, '特殊项链的双孔不变');
console.log('项链缓存孔数修正、自选绿字保留、超额宝石清理与特殊双孔保持：通过');

})();

// The actual clear-button binding must override locks only after confirmation.
{
    const elements = new Map(), storage = new Map([['wowdaily:gear-builder:loadouts:v1', 'keep']]);
    const effects = [], prompts = [];
    let confirmed = false;
    const getElement = id => {
        if (!elements.has(id)) elements.set(id, {listeners: {},
            addEventListener(name, callback) { this.listeners[name] = callback; }});
        return elements.get(id);
    };
    const clearContext = gearRenderer(`
        bootstrap = {slots: slotKeys.map(key => ({key})), catalog: {batch_key: 'test'}};
        renderAll = () => effects.push('render');
        loadCandidates = reset => effects.push(['catalog', reset]);
        toast = message => effects.push(['toast', message]);
        requestJson = () => { throw new Error('clear must not write remote collections'); };
        savedLoadouts = [{id: 'saved-local'}];
        onlineLoadouts = [{id: 10}];
        ownedItems = [{id: 20}];
        bindEvents();
        globalThis.qa = {
            setState(value) { state = normalizeState(value); },
            get state() { return state; },
            collections() { return {savedLoadouts, onlineLoadouts, ownedItems}; },
            loadStored, storageKey,
        };
    `, {slotKeys, effects, structuredClone,
        window: {confirm(message) { prompts.push(message); return confirmed; }},
        document: {querySelector: () => ({dataset: {}}), getElementById: getElement,
            querySelectorAll: () => [], addEventListener() {}},
        localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)},
    });
    const qa = clearContext.qa;
    qa.setState({className: 'Warrior', specName: 'Fury', lockedSlots: ['head', 'off_hand'],
        snapshotSlots: ['head'], equipment: {head: entry(), neck: entry()}});
    const before = plain(qa.state), collections = plain(qa.collections());
    const click = getElement('gear-clear').listeners.click;
    assert.equal(typeof click, 'function', 'real clear button is wired');
    click();
    assert.deepEqual(plain(qa.state), before, 'cancel preserves equipment, enhancements and locks');
    assert.deepEqual([...storage], [['wowdaily:gear-builder:loadouts:v1', 'keep']], 'cancel performs no persistence');
    assert.deepEqual(effects, [], 'cancel does not render or reload');
    confirmed = true;
    click();
    assert.deepEqual(plain(qa.state.equipment), {}, 'confirmed clear removes locked and unlocked equipment');
    assert.deepEqual(plain(qa.state.lockedSlots), [], 'full clear also removes occupied and empty slot locks');
    assert.deepEqual(plain(qa.state.snapshotSlots), [], 'removed equipment has no retained snapshot slots');
    assert.match(prompts.at(-1), /全部配装.*锁定/);
    assert.match(prompts.at(-1), /已保存方案.*备选装备.*不受影响/);
    assert.deepEqual(plain(qa.collections()), collections, 'saved plans and owned equipment are untouched');
    assert.equal(storage.get('wowdaily:gear-builder:loadouts:v1'), 'keep');
    assert.deepEqual(plain(qa.loadStored('Warrior', 'Fury')), plain(qa.state), 'empty draft survives reload');
    assert.deepEqual(plain(effects), ['render', ['catalog', true], ['toast', '当前配装已全部清空。']]);
}
