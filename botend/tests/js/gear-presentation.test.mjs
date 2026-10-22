import assert from 'node:assert/strict';
import {test} from 'node:test';
import harness from '../vm_harness.js';
const {gearRenderer} = harness;

test("gem and embellishment text: normalization, provenance, missing values", () => {

// 调用页面真实格式化函数，验证整段提示清理时不误删有效特效。
const context = gearRenderer("globalThis.qa = {gemDescription, tooltipText, optionRow};");
const {gemDescription, tooltipText, optionRow} = context.qa;
const upgraded = {
  type: 'drop_equipment', track_label: '神话', track_rank: 6, track_max_rank: 6,
  tooltip: '物品等级 334\n升级：勇士 6/6\n头部 板甲\n+189 力量',
};
assert.equal(tooltipText({}, upgraded), '物品等级 334\n升级：神话 6/6\n头部 板甲\n+189 力量', '升级档位读取当前变体，不沿用基础物品的勇士标签');
assert.ok(upgraded.tooltip.includes('升级：勇士 6/6'), '不修改中央来源原文');
assert.equal(tooltipText({}, {...upgraded, track_rank: 8, tooltip: 'Upgrade: Hero 6/6\n装备：升级效果仍保留。'}), '升级：神话 8/6\n装备：升级效果仍保留。', '英文基础档位同样使用后端当前档位，不改动特效');
const item = {name: '无瑕迅捷榄石', description: '无瑕迅捷榄石 榄石 物品等级： 295 + 17 急速 使用: 最大叠加: 200 售价: 3 10'};
const gem = {type: 'gem', stats: {haste: 17}, effects: [], tooltip: item.description};
assert.equal(gemDescription(item, gem), '急速 17', '整段提示只保留有效属性');
assert.equal(tooltipText(item, gem), '急速 17', '悬停提示同样清理旧说明');
assert.equal(gemDescription({...item, name: '无瑕精湛紫晶', description: '无瑕精湛紫晶 紫晶\n物品等级：295\n+17 精通\n使用：\n最大叠加：200\n售价：3金10银'}, {...gem, stats: {mastery: 17}}), '精通 17');
assert.equal(gemDescription({name: '无瑕迅捷榴石', description: '榴石\n+ 16 爆击和+ 7 急速'}, {...gem, stats: {crit: 16, haste: 7}}), '暴击 16 · 急速 7', '爆击别名不重复展示');
assert.equal(gemDescription({name: '测试宝石', description: '测试宝石 物品等级: 295 +17 急速 使用: 提高 200 点力量，持续 10 秒。 最大叠加: 200 售价: 3 10'}, {...gem, stats: {}}), '+17 急速 使用: 提高 200 点力量 · 持续 10 秒。', '数据缺失时保留文本属性和非空使用效果');
assert.equal(gemDescription(item, {...gem, effects: [{description_zh: '装备：每种不同颜色使暴击效果提高0.15%。'}, {description_zh: '每种不同颜色使暴击效果提高0.15%。'}]}), '急速 17 · 装备:每种不同颜色使暴击效果提高0.15%。', '有效特效保留且去重');
assert.equal(gemDescription({name: '测试宝石', description: '+23 主属性，受到失控效果影响时 +5% 伤害减免'}, {...gem, stats: {strength: 23}}), '力量 23 · 受到失控效果影响时 +5% 伤害减免');
assert.equal(gemDescription({name: '测试宝石', description: '+17 急速'}, {...gem, stats: {haste: 16}}), '急速 16 · +17 急速', '不把与结构化数值不一致的文字静默删除');
assert.equal(gemDescription({...item, text_schema_version: 2, description: '一段独立的风味描述'}, {...gem, text_schema_version: 2}), '急速 17', '规范化描述不再作为特效回退');
const embellishment = {
  item_id: 273060, name: '猎人仪式石', text_schema_version: 2,
  description: 'Optional Crafting Reagent\n+5 Recipe Difficulty\nUsable with: Midnight Blacksmithing Weapons',
  variants: [{id: 49308, type: 'embellishment', tooltip: '旧版完整制作说明', effects: [
    {description: 'Your damaging spells and abilities have a chance to grant 101 of a random secondary stat for 15 sec.',
     description_zh: '你的伤害性法术和技能有一定几率使一项随机次要属性提高101，持续15秒。'},
  ]}],
};
const effect = embellishment.variants[0].effects[0].description_zh;
const markup = optionRow(embellishment, 'embellishment');
assert.ok(markup.includes('猎人仪式石') && markup.includes(effect), '美化名称和特效使用中文，并保留完整标点');
for (const text of ['Optional Crafting Reagent', 'Recipe Difficulty', 'Usable with', 'Your damaging', '旧版完整制作说明', 'gear-option-description']) {
  assert.ok(!markup.includes(text), `美化列表不能混入无关描述：${text}`);
}
assert.equal(tooltipText(embellishment, embellishment.variants[0]), effect, '美化悬停也只显示特效');
assert.equal(tooltipText(embellishment, {...embellishment.variants[0], effects: [{description_zh: `提供下列属性：${effect}`}]}), effect, '仅展示中文特效正文，不重复通用标题');
assert.equal(tooltipText(embellishment, {...embellishment.variants[0], effects: [{description: 'Provides the following property: English only'}]}), '特效数据待补全', '无中文时不显示英文制作信息');
assert.equal(tooltipText(embellishment, {...embellishment.variants[0], effects: []}), '特效数据待补全', '缺失特效不能回退到制作说明');
console.log('宝石与美化说明验证通过：中文特效、完整标点、制作说明隔离、缺失效果与悬停去重。');

});

