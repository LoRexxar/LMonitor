const assert = require('node:assert/strict');
const {test, createDOM, mount, response, tick} = require('./player_analysis_dom_harness');

test('证据不足回读终态后停止轮询，GET不重发，人工重新检查仍可用', async () => {
    const dom = createDOM('<main id="player-analysis-app"></main>', {url:'https://example.com/dashboard/'});
    const doc = dom.window.document, calls = [];
    let control = {state:'insufficient_evidence', phase:'complete',
        message:'本次检查完成：证据不足，不生成新标签。', finished_at:'2026-11-02T00:00:00Z'};
    const row = () => ({id:7, revision:3, player_id:null, activity_id:9, can_recheck:true,
        status:'ready', review_status:'rejected', is_published:false, manual:null,
        ai:{validation_status:'missing'}, recheck:control});
    let pollTimers = 0;
    const setTimeout = dom.window.setTimeout.bind(dom.window);
    dom.window.setTimeout = (callback, delay, ...args) => {
        if (delay === 19) pollTimers++;
        return setTimeout(callback, delay, ...args);
    };
    const app = mount(doc.querySelector('main'), {pollInterval:19, fetch:async (url, options) => {
        calls.push({url, method:options.method});
        if (url.endsWith('/recheck/')) {
            if (options.method === 'POST') {
                assert.deepEqual(JSON.parse(options.body), {revision:3});
                control = {...control, token:'new-manual-check', finished_at:'2026-11-02T01:00:00Z'};
            }
            return response(control);
        }
        return response(url.includes('?')
            ? {items:[row()], total:1, page:1, total_pages:1, tag_options:[]}
            : row());
    }});
    await app.ready;
    assert.ok(calls.every(call => call.method === 'GET'));
    doc.querySelector('[data-action="open"]').click();
    await app.settled();
    assert.match(doc.querySelector('[data-check-status]').textContent, /^证据不足 · 本次检查完成/);
    assert.equal(doc.querySelector('.pa-detail [data-action="recheck"]').disabled, false);
    assert.equal(calls.filter(call => call.url.endsWith('/recheck/')).length, 1);
    // The terminal readback includes both the exact detail and the list.
    assert.equal(calls.filter(call => call.url.endsWith('/7/')).length, 2);
    assert.equal(calls.filter(call => call.url.includes('?')).length, 2);
    assert.equal(pollTimers, 0);
    assert.ok(calls.every(call => call.method === 'GET'));

    doc.querySelector('.pa-detail [data-action="recheck"]').click();
    await app.settled();
    await tick();
    assert.equal(calls.filter(call => call.method === 'POST').length, 1);
    assert.equal(calls.filter(call => call.url.endsWith('/recheck/') && call.method === 'GET').length, 2);
    assert.equal(doc.querySelector('.pa-detail [data-action="recheck"]').disabled, false);
    assert.equal(pollTimers, 0);
});
