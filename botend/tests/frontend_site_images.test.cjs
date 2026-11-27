const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const source = p => fs.readFileSync(path.join(root, p), 'utf8');

test('a cached combat page without helpers renders managed portraits only', async () => {
  for (const [src, allowed] of [['https://oss.wowdaily.cn/photo.jpg?x-oss-signature=test-only', false], ['https://unknown.example/icon.jpg', false], ['https://oss.wowdaily.cn/real.jpg', true]]) {
    const assignments = [];
    const row = {querySelector: () => ({}), prepend() {}};
    const button = {form: {}, closest: () => row};
    await vm.runInNewContext(source('static/dashboard/js/combat_public_roles.js'), {
      window: {}, document: {querySelectorAll: () => [button]}, FormData: class { set() {} },
      Image: class {set src(v) { assignments.push(v); } get src() { return assignments.at(-1); }},
      fetch: async () => ({ok: true, json: async () => ({label:'Fire', icon:src})})
    });
    assert.deepEqual(assignments, allowed ? [src] : []);
    assert.equal(Boolean(button.hidden), allowed);
  }
});

// A synthetic visible spell: the test has no browser network or user data.
test('visible spell uses the exact published image before setting src; misses never request upstream', async () => {
  for (const hit of [true, false]) {
    const upstream = 'https://wow.zamimg.com/images/wow/icons/small/spell_fire.jpg';
    const published = 'https://oss.wowdaily.cn/fixture/verified.jpg';
    let observer, component, resolve;
    const drawn = new Promise(r => { resolve = r; });
    const context = vm.createContext({
      URL, location: new URL('https://wowdaily.cn/dashboard/'), document: {getElementById: () => ({textContent: JSON.stringify(hit ? {[upstream]:published} : {})})},
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
    Object.defineProperty(element, 'hidden', {set: value => { if (value) resolve(undefined); }});
    element.connectedCallback(); observer([{target:element,isIntersecting:true}]);
    assert.equal(await drawn, hit ? published : undefined);
    assert.equal(context.publishedImageUrl(upstream + '?private=1'), '');
  }
});

test('exact public static mapping, host validation and non-public paths', () => {
  const local = '/static/portal/images/portal-logo.webp', oss = 'https://oss.wowdaily.cn/release/logo.webp';
  const location = new URL('https://wowdaily.cn/portal/');
  const ctx = {URL, location, document:{getElementById:()=>null}, publishedImageEntries:{[local]:oss, broken:'https://untrusted.example/x'}};
  ctx.window = ctx;
  vm.runInNewContext(source('static/shared/js/site-images.js'), ctx);
  for (const u of [local, location.origin + local, oss]) assert.equal(ctx.publishedImageUrl(u), oss);
  for (const u of ['broken','//unknown.example/x','/static/missing.png','/redirect/?next=external',
                   'https://oss.wowdaily.cn@unknown.example/x','https://oss.wowdaily.cn.invalid/x']) {
    assert.equal(ctx.publishedImageUrl(u), '');
  }
});

// Optional dependency (also shipped in the Python Playwright distribution).
let chromium;
try { ({chromium} = require(process.env.PLAYWRIGHT_NODE_MODULE || 'playwright')); }
catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
test('Chromium: dynamic rich HTML and a failed character portrait never contact the original', {skip: !chromium}, async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    const oss = 'https://oss.wowdaily.cn', privateUrl = 'https://unknown.example/original.png';
    const local = '/static/portal/favicons/default.svg', avatar = 'https://source.example/actual-avatar.jpg';
    const requests = [];
    await page.route('**/*', route => {
      const url = route.request().url();
      if (url === 'https://site.test/') return route.fulfill({contentType:'text/html', body:'<main></main>'});
      requests.push(url);
      return route.fulfill(url.endsWith('broken.jpg') ? {status:404, body:''} : {
        contentType:'image/svg+xml', body:'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"/>'});
    });
    await page.goto('https://site.test/');
    await page.evaluate(map => window.publishedImageEntries = map, {
      [local]:oss+'/favicon.svg', [avatar]:oss+'/avatar.jpg',
      'css-sha256:6dacbc8112b87ed35678fa93fcd78e5c9eac24c34ca28215102989a93ca97250':oss+'/check.svg'
    });
    await page.addScriptTag({content:source('static/shared/js/site-images.js')});
    await page.addStyleTag({content:source('static/dashboard/css/combat_theme.css')});
    await page.evaluate(({oss, privateUrl, local, avatar}) => {
      const html = `<link rel="icon" href="${local}"><picture><source srcset="${privateUrl} 2x, ${avatar} 1x"><img src="${privateUrl}"></picture>
        <div style="height:12px;background-image:url('${local}'); border-image-source:url('${privateUrl}')">style</div>
        <div style="height:12px;background-image:image-set('${privateUrl}' 1x)">indirect</div>
        <div style="height:20px;background-image:var(--check-image)">check</div>
        <a href="${privateUrl}">article</a><img id="role" src="${oss}/broken.jpg" data-character-icon data-fallback="${avatar}">
        <template><img src="${privateUrl}"></template><img id="unknown" src="${privateUrl}" onerror="this.src='${privateUrl}'">`;
      document.querySelector('main').append(window.publishedImageFragment(html));
      document.body.append(document.querySelector('template').content.cloneNode(true));
    }, {oss, privateUrl, local, avatar});
    await page.addScriptTag({content:source('static/dashboard/js/character_bindings.js')});
    await page.waitForFunction(() => document.querySelector('#role').naturalWidth > 0);
    await page.waitForLoadState('networkidle');
    assert.equal(await page.getAttribute('picture > source','srcset'), oss+'/avatar.jpg 1x');
    assert.equal(await page.getAttribute('#role','src'), oss+'/avatar.jpg');
    assert.equal(await page.getAttribute('#unknown','src'), null);
    assert.equal(await page.getAttribute('a','href'), privateUrl);
    assert.deepEqual(new Set(requests), new Set([oss+'/avatar.jpg', oss+'/favicon.svg', oss+'/check.svg', oss+'/broken.jpg']));
  } finally { await browser.close(); }
});
