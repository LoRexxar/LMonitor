"""跨职业、变量时序、未知字段和全部默认 APL 的条件展示回归。"""

import gzip
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.template.loader import render_to_string
from botend.services.simc_apl.condition_audit import audit_source
from botend.services.simc_apl.readable import readable_apl
from botend.services.simc_apl.routing import route_text


class UniversalConditionTests(SimpleTestCase):
    def rows(self, source, symbols=None, **kwargs):
        return [r for g in readable_apl(source, symbols or {}, **kwargs)['groups'] for r in g['rows']]

    def test_all_34_default_apls_all_expression_options_are_covered(self):
        path = Path(__file__).parent / 'fixtures/simc_reference_default_conditions.json.gz'
        corpus = json.loads(gzip.decompress(path.read_bytes()))['sources']
        self.assertEqual(len(corpus), 34)
        total = 0
        for name, source in corpus.items():
            with self.subTest(apl=name):
                report = audit_source(source, name)
                self.assertEqual(report['parse_issues'], 0)
                self.assertEqual(report['unknown_fields'], [])
                self.assertGreater(report['expressions'], 0)
                total += report['expressions']
        self.assertEqual(total, 2450)

    def test_implicit_fields_belong_to_each_current_action(self):
        rows = self.rows('actions=moonfire,if=refreshable&remains<gcd\n'
                         'actions+=/sunfire,if=remains<3&full_recharge_time<gcd', {
                             ('action', 'moonfire'): {'name': '月火术'}, ('action', 'sunfire'): {'name': '阳炎术'}})
        self.assertIn('月火术已进入刷新窗口', rows[0]['guide_text'])
        self.assertIn('月火术持续效果剩余时间', rows[0]['guide_text'])
        self.assertIn('阳炎术持续效果剩余时间低于3秒', rows[1]['guide_text'])
        self.assertIn('阳炎术全部充能恢复时间', rows[1]['guide_text'])
        self.assertNotIn('月火术', rows[1]['guide_text'])

    def test_resource_types_are_not_boolean_flags(self):
        cases = {'astral_power.deficit<=20': '星界能量距上限还缺少不超过20点',
                 'maelstrom>=60': '漩涡值达到60点', 'health.pct<40': '生命值比例低于40%',
                 'evoker.allied_cds_up>=2': '数量（无黑檀目标时检查先知先觉目标）达到2个',
                 'action.fireball.execute_remains>0.1': '当前施放剩余时间高于0.1秒'}
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertIn(expected, self.rows(f'actions=hit,if={expression}')[0]['guide_text'])

    def test_trinkets_keep_slot_buff_and_cooldown_semantics(self):
        row = self.rows('actions=use_item,slot=trinket1,if=trinket.1.has_use_buff&'
                        'trinket.2.cooldown.remains>20&!other_trinket.has_use_damage')[0]
        self.assertIn('饰品1具有主动使用的输出属性增益', row['guide_text'])
        self.assertIn('饰品2冷却剩余时间高于20秒', row['guide_text'])
        self.assertIn('另一件饰品不具有主动使用的伤害效果', row['guide_text'])
        self.assertFalse(row['has_unexplained_conditions'])

    def test_cooldown_up_and_numeric_remains_are_different_types(self):
        rows = self.rows('actions=hit,if=cooldown.hit.up=1\n'
                         'actions+=/hit,if=cooldown.hit.remains=1', {('action', 'hit'): {'name': '打击'}})
        self.assertIn('打击已冷却完毕', rows[0]['guide_text'])
        self.assertIn('打击冷却剩余时间为1秒', rows[1]['guide_text'])

    def test_unknown_leaves_do_not_hide_supported_neighbors_or_comparisons(self):
        expression = '!(custom.damage+fury*2<100|active_enemies=1)'
        row = self.rows(f'actions=hit,if={expression}')[0]
        self.assertEqual(row['condition_text'], '（custom.damage加上（恶魔之怒的2倍））达到100，并且敌人数量不为1个')
        self.assertEqual(row['condition_diagnostics'], [{'reason': 'unknown_semantics', 'token': 'custom.damage'}])
        self.assertNotIn('额外判定', row['guide_text'])
        html = render_to_string('portal/_simc_reference_condition.html', {'segments': row['guide']})
        self.assertIn('custom.damage', html)
        self.assertIn('data-condition-unresolved="unknown_semantics"', html)

    def test_mixed_xor_group_is_preserved(self):
        row = self.rows('actions=hit,if=(fury>1^energy>2)&active_enemies=1')[0]
        self.assertIn('（以下两个条件恰好一个成立：', row['guide_text'])
        self.assertIn('），并且有且仅有1个敌人', row['guide_text'])

    def test_target_selector_keeps_score_and_condition_distinct(self):
        row = self.rows('actions=hit,target_if=min:dot.bleed.remains+100*(self.target=target),if=energy>40')[0]
        self.assertIn('最小的目标', route_text(row['target_guide']))
        self.assertIn('时取1，否则取0', route_text(row['target_guide']))
        self.assertIn('角色当前主目标的编号', route_text(row['target_guide']))
        self.assertEqual(row['condition_text'], '能量高于40点')

    def test_unmapped_identity_keeps_property_without_fabricating_tooltip(self):
        row = self.rows('actions=hit,if=action.unmapped.cast_time<2')[0]
        self.assertIn('action.unmapped施法时间低于2秒', row['guide_text'])
        self.assertFalse(row['has_unexplained_conditions'])
        self.assertEqual(row['condition_diagnostics'][0]['reason'], 'missing_identity')
        self.assertFalse(any('reference' in s for s in row['guide']))

    def test_variable_definitions_preserve_conditional_updates_and_all_assignments(self):
        source = ('actions=variable,name=pool,value=40\n'
                  'actions+=/variable,name=pool,op=add,value=10,if=active_enemies>1\n'
                  'actions+=/hit,if=fury>=variable.pool')
        rows = self.rows(source)
        row = rows[-1]
        self.assertEqual(row['condition_text'], '恶魔之怒达到变量「pool」的当前值')
        definitions = row['variables'][0]['definitions']
        self.assertEqual([d['operation'] for d in definitions], ['赋值', '加上'])
        self.assertIn('多于1个敌人', route_text(definitions[1]['fields'][-1]['segments']))
        self.assertEqual([d['rule_anchor'] for d in definitions], ['apl-list-0-rule-1', 'apl-list-0-rule-2'])
        html = render_to_string('portal/_simc_reference_variables.html', {'row': row})
        self.assertIn('最近一次计算后保存的值', html)
        self.assertIn('不是用此刻的状态重新计算', html)

    def test_variable_setif_and_reset_keep_branch_and_default(self):
        rows = self.rows('actions=variable,name=mode,op=setif,condition=active_enemies>1,value=2,value_else=1\n'
                         'actions+=/variable,name=mode,op=reset,if=fury<20\n'
                         'actions+=/hit,if=variable.mode=2')
        defs = rows[-1]['variables'][0]['definitions']
        self.assertEqual([d['operation'] for d in defs], ['按条件选择值', '重置为默认值'])
        self.assertEqual([f['name'] for f in defs[0]['fields']], ['condition', 'value', 'value_else'])

    def test_variable_cycles_are_links_not_recursive_expansion(self):
        source = ('actions=variable,name=a,value=variable.b+1\n'
                  'actions+=/variable,name=b,value=variable.a+1\n'
                  'actions+=/hit,if=variable.a')
        result = readable_apl(source, {})
        json.dumps(result)
        row = result['groups'][0]['rows'][-1]
        self.assertEqual(len(row['variables']), 1)
        self.assertIn('变量「b」', route_text(row['variables'][0]['definitions'][0]['fields'][0]['segments']))

    def test_unknown_variable_dependency_reaches_consumer_without_recursive_expansion(self):
        rows = self.rows('actions=variable,name=a,value=variable.b+1\n'
                         'actions+=/variable,name=b,value=variable.a+custom.unknown\n'
                         'actions+=/hit,if=variable.a>10')
        self.assertTrue(rows[-1]['has_unexplained_conditions'])
        self.assertEqual(rows[-1]['variable_diagnostics'], [
            {'reason': 'unknown_semantics', 'token': 'custom.unknown', 'variable': 'b'}])

    def test_audit_reports_real_missing_names_separately_from_semantics(self):
        report = audit_source('actions=hit,if=cooldown.hit.up', '测试', {('action', 'hit'): {'name': 'hit', 'token': 'hit'}})
        self.assertEqual(report['unknown_fields'], [])
        self.assertEqual(report['identity_fallbacks'][0]['reason'], 'unlocalized_identity')

    def test_list_reset_removes_obsolete_variable_definitions(self):
        rows = self.rows('actions=variable,name=old,value=1\nactions=hit,if=variable.old')
        self.assertEqual(rows[0]['variables'], [])
        self.assertTrue(rows[0]['has_unexplained_conditions'])
        self.assertEqual(rows[0]['condition_diagnostics'][0]['reason'], 'undefined_variable')

    def test_hero_filter_removes_other_tree_variable_definitions(self):
        rows = self.rows('actions=variable,name=other,value=1,if=hero_tree.voidscarred\n'
                         'actions+=/hit,if=variable.other', hero_id=124)
        self.assertEqual(rows[-1]['variables'], [])
        self.assertTrue(rows[-1]['has_unexplained_conditions'])

    def test_boolean_arithmetic_uses_zero_one_not_a_standalone_gate(self):
        row = self.rows('actions=hit,if=energy>=40+20*(trinket.1.has_use_buff&active_enemies>1)')[0]
        self.assertIn('能量达到60点', row['guide_text'])
        self.assertIn('能量达到40点', row['guide_text'])
        self.assertNotIn('时取1，否则取0', row['guide_text'])
        self.assertFalse(row['has_unexplained_conditions'])

    def test_comparison_of_boolean_result_is_simplified_by_truth_table(self):
        cases = {'!trinket.1.cast_time>0': '饰品1使用施法时间为0',
                 '(energy>40)=0': '能量不超过40点',
                 '0<(energy>40)': '能量高于40点',
                 '(energy>40)>=2': '条件始终不成立',
                 '(energy>40)<2': '条件始终成立',
                 '!((custom.unknown>2)=0)': 'custom.unknown高于2'}
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                row = self.rows('actions=hit,if=' + expression)[0]
                self.assertEqual(row['condition_text'], expected)

    def test_boolean_simplification_keeps_parent_logical_grouping(self):
        row = self.rows('actions=hit,if=((energy>40|fury>60)=1)&active_enemies=1')[0]
        self.assertEqual(row['condition_text'], '（能量高于40点，或者恶魔之怒高于60点），并且有且仅有1个敌人')

    def test_foreign_specializations_have_meaningful_conditions(self):
        samples = {'combo_strike': '与上次计入连击的技能不同',
                   'howl_summon.ready': '至少有一个就绪',
                   'scorch_execute.active': '目标处于其斩杀血量范围',
                   'stealthed.rogue': '潜行状态正在生效',
                   'demonic_art': '至少有一个生效',
                   'eclipse.lunar': '被切换为月蚀模式',
                   'action.sigil_of_flame.placed': '符咒已放置且尚未激活'}
        for expression, expected in samples.items():
            with self.subTest(expression=expression):
                self.assertIn(expected, self.rows('actions=hit,if=' + expression)[0]['guide_text'])


