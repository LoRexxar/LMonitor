"""玩家展示的 APL 语义、分支资料、天赋拓扑及提示回归。"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from botend.constants.hero_talents import spec_hero_subtree_ids
from botend.models import (
    SimcApl, SimcAplSymbol, SimcAplSymbolScope, SimcProfile, SimcTalentString,
    WowItemSnapshot, WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion,
)
from botend.services.simc_apl.readable import display_symbols, readable_apl
from botend.services.simc_reference_display import reference_trees
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.build_code import TalentBuildCodeEncoder


def presentation_fixture():
    """隔离演示夹具，不读取或修改业务库；样例不代表真实天赋构筑。"""
    user = User.objects.create_user(username='玩家界面演示')
    version = WowTalentVersion.objects.create(key='演示天赋版本', branch='retail', is_active=True,
                                             is_default_simulator=True, is_default_player_tree=True)
    spell_rows = [
        (23881, 'bloodthirst', '嗜血', '猛击目标，造成物理伤害并恢复生命值。', 'spell_nature_bloodlust'),
        (184367, 'rampage', '暴怒', '消耗怒气，连续发动攻击并进入激怒状态。', 'ability_warrior_rampage'),
        (1719, 'recklessness', '鲁莽', '提高怒气获取和暴击几率。', 'ability_criticalstrike'),
        (5308, 'execute', '斩杀', '尝试终结受伤的目标，消耗怒气造成物理伤害。', 'inv_sword_48'),
        (184362, 'enrage', '激怒', '攻击速度提高，造成的伤害提高。', 'ability_warrior_enrage'),
    ]
    for sid, token, name, description, icon in spell_rows:
        WowSpellSnapshot.objects.create(spell_id=sid, branch='wow', locale='zhCN',
                                       name_zh=name, description=description, icon=icon)
        kind = 'buff' if token == 'enrage' else 'action'
        symbol = SimcAplSymbol.objects.create(token=token, symbol_kind=kind)
        SimcAplSymbolScope.objects.create(symbol=symbol, class_name='warrior', spec='fury', spell_id=sid, name_zh=name)
    hero_id = min(spec_hero_subtree_ids('Warrior', 'Fury'))
    for tree_index, tree_type in enumerate(('class', 'spec', 'hero')):
        for n in range(9):
            sid, _, name, description, icon = spell_rows[n % len(spell_rows)]
            # 保持每棵树的技能身份唯一，避免真实模型的别名合并掩盖未选节点。
            if n >= 5:
                sid = 910000 + tree_index * 100 + n
                name = f'演示天赋 {n + 1}'
            WowTalentNodeMetadata.objects.create(
                talent_version=version, class_name='Warrior', spec_name='Fury', tree_type=tree_type,
                node_id=700000 + tree_index * 100 + n,
                talent_id=800000 + tree_index * 100 + n,
                spell_id=sid, name=name, name_zh=name, description_zh=description, icon=icon,
                row=n // 3 + 1, column=n % 3 + 1, max_points=1,
                parents_json=[800000 + tree_index * 100 + n - 3] if n >= 3 else [],
                db2_subtree_id=hero_id if tree_type == 'hero' else 0, source='db2',
            )
    provider = TalentMetadataProvider(talent_version=version)
    nodes = provider.get_decoder_node_list('Warrior')
    code = TalentBuildCodeEncoder.encode_node_states(
        'CgEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgGDjxMsMzMzMDjZmZGzMzsMzMGzMbDzMAAQMWWGYBMBzwEYG2AmZ2Y2GAAMzYYMzMMYA',
        nodes, [{**node, 'points': 1, 'selected': True} for i, node in enumerate(nodes) if i % 3 != 2],
    )
    talent_symbol = SimcAplSymbol.objects.create(token='recklessness', symbol_kind='talent')
    SimcAplSymbolScope.objects.create(symbol=talent_symbol, class_name='warrior', spec='fury', trait_id=800102, name_zh='鲁莽天赋')
    apl = SimcApl.objects.create(name='演示 · 技能决策', spec='warrior_fury', class_name='warrior',
        is_system=True, is_selectable=True, source='simc_upstream', content='\n'.join([
            'actions.precombat=snapshot_stats',
            'actions=auto_attack',
            'actions+=/call_action_list,name=burst,if=talent.recklessness.enabled&active_enemies>=2',
            'actions+=/rampage,if=buff.enrage.down|rage>=100',
            'actions+=/execute,if=target.health.pct<20',
            'actions+=/bloodthirst',
            'actions.burst=recklessness,if=cooldown.recklessness.ready',
            'actions.burst+=/rampage,if=rage>=80',
        ]))
    talent = SimcTalentString.objects.create(name='演示 · 天赋构筑', spec='warrior_fury',
        is_system=True, talent=code, default_apl=apl, system_key='simc_upstream:warrior_fury')
    equipment = ['warrior="界面演示"', 'spec=fury']
    slots = [('head', '勇士的战盔'), ('neck', '远征者的项链'), ('shoulder', '勇士的肩甲'),
             ('back', '远征披风'), ('chest', '勇士的胸甲'), ('wrist', '精工护腕'),
             ('hands', '勇士的手甲'), ('waist', '精工腰带'), ('legs', '勇士的腿甲'),
             ('feet', '远征战靴'), ('finger1', '决意指环'), ('finger2', '胜利指环'),
             ('trinket1', '勇气徽记'), ('trinket2', '冲锋号角'), ('main_hand', '远征者的巨剑'), ('off_hand', '不屈战刃')]
    for n, (slot, name) in enumerate(slots):
        iid = 990000 + n
        WowItemSnapshot.objects.create(item_id=iid, name_zh=name, quality=4, icon='inv_sword_48',
                                       description_zh='物品等级 285\n+150 力量\n+95 暴击\n装备：用于界面验证的演示属性。')
        equipment.append(f'{slot}=,id={iid},ilevel=285')
    profile = SimcProfile.objects.create(name='演示 · 完整装备', spec='warrior_fury', class_name='warrior',
        source='simc_upstream', player_config_mode='manual_equipment', player_equipment='\n'.join(equipment))
    from botend.tests.simc_reference_test_fixtures import publish_resources
    publish_resources('warrior_fury', profiles=[profile], talents=[talent], apls=[apl])
    return user, version, profile, talent, apl


class ReadableAplTests(SimpleTestCase):
    def test_cooldown_one_second_comparison_is_not_a_boolean(self):
        refs = {('action', 'hit'): {'name': '测试技能'}}
        rows = readable_apl('actions=hit,if=cooldown.hit.remains=1\nactions+=/hit,if=cooldown.hit.remains!=1\nactions+=/hit,if=cooldown.hit.remains=0', refs)['groups'][0]['rows']
        self.assertEqual(rows[0]['guide_text'], '当测试技能冷却剩余时间为1秒时使用。')
        self.assertEqual(rows[1]['guide_text'], '当测试技能冷却剩余时间不为1秒时使用。')
        self.assertEqual(rows[2]['guide_text'], '当测试技能已冷却完毕时使用。')

    def test_negated_composite_keeps_effective_or_grouping(self):
        rows = readable_apl('actions=hit,if=!(rage>10&energy>20)&active_enemies=1\nactions+=/hit,if=!(rage>10&energy>20)&variable.unknown', {})['groups'][0]['rows']
        self.assertIn('（怒气不超过10点，或者能量不超过20点），并且', rows[0]['guide_text'])
        self.assertIn('（怒气不超过10点，或者能量不超过20点），并且额外判定', rows[1]['guide_text'])

    def test_upstream_hero_spec_and_bare_talent_routing(self):
        refs = {('talent', 'slayers_dominance'): {'name': '屠戮者的统御'},
                ('action', 'thunder_clap'): {'name': '雷霆一击'}}
        result = readable_apl('actions=run_action_list,name=slayer,if=talent.slayers_dominance&active_enemies=1\nactions+=/run_action_list,name=thane_aoe,if=hero_tree.mountain_thane&spell_targets.thunder_clap>=3&spec.protection\nactions.slayer=hit\nactions.thane_aoe=hit', refs)
        self.assertEqual(result['groups'][1]['entries'][0]['text'], '已学习屠戮者的统御，并且有且仅有1个敌人')
        entry = result['groups'][2]['entries'][0]['text']
        self.assertIn('选择了山丘领主英雄天赋', entry)
        self.assertIn('雷霆一击可命中的目标数达到3个', entry)
        self.assertIn('选择了防护专精', entry)

    def test_player_guide_describes_usage_instead_of_expression_tokens(self):
        refs = {('buff', 'enrage'): {'name': '激怒'}}
        row = readable_apl('actions=rampage,if=buff.enrage.down|rage>=100', refs)['groups'][0]['rows'][0]
        self.assertEqual(row['guide_text'], '当没有激怒效果，或者怒气达到100点时使用。')
        self.assertNotIn('≥', row['guide_text'])

    def test_mixed_boolean_groups_and_negation_keep_their_meaning(self):
        refs = {('talent', 'x'): {'name': '某天赋'}}
        rows = readable_apl('actions=hit,if=!(talent.x.enabled|rage<50)&target.health.pct<20', refs)['groups'][0]['rows']
        self.assertIn('未学习某天赋，并且怒气达到50点', rows[0]['guide_text'])
        self.assertIn('目标生命值低于20%', rows[0]['guide_text'])
        grouped = readable_apl('actions=hit,if=(rage>50|rage<10)&target.health.pct<20', {})['groups'][0]['rows'][0]
        self.assertIn('（怒气高于50点，或者怒气低于10点），并且', grouped['guide_text'])

    def test_unknown_conditions_are_not_summarized_as_unconditional(self):
        row = readable_apl('actions=hit,if=variable.burst&rage>50,line_cd=3', {})['groups'][0]['rows'][0]
        self.assertIn('额外判定', row['guide_text'])
        self.assertIn('执行设置', row['guide_text'])
        self.assertIn('额外判定成立（详见原始规则），并且怒气高于50点', row['guide_text'])

    def test_scenarios_use_final_call_edges_and_preserve_parent_conditions(self):
        refs = {('talent', 'slayer'): {'name': '屠戮者'}}
        result = readable_apl('\n'.join([
            'actions=call_action_list,name=unused',
            'actions=call_action_list,name=slayer,if=talent.slayer.enabled',
            'actions.slayer=call_action_list,name=slayer_st,if=active_enemies=1',
            'actions.slayer+=/run_action_list,name=slayer_aoe,if=active_enemies>=2',
            'actions.slayer_st=hit', 'actions.slayer_aoe=cleave', 'actions.unused=old',
        ]), refs)
        groups = {g['name']: g for g in result['groups']}
        self.assertEqual(groups['slayer_st']['label'], '屠戮者 · 单体')
        self.assertEqual(groups['slayer_aoe']['label'], '屠戮者 · 多目标')
        self.assertEqual(groups['slayer']['entries'][0]['text'], '已学习屠戮者')
        self.assertEqual(groups['slayer_aoe']['entries'][0]['text'], '至少有2个敌人')
        self.assertEqual(groups['slayer_aoe']['entries'][0]['anchor'], groups['slayer']['anchor'])
        self.assertEqual(groups['unused']['entries'], [])

    def test_multiple_entry_paths_and_cycles_remain_separate(self):
        result = readable_apl('actions=call_action_list,name=shared,if=rage>20\nactions.other=run_action_list,name=shared,if=rage<10\nactions.shared=call_action_list,name=other', {})
        shared = next(g for g in result['groups'] if g['name'] == 'shared')
        self.assertEqual(len(shared['entries']), 2)
        self.assertEqual([e['text'] for e in shared['entries']], ['怒气高于20点', '怒气低于10点'])

    def test_target_selection_is_not_rewritten_as_an_if_filter(self):
        row = readable_apl('actions=hit,target_if=min:target.health.pct,if=rage>30', {})['groups'][0]['rows'][0]
        self.assertEqual(''.join(s.get('text', '') for s in row['target_guide']),
                         '先选择目标生命值最低的目标；再检查本项的使用条件。')

    def test_priority_resets_and_appends_match_execution_order(self):
        result = readable_apl('actions=old\nactions+=/old2\nactions=new\nactions+=/last', {})
        self.assertEqual([r['name'] for r in result['groups'][0]['rows']], ['new', 'last'])
        self.assertEqual([r['priority'] for r in result['groups'][0]['rows']], [1, 2])

    def test_expression_preserves_grouping_and_typed_references(self):
        refs = {('talent', 'x'): {'name': '测试天赋'}, ('buff', 'x'): {'name': '测试增益'}}
        result = readable_apl('actions=hit,if=(talent.x.enabled|buff.x.down)&rage>=80', refs)
        segments = result['groups'][0]['rows'][0]['fields'][0]['segments']
        self.assertEqual([p['reference']['name'] for p in segments if 'reference' in p], ['测试天赋', '测试增益'])
        text = ''.join(p.get('text', '技能') for p in segments)
        self.assertIn('或', text)
        self.assertIn('且', text)
        self.assertIn('怒气 ≥ 80', text)
        self.assertEqual(text.count('('), text.count(')'))

    def test_named_default_is_not_the_main_list_and_targets_remain_explicit(self):
        result = readable_apl('actions=call_action_list,name=default\nactions.default=hit\nactions.foo=run_action_list,name=missing', {})
        groups = result['groups']
        self.assertIsNone(groups[0]['name'])
        self.assertEqual(groups[1]['name'], 'default')
        self.assertEqual(groups[0]['rows'][0]['target_anchor'], groups[1]['anchor'])
        self.assertEqual(groups[2]['rows'][0]['target_anchor'], '')
        self.assertIn('停止', groups[2]['rows'][0]['behavior'])

    def test_invalid_condition_is_not_presented_as_unconditional(self):
        result = readable_apl('actions=hit,if=foo&&bar', {})
        self.assertTrue(result['groups'][0]['rows'][0]['unsupported'])
        self.assertIn('if=foo&&bar', result['groups'][0]['rows'][0]['raw'])

    def test_target_selector_and_untranslated_tokens_are_preserved(self):
        result = readable_apl('actions=hit,target_if=min:target.health.pct,if=unknown.token>2', {})
        fields = result['groups'][0]['rows'][0]['fields']
        self.assertIn('取最小值', fields[0]['segments'][0]['text'])
        self.assertIn('unknown.token', ''.join(s.get('text', '') for s in fields[1]['segments']))


class ReferencePresentationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.version, cls.profile, cls.talent, cls.apl = presentation_fixture()

    def test_full_tree_uses_the_player_model_with_selected_nodes_and_paths(self):
        result = reference_trees(self.talent.talent,
            {'class_name': 'Warrior', 'spec_name': 'Fury', 'key': 'warrior_fury'}, self.version)
        self.assertEqual({t['tree_type'] for t in result['trees']}, {'class', 'spec', 'hero'})
        self.assertTrue(any(t['paths'] for t in result['trees']))
        self.assertTrue(any(n['selected'] for t in result['trees'] for n in t['nodes']))
        self.assertTrue(any(not n['selected'] for t in result['trees'] for n in t['nodes']))
        self.assertEqual(result['render_model']['trees'], result['trees'])
        self.assertGreater(result['render_model']['layout']['width'], 0)

    def test_player_and_reference_share_the_same_tree_markup(self):
        from django.template.loader import render_to_string
        display = reference_trees(self.talent.talent,
            {'class_name': 'Warrior', 'spec_name': 'Fury', 'key': 'warrior_fury'}, self.version)
        shared = render_to_string('portal/spec_detail/_talent_tree.html', {'render_model': display['render_model']})
        player = render_to_string('portal/spec_detail/player_detail.html', {
            'player_detail': {'talent_render_model': display['render_model'],
                              'talent_build_code': self.talent.talent, 'talent_code': self.talent.talent},
        })
        reference = render_to_string('portal/_simc_reference_trees.html', {'talent_display': display})
        self.assertIn(shared.strip(), player)
        self.assertIn(shared.strip(), reference)
        self.assertIn('player-talent-tree.js', player)
        self.assertIn('player-talent-tree.css', player)

    def test_hero_filter_also_filters_the_shared_render_model(self):
        from botend.services.simc_reference_heroes import filter_tree_panels
        allowed = {'tree_type': 'hero', 'nodes': [{'db2_subtree_id': 60}]}
        excluded = {'tree_type': 'hero', 'nodes': [{'db2_subtree_id': 61}]}
        display = {'trees': [allowed, excluded], 'render_model': {'trees': [allowed, excluded]}}
        result = filter_tree_panels(display, {60})
        self.assertEqual(result['render_model']['trees'], [allowed])
        self.assertEqual(result['render_model']['nodes'], allowed['nodes'])

    def test_talent_mapping_uses_trait_node_id_and_current_branch(self):
        rows = display_symbols('warrior', 'fury', self.version)
        self.assertEqual(rows[('talent', 'recklessness')]['spell_id'], 1719)
        self.assertIn('暴击', rows[('talent', 'recklessness')]['description'])
        self.assertTrue(rows[('action', 'bloodthirst')]['icon'])
        ptr = WowTalentVersion.objects.create(key='演示PTR', branch='ptr', is_active=True)
        rows = display_symbols('warrior', 'fury', ptr)
        self.assertIsNone(rows[('talent', 'recklessness')]['spell_id'])
        self.assertEqual(rows[('action', 'bloodthirst')]['description'], '当前资料尚未提供说明。')

    def test_private_page_renders_tree_equipment_and_readable_apl_offline(self):
        self.client.force_login(self.user)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('只读展示禁止联网')):
            response = self.client.get('/portal/simc-reference/?spec=warrior_fury')
        self.assertEqual(response.status_code, 200)
        for text in ('talent-node-card--tree', 'data-wow-item-tooltip', '勇士的战盔',
                     '嗜血', '暴怒', '使用条件', '子列表没有执行技能时', '原始规则'):
            self.assertContains(response, text)
        self.assertTemplateUsed(response, 'portal/spec_detail/_talent_tree.html')
        self.assertContains(response, 'player-talent-tree.js')
        self.assertNotContains(response, 'reference-talent-node')
        self.assertContains(response, 'data-apl-tabs')
        self.assertContains(response, 'reference-rule-line')
        self.assertContains(response, '什么时候进入本组')

    def test_compound_spec_identity_and_aura_description(self):
        WowTalentNodeMetadata.objects.create(
            talent_version=self.version, class_name='Hunter', spec_name='BeastMastery',
            tree_type='spec', node_id=799001, talent_id=899001, spell_id=1719,
        )
        symbol = SimcAplSymbol.objects.create(token='demo_talent', symbol_kind='talent')
        SimcAplSymbolScope.objects.create(symbol=symbol, class_name='hunter',
                                         spec='beast_mastery', trait_id=899001)
        self.assertEqual(display_symbols('hunter', 'beast_mastery', self.version)[
            ('talent', 'demo_talent')]['spell_id'], 1719)
        WowSpellSnapshot.objects.filter(spell_id=184362).update(aura_description='增益状态专属说明')
        self.assertEqual(display_symbols('warrior', 'fury', self.version)[
            ('buff', 'enrage')]['description'], '增益状态专属说明')

    def test_wrong_spec_talent_does_not_render_a_different_tree(self):
        result = reference_trees(self.talent.talent,
            {'class_name': 'Mage', 'spec_name': 'Frost', 'key': 'mage_frost'}, self.version)
        self.assertEqual(result['trees'], [])
        self.assertIn('无法匹配', result['message'])
