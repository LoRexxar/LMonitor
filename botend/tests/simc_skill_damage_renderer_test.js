const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 在轻量 DOM 替身中执行真实渲染函数，验证公式与条件筛选的输出契约。
const source = fs.readFileSync(path.join(__dirname, '../../static/dashboard/js/main.js'), 'utf8');
const start = source.indexOf('function renderSimcSkillIdentity(');
const end = source.indexOf('function initSimcSkillDamagePanel(', start);
const elements = new Map();
function element(id) {
    if (!elements.has(id)) elements.set(id, {
        value: '', dataset: {}, innerHTML: '', textContent: '',
        classList: {add() {}, remove() {}}, setAttribute() {},
    });
    return elements.get(id);
}
element('simc-skill-damage-spec').value = 'warrior:fury';
element('simc-skill-damage-hero-tree').value = '60';
const target = {dataset: {targetCount: '1'}, getAttribute: () => 'true'};
const context = vm.createContext({
    document: {getElementById: element, querySelectorAll: () => [target]},
    escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;'),
});
vm.runInContext(source.slice(start, end), context);
const payload = {actors: [{class: 'warrior', specialization: 'fury',
    hero_talent_trees: [{id: 60, name_zh: '屠戮者'}], actions: [{
        spell_id: 23881, display_name: '嗜血', variant: {},
        product: {normalized_base_damage: 100, final_normalized_damage: 120,
            final_normalized_damage_by_target: {'2': 180},
            formula_components: [{base_damage: 100, runtime_factors: [1.2], final_damage: 120,
                final_damage_by_target: {'2': 180}}]},
    }]}]};
function render() {
    context.renderSimcSkillDamageSnapshot(payload);
    return element('simc-skill-damage-body').innerHTML;
}
assert.match(render(), /≈/);
target.dataset.targetCount = '2';
assert.match(render(), /（多目标）/);
const formula = payload.actors[0].actions[0].product.formula_components[0];
formula.final_damage = 0;
assert.match(render(), /公式未完整解析/);
formula.final_damage = 120;
target.dataset.targetCount = '1';
formula.base_damage = 90;
assert.match(render(), /公式未完整解析/);
formula.base_damage = 100;
formula.status = 'incomplete';
assert.match(render(), /公式未完整解析/);
delete formula.status;
const row = payload.actors[0].actions[0];
row.variant = {scenario_tokens: ['buff.whirlwind'], runtime_conditions: [{
    token: 'buff.whirlwind', scope: 'self', spell_id: 85739, name_zh: '旋风斩',
    stacks: 1, stack_values: [1, 2, 3, 4],
}]};
assert.match(render(), /自身存在 旋风斩 效果时/);
assert.doesNotMatch(render(), /自身存在 whirlwind 效果时/);
row.variant.runtime_condition = '点出测试天赋，血量低于35%';
assert.match(render(), /点出测试天赋，血量低于35%，自身存在 旋风斩 效果时/);
row.variant.runtime_condition = '点出测试天赋，且自身存在旋风斩效果时';
assert.doesNotMatch(render(), /自身存在旋风斩效果时，自身存在/);
row.variant.runtime_conditions[0].stacks = 3;
assert.match(render(), /自身存在旋风斩（3层）效果时/);
assert.doesNotMatch(render(), /自身存在旋风斩（3层）效果时，自身存在/);
row.variant.runtime_condition = '';
assert.match(render(), /自身存在 旋风斩（3层） 效果时/);
row.variant.runtime_conditions[0].stacks = 1;
delete row.variant.scenario_tokens;
row.variant.runtime_condition = '点出测试天赋';
assert.match(render(), /点出测试天赋，自身存在 旋风斩 效果时/);
row.variant.scenario_tokens = ['buff.whirlwind'];
row.variant.runtime_condition = '';
const singleStack = structuredClone(row);
delete singleStack.variant.runtime_conditions[0].stack_values;
payload.actors[0].actions.push(singleStack);
render();
const filters = element('simc-skill-damage-filter-buffs').innerHTML;
assert.match(filters, /1\/2\/3\/4层等伤害/);
const keys = [...filters.matchAll(/data-condition-key="([^"]+)"/g)].map(match => match[1]);
assert.equal(keys.length, 2);
assert.equal(new Set(keys).size, 2);
assert.match(render(), /旧快照为非暴击值，需重新生成/);

payload.actors[0].actions = [{
    spell_id: 23881, display_name: '普通暴击测试', variant: {},
    product: {damage_metric: 'critical_expectation', normalized_base_damage: 100,
        final_normalized_damage: 120, final_normalized_damage_by_target: {'1': 120, '2': 250},
        formula_components: [{base_damage: 100, runtime_factors: [],
            noncrit_damage: 100, crit_damage: 200, crit_chance: 0.2,
            noncrit_contribution: 80, crit_contribution: 40, final_damage: 120,
            noncrit_damage_by_target: {'2': 150}, final_damage_by_target: {'2': 250},
            noncrit_contribution_by_target: {'2': 90}, crit_contribution_by_target: {'2': 160}}]},
}];
const expectedHtml = render();
assert.match(expectedHtml, /伤害期望：100 × 80(?:\.00)?% \+ 200 × 20(?:\.00)?%（暴击） ≈ 120/);
assert.doesNotMatch(expectedHtml, /旧快照|公式未完整解析|证据不完整/);
const criticalRow = structuredClone(payload.actors[0].actions[0]);
criticalRow.display_name = '高暴击伤害测试';
criticalRow.spell_id = 23882;
criticalRow.product.final_normalized_damage = 130;
criticalRow.product.final_normalized_damage_by_target['1'] = 130;
Object.assign(criticalRow.product.formula_components[0], {crit_damage: 250, crit_contribution: 50, final_damage: 130});
payload.actors[0].actions.push(criticalRow);
const sortedHtml = render();
assert.match(sortedHtml, /250 × 20(?:\.00)?%（暴击） ≈ 130/);
assert.ok(sortedHtml.indexOf('高暴击伤害测试') < sortedHtml.indexOf('普通暴击测试'));

target.dataset.targetCount = '2';
const multiHtml = render();
assert.match(multiHtml, /90（非暴击部分）\+ 160（暴击部分） ≈ 250/);
assert.doesNotMatch(multiHtml, /公式未完整解析|证据不完整/);
criticalRow.product.formula_components[0].crit_contribution_by_target['2'] = 150;
assert.match(render(), /暴击期望证据不完整/);

target.dataset.targetCount = '1';
criticalRow.product.formula_components[0].crit_chance = 0.3;
assert.match(render(), /暴击期望证据不完整/);
console.log('真实渲染函数的基础公式、旧快照提示、暴击期望、排序、多目标与证据一致性断言通过。');
payload.unresolved = [{class: 'monk', specialization: 'windwalker', target_health_percentage: 34,
    action: {name: '触发伤害'}, reason: 'runtime_damage_context_unavailable'}];
render();
assert.match(element('simc-skill-damage-unresolved').innerHTML, /触发伤害/);
assert.match(element('simc-skill-damage-unresolved').innerHTML, /列表不代表完整覆盖/);
