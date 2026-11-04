// Pure renderer contracts: no transport, simulation, or production acceptance.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readSource, beforeMarker} = require('./vm_harness');

function element(tag = 'div') {
  const el = {tagName: tag.toUpperCase(), children: [], style: {}, dataset: {}, attributes: {}, handlers: {}, textContent: '',
    append(...nodes) { for (const child of nodes) { this.children.push(child); child.parentNode = this; } },
    appendChild(child) { this.append(child); return child; },
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    getAttribute(key) { return this.attributes[key]; },
    addEventListener(type, handler) { this.handlers[type] = handler; },
    remove() { if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); },
    focus() { this.focused = true; }, scrollIntoView(options) { this.scrolled = options; },
    click() { this.handlers.click?.(); },
    querySelector() { return null; },
    get childElementCount() { return this.children.length; },
    get childNodes() { return this.children; },
    getBoundingClientRect() { return {left: 0, top: 0, width: 1000}; },
  };
  el.classList = {
    add(...names) { el.className = [...new Set([...(el.className || '').split(' '), ...names])].filter(Boolean).join(' '); },
    remove(name) { el.className = (el.className || '').split(' ').filter(n => n !== name).join(' '); },
    toggle(name, enabled) { if (enabled) this.add(name); else this.remove(name); },
  };
  return el;
}
const context = vm.createContext({document: {createElement: element}, Intl, URL, URLSearchParams, AbortController,
  window: {location: {origin: 'https://wowdaily.cn', search: ''}},
  fetch() { throw new Error('rendering must not fetch'); },
});
const source = readSource('static/portal/js/simc-benchmarks.js');
vm.runInContext(beforeMarker(source, '  document.addEventListener("DOMContentLoaded", loadBenchmarks);') + `
  globalThis.api = {overview: renderTierOverview, gear: renderGearResultChart,
    spec: renderSpecComparison, gain: renderOptionGain, gainScenarios: renderOptionGainByScenario,
    coordinate: renderCoordinate, results: renderResults};
})();`, context);
const {overview, gear, spec, gain, coordinate} = context.api;
function descendants(root, name) {
  return (root.children || []).flatMap(child => [
    ...((child.className || '').split(' ').includes(name) ? [child] : []), ...descendants(child, name),
  ]);
}
function text(root) { return [root.textContent, ...(root.children || []).map(text)].join(' '); }
const cards = root => descendants(root, 'simc-benchmark-tier-item');
function tiers(root) {
  return Object.fromEntries(descendants(root, 'simc-benchmark-tier-row').map(row => [row.attributes['data-tier'], cards(row).map(card => descendants(card, 'simc-benchmark-tier-name')[0].textContent)]));
}
function card(root, name) { return cards(root).find(c => descendants(c, 'simc-benchmark-tier-name')[0].textContent === name); }
function metric(root, name) { return descendants(card(root, name), 'simc-benchmark-tier-metric')[0].textContent; }
function ranges(root) {
  return descendants(root, 'simc-benchmark-tier-label').slice(0,7).map(label => label.children[1].textContent);
}
function equalRangeBoard(root, unit) {
  const board = descendants(root, 'simc-benchmark-tier-board')[0] || root;
  assert.equal(board.attributes['data-tier-mode'], 'equal_range');
  const labels = ranges(root);
  assert.equal(labels.length, 7);
  for (const label of labels) {
    assert.match(label, /约.*[0-9]/, 'left label shows approximate numeric bounds, not a segment number');
    assert.ok(label.includes(unit), `range label includes ${unit}: ${label}`);
    assert.doesNotMatch(label, /第\d段|≥95%|90–95%/);
  }
  assert.equal(new Set(labels).size, 7, 'small ranges must not collapse to identical labels');
  return board;
}
const entry = (label, value, extra = {}) => ({label, value, metricLabel: 'DPS', ...extra});
const borderEntries = [100, 95, 94.999, 90, 89.999, 85, 84.999, 80, 79.999, 75, 74.999, 70, 69.999, 0, -5].map(v => entry(String(v), v));
const borderBefore = JSON.stringify(borderEntries);
const borders = overview(borderEntries);
assert.deepEqual(tiers(borders), {S:['100','95','94.999','90','89.999','85'], A:['84.999','80','79.999','75','74.999','70'], B:['69.999'], C:[], D:[], E:[], F:['0','-5']});
equalRangeBoard(borders, 'DPS');
assert.equal(JSON.stringify(borderEntries), borderBefore);
assert.match(text(borders), /当前.*最高值.*最低值/);
assert.match(text(borders), /不代表.*整体.*强度/);
for (const value of [null, undefined, '', ' ', false, true, NaN, Infinity, {}, [], 'invalid']) {
  const board = overview([entry('Leader', 100), entry('Unknown', value, {status: '结果待确认'})]);
  assert.deepEqual(tiers(board).unrated, ['Unknown']);
  assert.match(text(card(board, 'Unknown')), /结果待确认/);
  assert.doesNotMatch(metric(board, 'Unknown'), /0 DPS/);
}
for (const values of [[0, 0], [-2, -2], [-1, -2]]) {
  const board = overview(values.map((v,i) => entry(`v${i}`, v)));
  assert.deepEqual(tiers(board).S, values[0] === values[1] ? ['v0','v1'] : ['v0']);
  assert.deepEqual(tiers(board).F, values[0] === values[1] ? [] : ['v1']);
  assert.equal(tiers(board).unrated, undefined);
}
const unknownOnly = overview([entry('null', null),entry('bool', false)]);
assert.deepEqual(tiers(unknownOnly).unrated, ['null','bool']);
assert.match(text(unknownOnly), /暂无有效结果/);
assert.match(text(overview([])), /暂无有效结果/);

