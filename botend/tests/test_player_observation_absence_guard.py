"""Minimal original clauses; full private captures are intentionally not copied."""
from django.test import SimpleTestCase
from botend.services import player_observation as observation
from botend.tests import test_player_observation_resilience_guard as guard


class AbsenceGuardTests(SimpleTestCase):
    validate = guard.ResilienceGuardTests.validate

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