class PublishedConditionAuditTests(TestCase):
    def test_audit_is_read_only_and_uses_all_published_apls_only(self):
        from botend.models import SimcApl, SimcReferencePublication
        public = SimcReferencePublication.objects.create(spec_key='warrior_fury', is_published=True)
        first = SimcApl.objects.create(name='公开甲', spec='warrior_fury', content='actions=hit,if=fury>10')
        second = SimcApl.objects.create(name='公开乙', spec='warrior_fury', content='actions=hit,if=cooldown.hit.up')
        SimcApl.objects.create(name='私人配置', spec='warrior_fury', owner_user_id=100, content='actions=private')
        public.apls.add(first, second)
        with patch('botend.management.commands.audit_simc_reference_conditions.display_symbols',
                   return_value={('action', 'hit'): {'name': '打击'}}) as symbols, \
             patch('botend.management.commands.audit_simc_reference_conditions.Path.write_text') as write, \
             CaptureQueriesContext(connection) as queries:
            call_command('audit_simc_reference_conditions', output='未写入的测试报告.json', stdout=StringIO())
        report = json.loads(write.call_args.args[0])
        self.assertEqual(len(report['files']), 2)
        self.assertTrue(all('私人配置' not in r['name'] for r in report['files']))
        symbols.assert_called_once()
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))

    def test_empty_publication_is_not_reported_as_success(self):
        with self.assertRaisesMessage(CommandError, '没有待审计的公开 APL'):
            call_command('audit_simc_reference_conditions', output='未写入的测试报告.json', stdout=StringIO())