test("effect grouping: counts, escaping, immutable totals", () => {
const context = gearRenderer("bootstrap = {slots: [\"neck\", \"main_hand\", \"trinket1\"].map(key => ({key}))}; globalThis.qa = {state, effectSummaryGroups, effectSummaryMarkup, totalsAndEffects};");
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
assert.ok(!markup.includes('触发美化提升100急速'), '紧凑汇总不展示特效正文');
assert.ok(!markup.includes('gear-effect-description') && !markup.includes('gear-effect-static') && !markup.includes('gear-effect-sources'), '逐项仅显示名称和数量');
assert.equal(groups[3].rows.flatMap(row => row.descriptions).filter(text => text === '触发美化提升100急速').length, 1, '源特效保留且不重复分类');
assert.ok(markup.includes('常驻属性合计'), '分组属性总和仍保留一行');
assert.ok(markup.includes('美化&lt;测试&gt;') && !markup.includes('美化<测试>'));
assert.equal(JSON.stringify(totalsAndEffects()), totals, '分类只改变显示，不改变属性计算或装备');
state.equipment.main_hand.gems.push({...gem, variant: {...gem.variant, id: 4, stats: {haste: 12}}});
assert.equal(effectSummaryGroups()[0].rows.length, 2, '同名不同品质/属性不合并');
state.equipment = {};
assert.equal(effectSummaryGroups().length, 4);assert.ok(effectSummaryMarkup(effectSummaryGroups()).includes('未镶嵌宝石'));
console.log('四类汇总：重复合并、纯属性强化、自带/附加美化分离、数值不变、转义与空状态通过');

});

test("preview: shared sources, enhancements, escaping, empty slots", () => {

const nodes = new Map();
const globals = {document: {
  querySelector: () => ({dataset: {}}),
  getElementById: id => {
    if (!nodes.has(id)) nodes.set(id, {});
    return nodes.get(id);
  },
}};
const context = gearRenderer(`globalThis.qa = {state, previewSlotMarkup, renderSlots,
  configure: value => {bootstrap = value;}};`, globals);
const {state, previewSlotMarkup, renderSlots, configure} = context.qa;
configure({slots: [{key: 'head', label: '头部'}], raid_boss_numbers: {9: {17: 3}}});
state.selectedSlot = 'head';
const entry = {
  item: {item_id: 1, name: '一件名字较长的装备'},
  variant: {id: 1, type: 'drop_equipment', item_level: 334, sources: []},
  gems: [{item: {name: '迅捷宝石'}}, {item: {name: '迅捷宝石'}}],
  enchant: {item: {name: '中文附魔'}},
  embellishment: {item: {name: '中文美化'}},
};
state.equipment = {head: entry};
for (const [sources, expected] of [
  [[{type: 'raid', instance_id: 9, encounter_id: 17, instance_zh: '测试团本', encounter_zh: '真实首领'}], '测试团本3号'],
  [[{type: 'mythic_plus', instance_zh: '测试地下城'}], '测试地下城'],
  [[{type: 'delve', type_zh: '地下堡', instance_zh: '地下堡'}], '地下堡'],
  [[{type: 'crafted', profession_zh: '锻造'}], '专业制造'],
  [[], '来源待补全'],
  [['<script>危险文本</script>'], '&lt;script&gt;危险文本&lt;/script&gt;'],
]) {
  entry.variant.sources = sources;
  renderSlots();
  const editor = nodes.get('gear-slot-list').innerHTML;
  const preview = previewSlotMarkup('head');
  assert.ok(editor.includes(expected), '编辑栏来源格式');
  assert.ok(preview.includes(`class="gear-preview-source-value">${expected}</span>`), '预览必须复用编辑栏来源规则');
  for (const value of ['美化：中文美化', '宝石：迅捷宝石×2', '附魔：中文附魔']) {
    assert.ok(preview.includes(`<small>${value}</small>`), '强化信息逐条独立展示');
  }
  assert.ok(!preview.includes('<script>'), '来源需要HTML转义');
  assert.ok(preview.includes('data-preview-slot="head"'), '保留点击回编辑的入口');
}
state.equipment = {};
assert.ok(!previewSlotMarkup('head').includes('gear-preview-slot-source'), '空槽位不伪造来源');
console.log('游戏预览通过：来源与编辑一致、强化分行、转义与空槽位。');

});
