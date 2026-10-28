"""Last-good榜单标签：冻结发布、轻量读取、人工保护与精确身份。"""
import copy
import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext

from botend.models import PlayerObservationAnalysis, RaiderIORunRoster, RaiderIOCharacterActivity, PlayerSpecTopPlayer
from botend.services import player_observation as observation
from botend.services.player_observation_badges import attach_badge_snapshots, public_badge_players, high_confidence_badges
from botend.services.spec_overview_service import SpecOverviewService
from botend.tests import test_player_four_plus_one as pipeline_fixtures
from botend.tests.test_player_observation import structured_response


class ObservationBadgeTests(TestCase):
    transport = pipeline_fixtures.FourPlusOnePipelineTests.transport
    refresh = pipeline_fixtures.FourPlusOnePipelineTests.refresh

    def setUp(self):
        pipeline_fixtures.FourPlusOnePipelineTests.setUp(self)
        self.refresh()
        def choose(prompt, **kwargs):
            data = json.loads(prompt)
            label = next(item for item in data['eligible_labels'] if item['code'] == 'carrying')
            return json.dumps({'observations': [dict(code='carrying', confidence='高',
                reason_id='repeated_witnesses', limitation_ids=label['required_limitation_ids'],
                evidence_ids=label['evidence_ids'])], 'conclusion_id': 'public_sample',
                'investigation': structured_response(data['metrics']['investigation']['response_contract'])})
        self.choose = choose
        self.assertEqual(observation.generate_observation(self.player, engine=Mock(send_message=choose))['status'], 'ready')
        self.players = [{'id': self.player.pk, 'rank': 1, **{field: getattr(self.player, field)
            for field in ('region', 'realm', 'character_name', 'profile_url')}}]
        attach_badge_snapshots(self.players)
        self.assertTrue(self.players[0].get('observation_badges'))

    def read(self):
        return public_badge_players(self.players, self.player.class_name, self.player.spec_name)[0]

    def test_strong_positive_only_and_manual_does_not_inherit_ai(self):
        def item(code, level, confidence, supported=True):
            return dict(code=code, value=code, supported=supported,
                evidence_strength=dict(level=level, confidence=confidence))
        view = dict(status='ready', source_kind='ai', display_judgements=[
            item('strong', 'strong', '高'), item('medium', 'partial', '中'),
            item('excluded', 'excluded', '高'), item('unknown', 'unverifiable', '低'),
            item('unsupported', 'strong', '高', False)])
        self.assertEqual([b['code'] for b in high_confidence_badges(view)], ['strong'])
        self.assertEqual(high_confidence_badges({**view, 'status': 'stale'}), [])
        self.assertEqual(high_confidence_badges({**view, 'source_kind': 'manual'}), [])

    def test_batch_projection_is_small_readonly_and_no_live_analysis(self):
        original = copy.deepcopy(self.players)
        with patch.object(observation, 'project_player_observation', side_effect=AssertionError('禁止现场分析')), \
                patch.object(observation, '_build', side_effect=AssertionError('禁止现场重建')), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('禁止联网')), \
                CaptureQueriesContext(connection) as queries:
            result = public_badge_players(self.players * 20, self.player.class_name, self.player.spec_name)
        self.assertLessEqual(len(queries), 3)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        # SQL may extract frozen label metadata, but never SELECT the evidence document.
        import re
        self.assertFalse(any(re.search(
            r'(?:SELECT |, )"wow_player_observation_analysis"\."evidence_json"(?:,| FROM)',
            q['sql']) for q in queries))
        self.assertTrue(all(row.get('observation_badges') for row in result))
        self.assertNotIn('_observation_guard', json.dumps(result))
        self.assertNotIn('source_stamps', json.dumps(result))
        self.assertLess(len(json.dumps(result[0])), 1800)
        self.assertEqual(self.players, original)

    def test_cached_leaderboard_hides_editorial_changes_immediately(self):
        row = PlayerObservationAnalysis.objects.get(activity=self.activity)
        with patch.object(SpecOverviewService, '_aggregate', return_value=({'players': self.players}, None)):
            self.assertTrue(SpecOverviewService.players(self.player.class_name, self.player.spec_name)['players'][0]['observation_badges'])
            for change in ({'is_published': False}, {'review_status': 'rejected'},
                           {'manual_json': {'tags': []}}):
                with self.subTest(change=change):
                    PlayerObservationAnalysis.objects.filter(pk=row.pk).update(
                        is_published=True, review_status='pending', manual_json={})
                    PlayerObservationAnalysis.objects.filter(pk=row.pk).update(**change)
                    result = SpecOverviewService.players(self.player.class_name, self.player.spec_name)
                    self.assertNotIn('observation_badges', result['players'][0])

    def test_expiry_and_source_drift_preserve_last_good(self):
        expected = self.read()['observation_badges']
        self.players[0]['_observation_guard']['expires_at'] = (self.now - timedelta(days=30)).isoformat()
        RaiderIOCharacterActivity.objects.update(fetched_at=self.now - timedelta(days=30),
            source_updated_at=self.now, module_fetched_at={}, last_error='source_failed', status='fetching')
        RaiderIORunRoster.objects.update(roster_visible=False, status='error')
        self.assertEqual(self.read()['observation_badges'], expected)
        attach_badge_snapshots(self.players)
        self.assertEqual(self.players[0]['observation_badges'], expected)

    def test_background_states_and_failure_keep_published_result(self):
        row = PlayerObservationAnalysis.objects.get(activity=self.activity)
        expected = self.read()['observation_badges']
        for state in ('queued', 'running', 'failed', 'blocked'):
            with self.subTest(state=state):
                PlayerObservationAnalysis.objects.filter(pk=row.pk).update(status='pending',
                    recheck_json={'state': state}, revision=row.revision + 1, last_error='recheck_failed')
                self.assertEqual(self.read()['observation_badges'], expected)
        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(lease_token='test-failure')
        observation._fail(row.pk, 'test-failure', 'engine_error')
        row.refresh_from_db()
        self.assertEqual(row.status, 'error')
        self.assertTrue(row.result_json)
        self.assertEqual(self.read()['observation_badges'], expected)

    def test_legacy_v5_frozen_labels_do_not_use_current_validator(self):
        row = PlayerObservationAnalysis.objects.get(activity=self.activity)
        evidence = copy.deepcopy(row.evidence_json)
        evidence['prompt_version'] = 5
        for label in evidence['eligible_labels']:
            label.pop('evidence_strength', None)
        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(prompt_version=5, evidence_json=evidence)
        with patch.object(observation, '_validate_frozen_publication', side_effect=AssertionError('旧发布不重验新契约')):
            self.assertEqual(self.read()['observation_badges'][0]['code'], 'carrying')
            attach_badge_snapshots(self.players)
            self.assertTrue(self.players[0]['observation_badges'])

    def test_neutral_observations_do_not_expand_badge_categories(self):
        row = PlayerObservationAnalysis.objects.get(activity=self.activity)
        evidence, result = copy.deepcopy(row.evidence_json), copy.deepcopy(row.result_json)
        evidence['eligible_labels'].append({'code': 'timed_experience', 'label': '限时经历', 'confidence_max': '高'})
        result['observations'].append({'code': 'timed_experience', 'confidence': '高'})
        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(evidence_json=evidence, result_json=result)
        self.assertNotIn('timed_experience', [b['code'] for b in self.read().get('observation_badges', [])])

    def test_identity_season_and_rank_fail_closed(self):
        for change in ({'character_name': '其他角色'}, {'rank': 21}, {'spec_name': 'Frost'}, {'region': 'eu'}):
            with self.subTest(change=change):
                PlayerSpecTopPlayer.objects.filter(pk=self.player.pk).update(
                    character_name=self.player.character_name, region=self.player.region,
                    rank=1, spec_name=self.player.spec_name)
                PlayerSpecTopPlayer.objects.filter(pk=self.player.pk).update(**change)
                self.assertNotIn('observation_badges', self.read())
        with patch('botend.services.spec_stats_service.SpecStatsService.get_active_season', return_value=None):
            self.assertNotIn('observation_badges', self.read())

    def test_activity_identity_drift_fails_closed(self):
        RaiderIOCharacterActivity.objects.filter(pk=self.activity.pk).update(character_name='another')
        self.assertNotIn('observation_badges', self.read())

    def test_old_empty_snapshot_is_hydrated_without_building_evidence(self):
        self.players[0].pop('observation_badges')
        self.players[0].pop('_observation_guard')
        with patch.object(observation, '_build', side_effect=AssertionError('禁止现场重建')):
            self.assertTrue(self.read().get('observation_badges'))
        self.players[0]['realm'] = 'another-realm'
        self.assertNotIn('observation_badges', self.read())

    def test_unbound_badges_are_not_trusted(self):
        result = public_badge_players([{'id': self.player.pk, 'observation_badges': [{'label': '无来源', 'confidence': '高'}]}],
            self.player.class_name, self.player.spec_name)
        self.assertEqual(result, [{'id': self.player.pk}])

    def test_low_confidence_or_missing_selected_label_never_promoted(self):
        row = PlayerObservationAnalysis.objects.get(activity=self.activity)
        original = copy.deepcopy(row.result_json)
        for confidence in ('低', '中', None):
            with self.subTest(confidence=confidence):
                result = copy.deepcopy(original)
                result['observations'][0]['confidence'] = confidence
                PlayerObservationAnalysis.objects.filter(pk=row.pk).update(result_json=result)
                self.assertNotIn('carrying', [b['code'] for b in self.read().get('observation_badges', [])])
        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(result_json=original)
        evidence = copy.deepcopy(row.evidence_json)
        for label in evidence['eligible_labels']:
            if label['code'] == 'carrying':
                label['evidence_strength']['confidence'] = '低'
        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(evidence_json=evidence)
        self.assertNotIn('carrying', [b['code'] for b in self.read().get('observation_badges', [])])

    def test_real_new_publication_replaces_old_cached_badges(self):
        self.assertTrue(self.read().get('observation_badges'))
        def no_labels(prompt, **kwargs):
            result = json.loads(self.choose(prompt, **kwargs))
            result['observations'] = []
            return json.dumps(result)
        self.assertEqual(observation.generate_observation(self.player, force=True,
            engine=Mock(send_message=no_labels))['status'], 'ready')
        self.assertNotIn('carrying', [b['code'] for b in self.read().get('observation_badges', [])])
        self.assertEqual(observation.generate_observation(self.player, force=True,
            engine=Mock(send_message=self.choose))['status'], 'ready')
        self.assertIn('carrying', [b['code'] for b in self.read().get('observation_badges', [])])

    def test_background_leaderboard_publishes_guard_and_clears_retracted_badges(self):
        from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor
        with patch('botend.services.spec_stats_service.SpecStatsService.get_player_list',
                   return_value={'players': self.players}), patch(
                'botend.controller.plugins.portal.SpecDetailAggregationMonitor.publish_stats') as publish:
            SpecDetailAggregationMonitor._aggregate_leaderboard(
                self.player.season_id, self.player.class_name, self.player.spec_name, '榜单测试目录')
            self.assertTrue(publish.call_args.args[1]['players'][0].get('_observation_guard'))
            PlayerObservationAnalysis.objects.filter(activity=self.activity).update(is_published=False)
            SpecDetailAggregationMonitor._aggregate_leaderboard(
                self.player.season_id, self.player.class_name, self.player.spec_name, '榜单测试目录')
            self.assertNotIn('observation_badges', publish.call_args.args[1]['players'][0])
            self.assertNotIn('_observation_guard', publish.call_args.args[1]['players'][0])