// Equal-width value intervals, not fixed leader percentages or rank quantiles.
const equalRange = {tierMode:'equal_range'};
const compressed = overview([7007,7006,7005,7004,7003,7002,7001,7000].map(v=>entry(String(v),v)),equalRange);
assert.deepEqual(tiers(compressed),{S:['7007','7006'],A:['7005'],B:['7004'],C:['7003'],D:['7002'],E:['7001'],F:['7000']});
assert.match(text(compressed),/最高.*最低.*7.*等分|最高.*最低.*等分.*7/);
assert.doesNotMatch(text(compressed),/≥95%|90–95%/);
const decimalRange=overview([0.8,0.7,0.6,0.5,0.4,0.3,0.2,0.1].map(v=>entry(String(v),v)),equalRange);
assert.deepEqual(tiers(decimalRange),{S:['0.8','0.7'],A:['0.6'],B:['0.5'],C:['0.4'],D:['0.3'],E:['0.2'],F:['0.1']});
// Large offsets must not lose decimal boundaries or swallow the adjacent float below.
function nextDown(value) {
  const bytes = new DataView(new ArrayBuffer(8));
  bytes.setFloat64(0, value);
  bytes.setBigUint64(0, bytes.getBigUint64(0) - 1n);
  return bytes.getFloat64(0);
}
function nextUp(value) {
  const bytes = new DataView(new ArrayBuffer(8));
  bytes.setFloat64(0, value);
  bytes.setBigUint64(0, bytes.getBigUint64(0) + 1n);
  return bytes.getFloat64(0);
}
for (const values of [
  [7000,7000.1,7000.2,7000.3,7000.4,7000.5,7000.6,7000.7],
  [7000,7000.000001,7000.000002,7000.000003,7000.000004,7000.000005,7000.000006,7000.000007],
  [7000,7000.000000001,7000.000000002,7000.000000003,7000.000000004,7000.000000005,7000.000000006,7000.000000007],
  [1e-7,2e-7,3e-7,4e-7,5e-7,6e-7,7e-7,8e-7],
]) {
  const labels = ['F','E','D','C','B','A','S','S'];
  const entries = values.map((value, i) => entry(`boundary ${i}`, value));
  for (let k = 1; k <= 6; k++) {
    entries.push(entry(`below ${k}`, nextDown(values[k])));
    entries.push(entry(`above ${k}`, nextUp(values[k])));
  }
  const before = JSON.stringify(entries);
  const board = overview(entries, equalRange);
  for (let k = 0; k < values.length; k++) assert.ok(tiers(board)[labels[k]].includes(`boundary ${k}`), `${values[k]} belongs to ${labels[k]}`);
  for (let k = 1; k <= 6; k++) assert.ok(tiers(board)[labels[k-1]].includes(`below ${k}`), `adjacent float below ${values[k]} stays lower`);
  for (let k = 1; k <= 6; k++) assert.ok(tiers(board)[labels[k]].includes(`above ${k}`), `adjacent float above ${values[k]} stays higher`);
  equalRangeBoard(board, 'DPS');
  assert.equal(JSON.stringify(entries), before);
  assert.equal(board.attributes['data-tier-minimum'], String(values[0]));
  assert.equal(board.attributes['data-tier-maximum'], String(values[7]));
  assert.equal(Number(board.attributes['data-tier-width']), (values[7]-values[0])/7);
}
for (const maximum of [7000.7,7000.000007]) {
  const minimum = 7000, width = (maximum-minimum)/7;
  const board = overview([entry('min',minimum),entry('max',maximum),entry('computed 2',minimum+2*width),entry('computed 4',minimum+4*width)],equalRange);
  assert.deepEqual(tiers(board).D,['computed 2']);
  // In the tiny range the computed fourth cut is actually 7000.0000039999995,
  // below the decimal boundary: do not promote it by a tolerance.
  assert.deepEqual(tiers(board)[maximum === 7000.7 ? 'B' : 'C'],['computed 4']);
}
const tinyRange = overview([entry('min',7000),entry('max',7000.000000007)],equalRange);
assert.match(text(tinyRange), /7,000\.000000007/);
assert.match(text(tinyRange), /每档约/);
for (const row of descendants(tinyRange,'simc-benchmark-tier-row')) {
  assert.match(row.title, /最低值\+极差×\d\/7 ≤ DPS [<≤] 最低值\+极差×\d\/7/);
}
const clustered=overview([100,99.999,99.998,70].map(v=>entry(String(v),v)),equalRange);
assert.deepEqual(tiers(clustered).S,['100','99.999','99.998']);
assert.deepEqual(tiers(clustered).F,['70']);assert.deepEqual(tiers(clustered).A,[]);
const equalValues=overview([entry('Tie A',84),entry('Tie B',84)],equalRange);
assert.deepEqual(tiers(equalValues).S,['Tie A','Tie B']);assert.deepEqual(tiers(equalValues).F,[]);
assert.match(text(equalValues),/相同|并列/);
const rangeUnknown=overview([entry('Top',100),entry('Low',99),entry('Missing',null),entry('Bad',false)],equalRange);
assert.deepEqual(tiers(rangeUnknown).S,['Top']);assert.deepEqual(tiers(rangeUnknown).F,['Low']);
assert.deepEqual(tiers(rangeUnknown).unrated,['Missing','Bad']);
// Even adjacent representable endpoints need distinguishable approximate range labels.
equalRangeBoard(overview([entry('low',nextDown(1)),entry('high',1)]), 'DPS');

