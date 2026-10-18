import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source = fs.readFileSync(new URL('../../../static/portal/js/gear_builder.js', import.meta.url), 'utf8');
const service = fs.readFileSync(new URL('../../services/gear_builder.py', import.meta.url), 'utf8');
// The real service owns the slot inventory; aliases model its SimC boundary contract.
const slotBlock = service.match(/EQUIPMENT_SLOTS = \(([\s\S]*?)\n\)/)[1];
const aliases = {shoulder: 'shoulders', wrist: 'wrists'};
const slots = [...slotBlock.matchAll(/\('([^']+)', '([^']+)'\)/g)].map(([, key, label]) => ({
  key, label, family: key, simc_key: Object.keys(aliases).find(alias => aliases[alias] === key) || key,
}));
const metadata = {slots, slot_aliases: aliases, classes: [], rules: {socket_additions: [{slot: 'wrists'}]}, catalog: {}};
const plain = value => JSON.parse(JSON.stringify(value));
const entry = id => ({item: {item_id: id, name: `物品${id}`},
  variant: {id, key: `variant-${id}`, item_level: 334, stats: {crit: 100}, effects: [], bonus_ids: [123], crafting_quality: 5},
  selectedStats: ['crit', 'haste'], gems: [{item: {item_id: 501}, variant: {id: 501, key: 'gem', stats: {crit: 1}}}],
  enchant: {item: {item_id: 502, enchantment_id: 777}, variant: {id: 502, key: 'enchant'}},
  embellishment: {item: {item_id: 503}, variant: {id: 503, key: 'embellishment', bonus_ids: [456]}},
  addedSocket: true, resolvedStats: {crit: 200}, resolvedEffects: [{description: '制造效果'}]});
function fixture() {
  const nodes = new Map();
  const store = new Map();
  const page = {dataset: {bootstrapUrl: '/bootstrap', simcUrl: '/import', shareUrl: '/share'}, setAttribute() {}};
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value: '', style: {}, classList: {add() {}, toggle() {}},
      querySelector: () => null, querySelectorAll: () => [], setAttribute() {}, removeAttribute() {}, close() {}, focus() {}});
    return nodes.get(id);
  };
  const context = {structuredClone, URLSearchParams, TextEncoder, TextDecoder, atob, btoa,
    location: {search: ''}, window: {},
    localStorage: {getItem: key => store.get(key) || null, setItem: (key, value) => store.set(key, value)},
    document: {querySelector: selector => selector === '.gear-builder-page' ? page : null,
      getElementById: node, addEventListener() {}},
  };
  vm.createContext(context);
  for (const name of ['wow-gear-sources', 'wow-gear-state']) {
    vm.runInContext(fs.readFileSync(new URL(`../../../static/shared/js/${name}.js`, import.meta.url), 'utf8'), context);
  }
  vm.runInContext(source.replace('  initialize();', `
    syncSelectors = bindEvents = renderCatalogStatus = loadActiveBrowser = loadCandidates = () => {};
    renderAll = () => { renderSlots(); renderStats(); renderPreview(); };
    toast = () => {};
    els.online_list = null;
    globalThis.qa = {
      get state() { return state; },
      configure: value => { bootstrap = value; },
      setState: value => { state = normalizeState(value); },
      request: value => { requestJson = value; },
      initialize, normalizeState, hydrateSharePayload, compactShareState, buildSimcProfile, importSimc,
      persist, loadStored, equippedCount, renderAll, saveCurrentLoadout, readSavedLoadouts, toggleSlotLock,
    };
  `), context);
  const api = context.qa;
  api.configure(metadata);
  return {api, nodes, store, node};
}
const storageKey = 'wowdaily:gear-builder:v1:Warrior:Fury';

