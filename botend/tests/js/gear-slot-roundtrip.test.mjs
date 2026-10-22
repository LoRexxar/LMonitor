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
function fixture(dataset = {}) {
  const nodes = new Map();
  const store = new Map();
  const page = {dataset: {bootstrapUrl: '/bootstrap', simcUrl: '/import', shareUrl: '/share', ...dataset}, setAttribute() {}};
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value: '', style: {}, classList: {add() {}, toggle() {}},
      querySelector: () => null, querySelectorAll: () => [], setAttribute() {}, removeAttribute() {}, close() {}, focus() {}});
    return nodes.get(id);
  };
  const context = {structuredClone, URLSearchParams, TextEncoder, TextDecoder, atob, btoa,
    URL, location: {search: '', href: 'https://fixture.invalid/gear/'}, window: {confirm: () => true},
    navigator: {clipboard: {writeText: async value => { context.copied = value; }}},
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
      refreshState, loadSavedLoadout, loadOnlineLoadout, copyShare, copySimcProfile, restoreShareIfPresent,
      saveOnlineLoadout,
      setLibrary: rows => { savedLoadouts = rows; },
      get library() { return savedLoadouts; },
    };
  `), context);
  const api = context.qa;
  api.configure(metadata);
  return {api, nodes, store, node, context, page};
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

test('legacy SimC shoulder and wrist tokens normalize at the input boundary without changing item parameters', async () => {
  const {api, node} = fixture();
  const legacy = [
    'warrior="wrists shoulders stay in names"', 'spec=fury',
    '# shoulders and wrists are old slot spellings',
    ' shoulders =old_shoulders,id=239049,ilevel=334,enchant_id=8031',
    '\twrists=old_wrists,id=123,ilevel=340,gem_id=240910/240910,bonus_id=1/2,crafted_stats=32/49',
    'wrist=single_form,id=123,ilevel=340', 'shoulder=single_form,id=239049,ilevel=334',
    '### Gear from Bags', '# wrists =bag_wrists,id=456,ilevel=334,enchant_id=789',
    '# shoulders=bag_shoulders,id=457,ilevel=334', 'unknown_wrists=unchanged,id=999',
  ].join('\r\n');
  const expected = legacy.replace(' shoulders =', ' shoulder =').replace('\twrists=', '\twrist=')
    .replace('# wrists =', '# wrist =').replace('# shoulders=', '# shoulder=');
  let requests = 0;
  api.request(async (_url, options) => {
    requests++;
    assert.equal(JSON.parse(options.body).profile, expected);
    return {equipment: [{slot: 'shoulders', item: entry(239049).item}, {slot: 'wrists', item: entry(123).item}]};
  });
  node('gear-simc-input').value = legacy;
  await api.importSimc();
  assert.equal(requests, 1);
  assert.equal(node('gear-simc-message').textContent, '');
  assert.equal(node('gear-simc-input').value, legacy, 'original pasted text stays recoverable');
  assert.deepEqual(Object.keys(api.state.equipment).sort(), ['shoulders', 'wrists']);
  assert.equal(api.state.equipment.shoulders.item.item_id, 239049);
  assert.equal(api.state.equipment.wrists.item.item_id, 123);
});

test('startup restores local and shared aliases, selection and locks without dropping pre-metadata state', async () => {
  for (const shared of [false, true]) {
    const {api, store, node} = fixture();
    const old = {...structuredClone(history['v1-full']), lockedSlots: ['shoulder', 'wrist']};
    const expected = {shoulders: old.equipment.shoulder, wrists: old.equipment.wrist};
    api.request(async () => ({equipment: expected}));
    api.configure(null);
    const pending = api.normalizeState(old);
    assert.deepEqual(plain(pending.lockedSlots), old.lockedSlots, 'metadata absence must not strip alias locks');
    api.configure(metadata);
    const restored = shared ? await api.hydrateSharePayload(old) : (store.set(storageKey, JSON.stringify(old)), api.loadStored('Warrior', 'Fury'));
    api.setState(restored);
    api.renderAll();
    assert.deepEqual(plain(api.state.equipment), expected);
    assert.equal(api.state.selectedSlot, 'wrists');
    assert.deepEqual(plain(api.state.lockedSlots), ['shoulders', 'wrists']);
    assert.ok(node('gear-slot-list').innerHTML.includes(expected.shoulders.item.name));
    assert.ok(node('gear-slot-list').innerHTML.includes(expected.wrists.item.name));
    api.persist();
    api.request(async url => url === '/bootstrap' ? metadata : {equipment: {}});
    await api.initialize();
    assert.deepEqual(plain(api.state.equipment), expected, 'real initialize / shared refresh retains complete entries');
    assert.equal(api.state.selectedSlot, 'wrists');
    assert.deepEqual(plain(api.state.lockedSlots), ['shoulders', 'wrists']);
  }
});

test('identical aliases dedupe; confirmed SimC replaces all slots and old locks, even across specs', async () => {
  for (const spec of ['Fury', 'Arms']) {
    const {api, node, store, context} = fixture();
    api.setState({selectedSlot: 'shoulder', lockedSlots: ['shoulder', 'wrist', 'head'], snapshotSlots: ['head'], equipment: {
      shoulder: entry(2), shoulders: entry(2), wrist: null, wrists: null, head: entry(3), neck: entry(4),
    }});
    assert.deepEqual(plain(api.state.equipment), {shoulders: entry(2), wrists: null, head: entry(3), neck: entry(4)});
    api.persist();
    store.set('wowdaily:gear-builder:loadouts:v1', 'saved-plans-preserved');
    if (spec === 'Arms') store.set('wowdaily:gear-builder:v1:Warrior:Arms', JSON.stringify({className: 'Warrior', specName: 'Arms', equipment: {feet: entry(5)}, lockedSlots: ['feet']}));
    const before = plain(api.state), storedBefore = [...store];
    let requests = 0;
    api.request(async () => { requests++; return {identity: {class_name: 'Warrior', spec_name: spec}, equipment: [
      {slot: 'shoulder', item: entry(99).item}, {slot: 'wrist', item: entry(100).item},
    ]}; });
    node('gear-simc-input').value = 'warrior=test';
    context.window.confirm = message => { assert.match(message, /替换.*锁定/); return false; };
    await api.importSimc();
    assert.equal(requests, 0, 'cancel must precede parsing and owned inventory requests');
    assert.deepEqual(plain(api.state), before);
    assert.deepEqual([...store], storedBefore);
    context.window.confirm = () => true;
    await api.importSimc();
    assert.equal(requests, 1);
    assert.equal(node('gear-simc-message').textContent, '');
    assert.deepEqual(Object.keys(api.state.equipment).sort(), ['shoulders', 'wrists']);
    assert.equal(api.state.equipment.shoulders.item.item_id, 99);
    assert.equal(api.state.equipment.wrists.item.item_id, 100);
    assert.deepEqual(plain(api.state.lockedSlots), []);
    assert.deepEqual(plain(api.state.snapshotSlots), []);
    assert.equal(api.state.specName, spec);
    assert.deepEqual(plain(api.loadStored('Warrior', spec)), plain(api.state));
    assert.equal(store.get('wowdaily:gear-builder:loadouts:v1'), 'saved-plans-preserved');
    if (spec === 'Arms') assert.deepEqual(JSON.parse(store.get(storageKey)), before, 'other spec draft is not overwritten');
  }
});

test('SimC parsing or owned-inventory failure never partially replaces the current draft', async () => {
  for (const failAt of ['/import', '/owned']) {
    const {api, node, store} = fixture({ownedItemsUrl: '/owned'});
    api.setState({lockedSlots: ['head'], equipment: {head: entry(2)}});
    api.persist();
    const before = plain(api.state), storedBefore = [...store], requests = [];
    api.request(async url => {
      requests.push(url);
      if (url === failAt) throw new Error('import failure');
      return {identity: {class_name: 'Warrior', spec_name: 'Arms'}, equipment: [{slot: 'head', item: entry(99).item}],
        owned_equipment: [{slot: 'head', item_id: 99}]};
    });
    node('gear-simc-input').value = 'warrior=test';
    await api.importSimc();
    assert.deepEqual(requests, failAt === '/import' ? ['/import'] : ['/import', '/owned']);
    assert.equal(node('gear-simc-message').textContent, 'import failure');
    assert.deepEqual(plain(api.state), before);
    assert.deepEqual([...store], storedBefore);
    assert.equal(node('gear-simc-submit').disabled, false);
  }
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
    assert.deepEqual(JSON.parse(options.body).e, [['shoulders', 2, 'variant-2']]);
    return {equipment: {shoulders: entry(2)}};
  });
  const restored = await api.hydrateSharePayload({v: 4, c: 'Warrior', s: 'Fury', e: [
    ['shoulder', 2, 'variant-2'], ['shoulders', 2, 'variant-2'],
  ]});
  assert.equal(restored.equipment.shoulders.item.item_id, 2, 'one legacy alias plus canonical remains compatible');
});

test('counts, previews and saved library follow actual slots, preserving unsupported data separately', async () => {
  const {api, node, store} = fixture();
  const dynamic = {...metadata, slots: [slots[0], slots[2], {key: 'future_slot', label: '未来槽位', simc_key: 'future_slot'}]};
  api.configure(dynamic);
  api.setState({equipment: {head: entry(1), shoulders: entry(2), shoulder: entry(2), future_slot: entry(3), shirt: entry(4)}});
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
  const oldCode = 'j' + Buffer.from(JSON.stringify({version: 1, equipment: {
    head: entry(1), shoulder: entry(3), shoulders: entry(3), shirt: entry(4),
  }})).toString('base64url');
  store.set('wowdaily:gear-builder:loadouts:v1', JSON.stringify([{id: 'old', code: oldCode, equippedCount: 18}]));
  const library = await api.readSavedLoadouts();
  assert.equal(library[0].equippedCount, 2, 'historical library count must derive from canonical keys, not old inflated count');
  const share = api.compactShareState(api.state);
  assert.ok(share.e.find(row => row[0] === 'shirt')[8], 'unknown entries retain snapshots across save/share');
  api.request(async (_url, options) => {
    assert.ok(!JSON.parse(options.body).e.some(row => row[0] === 'shirt'), 'unsupported snapshot is not sent to slot-restricted resolver');
    return {equipment: {head: entry(1), shoulders: entry(2), future_slot: entry(3)}};
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

const history = JSON.parse(fs.readFileSync(new URL('./fixtures/gear-share-history.json', import.meta.url), 'utf8'));
const codeFor = payload => 'j' + Buffer.from(JSON.stringify(payload)).toString('base64url');

test('real historical v2/v3/v4 producer payloads retain reference layouts and all 16 slots', async () => {
  for (const name of ['v2-aliases', 'v3-aliases', 'v4-aliases', 'v4-equal-18']) {
    const {api} = fixture();
    const old = history[name];
    let calls = 0;
    api.request(async (_url, options) => {
      calls++;
      const sent = JSON.parse(options.body);
      assert.equal(sent.strict, true, 'restoration must request all-or-nothing resolution');
      assert.equal(sent.v, old.v, 'v2 DB IDs and v3 four-field references must not be reinterpreted as v4');
      assert.equal(sent.e.length, slots.length);
      for (const row of sent.e) {
        const original = old.e.find(candidate => (aliases[candidate[0]] || candidate[0]) === row[0]);
        assert.deepEqual(row.slice(1), original.slice(1), 'retain original references including raw snapshots');
      }
      return {equipment: structuredClone(history.resolved.equipment)};
    });
    const restored = await api.hydrateSharePayload(old);
    assert.equal(calls, 1);
    assert.deepEqual(plain(restored.equipment), history.resolved.equipment);
    assert.equal(Object.keys(restored.equipment).length, slots.length);
  }
});

test('real v1 full snapshots retain all enhancement facts when current refresh cannot resolve them', async () => {
  const {api, store} = fixture();
  const old = history['v1-full'];
  let validations = 0;
  api.request(async (_url, options) => {
    validations++;
    const sent = JSON.parse(options.body);
    assert.equal(sent.strict, true);
    assert.equal(sent.v, 4);
    assert.equal(sent.equipment, undefined, 'full snapshots must not be duplicated in the request');
    assert.deepEqual(sent.e.map(row => row[8]), Object.values(old.equipment));
    return {equipment: {}}; // Validated self-contained facts need no central references.
  });
  const restored = await api.hydrateSharePayload(old);
  assert.equal(validations, 1, 'full payloads must pass strict server validation');
  api.setState(restored);
  api.persist();
  const before = plain(api.state);
  const saved = store.get(storageKey);
  api.request(async (_url, options) => {
    assert.equal(JSON.parse(options.body).strict, true);
    const degraded = structuredClone(restored.equipment);
    degraded.wrists.gems = [];
    degraded.shoulders.enchant = null;
    return {equipment: degraded, warnings: ['宝石引用已失效', '附魔引用已失效']};
  });
  await assert.rejects(api.refreshState(api.state), /宝石|附魔|完整|刷新/);
  assert.deepEqual(plain(api.state), before);
  assert.equal(store.get(storageKey), saved);
  const shared = api.compactShareState(api.state);
  for (const row of shared.e) {
    const oldSlot = Object.keys(aliases).find(key => aliases[key] === row[0]);
    assert.deepEqual(plain(row[8]), old.equipment[oldSlot], 'snapshots stay self-contained even if old catalogue IDs are stale');
  }
});

test('raw row[8] is sent intact in strict requests and restored without manufacturing items', async () => {
  const {api} = fixture();
  const snapshot = history['v1-full'].equipment.wrist;
  const old = {v: 2, c: 'Warrior', s: 'Fury', e: [['wrist', 0, 0, [], 0, [], 0, 1, snapshot]]};
  api.request(async (_url, options) => {
    const sent = JSON.parse(options.body);
    assert.equal(sent.strict, true);
    assert.deepEqual(sent.e[0][8], snapshot);
    return {equipment: {}};
  });
  const restored = await api.hydrateSharePayload(old);
  assert.deepEqual(plain(restored.equipment.wrists), snapshot);
  assert.deepEqual(plain(api.compactShareState(restored).e[0][8]), snapshot);
});

test('divergent aliases and explicit null never silently choose a winner or overwrite local state', async () => {
  for (const equipment of [
    {shoulder: entry(1), shoulders: entry(2)},
    {wrist: entry(1), wrists: null},
    {shoulder: entry(1), shoulders: {...entry(1), addedSocket: false}},
  ]) {
    const {api, store} = fixture();
    api.setState({equipment: {head: entry(3)}, lockedSlots: ['head']});
    const before = plain(api.state);
    const stored = JSON.stringify({equipment});
    store.set(storageKey, stored);
    assert.throws(() => api.loadStored('Warrior', 'Fury'), /冲突/);
    assert.throws(() => api.compactShareState({equipment}), /冲突/);
    await assert.rejects(api.hydrateSharePayload({version: 1, equipment}), /冲突/);
    assert.deepEqual(plain(api.state), before);
    assert.equal(store.get(storageKey), stored);
  }
  const {api} = fixture();
  api.request(async () => { throw Error('conflicts must fail before requesting'); });
  await assert.rejects(api.hydrateSharePayload(history['v4-conflict']), /冲突/);
});

test('failed local/online loads preserve current state, locks and both original saved codes', async () => {
  for (const online of [false, true]) {
    const {api, store, page} = fixture();
    page.dataset.onlineLoadoutsUrl = '/online/';
    api.setState({equipment: {head: entry(3)}, lockedSlots: ['head']});
    api.persist();
    const before = plain(api.state);
    const persisted = store.get(storageKey);
    const record = {id: '41', name: '历史配装', code: codeFor(history['v4-unresolved'])};
    api.setLibrary([record]);
    const library = JSON.stringify([record]);
    store.set('wowdaily:gear-builder:loadouts:v1', library);
    let writes = 0;
    api.request(async (_url, options) => {
      if (!options) return {loadout: record};
      writes++;
      return {equipment: {head: entry(1)}, warnings: ['肩部的装备引用已失效']};
    });
    await assert.rejects(online ? api.loadOnlineLoadout(record) : api.loadSavedLoadout(record), /失效|完整/);
    assert.equal(writes, 1);
    assert.deepEqual(plain(api.state), before);
    assert.equal(store.get(storageKey), persisted);
    assert.equal(store.get('wowdaily:gear-builder:loadouts:v1'), library);
    assert.equal(api.library[0].code, record.code);
  }
});

test('startup share failure leaves raw URL/local/library intact and blocks empty copy/save', async () => {
  const {api, store, context, node} = fixture();
  const oldCode = codeFor(history['v4-unresolved']);
  context.location.search = '?code=' + oldCode;
  const local = JSON.stringify({equipment: {wrist: entry(8)}, lockedSlots: ['wrist']});
  store.set(storageKey, local);
  store.set('wowdaily:gear-builder:loadouts:v1', '[]');
  api.request(async url => url === '/bootstrap' ? metadata : {equipment: {}, warnings: ['肩部的装备引用已失效']});
  await api.initialize();
  assert.equal(store.get(storageKey), local);
  assert.equal(context.location.search, '?code=' + oldCode);
  await assert.rejects(api.copyShare(), /恢复|载入|完整/);
  await assert.rejects(api.copySimcProfile(), /恢复|载入|完整/);
  await assert.rejects(api.saveCurrentLoadout('不得覆盖'), /恢复|载入|完整/);
  node('gear-online-name').value = '不得覆盖';
  await assert.rejects(api.saveOnlineLoadout(), /恢复|载入|完整/);
  assert.equal(context.copied, undefined);
  assert.equal(store.get('wowdaily:gear-builder:loadouts:v1'), '[]');
});

test('full saved loads validate before replacing current state and never rewrite saved originals', async () => {
  for (const online of [false, true]) {
    for (const invalid of [true, false]) {
      const {api, store, page} = fixture();
      page.dataset.onlineLoadoutsUrl = '/online/';
      api.setState({equipment: {head: entry(3)}, lockedSlots: ['head']});
      api.persist();
      const before = plain(api.state);
      const persisted = store.get(storageKey);
      const old = structuredClone(history['v1-full']);
      if (invalid) old.equipment.wrist.variant.item_id = 1; // Server owns identity validation.
      const record = {id: '41', name: '旧完整方案', code: codeFor(old)};
      api.setLibrary([record]);
      const library = JSON.stringify([record]);
      store.set('wowdaily:gear-builder:loadouts:v1', library);
      let validations = 0;
      api.request(async (_url, options) => {
        if (!options) return {loadout: record};
        validations++;
        assert.equal(JSON.parse(options.body).strict, true);
        assert.deepEqual(plain(api.state), before, 'no replacement before validation completes');
        assert.equal(store.get(storageKey), persisted);
        if (invalid) throw Error('快照物品身份无效');
        return {equipment: {}};
      });
      const load = online ? api.loadOnlineLoadout(record) : api.loadSavedLoadout(record);
      if (invalid) {
        await assert.rejects(load, /身份/);
        assert.deepEqual(plain(api.state), before);
        assert.equal(store.get(storageKey), persisted);
      } else {
        await load;
        assert.deepEqual(plain(api.state.equipment), {shoulders: old.equipment.shoulder, wrists: old.equipment.wrist});
        assert.deepEqual(plain(api.state.lockedSlots), [], 'independent load replaces old locks');
      }
      assert.equal(validations, 1);
      assert.equal(store.get('wowdaily:gear-builder:loadouts:v1'), library);
      assert.equal(api.library[0].code, record.code);
    }
  }
});

test('full startup validates snapshots then preserves them on failed reference refresh and re-export', async () => {
  const {api, context, store} = fixture();
  const old = history['v1-full'];
  context.location.search = '?code=' + codeFor(old);
  let validations = 0, refreshes = 0;
  api.request(async (url, options) => {
    if (url === '/bootstrap') return metadata;
    const sent = JSON.parse(options.body);
    assert.equal(sent.strict, true);
    if (sent.e.every(row => row[8])) { validations++; return {equipment: {}}; }
    refreshes++;
    throw Error('中央引用失效');
  });
  await api.initialize();
  assert.equal(validations, 1);
  assert.equal(refreshes, 1);
  const expected = {shoulders: old.equipment.shoulder, wrists: old.equipment.wrist};
  assert.deepEqual(plain(api.state.equipment), expected);
  assert.deepEqual(JSON.parse(store.get(storageKey)).equipment, expected);
  assert.deepEqual(plain(api.compactShareState(api.state).e.map(row => row[8])), Object.values(expected));
});

test('persist keeps a historical snapshot batch instead of relabeling it as the current catalog', () => {
  const {api, store} = fixture();
  const old = structuredClone(history['v1-full']);
  api.configure({...metadata, catalog: {batch_key: 'review-new-batch'}});
  api.setState({...old, snapshotSlots: ['shoulders', 'wrists']});
  api.persist();
  const share = api.compactShareState(api.state);
  assert.equal(share.b, old.batchKey);
  assert.equal(JSON.parse(store.get(storageKey)).batchKey, old.batchKey);
  assert.deepEqual(plain(share.e.map(row => row[8])), Object.values(old.equipment));
});

test('partial refresh preserves each untouched snapshot component origin across batch changes', async () => {
  const {api} = fixture();
  const old = {...entry(2), external: true};
  const newer = structuredClone(entry(1));
  for (const component of [newer, newer.embellishment, ...newer.gems, newer.enchant]) component.variant.batch_key = 'new-batch';
  api.configure({...metadata, catalog: {batch_key: 'new-batch'}});
  api.request(async () => ({equipment: {head: newer}, batch_key: 'new-batch', warnings: []}));
  const refreshed = await api.refreshState({batchKey: 'old-batch', equipment: {head: entry(1), wrists: old}});
  api.setState(refreshed);api.persist();
  const exported = api.compactShareState(api.state);
  const preserved = exported.e.find(row => row[0] === 'wrists')[8];
  assert.equal(exported.b, 'new-batch');
  for (const component of [preserved, preserved.embellishment, ...preserved.gems, preserved.enchant]) assert.equal(component.variant.batch_key, 'old-batch');
  assert.equal(old.variant.batch_key, undefined, 'source object is not rewritten');
});

test('mixed known component batches export as explicit snapshots, while uniform current gear stays compact', () => {
  const {api} = fixture();
  const mixed = structuredClone(entry(1));
  mixed.variant.batch_key = 'old-batch';
  mixed.enchant.variant.batch_key = 'new-batch';
  api.setState({batchKey: 'old-batch', equipment: {head: mixed}});api.persist();
  assert.equal(api.compactShareState(api.state).e[0][8].enchant.variant.batch_key, 'new-batch');
  for (const component of [mixed, mixed.embellishment, ...mixed.gems, mixed.enchant]) component.variant.batch_key = 'new-batch';
  api.setState({batchKey: 'old-batch', equipment: {head: mixed}});api.persist();
  const compact = api.compactShareState(api.state);
  assert.equal(compact.b, 'new-batch');assert.equal(compact.e[0].length, 8);
});

test('incomplete response without warnings and malformed legacy payload cannot become a successful empty load', async () => {
  const {api} = fixture();
  api.request(async () => ({equipment: {}}));
  await assert.rejects(api.hydrateSharePayload(history['v4-aliases']), /完整|失效/);
  for (const payload of [{}, {v: 2}, {v: 99, e: []}, {equipment: []}, {equipment: {}},
    {equipment: {shoulder: {item: {item_id: 1}}}}, {version: 1, equipment: {shoulder: {item: {item_id: 1}}}}]) {
    await assert.rejects(api.hydrateSharePayload(payload), /格式|版本/);
  }
  api.configure(null);
  await assert.rejects(api.hydrateSharePayload(history['v4-aliases']), /就绪|元数据/);
});
