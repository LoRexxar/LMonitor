"""Frequency gate is shared by old frozen reads and new publication versions."""
from copy import deepcopy
from types import SimpleNamespace
import json
from django.test import SimpleTestCase
from botend.services import player_observation as service
from botend.services.player_observation_badges import _frozen_badges


def metadata_row():
    strength = {'level': 'strong', 'confidence': '高', 'label': '证据充分'}
    return {
        'badge_labels': [{'code': code, 'label': label, 'confidence_max': '高',
                          'evidence_strength': deepcopy(strength)}
                         for code, label in [('carrying', '4带1协助上分模式'),
                                             ('joint_progress', '共同提高成绩线索')]],
        'badge_observations': [{'code': code, 'confidence': '高'}
                               for code in ('carrying', 'joint_progress')],
        'badge_carrying_review': {
            'structure': {'recent_runs': 20, 'recent_run_ids': list(range(1, 21))},
            'witnesses': {'carrying': [1, 2]},
            'evidence_strengths': {'carrying': {**strength, 'verified_runs': 2,
                                               'distinct_beneficiaries': 2, 'core_runs': 3}},
        },
    }


class CarryingPublicationPolicyTests(SimpleTestCase):
    def test_old_strong_metadata_cannot_substitute_for_six_independent_events(self):
        row = metadata_row()
        self.assertEqual([b['code'] for b in _frozen_badges(row)], ['joint_progress'])
        # Frozen labels and evidence are never edited to implement read policy.
        self.assertEqual(row['badge_labels'][0]['evidence_strength']['level'], 'strong')

    def test_missing_frequency_never_inherits_a_historical_strong_label(self):
        row = metadata_row()
        row.pop('badge_carrying_review')
        self.assertEqual([b['code'] for b in _frozen_badges(row)], ['joint_progress'])

    def test_counts_without_saved_independent_proof_are_not_six_events(self):
        row = metadata_row()
        row['badge_carrying_review']['evidence_strengths']['carrying']['verified_runs'] = 99
        self.assertNotIn('carrying', [b['code'] for b in _frozen_badges(row)])

    def test_new_generation_binds_a_distinct_version_and_keeps_history_readable(self):
        self.assertEqual(service.PROMPT_VERSION, 10)
        self.assertEqual(service.READABLE_PUBLICATION_VERSIONS, frozenset({7, 8, 9, 10}))

    def test_summary_cannot_claim_six_without_six_saved_witnesses(self):
        from botend.services.player_observation_carrying_policy import build_frequency_summary, assess_carrying_frequency
        core = [f'{i:064x}' for i in range(1, 5)]
        events = [{'run_id': i, 'guest': f'{5 + i % 2:064x}'} for i in range(1, 7)]
        frequency = build_frequency_summary(selected_ids=range(1, 21), events=events,
            unknown_ids=range(7, 21), core_members=core, core_run_ids=range(1, 7))
        review = {'carrying_frequency': frequency,
                  'structure': {'recent_run_ids': list(range(1, 21))},
                  'witnesses': {'carrying': [1, 2]}}
        self.assertFalse(assess_carrying_frequency(review)['supported'])
        review['witnesses']['carrying'] = list(range(1, 7))
        self.assertTrue(assess_carrying_frequency(review)['supported'])
        review['structure']['recent_run_ids'] = [1, 2]
        self.assertFalse(assess_carrying_frequency(review)['supported'])
