// 使用真实页面脚本验证本地副本筛选、搜索和准备状态。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../..');
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
vm.runInContext(fs.readFileSync(path.join(root, 'static/portal/js/main.js'), 'utf8'), context);
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
