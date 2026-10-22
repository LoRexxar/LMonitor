const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '../..');

// Exercise the real template/controller; only the read-only HTTP boundary is a fixture.
test('hero entrances are labelled in list and detail without hiding real skill gaps', async t => {
    const records = [
        {id: 123287, node_id: 123287, tree_type: 'hero_anchor', identity_kind: 'node', name: '虚空编织者', description: '', expected: '英雄天赋'},
        {id: 456, node_id: 123, tree_type: 'hero', identity_kind: 'spell', name: '英雄技能', description: '保留 A & B < C', expected: '保留 A & B < C'},
        {id: 457, node_id: 124, tree_type: 'hero', identity_kind: 'spell', name: '缺失的英雄技能', description: '', expected: '描述待补齐'},
        {id: 458, node_id: 125, tree_type: 'spec', identity_kind: 'spell', name: '缺失的专精技能', description: '', expected: '描述待补齐'},
    ].map(row => ({class_name: '牧师', spec_name: '戒律', tree: '展示名称不参与类型判断', count: 1, ...row}));
    const dom = new JSDOM(`<section id="central-data">${fs.readFileSync(path.join(root, 'templates/dashboard/central_data.html'), 'utf8')}</section>`, {
        url: 'https://example.test/dashboard/?section=central-data&cd_resource=talents', runScripts: 'outside-only',
    });
    t.after(() => dom.window.close());
    const w = dom.window;
    const dialog = w.document.querySelector('dialog');
    dialog.showModal = () => { dialog.open = true; };
    dialog.close = () => { dialog.open = false; dialog.dispatchEvent(new w.Event('close')); };
    const requests = [];
    w.fetch = async url => {
        const u = new URL(url, w.location.href);
        assert.equal(u.pathname, '/api/dashboard/central-data/');
        assert.equal(u.searchParams.get('resource'), 'talents');
        requests.push(u);
        const id = u.searchParams.get('id');
        const row = records.find(r => String(r.id) === id);
        if (row) assert.equal(u.searchParams.get('identity_kind'), row.identity_kind);
        const payload = row ? {record: {...row, central: {}}, entries: [row]} : {records, facets: {}};
        return {ok: true, json: async () => ({success: true, branch: 'retail', pagination: {page: 1, pages: 1, total: row ? 1 : records.length}, ...payload})};
    };
    // Let the native load event finish before injecting the controller once.
    await new Promise(resolve => w.addEventListener('load', resolve, {once: true}));
    w.eval(fs.readFileSync(path.join(root, 'static/dashboard/js/central-data.js'), 'utf8'));
    async function until(predicate) {
        for (let i = 0; i < 30; i++) {
            if (predicate()) return;
            await new Promise(resolve => setTimeout(resolve, 5));
        }
        assert.fail('central-data DOM did not become ready');
    }
    await until(() => w.document.querySelectorAll('#cd-table-body tr').length === records.length);
    const tableRows = [...w.document.querySelectorAll('#cd-table-body tr')];
    for (const [i, row] of records.entries()) {
        assert.equal(tableRows[i].querySelector('.cd-description').textContent, row.expected, row.name + ' list');
        tableRows[i].querySelector('[data-cd-detail]').click();
        await until(() => dialog.querySelector('.cd-entry'));
        assert.equal(dialog.querySelector('.cd-detail-section > .cd-copy').textContent, row.expected, row.name + ' detail');
        assert.equal(dialog.querySelector('.cd-entry .cd-copy').textContent, row.expected, row.name + ' entry');
        dialog.close();
    }
    assert.equal(requests.length, records.length + 1);
});
