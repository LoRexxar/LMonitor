const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readSource} = require('./vm_harness');
const source = readSource('static/portal/js/main.js');

test("loading: bounded concurrency, failures, timeouts, incremental search", async () => {
// 使用真实首页加载逻辑验证有界并发、导航故障隔离、超时与增量搜索。
const context = vm.createContext({console, URL, AbortController, setTimeout, clearTimeout,
  document: {addEventListener() {}, getElementById() { return null; }}});
vm.runInContext(source, context);
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
  const pending = new Map(), started = [];
  let active = 0, peak = 0, bound = false, navigation;
  context.loadSection = key => {
    started.push(key); active++; peak = Math.max(active, peak);
    return new Promise((resolve, reject) => pending.set(key, {resolve, reject}))
      .finally(() => { active--; });
  };
  context.loadTools = () => new Promise((resolve, reject) => { navigation = {resolve, reject}; });
  context.bindSearch = () => { bound = true; };
  context.bindExwindSourceTabs = context.renderExwindSourceTabs = context.updateSearchMeta = () => {};
  const loading = context.loadAll();
  await flush();
  assert.equal(bound, true, '导航未完成时搜索也应可用');
  assert.deepEqual(started, ['today_in_wow', 'daily_report', 'blueposts']);
  pending.get('daily_report').reject(Error('模拟板块失败'));
  navigation.reject(Error('模拟导航失败'));
  await flush();
  assert.equal(started[3], 'exwind', '空出的并发位置应马上加载后续板块');
  // 首个板块持续未完成，其余板块仍应全部加载。
  while (started.length < 11) {
    for (const [key, task] of pending) if (key !== 'today_in_wow') task.resolve();
    await flush();
  }
  assert.equal(new Set(started).size, 11);
  assert.equal(peak, 3, '内容板块最多并发三个');
  for (const task of pending.values()) task.resolve();
  await loading;

  let abortTimer, cleared = 0;
  context.setTimeout = fn => { abortTimer = fn; return 1; };
  context.clearTimeout = () => { cleared++; };
  context.fetch = (_url, {signal}) => new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(Error('超时'))));
  const timeout = context.fetchJson('/slow');
  abortTimer();
  await assert.rejects(timeout, /超时/);
  assert.equal(cleared, 1);
  context.fetch = async () => ({ok: false});
  await assert.rejects(context.fetchJson('/failed'), /未成功/);
  assert.equal(cleared, 2);
  context.fetch = async () => ({ok: true, json: async () => ({data: []})});
  assert.equal((await context.fetchJson('/good')).data.length, 0);

  // 另一个环境执行真实板块完成逻辑，核对搜索结果随数据到达更新。
  const elements = new Map([['wowhead-list', {}], ['portal-search-meta', {}]]);
  const incremental = vm.createContext({console, URL, document: {addEventListener() {}, getElementById(id) { return elements.get(id) || null; }}});
  vm.runInContext(source, incremental);
  incremental.fetchJson = async () => ({data: [{title:'法师新内容'}, {title:'战士新内容'}]});
  incremental.renderSimpleList = incremental.renderTodayStrip = incremental.renderDailyReportCard = () => {};
  vm.runInContext('PORTAL_STATE.query = "法师";', incremental);
  await incremental.loadSection('wowhead');
  assert.equal(elements.get('portal-search-meta').textContent, '过滤结果：1/2');
  incremental.fetchJson = async () => { throw Error('模拟失败'); };
  await incremental.loadSection('nga');
  assert.equal(elements.get('portal-search-meta').textContent, '过滤结果：1/2');
  let oldData;
  incremental.fetchJson = () => new Promise(resolve => { oldData = resolve; });
  const oldSection = incremental.loadSection('wowhead');
  incremental.fetchJson = async () => ({data: [{title:'法师最新内容'}]});
  await incremental.loadSection('wowhead');
  oldData({data: [{title:'战士旧内容'}]});
  await oldSection;
  assert.equal(elements.get('portal-search-meta').textContent, '过滤结果：1/1', '迟到的旧请求不得覆盖新数据');
  incremental.fetchJson = async () => ({data: [], snapshot: {state: 'pending', message: '新闻数据正在准备，请稍后重试。'}});
  await incremental.loadSection('wowhead');
  assert.match(elements.get('wowhead-list').innerHTML, /新闻数据正在准备/);
  const failureElements = new Map([['wow-today-panel', {dataset:{}}], ['featured-news-card', {}]]);
  const failureUI = vm.createContext({console, URL, document: {addEventListener() {}, getElementById(id) { return failureElements.get(id) || null; }}});
  vm.runInContext(source, failureUI);
  failureUI.renderTodayStrip = () => {};
  failureUI.fetchJson = async () => { throw Error('模拟超时'); };
  await failureUI.loadSection('today_in_wow');
  await failureUI.loadSection('daily_report');
  assert.match(failureElements.get('wow-today-panel').innerHTML, /暂时无法读取/);
  assert.match(failureElements.get('featured-news-card').innerHTML, /暂时无法读取/);
  failureUI.fetchJson = async () => ({data: null});
  await failureUI.loadSection('daily_report');
  assert.match(failureElements.get('featured-news-card').innerHTML, /正在准备中/);
  console.log('首页加载：并发上限、慢板块隔离、导航失败、超时恢复和增量搜索通过。');


});

