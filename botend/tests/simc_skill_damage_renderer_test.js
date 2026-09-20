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
        description_zh: '攻击目标并造成伤害。',
        product: {normalized_base_damage: 100, final_normalized_damage: 120,
            final_normalized_damage_by_target: {'2': 180},
            formula_components: [{base_damage: 100, runtime_factors: [1.2], final_damage: 120,
                final_damage_by_target: {'2': 180}}]},
    }]}]};
function render() {
    context.renderSimcSkillDamageSnapshot(payload);
    return element('simc-skill-damage-body').innerHTML;
}
function renderText() {
    return render().replace(/<[^>]+>/g, '');
}
payload.actors[0].global_skill_effects = [{display_name:'激怒', source_spell_ids:[184362],
    description_zh:'提高你造成的伤害。',
    effect_details:[{label:'直接伤害',value_kind:'mastery',normalized_mastery_percent:50},
        {label:'周期伤害',value_kind:'mastery',normalized_mastery_percent:50}],
    runtime_condition:'自身激怒存在时'},
    {display_name:'狂暴姿态',source_spell_ids:[386196],
        effect_details:[{label:'自动攻击伤害',value_kind:'percent',base_value:15}]},
    {display_name:'防御姿态',source_spell_ids:[386208],
        projections:[{kind:'damage_multiplier',value:0.9}],runtime_condition:'防御姿态生效时'}];
render();
const globalHtml = element('simc-skill-damage-global-modifiers').innerHTML;
assert.match(element('simc-skill-damage-body').innerHTML, /data-wow-item-tooltip="攻击目标并造成伤害。"/);
assert.match(globalHtml, /data-wow-item-tooltip="提高你造成的伤害。"/);
assert.match(globalHtml, /全局伤害效果/);
assert.match(globalHtml, /激怒/);
assert.match(globalHtml, /直接伤害 \+50\.00%（精通50%时）/);
assert.match(globalHtml, /周期伤害 \+50\.00%（精通50%时）/);
assert.match(globalHtml, /自动攻击伤害 \+15\.00%/);
assert.match(globalHtml, /-10\.00%/);
assert.doesNotMatch(globalHtml, /共 \d+ 个效果分量|已剔除的全局分量/);
payload.actors[0].global_skill_effects.push({display_name:'配置变化的增伤',source_spell_ids:[999],
    effect_details:[{label:'直接伤害',value_kind:'percent',base_value:10}],
    projections:[{kind:'damage_multiplier_range',minimum:1.1,maximum:1.3}]});
render();
assert.match(element('simc-skill-damage-global-modifiers').innerHTML,/已验证条件下的加成：\+10\.00% 至 \+30\.00%/);
assert.match(element('simc-skill-damage-global-modifiers').innerHTML,/基础加成：直接伤害 \+10\.00%/);
payload.actors[0].global_skill_effects.push({display_name:'另一配置的增伤',source_spell_ids:[999],
    projections:[{kind:'damage_multiplier_range',minimum:1.2,maximum:1.4}]});
render();
assert.match(element('simc-skill-damage-global-modifiers').innerHTML,/已验证条件下的加成：\+20\.00% 至 \+40\.00%/);
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
    stacks: 1, stack_values: [1, 2, 3, 4], description_zh: '使后续伤害提高。',
}], talent_name_zh: '测试天赋', talent_description_zh: '强化技能的伤害。'};
assert.match(renderText(), /自身存在 旋风斩 效果时/);
assert.match(render(), /data-wow-item-tooltip="使后续伤害提高。"/);
assert.match(render(), /data-wow-item-tooltip="强化技能的伤害。"/);
assert.doesNotMatch(render(), /自身存在 whirlwind 效果时/);
row.variant.runtime_condition = '点出测试天赋，血量低于35%';
assert.match(renderText(), /点出测试天赋，血量低于35%，自身存在 旋风斩 效果时/);
row.variant.runtime_condition = '点出测试天赋，且自身存在旋风斩效果时';
assert.doesNotMatch(render(), /自身存在旋风斩效果时，自身存在/);
row.variant.runtime_conditions[0].stacks = 3;
assert.match(renderText(), /自身存在旋风斩（3层）效果时/);
assert.doesNotMatch(render(), /自身存在旋风斩（3层）效果时，自身存在/);
row.variant.runtime_condition = '';
assert.match(renderText(), /自身存在 旋风斩（3层） 效果时/);
row.variant.runtime_conditions[0].stacks = 1;
delete row.variant.scenario_tokens;
row.variant.runtime_condition = '点出测试天赋';
assert.match(renderText(), /点出测试天赋，自身存在 旋风斩 效果时/);
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