const tooltip = '完整装备\n物品等级 321\n+123 急速\n装备：造成 456 点伤害并提高属性。';
const items = [
  {key:'a-low', item_id:1, label:'A', item_level:321, dps:100, tooltip, icon_url:'/icons/a.jpg'},
  {key:'a-high', item_id:1, label:'A', item_level:340, dps:200, tooltip},
  {key:'b', item_id:2, label:'B', item_level:340, dps:190},
];
const itemsBefore = JSON.stringify(items);
const ordinary = gear(items, {dps:80});
assert.equal(cards(ordinary).length, 2, 'one icon per original grouped item, not per item level');
assert.deepEqual(tiers(ordinary).S, ['A']);
assert.deepEqual(tiers(ordinary).F, ['B']);
equalRangeBoard(ordinary, 'DPS');
assert.match(metric(ordinary, 'A'), /200.*DPS/);
const a = card(ordinary, 'A');
assert.equal(a.tagName, 'BUTTON'); assert.equal(a.type, 'button');
assert.notEqual(a.tabIndex, -1);
for (const node of [a, ...descendants(a, 'simc-benchmark-tier-icon'), ...descendants(a, 'simc-benchmark-tier-name')]) {
  assert.equal(node.attributes['data-wow-item-tooltip'], tooltip);
  assert.equal(node.attributes['data-wow-item-tooltip-name'], 'A');
}
assert.equal(descendants(a, 'simc-benchmark-tier-icon')[0].src, 'https://wowdaily.cn/icons/a.jpg');
a.handlers.focus(); assert.match(text(descendants(ordinary, 'simc-benchmark-tier-detail')[0]), /A.*200/);
a.click();
const originalA = descendants(ordinary, 'simc-benchmark-gear-identity')[0];
assert.equal(originalA.focused, true); assert.ok(originalA.scrolled);
assert.equal(originalA.attributes['data-wow-item-tooltip'], tooltip, 'original tooltip remains');
assert.equal(descendants(ordinary, 'simc-benchmark-gear-name').length, 2, 'new icons do not pollute old chart classes');
assert.equal(descendants(ordinary, 'simc-benchmark-gear-segment').length, 3);
assert.equal(JSON.stringify(items), itemsBefore);
const displayChart = gear([
  {key:'raw-high',label:'Raw high',dps:1000,display_dps:900},
  {key:'display-high',label:'Display high',dps:950},
], null);
assert.deepEqual(tiers(displayChart).S,['Display high']);
assert.deepEqual(tiers(displayChart).F,['Raw high'],'ordinary gear ranges use existing display DPS, not raw ordering');
const brokenIcon = descendants(a, 'simc-benchmark-tier-icon')[0]; brokenIcon.handlers.error();
assert.ok(descendants(a, 'simc-benchmark-tier-placeholder').some(node => !node.hidden), 'failed assets reveal an explicit text placeholder');
const unsafe = overview([entry('Unsafe <script>', 100, {icon_url:'javascript:alert(1)', tooltip})]);
assert.equal(descendants(unsafe, 'simc-benchmark-tier-icon').length, 0);
assert.match(text(unsafe), /无图/);
assert.match(card(unsafe, 'Unsafe <script>').attributes['aria-label'], /Unsafe <script>/);

