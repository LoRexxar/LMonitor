"""Legacy prose validation: absence, predicate scope, and adversarial neighbors."""
from django.test import SimpleTestCase
from botend.services import player_observation as observation
from botend.tests import test_player_observation_resilience_guard as guard


class AbsenceGuardTests(SimpleTestCase):
    validate = guard.LegacyProseFixtureMixin.validate

    def test_seven_captured_missing_fact_clauses(self):
        for text in (
            '公开记录无历史加分或交易事实',
            '组队目的与收费亦无公开事实',
            '钥匙归属、背包库存与交易均无公开证据',
            '不确认交易',
            '钥匙归属与交易事实在公开数据中完全缺失',
            '无净增分或交易证据',
            '钥匙归属与交易均无事实',
        ):
            for explanation in (False, True):
                with self.subTest(text=text, explanation=explanation):
                    self.validate(text, explanation=explanation)

    def test_legacy_missing_fact_support_and_basis(self):
        for text in ('交易无公开事实支持', '收费无公开事实依据'):
            for bounded in (False, True):
                for explanation in (False, True):
                    with self.subTest(text=text, bounded=bounded, explanation=explanation):
                        self.validate(text, bounded=bounded, explanation=explanation)
        for text in ('交易无公开事实支持且交易确实发生',
                     '收费无公开事实依据但已经付款',
                     '交易无公开事实支持收费'):
            for bounded in (False, True):
                with self.subTest(text=text, bounded=bounded):
                    with self.assertRaisesRegex(observation.InvalidResult, 'invalid_inference_text'):
                        self.validate(text, bounded=bounded)

    def test_absence_cannot_absorb_positive_predicates(self):
        for text in (
            '已经交易', '交易确实发生',
            '交易已经发生但细节在公开数据中完全缺失',
            '收费亦无公开事实，但是已经付款',
            '不能确认交易，但已经付款',
            '无历史加分或交易已经发生的事实',
            '无净增分且交易事实存在',
            '交易均无事实证明没有交易',
            '不确认交易并收取费用且收费',
            '不确认交易且交易确实发生',
            '交易事实在公开数据中完全缺失且交易已发生',
        ):
            for explanation in (False, True):
                with self.subTest(text=text, explanation=explanation):
                    with self.assertRaisesRegex(observation.InvalidResult, 'invalid_inference_text'):
                        self.validate(text, explanation=explanation)

    def test_missing_facts_do_not_override_inference_guards(self):
        for text in (
            '本人无净收益', '本人没有任何加分', '并非为本人上分',
            '本人同本更高削弱自然掉钥解释', '本人跑前保护均未知',
            '不证明本人无收益且事实上本人无净收益。',
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
                    self.validate(text)
        for text in ('不能确认本人无净收益', '也不能推广为并非为本人上分'):
            self.validate(text)


class PredicateScopeTests(SimpleTestCase):
    validate = guard.LegacyProseFixtureMixin.validate

    def test_negated_predicate_pairs(self):
        for text in ('不能上升为交易或带人定论', '不能反推钥匙主人或交易',
                     '不能倒推动机或交易', '也不指向动机或交易',
                     '故不外推为带人或交易', '不等同于本人无净收益',
                     '不等同本人没有净收益', '不得统一称跑前保护未知或已确认',
                     '钥匙归属与交易事实均未观测',
                     '不证明全员实际净增分、动机或交易',
                     '不能据此定性为交易或确认每场净增分',
                     '无法确认纯粹社交协作或排除交易',
                     '缺少稳定合作性质、收费及完整历史的证据',
                     '钥匙归属、收费、真实核心身份均未知',
                     '钥匙归属与交易在公开通关与当前计分中均无事实依据',
                     '无法确认谁受益、是否收费'):
            with self.subTest(text=text):
                self.validate(text)
                for suffix in ('且他收费', '并他收费'):
                    with self.assertRaises(observation.InvalidResult):
                        self.validate(text + suffix)

    def test_cited_domain_member_and_quantifier_contract(self):
        context = {'subject_key': 'subject', 'events': [
            {'run_id': rid, 'level': 22, 'members': {
                key: {'state': 'confirmed_before_start' if key in ('subject', 'a', 'b')
                      else 'unknown', 'required_level': 23}
                for key in ('subject', 'a', 'b', 'c', 'd')}}
            for rid in (200, 201, 202)]}
        kwargs = dict(resilience=context, explanation=True, run_ids=(200, 201))
        for text in ('多数场次本人同本更高且跑前保护未知',
                     '部分场次本人跑前保护未知', '部分场次缺本人跑前保护记录',
                     '另一场全跑前保护未知', '另一场全队跑前保护未知',
                     '这些记录仅证明被引用场次跑前保护已覆盖全队',
                     '三场五人身份已验证且稳定组合均在场', '十一场五人身份已验证',
                     '三场独立通关中本人开打前已有同本更高限时'):
            with self.subTest(text=text), self.assertRaisesRegex(
                    observation.InvalidResult, 'unsupported_inference_assertion'):
                self.validate(text, **kwargs)
        for text in ('缺跑前保护记录的场次不能排除升钥或库存',
                     '这些记录仅证明被引用场次跑前保护已覆盖本人',
                     '两场五人身份已验证且稳定三人组合均在场',
                     '部分成员跑前保护未知', '第十三场不能证明净增分'):
            with self.subTest(text=text):
                self.validate(text, **kwargs)
        with self.assertRaises(observation.InvalidResult):
            self.validate('多场五人身份已验证且稳定组合在场',
                          resilience=context, explanation=True, run_ids=(200,))
        for rid in (200, 201):
            context['events'][rid - 200]['members']['subject']['state'] = 'unknown'
        self.validate('多数场次本人同本更高且跑前保护未知',
                      resilience=context, explanation=True, run_ids=(200, 201, 202))
        for member in context['events'][0]['members'].values():
            member['state'] = 'unknown'
        self.validate('另一场全跑前保护未知', **kwargs)

    def test_prior_chronology_covers_every_cited_run(self):
        text = '已有同本较高层观测说明被引用场次跑前已有更高记录'
        self.validate(text, explanation=True, counter_run_ids=(200,))
        with self.assertRaises(observation.InvalidResult):
            self.validate(text, explanation=True, run_ids=(201,), counter_run_ids=(200,))
        with self.assertRaises(observation.InvalidResult):
            self.validate('本人已有同本较高层观测，通常削弱被带低层的解释')
        self.validate('本人已有同本较高层观测，不能据此削弱被带低层的解释')

    def test_hidden_semantic_assertions(self):
        for text in ('本人已观测同本较高层仅反驳对应场次的后续进步解释',
                     '已有同本较高层观测说明被引用场次跑前已有更高记录',
                     '这只反驳对应场次的后续进步叙述',
                     '这些场次不构成其净加分',
                     '仅证明被引用场次跑前保护'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
                    self.validate(text, resilience={'subject_key': 'subject', 'events': []})
                self.validate('不能断言' + text, resilience=None)

    def test_conditional_category_not_whole_sample(self):
        self.validate('缺跑前保护记录的场次不能排除升钥或库存')
        # The default fixture has only one unknown subject out of two runs.
        with self.assertRaises(observation.InvalidResult):
            self.validate('多数场次本人同本更高且跑前保护未知')
        self.validate('其他成员同本历史与跑前坚韧条件未知')
        with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
            self.validate('其他成员和本人跑前保护未知')
        with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
            self.validate('本人跑前保护未知')


class ProseScopeTests(SimpleTestCase):
    validate = guard.LegacyProseFixtureMixin.validate

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
