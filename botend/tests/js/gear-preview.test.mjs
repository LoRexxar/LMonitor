import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../../static/portal/js/gear_builder.js', import.meta.url), 'utf8');
const nodes = new Map();
const context = {document: {
  querySelector: () => ({dataset: {}}),
  getElementById: id => {
    if (!nodes.has(id)) nodes.set(id, {});
    return nodes.get(id);
  },
}};
vm.runInNewContext(source.replace('  initialize();', `globalThis.qa = {state, previewSlotMarkup, renderSlots,
  configure: value => {bootstrap = value;}};`), context);
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