const effect = (label, dps, display, extra = {}) => ({key:label, item_id:label, label, dps, baseline_dps:100,
  comparison_mode:'equipment_effect', effect_delta_percent:display, effect_validation:{status:'valid'}, ...extra});
const effectRows = [
  effect('High DPS', 1000, 1, {item_id:11}),
  effect('Best gain', 120, 20, {item_id:12, item_level:321}),
  effect('Best gain', 150, 10, {item_id:12, item_level:340, key:'other'}),
  effect('Display zero', 90, 0, {item_id:13}),
  effect('Invalid', 999, 50, {item_id:14, effect_validation:{status:'invalid', reason:'目标特效未加载'}}),
  effect('Pending', 999, 60, {item_id:15, effect_validation:{status:'pair_pending'}}),
  effect('No control', 999, 60, {item_id:16, baseline_dps:null}),
  effect('Boolean', 999, false, {item_id:17}),
];
const effectRange = gear([effect('Top 11.43',120,11.43),effect('Middle 5.82',999,5.82),effect('Zero',100,0)],null);
assert.deepEqual(tiers(effectRange).S,['Top 11.43']);
assert.deepEqual(tiers(effectRange).C,['Middle 5.82'], 'effect midpoint uses backend display gain, not DPS or fixed leader percent');
assert.deepEqual(tiers(effectRange).F,['Zero']);
equalRangeBoard(effectRange, '%');
assert.match(ranges(effectRange)[0], /9\.8.*11\.43/);
assert.match(ranges(effectRange)[6], /0%.*1\.63/);
const zeroEffects = gear([effect('Zero A',90,0),effect('Zero B',110,0),effect('Invalid zero',100,0,{effect_validation:{status:'invalid'}})],null);
assert.deepEqual(tiers(zeroEffects).S,['Zero A','Zero B']);
assert.deepEqual(tiers(zeroEffects).unrated,['Invalid zero']);
const effectBefore = JSON.stringify(effectRows);
const effectChart = gear(effectRows, null);
assert.deepEqual(tiers(effectChart).S, ['Best gain']);
assert.ok(tiers(effectChart).F.includes('High DPS'));
assert.ok(tiers(effectChart).F.includes('Display zero'));
assert.match(metric(effectChart, 'Best gain'), /\+20\.00%.*特效提升/);
assert.match(metric(effectChart, 'Display zero'), /\+0\.00%/);
assert.deepEqual(tiers(effectChart).unrated.sort(), ['Boolean','Invalid','No control','Pending'].sort());
assert.match(text(card(effectChart, 'Invalid')), /特效结果无效.*目标特效未加载/);
assert.match(text(card(effectChart, 'Pending')), /特效待验证/);
assert.equal(JSON.stringify(effectRows), effectBefore);
for (const bad of [null, false, '', NaN]) {
  const root = gear([{key:'missing',label:'Missing DPS',dps:bad}], null);
  assert.deepEqual(tiers(root).unrated, ['Missing DPS']);
}

