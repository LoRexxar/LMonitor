"""证据等级的边界与未知动机隔离；纯本地测试，不请求外部数据。"""
from datetime import datetime, timezone
from unittest import TestCase

from botend.services.player_observation_strength import grade_review


class EvidenceStrengthTests(TestCase):
    def other(self, ids, days, code='joint_progress'):
        review = {'witnesses': {code: ids}, 'structure': {'recent_run_ids': []}}
        records = {rid: {'completed': datetime(2026, 10, day, tzinfo=timezone.utc)}
                   for rid, day in zip(set(ids), days)}
        return grade_review(review, {'events': [], 'candidates': [], 'questions': []}, {}, records)[code]

    def test_other_patterns_require_independent_runs_and_multiple_dates(self):
        self.assertEqual(self.other([1], [1])['level'], 'limited')
        self.assertEqual(self.other([1, 1], [1])['level'], 'limited')
        self.assertEqual(self.other([1, 2], [1, 2])['level'], 'partial')
        self.assertEqual(self.other([1, 2, 3], [1, 1, 1])['level'], 'partial')
        self.assertEqual(self.other([1, 2, 3], [1, 1, 2])['level'], 'strong')

    def test_key_ownership_inference_is_not_upgraded_by_repetition(self):
        self.assertEqual(self.other([1, 2, 3], [1, 1, 2], 'key_access')['level'], 'limited')

    def test_duplicate_guest_or_two_runs_cannot_reach_strong(self):
        core = ['a', 'b', 'c', 'd']
        teams = {1: set(core + ['x']), 2: set(core + ['y']), 3: set(core + ['x'])}
        records = {rid: {'timed': True} for rid in teams}
        review = {'witnesses': {}, 'structure': {'recent_run_ids': [1, 2, 3]}}
        events = [{'core': core, 'guest': 'x', 'run_id': 1}, {'core': core, 'guest': 'x', 'run_id': 3}]
        inquiry = {'events': events, 'candidates': events, 'questions': []}
        self.assertEqual(grade_review(review, inquiry, teams, records)['carrying']['level'], 'partial')
        events[1] = {'core': core, 'guest': 'y', 'run_id': 2}
        review['structure']['recent_run_ids'] = [1, 2]
        self.assertEqual(grade_review(review, inquiry, teams, records)['carrying']['level'], 'partial')
        review['structure']['recent_run_ids'] = [1, 2, 3]
        self.assertEqual(grade_review(review, inquiry, teams, records)['carrying']['level'], 'strong')
