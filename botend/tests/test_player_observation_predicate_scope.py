"""Complete negated predicates must not license neighboring assertions."""
from django.test import SimpleTestCase
from botend.services import player_observation as observation
from botend.tests import test_player_observation_resilience_guard as _fixture_module


class PredicateScopeTests(SimpleTestCase):
    validate = _fixture_module.ResilienceGuardTests.validate

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
