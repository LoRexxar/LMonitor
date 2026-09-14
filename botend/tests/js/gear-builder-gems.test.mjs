import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// 调用页面真实格式化函数，验证整段提示清理时不误删有效特效。
const source = fs.readFileSync(new URL('../../../static/portal/js/gear_builder.js', import.meta.url), 'utf8');
const context = {document: {querySelector: () => ({dataset: {}}), getElementById: () => ({})}};
vm.runInNewContext(source.replace('  initialize();', 'globalThis.qa = {gemDescription, tooltipText};'), context);
const {gemDescription, tooltipText} = context.qa;
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
console.log('宝石说明验证通过：整段提示、分类标题、属性别名、有效特效与悬停去重。');
