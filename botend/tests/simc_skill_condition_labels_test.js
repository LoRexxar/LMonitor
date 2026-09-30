const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Execute the real table renderer: the condition cell must preserve every
// independently frozen talent, health and runtime-state condition.
const source = fs.readFileSync(path.join(__dirname, '../../static/dashboard/js/main.js'), 'utf8');
const elements = new Map();
function element(id) {
    if (!elements.has(id)) elements.set(id, {value: '', dataset: {}, innerHTML: '', textContent: '',
        classList: {add() {}, remove() {}}, setAttribute() {}});
    return elements.get(id);
}
element('simc-skill-damage-spec').value = 'warrior:fury';
element('simc-skill-damage-hero-tree').value = '60';
const context = vm.createContext({
    document: {getElementById: element, querySelectorAll: () => [{dataset: {targetCount: '1'}, getAttribute: () => 'true'}]},
    escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;'),
});
const start = source.indexOf('function renderSimcSkillIdentity(');
vm.runInContext(source.slice(start, source.indexOf('function initSimcSkillDamagePanel(', start)), context);
const product = {damage_metric: 'critical_expectation', normalized_base_damage: 100,
    final_normalized_damage: 156, formula_components: [{base_damage: 100, runtime_factors: [1.3],
        noncrit_damage: 130, crit_damage: 260, crit_chance: 0.2,
        noncrit_contribution: 104, crit_contribution: 52, final_damage: 156}]};
function render(variant) {
    const payload = {actors: [{class: 'warrior', specialization: 'fury',
        hero_talent_trees: [{id: 60}], actions: [{spell_id: 123, display_name: '验证技能', variant, product}]}]};
    const frozen = JSON.stringify(payload);
    context.renderSimcSkillDamageSnapshot(payload);
    assert.equal(JSON.stringify(payload), frozen, 'rendering must not change frozen conditions or damage');
    const cells = [...element('simc-skill-damage-body').innerHTML.matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/g)].map(m => m[1]);
    assert.equal(cells.length, 5);
    assert.match(cells[3], /基础伤害 100 × 1\.3 ≈ 130/);
    assert.equal(cells[4], '156.00');
    return cells[1].replace(/<[^>]+>/g, '');
}
const talent = {talent_id: 10, trait_entry_id: 20, talent_name_zh: '已选天赋'};
assert.equal(render({...talent, runtime_condition: '血量低于35%'}), '点出已选天赋，血量低于35%');
const buff = {token: 'buff.measured', scope: 'self', spell_id: 456, name_zh: '测量状态'};
assert.equal(render({...talent, runtime_condition: '自身存在测量状态效果时',
    scenario_tokens: ['buff.measured'], runtime_conditions: [buff]}), '点出已选天赋，自身存在测量状态效果时');
assert.equal(render({...talent, runtime_condition: '点出已选天赋，血量低于35%',
    scenario_tokens: ['buff.measured'], runtime_conditions: [buff]}), '点出已选天赋，血量低于35%，自身存在 测量状态 效果时');
assert.equal(render({...talent, runtime_condition: ''}), '点出已选天赋');
assert.equal(render({runtime_condition: '血量低于35%'}), '血量低于35%');
assert.equal(render({}), '基础技能');
assert.equal(render({...talent, talent_name_zh: '<已选天赋>'}), '点出&lt;已选天赋>天赋');
const referenceContext = {source: 'native_reference_selection', trait_entry_ids: [30, 40],
    traits: [{trait_entry_id: 30, name_zh: '固定前置'}, {trait_entry_id: 40}]};
assert.match(render({...talent, reference_context: referenceContext}), /固定参考天赋：固定前置、TraitEntry 40/);
console.log('条件列保留天赋、血量与Buff，不重复标签、不修改公式、正确转义。');
