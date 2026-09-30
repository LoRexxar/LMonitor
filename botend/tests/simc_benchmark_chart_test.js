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
console.log('通过：公开页装等倒挂，以及后台正负收益和独立对照基准的色条均不反向覆盖。');
