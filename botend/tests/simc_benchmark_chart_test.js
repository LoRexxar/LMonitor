// 执行真实图表渲染器，防止装等与收益顺序不一致时发生反向覆盖。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function element() {
  return {children: [], style: {}, dataset: {}, attributes: {}, textContent: '',
    append(...children) { this.children.push(...children); },
    appendChild(child) { this.append(child); },
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener() {},
  };
}
function renderer(file, startup) {
  const source = fs.readFileSync(path.join(__dirname, '../../', file), 'utf8');
  const context = vm.createContext({document: {createElement: element}, Intl, URL});
  assert.ok(source.includes(startup));
  vm.runInContext(source.slice(0, source.indexOf(startup)) + 'globalThis.render = renderGearResultChart;})();', context);
  return context.render;
}
const portal = renderer('static/portal/js/simc-benchmarks.js', '  document.addEventListener("DOMContentLoaded", loadBenchmarks);');
const dashboard = renderer('static/dashboard/js/simc-benchmark-dashboard.js', "if(typeof module==='object'&&module.exports)");
function descendants(node, className) {
  return (node.children || []).flatMap(child => [
    ...(child.className === className ? [child] : []), ...descendants(child, className),
  ]);
}
function checkNoOverlap(segments) {
  const spans = segments.map(segment => ({left: parseFloat(segment.style.left), width: parseFloat(segment.style.width)})).sort((a,b) => a.left-b.left);
  for (let i=1; i<spans.length; i++) assert.ok(spans[i-1].left + spans[i-1].width <= spans[i].left + 1e-8, JSON.stringify(spans));
}
const levels = [321,328,334,340];
const candidates = values => values.map((dps,i) => ({key:`item-${levels[i]}`, item_id:1,
  item_variant_key:'同一装备', label:'测试装备', item_level:levels[i], dps}));
const live = candidates([83956.8,87286.5,84514.5,87883.8]);
const chart = portal(live, {dps:77000}, {lowest:77000, highest:90000, range:13000});
const segments = descendants(chart, 'simc-benchmark-gear-segment');
assert.deepEqual(segments.map(segment=>segment.textContent), ['321','334','328','340']);
checkNoOverlap(segments);
assert.equal(segments[1].style.backgroundColor, '#59a14f');
assert.match(segments[1].attributes['aria-label'], /334.*84,514\.5/);
// 后台按相对收益绘制，包含全正、全负、正负混合及独立对照基准。
for (const values of [[83956.8,87286.5,84514.5,87883.8],[90,80,95,85],[90,120,80,110]]) {
  const baseline = values[0]>1000 ? 77000 : 100;
  const rows = candidates(values).map(candidate=>({candidate, coordinate:{}, dps:candidate.dps, baseline_dps:baseline}));
  const dashboardSegments = descendants(dashboard(rows), 'benchmark-gear-segment');
  assert.equal(dashboardSegments.length, 4);
  checkNoOverlap(dashboardSegments);
}
const rows = candidates([120,140,160,180]).map((candidate,i)=>({candidate, coordinate:{},
  dps:candidate.dps, baseline_dps:[100,100,200,140][i]}));