test('export → parser alias payload → import updates visible slots without 16→18 ghost entries', async () => {
  const {api, node} = fixture();
  const equipment = Object.fromEntries(slots.map((slot, index) => [slot.key, entry(index + 1)]));
  api.setState({equipment});
  api.persist();
  const exported = api.buildSimcProfile();
  const rows = exported.split('\n').filter(line => /^[a-z_0-9]+=.*\bid=/.test(line)).map(line => {
    const [key] = line.split('=');
    const slot = Object.keys(aliases).find(alias => aliases[alias] === key) || key;
    const original = equipment[aliases[slot] || slot];
    return {slot, item: {...original.item, name: `${slot}-imported`}, variant: original.variant,
      gems: original.gems, enchant: original.enchant, crafted_stats: original.selectedStats};
  });
  api.request(async () => ({identity: {class_name: 'Warrior', spec_name: 'Fury'}, equipment: rows}));
  node('gear-simc-input').value = exported;
  await api.importSimc();
  assert.equal(node('gear-simc-message').textContent, '');
  assert.equal(Object.keys(api.state.equipment).length, slots.length, 'aliases must not create two hidden extra entries');
  for (const key of ['shoulders', 'wrists']) {
    assert.match(api.state.equipment[key].item.name, /-imported$/, 'new imports replace canonical entries');
    assert.equal(api.state.equipment[key].gems[0].item.item_id, 501);
    assert.equal(api.state.equipment[key].enchant.item.enchantment_id, 777);
    assert.deepEqual(plain(api.state.equipment[key].selectedStats), ['crit', 'haste']);
  }
  assert.match(exported, /^shoulder=/m);
  assert.match(exported, /^wrist=/m);
  assert.doesNotMatch(exported, /^(shoulders|wrists)=/m);
  assert.equal(api.buildSimcProfile().split('\n').filter(line => /^[a-z_0-9]+=.*\bid=/.test(line)).length, slots.length);
  assert.equal(node('gear-equipped-count').textContent, `${slots.length}/${slots.length}`);
});

test('startup restores local and shared aliases, selection and locks without dropping pre-metadata state', async () => {
  for (const shared of [false, true]) {
    const {api, store, node} = fixture();
    const old = {selectedSlot: 'wrist', lockedSlots: ['shoulder', 'wrist'], equipment: {shoulder: entry(1), wrist: entry(2)}};
    api.configure(null);
    const pending = api.normalizeState(old);
    assert.deepEqual(plain(pending.lockedSlots), old.lockedSlots, 'metadata absence must not strip alias locks');
    api.configure(metadata);
    const restored = shared ? await api.hydrateSharePayload(old) : (store.set(storageKey, JSON.stringify(old)), api.loadStored('Warrior', 'Fury'));
    api.setState(restored);
    api.renderAll();
    assert.deepEqual(plain(api.state.equipment), {shoulders: entry(1), wrists: entry(2)});
    assert.equal(api.state.selectedSlot, 'wrists');
    assert.deepEqual(plain(api.state.lockedSlots), ['shoulders', 'wrists']);
    assert.match(node('gear-slot-list').innerHTML, /物品1/);
    assert.match(node('gear-slot-list').innerHTML, /物品2/);
    api.persist();
    api.request(async url => url === '/bootstrap' ? metadata : {equipment: {}});
    await api.initialize();
    assert.deepEqual(plain(api.state.equipment), {shoulders: entry(1), wrists: entry(2)}, 'real initialize / shared refresh retains complete entries');
    assert.equal(api.state.selectedSlot, 'wrists');
    assert.deepEqual(plain(api.state.lockedSlots), ['shoulders', 'wrists']);
  }
});

test('historical duplicate keys prefer canonical objects; imports obey migrated locks including empty slots', async () => {
  const {api, node} = fixture();
  api.setState({selectedSlot: 'shoulder', lockedSlots: ['shoulder', 'wrist'], equipment: {
    shoulder: entry(1), shoulders: entry(2), wrist: entry(3), wrists: null,
  }});
  assert.deepEqual(plain(api.state.equipment), {shoulders: entry(2), wrists: null});
  api.persist();
  api.request(async () => ({identity: {class_name: 'Warrior', spec_name: 'Fury'}, equipment: [
    {slot: 'shoulder', item: entry(99).item}, {slot: 'wrist', item: entry(100).item},
  ]}));
  node('gear-simc-input').value = 'warrior=test';
  await api.importSimc();
  assert.deepEqual(plain(api.state.equipment), {shoulders: entry(2), wrists: null}, 'alias imports may not bypass filled or empty locks');
  api.toggleSlotLock('wrists');
  await api.importSimc();
  assert.equal(api.state.equipment.wrists.item.item_id, 100, 'unlocked import replaces explicit canonical empty slot');
  assert.equal(api.state.equipment.shoulders.item.item_id, 2);
});

test('compact historical shares normalize request, hydrated keys and snapshot fallbacks', async () => {
  const {api} = fixture();
  const old = {v: 4, c: 'Warrior', s: 'Fury', u: ['wrist'], l: ['wrist', 'shoulder'],
    e: [['shoulder', 1, 'variant-1'], ['wrist', 0, 0, [], 0, [], 0, 1, entry(2)]]};
  api.request(async (_url, options) => {
    const sent = JSON.parse(options.body);
    assert.deepEqual(sent.e.map(row => row[0]), ['shoulders', 'wrists']);
    return {equipment: {shoulder: entry(1)}};
  });
  const restored = await api.hydrateSharePayload(old);
  assert.deepEqual(Object.keys(restored.equipment).sort(), ['shoulders', 'wrists']);
  assert.deepEqual(plain(restored.equipment.shoulders), entry(1));
  assert.equal(restored.equipment.wrists.item.item_id, 2);
  assert.equal(restored.equipment.wrists.embellishment.item.item_id, 503);
  assert.equal(restored.selectedSlot, 'wrists');
  assert.deepEqual(plain(restored.lockedSlots), ['wrists', 'shoulders']);
});

