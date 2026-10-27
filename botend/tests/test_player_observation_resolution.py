"""逐标签补证及否定边界回归，不依赖网络和业务数据库。"""
import copy
from datetime import timedelta
from unittest import TestCase

from botend.tests import test_player_observation_behavior as fixtures
from botend.services.player_observation_resolution import LABELS, render_label_reviews


def label_publication():
    """真实冻结校验器可读取的七标签发布样本，供接口和浏览器共同验证。"""
    from botend.tests.test_player_analysis_presentation import publication, TEXT
    from botend.tests.test_player_observation import structured_response
    from botend.services.player_observation import _hash, _merge_behavior_investigation
    from botend.services.player_observation_contract import build_contract
    row = publication()
    b = fixtures.BehaviorReviewTests()
    b.setUp()
    for key in b.keys[:5]:
        b.records(key, 23)
    review = b.build()
    for item in review['evidence']:
        item['text'] += ' ' + TEXT
    evidence = row.evidence_json
    evidence['prompt_version'] = row.prompt_version = 9
    plan = _merge_behavior_investigation({**evidence['metrics']['investigation'], 'evidence': []}, review)
    evidence['metrics'].update(behavior_review=review, investigation=plan)
    evidence['evidence'] = list({item['id']: item for item in
        evidence['evidence'] + review['evidence'] + plan['evidence']}.values())
    row.result_json['investigation'] = structured_response(build_contract(evidence))
    evidence.pop('evidence_hash', None)
    evidence['evidence_hash'] = row.evidence_hash = _hash(evidence)
    return row


class LabelResolutionTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.BehaviorReviewTests()
        self.fixture.setUp()
        self.b = self.fixture

    def questions(self, code):
        return [q for q in self.b.build()['questions'] if code in q.get('label_codes', [])]

    def resolution(self, code):
        return self.b.build()['label_reviews'][code]

    def test_collaborator_snapshot_gap_becomes_supported(self):
        b = self.b
        b.records(b.keys[0], 23)
        questions = self.questions('high_score_collaborator')
        self.assertEqual(len(questions), 4)
        self.assertTrue(all(q['purpose'] == 'verify_relative_score' for q in questions))
        for key in b.keys[1:4]:
            b.current_snapshot(key)
        self.assertEqual(self.resolution('high_score_collaborator')['status'], 'supported')
        self.assertEqual(self.questions('high_score_collaborator'), [])

    def test_three_runs_across_dates_raise_evidence_strength(self):
        b = self.b
        b.records(b.keys[0], 23)
        for key in b.keys[1:4]:
            b.current_snapshot(key)
        b.add_run(103, 22, b.now - timedelta(hours=36), b.keys[:5])
        self.assertEqual(b.build()['evidence_strengths']['high_score_collaborator']['level'], 'strong')

    def test_being_carried_snapshot_gap_becomes_supported(self):
        b = self.b
        for key in b.keys[1:4]:
            b.records(key, 23, all_dungeons=True)
        self.assertTrue(any(q['kind'] == 'scored_runs' for q in self.questions('being_carried')))
        b.current_snapshot(b.keys[0])
        self.assertEqual(self.resolution('being_carried')['status'], 'supported')
        self.assertEqual(self.questions('being_carried'), [])

    def test_later_high_score_requests_prior_timing_instead_of_claiming_support(self):
        b = self.b
        b.records(b.keys[0], 23, when=b.now - timedelta(hours=1))
        for key in b.keys[1:4]:
            b.current_snapshot(key)
        self.assertEqual(self.resolution('high_score_collaborator')['status'], 'unknown')
        self.assertTrue(all(q['purpose'] == 'verify_relative_history' for q in self.questions('high_score_collaborator')))
        self.assertTrue(self.questions('high_score_collaborator'))
        b.records(b.keys[0], 23)
        self.assertEqual(self.resolution('high_score_collaborator')['status'], 'supported')

    def test_positive_runs_prevent_whole_sample_exclusion(self):
        b = self.b
        b.records(b.keys[0], 23)
        for key in b.keys[1:4]:
            b.current_snapshot(key)
        b.add_run(103, 22, b.now - timedelta(hours=1), [b.keys[i] for i in (0, 3, 4, 5, 6)])
        item = self.resolution('high_score_collaborator')
        self.assertEqual(item['status'], 'supported')
        self.assertTrue(item['supported_run_ids'])
        self.assertTrue(item['unknown_run_ids'])

    def test_being_carried_only_requests_specific_protection_gap(self):
        b = self.b
        b.current_snapshot(b.keys[0])
        for key in b.keys[1:4]:
            b.records(key, 23, all_dungeons=True)
        missing = b.args['scores'][b.keys[3]].pop()
        questions = self.questions('being_carried')
        self.assertEqual([(q['purpose'], q['member_key'], q['dungeon_id'], q['required_level']) for q in questions],
                         [('resilience_gap', b.keys[3], 8, 23)])
        b.args['scores'][b.keys[3]].append(missing)
        self.assertEqual(self.resolution('being_carried')['status'], 'supported')

    def test_relative_reverse_records_exclude_only_complete_sample(self):
        b = self.b
        for key in b.keys[:5]:
            b.records(key, 23)
        for code in ('being_carried', 'high_score_collaborator'):
            item = self.resolution(code)
            self.assertEqual(item['status'], 'excluded')
            self.assertEqual(len(item['counter_ids']), 3)
            self.assertIn('可见跑前', item['scope'])
        b.args['rosters'].pop(100)
        self.assertNotEqual(self.resolution('being_carried')['status'], 'excluded')

    def test_dated_records_without_valid_current_ceiling_cannot_exclude(self):
        b = self.b
        for key in b.keys[:5]:
            b.records(key, 23)
        b.args['score_times'] = {key: b.now - timedelta(days=3) for key in b.keys[:5]}
        for code in ('being_carried', 'high_score_collaborator'):
            self.assertNotEqual(self.resolution(code)['status'], 'excluded')
        for key in b.keys[:5]:
            b.records(key, 22)
        # 本轮更高的已知记录与旧低分快照冲突，不能据此排除。
        self.assertNotEqual(self.resolution('high_score_collaborator')['status'], 'excluded')

    def test_future_or_missing_history_is_never_counterevidence(self):
        b = self.b
        for key in b.keys[:5]:
            b.records(key, 22, when=b.now - timedelta(hours=1))
        for code in ('being_carried', 'high_score_collaborator', 'joint_progress'):
            self.assertNotEqual(self.resolution(code)['status'], 'excluded')
        b.args['scores'].clear()
        self.assertEqual(self.resolution('joint_progress')['status'], 'unknown')

    def test_joint_history_gap_becomes_supported(self):
        b = self.b
        for key in b.keys[:2]:
            b.records(key, 21)[0]['keystone_run_id'] = 900
        questions = self.questions('joint_progress')
        self.assertEqual(len(questions), 3)
        self.assertTrue(all(q['purpose'] == 'verify_joint_history' and q['prior_run_ids'] == [900] for q in questions))
        for key in b.keys[2:4]:
            b.records(key, 21)[0]['keystone_run_id'] = 900
        self.assertEqual(self.resolution('joint_progress')['status'], 'supported')
        self.assertEqual(self.questions('joint_progress'), [])

    def test_joint_candidate_full_roster_can_exclude_candidate(self):
        b = self.b
        b.add_run(900, 21, b.now - timedelta(days=2), [b.keys[i] for i in (0, 1, 4, 5, 6)], score=500)
        b.args['recent'] = [r for r in b.args['recent'] if r['keystone_run_id'] != 900]
        item = self.resolution('joint_progress')
        self.assertEqual(item['status'], 'excluded')
        self.assertIn('候选', item['scope'])
        b.args['rosters'].pop(900)
        self.assertEqual(self.resolution('joint_progress')['status'], 'unknown')

    def test_shared_id_with_conflicting_time_does_not_authorize_joint_history(self):
        b = self.b
        b.records(b.keys[0], 21)[0]['keystone_run_id'] = 900
        b.records(b.keys[1], 21, when=b.now - timedelta(days=3))[0]['keystone_run_id'] = 900
        self.assertEqual(self.questions('joint_progress'), [])

    def test_empty_partial_history_closes_request_without_exclusion(self):
        b = self.b
        for key in b.keys[:2]:
            b.records(key, 21)[0]['keystone_run_id'] = 900
        b.args['histories'] = {key: {'1': dict(fetched_at=b.now.isoformat(),
            source_url='https://raider.io/api/characters/mythic-plus-runs',
            ui=dict(season='season-mn-2', characterId=77, role='all', mode='scored',
                    affixes='all', date='all', dungeonId=1), runs=[])} for key in b.keys[2:5]}
        self.assertEqual(self.questions('joint_progress'), [])
        self.assertEqual(self.resolution('joint_progress')['status'], 'unknown')

    def test_structure_has_positive_negative_and_missing_states(self):
        b = self.b
        self.assertEqual(self.resolution('fixed_team')['status'], 'supported')
        self.assertEqual(self.resolution('rotating_core')['status'], 'excluded')
        b.args['rosters'].pop(100)
        self.assertNotEqual(self.resolution('rotating_core')['status'], 'excluded')
        self.assertNotIn(100, self.resolution('fixed_team')['supported_run_ids'])

    def test_key_origin_is_explicitly_unverifiable_and_all_seven_are_projected(self):
        b = self.b
        b.records(b.keys[0], 23)
        for key in b.keys[1:5]:
            b.current_snapshot(key)
        review = b.build()
        self.assertNotIn('key_access', review['supported'])
        self.assertEqual(review['label_reviews']['key_access']['status'], 'unverifiable')
        self.assertEqual(self.questions('key_access'), [])
        evidence = {'metrics': {'behavior_review': review}, 'evidence': review['evidence']}
        frozen = copy.deepcopy(evidence)
        projected = render_label_reviews(evidence)
        self.assertEqual({row['code'] for row in projected}, set(LABELS))
        self.assertTrue(next(row for row in projected if row['code'] == 'rotating_core')['counter_evidence'])
        projected[0]['support_ids'].append('修改投影')
        self.assertEqual(evidence, frozen)

    def test_new_publication_displays_all_labels_and_rejects_tampering(self):
        from botend.services.player_analysis_management import frozen_ai
        from django.template.loader import render_to_string
        row = label_publication()
        ai = frozen_ai(row)
        self.assertEqual(ai['validation_status'], 'valid')
        self.assertEqual(len(ai['label_reviews']), 7)
        html = render_to_string('portal/spec_detail/_player_observation.html', {'observation': dict(
            status='ready', has_supported_judgements=True, label_reviews=ai['label_reviews'],
            source_kind='ai', investigation=ai['rendered_investigation'])})
        self.assertEqual(html.count('data-label-review="'), 7)
        self.assertIn('来源无法判定', html)
        self.assertIn('反向 3 场', html)
        self.assertNotIn('<img', html)
        row.evidence_json['metrics']['behavior_review']['label_reviews']['key_access']['status'] = 'supported'
        self.assertEqual(frozen_ai(row)['label_reviews'], [])

    def test_each_resolved_menu_direction_is_required_and_not_model_reversible(self):
        from botend.services.player_observation_contract import build_contract, validate_investigation, InvalidContract
        row = label_publication()
        menu = build_contract(row.evidence_json)
        for item in menu['hypotheses']:
            if item.get('evidence_strength', {}).get('level') != 'excluded':
                continue
            self.assertEqual(item['assessment_options'], ['weakened'])
            self.assertIn(item['id'], menu['required_hypothesis_ids'])
            changed = copy.deepcopy(row.result_json['investigation'])
            next(h for h in changed['hypotheses'] if h['id'] == item['id'])['assessment'] = 'supported'
            with self.assertRaises(InvalidContract):
                validate_investigation(changed, row.evidence_json)
