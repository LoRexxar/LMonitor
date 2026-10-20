"""Bounded-generation guards; no frozen publication or network dependency."""
import json
from django.test import SimpleTestCase
from botend.services import player_observation as observation


class ResilienceGuardTests(SimpleTestCase):
    def validate(self, text, *, resilience=True, bounded=True, explanation=False,
                 run_ids=(), counter_run_ids=()):
        plan = {'triggered': True, 'questions': [], 'hypotheses': [
            {'id': name, 'status': 'unknown', 'support_ids': [],
             'counter_ids': [f'behavior:prior_higher:{rid}' for rid in counter_run_ids],
             'unknown_ids': [f'behavior:key_recovery_unknown:{rid}' for rid in run_ids]}
            for name in ('key_access', 'routine_group')]}
        context = {'subject_key': 'subject', 'events': [
            {'run_id': 200, 'level': 22, 'members': {'subject': {
                'state': 'confirmed_before_start', 'required_level': 23}}},
            {'run_id': 201, 'level': 22, 'members': {'subject': {'state': 'unknown'}}}]}
        evidence = {'eligible_labels': [], 'evidence': [], 'metrics': {
            'investigation': plan, 'behavior_review': {
                'resilience': context if resilience is True else resilience}}}
        value = {'summary': '保留未知解释。' if explanation else text,
                 'confidence': '低', 'next_question_ids': [], 'hypotheses': [
            {'id': h['id'], 'assessment': 'unknown', 'evidence_ids': h['unknown_ids'],
             'counter_evidence_ids': h['counter_ids'], 'explanation': text if explanation else '保留未知解释。'}
            for h in plan['hypotheses']]}
        return observation._validate(json.dumps({'observations': [],
            'conclusion_id': 'public_sample', 'investigation': value}), evidence,
            require_investigation=bounded)

    def test_reject_observed_contradictions(self):
        for text in (
            '该假设只能保持未知：已知缺全更高层跑前保护记录，可能需要升钥。',
            '钥匙归属、跑前全层保护与背包库存均未知。',
            '本人已有同本更高观测只削弱自然掉钥解释',
            '本人跑前坚韧保护均未知。',
        ):
            for explanation in (False, True):
                with self.subTest(text=text, explanation=explanation):
                    with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
                        self.validate(text, explanation=explanation)

    def test_allow_scoped_unknown_and_negation(self):
        for text in (
            '本人部分场次跑前保护已确认，其他场次跑前保护仍未知。',
            '其他成员跑前全层保护与背包库存均未知。',
            '本人已确认跑前保护，队友跑前保护未知。',
            '不能将本人跑前全层保护概括为全部未知。',
            '不能说已知缺全更高层跑前保护记录。',
            '不能据单本更高削弱自然掉钥解释。',
            '本人同本更高不能削弱自然掉钥解释。',
            '本人同本更高不足以排除自然掉钥。',
            '本人跑前全层保护未完整证实。',
        ):
            with self.subTest(text=text):
                self.validate(text)

    def test_no_context_or_other_members_proof_does_not_guess(self):
        for context in (None, {}, {'events': [{'members': {'other': {'state': 'confirmed_before_start'}}}]},
                        {'subject_key': 'subject', 'events': [{'members': {
                            'subject': {'state': 'unknown'}, 'other': {'state': 'confirmed_before_start'}}}]}):
            with self.subTest(context=context):
                self.validate('跑前全层保护均未知。', resilience=context)
        self.validate('跑前全层保护均未知。', bounded=False)
        self.validate('本人已有同本更高观测只削弱自然掉钥解释', bounded=False)

    def test_non_entailment_does_not_cover_new_conjoined_assertion(self):
        for text in ('不证明本人无收益且事实上本人无净收益。',
                     '同本更高不证明掉钥且确实削弱自然掉钥解释。',
                     '不证明存在保护且本人跑前保护未知。',
                     '不能证明本人无收益且事实上本人无净收益。'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
                    self.validate(text)
        self.validate('不证明本人无净收益。')
        self.validate('不能据同本更高且阵容固定削弱自然掉钥解释。')

    def test_logical_non_entailment_is_not_a_zero_benefit_claim(self):
        for text in ('也不证明本人无净收益。', '同本更高不代表本人无收益。',
                     '同本更高不说明并非为本人上分。',
                     '本人同本更高不证明能够削弱自然掉钥解释。',
                     '不能断言本人无收益或并非为本人上分。'):
            with self.subTest(text=text):
                self.validate(text)
        for text in ('不证明本人无收益，但本人当场无净收益。',
                     '不代表本人无收益，因此并非为本人上分。'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
                    self.validate(text)

    def test_postposed_buming_has_a_fact_boundary(self):
        self.validate('钥匙归属与收费不明。')
        self.validate('钥匙归属与收费不明且无法确认交易。')
        for text in ('收费不明智。', '收费不明但已经交易。', '收费不明，因此确定是代练。'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(observation.InvalidResult, 'invalid_inference_text'):
                    self.validate(text)

    def test_review_qualified_missing_and_coordinated_negation(self):
        for text in (
            '本人跑前保护已确认，但仍缺队友跑前保护记录。',
            '本人跑前保护已确认，仍缺其他场次跑前保护记录。',
            '多数场次全层跑前保护未知。',
            '不能据同本更高和阵容固定削弱自然掉钥解释。',
            '不能断言本人无收益或并非为本人上分。',
            '钥匙归属、交易与动机均无公开事实。',
        ):
            for explanation in (False, True):
                with self.subTest(text=text, explanation=explanation):
                    self.validate(text, explanation=explanation)

    def test_joint_protection_requires_same_run_and_l_plus_one(self):
        text = '本人同本更高且开打前全更高层坚韧保护已确认可削弱自然掉钥解释。'
        for kwargs in ({'run_ids': (200,)}, {'counter_run_ids': (200,)}):
            with self.subTest(kwargs=kwargs):
                self.validate(text, explanation=True, **kwargs)
        context = {'subject_key': 'subject', 'events': [
            {'run_id': 200, 'level': 22, 'members': {'subject': {
                'state': 'confirmed_before_start', 'required_level': 23}}}]}
        self.validate(text, resilience=context)  # Summary uses all known events.
        for kwargs in (
            {'explanation': True},  # No references means no guessed scope.
            {'explanation': True, 'run_ids': (201,)},
            {'explanation': True, 'run_ids': (200, 201)},
            {'explanation': True, 'run_ids': (999,)},
            {},  # A mixed summary cannot borrow the one confirmed run.
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(
                    observation.InvalidResult, 'unsupported_inference_assertion'):
                self.validate(text, **kwargs)
        for state in (
            {'state': 'unknown', 'required_level': 23},
            {'state': 'confirmed_before_start', 'required_level': 22},
            {'state': 'confirmed_before_start'},
        ):
            context['events'][0]['members']['subject'] = state
            with self.subTest(state=state), self.assertRaises(observation.InvalidResult):
                self.validate(text, resilience=context, explanation=True, run_ids=(200,))

    def test_review_relaxations_do_not_excuse_positive_assertions(self):
        for text in (
            '本人跑前保护已确认，但仍缺本人跑前保护记录。',
            '本人跑前保护已确认，仍缺跑前保护记录。',
            '本人已有同本更高限时削弱自然掉钥解释。',
            '本人当场无净收益。',
            '本人同本更高且队友开打前全更高层坚韧保护已确认可削弱自然掉钥解释。',
            '队友同本更高且开打前全更高层坚韧保护已确认可削弱自然掉钥解释。',
            '本人同本更高且开打前全更高层坚韧保护已确认可排除自然掉钥解释。',
            '钥匙归属、交易与动机均无公开事实，但已经交易。',
            '钥匙归属、交易与动机均无公开事实因此他交易钥匙。',
        ):
            with self.subTest(text=text), self.assertRaises(observation.InvalidResult):
                self.validate(text, explanation=True, run_ids=(200,))
        for connector in ('但', '因此', '却'):
            for text in (
                f'不能据同本更高和阵容固定削弱自然掉钥解释{connector}本人同本更高可削弱自然掉钥解释。',
                f'不能断言本人无收益{connector}并非为本人上分。',
            ):
                with self.subTest(text=text), self.assertRaises(observation.InvalidResult):
                    self.validate(text)

    def test_single_dungeon_guard_does_not_need_confirmed_context(self):
        with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
            self.validate('本人已有同本更高观测只削弱自然掉钥解释', resilience=None)

class ReviewedPreflightContractTests(SimpleTestCase):
    def test_untouched_preflight_raw_results(self):
        for case in REVIEWED_PREFLIGHT_CASES:
            with self.subTest(analysis_id=case['analysis_id']):
                if case['analysis_id'] == 41:
                    observation._validate(case['raw'], case['evidence'], require_investigation=True)
                else:
                    with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
                        observation._validate(case['raw'], case['evidence'], require_investigation=True)
                observation._validate(case['raw'], case['evidence'])


# Exact GLM raw_result strings from candidate-preflight-v7-{protection,positive}-r1.
# Inputs retain the validator contract; unrelated metrics and proof detail omitted.
REVIEWED_PREFLIGHT_CASES = [{'analysis_id': 41,
  'raw': '{"observations":[{"code":"recent_inquiry","confidence":"低","reason_id":"recent_clue","limitation_ids":["current_not_history","unknown_motive"],"evidence_ids":["investigation:own:21063045","investigation:team:21063045","investigation:own:21070615"]}],"conclusion_id":"public_sample","investigation":{"summary":"近期多次出现本人低于自身同本现有记录仍与稳定组合连打的情况，形成具体调查线索。各场开打前的跑前保护仅两场对本人已确认，其余成员与多数场次坚韧状态未知；钥匙归属、交易与动机均无公开事实。协助、钥匙协调、日常协作与旧库存解释并存，后续进步解释被跑前更高记录削弱。","confidence":"低","hypotheses":[{"id":"key_access","assessment":"unknown","evidence_ids":["behavior:interpretation_limits"],"counter_evidence_ids":[],"explanation":"钥匙协调无法证实：多数场次全层跑前保护未知，已解锁与已证实保护也不能证明钥匙归属，仍可能为旧库存或他人钥匙；缺乏支持与反证，只能保留为未知。"},{"id":"assistance","assessment":"possible","evidence_ids":["behavior:interpretation_limits"],"counter_evidence_ids":[],"explanation":"本人低于自身同本记录的连打与稳定组合、外围席位变化构成协助线索，但外围成员成绩方向未知，不能当作受益者；钥匙归属与动机不明，朋友互助也可同属协助，仅为低置信可能。"},{"id":"routine_group","assessment":"possible","evidence_ids":["investigation:team:21063045","investigation:team:21070615","investigation:motive_unknown:21063045"],"counter_evidence_ids":[],"explanation":"稳定组合多场同场支持日常协作或练习，朋友互助与协助上分并不互斥；动机与钥匙归属未知，使协作解释不能升级为确证，只能作为与协助并列的可能。"},{"id":"later_progress","assessment":"weakened","evidence_ids":["investigation:motive_unknown:21063045"],"counter_evidence_ids":["behavior:prior_higher:21040182","behavior:prior_higher:21063045","behavior:prior_higher:21070615","behavior:prior_higher:21078170","behavior:prior_higher:21088094","behavior:prior_higher:21094475","behavior:prior_higher:21098254"],"explanation":"各场开打前本人均已有同本更高限时，故不能以之后才达到更高层解释这些低层记录；这只削弱对应场次的后续进步解释，不等同坚韧资格，也不排除他人受益或库存解释。"}],"next_question_ids":["scored_runs:2c9e048885299d0bdb9e8a3447fb97e1c2eaaeab81974da2695c0d86e6375a93","scored_runs:316d805e3289ac5b5fc822c31f4fbfab291cf7f323df7b71a7fad0c66ddbf69e","scored_runs:a93012d60c58af8d5426ce926ef2d6aba32f793afbc7a4b27d12983dbe17def7"]}}',
  'evidence': {'eligible_labels': [{'code': 'timed_experience',
                                    'label': '限时经历',
                                    'confidence_max': '高',
                                    'evidence_ids': ['timed_experience'],
                                    'reason_options': {'timed_record': '公开记录提供了可确认的限时经历。'},
                                    'limitation_options': {'sample_scope': '已知限时记录不能说明整个赛季的成功率或未公开经历。'},
                                    'required_limitation_ids': ['sample_scope']},
                                   {'code': 'multiple_specs',
                                    'label': '多专精活动',
                                    'confidence_max': '高',
                                    'evidence_ids': ['multiple_specs'],
                                    'reason_options': {'observed_specs': '已验证的当场阵容或近期记录显示本人使用过不同专精。'},
                                    'limitation_options': {'no_main_spec': '不能由这些样本判断主副专精、熟练程度或整个赛季的专精使用比例。'},
                                    'required_limitation_ids': ['no_main_spec']},
                                   {'code': 'fixed_team',
                                    'label': '近期固定五人／偶尔替补',
                                    'confidence_max': '中',
                                    'evidence_ids': ['fixed_team'],
                                    'reason_options': {'stable_five': '近期已验证阵容呈固定五人或偶尔替补结构，不代表真实核心或组队目的。'},
                                    'limitation_options': {'sample_scope': '仅判断已知阵容样本；固定车队也可能偶尔替补，不能推断整个赛季始终固定。'},
                                    'required_limitation_ids': ['sample_scope']},
                                   {'code': 'recent_inquiry',
                                    'label': '近期协作线索（待解释）',
                                    'confidence_max': '低',
                                    'evidence_ids': ['investigation:own:21063045', 'investigation:team:21063045',
                                                     'investigation:own:21070615'],
                                    'reason_options': {'recent_clue': '已观测的成绩时序或成员关系形成具体调查线索，仍需比较协作与钥匙解释。'},
                                    'limitation_options': {'current_not_history': '当前成绩仅为代理，不代表历史当场加分。',
                                                           'unknown_motive': '协作、练习和后续进步仍可能；无法确认钥匙归属或交易。'},
                                    'required_limitation_ids': ['current_not_history', 'unknown_motive']}],
               'evidence': [{'id': 'timed_experience'}, {'id': 'multiple_specs'},
                            {'id': 'behavior:prior_higher:21040182'}, {'id': 'behavior:lead:21040182'},
                            {'id': 'behavior:key_recovery_unknown:21040182'}, {'id': 'behavior:prior_higher:21063045'},
                            {'id': 'behavior:lead:21063045'}, {'id': 'behavior:key_recovery_unknown:21063045'},
                            {'id': 'behavior:prior_higher:21070615'}, {'id': 'behavior:lead:21070615'},
                            {'id': 'behavior:carrying:21070615'}, {'id': 'behavior:prior_higher:21078170'},
                            {'id': 'behavior:lead:21078170'}, {'id': 'behavior:key_recovery_unknown:21078170'},
                            {'id': 'behavior:prior_higher:21088094'}, {'id': 'behavior:key_recovery_unknown:21088094'},
                            {'id': 'behavior:prior_higher:21094475'}, {'id': 'behavior:key_recovery_unknown:21094475'},
                            {'id': 'behavior:prior_higher:21098254'}, {'id': 'behavior:key_recovery_unknown:21098254'},
                            {'id': 'fixed_team'}, {'id': 'investigation:own:21063045'},
                            {'id': 'investigation:team:21063045'}, {'id': 'investigation:unknown:21063045'},
                            {'id': 'investigation:unknown:group1:21063045'},
                            {'id': 'investigation:unknown:group2:21063045'},
                            {'id': 'investigation:unknown:group3:21063045'},
                            {'id': 'investigation:unknown:group4:21063045'},
                            {'id': 'investigation:group_unknown:21063045'},
                            {'id': 'investigation:core_unknown:21063045'},
                            {'id': 'investigation:motive_unknown:21063045'}, {'id': 'investigation:own:21070615'},
                            {'id': 'investigation:team:21070615'}, {'id': 'investigation:unknown:21070615'},
                            {'id': 'investigation:unknown:group1:21070615'},
                            {'id': 'investigation:unknown:group2:21070615'},
                            {'id': 'investigation:unknown:group3:21070615'},
                            {'id': 'investigation:unknown:group4:21070615'},
                            {'id': 'investigation:group_unknown:21070615'},
                            {'id': 'investigation:core_unknown:21070615'},
                            {'id': 'investigation:motive_unknown:21070615'}, {'id': 'investigation:own:21078170'},
                            {'id': 'investigation:team:21078170'}, {'id': 'investigation:unknown:21078170'},
                            {'id': 'investigation:unknown:group1:21078170'},
                            {'id': 'investigation:unknown:group2:21078170'},
                            {'id': 'investigation:unknown:group3:21078170'},
                            {'id': 'investigation:unknown:group4:21078170'},
                            {'id': 'investigation:group_unknown:21078170'},
                            {'id': 'investigation:core_unknown:21078170'},
                            {'id': 'investigation:motive_unknown:21078170'}, {'id': 'behavior:interpretation_limits'}],
               'metrics': {'investigation': {'triggered': True,
                                             'hypotheses': [{'id': 'routine_group',
                                                             'label': '日常协作、朋友互助或练习',
                                                             'status': 'compatible',
                                                             'support_ids': ['investigation:team:21063045',
                                                                             'investigation:team:21070615',
                                                                             'investigation:team:21078170'],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['investigation:motive_unknown:21063045',
                                                                             'investigation:motive_unknown:21070615',
                                                                             'investigation:motive_unknown:21078170']},
                                                            {'id': 'assistance',
                                                             'label': '疑似协助他人上分',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits',
                                                                             'investigation:unknown:21063045',
                                                                             'investigation:unknown:group1:21063045',
                                                                             'investigation:unknown:group2:21063045',
                                                                             'investigation:unknown:group3:21063045',
                                                                             'investigation:unknown:group4:21063045',
                                                                             'investigation:group_unknown:21063045',
                                                                             'investigation:core_unknown:21063045',
                                                                             'investigation:unknown:21070615',
                                                                             'investigation:unknown:group1:21070615',
                                                                             'investigation:unknown:group2:21070615',
                                                                             'investigation:unknown:group3:21070615',
                                                                             'investigation:unknown:group4:21070615',
                                                                             'investigation:group_unknown:21070615',
                                                                             'investigation:core_unknown:21070615',
                                                                             'investigation:unknown:21078170',
                                                                             'investigation:unknown:group1:21078170',
                                                                             'investigation:unknown:group2:21078170',
                                                                             'investigation:unknown:group3:21078170',
                                                                             'investigation:unknown:group4:21078170',
                                                                             'investigation:group_unknown:21078170',
                                                                             'investigation:core_unknown:21078170',
                                                                             'investigation:motive_unknown:21063045',
                                                                             'investigation:motive_unknown:21070615',
                                                                             'investigation:motive_unknown:21078170',
                                                                             'behavior:key_recovery_unknown:21040182',
                                                                             'behavior:key_recovery_unknown:21063045',
                                                                             'behavior:key_recovery_unknown:21078170',
                                                                             'behavior:key_recovery_unknown:21088094',
                                                                             'behavior:key_recovery_unknown:21094475',
                                                                             'behavior:key_recovery_unknown:21098254']},
                                                            {'id': 'key_access',
                                                             'label': '钥匙协调线索',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits',
                                                                             'investigation:motive_unknown:21063045',
                                                                             'investigation:motive_unknown:21070615',
                                                                             'investigation:motive_unknown:21078170']},
                                                            {'id': 'external_assistance',
                                                             'label': '高成绩协作者线索',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits',
                                                                             'investigation:unknown:21063045',
                                                                             'investigation:unknown:group1:21063045',
                                                                             'investigation:unknown:group2:21063045',
                                                                             'investigation:unknown:group3:21063045',
                                                                             'investigation:unknown:group4:21063045',
                                                                             'investigation:group_unknown:21063045',
                                                                             'investigation:core_unknown:21063045',
                                                                             'investigation:unknown:21070615',
                                                                             'investigation:unknown:group1:21070615',
                                                                             'investigation:unknown:group2:21070615',
                                                                             'investigation:unknown:group3:21070615',
                                                                             'investigation:unknown:group4:21070615',
                                                                             'investigation:group_unknown:21070615',
                                                                             'investigation:core_unknown:21070615',
                                                                             'investigation:unknown:21078170',
                                                                             'investigation:unknown:group1:21078170',
                                                                             'investigation:unknown:group2:21078170',
                                                                             'investigation:unknown:group3:21078170',
                                                                             'investigation:unknown:group4:21078170',
                                                                             'investigation:group_unknown:21078170',
                                                                             'investigation:core_unknown:21078170',
                                                                             'investigation:motive_unknown:21063045',
                                                                             'investigation:motive_unknown:21070615',
                                                                             'investigation:motive_unknown:21078170']},
                                                            {'id': 'later_progress',
                                                             'label': '后续进步造成当前上限与近期通关差距',
                                                             'status': 'weakened',
                                                             'support_ids': [],
                                                             'counter_ids': ['behavior:prior_higher:21040182',
                                                                             'behavior:prior_higher:21063045',
                                                                             'behavior:prior_higher:21070615',
                                                                             'behavior:prior_higher:21078170',
                                                                             'behavior:prior_higher:21088094',
                                                                             'behavior:prior_higher:21094475',
                                                                             'behavior:prior_higher:21098254'],
                                                             'unknown_ids': ['investigation:motive_unknown:21063045',
                                                                             'investigation:motive_unknown:21070615',
                                                                             'investigation:motive_unknown:21078170']},
                                                            {'id': 'being_carried',
                                                             'label': '疑似获得队友协助',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits']},
                                                            {'id': 'joint_progress',
                                                             'label': '共同提高成绩线索',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits']}],
                                             'questions': [{'id': 'scored_history:36abaedcf8acb88d053afd09cb4ff8fcbf75df37fd1d0b3c36dfe5483f402c5a:16359'},
                                                           {'id': 'scored_history:36abaedcf8acb88d053afd09cb4ff8fcbf75df37fd1d0b3c36dfe5483f402c5a:16865'},
                                                           {'id': 'scored_history:73d320003a381df7d7035c3d3a832022cb8a55a2cd011136b8d148cd1fa1dff0:16865'},
                                                           {'id': 'scored_history:e031e7428cbba27b282a92cb54a14defd78a671de08d9f48f3e300ae290ed9c1:16865'},
                                                           {'id': 'scored_runs:2c9e048885299d0bdb9e8a3447fb97e1c2eaaeab81974da2695c0d86e6375a93'},
                                                           {'id': 'scored_runs:316d805e3289ac5b5fc822c31f4fbfab291cf7f323df7b71a7fad0c66ddbf69e'},
                                                           {'id': 'scored_runs:a93012d60c58af8d5426ce926ef2d6aba32f793afbc7a4b27d12983dbe17def7'}]},
                           'behavior_review': {'resilience': {'subject_key': 'e031e7428cbba27b282a92cb54a14defd78a671de08d9f48f3e300ae290ed9c1',
                                                              'events': [{'run_id': 21029115,
                                                                          'level': 22,
                                                                          'members': {'e031e7428cbba27b282a92cb54a14defd78a671de08d9f48f3e300ae290ed9c1': {'state': 'unknown',
                                                                                                                                                           'required_level': 23}}},
                                                                         {'run_id': 21036653,
                                                                          'level': 21,
                                                                          'members': {'e031e7428cbba27b282a92cb54a14defd78a671de08d9f48f3e300ae290ed9c1': {'state': 'unknown',
                                                                                                                                                           'required_level': 22}}},
                                                                         {'run_id': 21040182,
                                                                          'level': 21,
                                                                          'members': {'e031e7428cbba27b282a92cb54a14defd78a671de08d9f48f3e300ae290ed9c1': {'state': 'unknown',
                                                                                                                                                           'required_level': 22}}},
                                                                         {'run_id': 21063045,
                                                                          'level': 21,
                                                                          'members': {'e031e7428cbba27b282a92cb54a14defd78a671de08d9f48f3e300ae290ed9c1': {'state': 'unknown',
                                                                                                                                                           'required_level': 22}}},
                                                                         {'run_id': 21070615,
                                                                          'level': 20,
                                                                          'members': {'e031e7428cbba27b282a92cb54a14defd78a671de08d9f48f3e300ae290ed9c1': {'state': 'confirmed_before_start',
                                                                                                                                                           'required_level': 21}}},
                                                                         {'run_id': 21078170,
                                                                          'level': 20,
                                                                          'members': {'e031e7428cbba27b282a92cb54a14defd78a671de08d9f48f3e300ae290ed9c1': {'state': 'confirmed_before_start',
                                                                                                                                                           'required_level': 21}}},
                                                                         {'run_id': 21088094,
                                                                          'level': 21,
                                                                          'members': {'e031e7428cbba27b282a92cb54a14defd78a671de08d9f48f3e300ae290ed9c1': {'state': 'unknown',
                                                                                                                                                           'required_level': 22}}},
                                                                         {'run_id': 21094475,
                                                                          'level': 21,
                                                                          'members': {'e031e7428cbba27b282a92cb54a14defd78a671de08d9f48f3e300ae290ed9c1': {'state': 'unknown',
                                                                                                                                                           'required_level': 22}}},
                                                                         {'run_id': 21098254,
                                                                          'level': 10,
                                                                          'members': {'e031e7428cbba27b282a92cb54a14defd78a671de08d9f48f3e300ae290ed9c1': {'state': 'unknown',
                                                                                                                                                           'required_level': 11}}},
                                                                         {'run_id': 21122760,
                                                                          'level': 22,
                                                                          'members': {'e031e7428cbba27b282a92cb54a14defd78a671de08d9f48f3e300ae290ed9c1': {'state': 'unknown',
                                                                                                                                                           'required_level': 23}}}]}}}}},
 {'analysis_id': 34,
  'raw': '{"observations": [{"code": "recent_inquiry", "confidence": "低", "reason_id": "recent_clue", '
         '"limitation_ids": ["current_not_history", "unknown_motive"], "evidence_ids": '
         '["behavior:interpretation_limits"]}], "conclusion_id": "public_sample", "investigation": {"summary": '
         '"近期样本显示稳定的多人组合与轮换结构，部分场次的当事人在开打前已有同本更高限时记录，因此这些场次不能以本人后续进步解释成绩来源。但跑前保护状态多数未知，钥匙归属、库存与收费均无法确认，朋友间互助也与协助上分不互斥。坚韧逐场核验显示仅个别成员个别场次已确认跑前条件，其余为未知而非缺口。整体只能保持低置信调查，需补齐同本历史后才能比较协作与钥匙类解释，不据此给商业定性。", '
         '"confidence": "低", "hypotheses": [{"id": "assistance", "assessment": "possible", "evidence_ids": '
         '["behavior:interpretation_limits", "behavior:key_recovery_unknown:21545839", '
         '"behavior:key_recovery_unknown:21404204"], "counter_evidence_ids": [], "explanation": '
         '"涉及场次的当事人在开打前已有同本更高限时，本人当场无净收益，为同伴协助上分提供了具体线索。但升钥与库存解释同样保留，跑前保护未完整证实，钥匙归属与动机未知，故只能是可能的调查方向而非结论。"}, {"id": '
         '"routine_group", "assessment": "possible", "evidence_ids": ["behavior:interpretation_limits"], '
         '"counter_evidence_ids": [], "explanation": '
         '"稳定合作结构可以来自朋友间日常互助与练习，这与疑似协助并不互斥，不能作为协助解释的反证。当前缺乏收费或交易证据，也无法确认钥匙归属，因此日常协作解释保持可能但与协助线索并存待区分。"}, {"id": '
         '"being_carried", "assessment": "unknown", "evidence_ids": ["behavior:interpretation_limits"], '
         '"counter_evidence_ids": [], "explanation": '
         '"对低分成员是否获得队友协助，当前没有逐场受益比较的完整记录，坚韧状态多数未知也不得视为缺口。虽有带抓取快照低于跑前记录的低置信候选，但不保证每场净增分，该解释既无支持也无反证，保持未知。"}, {"id": '
         '"key_access", "assessment": "unknown", "evidence_ids": ["behavior:interpretation_limits"], '
         '"counter_evidence_ids": [], "explanation": '
         '"涉及场次的跑前保护状态未完整证实，已解锁或更高记录也不能证明本场钥匙归属或排除旧钥匙与他人钥匙。钥匙协调是否存在缺乏直接证据，仅能保持未知，后续应优先核验同本历史以缩小范围。"}], '
         '"next_question_ids": '
         '["scored_history:825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa:9526", '
         '"scored_history:825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa:9527", '
         '"scored_history:49417bb150de094673086e9707e6e108e54edb79f7528f69b79388f846c815b7:16865"]}}',
  'evidence': {'eligible_labels': [{'code': 'timed_experience',
                                    'label': '限时经历',
                                    'confidence_max': '高',
                                    'evidence_ids': ['timed_experience'],
                                    'reason_options': {'timed_record': '公开记录提供了可确认的限时经历。'},
                                    'limitation_options': {'sample_scope': '已知限时记录不能说明整个赛季的成功率或未公开经历。'},
                                    'required_limitation_ids': ['sample_scope']},
                                   {'code': 'rotating_core',
                                    'label': '近期固定组合与轮换成员',
                                    'confidence_max': '中',
                                    'evidence_ids': ['rotating_core'],
                                    'reason_options': {'core_roster': '已知限时阵容中存在重复共同出现的核心成员，其余成员组合发生变化。'},
                                    'limitation_options': {'other_groups': '朋友或临时组队也可能形成重复核心，不能据此确定组织关系或动机。',
                                                           'current_not_history': '当前同副本成绩比较不是历史当场加分；后续进步、朋友互助和练号仍是替代解释。',
                                                           'sample_scope': '仅代表已公开的限时阵容样本。'},
                                    'required_limitation_ids': ['other_groups']},
                                   {'code': 'recent_inquiry',
                                    'label': '近期协作线索（待解释）',
                                    'confidence_max': '低',
                                    'evidence_ids': ['behavior:interpretation_limits'],
                                    'reason_options': {'recent_clue': '已观测的成绩时序或成员关系形成具体调查线索，仍需比较协作与钥匙解释。'},
                                    'limitation_options': {'current_not_history': '当前成绩仅为代理，不代表历史当场加分。',
                                                           'unknown_motive': '协作、练习和后续进步仍可能；无法确认钥匙归属或交易。'},
                                    'required_limitation_ids': ['current_not_history', 'unknown_motive']}],
               'evidence': [{'id': 'timed_experience'}, {'id': 'behavior:prior_higher:21404204'},
                            {'id': 'behavior:key_recovery_unknown:21404204'}, {'id': 'behavior:prior_higher:21545839'},
                            {'id': 'behavior:lead:21545839'}, {'id': 'behavior:key_recovery_unknown:21545839'},
                            {'id': 'behavior:prior_higher:21563501'}, {'id': 'behavior:lead:21563501'},
                            {'id': 'behavior:carrying:21563501'}, {'id': 'rotating_core'},
                            {'id': 'behavior:interpretation_limits'}],
               'metrics': {'investigation': {'triggered': True,
                                             'hypotheses': [{'id': 'assistance',
                                                             'label': '疑似协助他人上分',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits',
                                                                             'behavior:key_recovery_unknown:21404204',
                                                                             'behavior:key_recovery_unknown:21545839']},
                                                            {'id': 'being_carried',
                                                             'label': '疑似获得队友协助',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits']},
                                                            {'id': 'joint_progress',
                                                             'label': '共同提高成绩线索',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits']},
                                                            {'id': 'key_access',
                                                             'label': '钥匙协调线索',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits']},
                                                            {'id': 'external_assistance',
                                                             'label': '高成绩协作者线索',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits']},
                                                            {'id': 'routine_group',
                                                             'label': '日常协作与朋友互助',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits']}],
                                             'questions': [{'id': 'scored_history:49417bb150de094673086e9707e6e108e54edb79f7528f69b79388f846c815b7:16865'},
                                                           {'id': 'scored_history:513161176f85b14a6963c8ff349c20f99bc4ee847bdbe14db43edeb99219ee75:9526'},
                                                           {'id': 'scored_history:825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa:9526'},
                                                           {'id': 'scored_history:825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa:9527'}]},
                           'behavior_review': {'resilience': {'subject_key': '825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa',
                                                              'events': [{'run_id': 21308927,
                                                                          'level': 23,
                                                                          'members': {'825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21316631,
                                                                          'level': 23,
                                                                          'members': {'825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21321760,
                                                                          'level': 23,
                                                                          'members': {'825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21331424,
                                                                          'level': 23,
                                                                          'members': {'825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21341484,
                                                                          'level': 24,
                                                                          'members': {'825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa': {'state': 'unknown',
                                                                                                                                                           'required_level': 25}}},
                                                                         {'run_id': 21404204,
                                                                          'level': 22,
                                                                          'members': {'825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa': {'state': 'unknown',
                                                                                                                                                           'required_level': 23}}},
                                                                         {'run_id': 21545839,
                                                                          'level': 23,
                                                                          'members': {'825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21551218,
                                                                          'level': 23,
                                                                          'members': {'825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21557491,
                                                                          'level': 23,
                                                                          'members': {'825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21563501,
                                                                          'level': 22,
                                                                          'members': {'825e6fc519fb10bc5f20d08f51144069aebbf99548305bdacbf7c2863dea97fa': {'state': 'confirmed_before_start',
                                                                                                                                                           'required_level': 23}}}]}}}}},
 {'analysis_id': 450,
  'raw': '{\n'
         '  "observations": [\n'
         '    {\n'
         '      "code": "recent_inquiry",\n'
         '      "confidence": "低",\n'
         '      "reason_id": "recent_clue",\n'
         '      "limitation_ids": ["current_not_history", "unknown_motive"],\n'
         '      "evidence_ids": ["behavior:interpretation_limits"]\n'
         '    }\n'
         '  ],\n'
         '  "conclusion_id": "public_sample",\n'
         '  "investigation": {\n'
         '    "summary": '
         '"样本显示近期稳定的同队完成记录，其中一场本人及一名队友已有同本更高限时，属于具体调查线索。跑前保护方面，仅该低层场次的两人已确认开打前具备所需钥匙层，其余成员状态未知，不得整体推断。钥匙归属、收益方向与协作动机均未知；朋友互助也可能同时构成协助。当前快照不能回填历史，需要补查相关成员同本成绩以比较协助与钥匙解释。",\n'
         '    "confidence": "低",\n'
         '    "hypotheses": [\n'
         '      {\n'
         '        "id": "key_access",\n'
         '        "assessment": "unknown",\n'
         '        "evidence_ids": ["behavior:interpretation_limits"],\n'
         '        "counter_evidence_ids": [],\n'
         '        "explanation": "低层场次的钥匙来源未知：本人已有同本更高限时削弱自然掉钥解释，但不排除旧库存或他人钥匙；其他成员跑前状态未知，无法确认钥匙由谁提供，故保持未知。"\n'
         '      },\n'
         '      {\n'
         '        "id": "assistance",\n'
         '        "assessment": "unknown",\n'
         '        "evidence_ids": ["behavior:interpretation_limits"],\n'
         '        "counter_evidence_ids": [],\n'
         '        "explanation": "稳定阵容与本人同本已有更高成绩之间存在协助他人的可能，但可比受益成员记录缺失，净收益无法确认；朋友互助也可属于协助，二者不互斥，证据尚不足以支持。"\n'
         '      },\n'
         '      {\n'
         '        "id": "routine_group",\n'
         '        "assessment": "possible",\n'
         '        "evidence_ids": ["behavior:interpretation_limits"],\n'
         '        "counter_evidence_ids": [],\n'
         '        "explanation": "阵容结构本身仅是背景，不证明带人；同队多次限时与日常协作或朋友互助相容，与协助解释也相容，作为低置信可能保留，等待成员成绩补查区分。"\n'
         '      },\n'
         '      {\n'
         '        "id": "being_carried",\n'
         '        "assessment": "unknown",\n'
         '        "evidence_ids": ["behavior:interpretation_limits"],\n'
         '        "counter_evidence_ids": [],\n'
         '        "explanation": "本人已有同本更高限时且部分场次跑前保护已确认，这削弱本人被动受助的解释；但完整历史与逐人收益未知，不能就此排除，故保持未知。"\n'
         '      }\n'
         '    ],\n'
         '    "next_question_ids": [\n'
         '      "scored_runs:573bb0f1050d5ca361644af503f94b93b485e95306b113ee97c47cef76c05853",\n'
         '      "scored_runs:684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7"\n'
         '    ]\n'
         '  }\n'
         '}',
  'evidence': {'eligible_labels': [{'code': 'timed_experience',
                                    'label': '限时经历',
                                    'confidence_max': '高',
                                    'evidence_ids': ['timed_experience'],
                                    'reason_options': {'timed_record': '公开记录提供了可确认的限时经历。'},
                                    'limitation_options': {'sample_scope': '已知限时记录不能说明整个赛季的成功率或未公开经历。'},
                                    'required_limitation_ids': ['sample_scope']},
                                   {'code': 'recent_inquiry',
                                    'label': '近期协作线索（待解释）',
                                    'confidence_max': '低',
                                    'evidence_ids': ['behavior:interpretation_limits'],
                                    'reason_options': {'recent_clue': '已观测的成绩时序或成员关系形成具体调查线索，仍需比较协作与钥匙解释。'},
                                    'limitation_options': {'current_not_history': '当前成绩仅为代理，不代表历史当场加分。',
                                                           'unknown_motive': '协作、练习和后续进步仍可能；无法确认钥匙归属或交易。'},
                                    'required_limitation_ids': ['current_not_history', 'unknown_motive']}],
               'evidence': [{'id': 'timed_experience'}, {'id': 'comparison:20753763'}, {'id': 'comparison:20908959'},
                            {'id': 'comparison:21419145'}, {'id': 'comparison:21443474'},
                            {'id': 'behavior:prior_higher:21501175'}, {'id': 'behavior:lead:21501175'},
                            {'id': 'behavior:key_recovery_unknown:21501175'},
                            {'id': 'behavior:interpretation_limits'}],
               'metrics': {'investigation': {'triggered': True,
                                             'hypotheses': [{'id': 'assistance',
                                                             'label': '疑似协助他人上分',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits',
                                                                             'behavior:key_recovery_unknown:21501175']},
                                                            {'id': 'being_carried',
                                                             'label': '疑似获得队友协助',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits']},
                                                            {'id': 'joint_progress',
                                                             'label': '共同提高成绩线索',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits']},
                                                            {'id': 'key_access',
                                                             'label': '钥匙协调线索',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits']},
                                                            {'id': 'external_assistance',
                                                             'label': '高成绩协作者线索',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits']},
                                                            {'id': 'routine_group',
                                                             'label': '日常协作与朋友互助',
                                                             'status': 'unknown',
                                                             'support_ids': [],
                                                             'counter_ids': [],
                                                             'unknown_ids': ['behavior:interpretation_limits']}],
                                             'questions': [{'id': 'scored_runs:573bb0f1050d5ca361644af503f94b93b485e95306b113ee97c47cef76c05853'},
                                                           {'id': 'scored_runs:684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7'}]},
                           'behavior_review': {'resilience': {'subject_key': '684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7',
                                                              'events': [{'run_id': 21419145,
                                                                          'level': 23,
                                                                          'members': {'684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21443474,
                                                                          'level': 23,
                                                                          'members': {'684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21462762,
                                                                          'level': 23,
                                                                          'members': {'684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21467280,
                                                                          'level': 23,
                                                                          'members': {'684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21473945,
                                                                          'level': 23,
                                                                          'members': {'684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21491216,
                                                                          'level': 23,
                                                                          'members': {'684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21497974,
                                                                          'level': 23,
                                                                          'members': {'684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21501175,
                                                                          'level': 22,
                                                                          'members': {'684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7': {'state': 'confirmed_before_start',
                                                                                                                                                           'required_level': 23}}},
                                                                         {'run_id': 21503699,
                                                                          'level': 23,
                                                                          'members': {'684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}},
                                                                         {'run_id': 21511880,
                                                                          'level': 23,
                                                                          'members': {'684cd2363abf9cfe8b977426bdfd4ff2de083c6910e890d3e7b19ae910c4e0a7': {'state': 'unknown',
                                                                                                                                                           'required_level': 24}}}]}}}}}]
