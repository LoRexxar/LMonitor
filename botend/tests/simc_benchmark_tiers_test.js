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
const entry = (label, value, extra = {}) => ({label, value, metricLabel: 'DPS', ...extra});
const borderEntries = [100, 95, 94.999, 90, 89.999, 85, 84.999, 80, 79.999, 75, 74.999, 70, 69.999, 0, -5].map(v => entry(String(v), v));
const borderBefore = JSON.stringify(borderEntries);
const borders = overview(borderEntries);
assert.deepEqual(tiers(borders), {S:['100','95'], A:['94.999','90'], B:['89.999','85'], C:['84.999','80'], D:['79.999','75'], E:['74.999','70'], F:['69.999','0','-5']});
assert.equal(JSON.stringify(borderEntries), borderBefore);
assert.match(text(borders), /当前.*最高正/);
assert.match(text(borders), /不代表.*整体.*强度/);
for (const value of [null, undefined, '', ' ', false, true, NaN, Infinity, {}, [], 'invalid']) {
  const board = overview([entry('Leader', 100), entry('Unknown', value, {status: '结果待确认'})]);
  assert.deepEqual(tiers(board).unrated, ['Unknown']);
  assert.match(text(card(board, 'Unknown')), /结果待确认/);
  assert.doesNotMatch(metric(board, 'Unknown'), /0 DPS/);
}
for (const values of [[0, 0], [-1, -2], [null, false]]) {
  const board = overview(values.map((v,i) => entry(`v${i}`, v)));
  assert.deepEqual(tiers(board).S, []);
  assert.equal(tiers(board).unrated.length, 2);
  assert.match(text(board), /无正值参照|暂无有效结果/);
}
assert.match(text(overview([])), /暂无有效结果/);

const tooltip = '完整装备\n物品等级 321\n+123 急速\n装备：造成 456 点伤害并提高属性。';
const items = [
  {key:'a-low', item_id:1, label:'A', item_level:321, dps:100, tooltip, icon_url:'/icons/a.jpg'},
  {key:'a-high', item_id:1, label:'A', item_level:340, dps:200, tooltip},
  {key:'b', item_id:2, label:'B', item_level:340, dps:190},
];
const itemsBefore = JSON.stringify(items);
const ordinary = gear(items, {dps:80});
assert.equal(cards(ordinary).length, 2, 'one icon per original grouped item, not per item level');
assert.deepEqual(tiers(ordinary).S, ['A','B']);
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
assert.deepEqual(tiers(displayChart).A,['Raw high'],'ordinary gear reuses the existing display DPS ranking, not raw ordering');
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
const specPayload = {results:{coordinates:specRows}};
const specBefore = JSON.stringify(specPayload), specShell = {body:element()};
spec(specShell, specPayload);
assert.deepEqual(tiers(specShell.body).S, ['Spec A']); assert.deepEqual(tiers(specShell.body).E, ['Spec B']);
assert.deepEqual(tiers(specShell.body).unrated, ['Missing','Boolean']);
assert.equal(descendants(specShell.body, 'simc-benchmark-spec-name').length, 4);
card(specShell.body, 'Spec A').click();
const specOriginal = descendants(specShell.body, 'simc-benchmark-spec-row-toggle')[0];
assert.equal(specOriginal.focused, true);
assert.equal(descendants(specShell.body, 'simc-benchmark-profile-details')[0].open, true);
assert.equal(specOriginal.attributes['aria-expanded'], 'true');
assert.equal(JSON.stringify(specPayload), specBefore);
const rawSpecShell={body:element()};
spec(rawSpecShell,{results:{coordinates:[coord('Raw spec',100,{candidates:[{key:'baseline',dps:100,display_dps:1}]}),coord('Other spec',95)]}});
assert.deepEqual(tiers(rawSpecShell.body).S,['Raw spec','Other spec'],'spec comparison retains its raw baseline DPS metric');

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
assert.deepEqual(tiers(gainShell.body).F,['Gain Zero','Gain Negative']);
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
const apl = coordinate(coord('APL',100,{spec_icon_url:'/icons/apl.jpg',candidates:[{key:'test',label:'APL candidate',dps:100}]}));
assert.equal(descendants(apl,'simc-benchmark-tier-icon')[0].src,'https://wowdaily.cn/icons/apl.jpg','plain APL falls back to the coordinate API icon');
const mixedSpec={body:element()};
spec(mixedSpec,{results:{coordinates:[coord('Selected',100),coord('Other scenario',10000,{scenario_key:'two'})]}});
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
  spec(scenarioShell,{results:{coordinates:[specRows[0]],coordinate_options:[specRows[0],nextSpec]}},{detailUrl:'/detail/'});
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
  console.log('通过：三类真实renderer的S–F边界/未知与非正参照/最佳变体和权威收益/完整tooltip/安全图标/键盘定位/输入不变/筛选重画与原按需GET（纯renderer单测，非生产验收）。');
})().catch(error=>{ console.error(error); process.exitCode=1; });