const coord = (label, dps, extra={}) => ({spec_key:label, profile_key:'p', talent_key:'p', scenario_key:'one',
  labels:{spec:label, hero_talent:'英雄天赋'}, candidates:[{key:'baseline',type:'baseline',dps}], ...extra});
const specRows = [coord('Spec A',100,{spec_icon_url:'/icons/spec.jpg'}),coord('Spec B',70),coord('Missing',null),coord('Boolean',false)];
const specOptions = rows => rows.map(row => ({spec_key:row.spec_key,value:row.spec_key,role:'dps'}));
const roleRows = [coord('Damage',1000),coord('Tank',100),coord('Healer',10),coord('Unknown',5000,{role:'dps'})];
const rolePayload = {spec_options:[{spec_key:'Damage',value:'Damage',role:'dps'}, {spec_key:'Tank',value:'Tank',role:'tank'}, {spec_key:'Healer',value:'Healer',role:'healer'}],results:{coordinates:roleRows}};
const roleBefore=JSON.stringify(rolePayload), roleShell={body:element()};
spec(roleShell,rolePayload);
const roleSelect=descendants(roleShell.body,'simc-benchmark-role-select')[0];
assert.ok(roleSelect,'spec comparison needs an authoritative role filter');
assert.equal(roleSelect.value,'dps');
assert.deepEqual(tiers(roleShell.body).S,['Damage']);
roleSelect.value='tank'; roleSelect.handlers.change();
assert.deepEqual(tiers(roleShell.body).S,['Tank'],'tank leader is S in its own scope, not F against DPS');
assert.deepEqual(descendants(roleShell.body,'simc-benchmark-spec-name').map(el=>el.textContent),['Tank']);
assert.equal(descendants(roleShell.body,'simc-benchmark-spec-rank')[0].textContent,'1');
assert.match(text(roleShell.body),/相对最高 100\.0%/);
roleSelect.value='healer'; roleSelect.handlers.change();
assert.deepEqual(tiers(roleShell.body).S,['Healer']);
roleSelect.value='unknown'; roleSelect.handlers.change();
assert.deepEqual(tiers(roleShell.body).S,[]);
assert.deepEqual(tiers(roleShell.body).unrated,['Unknown']);
assert.match(text(roleShell.body),/职责未识别/);
assert.match(text(roleShell.body),/5,000 DPS/);
assert.equal(JSON.stringify(rolePayload),roleBefore);
const specPayload = {spec_options:specOptions(specRows),results:{coordinates:specRows}};
const specBefore = JSON.stringify(specPayload), specShell = {body:element()};
spec(specShell, specPayload);
assert.deepEqual(tiers(specShell.body).S, ['Spec A']); assert.deepEqual(tiers(specShell.body).F, ['Spec B']);
assert.deepEqual(tiers(specShell.body).unrated, ['Missing','Boolean']);
equalRangeBoard(specShell.body, 'DPS');
assert.equal(descendants(specShell.body, 'simc-benchmark-spec-name').length, 4);
card(specShell.body, 'Spec A').click();
const specOriginal = descendants(specShell.body, 'simc-benchmark-spec-row-toggle')[0];
assert.equal(specOriginal.focused, true);
assert.equal(descendants(specShell.body, 'simc-benchmark-profile-details')[0].open, true);
assert.equal(specOriginal.attributes['aria-expanded'], 'true');
assert.equal(JSON.stringify(specPayload), specBefore);
const rawSpecShell={body:element()};
spec(rawSpecShell,{spec_options:specOptions([coord('Raw spec'),coord('Other spec')]),results:{coordinates:[coord('Raw spec',100,{candidates:[{key:'baseline',dps:100,display_dps:1}]}),coord('Other spec',95)]}});
assert.deepEqual(tiers(rawSpecShell.body).S,['Raw spec'],'spec comparison retains its raw baseline DPS metric');
assert.deepEqual(tiers(rawSpecShell.body).F,['Other spec']);

