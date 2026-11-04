"""Run the unmodified production script and real source/scope click handlers in Node."""
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


PROGRAM = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
    constructor(tag = 'div') {
        this.tagName = tag; this.children = []; this.className = ''; this.ownText = '';
        this.style = {setProperty(key, value) {this[key] = value;}};
        this.attrs = {}; this.listeners = {}; this.dataset = {};
        this.classList = {
            add: (...names) => { this.className = [...new Set([...this.className.split(' '), ...names])].join(' '); },
            toggle: (name, on) => {
                this.className = this.className.split(' ').filter(value => value !== name).join(' ');
                if (on) this.classList.add(name);
            }
        };
    }
    set textContent(value) {this.ownText = String(value); this.children = [];}
    get textContent() {return this.ownText + this.children.map(child => child.textContent).join('');}
    get lastElementChild() {return this.children.at(-1);}
    append(...nodes) {this.children.push(...nodes);}
    appendChild(node) {this.append(node); return node;}
    replaceChildren(...nodes) {this.ownText = ''; this.children = [...nodes];}
    setAttribute(key, value) {this.attrs[key] = String(value);}
    addEventListener(event, callback) {this.listeners[event] = callback;}
    click() {this.listeners.click?.();}
    querySelector(selector) {return this.all().find(node => node.className.split(' ').includes(selector.slice(1))) || null;}
    all() {return this.children.flatMap(child => [child, ...child.all()]);}
}
const ids = new Map();
const get = id => {if (!ids.has(id)) ids.set(id, new Node()); return ids.get(id);};
const sources = ['local', 'mythicstats'].map(source => {const node = new Node('button'); node.dataset.source = source; return node;});
const document = {getElementById: get, createElement: tag => new Node(tag), querySelectorAll: () => sources};
const row = (average_dps, tier = 'S', extra = {}) => ({average_dps, tier, rank: 1,
    highest_dps: 10000, class_name_cn: '法师', spec_name_cn: '测试',
    detail_url: '/portal/spec/Mage/Arcane/dungeons/', ...extra});
const scopes = [
    ['overall', [7000.7, 7000.6, 7000.599999999999, 7000]],
    ['dungeon:1', [42, 35, 28, 21, 14, 7, 0]],
    ['dungeon:2', [0, 0]], ['dungeon:3', [123]],
    ['dungeon:4', [7000.000000000001, 7000]],
    ['dungeon:5', [7e-13, 6e-13, 5.999999999999e-13, 0]],
    ['dungeon:6', [null, true, false, '', 'bad', 'NaN', -1, 5, 4]],
    ['dungeon:7', []],
];
const local = {generated_at: '2026-09-05T01:02:03Z', source_updated_at: '2026-09-04T00:00:00Z',
    scopes: scopes.map(([key]) => ({key, label: key})),
    rankings: Object.fromEntries(scopes.map(([key, values]) => [key, values.map(value => row(value))])),
    method: {sample_cap_per_spec_dungeon: 100, time_weighting: {kind: 'exponential', half_life_days: 7}}};
local.rankings['dungeon:6'].push(row(99999, 'E', {source: 'mythicstats'}));
const mythicRow = (avg_value, tier) => ({...row(undefined, tier), avg_value, top_value: 10000,
    spec_url: '/spec/frost-mage', runs: '16K', diff_raw: '+2', diff_value: 2});
const mythic = {roles: {damage: [mythicRow(10000, 'F'), mythicRow(1, 'S'), mythicRow(1, 'A')],
    tank: [mythicRow(0, 'E')], healer: [mythicRow(0, 'B')]},
    source_note: '原口径', source_url: '/dps', periods: []};
const frozen = JSON.stringify(local);
const calls = [];
const context = {document, URL, URLSearchParams, console,
    window: {location: {href: 'https://example.test/portal/mplus/dps-rankings/', search: ''}, history: {replaceState() {}}},
    localStorage: {getItem() {return '';}, setItem() {}},
    fetch: async url => {calls.push(url); return {ok: true, json: async () => url.includes('mythicstats') ? {data: mythic} : local};}};
