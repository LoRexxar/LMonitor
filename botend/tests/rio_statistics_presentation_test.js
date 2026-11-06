const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readSource} = require('./vm_harness');

// Unit-only DOM doubles; layout, authentication and actual payloads are verified
// separately in Playwright against the deployed page.
function setup() {
  const nodes = new Map(), urls = [], requests = [];
  function node() {
    return {textContent: '', value: '', hidden: false, disabled: false,
      selectedOptions: [], options: [], dataset: {}, style: {}, children: [], clientWidth: 720,
      addEventListener() {}, setAttribute(k, v) { this[k] = v; },
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      parentElement: {clientWidth: 720}, getContext: () => ({scale() {}, fillRect() {}}),
    };
  }
  const get = name => {
    if (!nodes.has(name)) nodes.set(name, node());
    return nodes.get(name);
  };
  const context = vm.createContext({
    document: {getElementById: id => get(id.replace(/^rio-/, '')),
      querySelectorAll: () => [], documentElement: {}, createElement: node, createElementNS: node},
    location: {search: '', pathname: '/portal/rio-statistics/'},
    history: {replaceState(_state, _title, url) { urls.push(url); }}, URLSearchParams, AbortController,
    MutationObserver: class { observe() {} },
    window: {addEventListener() {}}, setInterval() {},
    fetch() { throw new Error('presentation must not request new data'); },
  });
  const source = readSource('static/portal/js/rio-statistics.js');
  const marker = '  bootstrap();\n})();';
  assert.equal(source.split(marker).length, 2, 'instrument only the initial bootstrap call');
  vm.runInContext(source.replace(marker,
    '  globalThis.unit = {state, updatePresentation, setTab, updateUrl, renderOverview, refresh, viewQuery: () => viewQuery()};\n})();'), context);
  const select = (name, value, text) => {
    Object.assign(get(name), {value, selectedOptions: [{value, textContent: text}], options: [{value, textContent: text}]});
  };
  select('season', 'unit-season', '测试赛季'); select('region', 'cn', '国服');
  select('days', '30', '最近 30 天'); select('percentile', '1', '前 1%');
  select('dungeon', 'test-dungeon', '测试副本'); select('level', '10-plus', '10层及以上');
  select('period', 'season', '赛季累计'); select('raid', 'test-raid', '测试团本');
  select('difficulty', 'mythic', '史诗'); get('opacity').value = '80';
  context.unit.state.catalog = {availability: {complete_list: false, modules: {runs: 0}}, available: [], levels: [{value: '10-plus', min: 10, max: 99}], weeks: {cn: []}};
  return {...context.unit, get, select, urls, requests,
    setApplied: query => { context.unit.state.appliedQuery = new URLSearchParams(query); context.unit.state.appliedTab = context.unit.state.tab; },
    respondWith: fn => { context.fetch = (...args) => { requests.push(args); return fn(...args); }; }};
}