const gains = [
  {spec_key:'Gain A',spec_label:'Gain A',profile_key:'p',scenario_key:'one',baseline_dps:100,comparison_dps:110,gain_percent:10},
  {spec_key:'Gain Zero',spec_label:'Gain Zero',profile_key:'p',scenario_key:'one',baseline_dps:100,comparison_dps:100,gain_percent:0},
  {spec_key:'Gain Negative',spec_label:'Gain Negative',profile_key:'p',scenario_key:'one',baseline_dps:100,comparison_dps:95,gain_percent:-5},
  {spec_key:'Gain Missing',spec_label:'Gain Missing',profile_key:'p',scenario_key:'one',baseline_dps:100,comparison_dps:110,gain_percent:null},
  {spec_key:'Gain No Control',spec_label:'Gain No Control',profile_key:'p',scenario_key:'one',baseline_dps:null,comparison_dps:110,gain_percent:20},
];
const gainPayload = {results:{option_gain_rows:gains,coordinates:gains.map(g=>coord(g.spec_key,100))}};
const gainBefore = JSON.stringify(gainPayload), gainShell={body:element()}; gain(gainShell,gainPayload);
assert.deepEqual(tiers(gainShell.body).S,['Gain A']);
assert.deepEqual(tiers(gainShell.body).D,['Gain Zero']);
assert.deepEqual(tiers(gainShell.body).F,['Gain Negative']);
equalRangeBoard(gainShell.body, '%');
assert.deepEqual(tiers(gainShell.body).unrated,['Gain No Control','Gain Missing']);
assert.match(metric(gainShell.body,'Gain Negative'), /-5\.00%/);
card(gainShell.body,'Gain A').click();
assert.equal(descendants(gainShell.body,'simc-benchmark-gain-row-content')[1].focused,true);
assert.equal(descendants(gainShell.body,'simc-benchmark-profile-details')[1].open,true);
assert.equal(descendants(gainShell.body,'simc-benchmark-spec-name').length,5);
assert.equal(JSON.stringify(gainPayload),gainBefore);
for (const bad of [false,true,'',NaN]) {
  const shell={body:element()};
  gain(shell,{results:{option_gain_rows:[{...gains[0],gain_percent:bad}]}});
  assert.deepEqual(tiers(shell.body).unrated,['Gain A']);
}
for (const values of [[11.43,5.82,0],[0,0],[-5,-5],[-1,-2,-3]]) {
  const shell = {body:element()};
  gain(shell,{results:{option_gain_rows:values.map((value,i)=>({...gains[0],spec_key:`gain ${i}`,spec_label:`gain ${i}`,gain_percent:value}))}});
  assert.deepEqual(tiers(shell.body).S, values.every(v=>v===values[0]) ? values.map((_,i)=>`gain ${i}`) : ['gain 0']);
  if (values[0]!==values[values.length-1]) {
    assert.deepEqual(tiers(shell.body).C,['gain 1']);
    assert.deepEqual(tiers(shell.body).F,['gain 2']);
    equalRangeBoard(shell.body, '%');
  }
}
const apl = coordinate(coord('APL',100,{spec_icon_url:'/icons/apl.jpg',candidates:[{key:'test',label:'APL candidate',dps:100},{key:'middle',label:'APL middle',dps:95},{key:'low',label:'APL low',dps:90}]}));
assert.deepEqual(tiers(apl).C,['APL middle']);
equalRangeBoard(apl, 'DPS');
assert.equal(descendants(apl,'simc-benchmark-tier-icon')[0].src,'https://wowdaily.cn/icons/apl.jpg','plain APL falls back to the coordinate API icon');
const mixedSpec={body:element()};
spec(mixedSpec,{spec_options:specOptions([coord('Selected'),coord('Other scenario')]),results:{coordinates:[coord('Selected',100),coord('Other scenario',10000,{scenario_key:'two'})]}});
assert.deepEqual(tiers(mixedSpec.body).S,['Selected'],'never grade against a different unselected scenario');
const mixedGain={body:element()};
gain(mixedGain,{results:{option_gain_rows:[gains[0],{...gains[0],spec_key:'Other scenario',spec_label:'Other scenario',scenario_key:'two',gain_percent:1000}]}});
assert.deepEqual(tiers(mixedGain.body).S,['Gain A']);

