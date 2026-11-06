const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readSource} = require('./vm_harness');

test('新闻区复用首页容器宽度，不另设桌面固定上限，更新样式缓存版本', () => {
  const css = readSource('static/portal/css/home-news.css');
  const root = css.match(/\.home-news\s*\{([^}]*)\}/)?.[1] || '';
  assert.match(root, /(?:^|;)\s*width:\s*100%\s*;/);
  assert.match(root, /(?:^|;)\s*min-width:\s*0\s*;/);
  assert.doesNotMatch(root, /(?:^|;)\s*max-width:\s*\d+(?:px|rem)\b/);
  const template = readSource('templates/portal/index.html');
  assert.match(template, /href="{% static 'portal\/css\/home-news\.css' %}\?v=[a-zA-Z0-9_]+"/);
  assert.doesNotMatch(template, /home-news\.css' %}\?v=20261106_(?:source_dates_7|home_width_1)/);
});

test('新闻来源桌面左右双栏，窄屏恢复单栏且卡片可收缩', () => {
  const css = readSource('static/portal/css/home-news.css');
  const grid = css.match(/\.home-news-sources\s*\{([^}]*)\}/)?.[1] || '';
  assert.match(grid, /grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
  assert.match(grid, /align-items:\s*start/);
  assert.match(css, /\.home-news-source\s*\{[^}]*min-width:\s*0\s*;/);
  assert.match(css, /@media\s*\(max-width:\s*900px\)\s*\{\s*\.home-news-sources\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
});

function setup(localStorage) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {innerHTML: '', textContent: '', attrs: {},
      setAttribute(key, value) { this.attrs[key] = value; }});
    return elements.get(id);
  };
  for (const key of ['blueposts', 'blizzard_cn', 'exwind', 'nga_preview', 'wowhead']) {
    element(`${key}-list`); element(`news-count-${key}`);
    element(`news-source-${key}`); element(`news-jump-${key}`);
  }
  const context = vm.createContext({console, URL, Date, localStorage, document: {
    addEventListener() {}, getElementById(id) { return elements.get(id) || null; },
  }});
  vm.runInContext(readSource('static/portal/js/main.js'), context);
  context.renderTodayStrip = context.renderDailyReportCard = () => {};
  return {context, element};
}

function preferenceStorage() {
  const values = new Map();
  return {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)};
}

test('日期偏好跨页面保存，近7天在下次打开时随北京时间滚动', () => {
  const storage = preferenceStorage();
  const first = setup(storage).context;
  vm.runInContext('PORTAL_STATE.homeNews.preset = 7; PORTAL_STATE.homeNews.range = homeNewsPresetRange(7, new Date("2026-11-05T15:00:00Z")); saveHomeNewsPreferences()', first);
  const next = setup(storage).context;
  const saved = next.loadHomeNewsPreferences(new Date('2026-11-05T16:01:00Z'));
  assert.equal(saved.preset, 7);
  assert.equal(saved.range.start_date, '2026-10-31');
  assert.equal(saved.range.end_date, '2026-11-06');
  const url = new URL(next.homeNewsUrl('wowhead'), 'https://example.invalid');
  assert.equal(url.searchParams.get('start_date'), next.homeNewsPresetRange(7).start_date);
  assert.equal(vm.runInContext('PORTAL_STATE.homeNews.preset', next), 7);
});

test('自定义日期跨页面恢复固定范围，再选预设后不残留旧范围', () => {
  const storage = preferenceStorage();
  const first = setup(storage).context;
  vm.runInContext('PORTAL_STATE.homeNews.preset = null; PORTAL_STATE.homeNews.range = {start_date:"2020-10-30", end_date:"2020-11-02"}; saveHomeNewsPreferences()', first);
  const next = setup(storage).context;
  const url = new URL(next.homeNewsUrl('wowhead'), 'https://example.invalid');
  assert.equal(url.searchParams.get('start_date'), '2020-10-30');
  assert.equal(url.searchParams.get('end_date'), '2020-11-02');
  assert.equal(vm.runInContext('PORTAL_STATE.homeNews.preset', next), null);
  vm.runInContext('PORTAL_STATE.homeNews.preset = 1; saveHomeNewsPreferences()', next);
  assert.equal(setup(storage).context.loadHomeNewsPreferences().preset, 1);
});

test('损坏、非法及无法访问的本地记录回退近3天，不阻止日期切换', () => {
  const invalid = ['{', 'null', JSON.stringify({version:1, preset:99}),
    ...[
      {start_date:'2026-02-30', end_date:'2026-03-01'},
      {start_date:'2026-11-06', end_date:'2026-11-05'},
      {start_date:'2026-11-06', end_date:'2026-11-07'},
      {start_date:'bad', end_date:'2026-11-06'},
    ].map(range => JSON.stringify({version:1, preset:null, range}))];
  for (const value of invalid) {
    const context = setup({getItem: () => value}).context;
    const saved = context.loadHomeNewsPreferences(new Date('2026-11-06T00:00:00Z'));
    assert.equal(saved.preset, 3);
    assert.equal(saved.range.start_date, '2026-11-04');
  }
  const context = setup({getItem() { throw Error('禁止读取'); }, setItem() { throw Error('禁止写入'); }}).context;
  assert.equal(context.loadHomeNewsPreferences().preset, 3);
  assert.doesNotThrow(() => context.saveHomeNewsPreferences());
});

