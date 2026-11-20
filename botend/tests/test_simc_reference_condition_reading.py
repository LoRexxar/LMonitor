"""人类决策式说明与原始条件必须等价，尤其注意数值门槛与互斥场景。"""

import gzip
import json
import math
import random
from pathlib import Path

from django.test import SimpleTestCase

from botend.services.simc_apl.ast import BinaryExpression, CallExpression, IdentifierExpression, NumberExpression, UnaryExpression
from botend.services.simc_apl.condition_audit import structural_symbols
from botend.services.simc_apl.condition_context import ConditionContext
from botend.services.simc_apl.condition_decisions import boolean_atom, decision, restrict
from botend.services.simc_apl.condition_variables import identifiers
from botend.services.simc_apl.parser import parse
from botend.services.simc_apl.readable import readable_apl


def evaluate(node, values):
    """独立数值求值器，用于检查展示转换没有改变真假结果。"""
    if isinstance(node, NumberExpression):
        return float(node.value)
    if isinstance(node, IdentifierExpression):
        return values[node.name]
    if isinstance(node, UnaryExpression):
        value = evaluate(node.operand, values)
        return {'!': lambda: float(not value), '+': lambda: value, '-': lambda: -value, '@': lambda: abs(value)}[node.operator]()
    if isinstance(node, CallExpression):
        return {'floor': math.floor, 'ceil': math.ceil}[node.function](evaluate(node.argument, values))
    a, b = evaluate(node.left, values), evaluate(node.right, values)
    return {'+': lambda: a + b, '-': lambda: a - b, '*': lambda: a * b,
            '%': lambda: a / b, '%%': lambda: math.fmod(a, b), '<?': lambda: max(a, b), '>?': lambda: min(a, b),
            '=': lambda: a == b, '!=': lambda: a != b, '<': lambda: a < b, '<=': lambda: a <= b,
            '>': lambda: a > b, '>=': lambda: a >= b, '&': lambda: bool(a) and bool(b),
            '|': lambda: bool(a) or bool(b), '^': lambda: bool(a) != bool(b)}[node.operator]()


class ConditionReadingTests(SimpleTestCase):
    symbols = {('talent', 'ascension'): {'name': '升腾'}, ('talent', 'conduit'): {'name': '天神汇聚'},
               ('buff', 'pillar'): {'name': '冰霜之柱'}, ('action', 'hit'): {'name': '打击'}}

    def row(self, expression):
        return readable_apl('actions=hit,if=' + expression, self.symbols)['groups'][0]['rows'][0]

    def test_talent_threshold_becomes_two_concrete_requirements(self):
        row = self.row('energy>=40+20*talent.ascension')
        self.assertEqual(row['condition_text'], '按情况判断：①已学习升腾时，能量达到60点；②未学习升腾时，能量达到40点')
        self.assertNotIn('当按情况', row['guide_text'])
        self.assertTrue(row['guide_text'].endswith('满足对应条件后使用。'))

    def test_multiple_threshold_adjustments_do_not_expand_exponentially(self):
        row = self.row('chi<3-!talent.ascension+talent.conduit')
        self.assertEqual(row['condition_text'], '真气低于动态门槛（基础为3点；未学习升腾时降低1点；已学习天神汇聚时增加1点；各项调整可叠加）')

    def test_duration_states_do_not_read_like_nonzero_checks(self):
        for expression, expected in [('buff.pillar.remains', '冰霜之柱效果仍在持续'),
                                     ('!buff.pillar.remains', '冰霜之柱效果已不在持续'),
                                     ('buff.pillar.remains>0', '冰霜之柱剩余时间高于0秒'),
                                     ('cooldown.hit.remains=0', '打击已冷却完毕')]:
            self.assertEqual(self.row(expression)['condition_text'], expected)
        self.assertIn('低于1秒', self.row('buff.pillar.remains<1')['condition_text'])
        self.assertIn('为1秒', self.row('buff.pillar.remains=1')['condition_text'])
        self.assertIn('不超过0秒', self.row('buff.pillar.remains<=0')['condition_text'])

    def test_common_enemy_gate_becomes_disjoint_scenarios(self):
        row = self.row('(active_enemies=1|energy>40)&(active_enemies!=1|chi>=3)')
        self.assertEqual(row['condition_text'], '按情况判断：①有且仅有1个敌人时，真气达到3点；②敌人数量不为1个时，能量高于40点')
        self.assertIn('active_enemies!=1', row['raw'])

    def test_numeric_buff_stack_is_never_assumed_to_be_boolean(self):
        row = self.row('energy>=40+20*buff.pillar.stack')
        self.assertNotIn('按情况判断', row['condition_text'])
        self.assertIn('层数', row['condition_text'])
        ranked = self.row('talent.ascension.rank&(talent.ascension.rank|energy>40)&chi>=talent.ascension.rank')
        self.assertNotIn('按情况判断', ranked['condition_text'])
        self.assertIn('天赋等级', ranked['condition_text'])

    def test_conditional_threshold_keeps_other_requirements(self):
        row = self.row('buff.pillar.up&energy>40+20*talent.ascension')
        self.assertEqual(row['condition_text'].count('具有冰霜之柱效果'), 2)
        self.assertIn('能量高于60点', row['condition_text'])
        self.assertIn('能量高于40点', row['condition_text'])

    def test_numeric_context_keeps_boolean_zero_one(self):
        expression = parse('actions=hit,if=energy>(1&chi)+20*talent.ascension').lines[0].actions[0].options[0].expression
        gate = expression.right.right.right
        result = restrict(expression, gate, True)
        for chi in (0, 1, 4):
            for energy in (20, 21, 22):
                values = {'energy': energy, 'chi': chi, 'talent.ascension': 1}
                self.assertEqual(evaluate(result, values), evaluate(expression, values))

    def test_different_function_calls_are_not_the_same_condition(self):
        row = self.row('energy>=40+20*(floor(chi)>1)+10*(ceil(fury)>1)')
        self.assertNotIn('按情况判断', row['condition_text'])
        self.assertIn('向下取整', row['condition_text'])
        self.assertIn('向上取整', row['condition_text'])

    def test_all_default_corpus_decisions_match_original_truth(self):
        corpus = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/simc_reference_default_conditions.json.gz').read_bytes()))['sources']
        rng = random.Random(20261120)
        checked = 0
        for source_name, source in corpus.items():
            document = parse(source)
            symbols = structural_symbols(document)
            for line in document.lines:
                for action in getattr(line, 'actions', ()):
                    context = ConditionContext(symbols).for_action(action.name)
                    for option in action.options:
                        node = getattr(option, 'expression', None)
                        if node is None:
                            continue
                        split = decision(node, context)
                        if split is None:
                            continue
                        predicate, yes, no = split
                        for _ in range(12):
                            values = {name: rng.choice((0, 1)) if boolean_atom(IdentifierExpression(name, node.range), context)
                                      else rng.choice((-2, 0, 0.5, 1, 2, 3, 4, 20, 40, 60, 100)) for name in identifiers(node)}
                            try:
                                original = bool(evaluate(node, values))
                                actual = bool(evaluate(yes if evaluate(predicate, values) else no, values))
                            except (ZeroDivisionError, ValueError, OverflowError):
                                continue
                            with self.subTest(apl=source_name, line=line.range.start.line, option=option.name):
                                self.assertEqual(actual, original)
                            checked += 1
        self.assertGreater(checked, 500)
