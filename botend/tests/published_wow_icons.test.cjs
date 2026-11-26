const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../../static/shared');
const src = fs.readFileSync(path.join(root,'js/published-wow-icons.js'),'utf8');
const doc = JSON.parse(fs.readFileSync(path.join(root,'data/published-wow-icons.json'),'utf8'));
const base = 'https://wow.zamimg.com/images/wow/icons/';
const cdn = 'https://oss.wowdaily.cn/wow_icons_oss/';
const fire = 'spell_fire_flamebolt.jpg', frost = 'spell_frost_frostbolt02.jpg';
function harness(ok) {
    const requests = [];
    const inventory = '/static/release.json';
    const context = {document:{currentScript:{dataset:{inventory}}}, window:{}, AbortSignal,
        fetch:async (url, options) => { requests.push([url,options.credentials,options.referrerPolicy]); return {ok,json:async()=>doc}; }};
    context.window.publishedImageUrl = s => s === base+'small/exact.jpg' ? '/static/exact.jpg' : s;
    vm.runInNewContext(src, context);
    return {resolve:context.window.publishedWowIconUrl, requests};
}
test('exact map first, no probes; documented same-size aliases share one public inventory', async () => {
    const {resolve,requests} = harness(true);
    for (const miss of ['?cast_icon=133', base+'small/'+fire+'?token=1', base+'tiny/'+fire, null]) assert.equal(await resolve(miss), miss);
    assert.equal(await resolve(base+'small/exact.jpg'), '/static/exact.jpg');
    assert.equal(requests.length, 0);
    const inputs = [base+'small/'+fire, 'https://render.worldofwarcraft.com/us/icons/56/'+frost,
                    base+'medium/'+fire,base+'small/not_published_fixture.jpg'];
    assert.deepEqual(await Promise.all(inputs.map(resolve)), [cdn+'small/'+fire, cdn+'large/'+frost,...inputs.slice(2)]);
    assert.deepEqual(requests,[['/static/release.json','omit','no-referrer']]);
});
test('unavailable inventory preserves original for every caller without retry storms', async () => {
    const {resolve,requests} = harness(false), url = base+'small/'+fire;
    assert.equal(await resolve(url), url); assert.equal(await resolve(url), url); assert.equal(requests.length,1);
});