test("rankings: local filtering, search, pending and safe player links", async () => {
// 使用真实页面脚本验证本地副本筛选、搜索和准备状态。
const elements = new Map();
function element() {
  return {innerHTML: '', value: '', textContent: '', listeners: {},
    querySelectorAll() { return []; },
    addEventListener(name, handler) { this.listeners[name] = handler; },
    replaceChildren(note) { this.innerHTML = note.textContent; },
    prepend(note) { this.innerHTML = note.textContent + this.innerHTML; }};
}
for (const id of ['mplus-controls', 'mplus-dungeon-select', 'mplus-rankings', 'portal-search-meta']) elements.set(id, element());
const context = vm.createContext({console, URL,
  fetch() { throw new Error('本地筛选不得请求网络'); },
  document: {addEventListener() {}, getElementById(id) { return elements.get(id) || null; }, createElement: element}});
vm.runInContext(source, context);
context.payload = {snapshot: {state: 'ready'}, dungeons: [{slug: 'alpha', name_cn: '副本甲'}, {slug: 'beta', name_cn: '副本乙'}],
  items: [{dungeon_slug: 'alpha', dungeon_cn: '副本甲', level: 30}, {dungeon_slug: 'beta', dungeon_cn: '副本乙', level: 31}],
  runs_by_dungeon: {alpha: [{dungeon_cn: '副本甲', level: 30}, {dungeon_cn: '副本甲', level: 29}],
                    beta: [{dungeon_cn: '副本乙', level: 31}]}};
vm.runInContext('PORTAL_STATE.mplusRankingsPayload = payload; renderMplusControls(payload.dungeons); renderMplusSelection();', context);
assert.match(elements.get('mplus-rankings').innerHTML, /副本甲/);
assert.match(elements.get('mplus-rankings').innerHTML, /副本乙/);
const select = elements.get('mplus-dungeon-select');
select.value = 'alpha';
select.listeners.change();
assert.match(elements.get('mplus-rankings').innerHTML, /副本甲/);
assert.doesNotMatch(elements.get('mplus-rankings').innerHTML, /副本乙/);
assert.match(elements.get('mplus-rankings').innerHTML, /\+29/);
vm.runInContext('PORTAL_STATE.query = "副本乙"; renderMplusSelection();', context);
assert.match(elements.get('mplus-rankings').innerHTML, /无匹配结果/);
select.value = 'beta';
select.listeners.change();
assert.match(elements.get('mplus-rankings').innerHTML, /副本乙/);
vm.runInContext('PORTAL_STATE.query = ""; PORTAL_STATE.mplusRankingsPayload = {snapshot: {state:"pending",message:"数据准备中"}}; renderMplusSelection();', context);
assert.equal(elements.get('mplus-rankings').innerHTML, '数据准备中');
context.runItems = [{dungeon_cn: '副本甲', level: 30,
  run_url: 'https://raider.io/mythic-plus-runs/123', party: [
    {name: 'Linked', role: 'tank', class_slug: 'warrior', spec_icon_url: '/icon.jpg',
      character_url: '/portal/character/?region=eu&realm=test&name=Linked'},
    {name: 'Unknown', role: 'healer', profile_url: 'https://raider.io/characters/eu/test/Unknown'},
    {name: '<Unsafe>', role: 'dps', character_url: 'javascript:alert(1)'}]}];
vm.runInContext('renderMplusRuns("mplus-rankings", runItems)', context);
const runHtml = elements.get('mplus-rankings').innerHTML;
const memberLink = runHtml.match(/<a href="\/portal\/character\/[^>]+>[\s\S]*?<\/a>/g);
assert.equal(memberLink.length, 1);
assert.match(memberLink[0], /<img[^>]+src="\/icon.jpg"/);
assert.match(memberLink[0], />Linked<\/span>/);
assert.doesNotMatch(memberLink[0], /target=/);
assert.match(runHtml, /href="https:\/\/raider.io\/mythic-plus-runs\/123" target="_blank"/);
assert.doesNotMatch(runHtml, /href="[^"]*Unknown|javascript:|<Unsafe>/);
assert.match(runHtml, /&lt;Unsafe&gt;/);
elements.set('peak-test', element());
context.peak = {items: [{class_slug: 'mage', spec_slug: 'fire', aggregate_url: '/wrong-aggregate/', items: [
  {name: 'First', character_url: '/portal/character/?player_id=1'},
  {name: 'Second', character_url: '/portal/character/?player_id=2'},
  {name: 'Unresolved'}, {name: 'Fourth', character_url: '/portal/character/?player_id=4'}]}]};
vm.runInContext('renderPeakSpecGrid("peak-test", peak)', context);
const peakHtml = elements.get('peak-test').innerHTML;
assert.match(peakHtml, /href="\/portal\/character\/\?player_id=1"/);
assert.match(peakHtml, /href="\/portal\/character\/\?player_id=2"/);
assert.match(peakHtml, />Unresolved<\/span>/);
assert.doesNotMatch(peakHtml, /wrong-aggregate|Fourth|target="_blank"/);
console.log('副本本地筛选、搜索、缺失状态、巅峰逐人物链接及阵容图标姓名共享入口验证通过');

});

test("today digest: selection, visibility, expansion and escaping", async () => {
// 验证今日重点的信息筛选、原有内容保留与展开行为。
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
vm.runInContext(source, context);
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

});
