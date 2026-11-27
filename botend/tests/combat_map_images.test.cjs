// Exercise the replay renderer's actual Image assignment, not a parallel resolver.
const test = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const js = readFileSync(require.resolve('../../static/dashboard/js/combat_replay.js'), 'utf8');
const start = js.indexOf('    const artCache = '), end = js.indexOf('    function onAtlas(', start);
assert.ok(start >= 0 && end > start);
const mdt = '/static/portal/mythic_planner/vendor/mdt-6.2.20/maps/murder-row/floor-1.webp';
const oss = 'https://oss.wowdaily.cn/mythic-planner/auto/maps/' + 'a'.repeat(64) + '.webp';
const native = '/static/dashboard/maps/12.1.7.71087/1958.webp';
const dynamic = '/portal/combat-logs/maps/12.1.7.71087/1959/';
const imageFor = vm.runInNewContext(js.slice(start, end) + 'imageFor', {
    Image: class {}, publishedImageUrl: url => url === mdt ? oss : '', draw() {},
});
test('MDT uses the receipt; native layers retain only the mapped first-party route shapes', () => {
    assert.equal(imageFor({url:mdt}).image.src, oss);
    for (const url of [native, dynamic]) assert.equal(imageFor({url}).image.src, url);
    for (const url of ['https://example.org' + native, '//' + 'example.org' + native,
                       native + '?token=redacted', '/media/replay.webp', '/static/dashboard/maps/../private.webp']) {
        const entry = imageFor({url});
        assert.equal(entry.status, 'error');
        assert.equal(entry.image.src, undefined);
    }
});
