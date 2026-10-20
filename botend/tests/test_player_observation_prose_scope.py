"""Minimal captured clauses plus adversarial neighbors, never private raw fixtures."""
from django.test import SimpleTestCase

from botend.services import player_observation as observation
from botend.tests import test_player_observation_resilience_guard as guard


class ProseScopeTests(SimpleTestCase):
    validate = guard.ResilienceGuardTests.validate

    def test_captured_unknown_predicates_and_noun_lists(self):
        for text in (
            '钥匙归属与交易完全未知',
            '收费与受益者未知',
            '收费与否不明',
            '收费与关系未知',
            '收费与组队目的未知',
            '钥匙归属、收费与完整历史也未确认',
            '钥匙归属、收费与完整计分历史均未知',
            '钥匙归属、库存与交易状态全部未知',
            '钥匙归属与收费仍完全未知',
            '归属与收费仍是未知项',
            '收费事实同样未知',
            '收费更无从判断',
        ):
            for explanation in (False, True):
                with self.subTest(text=text, explanation=explanation):
                    self.validate(text, explanation=explanation)

    def test_unknown_protection_noun_list_needs_unknown_context(self):
        # The lexical fixture previously borrowed a confirmed subject from
        # ResilienceGuardTests. A shared unknown predicate must not erase it.
        text = '钥匙归属、收费与跑前保护状态均未知'
        context = {'subject_key': 'subject', 'events': [
            {'run_id': 200, 'level': 22, 'members': {'subject': {'state': 'unknown'}}}]}
        for explanation in (False, True):
            self.validate(text, explanation=explanation, resilience=context)
            with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
                self.validate(text, explanation=explanation)

    def test_captured_absence_and_negated_entailment(self):
        for text in (
            '公开通关与计分均不含钥匙归属、库存或交易事实',
            '公开记录不包含钥匙主人或交易事实',
            '钥匙归属与交易事实均不可见',
            '交易事实未出现',
            '钥匙归属与交易没有公开事实支撑',
            '收费与动机均无法从公开记录确认',
            '钥匙归属、动机与收费均无已确认信息',
            '也不存在归属或交易的直接事实',
            '不推断交易或动机',
            '不作任何交易或归属定性',
            '不下交易或归属结论',
            '不以任何单场跑前记录推断钥匙主人',
            '不决定本场钥匙属于谁',
            '不得泛化为长期身份或交易',
            '共同提高不等于证明协助动机或交易',
            '已确认的跑前更高记录只针对被引用场次且不等于钥匙主人',
            '坚韧确认仅涉及保护状态而非钥匙主人',
            '任何归属与交易判断都不成立',
            '支持不延伸到钥匙协调或收费定性',
            '目前无证据能将日常协作与升钥或交易区分开',
            'cannot确认谁提供钥匙',
            '各场钥匙由谁提供、是否旧钥匙或他人钥匙均无公开事实',
            '且协助不需要收费',
        ):
            for explanation in (False, True):
                with self.subTest(text=text, explanation=explanation):
                    self.validate(text, explanation=explanation)

    def test_existing_non_entailment_survives_tighter_scope(self):
        for text in (
            '也不证明动机或交易',
            '已确认跑前保护的场次也不证明钥匙属于本人',
            '在缺乏收费与钥匙归属证据时',
            '不能定性交易',
            '坚韧资格与钥匙主人不能由结构推断',
            '不能延伸为钥匙归属、组队目的或交易',
            '不证明全员净增分、动机或交易',
            '共同升层的时序既可能是他人提供钥匙',
            '更不能确定钥匙归属或交易',
            '不能据此确定交易或钥匙归属',
            '仅凭快照不能证明每场净增分或交易',
            '公开记录无任何交易或归属事实',
            '不能确定本场钥匙主人或来源',
            '不证明任何人净增分、钥匙归属或交易',
            '因缺少跑前保护确认与收费信息',
        ):
            with self.subTest(text=text):
                self.validate(text)
                with self.assertRaisesRegex(observation.InvalidResult, 'invalid_inference_text'):
                    self.validate(text + '且他收费')

    def test_remaining_runs_are_not_the_confirmed_subset(self):
        self.validate('其余各场跑前保护未知')
        for text in ('本人跑前保护均未知', '其余各场未知且本人跑前保护均未知'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
                    self.validate(text)

    def test_neighbors_cannot_inherit_unknown_or_negation(self):
        for text in (
            '交易完全未知且收费',
            '收费与受益者未知且交易确实发生',
            '收费与否不明所以他交易钥匙',
            '收费不明智',
            '交易完全未知情的队友已经付款',
            '公开通关与计分不含钥匙归属且交易事实存在',
            '公开记录不包含钥匙主人或交易已经发生的事实',
            '没有交易证据且他收费',
            '没有交易已经发生的事实',
            '交易事实不可见但已经付款',
            '不能确认交易且收费',
            '不推断交易而且收费',
            '不作任何交易或归属定性且他付款',
            '不等于交易所以他收费',
            '不涉及交易并提供了钥匙',
            '协助不需要收费且他实际收费',
            '协助需要收费',
            '不能确认：他提供钥匙',
            '不能确认？他提供钥匙',
            '钥匙由谁提供？他提供了钥匙',
            '不可见交易已经发生',
        ):
            for explanation in (False, True):
                with self.subTest(text=text, explanation=explanation):
                    with self.assertRaisesRegex(observation.InvalidResult, 'invalid_inference_text'):
                        self.validate(text, explanation=explanation)

    def test_old_semantic_controls_still_block(self):
        for text in (
            '本人无净收益', '本人没有任何加分',
            '不能确认本人无净收益且本人没有任何加分',
            '并非为本人上分', '本人同本更高削弱自然掉钥解释',
            '本人跑前保护均未知',
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
                    self.validate(text)
        for text in ('不能确认本人无净收益', '也不能推广为并非为本人上分'):
            self.validate(text)
        joint = '本人同本更高且本人跑前全本更高层保护已确认削弱自然掉钥解释'
        self.validate(joint, explanation=True, run_ids=(200,))
        with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
            self.validate(joint, explanation=True, run_ids=(201,))
        with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
            self.validate(joint.replace('削弱', '排除'), explanation=True, run_ids=(200,))

    def test_format_is_not_relaxed_or_rewritten(self):
        for text in ('交易未知一百' * 50, '交易未知1', '交易未知\n', '交易未知https://example.com'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(observation.InvalidResult, 'invalid_inference_text'):
                    self.validate(text)
        text = '交易未知。'
        self.assertEqual(self.validate(text)['investigation']['summary'], text)
