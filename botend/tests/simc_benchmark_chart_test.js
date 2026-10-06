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
].map(candidate=>({...candidate, comparison_mode:'equipment_effect', effect_delta_percent: candidate.baseline_dps > 0 ? Math.max(0,(candidate.dps-candidate.baseline_dps)*100/candidate.baseline_dps) : null}));
const effectChart = portal(effectCandidates, {dps:100}, {lowest:90,highest:300,range:210});
assert.deepEqual(descendants(effectChart,'simc-benchmark-gear-name').map(n=>n.textContent), ['B','A','C','D','E']);
const metrics = descendants(effectChart,'simc-benchmark-candidate-value').map(n=>n.textContent);
assert.match(metrics[0], /\+60\.00%.*特效提升/);
assert.match(metrics[1], /\+21\.05%.*特效提升/);
assert.match(metrics[2], /\+0\.00%/);
assert.match(metrics[3], /\+0\.00%/);
assert.match(metrics[4], /无特效对照/);
const effectRows = descendants(effectChart,'simc-benchmark-gear-row');
const rowAText = descendants(effectRows[1],'simc-benchmark-relative').map(n=>n.textContent).join(' ');
assert.match(rowAText, /230/); assert.match(rowAText, /340/);
const endpoints = descendants(effectChart,'simc-benchmark-gear-segment');
assert.match(endpoints[0].attributes['aria-label'], /特效提升/);
assert.ok(endpoints.every(segment=>segment.style.minWidth==='0'&&segment.style.padding==='0'&&segment.style.boxSizing==='border-box'));
// 特效展示从零开始；负值已由后端归零，原始 DPS 保持不变。
assert.equal(parseFloat(endpoints[0].style.left),0);
assert.equal(parseFloat(endpoints[0].style.width),100);
const smallChart=portal([
  {key:'small',item_id:101,label:'Small',effect_delta_percent:0.5,dps:100.5,baseline_dps:100,comparison_mode:'equipment_effect'},
  {key:'large',item_id:102,label:'Large',effect_delta_percent:8,dps:108,baseline_dps:100,comparison_mode:'equipment_effect'},
],{dps:100});
const smallSegments=descendants(smallChart,'simc-benchmark-gear-segment');
assert.equal(parseFloat(smallSegments[0].style.width),100);
assert.equal(parseFloat(smallSegments[1].style.width),25);
assert.match(descendants(smallChart,'simc-benchmark-candidate-value')[1].textContent,/\+0\.50%/);
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
assert.deepEqual(backendNames,['B','A','C','D','E']);
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
const conditional={...effectCandidates[0],comparison_kind:'conditional_increment',comparison_baseline_label:'保留 neck 特效，仅关闭 waist 特效',effect_validation:{status:'valid'}};
const conditionalChart=portal([conditional],{dps:100});
assert.match(descendants(conditionalChart,'simc-benchmark-candidate-value')[0].textContent,/条件增量/);
assert.ok(descendants(conditionalChart,'simc-benchmark-relative').some(n=>n.textContent.includes('保留 neck')));
assert.match(descendants(conditionalChart,'simc-benchmark-gear-segment')[0].attributes['aria-label'],/条件增量/);
const conditionalDashboard=dashboard([{candidate:conditional,coordinate:{},dps:conditional.dps,baseline_dps:conditional.baseline_dps}]);
assert.match(descendants(conditionalDashboard,'benchmark-aggregate-dps')[0].textContent,/条件增量/);
const pendingConditional=portal([{...conditional,effect_validation:{status:'pair_pending'}}],{dps:100});
assert.equal(descendants(pendingConditional,'simc-benchmark-gear-segment').length,0);
assert.doesNotMatch(descendants(pendingConditional,'simc-benchmark-candidate-value')[0].textContent,/%/);
// Consume the authoritative display field, never re-derive a raw negative/noisy gain.
for (const [dps, display] of [[900,0],[1000,0],[1000.999,0],[1001,0.1],[1002,0.2]]) {
 const candidate={key:'floor',item_id:10,label:'Floor',item_level:321,dps,baseline_dps:1000,
   comparison_mode:'equipment_effect',effect_delta_percent:display,effect_validation:{status:'valid'}};
 const before=JSON.stringify(candidate);
 for(const [render,prefix,metric] of [[c=>portal([c],{dps:1000}),'simc-benchmark','simc-benchmark-candidate-value'],
   [c=>dashboard([{candidate:c,coordinate:{},dps:c.dps,baseline_dps:c.baseline_dps}]),'benchmark','benchmark-aggregate-dps']]) {
  const chart=render(candidate),segment=descendants(chart,`${prefix}-gear-segment`)[0];
  assert.equal(parseFloat(segment.style.left),0);
  assert.equal(parseFloat(segment.style.width)===0,display===0);
  assert.ok(descendants(chart,metric)[0].textContent.includes(`+${display.toFixed(2)}%`));
 }
 assert.equal(JSON.stringify(candidate),before);
}
console.log('通过：v2特效与v3条件增量标签/保留上下文对照；未验证条件对比不展示收益；既有排序与普通DPS语义保持。');