test('overview context uses readable selected labels, including percentile', () => {
  const h = setup(); h.updatePresentation();
  assert.match(h.get('context').textContent, /国服/);
  assert.match(h.get('context').textContent, /最近 30 天/);
  assert.match(h.get('context').textContent, /前 1%/);
  assert.doesNotMatch(h.get('context').textContent, /unit-season|test-dungeon|undefined/);
  assert.match(h.get('module-title').textContent, /统计总览/);
});
test('all module switches update explanatory text and host state', () => {
  const h = setup();
  for (const tab of ['overview', 'runs', 'heatmaps', 'raids']) {
    h.setTab(tab, false);
    assert.equal(h.get('statistics').dataset.tab, tab);
    assert.ok(h.get('module-description').textContent.length > 8);
  }
  assert.match(h.get('context').textContent, /测试团本.*史诗/);
});
test('heatmap tells density from probability and updates layer explanation', () => {
  const h = setup(); h.state.tab = 'heatmaps';
  for (const [layer, label] of [['player_death', '死亡位置'], ['player_spell', '嗜血位置'], ['npc_death', '怪物击杀']]) {
    h.state.layer = layer; h.updatePresentation();
    assert.match(h.get('heatmap-title').textContent, new RegExp(`测试副本.*${label}`));
    assert.match(h.get('heatmap-description').textContent, /密度/);
    assert.match(h.get('heatmap-description').textContent, /不.*概率/);
    assert.match(h.get('context').textContent, new RegExp(label));
  }
});
test('custom ranges are readable and not raw custom tokens', () => {
  const h = setup(); h.state.tab = 'heatmaps';
  h.select('level', 'custom', '自选层数'); h.get('min_level').value = '8'; h.get('max_level').value = '12';
  h.select('period', 'custom', '自选周次'); h.select('week_start', '2', '第 2 周'); h.select('week_end', '4', '第 4 周');
  h.setApplied('level=custom&min_level=8&max_level=12');
  h.updatePresentation();
  assert.match(h.get('context').textContent, /8.*12.*层/);
  assert.match(h.get('context').textContent, /第 2 周.*第 4 周/);
  assert.doesNotMatch(h.get('context').textContent, /custom/);
});
test('zero-death option is described only for an enabled non-death layer', () => {
  const h = setup(); h.state.tab = 'heatmaps'; h.get('zero').checked = true;
  h.state.layer = 'player_spell'; h.updatePresentation();
  assert.match(h.get('context').textContent, /零死亡/);
  h.state.layer = 'player_death'; h.get('zero').disabled = true; h.updatePresentation();
  assert.doesNotMatch(h.get('context').textContent, /零死亡/);
});
test('runs tag distinguishes zero availability from unknown and available', () => {
  const h = setup(); h.updatePresentation();
  assert.equal(h.get('runs-availability').hidden, false);
  assert.match(h.get('runs-availability').textContent, /暂无快照/);
  h.state.catalog.availability.modules.runs = 2; h.updatePresentation();
  assert.equal(h.get('runs-availability').hidden, true);
  delete h.state.catalog.availability; h.updatePresentation();
  assert.equal(h.get('runs-availability').hidden, true, 'absence is not evidence of no source');
});
test('draft advanced filters do not become applied through copy or floor URL updates', () => {
  const h = setup(); h.state.tab = 'heatmaps';
  h.setApplied('item_min=100');
  h.get('item_min').value = '200'; h.get('deaths_max').value = '5';
  const payload = {num_runs: 7}; h.state.payload = payload;
  h.updateUrl();
  assert.match(h.get('context').textContent, /已应用 1 项高级条件/);
  assert.equal(h.viewQuery().get('item_min'), '100');
  assert.equal(h.viewQuery().has('deaths_max'), false);
  assert.equal(h.state.payload, payload);
  h.setApplied('item_min=200&deaths_max=5');
  h.updatePresentation();
  assert.match(h.get('context').textContent, /已应用 2 项高级条件/);
});
test('unsubmitted custom level range is not projected onto the displayed map', () => {
  const h = setup(); h.state.tab = 'heatmaps';
  h.select('level', 'custom', '自选层数');
  h.setApplied('level=custom&min_level=8&max_level=12');
  h.get('min_level').value = '10'; h.get('max_level').value = '20';
  h.updateUrl();
  assert.match(h.get('context').textContent, /8.*12.*层/);
  assert.equal(h.viewQuery().get('min_level'), '8');
  assert.equal(h.viewQuery().get('max_level'), '12');
});
test('only a successful matching response promotes the actual request, not a later draft', async () => {
  const h = setup(); h.state.tab = 'heatmaps'; h.get('item_min').value = '100';
  h.respondWith(async () => {
    h.get('item_min').value = '200';
    return {ok: true, json: async () => ({success: true, data: {data: {floors: []}, snapshot: {state: 'ready'}}})};
  });
  await h.refresh();
  assert.equal(h.get('error').hidden, true, 'matching response rendered without error');
  assert.equal(h.state.appliedQuery.get('item_min'), '100');
  assert.equal(h.viewQuery().get('item_min'), '100');
  assert.match(h.get('context').textContent, /已应用 1 项高级条件/);
  assert.doesNotMatch(h.get('source-map').href, /minItemLevel=200/);
  h.respondWith(async () => ({ok: true, json: async () => ({success: true, data: {data: null, snapshot: {state: 'pending'}}})}));
  await h.refresh();
  assert.equal(h.state.appliedQuery.get('item_min'), '100', 'missing response cannot promote draft');
});
test('runs custom level drafts do not rewrite the loaded range or shared URL', async () => {
  const h = setup(); h.state.tab = 'runs'; h.select('level', 'custom', '自选层数');
  h.get('min_level').value = '8'; h.get('max_level').value = '12';
  h.respondWith(async () => ({ok: true, json: async () => ({success: true, data: {data: {rows: []}, snapshot: {state: 'ready'}}})}));
  await h.refresh();
  assert.equal(h.get('error').hidden, true);
  const payload = h.state.payload;
  h.get('min_level').value = '10'; h.get('max_level').value = '20'; h.updateUrl();
  assert.match(h.get('context').textContent, /8.*12.*层/);
  assert.equal(new URLSearchParams(h.urls.at(-1).split('?')[1]).get('min_level'), '8');
  assert.equal(new URLSearchParams(h.urls.at(-1).split('?')[1]).get('max_level'), '12');
  assert.equal(h.get('min_level').value, '10'); assert.equal(h.requests.length, 1);
  assert.equal(h.state.payload, payload);
});
test('no-data and network failures cannot promote a second advanced count', async () => {
  const h = setup(); h.state.tab = 'heatmaps'; h.get('item_min').value = '100';
  h.respondWith(async () => ({ok: true, json: async () => ({success: true, data: {data: {floors: []}, snapshot: {state: 'ready'}}})}));
  await h.refresh();
  assert.equal(h.get('error').hidden, true);
  assert.match(h.get('filter-summary').textContent, /已应用 1 项/);
  h.get('item_min').value = '200'; h.get('deaths_max').value = '5';
  h.respondWith(async () => ({ok: true, json: async () => ({success: true, data: {data: null, snapshot: {state: 'pending'}}})}));
  await h.refresh(); h.updateUrl();
  assert.match(h.get('filter-summary').textContent, /已应用 1 项/);
  assert.match(h.get('context').textContent, /已应用 1 项高级条件/);
  assert.equal(new URLSearchParams(h.urls.at(-1).split('?')[1]).get('item_min'), '100');
  assert.equal(h.get('item_min').value, '200');
  h.respondWith(async () => { throw new Error('unit-only network failure'); });
  await h.refresh(); h.updateUrl();
  assert.match(h.get('filter-summary').textContent, /已应用 1 项/);
  assert.equal(h.state.appliedQuery.get('item_min'), '100');
});
test('late response serial does not replace the newer successfully displayed query', async () => {
  const h = setup(); h.state.tab = 'heatmaps'; const pending = [];
  h.respondWith(() => new Promise(resolve => pending.push(resolve)));
  h.get('item_min').value = '100'; const old = h.refresh();
  h.get('item_min').value = '200'; const current = h.refresh();
  const response = {ok: true, json: async () => ({success: true, data: {data: {floors: []}, snapshot: {state: 'ready'}}})};
  pending[1](response); await current;
  pending[0](response); await old;
  assert.equal(h.state.appliedQuery.get('item_min'), '200');
  assert.equal(h.get('error').hidden, true);
  assert.match(h.get('source-map').href, /minItemLevel=200/);
});
test('opacity output reflects the existing slider value without fetching', () => {
  const h = setup(); h.get('opacity').value = '45'; h.updatePresentation();
  assert.equal(h.get('opacity-value').textContent, '45%');
});
test('overview range and change direction derive only from displayed history', () => {
  const h = setup();
  h.state.payload = {snapshot_date: '2026-10-02', populations: [{faction: 'all', population: 120,
    percentiles: [{percentile: 1, score: 2500}]}], history: [
    {date: '2026-10-01', population: 100, cutoffs: [{percentile: 1, score: 2400}]},
    {date: '2026-10-02', population: 120, cutoffs: [{percentile: 1, score: 2500}]},
  ]};
  h.renderOverview();
  assert.match(h.get('population-range').textContent, /2026-10-01.*2026-10-02/);
  assert.equal(h.get('score-range').textContent, h.get('population-range').textContent);
  assert.equal(h.get('change').dataset.direction, 'positive');
  h.state.payload.history[1].population = 90; h.renderOverview();
  assert.equal(h.get('change').dataset.direction, 'negative');
  h.state.payload.history = h.state.payload.history.slice(0, 1); h.renderOverview();
  assert.equal(h.get('change').dataset.direction, 'unknown');
  assert.equal(h.get('change').textContent, '样本不足');
});
