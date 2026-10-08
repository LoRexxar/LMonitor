// 使用真实首页加载逻辑验证有界并发、导航故障隔离、超时与增量搜索。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../static/portal/js/main.js'), 'utf8');
const context = vm.createContext({console, URL, AbortController, setTimeout, clearTimeout,
  document: {addEventListener() {}, getElementById() { return null; }}});
vm.runInContext(source, context);
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
(async () => {
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
})().catch(error => { console.error(error); process.exitCode = 1; });
