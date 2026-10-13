import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../../../static/portal/js/gear_assistant.js', import.meta.url), 'utf8');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {disabled: false, textContent: '', innerHTML: '', classList: {add() {}, remove() {}}});
  return elements.get(id);
};
let confirmed = false, requests = [], request;
const context = {
  document: {querySelector: () => ({dataset: {ownedUrl: '/owned/'}}), getElementById: element},
  window: {confirm: message => { assert.match(message, /全部/); assert.match(message, /不会/); return confirmed; }},
};
vm.runInNewContext(source.replace('  initialize();', `
  requestJson = (...args) => globalThis.send(...args);
  toast = () => {};
  globalThis.qa = {clearOwnedItems, renderOwned,
    setData(rows) { assistantData = {owned_items: rows}; currentState = {equipment: {head: {item: {item_id: 99}}}}; lockedSlots = new Set(['head']); },
    get rows() { return assistantData.owned_items; },
    get current() { return currentState; },
    get locks() { return [...lockedSlots]; },
  };
`), Object.assign(context, {send: (...args) => { requests.push(args); return request(...args); }}));
const qa = context.qa, plain = x => JSON.parse(JSON.stringify(x));
qa.setData([{id: 1}, {id: 2}]); qa.renderOwned();
const current = plain(qa.current);
await qa.clearOwnedItems();
assert.equal(requests.length, 0, '取消不发送删除');
assert.equal(qa.rows.length, 2);
confirmed = true;
request = async () => { throw new Error('失败'); };
await qa.clearOwnedItems();
assert.equal(qa.rows.length, 2, '请求失败保留装备库');
assert.equal(element('assistant-clear-owned').disabled, false);
requests = [];
let complete;
request = () => new Promise(resolve => { complete = resolve; });
const pending = qa.clearOwnedItems();
await qa.clearOwnedItems();
assert.equal(requests.length, 1, '防止重复清空');
assert.equal(requests[0][1].method, 'DELETE');
assert.deepEqual(JSON.parse(requests[0][1].body), {confirm_clear: true});
complete({success: true, deleted_count: 2}); await pending;
assert.equal(qa.rows.length, 0);
assert.equal(element('assistant-owned-tab-count').textContent, '0');
assert.equal(element('assistant-clear-owned').disabled, true, '空列表不可再清空');
assert.deepEqual(plain(qa.current), current, '当前配装不变');
assert.deepEqual(plain(qa.locks), ['head'], '锁定不变');
console.log('PASS: clear owned cancel/failure/success/double-click and draft preservation');
