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
console.log('副本本地筛选、搜索交集和缺失状态验证通过');
