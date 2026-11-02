"""Public details show confirmed labels, never candidate review surfaces."""
from copy import deepcopy
from django.template.loader import render_to_string
from django.test import SimpleTestCase


class ConfirmedObservationDisplayTests(SimpleTestCase):
    def test_only_confirmed_labels_and_their_frozen_evidence_are_public(self):
        strong = {'level': 'strong', 'confidence': '高', 'label': '证据充分'}
        confirmed = {'code': 'joint_progress', 'label': '行为线索', 'value': '确定的共同提高成绩',
                     'supported': True, 'evidence_strength': strong, 'reason': '确定标签原始依据',
                     'evidence': [{'text': '确定证据 A & B < C', 'url': 'https://example.com/proof'}]}
        uncertain = {'code': 'carrying', 'label': '尚未确定的标签', 'value': '未达门槛的协助事件',
                     'supported': True, 'carrying_event_only': True,
                     'evidence_strength': {'level': 'partial', 'confidence': '中'},
                     'reason': '不应公开的候选理由', 'evidence': []}
        view = {'status': 'ready', 'source_kind': 'ai', 'has_supported_judgements': True,
                'display_judgements': [confirmed, uncertain],
                'carrying_frequency': {'supported': False, 'verified_runs': 5, 'observed_runs': 20},
                'label_reviews': [
                    {'code': 'joint_progress', 'label': '确定的共同提高成绩', 'strength': strong,
                     'reason': '确定标签核验依据', 'counter_evidence': [{'text': '确定标签的完整反证'}]},
                    {'code': 'carrying', 'label': '尚未确定的标签', 'status': 'partial'}],
                'investigation': {'triggered': True, 'summary': '未确定的综合推测',
                                  'hypotheses': [{'label': '未确定候选解释', 'assessment': 'possible'}]}}
        before = deepcopy(view)
        for status in ('ready', 'stale'):
            with self.subTest(status=status):
                html = render_to_string('portal/spec_detail/_player_observation.html',
                                        {'observation': {**view, 'status': status}})
                for text in ('确定的共同提高成绩', '确定标签原始依据', '确定标签核验依据',
                             '确定标签的完整反证', 'A &amp; B &lt; C', 'https://example.com/proof'):
                    self.assertIn(text, html)
                for text in ('尚未确定的标签', '未达门槛的协助事件', '不应公开的候选理由',
                             '未确定的综合推测', '未确定候选解释', 'data-player-investigation',
                             'data-carrying-frequency', '查看全部标签的核验进度'):
                    self.assertNotIn(text, html)
        only_uncertain = {**view, 'display_judgements': [uncertain]}
        self.assertNotIn('id="player-observation"', render_to_string(
            'portal/spec_detail/_player_observation.html', {'observation': only_uncertain}))
        self.assertEqual(view, before, 'display filtering must not rewrite the source DTO or evidence')
