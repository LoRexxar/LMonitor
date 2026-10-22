/**
 * Profile UI/API behavior contracts. Run from the repository root:
 *   node --test botend/tests/profile_api_contract_test.js
 * Requires jsdom@26.1.0 (test-only); see botend/tests/README.md for setup.
 * NODE_PATH may point at an isolated installation; never a production dependency.
 *
 * Executes the complete dashboard script and actual template fragments, not
 * source substrings or extracted private helpers. Only HTTP is replaced with
 * explicit responses; no server, database, or external network is contacted.
 * Backend canonicalization/permissions/persistence are covered separately by
 * SimcProfileResourceListTests in test_simc_dashboard_api.py.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');

const root = path.resolve(__dirname, '../..');
const script = new vm.Script(fs.readFileSync(path.join(root, 'static/dashboard/js/main.js'), 'utf8'), {
    filename: 'static/dashboard/js/main.js',
});
const template = new JSDOM(fs.readFileSync(path.join(root, 'templates/dashboard/index.html'), 'utf8'));
// Keep the production profile/dialog markup, without booting unrelated panels.
const fixture = [
    'simc-workbench-profiles-panel', 'simc-workbench-dialog',
    'simc-sim-spec', 'simc-sim-bnet-spec', 'simc-profile-spec-filter',
    'simc-talent-string-spec-filter', 'simc-wb-mastery-form',
].map(id => {
    const node = template.window.document.getElementById(id);
    assert.ok(node, `production template must contain #${id}`);
    return node.outerHTML;
}).join('\n');
template.window.close();

const catalog = [
    { value: 'mage_frost', label: '冰霜 · 法师' },
    { value: 'deathknight_frost', spec_label: '冰霜', class_label: '死亡骑士' },
    { value: 'warrior_fury', label: '狂暴 · 战士' },
    // Deliberately not a client-known identity: choices belong to the API.
    { value: 'catalog_new_spec', label: 'A & B <img src=x onerror=alert(1)>' },
];
const tick = () => new Promise(resolve => setImmediate(resolve));
const response = (body, ok = true) => ({ ok, json: async () => body });

async function harness(t) {
    const dom = new JSDOM(fixture, { url: 'https://dashboard.invalid/', runScripts: 'outside-only' });
    t.after(() => dom.window.close());
    // Wait before evaluating: global DOMContentLoaded startup is deliberately
    // outside this profile boundary. Profile controls are bound explicitly below.
    await new Promise(resolve => dom.window.addEventListener('load', resolve, { once: true }));
    const { window } = dom;
    const calls = [];
    const unexpected = [];
    const handlers = new Map([
        ['GET /api/simc-spec-options/', () => response({ success: true, data: catalog })],
        ['GET /api/simc-profile/', () => response({ success: true, data: [] })],
    ]);
    window.fetch = async (url, options = {}) => {
        const call = { url, method: options.method || 'GET', ...options };
        calls.push(call);
        const handler = handlers.get(`${call.method} ${url}`);
        if (!handler) {
            unexpected.push(`${call.method} ${url}`);
            throw new Error(`Unexpected request: ${call.method} ${url}`);
        }
        return handler(call);
    };
    t.after(() => assert.deepEqual(unexpected, [], 'no generic CRUD or unplanned HTTP requests'));
    window.document.cookie = 'csrftoken=test-csrf';
    script.runInContext(dom.getInternalVMContext());
    return {
        window, document: window.document, calls, handlers,
        select: selector => window.document.querySelector(selector),
        rows: () => [...window.document.querySelectorAll('#simc-wb-profile-list [data-profile-row-action="view"]')]
            .map(node => Number(node.dataset.profileId)),
    };
}

function setField(h, name, value) {
    const field = h.select(`#simc-wb-profile-form [name="${name}"]`);
    assert.ok(field, `profile form field ${name}`);
    if (field.type === 'checkbox') field.checked = value;
    else field.value = value;
}

const specSelectors = [
    '#simc-sim-spec', '#simc-sim-bnet-spec', '#simc-wb-profile-spec-filter',
    '#simc-profile-spec-filter', '#simc-talent-string-spec-filter',
    '#simc-wb-profile-form-source select[name="spec"]', '#simc-wb-mastery-form select[name="spec"]',
];

test('dynamic catalog populates all consumers, preserves canonical selection and escapes labels', async t => {
    const h = await harness(t);
    h.select('#simc-sim-spec').innerHTML = '<option value="mage_frost">old label</option>';
    await Promise.all([h.window.loadSimcSpecOptions(), h.window.loadSimcSpecOptions()]);
    for (const selector of specSelectors) {
        const select = h.select(selector);
        assert.deepEqual([...select.options].map(option => option.value), ['', ...catalog.map(row => row.value)]);
        assert.equal(select.options[2].textContent, '冰霜 · 死亡骑士');
        assert.equal(select.options[4].textContent, catalog[3].label);
        assert.equal(select.querySelector('img'), null);
    }
    assert.equal(h.select('#simc-sim-spec').value, 'mage_frost');
    assert.equal(h.calls.length, 1, 'concurrent consumers share the catalog request');
    assert.equal(h.calls[0].headers['X-CSRFToken'], 'test-csrf');
});

test('catalog failure is surfaced, does not mark selects loaded, and can be retried', async t => {
    const h = await harness(t);
    h.handlers.set('GET /api/simc-spec-options/', () => response({ success: false, error: 'catalog unavailable' }, false));
    await assert.rejects(h.window.loadSimcSpecOptions(), /catalog unavailable/);
    assert.notEqual(h.select('#simc-wb-profile-spec-filter').dataset.specCatalogLoaded, '1');
    h.handlers.set('GET /api/simc-spec-options/', () => response({ success: true, data: catalog }));
    await h.window.loadSimcSpecOptions();
    assert.equal(h.calls.length, 2);
    assert.equal(h.select('#simc-wb-profile-spec-filter').options.length, catalog.length + 1);
});

test('canonical filtering works for every source and refuses ambiguous/raw identity fallback', async t => {
    const h = await harness(t);
    const matches = h.window.simcProfileMatchesSpecFilter;
    for (const source of [
        { is_system: true },
        { is_system: false, player_config_mode: 'manual_equipment' },
        { is_system: false, player_config_mode: 'battlenet' },
        { is_system: false, player_config_mode: 'attribute_only' },
    ]) {
        assert.equal(matches({ ...source, canonical_spec: ' MAGE_FROST ', spec: 'frost' }, ' mage_frost '), true);
        assert.equal(matches({ ...source, canonical_spec: 'deathknight_frost', spec: 'frost' }, 'mage_frost'), false);
        assert.equal(matches({ ...source, spec: 'mage_frost', class_name: 'mage' }, 'mage_frost'), false);
        assert.equal(matches({ ...source, canonical_spec: '', spec: 'frost' }, 'mage_frost'), false);
        assert.equal(matches(source, ''), true);
    }
});

test('list filters before pagination; changing catalog selection resets the page and preserves row capabilities', async t => {
    const h = await harness(t);
    const profiles = Array.from({ length: 21 }, (_, i) => ({
        id: i + 1, canonical_spec: 'mage_frost', spec: i % 2 ? 'mage_frost' : 'frost',
        name: `Profile ${i + 1}`, is_system: i === 0, is_active: true,
        can_edit: i !== 0, can_delete: i !== 0,
    }));
    profiles.splice(0, 0, { id: 99, canonical_spec: 'deathknight_frost', spec: 'frost', name: 'Other frost' });
    profiles.push({ id: 100, canonical_spec: '', spec: 'mage_frost', name: 'Unresolved' });
    h.handlers.set('GET /api/simc-profile/', () => response({ success: true, data: profiles }));
    h.window.bindSimcWorkbenchProfilesControls();
    await tick();
    h.window.loadSimcWorkbenchProfiles(2);
    await tick();
    assert.deepEqual(h.rows(), [20, 21, 100]);
    const filter = h.select('#simc-wb-profile-spec-filter');
    filter.value = 'mage_frost';
    filter.dispatchEvent(new h.window.Event('change'));
    await tick();
    assert.deepEqual(h.rows(), Array.from({ length: 20 }, (_, i) => i + 1));
    const first = h.select('#simc-wb-profile-list tr');
    assert.equal(first.querySelector('[data-profile-row-action="delete"]'), null);
    assert.equal(first.querySelector('[data-profile-row-action="edit"]'), null);
    assert.equal(first.querySelector('[data-profile-row-action="simulate"]').dataset.profileSpec, 'mage_frost');
    assert.ok(h.select('[data-profile-row-action="delete"][data-profile-id="2"]'));
    h.select('#simc-wb-profile-pagination [data-page="2"]').click();
    await tick();
    assert.deepEqual(h.rows(), [21]);
    filter.value = 'catalog_new_spec';
    filter.dispatchEvent(new h.window.Event('change'));
    await tick();
    assert.deepEqual(h.rows(), []);
    assert.match(h.select('#simc-wb-profile-list').textContent, /暂无配置/);
    for (const call of h.calls.filter(call => call.url === '/api/simc-profile/')) {
        assert.equal(call.method, 'GET');
        assert.equal(call.headers['X-CSRFToken'], 'test-csrf');
    }
});

test('create sends canonical spec, both mode fields, explicit PTR and nullable/zero stat overrides', async t => {
    const h = await harness(t);
    h.handlers.set('POST /api/simc-profile/', () => response({ success: true }));
    await h.window.simcWbToggleProfileForm('create');
    for (const [name, value] of Object.entries({
        name: ' New profile ', spec: 'mage_frost', player_config_mode: 'manual_equipment',
        player_equipment: 'mage="Tester"\nspec=frost', talent: 'BUILD', use_ptr: true,
        gear_strength: '0', gear_crit: '123',
    })) setField(h, name, value);
    await h.window.simcWbSaveProfile();
    const call = h.calls.find(call => call.method === 'POST');
    assert.ok(call);
    assert.deepEqual(JSON.parse(call.body), {
        name: 'New profile', spec: 'mage_frost', player_config_mode: 'manual_equipment',
        player_import_mode: 'manual_equipment', use_ptr: true,
        battlenet_region: 'eu', battlenet_realm: '', battlenet_character: '',
        player_equipment: 'mage="Tester"\nspec=frost', talent: 'BUILD',
        gear_strength: 0, gear_crit: 123, gear_haste: null, gear_mastery: null, gear_versatility: null,
    });
    assert.equal(call.headers['Content-Type'], 'application/json');
    assert.equal(call.headers['X-CSRFToken'], 'test-csrf');
    assert.ok(h.select('#simc-workbench-dialog').classList.contains('hidden'));
});

test('detail GET consumes top-level API fields and PUT preserves canonical identity, legacy mode, null and zero', async t => {
    const h = await harness(t);
    const profile = {
        success: true, id: 42, name: 'Existing', canonical_spec: 'mage_frost', spec: 'frost_death_knight',
        player_import_mode: 'equipment', player_equipment: 'mage="Old"\nspec=frost',
        use_ptr: true, gear_strength: 0, gear_crit: null, gear_haste: 25,
    };
    h.handlers.set('GET /api/simc-profile/42/', () => response(profile));
    // Equipment preview is independently optional, not a second profile envelope.
    h.handlers.set('GET /api/simc-player-config-detail/?profile_id=42', () => response({ success: false }, false));
    h.handlers.set('PUT /api/simc-profile/', () => response({ success: true }));
    await h.window.simcWbEditProfile(42);
    assert.equal(h.select('#simc-wb-profile-form [name="spec"]').value, 'mage_frost');
    assert.equal(h.select('#simc-wb-profile-form [name="player_config_mode"]').value, 'manual_equipment');
    assert.equal(h.select('#simc-wb-profile-form [name="gear_strength"]').value, '0');
    assert.equal(h.select('#simc-wb-profile-form [name="gear_crit"]').value, '');
    setField(h, 'name', 'Edited');
    await h.window.simcWbSaveProfile();
    const call = h.calls.find(call => call.method === 'PUT');
    const payload = JSON.parse(call.body);
    assert.equal(payload.id, 42);
    assert.equal(payload.name, 'Edited');
    assert.equal(payload.spec, 'mage_frost');
    assert.equal(payload.player_config_mode, 'manual_equipment');
    assert.equal(payload.player_import_mode, 'manual_equipment');
    assert.equal(payload.player_equipment, profile.player_equipment);
    assert.equal(payload.use_ptr, true);
    assert.equal(payload.gear_strength, 0);
    assert.equal(payload.gear_crit, null);
    assert.equal(payload.gear_haste, 25);
});

test('legacy disambiguated edit specs select canonical catalog identities, without static template options', async t => {
    const h = await harness(t);
    const aliases = {
        frost_death_knight: 'deathknight_frost', frost_mage: 'mage_frost',
        protection_paladin: 'paladin_protection', protection_warrior: 'warrior_protection',
        holy_paladin: 'paladin_holy', holy_priest: 'priest_holy',
        restoration_druid: 'druid_restoration', restoration_shaman: 'shaman_restoration',
    };
    h.handlers.set('GET /api/simc-spec-options/', () => response({
        success: true, data: Object.values(aliases).map(value => ({ value, label: value })),
    }));
    for (const [spec, canonical] of Object.entries(aliases)) {
        await h.window.simcWbToggleProfileForm('edit', { id: 42, name: spec, spec });
        assert.equal(h.select('#simc-wb-profile-form [name="spec"]').value, canonical, spec);
    }
    assert.equal(h.calls.length, 1, 'reopening the editor reuses the shared catalog');
});

test('rendered delete action confirms before sending dedicated DELETE with numeric id', async t => {
    const h = await harness(t);
    h.handlers.set('GET /api/simc-profile/', () => response({ success: true, data: [
        { id: 42, name: 'Deletable', canonical_spec: 'mage_frost', can_delete: true },
    ] }));
    h.handlers.set('DELETE /api/simc-profile/', () => response({ success: true }));
    h.window.bindSimcWorkbenchProfilesControls();
    h.window.loadSimcWorkbenchProfiles(1);
    await tick();
    const button = h.select('[data-profile-row-action="delete"]');
    assert.ok(button);
    button.click();
    assert.equal(h.calls.filter(call => call.method === 'DELETE').length, 0);
    button.click();
    await tick();
    const calls = h.calls.filter(call => call.method === 'DELETE');
    assert.equal(calls.length, 1);
    assert.deepEqual(JSON.parse(calls[0].body), { id: 42 });
    assert.equal(calls[0].headers['X-CSRFToken'], 'test-csrf');
});

test('saved-profile loader propagates signal, returns API data and rejects HTTP/application failures', async t => {
    const h = await harness(t);
    const profiles = [{ id: 7, canonical_spec: 'mage_frost', is_system: true }];
    h.handlers.set('GET /api/simc-profile/', () => response({ success: true, data: profiles }));
    const controller = new h.window.AbortController();
    assert.deepEqual(await h.window.simcWbFetchProfilesForWorkbench(controller.signal), profiles);
    assert.equal(h.calls[0].signal, controller.signal);
    h.handlers.set('GET /api/simc-profile/', () => response({ success: true, data: null }));
    assert.equal((await h.window.simcWbFetchProfilesForWorkbench()).length, 0);
    for (const [ok, success] of [[false, true], [true, false]]) {
        h.handlers.set('GET /api/simc-profile/', () => response({ success, error: 'cannot load' }, ok));
        await assert.rejects(h.window.simcWbFetchProfilesForWorkbench(), /cannot load/);
    }
});
