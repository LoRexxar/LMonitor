const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const source = p => fs.readFileSync(path.join(root, p), 'utf8');

test('a cached combat page without image helpers still shows a refreshed spec', async () => {
  let rendered;
  const row = {querySelector: () => ({}), prepend: img => { rendered = img.src; }};
  const button = {form: {}, closest: () => row};
  const src = '/icons/spec.jpg';
  await vm.runInNewContext(source('static/dashboard/js/combat_public_roles.js'), {
    window: {}, document: {querySelectorAll: () => [button]},
    FormData: class { set() {} }, Image: class {},
    fetch: async () => ({ok: true, json: async () => ({label: 'Fire', icon: src})})
  });
  assert.equal(rendered, src);
  assert.equal(button.hidden, true);
});

// A synthetic visible spell: the test has no browser network or user data.
test('visible spell uses the exact published image before setting src; misses retain upstream', async () => {
  for (const hit of [true, false]) {
    const upstream = 'https://wow.zamimg.com/images/wow/icons/small/spell_fire.jpg';
    const published = 'https://oss.wowdaily.cn/fixture/verified.jpg';
    let observer, component, resolve;
    const drawn = new Promise(r => { resolve = r; });
    const context = vm.createContext({
      document: {getElementById: () => ({textContent: JSON.stringify(hit ? {[upstream]:published} : {})})},
      customElements: {get: () => null, define: (_, klass) => { component = klass; }},
      sessionStorage: {getItem: () => JSON.stringify([['42', 'spell_fire']])},
      IntersectionObserver: class {constructor(cb) {observer = cb;} observe(){} unobserve(){}},
      HTMLElement: class {}, Image: class { set src(value) { this.drawn = value; } },
    });
    context.window = context;
    vm.runInContext(source('static/shared/js/site-images.js'), context);
    vm.runInContext(source('static/dashboard/js/combat_spell_icon.js'), context);
    const element = new component();
    Object.assign(element, {dataset:{spell:'42'}, isConnected:true, replaceChildren: img => resolve(img.drawn)});
    element.connectedCallback(); observer([{target:element,isIntersecting:true}]);
    assert.equal(await drawn, hit ? published : upstream);
    assert.equal(context.publishedImageUrl(upstream + '?private=1'), upstream + '?private=1');
  }
});
