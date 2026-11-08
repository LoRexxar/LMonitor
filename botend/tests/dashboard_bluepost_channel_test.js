const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readSource} = require('./vm_harness');

function article(overrides = {}) {
    return {id: 42, source: 'blizzard_tracker', category: '原分类', title: 'Original PTR Forever title',
        title_cn: '跨板块原中文标题', author: '原作者', publish_time: '2026-11-08 08:09:10', reply_count: 7,
        url: 'https://example.invalid/topic/42', content_cn: '完整中文正文', ...overrides};
}

async function render(surface, row) {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, {innerHTML: '', classList: {add() {}, remove() {}}});
        return elements.get(id);
    };
    const requests = [];
    const context = vm.createContext({console, URL, Date, setTimeout, window: {},
        document: {addEventListener() {}, getElementById: element, cookie: 'csrftoken=fixture-token', body: {style: {}}},
        fetch: async (url, options) => {
            requests.push([url, JSON.parse(options.body)]);
            return {ok: true, json: async () => ({status: 'success', data: row})};
        },
    });
    vm.runInContext(readSource('static/dashboard/js/main.js'), context);
    if (surface === 'list') return context.renderNewsWowListItem(row);
    context.openNewsWowDetail(row.id);
    // openNewsWowDetail currently starts a promise chain without returning it.
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(requests, [['/dashboard/', {action: 'get_wow_article_detail', id: 42}]]);
    const html = element('news-wow-detail-body').innerHTML;
    assert.match(html, /完整中文正文/);
    return html;
}

for (const surface of ['list', 'detail']) {
    test(`${surface}: canonical labels appear once in metadata without rewriting original presentation`, async () => {
        for (const [game_version, game_version_label] of [
            ['retail', '正式服'], ['forever', '无限服'], ['test', '测试服'],
        ]) {
            const row = article({game_version, game_version_label,
                forum_category: {id: 123, name: '真实来源板块', url: 'https://example.invalid/board'}});
            const frozen = JSON.stringify(row);
            const html = await render(surface, row);
            const badges = html.match(/<span\b[^>]*>\[(正式服|无限服|测试服)\]<\/span>/g) || [];
            assert.equal(badges.length, 1);
            assert.ok(badges[0].includes(`[${game_version_label}]`));
            assert.ok(badges[0].includes('bluepost-channel-badge'));
            assert.ok(badges[0].includes(`data-game-version="${game_version}"`));
            assert.ok(badges[0].includes('title="真实来源板块"'));
            assert.equal(html.replace(badges[0], ''), await render(surface, article()));
            assert.equal(JSON.stringify(row), frozen);
        }
    });

    test(`${surface}: old/unknown payloads never infer labels from title or board name`, async () => {
        const baseline = await render(surface, article());
        for (const fields of [{game_version: 'test'}, {game_version_label: ''},
            {game_version_label: null}, {forum_category: {name: 'PTR 无限服 正式服'}}]) {
            assert.equal(await render(surface, article(fields)), baseline);
        }
    });

    test(`${surface}: unrelated sources cannot display a backend channel label`, async () => {
        for (const source of ['wowhead', 'exwind', 'blizzard_cn', 'nga', 'unknown']) {
            const row = article({source});
            assert.equal(await render(surface, {...row, game_version_label: '正式服',
                forum_category: {name: '错误渠道'}}), await render(surface, row));
        }
    });

    test(`${surface}: label and optional category tooltip are escaped, not classified locally`, async () => {
        const html = await render(surface, article({game_version_label: '权威<&"标签>',
            forum_category: {name: '板块"><img src=x onerror=alert(1)>&'}}));
        assert.ok(html.includes('[权威&lt;&amp;&quot;标签&gt;]'));
        assert.ok(html.includes('title="板块&quot;&gt;&lt;img src=x onerror=alert(1)&gt;&amp;"'));
        assert.doesNotMatch(html, /<img|title="板块">/);
        for (const forum_category of [undefined, null, {}, {name: ''}]) {
            const safe = await render(surface, article({game_version_label: '测试服', forum_category}));
            const badge = safe.match(/<span\b[^>]*>\[测试服\]<\/span>/)?.[0];
            assert.ok(badge);
            assert.doesNotMatch(badge, /\btitle=/);
        }
    });
}