vm.runInNewContext(fs.readFileSync('static/portal/js/mplus-dps-rankings.js', 'utf8'), context);
const settled = () => new Promise(resolve => setImmediate(resolve));
const badges = () => get('mplus-rank-list').all().filter(n => n.className.split(' ').includes('mplus-rank-tier')).map(n => n.textContent);
const labels = () => get('mplus-rank-tier-groups').children.map(group => group.children[0].children[1]);
const selectScope = async key => {
    const label = {damage: '输出', tank: '坦克', healer: '治疗'}[key] || key;
    const tab = get('mplus-rank-tabs').children.find(tab => tab.textContent === label);
    assert.ok(tab, `missing scope ${key}`);
    tab.click(); await settled();
};
(async () => {
    await settled();
    assert.deepEqual(badges(), ['S', 'S', 'A', 'F'], 'local must ignore cached old S tiers');
    const overallLabels = labels().map(label => label.textContent);
    assert.equal(overallLabels.length, 7);
    assert.ok(overallLabels.every(label => label.includes('约') && label.includes('DPS') && !label.includes('%')));
    assert.ok(labels().every(label => label.title.includes('/7') && label.title.includes('7000')));
    assert.ok(get('mplus-rank-method').textContent.includes('等分为 7 档'));
    assert.ok(!get('mplus-rank-method').textContent.includes('95%'));
    assert.ok(get('mplus-rank-method').textContent.includes(overallLabels[0]));
    await selectScope('dungeon:1');
    assert.deepEqual(badges(), ['S', 'A', 'B', 'C', 'D', 'E', 'F']);
    const dungeonLabels = labels().map(label => label.textContent);
    assert.notDeepEqual(dungeonLabels, overallLabels);
    assert.ok(get('mplus-rank-method').textContent.includes(dungeonLabels[0]), 'method bands follow active scope');
    for (const key of ['dungeon:2', 'dungeon:3']) {
        await selectScope(key);
        assert.ok(badges().every(tier => tier === 'S'));
        assert.ok(labels()[0].textContent.includes('并列'));
    }
    await selectScope('dungeon:4');
    assert.deepEqual(badges(), ['S', 'F']);
    const tiny = labels().map(label => label.textContent);
    assert.equal(new Set(tiny).size, 7, 'small-range labels cannot collapse');
    assert.ok(tiny.every(label => label.length < 65 && !label.match(/\d{15}/)), 'no 17-digit fine print');
    await selectScope('dungeon:5');
    assert.deepEqual(badges(), ['S', 'S', 'A', 'F']);
    await selectScope('dungeon:6');
    assert.deepEqual(badges(), ['—', '—', '—', '—', '—', '—', '—', 'S', 'F', 'E']);
    await selectScope('dungeon:7');
    assert.deepEqual(badges(), []);
    assert.equal(get('mplus-rank-tier-board').hidden, true);
    assert.equal(get('mplus-rank-list').hidden, true);
    await selectScope('dungeon:1');
    sources[1].click(); await settled();
    assert.deepEqual(badges(), ['F', 'S', 'A'], 'Mythicstats must keep counterintuitive source tiers');
    assert.ok(labels().every(label => label.textContent === '' && !label.title), 'no local intervals on forwarded data');
    assert.ok(get('mplus-rank-method').textContent.includes('评级沿用来源数据，输出、坦克、治疗分别排名'));
    assert.ok(!get('mplus-rank-method').textContent.includes('等分'));
    const plot = get('mplus-rank-list').all().find(n => n.className === 'mplus-rank-plot');
    assert.equal(plot.href, 'https://mythicstats.com/spec/frost-mage');
    assert.equal(plot.target, '_blank');
    assert.ok(get('mplus-rank-list').textContent.includes('16K'));
    assert.ok(get('mplus-rank-list').textContent.includes('+2'));
    await selectScope('tank'); assert.deepEqual(badges(), ['E']);
    await selectScope('healer'); assert.deepEqual(badges(), ['B']);
    sources[0].click(); await settled();
    assert.deepEqual(badges(), ['S', 'A', 'B', 'C', 'D', 'E', 'F']);
    assert.deepEqual(labels().map(label => label.textContent), dungeonLabels);
    assert.equal(JSON.stringify(local), frozen, 'renderer does not rewrite source payload');
    assert.equal(calls.length, 2, 'switch back uses the existing source cache');
    console.log('PASS real renderer: exact local scopes, dynamic ranges, unknowns, Mythicstats tiers/roles/links, source round trip');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""


class MplusDpsEqualRangeUiTests(SimpleTestCase):
    def test_real_renderer_local_equal_ranges_and_mythicstats_source_isolation(self):
        result = subprocess.run(
            ['node', '-e', PROGRAM], cwd=Path(settings.BASE_DIR),
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