// Pure unit transport fixtures verify existing GET boundaries, not live data.
(async () => {
  const calls=[];
  let response;
  context.fetch=async (url,options) => { calls.push({url,options}); return {ok:true,json:async()=>response}; };
  const nextSpec=coord('New scenario',50,{scenario_key:'two'});
  const scenarioShell={body:element()};
  spec(scenarioShell,{spec_options:specOptions([specRows[0],nextSpec]),results:{coordinates:[specRows[0]],coordinate_options:[specRows[0],nextSpec]}},{detailUrl:'/detail/'});
  assert.equal(calls.length,0,'overview never eagerly fetches');
  const select=descendants(scenarioShell.body,'simc-benchmark-filter-select')[0];
  select.value='two'; response={results:{coordinates:[nextSpec]}};
  await select.handlers.change();
  assert.deepEqual(tiers(scenarioShell.body).S,['New scenario']);
  assert.equal(calls.length,1,'one existing selected GET on scenario change');
  assert.equal(calls[0].url,'/detail/?selected=1&scenario=two');
  card(scenarioShell.body,'New scenario').click();
  assert.equal(calls.length,1,'overview click only opens the original deferred details');
  const details=descendants(scenarioShell.body,'simc-benchmark-profile-details')[0];
  response={results:{coordinates:[{...nextSpec,profile_detail:{identity:{name:'Fixture'}}}]}};
  await details.handlers.toggle();
  assert.equal(calls.length,2);
  assert.equal(calls[1].url,'/detail/?detail=1&spec=New+scenario&talent=p&scenario=two');
  assert.ok(calls.every(call=>!call.options.method || call.options.method==='GET'));

  calls.length=0;
  const tankTwo=coord('Tank',200,{scenario_key:'two'}), healerTwo=coord('Healer',20,{scenario_key:'two'});
  const allRoleCoordinates=[...roleRows,tankTwo,healerTwo];
  context.window.location={origin:'https://wowdaily.cn',pathname:'/bench/',search:'?role=tank&scenario=one'};
  context.window.history={replaceState(_state,_title,url){context.window.location.search=url.slice(url.indexOf('?'));}};
  const scopedShell={body:element()};
  const scopedPayload={...rolePayload,results:{coordinates:roleRows,coordinate_options:allRoleCoordinates}};
  const scopedBefore=JSON.stringify(scopedPayload);
  spec(scopedShell,scopedPayload,{syncLocation:true,detailUrl:'/roles/'});
  assert.deepEqual(tiers(scopedShell.body).S,['Tank'],'URL selects tank');
  const scopedRole=descendants(scopedShell.body,'simc-benchmark-role-select')[0];
  const scopedScenario=descendants(scopedShell.body,'simc-benchmark-filter-select').find(el=>el.attributes['aria-label']==='场景');
  let resolveScenario;
  context.fetch=(url,options)=>{calls.push({url,options});return new Promise(resolve=>{resolveScenario=resolve;});};
  scopedScenario.value='two';const pending=scopedScenario.handlers.change();
  scopedRole.value='healer';scopedRole.handlers.change();
  assert.equal(cards(scopedShell.body).length,0,'never show old scenario while waiting under a new label');
  assert.equal(descendants(scopedShell.body,'simc-benchmark-spec-name').length,0);
  assert.equal(calls.length,1,'role changes do not fetch');
  assert.equal(calls[0].url,'/roles/?selected=1&scenario=two');
  resolveScenario({ok:true,json:async()=>({results:{coordinates:[tankTwo,healerTwo,...roleRows]}})});
  await pending;
  assert.deepEqual(tiers(scopedShell.body).S,['Healer'],'reply uses current role and excludes old scenario coordinates');
  assert.equal(descendants(scopedShell.body,'simc-benchmark-spec-dps')[0].textContent,'20 DPS');
  assert.match(context.window.location.search,/role=healer/);assert.match(context.window.location.search,/scenario=two/);
  scopedRole.value='tank';scopedRole.handlers.change();
  assert.deepEqual(tiers(scopedShell.body).S,['Tank']);
  assert.equal(descendants(scopedShell.body,'simc-benchmark-spec-dps')[0].textContent,'200 DPS');
  assert.equal(calls.length,1);assert.equal(JSON.stringify(scopedPayload),scopedBefore);
  const directShell={body:element()};
  context.window.location.search='?role=tank&scenario=two';
  spec(directShell,{...rolePayload,results:{coordinates:allRoleCoordinates}},{syncLocation:true});
  assert.deepEqual(tiers(directShell.body).S,['Tank']);
  assert.equal(descendants(directShell.body,'simc-benchmark-spec-dps')[0].textContent,'200 DPS','URL scenario and role scope the entire original table');
  const emptyRoleShell={body:element()};
  context.window.location.search='?role=dps&scenario=two';
  spec(emptyRoleShell,{...rolePayload,results:{coordinates:allRoleCoordinates}},{syncLocation:true});
  assert.equal(cards(emptyRoleShell.body).length,0);assert.match(text(emptyRoleShell.body),/暂无/);
  scopedScenario.value='one';const stale=scopedScenario.handlers.change();const resolveStale=resolveScenario;
  scopedScenario.value='two';const latest=scopedScenario.handlers.change();
  assert.equal(calls[1].options.signal.aborted,true,'superseded scenario generation is aborted');
  resolveScenario({ok:true,json:async()=>({results:{coordinates:[tankTwo]}})});await latest;
  resolveStale({ok:true,json:async()=>({results:{coordinates:roleRows}})});await stale;
  assert.equal(descendants(scopedShell.body,'simc-benchmark-spec-dps')[0].textContent,'200 DPS','stale generation cannot replace current scenario');
  const legacyShell={body:element()};spec(legacyShell,{results:{coordinates:[coord('Legacy',100)]}});
  assert.deepEqual(tiers(legacyShell.body).unrated,['Legacy']);assert.deepEqual(tiers(legacyShell.body).S,[]);
  card(legacyShell.body,'Legacy').click();assert.equal(descendants(legacyShell.body,'simc-benchmark-profile-details')[0].open,true);
  context.window.location.search='';
  context.fetch=async (url,options)=>{calls.push({url,options});return {ok:true,json:async()=>response};};
  calls.length=0;
  const nextGain={...gains[0],spec_key:'New gain',spec_label:'New gain',scenario_key:'two',gain_percent:3};
  const gainsShell={body:element()};
  context.api.gainScenarios(gainsShell,{results:{option_gain_rows:[gains[0]],coordinate_options:[specRows[0],nextSpec]}},{detailUrl:'/gain/'});
  const gainSelect=descendants(gainsShell.body,'simc-benchmark-filter-select')[0];
  gainSelect.value='two'; response={results:{option_gain_rows:[nextGain]}};
  await gainSelect.handlers.change();
  assert.deepEqual(tiers(gainsShell.body).S,['New gain']);
  assert.equal(calls.length,1); assert.equal(calls[0].url,'/gain/?selected=1&scenario=two');

  calls.length=0;
  const gearOne=coord('Gear spec',100,{candidates:[{key:'gear-one',label:'First item',dps:100}]});
  const gearTwo={...gearOne,scenario_key:'two',candidates:[{key:'gear-two',label:'Second item',dps:50}]};
  const gearShell={body:element()};
  context.api.results(gearShell,{results:{coordinates:[gearOne],coordinate_options:[gearOne,gearTwo]}},{detailUrl:'/gear/'});
  const gearSelect=descendants(gearShell.body,'simc-benchmark-filter-select').find(el=>el.dataset.dimension==='scenario_key');
  gearSelect.value='two'; response={results:{coordinates:[gearTwo],coordinate_options:[gearOne,gearTwo]}};
  await gearSelect.handlers.change();
  assert.deepEqual(tiers(gearShell.body).S,['Second item']);
  assert.equal(calls.length,1);
  const request=new URL(calls[0].url,'https://wowdaily.cn');
  assert.equal(request.searchParams.get('selected'),'1');
  assert.equal(request.searchParams.get('spec'),'Gear spec');
  assert.equal(request.searchParams.get('talent'),'p');
  assert.equal(request.searchParams.get('scenario'),'two');
  console.log('通过：全部真实renderer统一等距七档/动态数值区间/精确边界/未知与零负有效值/最佳变体和权威收益/完整tooltip/安全图标/键盘定位/输入不变/筛选重画与原按需GET（纯renderer单测，非生产验收）。');
})().catch(error=>{ console.error(error); process.exitCode=1; });