test('raw duplicate share slots are rejected before alias merging or snapshot filtering', async () => {
  for (const rows of [
    [['shoulder', 1, 'v1'], ['shoulder', 2, 'v2'], ['shoulders', 3, 'v3']],
    [['wrists', 1, 'v1'], ['wrists', 2, 'v2']],
    [['shirt', 0, 0, [], 0, [], 0, 0, entry(1)], ['shirt', 0, 0, [], 0, [], 0, 0, entry(2)]],
  ]) {
    const {api} = fixture(); let calls = 0;
    api.request(async () => {calls++; return {equipment: {}};});
    await assert.rejects(api.hydrateSharePayload({v: 4, c: 'Warrior', s: 'Fury', e: rows}), /重复装备槽位/);
    assert.equal(calls, 0, 'invalid original rows must never be sanitized into an accepted request');
  }
  const {api} = fixture();
  api.request(async (_url, options) => {
    assert.deepEqual(JSON.parse(options.body).e, [['shoulders', 2, 'v2']]);
    return {equipment: {shoulders: entry(2)}};
  });
  const restored = await api.hydrateSharePayload({v: 4, c: 'Warrior', s: 'Fury', e: [
    ['shoulder', 1, 'v1'], ['shoulders', 2, 'v2'],
  ]});
  assert.equal(restored.equipment.shoulders.item.item_id, 2, 'one legacy alias plus canonical remains compatible');
});

test('counts, previews and saved library follow actual slots, preserving unsupported data separately', async () => {
  const {api, node, store} = fixture();
  const dynamic = {...metadata, slots: [slots[0], slots[2], {key: 'future_slot', label: '未来槽位', simc_key: 'future_slot'}]};
  api.configure(dynamic);
  api.setState({equipment: {head: entry(1), shoulders: entry(2), shoulder: entry(9), future_slot: entry(3), shirt: entry(4)}});
  api.renderAll();
  assert.equal(api.equippedCount(), dynamic.slots.length);
  assert.equal(node('gear-equipped-count').textContent, '3/3');
  assert.equal(node('gear-mobile-count').textContent, '3/3');
  assert.equal(node('gear-preview-count').textContent, '3 / 3');
  assert.match(node('gear-stats-context').textContent, /已装备 3\/3/);
  assert.equal(node('gear-preview-progress-bar').style.width, '100%');
  const preview = node('gear-preview-left').innerHTML + node('gear-preview-right').innerHTML;
  assert.equal((preview.match(/data-preview-slot=/g) || []).length, dynamic.slots.length);
  assert.match(preview, /data-preview-slot="future_slot"/);
  assert.ok(api.state.equipment.shirt, 'do not silently delete unsupported true equipment slots');
  const saved = await api.saveCurrentLoadout('动态槽位');
  assert.equal(saved.record.equippedCount, 3);
  assert.match(node('gear-loadout-list').innerHTML, /3\/3/);
  const oldCode = 'j' + Buffer.from(JSON.stringify({equipment: {
    head: entry(1), shoulder: entry(2), shoulders: entry(3), shirt: entry(4),
  }})).toString('base64url');
  store.set('wowdaily:gear-builder:loadouts:v1', JSON.stringify([{id: 'old', code: oldCode, equippedCount: 18}]));
  const library = await api.readSavedLoadouts();
  assert.equal(library[0].equippedCount, 2, 'historical library count must derive from canonical keys, not old inflated count');
  const share = api.compactShareState(api.state);
  assert.ok(share.e.find(row => row[0] === 'shirt')[8], 'unknown entries retain snapshots across save/share');
  api.request(async (_url, options) => {
    assert.ok(!JSON.parse(options.body).e.some(row => row[0] === 'shirt'), 'unsupported snapshot is not sent to slot-restricted resolver');
    return {equipment: {}};
  });
  const restored = await api.hydrateSharePayload(share);
  assert.equal(restored.equipment.shirt.item.item_id, 4);
  api.persist();
  api.request(async (url, options) => {
    if (url === '/bootstrap') return dynamic;
    assert.ok(!JSON.parse(options.body).e.some(row => row[0] === 'shirt'), 'startup refresh must also exclude unsupported slots from resolver');
    return {equipment: {}};
  });
  await api.initialize();
  assert.notEqual(node('gear-catalog-status').textContent, '配装器初始化失败');
  assert.equal(api.state.equipment.shirt.item.item_id, 4);
});
