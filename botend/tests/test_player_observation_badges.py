"""榜单标签正向门槛、冻结投影和即时撤回的回归。"""
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
        self.assertEqual(observation.generate_observation(self.player, engine=Mock(send_message=choose))['status'], 'ready')
        self.players = [{'id': self.player.pk, 'rank': 1, 'character_name': self.player.character_name}]
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
                patch('requests.sessions.Session.request', side_effect=AssertionError('禁止联网')), \
                CaptureQueriesContext(connection) as queries:
            result = public_badge_players(self.players * 20, self.player.class_name, self.player.spec_name)
        self.assertEqual(len(queries), 5)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
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
                           {'manual_json': {'tags': []}}, {'revision': row.revision + 1},
                           {'last_error': 'error'}, {'status': 'pending'}):
                with self.subTest(change=change):
                    PlayerObservationAnalysis.objects.filter(pk=row.pk).update(
                        is_published=True, review_status='pending', manual_json={}, revision=row.revision,
                        last_error='', status='ready')
                    PlayerObservationAnalysis.objects.filter(pk=row.pk).update(**change)
                    result = SpecOverviewService.players(self.player.class_name, self.player.spec_name)
                    self.assertNotIn('observation_badges', result['players'][0])

    def test_expiry_identity_season_and_rank_fail_closed(self):
        guard = self.players[0]['_observation_guard']
        saved = guard['expires_at']
        guard['expires_at'] = (self.now - timedelta(seconds=1)).isoformat()
        with self.assertNumQueries(0):
            self.assertNotIn('observation_badges', self.read())
        guard['expires_at'] = saved
        for change in ({'character_name': '其他角色'}, {'rank': 21}, {'spec_name': 'Frost'}):
            with self.subTest(change=change):
                PlayerSpecTopPlayer.objects.filter(pk=self.player.pk).update(
                    character_name=self.player.character_name, rank=1, spec_name=self.player.spec_name)
                PlayerSpecTopPlayer.objects.filter(pk=self.player.pk).update(**change)
                self.assertNotIn('observation_badges', self.read())

    def test_roster_and_history_privacy_retract_badges(self):
        guard = self.players[0]['_observation_guard']
        roster_id = guard['source_stamps']['rosters'][0]['run_id']
        RaiderIORunRoster.objects.filter(run_id=roster_id).update(roster_visible=False)
        self.assertNotIn('observation_badges', self.read())
        RaiderIORunRoster.objects.filter(run_id=roster_id).update(roster_visible=True)
        self.assertTrue(self.read().get('observation_badges'))
        ref = guard['source_stamps']['histories'][0]
        activity = RaiderIOCharacterActivity.objects.get(pk=ref['activity_id'])
        activity.payload_json['mythic_plus_scored_history'][str(ref['dungeon_id'])]['private'] = True
        activity.save(update_fields=['payload_json'])
        self.assertNotIn('observation_badges', self.read())

    def test_legacy_and_unbound_badges_are_not_trusted(self):
        with self.assertNumQueries(0):
            result = public_badge_players([{'id': 7, 'observation_badges': [{'label': '无来源', 'confidence': '高'}]}], 'Mage', 'Fire')
        self.assertEqual(result, [{'id': 7}])

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
