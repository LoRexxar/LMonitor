"""玩家条件说明必须保留运算、布尔分组、数值含义和技能提示身份。"""

from django.test import SimpleTestCase
from django.template.loader import render_to_string

from botend.services.simc_apl.readable import readable_apl
from botend.services.simc_apl.routing import route_text


class ReferenceConditionTests(SimpleTestCase):
    symbols = {
        ('action', 'reap'): {'name': '收割', 'spell_id': 1, 'icon': '/reap.jpg', 'description': '收割说明'},
        ('action', 'void_ray'): {'name': '虚空射线'},
        ('action', 'soul_immolation'): {'name': '灵魂献祭'},
        ('buff', 'eradicate'): {'name': '根除'},
        ('buff', 'metamorphosis'): {'name': '虚空变形'},
        ('buff', 'collapsing_star_stacking'): {'name': '坍缩之星'},
        ('buff', 'void_metamorphosis_stack'): {'name': '虚空变形层数'},
        ('talent', 'devourers_bite'): {'name': '吞噬者之咬'},
    }

    def row(self, expression, action='call_action_list,name=child'):
        return readable_apl(f'actions={action},if={expression}\nactions.child=reap',
                            self.symbols)['groups'][0]['rows'][0]

    def test_reported_live_rule_is_complete_and_retains_mixed_groups(self):
        expression = ('(active_enemies=1|buff.eradicate.up)&action.reap.souls_consumed>=4'
                      '&!(talent.devourers_bite&active_enemies=1&fury<void_metamorphosis_base_drain_ps*2)')
        row = self.row(expression)
        expected = ('按情况判断：①有且仅有1个敌人时，收割此次可消耗的灵魂残片数达到4个，'
                    '并且（未学习吞噬者之咬，或者恶魔之怒达到（虚空变形当前每秒基础怒气消耗的2倍））；'
                    '②敌人数量不为1个时，具有根除效果，并且收割此次可消耗的灵魂残片数达到4个')
        self.assertEqual(row['entry_text'], expected)
        self.assertEqual(row['condition_text'], expected)
        self.assertFalse(any('raw' in part for part in row['entry_condition']))
        html = render_to_string('portal/_simc_reference_condition.html', {'segments': row['entry_condition']})
        self.assertIn('data-spell-id="1"', html)
        self.assertIn('收割说明', html)
        self.assertNotIn('souls_consumed', html)
        self.assertEqual(row['raw'], f'call_action_list,name=child,if={expression}')

    def test_unknown_neighbor_cannot_remove_known_or_grouping(self):
        row = self.row('(active_enemies=1|buff.eradicate.up)&variable.unresolved>4')
        self.assertEqual(row['entry_text'],
            '（有且仅有1个敌人，或者具有根除效果），并且variable.unresolved高于4')
        negated = self.row('!((active_enemies=1|buff.eradicate.up)&variable.unresolved>4)')
        self.assertEqual(negated['entry_text'],
            '（敌人数量不为1个，并且没有根除效果），或者variable.unresolved不超过4')

    def test_unknown_negated_action_condition_keeps_de_morgan_and_negation(self):
        row = self.row('!(variable.unresolved|fury<40)', 'reap')
        self.assertIn('variable.unresolved为0，并且恶魔之怒达到40点', row['guide_text'])

    def test_arithmetic_resource_thresholds_are_not_dropped(self):
        cases = [
            ('buff.collapsing_star_stacking.stack+action.reap.souls_consumed>=30',
             '（坍缩之星层数加上收割此次可消耗的灵魂残片数）达到30'),
            ('fury<void_metamorphosis_base_drain_ps*cooldown.void_ray.remains',
             '恶魔之怒低于（虚空变形当前每秒基础怒气消耗乘以虚空射线冷却剩余时间）'),
            ('time-buff.metamorphosis.last_trigger<15',
             '（开战时间减去虚空变形上次触发时的战斗时间）低于15'),
            ('action.void_ray.last_used<buff.metamorphosis.last_trigger',
             '虚空射线上次使用时的战斗时间低于虚空变形上次触发时的战斗时间'),
        ]
        for expression, expected in cases:
            with self.subTest(expression=expression):
                row = self.row(expression)
                self.assertEqual(row['entry_text'], expected)
                self.assertEqual(route_text(row['fields'][0]['segments']), expected)

    def test_boolean_in_numeric_threshold_keeps_zero_one_semantics(self):
        row = self.row('action.reap.souls_consumed>=4+4*(active_enemies>1&buff.eradicate.up)')
        self.assertIn('多于1个敌人，并且具有根除效果时，收割此次可消耗的灵魂残片数达到8个', row['entry_text'])
        self.assertIn('收割此次可消耗的灵魂残片数达到4个', row['entry_text'])
        self.assertNotIn('时取1，否则取0', row['entry_text'])
        self.assertNotIn('成立', row['entry_text'])
        self.assertFalse(any('raw' in part for part in row['entry_condition']))

    def test_previous_cast_stack_cap_and_active_dot(self):
        cases = {
            'prev_gcd.1.void_ray': '此前第1个公共冷却技能是虚空射线',
            '!prev_gcd.2.void_ray': '此前第2个公共冷却技能不是虚空射线',
            'prev_gcd.1.void_ray=0': '此前第1个公共冷却技能不是虚空射线',
            '!buff.void_metamorphosis_stack.at_max_stacks': '虚空变形层数尚未叠满',
            'buff.void_metamorphosis_stack.at_max_stacks=1': '虚空变形层数已叠满',
            'active_dot.soul_immolation=0': '受灵魂献祭持续效果影响的目标数为0个',
            '!active_dot.soul_immolation': '受灵魂献祭持续效果影响的目标数为0',
            'soul_fragments<=8': '当前可用灵魂残片数量不超过8个',
            'buff.eradicate.react>=3': '根除已过反应时间的层数达到3层',
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertEqual(self.row(expression)['entry_text'], expected)

    def test_numeric_truth_is_nonzero_not_positive(self):
        self.assertEqual(self.row('fury')['entry_text'], '恶魔之怒不为0')
        self.assertEqual(self.row('!fury')['entry_text'], '恶魔之怒为0')

    def test_engine_min_max_remainder_and_rounding(self):
        # SimC 的 <? 取较大值、>? 取较小值，不能按字形猜测。
        cases = {'fury<(energy<?40)': '中的较大值', 'fury<(energy>?40)': '中的较小值',
                 'fury<energy%%3': '取余于', 'fury<floor(energy%3)': '向下取整',
                 'fury<ceil(energy%3)': '向上取整', 'fury<@energy': '绝对值',
                 'fury<-1': '相反数'}
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertIn(expected, self.row(expression)['entry_text'])

    def test_reversed_comparisons_and_negation_preserve_boundaries(self):
        for expression, expected in {
            '4<=action.reap.souls_consumed': '达到4个',
            '!(action.reap.souls_consumed>=4)': '低于4个',
            '!(fury<energy*2)': '达到（能量的2倍）',
            '!(fury<=energy*2)': '高于（能量的2倍）',
        }.items():
            with self.subTest(expression=expression):
                self.assertIn(expected, self.row(expression)['entry_text'])

    def test_unknown_action_property_and_absent_identity_stay_explicit(self):
        for expression in ('action.reap.mystery>=4', 'action.missing.souls_consumed>=4'):
            self.assertTrue(any('raw' in part for part in self.row(expression)['entry_condition']))
