"""Model-free public rendering of current carrying policy over frozen projections."""
from copy import deepcopy

from django.template.loader import render_to_string
from django.test import SimpleTestCase


EVENT_LABEL = '协助上分事件线索（未达模式门槛）'


def render(view):
    return render_to_string('portal/spec_detail/_player_observation.html', {'observation': view})


def frequency(verified=5, **extra):
    return {'supported': verified >= 6, 'verified_runs': verified, 'observed_runs': 10,
            'unknown_runs': 3, 'distinct_beneficiaries': 2, 'window_days': 14,
            'sample_limit': 20, 'min_verified_runs': 6, 'legacy': False, **extra}


def projection():
    proof = [{'id': 'behavior:carrying:123', 'text': '本场四人跑前能力与第五人提升已核实 <script>bad()</script>',
              'run_url': 'https://raider.io/mythic-plus-runs/season-mn-2/123'}]
    strong = {'level': 'strong', 'label': '证据充分', 'confidence': '高', 'verified_runs': 2}
    other = {'code': 'joint_progress', 'label': '共同提高成绩线索', 'value': '共同提高成绩线索 · 证据充分',
             'supported': True, 'reason': '共同提高的原依据', 'evidence_strength': deepcopy(strong), 'evidence': []}
    return {'status': 'stale', 'stale_reason': 'source_changed', 'source_kind': 'ai',
            'has_supported_judgements': True,
            'display_judgements': [{'code': 'carrying', 'label': '行为线索', 'value': '4带1模式 · 证据充分',
                                   'supported': True, 'reason': '旧三场两人即可充分',
                                   'evidence_strength': deepcopy(strong), 'evidence': deepcopy(proof)}, deepcopy(other)],
            'observations': [{'code': 'carrying', 'label': '4带1模式', 'confidence': '高',
                              'evidence_strength': deepcopy(strong), 'reason': '旧强判断', 'evidence': deepcopy(proof)}, deepcopy(other)],
            'label_reviews': [{'code': 'carrying', 'label': '4带1模式', 'status': 'supported',
                               'strength': deepcopy(strong), 'reason': '旧三场两人即可充分',
                               'supported_run_ids': [123, 124], 'excluded_run_ids': [], 'unknown_run_ids': [125],
                               'evidence': deepcopy(proof), 'counter_evidence': [], 'questions': []}, deepcopy(other)],
            'investigation': {'schema_version': 9, 'triggered': True, 'summary': '旧4带1模式已证据充分',
                              'hypotheses': [{'id': 'assistance', 'label': '4带1协助上分模式', 'assessment': 'supported',
                                              'assessment_label': '证据充分', 'evidence_strength': deepcopy(strong),
                                              'explanation': '旧三场两人即可充分', 'summary': '旧4带1模式已证据充分',
                                              'evidence': deepcopy(proof), 'counter_evidence': [], 'unknown_evidence': []},
                                             {'id': 'joint_progress', 'label': '共同提高成绩线索', 'assessment': 'supported',
                                              'explanation': '共同提高的原依据', 'evidence_strength': deepcopy(strong), 'evidence': []}]},
            'evidence_strength_rules': {'carrying': ['旧三场两人即可充分'], 'other': ['其他规则不变']}}