test('新闻按北京时间自然日查询，来源请求保持独立', () => {
  const {context} = setup();
  assert.equal(context.homeNewsToday(new Date('2026-11-05T16:00:00Z')), '2026-11-06');
  const range = context.homeNewsPresetRange(3, new Date('2026-11-01T16:01:00Z'));
  assert.equal(range.start_date, '2026-10-31');
  assert.equal(range.end_date, '2026-11-02');
  context.selectedRange = range;
  vm.runInContext('PORTAL_STATE.homeNews.range = selectedRange', context);
  for (const key of ['exwind', 'blizzard_cn', 'nga_preview']) {
    const url = new URL(context.homeNewsUrl(key), 'https://example.invalid');
    assert.equal(url.searchParams.get('source'), key);
    assert.equal(url.searchParams.get('start_date'), '2026-10-31');
    assert.equal(url.searchParams.get('end_date'), '2026-11-02');
    assert.equal(url.searchParams.has('limit'), false);
  }
});

test('同一天620条新闻全部渲染，长标题、来源与转义保留', async () => {
  const {context, element} = setup();
  const longTitle = '职业调整：多个专精的技能伤害、天赋效果与大秘境表现发生变化，完整细节必须保留到标题末尾';
  const rows = Array.from({length: 620}, (_, i) => ({id: i + 1, title: `英文标题 ${i}`,
    title_cn: `${longTitle} ${i}`, source: 'wowhead', publish_time: '2026-11-06 12:30:00', url: 'https://example.invalid/news'}));
  rows[0].title_cn = '<img src=x onerror=alert(1)>'; rows[0].url = 'javascript:alert(1)';
  rows[1].author = '新闻作者';
  rows[1].publish_time = '2026-11-05 09:10:00';
  context.fetchJson = async () => ({data: rows, snapshot: {state: 'ready'}});
  await context.loadSection('wowhead');
  const html = element('wowhead-list').innerHTML;
  assert.equal((html.match(/class="home-news-row"/g) || []).length, 620);
  assert.match(html, new RegExp(longTitle));
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<img|javascript:|portal-line-clamp|英文标题/);
  assert.match(html, /home-news-origin">Wowhead/);
  assert.match(html, /datetime="2026-11-06T12:30:00\+08:00"/);
  assert.doesNotMatch(html, /<details|<summary|home-news-day/);
  assert.match(html, /新闻作者<\/span>\s*<time[^>]+>2026-11-05 09:10<\/time>/);
  assert.equal(element('news-count-wowhead').textContent, '所选日期 · 620 条');
  assert.equal(element('wowhead-list').attrs['aria-busy'], 'false');
  vm.runInContext('PORTAL_STATE.query = "完整细节"', context);
  context.renderHomeNews('wowhead');
  assert.equal((element('wowhead-list').innerHTML.match(/class="home-news-row"/g) || []).length, 619);
});

test('切换日期立即清空旧新闻，迟到请求不覆盖新结果，失败可重试', async () => {
  const {context, element} = setup();
  let resolveOld;
  context.fetchJson = () => new Promise(resolve => { resolveOld = resolve; });
  const oldRequest = context.loadSection('wowhead');
  vm.runInContext('PORTAL_STATE.homeNews.range = {start_date:"2026-11-06", end_date:"2026-11-06"}', context);
  context.fetchJson = async () => ({data: [{id: 99, title: '新日期新闻', source: 'wowhead', publish_time: '2026-11-06 12:00:00'}]});
  await context.reloadHomeNews();
  resolveOld({data: [{title: '旧日期新闻', publish_time: '2026-11-01 12:00:00'}]});
  await oldRequest;
  assert.match(element('wowhead-list').innerHTML, /新日期新闻/);
  assert.doesNotMatch(element('wowhead-list').innerHTML, /旧日期新闻/);
  context.fetchJson = async () => { throw Error('模拟来源失败'); };
  await context.loadSection('wowhead');
  assert.match(element('wowhead-list').innerHTML, /data-home-news-retry="wowhead"/);
  assert.doesNotMatch(element('wowhead-list').innerHTML, /新日期新闻/);
  context.fetchJson = async () => ({data: [], snapshot: {state: 'ready'}});
  await context.loadSection('wowhead');
  assert.equal(element('wowhead-list').innerHTML, '');
  assert.equal(element('news-source-wowhead').hidden, true);
  assert.equal(element('news-jump-wowhead').hidden, true);
  context.fetchJson = async () => ({data: [], snapshot: {state: 'pending'}});
  await context.loadSection('wowhead');
  assert.match(element('wowhead-list').innerHTML, /新闻数据正在准备/);
  assert.equal(element('news-count-wowhead').textContent, '数据准备中');
  assert.equal(element('news-source-wowhead').hidden, false);
});

test('搜索无匹配时隐藏整个来源和跳转入口，清除搜索或切换日期后恢复', async () => {
  const {context, element} = setup();
  context.fetchJson = async () => ({data: [{id: 1, title: '职业调整', source: 'wowhead', publish_time: '2026-11-06 12:00:00'}]});
  await context.loadSection('wowhead');
  vm.runInContext('PORTAL_STATE.query = "没有此标题"', context);
  context.renderHomeNews('wowhead');
  assert.equal(element('news-source-wowhead').hidden, true);
  assert.equal(element('news-jump-wowhead').hidden, true);
  vm.runInContext('PORTAL_STATE.query = ""', context);
  context.renderHomeNews('wowhead');
  assert.equal(element('news-source-wowhead').hidden, false);
  assert.equal(element('news-jump-wowhead').hidden, false);
  assert.match(element('wowhead-list').innerHTML, /职业调整/);
  context.fetchJson = async () => ({data: []});
  await context.loadSection('wowhead');
  assert.equal(element('news-source-wowhead').hidden, true);
  context.fetchJson = async () => ({data: [{id: 2, title: '新日期新闻', source: 'wowhead'}]});
  await context.loadSection('wowhead');
  assert.equal(element('news-source-wowhead').hidden, false);
  assert.match(element('wowhead-list').innerHTML, /新日期新闻/);
});
