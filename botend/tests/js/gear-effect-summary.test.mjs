import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../../../static/portal/js/gear_builder.js', import.meta.url), 'utf8');
const context = {document: {querySelector: () => ({dataset: {}}), getElementById: () => ({})}};
vm.runInNewContext(source.replace('  initialize();', 'globalThis.qa = {state, effectSummaryGroups, effectSummaryMarkup, totalsAndEffects};'), context);
const {state, effectSummaryGroups, effectSummaryMarkup, totalsAndEffects} = context.qa;
const gem = {item: {item_id: 1, name: '迅捷宝石'}, variant: {id: 1, stats: {haste: 17}, effects: []}};
const enchant = {item: {item_id: 2, name: '急速附魔'}, variant: {id: 2, stats: {haste: 21}, effects: []}};
const beauty = {item: {item_id: 3, name: '美化<测试>'}, variant: {id: 3, effects: [{description_zh: '触发美化提升100急速'}]}};
state.equipment = {
 neck: {item: {name: '自带美化项链'}, variant: {id: 10, is_intrinsic_embellishment: true, stats: {stamina: 100}, effects: [{description_zh: '凤凰触发'}]}, gems: [gem], enchant},
 main_hand: {item: {name: '制造武器'}, variant: {id: 11, stats: {strength: 100}, effects: [{description_zh: '武器自身特效'}]}, resolvedEffects: [{description_zh: '武器自身特效'}, ...beauty.variant.effects], embellishment: beauty, gems: [gem], enchant},
 trinket1: {item: {name: '饰品'}, variant: {id: 12, effects: [{description_zh: '使用饰品触发'}]}},
};
state.lockedSlots = ['neck'];
const totals = JSON.stringify(totalsAndEffects());
const groups = effectSummaryGroups();
assert.deepEqual(Array.from(groups, g => g.key), ['gems', 'enchants', 'equipment', 'embellishments']);
assert.equal(groups[0].count, 2);assert.equal(groups[0].rows.length, 1);assert.equal(groups[0].stats.haste, 17 * 2);
assert.equal(groups[1].count, 2);assert.equal(groups[1].rows.length, 1);assert.equal(groups[1].stats.haste, 21 * 2);
assert.equal(groups[2].count, 2);assert.equal(groups[3].count, 2);
assert.equal(Object.keys(groups[3].stats).length, 0, '固有美化不得把装备的耐力算作美化属性');
const markup = effectSummaryMarkup(groups);
for (const label of ['宝石', '附魔', '装备特效', '美化']) assert.ok(markup.includes(label));
assert.ok(markup.includes('迅捷宝石') && markup.includes('×2'));
assert.equal(markup.split('触发美化提升100急速').length - 1, 1, '制造已解析效果中的美化不重复混入装备特效');
assert.ok(markup.includes('美化&lt;测试&gt;') && !markup.includes('美化<测试>'));
assert.equal(JSON.stringify(totalsAndEffects()), totals, '分类只改变显示，不改变属性计算或装备');
state.equipment.main_hand.gems.push({...gem, variant: {...gem.variant, id: 4, stats: {haste: 12}}});
assert.equal(effectSummaryGroups()[0].rows.length, 2, '同名不同品质/属性不合并');
state.equipment = {};
assert.equal(effectSummaryGroups().length, 4);assert.ok(effectSummaryMarkup(effectSummaryGroups()).includes('未镶嵌宝石'));
console.log('四类汇总：重复合并、纯属性强化、自带/附加美化分离、数值不变、转义与空状态通过');