class CarryingFrequencyDisplayTests(SimpleTestCase):
    def apply(self, view, summary):
        from botend.services.player_observation_carrying_display import apply_carrying_frequency_policy
        return apply_carrying_frequency_policy(view, summary)

    def test_template_renders_frequency_without_any_other_judgement(self):
        html = render({'status': 'ready', 'source_kind': 'ai', 'carrying_frequency': frequency(),
                       'has_supported_judgements': False})
        self.assertIn('data-carrying-frequency', html)
        self.assertIn('近14天', html)
        self.assertIn('最多20场', html)
        self.assertIn('5/10', html)
        self.assertIn('未知 3 场', html)
        self.assertIn('未知仍计入分母', html)
        self.assertIn('未公开场次不包含在本样本', html)

    def test_five_runs_downgrades_every_carrying_surface_preserves_facts_and_others(self):
        original = projection()
        frozen = deepcopy(original)
        view = self.apply(deepcopy(original), frequency())
        for field in ('display_judgements', 'observations', 'label_reviews'):
            self.assertEqual(view[field][1], original[field][1])
            self.assertEqual(view[field][0]['evidence'], original[field][0]['evidence'])
        self.assertEqual(original, frozen)
        self.assertEqual(view['display_judgements'][0]['value'], EVENT_LABEL)
        self.assertNotEqual(view['label_reviews'][0]['status'], 'supported')
        for grade in (view['display_judgements'][0]['evidence_strength'], view['observations'][0]['evidence_strength'],
                      view['label_reviews'][0]['strength'], view['investigation']['hypotheses'][0]['evidence_strength']):
            self.assertEqual(grade['level'], 'partial')
            self.assertNotEqual(grade['label'], '证据充分')
        self.assertEqual(view['observations'][0]['label'], EVENT_LABEL)
        hypothesis = view['investigation']['hypotheses'][0]
        self.assertNotEqual(hypothesis['assessment'], 'supported')
        self.assertNotEqual(hypothesis['assessment_label'], '证据充分')
        self.assertNotIn('旧4带1', hypothesis['summary'])
        self.assertEqual(hypothesis['evidence'], original['investigation']['hypotheses'][0]['evidence'])
        self.assertEqual(view['investigation']['hypotheses'][1], original['investigation']['hypotheses'][1])
        from botend.services.player_observation_strength import RULES
        self.assertEqual(view['evidence_strength_rules']['carrying'], RULES['carrying'])
        self.assertEqual(view['evidence_strength_rules']['other'], ['其他规则不变'])
        html = render(view)
        self.assertIn(EVENT_LABEL, html)
        self.assertIn('5/10', html)
        self.assertIn('资料已更新', html)
        self.assertIn('共同提高的原依据', html)
        self.assertNotIn('旧三场两人即可充分', html)
        self.assertNotIn('旧4带1模式已证据充分', html)
        self.assertNotIn('4带1模式 · 证据充分', html)
        self.assertIn('&lt;script&gt;bad()&lt;/script&gt;', html)
        self.assertNotIn('<script>', html)

    def test_six_runs_retains_current_carrying_reason_and_evidence(self):
        original = projection()
        view = self.apply(deepcopy(original), frequency(6))
        for field in ('display_judgements', 'observations', 'label_reviews', 'investigation'):
            self.assertEqual(view[field], original[field])
        html = render(view)
        self.assertIn('6/10', html)
        self.assertIn('4带1模式 · 证据充分', html)

    def test_old_saved_two_are_lower_bound_not_a_complete_event_count(self):
        view = self.apply(projection(), frequency(2, legacy=True, supported=False,
                                                 unknown_runs=None, distinct_beneficiaries=None))
        html = render(view)
        self.assertIn('已保存代表事件 2/10', html)
        self.assertIn('旧结果仅保存代表事件，无法据此确认达到6场', html)
        self.assertIn('未保存，无法精确回溯', html)
        self.assertNotIn('已核实协助 2/10', html)
        self.assertNotIn('仅2场', html)
        self.assertNotIn('4带1模式 · 证据充分', html)

    def test_missing_summary_and_no_proof_does_not_invent_event_or_zero_unknown(self):
        view = projection()
        for field in ('display_judgements', 'observations', 'label_reviews'):
            view[field] = [item for item in view[field] if item.get('code') != 'carrying']
        view['investigation']['hypotheses'] = []
        view = self.apply(view, {})
        html = render(view)
        self.assertIn('未保存，无法精确回溯', html)
        self.assertNotIn('data-judgement="carrying"', html)
        self.assertNotIn('未知 0 场', html)
        self.assertNotIn(EVENT_LABEL, html)

    def test_invalid_frequency_is_unknown_not_zero_and_is_explained(self):
        from botend.services.player_observation_carrying_policy import assess_carrying_frequency
        for raw in (None, {'verified_runs': 6}, []):
            with self.subTest(raw=raw):
                summary = assess_carrying_frequency({'carrying_frequency': raw})
                view = self.apply(projection(), summary)
                self.assertIsNone(view['carrying_frequency']['verified_runs'])
                self.assertFalse(view['carrying_frequency']['supported'])
                html = render(view)
                self.assertNotIn('已核实协助 0/', html)
                self.assertIn('频率元数据无效，无法精确回溯', html)

    def test_claimed_support_without_six_runs_or_two_beneficiaries_fails_closed(self):
        for summary in (frequency(5, supported=True), frequency(6, distinct_beneficiaries=1),
                        frequency(6, legacy=True), frequency(6, invalid=True)):
            with self.subTest(summary=summary):
                view = self.apply(projection(), summary)
                self.assertEqual(view['display_judgements'][0]['value'], EVENT_LABEL)

    def test_hidden_manual_or_unpublished_cannot_be_revived(self):
        for updates in ({'status': 'hidden'}, {'status': 'pending'}, {'status': 'error'},
                        {'status': 'archived'}, {'source_kind': 'manual'}, {'is_published': False},
                        {'review_status': 'rejected'}):
            with self.subTest(updates=updates):
                view = {**projection(), **updates}
                before = deepcopy(view)
                self.assertIs(self.apply(view, frequency()), view)
                self.assertEqual(view, before)
                self.assertNotIn('data-carrying-frequency', render(view))

    def test_summary_without_assistance_row_cannot_repeat_old_carrying_claim(self):
        view = projection()
        view['investigation']['hypotheses'] = view['investigation']['hypotheses'][1:]
        view = self.apply(view, frequency())
        self.assertNotIn('旧4带1模式已证据充分', render(view))
        self.assertEqual(view['investigation']['hypotheses'][0]['explanation'], '共同提高的原依据')

    def test_generic_assistance_signal_is_not_promoted_to_verified_event(self):
        view = projection()
        for key in ('display_judgements', 'observations', 'label_reviews'):
            view[key] = [row for row in view[key] if row.get('code') != 'carrying']
        view['investigation']['hypotheses'][0]['evidence'] = [
            {'id': 'investigation:low:123', 'text': '重复低层同场线索，不是受益事件'}]
        view = self.apply(view, frequency(0))
        self.assertNotIn(EVENT_LABEL, render(view))
        self.assertEqual(view['investigation']['hypotheses'][0]['evidence_strength']['level'], 'insufficient')

    def test_event_references_are_expandable_without_inventing_an_ai_observation(self):
        view = {'status': 'ready', 'source_kind': 'ai', 'has_supported_judgements': False,
                'display_judgements': [], 'observations': [],
                'carrying_frequency_evidence': [{'id': 'behavior:carrying:456',
                    'text': '跑前能力与受益提升的原事实 <script>bad()</script>',
                    'run_url': 'https://raider.io/mythic-plus-runs/season-mn-2/456'}]}
        view = self.apply(view, frequency(1))
        html = render(view)
        self.assertEqual(view['observations'], [])
        self.assertEqual(view['display_judgements'], [])
        self.assertIn('查看已核实事件证据', html)
        self.assertIn('跑前能力与受益提升的原事实', html)
        self.assertIn('&lt;script&gt;bad()&lt;/script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertIn('1/10', html)
        self.assertNotIn('data-judgement="carrying"', html)

    def test_projection_aliases_do_not_mutate_frozen_strengths_or_investigation(self):
        view = projection()
        frozen = {'observations': view['observations'], 'investigation': view['investigation'],
                  'label_reviews': view['label_reviews'], 'evidence_strength_rules': view['evidence_strength_rules']}
        before = deepcopy(frozen)
        self.apply(view, frequency())
        self.assertEqual(frozen, before)

    def test_rendered_counts_never_trust_html_strings(self):
        view = self.apply(projection(), frequency(5, observed_runs='<img src=x>', unknown_runs='<script>x</script>'))
        html = render(view)
        self.assertNotIn('<img', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('5/0', html)
