"""Real ORM/API state-machine tests; external process/network boundaries only mocked."""
import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.utils import timezone

from botend.models import PlayerObservationAnalysis, PlayerSpecTopPlayer, RaiderIOCharacterActivity, SeasonMeta
from botend.services.player_raiderio_activity import player_identity


class RecheckTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='recheck', season_name='Test', rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu', realm='test-realm', character_name='Member0', class_name='Priest', spec_name='Holy', rank=1, profile_url='https://raider.io/characters/eu/test-realm/Member0')
        self.activity = RaiderIOCharacterActivity.objects.create(season=self.season, **player_identity(self.player))
        self.row = PlayerObservationAnalysis.objects.create(player=self.player, activity=self.activity, manual_json={'conclusion': 'retain'}, is_published=False, review_status='rejected')
        self.admin = get_user_model().objects.create_user('root', is_superuser=True)
        self.client.force_login(self.admin)
        self.url = f'/dashboard/api/player-analyses/{self.row.pk}/recheck/'

    def post(self, payload=None):
        return self.client.post(self.url, json.dumps({'revision': 0} if payload is None else payload), content_type='application/json')

    def test_queue_is_persisted_deduplicated_and_dispatches_after_commit(self):
        with patch('botend.services.player_analysis_recheck.subprocess.Popen') as spawn:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post()
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()['data']['state'], 'queued')
                spawn.assert_not_called()
            spawn.assert_called_once()
            self.assertTrue(spawn.call_args.kwargs['start_new_session'])
            again = self.post().json()['data']
            self.assertEqual(again['token'], response.json()['data']['token'])
            spawn.assert_called_once()
        self.row.refresh_from_db()
        self.assertEqual(self.row.recheck_json['state'], 'queued')
        self.assertEqual(self.row.revision, 0)
        self.assertEqual(self.client.get(self.url).json()['data']['token'], again['token'])

    def test_permissions_csrf_strict_json_and_revision(self):
        self.client.logout()
        self.assertEqual(self.post().status_code, 403)
        self.client.force_login(self.admin)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post(self.url, '{}', content_type='application/json').status_code, 403)
        for data in ({'revision': True}, {'revision': 0, 'force': True}, {}, {'revision': -1}):
            self.assertEqual(self.post(data).status_code, 400)
        self.assertEqual(self.post({'revision': 7}).status_code, 409)
        self.assertEqual(self.client.post(self.url, '{"revision":0,"revision":0}', content_type='application/json').status_code, 400)
        self.assertEqual(self.client.post(self.url, ' ' * (128 * 1024 + 1), content_type='application/json').status_code, 413)

    def queue(self):
        from botend.services.player_analysis_recheck import enqueue
        return enqueue(self.row.pk, {'revision': self.row.revision})['token']

    def test_real_collectors_and_generator_preserve_editorial_and_publish_local_empty(self):
        from unittest.mock import Mock
        from botend.services.player_analysis_recheck import run
        from botend.tests.test_player_raiderio_activity import payload
        from botend.tests.test_player_raid_observation_sources import fixtures, BASE, CATALOG
        html, catalog, details, central = fixtures()
        self.season.raid_encounters = central
        self.season.save()
        base = 'https://raider.io/characters/eu/test-realm/member0'
        html = html.replace(BASE.replace('/characters/', '/api/characters/'), base.replace('/characters/', '/api/characters/'))
        details['characterDetails']['character'] = {'id': 100, 'name': 'Member0', 'region': {'slug': 'eu'}, 'realm': {'slug': 'test-realm'}, 'path': '/characters/eu/test-realm/Member0'}
        details['characterRaidProgress']['raidProgress'] = []
        data = payload('Member0')
        data['mythic_plus_best_runs'] = []
        calls = []
        def transport(url, **kwargs):
            calls.append(url)
            self.assertEqual(kwargs['timeout'], 25)
            if url.endswith('/profile'):
                return Mock(status_code=200, headers={}, json=Mock(return_value=data))
            if url == base:
                return Mock(status_code=200, headers={}, text=html)
            self.assertTrue(url == CATALOG or '/api/characters/' in url, url)
            return Mock(status_code=200, headers={}, json=Mock(return_value=catalog if url == CATALOG else details))
        token = self.queue()
        with patch('requests.Session.get', side_effect=transport), patch('botend.services.player_observation_sources._pace', return_value=True), patch('botend.services.player_raid_observation_sources._pace', return_value=True), patch('botend.services.player_observation.PlayerObservationGLMClient', side_effect=AssertionError('no supported judgement means no AI')):
            result = run(self.row.pk, token)
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertEqual(len(calls), 4)
        self.row.refresh_from_db()
        self.assertEqual(self.row.revision, 1)
        self.assertEqual(self.row.manual_json, {'conclusion': 'retain'})
        self.assertFalse(self.row.is_published)
        self.assertEqual(self.row.review_status, 'rejected')
        self.assertEqual(self.row.result_json['metadata']['source_kind'], 'local_deterministic')
        self.assertEqual(run(self.row.pk, token)['state'], 'succeeded')

    def test_partial_stops_before_raid_and_ai_preserves_last_good(self):
        from botend.services.player_analysis_recheck import run
        PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(result_json={'last': 'good'}, evidence_hash='old')
        token = self.queue()
        with patch('botend.services.player_observation_sources.refresh_observation_sources', return_value={'status': 'partial'}) as sources, patch('botend.services.player_raid_observation_sources.refresh_raid_sources') as raid, patch('botend.services.player_observation.generate_observation') as generate:
            self.assertEqual(run(self.row.pk, token)['state'], 'blocked')
            raid.assert_not_called()
            generate.assert_not_called()
            self.assertEqual(sources.call_args.kwargs, {'progressive': True, 'refresh_recent': True, 'max_basic_roster_requests': 5, 'max_followup_requests': 2, 'max_runs': 18, 'max_characters': 40})
        self.row.refresh_from_db()
        self.assertEqual(self.row.result_json, {'last': 'good'})

    def test_expiry_get_is_readonly_stale_token_and_flock_do_not_collect(self):
        from botend.services.player_analysis_recheck import run, character_lock
        token = self.queue()
        with character_lock(self.activity), patch('botend.services.player_observation_sources.refresh_observation_sources') as sources:
            self.assertEqual(run(self.row.pk, token)['state'], 'blocked')
            sources.assert_not_called()
        self.row.refresh_from_db()
        data = {**self.row.recheck_json, 'state': 'running', 'expires_at': (timezone.now()-timedelta(seconds=1)).isoformat()}
        PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(recheck_json=data)
        self.assertEqual(self.client.get(self.url).json()['data']['state'], 'failed')
        self.row.refresh_from_db()
        self.assertEqual(self.row.recheck_json, data)
        new_token = self.queue()
        with patch('botend.services.player_observation_sources.refresh_observation_sources') as sources:
            run(self.row.pk, token)
            sources.assert_not_called()
        self.row.refresh_from_db()
        self.assertEqual(self.row.recheck_json['token'], new_token)

    def test_generator_publication_fences_supersession_and_source_failure(self):
        # Run the AI callback where source failure/token replacement is injected.
        self.enterContext(patch('django.utils.timezone.now',
            return_value=datetime(2026, 10, 11, 12, tzinfo=datetime_timezone.utc)))
        from botend.tests.test_player_observation import PlayerObservationTests
        from botend.services.player_analysis_recheck import RUN_SECONDS
        from botend.services.player_observation import generate_observation
        from botend.services.player_raiderio_activity import MODULES
        PlayerObservationTests.setUp(self)
        now = timezone.now()
        self.activity.refresh_from_db()
        self.activity.module_fetched_at = {key: now.isoformat() for key in MODULES.values()}
        # This publication-fence fixture must contain the counted module whose
        # clock it declares; missing is not an authoritative empty response.
        from botend.tests.test_player_raiderio_activity import payload
        self.activity.payload_json['mythic_plus_dungeon_run_counts'] = payload()['mythic_plus_dungeon_run_counts']
        self.activity.payload_json['raid_first_kills'] = {'version': 1, 'visible': True, 'status': 'ready',
            'last_error': '', 'identity_digest': self.activity.identity_digest, 'rio_season': self.season.rio_season,
            'fetched_at': now.isoformat(), 'source': {'character_id': 100}, 'raids': []}
        self.activity.save()
        self.row = PlayerObservationAnalysis.objects.create(player=self.player, activity=self.activity, result_json={'last': 'good'})
        token = self.queue()
        self.row.refresh_from_db()
        self.row.recheck_json.update(state='running', expires_at=(now+timedelta(seconds=RUN_SECONDS)).isoformat())
        self.row.save(update_fields=['recheck_json'])
        def fail_source(result):
            RaiderIOCharacterActivity.objects.filter(pk=self.activity.pk).update(last_error='transport_error')
        engine = PlayerObservationTests.engine(self, fail_source)
        result = generate_observation(self.player, force=True, engine=engine, recheck_token=token)
        engine.send_message.assert_called_once()
        self.assertEqual(result['status'], 'recheck_invalidated')
        self.row.refresh_from_db()
        self.assertEqual(self.row.result_json, {'last': 'good'})
        self.assertEqual(self.row.revision, 0)
        RaiderIOCharacterActivity.objects.filter(pk=self.activity.pk).update(last_error='')
        PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(next_attempt_at=None)
        def supersede(result):
            data = {**self.row.recheck_json, 'token': 'f'*32}
            PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(recheck_json=data)
        replacement_engine = PlayerObservationTests.engine(self, supersede)
        result = generate_observation(self.player, force=True, engine=replacement_engine, recheck_token=token)
        replacement_engine.send_message.assert_called_once()
        self.assertEqual(result['status'], 'recheck_invalidated')
        self.row.refresh_from_db()
        self.assertEqual(self.row.result_json, {'last': 'good'})
        self.assertEqual(self.row.recheck_json['token'], 'f'*32)

    def test_completed_progressive_followup_and_private_rosters_are_usable(self):
        from botend.services.player_analysis_recheck import require_sources, Blocked
        base = {'status': 'partial', 'collection_mode': 'progressive',
                'activity': {'status': 'ready'}, 'runs': [{'status': 'fresh'}],
                'characters': [{'status': 'pending'}], 'followup_triggered': True,
                'followup_budget_reason': 'completed'}
        require_sources(base)
        require_sources({**base, 'runs': [{'status': 'unavailable'}],
                         'followup_triggered': False, 'followup_budget_reason': 'not_needed'})
        require_sources({**base, 'followup_triggered': False, 'characters_truncated': True})
        for bad in ({'followup_budget_reason': 'budget_exhausted'},
                    {'runs': [{'status': 'backoff'}]}, {'identity_conflicts': 1},
                    {'runs_truncated': True}, {'activity': {'status': 'partial'}}):
            with self.subTest(bad=bad), self.assertRaises(Blocked):
                require_sources({**base, **bad})

    def test_other_active_character_blocks_dispatch_but_expired_job_does_not(self):
        other_activity = RaiderIOCharacterActivity.objects.create(season=self.season,
            region='eu', realm_slug='test-realm', character_name='Other')
        other = PlayerObservationAnalysis.objects.create(activity=other_activity,
            recheck_json={'state': 'running', 'token': 'a'*32,
                          'expires_at': (timezone.now()+timedelta(minutes=5)).isoformat()})
        with patch('botend.services.player_analysis_recheck.subprocess.Popen') as spawn:
            with self.captureOnCommitCallbacks(execute=True):
                result = self.post().json()['data']
            self.assertEqual(result['state'], 'blocked')
            self.assertIn('其他角色', result['message'])
            spawn.assert_not_called()
            other.recheck_json['expires_at'] = (timezone.now()-timedelta(seconds=1)).isoformat()
            other.save(update_fields=['recheck_json'])
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.post().json()['data']['state'], 'queued')
            spawn.assert_called_once()

    def test_identity_and_backoff_block_without_dispatch(self):
        with patch('botend.services.player_analysis_recheck.subprocess.Popen') as spawn:
            PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(next_attempt_at=timezone.now()+timedelta(minutes=30))
            self.assertEqual(self.post().json()['data']['state'], 'blocked')
            PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(next_attempt_at=None, player=None)
            self.assertEqual(self.post().json()['data']['state'], 'blocked')
            spawn.assert_not_called()
