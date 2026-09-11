// 验证今日重点的信息筛选、原有内容保留与展开行为。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const strip = { hidden: false };
const more = { addEventListener(_event, handler) { this.click = handler; } };
const items = {
  innerHTML: '', dataset: {}, addEventListener() {},
  insertAdjacentHTML(_where, html) { this.innerHTML += html; },
  querySelector() { return more; },
};
const context = vm.createContext({
  URL, console,
  document: {
    addEventListener() {},
    getElementById(id) { return id === 'portal-today-strip' ? strip : items; },
  },
});
vm.runInContext(fs.readFileSync(path.join(root, 'static/portal/js/main.js'), 'utf8'), context);
function render(updates, sections = {}) {
  context.updates = updates;
  context.sections = sections;
  vm.runInContext(`
    PORTAL_STATE.todayUpdates = updates;
    PORTAL_STATE.dataBySection = sections;
    PORTAL_STATE.todayUpdatesSettled = true;
    PORTAL_STATE.todayExpanded = false;
    PORTAL_STATE.todaySourcesSettled = {blueposts:true, exwind:true, wowhead:true, videos:true, mplus_cutoffs:true};
    renderTodayStrip();
  `, context);
  return items.innerHTML;
}
const today = {key:'skill_diffs', label:'改动挖掘', headline:'3 职业 · 17 项技能改动', status:'today', url:'/portal/wow-skill-diff/1/'};
let html = render([today, {key:'gear', label:'装备目录', status:'older'}, {key:'mdt', label:'MDT', status:'empty'}]);
assert.match(html, /3 职业 · 17 项技能改动/);
assert.match(html, /href="\/portal\/wow-skill-diff\/1\/"/);
assert.doesNotMatch(html, /装备目录|MDT|暂无数据|portal-update-item/);
html = render([today], {mplus_cutoffs:[{region:'cn', cutoff_1:3000}], wowhead:[{title:'职业调整新闻'}]});
assert.match(html, /国服 1% 3000.00/);
assert.match(html, /Wowhead 新闻/);
assert.ok(html.indexOf('国服 1%') < html.indexOf('改动挖掘'));
render([]);
assert.equal(strip.hidden, true);
render([today]);
assert.equal(strip.hidden, false);
html = render(Array.from({length:8}, (_, index) => ({...today, label:`更新${index}`})));
assert.equal((html.match(/<a /g) || []).length, 6);
assert.match(html, /另 2 条重点/);
more.click();
assert.equal((items.innerHTML.match(/<a /g) || []).length, 8);
assert.match(items.innerHTML, /收起/);
more.click();
assert.equal((items.innerHTML.match(/<a /g) || []).length, 6);
html = render([{...today, headline:'<img src=x onerror=alert(1)>'}]);
assert.doesNotMatch(html, /<img/);
assert.match(html, /&lt;img/);
console.log('今日重点：筛选、原有内容、空状态、展开收起与转义验证通过');