checkNoOverlap(descendants(dashboard(rows), 'benchmark-gear-segment'));
// 特效排名必须按独立无特效对照的收益，而非总 DPS；同装备也选最高收益装等。
const effectCandidates = [
  {key:'a-321', item_id:1, label:'A', item_level:321, dps:240, baseline_dps:200},
  {key:'a-340', item_id:1, label:'A', item_level:340, dps:230, baseline_dps:190},
  {key:'b-340', item_id:2, label:'B', item_level:340, dps:160, baseline_dps:100},
  {key:'c-340', item_id:3, label:'C', item_level:340, dps:90, baseline_dps:100},
  {key:'d-340', item_id:4, label:'D', item_level:340, dps:97, baseline_dps:100},
  {key:'e-340', item_id:5, label:'E', item_level:340, dps:300, baseline_dps:0},
].map(candidate=>({...candidate, comparison_mode:'equipment_effect'}));
const effectChart = portal(effectCandidates, {dps:100}, {lowest:90,highest:300,range:210});
assert.deepEqual(descendants(effectChart,'simc-benchmark-gear-name').map(n=>n.textContent), ['B','A','D','C','E']);
const metrics = descendants(effectChart,'simc-benchmark-candidate-value').map(n=>n.textContent);
assert.match(metrics[0], /\+60\.00%.*特效提升/);
assert.match(metrics[1], /\+21\.05%.*特效提升/);
assert.match(metrics[2], /-3\.00%/);
assert.match(metrics[3], /-10\.00%/);
assert.match(metrics[4], /无特效对照/);
const effectRows = descendants(effectChart,'simc-benchmark-gear-row');
const rowAText = descendants(effectRows[1],'simc-benchmark-relative').map(n=>n.textContent).join(' ');
assert.match(rowAText, /230/); assert.match(rowAText, /340/);
const endpoints = descendants(effectChart,'simc-benchmark-gear-segment');
assert.match(endpoints[0].attributes['aria-label'], /特效提升/);
assert.ok(endpoints.every(segment=>segment.style.minWidth==='0'&&segment.style.padding==='0'&&segment.style.boxSizing==='border-box'));
// 60% 是最长正向色条；零点来自 -10%~60% 的实际收益范围，不是 90~300 DPS。
assert.ok(Math.abs(parseFloat(endpoints[0].style.left) - 100*10/70) < 1e-8);
assert.ok(Math.abs(parseFloat(endpoints[0].style.width) - 100*60/70) < 1e-8);
assert.equal(descendants(effectRows[4],'simc-benchmark-gear-segment').length,0);
checkNoOverlap(descendants(effectRows[1],'simc-benchmark-gear-segment'));
for(const values of [[100,100],[95,90],[110,90]]){
 const variants=candidates(values).map(c=>({...c,baseline_dps:100,comparison_mode:'equipment_effect'}));
 const chart=portal(variants,{dps:100},{lowest:0,highest:1,range:1});
 for(const segment of descendants(chart,'simc-benchmark-gear-segment')){
  assert.ok(Number.isFinite(parseFloat(segment.style.left)));
  assert.ok(Number.isFinite(parseFloat(segment.style.width)));
 }
}
const backendRows=effectCandidates.map(candidate=>({candidate,coordinate:{},dps:candidate.dps,baseline_dps:candidate.baseline_dps}));
const backendChart=dashboard(backendRows);
const backendNames=descendants(backendChart,'benchmark-gear-identity-text').map(n=>n.children[0].textContent);
assert.deepEqual(backendNames,['B','A','D','C','E']);
assert.match(descendants(backendChart,'benchmark-aggregate-dps')[0].textContent,/\+60\.00%.*特效提升/);
assert.match(descendants(backendChart,'benchmark-aggregate-delta')[1].textContent,/230/);
assert.equal(descendants(descendants(backendChart,'benchmark-gear-row')[4],'benchmark-gear-segment').length,0);
assert.ok(descendants(backendChart,'benchmark-gear-segment').every(segment=>segment.style.minWidth==='0'&&segment.style.padding==='0'&&segment.style.boxSizing==='border-box'));
const invalid = {...effectCandidates[2], item_id:6, label:'Invalid effect', dps:999,
  effect_validation:{status:'invalid',reason:'目标特效未加载'}};
const invalidChart=portal([...effectCandidates,invalid],{dps:100},{lowest:0,highest:1,range:1});
const invalidRows=descendants(invalidChart,'simc-benchmark-gear-row');
assert.equal(descendants(invalidChart,'simc-benchmark-gear-name').at(-1).textContent,'Invalid effect');
assert.match(descendants(invalidRows.at(-1),'simc-benchmark-candidate-value')[0].textContent,/特效结果无效/);
assert.equal(descendants(invalidRows.at(-1),'simc-benchmark-gear-segment').length,0);
assert.equal(descendants(invalidRows.at(-1),'simc-benchmark-gear-rank')[0].textContent,'—');
console.log('通过：特效百分比排序/绘图、最高收益装等、负收益及缺失对照；普通DPS图表保持原有语义。');
