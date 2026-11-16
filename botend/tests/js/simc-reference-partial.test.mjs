// 验证真实交互脚本的请求竞态、筛选合并、失败恢复与历史导航。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createRequire} from 'node:module';
const {JSDOM} = createRequire(import.meta.url)('jsdom');
const script = fs.readFileSync(new URL('../../../static/portal/js/simc-reference.js', import.meta.url), 'utf8');
const sections = ['reference-talents', 'reference-equipment', 'reference-apl'];
const markup = (section = sections[0], talent = '1') => `
<form data-hero-filter><input name="spec" value="warrior_fury" type="hidden"><input type="checkbox" name="hero" value="60"><input type="checkbox" name="hero" value="61"></form>
<div class="reference-results">结果对应天赋 ${talent}</div>
<nav data-reference-tabs>${sections.map(id => `<a id="${id}-tab" data-reference-tab href="?spec=warrior_fury&talent=${talent}&view=${id}#${id}">${id}</a>`).join('')}</nav>
${sections.map(id => `<section data-reference-panel id="${id}" data-loaded="${id === section}">
${id === section ? `<form><input name="spec" value="warrior_fury" type="hidden"><select id="reference-talent" name="talent">${['1', '2', '3'].map(value => `<option value="${value}" ${value === talent ? 'selected' : ''}>天赋${value}</option>`).join('')}</select></form>` : ''}
${id === section && id === 'reference-apl' ? '<nav data-apl-tabs><a data-apl-tab data-apl-jump href="#apl-list-1">主优先级</a><a data-apl-tab data-apl-jump href="#apl-list-2">爆发</a></nav><div id="apl-list-1" data-apl-panel data-apl-default>主优先级</div><div id="apl-list-2" data-apl-panel>爆发</div>' : ''}</section>`).join('')}`;
const dom = new JSDOM(`<main class="simc-reference"><nav class="simc-module-navigation"></nav>
<div id="reference-navigation-status" hidden><b data-reference-loading-label></b><p data-reference-loading-detail></p><a data-reference-loading-retry></a></div>
<div class="reference-workbench">${markup()}</div><p id="reference-copy-status"></p></main>`, {
    url: 'http://localhost/portal/simc-reference/?spec=warrior_fury&talent=1', runScripts: 'outside-only', pretendToBeVisual: true,
});
const {window: w} = dom;
w.matchMedia = () => ({matches: false, addEventListener() {}});
const requests = [];
w.fetch = (url, options) => new Promise(resolve => requests.push({url: new URL(url), options, resolve}));
const tick = (ms = 15) => new Promise(resolve => setTimeout(resolve, ms));
const change = (value) => {
    const select = w.document.getElementById('reference-talent');
    select.value = value;
    select.dispatchEvent(new w.Event('change', {bubbles: true}));
};
const reply = (request, talent, section = sections[0]) => request.resolve({
    ok: true, redirected: false, headers: {get: () => 'application/json'},
    json: async () => ({html: markup(section, talent), navigation: '<nav class="simc-module-navigation">新榜单入口</nav>',
        url: `/portal/simc-reference/?spec=warrior_fury&talent=${talent}&view=${section}`, section}),
});
w.eval(script);
change('2'); await tick();
change('3'); await tick();
assert.equal(requests.length, 2);
assert.equal(requests[0].options.signal.aborted, true, '新选择取消旧请求');
reply(requests[1], '3'); await tick();
reply(requests[0], '2'); await tick();
assert.equal(w.document.querySelector('.reference-results').textContent, '结果对应天赋 3', '旧响应不得覆盖新结果');
assert.equal(w.document.getElementById('reference-talent').value, '3');
assert.equal(w.location.search.includes('talent=3'), true);
assert.equal(w.document.activeElement.id, 'reference-talent', '动态替换后保留键盘焦点');
w.document.getElementById('reference-equipment-tab').click(); await tick();
assert.equal(requests.at(-1).url.searchParams.get('view'), 'reference-equipment');
assert.equal(requests.at(-1).options.headers['X-Simc-Reference'], 'workspace');
reply(requests.at(-1), '3', sections[1]); await tick();
assert.equal(w.document.getElementById('reference-equipment').hidden, false);
assert.equal(w.document.getElementById('reference-talents').hidden, true);
const before = requests.length;
for (const checkbox of w.document.querySelectorAll('[name=hero]')) {
    checkbox.checked = true;
    checkbox.dispatchEvent(new w.Event('change', {bubbles: true}));
}
await tick(220);
assert.equal(requests.length, before + 1, '连续勾选合并成一个请求');
assert.deepEqual(requests.at(-1).url.searchParams.getAll('hero'), ['60', '61']);
requests.at(-1).resolve({ok: false}); await tick();
assert.equal(w.document.querySelector('.reference-results').textContent, '结果对应天赋 3');
assert.equal(w.document.querySelector('[name=hero]').checked, false, '失败后恢复原选择');
assert.equal(w.document.querySelector('[data-reference-loading-retry]').hidden, false);
assert.equal(w.document.querySelector('.reference-workbench').getAttribute('aria-busy'), 'false');
w.history.replaceState(null, '', '?spec=warrior_fury&talent=1&view=reference-talents');
w.dispatchEvent(new w.PopStateEvent('popstate')); await tick();
reply(requests.at(-1), '1'); await tick();
assert.equal(w.document.getElementById('reference-talent').value, '1', '历史导航重新校验并还原配置');
assert.equal(w.document.querySelector('.reference-results').textContent, '结果对应天赋 1');
assert.equal(w.document.getElementById('reference-navigation-status').hidden, true);
const beforeHashHistory = requests.length;
w.history.replaceState(null, '', `${w.location.search}#reference-talents`);
w.dispatchEvent(new w.PopStateEvent('popstate'));
w.dispatchEvent(new w.HashChangeEvent('hashchange')); await tick();
assert.equal(requests.length, beforeHashHistory, '同一配置的分组历史不得重复加载');
w.document.getElementById('reference-apl-tab').click(); await tick();
reply(requests.at(-1), '1', sections[2]); await tick();
w.document.querySelector('[data-apl-tab][href="#apl-list-2"]').click();
assert.equal(w.document.getElementById('apl-list-2').hidden, false);
const beforeAplBack = requests.length;
w.history.replaceState(null, '', `${w.location.search}#reference-apl`);
w.dispatchEvent(new w.PopStateEvent('popstate'));
w.dispatchEvent(new w.HashChangeEvent('hashchange')); await tick();
assert.equal(w.document.getElementById('apl-list-1').hidden, false, '回到章节入口时恢复主优先级');
assert.equal(w.document.getElementById('apl-list-2').hidden, true);
assert.equal(requests.length, beforeAplBack);
dom.window.close();
console.log('局部切换、竞态隔离、筛选合并、失败恢复和历史导航通过');
