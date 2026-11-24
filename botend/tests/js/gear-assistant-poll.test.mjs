import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../../../static/portal/js/gear_assistant.js', import.meta.url), 'utf8');
const id = 'df3bc2e2-acd9-4f60-b538-31841b1c096a';
const storage = new Map();
let mode = 'first';
let polls = 0;
const delayedRefreshes = new Map();
const calls = [];
const el = new Map();
const element = key => {
  if (!el.has(key)) el.set(key, {disabled: false, checked: false, value: '25', classList: {remove() {}}});
  return el.get(key);
};
const context = {
  document: {querySelector: () => ({dataset: {optimizeUrl: '/optimize/', assistantBootstrapUrl: '/bootstrap/', jobOwner: '245'}}), getElementById: element},
  WowGearState: {refresh: state => new Promise(resolve => delayedRefreshes.set(state.specName, () => resolve({...state})))},
  window: {matchMedia: () => ({matches: false}), setTimeout: fn => queueMicrotask(fn), location: {}},
  sessionStorage: {getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v), removeItem: k => storage.delete(k)},
  localStorage: {getItem: () => null, setItem: (k,v) => storage.set(k,v)},
  AbortSignal, crypto: {randomUUID: () => id},
  send: async (url, opts) => {
    calls.push([url, opts]);
    if (url.startsWith('/bootstrap/')) return {catalog: {available: true, season_name: new URL(url, 'https://example.test').searchParams.get('spec')}, flasks: [{key: 'auto', name: 'Auto'}]};
    if (opts.method === 'POST') {
      assert.equal(element('assistant-edit-config').disabled, true);
      assert.equal(element('target-crit').disabled, true);
      if (mode === 'reject') throw Object.assign(new Error('目标属性无效'), {status: 400});
      throw new Error('响应丢失');
    }
    polls++;
    if (['first', 'reject'].includes(mode)) throw Object.assign(new Error('not yet committed'), {status: 404});
    return {job: {id, state: polls === 4 ? 'running' : 'done', class_name: 'Warrior', spec_name: 'Fury',
                  result: {plans: [{key: 'all'}], catalog: {batch_key: 'original-batch'}}}};
  },
};
vm.runInNewContext(source.replace('  initialize();', `
  currentState = {className:'Warrior', specName:'Fury', equipment:{head:{item:{name:'Helm'},variant:{id:23},gems:[{variant:{id:59}}]}}};
  lockedSlots = new Set(['head']);
  toast = () => {};
  renderPlans = plans => { globalThis.rendered = plans; };
  requestJson = (...args) => send(...args);
  globalThis.run = generate;
  globalThis.ready = () => { restoreJob(); initialized = assistantReady = true; };
  globalThis.select = (className, specName) => { currentState = {className,specName,equipment:{}}; return loadAssistantData(); };
  globalThis.config = () => currentState.specName;
  globalThis.apply = () => { currentState = {className:'Mage', specName:'Fire'}; applyPlan('all'); };
`), context);
await context.run();
assert.equal(calls.length, 0, 'Generate during bootstrap never submits');
context.ready();
await Promise.all([context.run(), context.run()]);
assert.equal(calls.filter(([,opts]) => opts.method === 'POST').length, 1);
assert.deepEqual(JSON.parse(calls[0][1].body).equipment, {head:{variant:{id:23},gems:[{variant:{id:59}}]}});
assert.equal(element('assistant-rerun').textContent, '继续查看');
assert.equal(JSON.parse([...storage.values()][0]).id, id, 'Late commit may still resolve the same preserved nonce');
assert.equal(element('assistant-edit-config').disabled, true);
mode = 'resume';
await context.run();
assert.equal(calls.filter(([,opts]) => opts.method === 'POST').length, 1);
assert.ok(calls.slice(1).every(([url,opts]) => url === `/optimize/${id}/` && opts.cache === 'no-store'));
assert.equal(context.rendered[0].key, 'all');
assert.equal(storage.size, 0);
assert.equal(element('assistant-rerun').textContent, '再次模拟');
context.apply();
assert.equal(JSON.parse(storage.get('wowdaily:gear-builder:v1:Warrior:Fury')).batchKey, 'original-batch');
assert.equal(storage.has('wowdaily:gear-builder:v1:Mage:Fire'), false);

mode = 'first';
await context.run();
const previousBody = calls.findLast(([,opts]) => opts.method === 'POST')[1].body;
mode = 'reject';
await context.run();
assert.equal(calls.findLast(([,opts]) => opts.method === 'POST')[1].body, previousBody, 'Replay uses identical nonce and inputs');
assert.equal(storage.has('wowdaily:gear-assistant:job:v2:245'), false);
assert.equal(element('assistant-edit-config').disabled, false);
assert.match(element('assistant-explanation').textContent, /目标属性无效/);

const old = context.select('Warrior', 'Fury');
const next = context.select('Mage', 'Fire');
const before = calls.length;
await context.run();
assert.equal(calls.length, before, 'No submission while the latest selection is loading');
delayedRefreshes.get('Fire')();
await next;
element('assistant-flask').value = 'haste';
delayedRefreshes.get('Fury')();
await old;
assert.equal(context.config(), 'Fire');
assert.equal(element('assistant-flask').value, 'haste');
assert.match(element('assistant-catalog-status').textContent, /Fire/);
assert.ok(calls.some(([url]) => url.includes('spec=Fire')) && !calls.some(([url]) => url.includes('spec=Fury')));
await context.run();
assert.equal(JSON.parse(calls.findLast(([,opts]) => opts.method === 'POST')[1].body).spec_name, 'Fire');

// Run the shipped initializer while the first bootstrap is paused. Its
// change handlers must not be reachable before the controls become ready.
const template = fs.readFileSync(new URL('../../../templates/portal/gear_assistant.html', import.meta.url), 'utf8');
const initElements = new Map();
const initElement = key => {
  if (!initElements.has(key)) {
    const element = {owner: key, events: new Map(), options: [{value: 'Fury'}],
      disabled: new RegExp(`<[^>]+id="${key}"[^>]*\\bdisabled\\b`).test(template),
      addEventListener(name, fn) {this.events.set(name, fn);}};
    initElements.set(key, element);
  }
  return initElements.get(key);
};
let unblock;
const bootstrap = new Promise(resolve => {unblock = resolve;});
const startContext = {
  document: {querySelector: () => ({dataset: {builderBootstrapUrl:'/builder/', assistantBootstrapUrl:'/bootstrap/'}, setAttribute() {}}),
    querySelectorAll: () => [], addEventListener() {}, getElementById: initElement},
  sessionStorage: {getItem: () => null}, localStorage: {getItem: () => null},
  WowGearState: {refresh: async state => state}, WowGearAssistantResults: {bindTooltips: () => () => {}},
  send: (url) => url === '/builder/' ? {classes:[{key:'Warrior', specs:[{key:'Fury'}]}]} : bootstrap,
};
vm.runInNewContext(source.replace('  initialize();', '  requestJson = (...args) => send(...args); globalThis.started = initialize();'), startContext);
await new Promise(resolve => setImmediate(resolve));
assert.equal(initElement('assistant-class').disabled, true);
assert.equal(initElement('assistant-spec').disabled, true);
assert.equal(initElement('assistant-spec').events.has('change'), false);
assert.equal(initElement('assistant-generate').disabled, true);
unblock({catalog:{available:true}, owned_items:[], flasks:[]});
await startContext.started;
assert.equal(initElement('assistant-spec').disabled, false);
assert.equal(initElement('assistant-spec').events.has('change'), true);
assert.equal(initElement('assistant-generate').disabled, false);