// 目标数校验发生在生成筛选项之前，不能只隐藏伤害数值。
payload.actors[0].actions = [{display_name:'横扫验证技能', affected_target_counts:[2,5,10,20],
    variant:{scenario_tokens:['buff.sweeping_strikes'],runtime_conditions:[{
        token:'buff.sweeping_strikes',scope:'self',spell_id:260708,name_zh:'横扫验证状态',stacks:1,
    }]},product:{final_normalized_damage:120,final_normalized_damage_by_target:{'2':180}}}];
target.dataset.targetCount = '1';
assert.doesNotMatch(render(), /横扫验证技能/);
assert.doesNotMatch(element('simc-skill-damage-filter-buffs').innerHTML, /横扫验证状态/);
target.dataset.targetCount = '2';
assert.match(render(), /横扫验证技能/);
assert.match(element('simc-skill-damage-filter-buffs').innerHTML, /横扫验证状态/);
target.dataset.targetCount = '1';
assert.doesNotMatch(render(), /横扫验证技能/);
console.log('仅多目标有效的状态不会出现在单目标伤害行或条件筛选项中。');

// 单目标为零的真实分量，不能从零单目标伤害反推多目标倍率。
payload.actors[0].actions=[{display_name:'副目标分量测试',variant:{},product:{
    damage_metric:'critical_expectation',normalized_base_damage:100,final_normalized_damage:0,
    final_normalized_damage_by_target:{'1':0,'2':120},formula_components:[{
        base_damage:100,runtime_factors:[0],noncrit_damage:0,crit_damage:0,crit_chance:0.2,
        noncrit_contribution:0,crit_contribution:0,final_damage:0,
        noncrit_damage_by_target:{'2':100},noncrit_contribution_by_target:{'2':80},
        crit_contribution_by_target:{'2':40},final_damage_by_target:{'2':120},
    }],
}}];
target.dataset.targetCount='2';
assert.match(render(),/多目标分量 100/);
assert.doesNotMatch(render(),/公式未完整解析|暴击期望证据不完整/);
target.dataset.targetCount='1';
assert.doesNotMatch(render(),/多目标分量|公式未完整解析|暴击期望证据不完整/);
payload.actors[0].actions[0].product.formula_components[0].status='incomplete';
target.dataset.targetCount='2';
assert.match(render(),/公式未完整解析/);
console.log('零单目标分量的多目标公式按实算值列示，并保留真正的证据缺失提示。');

// 无额外增伤场景时，替换技能的施法前提仍须显示并参与筛选。
payload.actors[0].actions = [{display_name:'浴血奋战验证', variant:{
    talent_id:119139, talent_name_zh:'替换天赋', scenario_tokens:[],
    runtime_conditions:[{token:'buff.recklessness',scope:'self',spell_id:1719,name_zh:'鲁莽',stacks:1}],
}, product:{final_normalized_damage:120, final_normalized_damage_by_target:{'1':120,'2':120}}}];
target.dataset.targetCount='1';
assert.match(render(), /浴血奋战验证/);
assert.match(renderText(), /自身存在 鲁莽 效果时/);
assert.match(element('simc-skill-damage-filter-buffs').innerHTML, /自身：鲁莽/);
console.log('替换技能的施法前提在无额外增伤场景时仍正确显示。');

// 索引首屏没有 actor payload 时，英雄天赋仍必须可选。
const loadedActors = payload.actors;
payload.actors = [];
payload.actor_index = [{class_name: 'warrior', specialization: 'fury', hero_talent_trees: [
    {id: 60, name_zh: '屠戮者'}, {id: 61, name_zh: '山丘领主'},
]}];
element('simc-skill-damage-hero-tree').value = '';
render();
assert.match(element('simc-skill-damage-spec').innerHTML, /value="warrior:fury"/);
assert.match(element('simc-skill-damage-hero-tree').innerHTML, /屠戮者/);
assert.match(element('simc-skill-damage-hero-tree').innerHTML, /山丘领主/);
payload.actors = loadedActors;
console.log('索引首屏也能加载英雄天赋选择项。');
