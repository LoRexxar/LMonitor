"""Central last-good publication: detail reasons and badge withdrawal agree."""
import copy
import threading
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

import requests

from botend.services.player_observation_history import MODULE, URL, collect_history, scope

from django.db import connection
from django.template.loader import render_to_string
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils.html import escape

from botend.models import PlayerObservationAnalysis, PlayerSpecTopPlayer, RaiderIORunRoster
from botend.services import player_observation as service
from botend.services.player_observation_badges import public_badge_players
from botend.tests import test_player_observation_badges as fixtures


class PublicationDetailTests(TestCase):
    transport = fixtures.ObservationBadgeTests.transport
    refresh = fixtures.ObservationBadgeTests.refresh

    def setUp(self):
        fixtures.ObservationBadgeTests.setUp(self)
        self.row = PlayerObservationAnalysis.objects.get(activity=self.activity)
        self.frozen = service.project_player_observation(self.player)
        self.assertEqual(self.frozen['status'], 'ready')
        for target in ('requests.sessions.Session.request',
                       'botend.services.player_observation.PlayerObservationGLMClient'):
            guard = patch(target, side_effect=AssertionError('read must not fetch or call AI'))
            guard.start()
            self.addCleanup(guard.stop)

    def badges(self):
        return public_badge_players(self.players, self.player.class_name, self.player.spec_name)[0].get('observation_badges', [])

    def change_source(self):
        self.activity.payload_json['mythic_plus_dungeon_run_counts'] = [{'season_runs_total': 900, 'season_runs_timed': 800}]
        self.activity.save(update_fields=['payload_json'])

    def assert_frozen(self, view):
        self.assertEqual(view['analysis_id'], self.row.pk)
        self.assertEqual(view['generated_at'], self.row.generated_at)
        for field in ('observations', 'display_judgements', 'conclusion', 'coverage',
                      'season_counts', 'source_stamps', 'limitations'):
            self.assertEqual(view[field], self.frozen[field], field)
        self.assertTrue(view['has_supported_judgements'])
        html = render_to_string('portal/spec_detail/_player_observation.html', {'observation': view})
        # Render the real consumer, not a duplicate test template.
        for judgement in view['display_judgements']:
            self.assertIn(escape(judgement['reason']), html)
            for evidence in judgement['evidence']:
                self.assertIn(escape(evidence['text']), html)

    def test_changed_source_keeps_exact_published_reasons_and_marks_history(self):
        expected_badges = self.badges()
        self.change_source()
        with CaptureQueriesContext(connection) as queries:
            view = service.project_player_observation(self.player)
        self.assertEqual(view['status'], 'stale')
        self.assertIs(view['publication_current'], False)
        self.assertEqual(view['stale_reason'], 'source_changed')
        self.assert_frozen(view)
        self.assertEqual(self.badges(), expected_badges)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))

    def test_same_facts_and_forward_clock_remain_current(self):
        for advance in (False, True):
            if advance:
                self.activity.fetched_at += timedelta(minutes=1)
                self.activity.save(update_fields=['fetched_at'])
            with patch('django.utils.timezone.now', return_value=self.now + timedelta(minutes=2)):
                view = service.project_player_observation(self.player)
            self.assertEqual(view['status'], 'ready')
            self.assertIs(view['publication_current'], True)
            self.assertEqual(view['stale_reason'], '')
            self.assert_frozen(view)

    def test_generation_and_source_failures_keep_last_good_without_claiming_current(self):
        for change in ({'status': 'fetching'}, {'status': 'error', 'last_error': 'engine_error'}):
            PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(**change)
            view = service.project_player_observation(self.player)
            self.assert_frozen(view)
            self.assertTrue(self.badges())
        self.activity.status = 'error'
        self.activity.last_error = 'transport_error'
        self.activity.fetched_at -= timedelta(days=30)
        self.activity.save()
        view = service.project_player_observation(self.player)
        self.assertEqual(view['status'], 'stale')
        self.assertIs(view['publication_current'], False)
        self.assertEqual(view['stale_reason'], 'source_failed')
        self.assert_frozen(view)
        self.assertTrue(self.badges())

    def test_current_builder_exception_keeps_history_but_never_ready(self):
        with patch.object(service, '_build', side_effect=ValueError('bad current data')):
            view = service.project_player_observation(self.player)
        self.assertEqual(view['status'], 'stale')
        self.assertIs(view['publication_current'], False)
        self.assertEqual(view['stale_reason'], 'source_failed')
        self.assert_frozen(view)

    def test_confirmed_roster_withdrawal_retracts_both_and_cannot_revive_on_error(self):
        rid = self.row.evidence_json['source_stamps']['rosters'][0]['run_id']
        for status in ('unavailable', 'error', 'fetching'):
            RaiderIORunRoster.objects.filter(rio_season=self.season.rio_season, run_id=rid).update(roster_visible=False, status=status)
            view = service.project_player_observation(self.player)
            self.assertFalse(view and view.get('display_judgements'))
            self.assertEqual(self.badges(), [])

    def collect_frozen_history(self, ref, *, at, response=None, error=None):
        activity = type(self.activity).objects.get(pk=ref['activity_id'])
        cid = int(parse_qs(urlsplit(ref['source_url']).query)['characterId'][0])
        character = {'character_id': cid, 'identity': {key: getattr(activity, key)
            for key in ('region', 'realm_slug', 'character_name')}}
        batch = SimpleNamespace(stop_event=threading.Event(), requests=0, budget=1,
            get=Mock(return_value=response, side_effect=error))
        with patch('django.utils.timezone.now', return_value=at):
            result = collect_history(batch, character, activity.season, ref['dungeon_id'])
        batch.get.assert_called_once_with(URL, params=scope(activity.season.rio_season,
            cid, ref['dungeon_id']), headers={'User-Agent': 'Mozilla/5.0'})
        activity.refresh_from_db()
        return result, activity.payload_json[MODULE][str(ref['dungeon_id'])]

    def test_collector_private_history_withdraws_both_stays_sticky_and_public_restores(self):
        ref = self.row.evidence_json['source_stamps']['histories'][0]
        at = self.now + timedelta(days=3)
        responses = [
            (Mock(status_code=403, json=Mock(return_value={'message': 'Character Profile is Private'})), None, 'private'),
            (Mock(status_code=503, headers={}), None, 'http_503'),
            (None, requests.ConnectionError('offline'), 'transport_error'),
        ]
        for response, error, expected in responses:
            result, entry = self.collect_frozen_history(ref, at=at, response=response, error=error)
            self.assertEqual(result['status'], expected)
            self.assertIs(entry['private'], True)
            with patch.object(service, '_build', side_effect=AssertionError('withdraw before evidence rebuild')):
                view = service.project_player_observation(self.player)
                self.assertFalse(view.get('display_judgements'))
                self.assertEqual(view['observations'], [])
                self.assertEqual(view['source_stamps'], {})
                self.assertFalse(view.get('investigation'))
                self.assertEqual(self.badges(), [])
            at += timedelta(hours=1)
        cid = int(parse_qs(urlsplit(ref['source_url']).query)['characterId'][0])
        response = self.transport(URL, params=scope(self.season.rio_season, cid, ref['dungeon_id']))
        result, entry = self.collect_frozen_history(ref, at=at, response=response)
        self.assertEqual(result['status'], 'ready')
        self.assertNotIn('private', entry)
        self.assert_frozen(service.project_player_observation(self.player))
        self.assertTrue(self.badges())

    def test_collector_ordinary_history_errors_preserve_frozen_publication(self):
        ref = self.row.evidence_json['source_stamps']['histories'][0]
        at = self.now + timedelta(days=3)
        for response, error in ((Mock(status_code=403, json=Mock(return_value={})), None),
                                (Mock(status_code=500, headers={}), None),
                                (None, requests.ConnectionError('offline'))):
            result, entry = self.collect_frozen_history(ref, at=at, response=response, error=error)
            self.assertIn(result['status'], ('http_403', 'http_500', 'transport_error'))
            self.assertFalse(entry.get('private'))
            view = service.project_player_observation(self.player)
            self.assertEqual(view['stale_reason'], 'source_failed')
            self.assert_frozen(view)
            self.assertTrue(self.badges())
            at += timedelta(hours=1)

    def test_unreferenced_private_histories_do_not_withdraw_and_scope_is_exact(self):
        from urllib.parse import urlencode
        original = self.row.evidence_json['source_stamps']['histories'][0]
        cid = int(parse_qs(urlsplit(original['source_url']).query)['characterId'][0])
        other_season = type(self.season).objects.create(season_key='other-history',
            season_name='Other history', rio_season='season-mn-1',
            mplus_zone_id=3, raid_zone_id=4, is_active=False)
        other_activity = type(self.activity).objects.create(season=self.season,
            region='cn', realm_slug='other-history', character_name='Other history')
        foreign_activity = type(self.activity).objects.create(season=other_season,
            region='cn', realm_slug='other-history', character_name='Other history')
        unrelated = []
        for activity_id, season, did in (
                (original['activity_id'], self.season.rio_season, 999999),
                (other_activity.pk, self.season.rio_season, original['dungeon_id']),
                (foreign_activity.pk, other_season.rio_season, original['dungeon_id'])):
            ref = {**original, 'activity_id': activity_id, 'dungeon_id': did,
                   'source_url': URL + '?' + urlencode(scope(season, cid, did))}
            result, entry = self.collect_frozen_history(ref, at=self.now + timedelta(days=3),
                response=Mock(status_code=403,
                    json=Mock(return_value={'message': 'Character Profile is Private'})))
            self.assertEqual(result['status'], 'private')
            self.assertIs(entry['private'], True)
            unrelated.append(ref)
        self.assert_frozen(service.project_player_observation(self.player))
        self.assertTrue(self.badges())
        # One batch can reference the same activity under different scopes. A
        # private dependency in one publication must not poison its neighbours.
        private = unrelated[0]
        wrong_character = {**private, 'source_url': URL + '?' + urlencode(
            scope(self.season.rio_season, cid + 1, private['dungeon_id']))}
        wrong_season = {**private, 'source_url': URL + '?' + urlencode(
            scope(other_season.rio_season, cid, private['dungeon_id']))}
        publications = [
            (1, self.season.rio_season, [], [original]),
            (2, self.season.rio_season, [], [private]),
            (3, self.season.rio_season, [], [wrong_character]),
            (4, other_season.rio_season, [], [wrong_season]),
            (5, self.season.rio_season, [], [{**private, 'activity_id': other_activity.pk}]),
        ]
        self.assertEqual(service.withdrawn_roster_publications(publications), {2})

    def test_privacy_metadata_query_is_flat_and_never_hydrates_evidence(self):
        import re
        rid = self.row.evidence_json['source_stamps']['rosters'][0]['run_id']
        for visible in (True, False):
            RaiderIORunRoster.objects.filter(rio_season=self.season.rio_season, run_id=rid).update(roster_visible=visible)
            counts = []
            for size in (1, 20):
                with patch.object(service, '_build', side_effect=AssertionError('no live evidence')), CaptureQueriesContext(connection) as queries:
                    result = public_badge_players(self.players * size, self.player.class_name, self.player.spec_name)
                counts.append(len(queries))
                self.assertLessEqual(len(queries), 5)
                self.assertTrue(all(bool(item.get('observation_badges')) == visible for item in result))
                roster_queries = [q['sql'] for q in queries if 'wow_raiderio_run_roster' in q['sql']]
                self.assertEqual(len(roster_queries), 1)
                self.assertNotIn('payload_json', roster_queries[0])
                history_queries = [q['sql'] for q in queries if 'mythic_plus_scored_history' in q['sql']]
                self.assertEqual(len(history_queries), 1)
                self.assertIn('JSON_EXTRACT', history_queries[0])
                self.assertNotIn('[9527]', history_queries[0])
                self.assertNotIn('[16359]', history_queries[0])
                for field in ('private', 'status', 'ui'):
                    self.assertIn(field, history_queries[0])
                self.assertNotIn('."runs"', history_queries[0])
                self.assertFalse(any(re.search(
                    r'(?:SELECT |, )"wow_raiderio_character_activity"\."payload_json"(?:,| FROM)',
                    q['sql']) for q in queries))
                self.assertFalse(any(re.search(
                    r'(?:SELECT |, )"wow_player_observation_analysis"\."evidence_json"(?:,| FROM)', q['sql']) for q in queries))
            self.assertEqual(counts[0], counts[1])

    def test_public_roster_transport_error_preserves_history(self):
        rid = self.row.evidence_json['source_stamps']['rosters'][0]['run_id']
        RaiderIORunRoster.objects.filter(rio_season=self.season.rio_season, run_id=rid).update(status='error', last_error='transport_error')
        view = service.project_player_observation(self.player)
        self.assertEqual(view['status'], 'stale')
        self.assertEqual(view['stale_reason'], 'source_failed')
        self.assert_frozen(view)
        self.assertTrue(self.badges())

    def test_unrelated_roster_and_independent_raid_privacy_do_not_withdraw_mplus(self):
        RaiderIORunRoster.objects.create(rio_season=self.season.rio_season, run_id=999999999, roster_visible=False, status='unavailable')
        self.activity.payload_json['raid_first_kills'] = {'status': 'private'}
        self.activity.save(update_fields=['payload_json'])
        self.assert_frozen(service.project_player_observation(self.player))
        self.assertTrue(self.badges())

    def test_editorial_overrides_and_invalid_publication_fail_closed(self):
        original = {field: copy.deepcopy(getattr(self.row, field)) for field in
                    ('is_published', 'review_status', 'manual_json', 'prompt_version', 'evidence_hash', 'evidence_json', 'generated_at', 'result_json')}
        invalid_scope = copy.deepcopy(self.row.evidence_json)
        invalid_scope['activity_id'] += 10000
        invalid_scope['evidence_hash'] = service._hash({k: v for k, v in invalid_scope.items() if k not in ('evidence_hash', 'source_failed')})
        invalid_season = copy.deepcopy(self.row.evidence_json)
        invalid_season['season'] = 'another-season'
        invalid_season['evidence_hash'] = service._hash({k: v for k, v in invalid_season.items() if k not in ('evidence_hash', 'source_failed')})
        for change in ({'is_published': False}, {'review_status': 'rejected'},
                       {'manual_json': {'tags': []}}, {'prompt_version': 999}, {'prompt_version': 5},
                       {'evidence_hash': '0' * 64}, {'generated_at': None},
                       {'evidence_json': invalid_scope, 'evidence_hash': invalid_scope['evidence_hash']},
                       {'evidence_json': invalid_season, 'evidence_hash': invalid_season['evidence_hash']}):
            with self.subTest(change=change):
                PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(**original)
                PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(**change)
                view = service.project_player_observation(self.player)
                self.assertFalse(view and view.get('display_judgements'))
                self.assertEqual(self.badges(), [])
        PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(**original)
        corrupt = copy.deepcopy(self.row.evidence_json)
        corrupt['coverage']['unique_runs'] = 999999
        PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(evidence_json=corrupt)
        self.assertFalse(service.project_player_observation(self.player).get('display_judgements'))

    def test_cross_spec_uses_same_central_analysis_and_identity_drift_retracts(self):
        sibling = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region=self.player.region,
            realm=self.player.realm, character_name=self.player.character_name, class_name=self.player.class_name,
            spec_name='Frost', rank=21, profile_url=self.player.profile_url)
        self.change_source()
        self.assert_frozen(service.project_player_observation(sibling))
        type(self.activity).objects.filter(pk=self.activity.pk).update(character_name='another-character')
        view = service.project_player_observation(self.player)
        self.assertFalse(view and view.get('display_judgements'))
        self.assertEqual(self.badges(), [])
