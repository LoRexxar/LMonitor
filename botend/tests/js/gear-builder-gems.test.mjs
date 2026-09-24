import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// 调用页面真实格式化函数，验证整段提示清理时不误删有效特效。
const source = fs.readFileSync(new URL('../../../static/portal/js/gear_builder.js', import.meta.url), 'utf8');
const context = {document: {querySelector: () => ({dataset: {}}), getElementById: () => ({})}};
vm.runInNewContext(source.replace('  initialize();', 'globalThis.qa = {gemDescription, tooltipText, optionRow};'), context);
const {gemDescription, tooltipText, optionRow} = context.qa;
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
assert.equal(tooltipText(embellishment, {...embellishment.variants[0], effects: []}), '特效数据待补全', '缺失特效不能回退到制作说明');
console.log('宝石与美化说明验证通过：中文特效、完整标点、制作说明隔离、缺失效果与悬停去重。');
