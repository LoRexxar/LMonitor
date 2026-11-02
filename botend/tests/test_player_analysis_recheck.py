"""Real ORM/API state-machine tests; external process/network boundaries only mocked."""
import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, Client
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
            self.assertEqual(run(self.row.pk, token)['state'], 'insufficient_evidence')
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


class InsufficientEvidenceTests(TransactionTestCase):
    """Full ORM snapshots and real collectors; only HTTP/AI are forbidden boundaries."""
    def setUp(self):
        now = timezone.now()
        self.season = SeasonMeta.objects.create(season_key='insufficient', season_name='Test',
            rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu',
            realm='test-realm', character_name='Member0', class_name='Priest', spec_name='Holy',
            rank=1, profile_url='https://raider.io/characters/eu/test-realm/Member0')
        self.activity = RaiderIOCharacterActivity.objects.create(season=self.season,
            **player_identity(self.player), status='error', last_error='transport_error',
            failure_count=3, last_attempt_at=now, next_attempt_at=now+timedelta(hours=1),
            payload_json={'last': 'good'}, fetched_at=now-timedelta(days=1))
        self.row = PlayerObservationAnalysis.objects.create(player=self.player, activity=self.activity,
            status='ready', prompt_version=9, result_json={'old': 'result'},
            evidence_json={'old': 'evidence'}, evidence_hash='old-hash', generated_at=now,
            manual_json={'conclusion': 'retain'}, is_published=False, review_status='rejected',
            revision=7, updated_at=now, reviewed_at=now, review_note='retain review',
            recheck_json={'state': 'blocked', 'token': 'old-token', 'queued_at': now.isoformat(),
                'started_at': now.isoformat(), 'expires_at': now.isoformat(),
                'retry_at': (now+timedelta(hours=1)).isoformat(),
                'collection': {'api_requests': 2}})

    def snapshot(self):
        return PlayerObservationAnalysis.objects.values().get(pk=self.row.pk)

    def source_snapshot(self):
        return RaiderIOCharacterActivity.objects.values().get(pk=self.activity.pk)

    def record(self, snapshot, reason='source_unavailable'):
        from botend.services.player_analysis_recheck import record_insufficient_evidence
        return record_insufficient_evidence(self.row.pk, expected_snapshot=snapshot, reason=reason)

    def test_real_collector_backoff_is_terminal_no_http_ai_or_new_publication(self):
        from botend.services.player_analysis_recheck import enqueue, run, get_state
        # Manual enqueue is allowed; a Source's error/backoff is never cleared.
        PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(recheck_json={})
        with patch('botend.services.player_analysis_recheck.subprocess.Popen'):
            token = enqueue(self.row.pk, {'revision': 7})['token']
        before, source = self.snapshot(), self.source_snapshot()
        with patch('requests.sessions.Session.request', side_effect=AssertionError('no HTTP')) as http, \
                patch('botend.services.player_observation.PlayerObservationGLMClient',
                      side_effect=AssertionError('no AI')) as ai:
            result = run(self.row.pk, token)
            self.assertEqual(result['state'], 'insufficient_evidence', result)
            self.assertEqual(run(self.row.pk, token), result)
            self.assertEqual(get_state(self.row.pk), result)
            http.assert_not_called()
            ai.assert_not_called()
        after = self.snapshot()
        self.assertEqual({k: v for k, v in after.items() if k != 'recheck_json'},
                         {k: v for k, v in before.items() if k != 'recheck_json'})
        self.assertEqual(self.source_snapshot(), source)
        data = after['recheck_json']
        self.assertEqual(data['sources']['status'], 'backoff')
        self.assertEqual(data['sources']['api_requests'], 0)
        self.assertEqual(data['phase'], 'complete')
        self.assertIs(data['ai_called'], False)
        self.assertIs(data['auto_retry'], False)
        self.assertNotIn('retry_at', data)
        self.assertEqual(result['message'], '本次检查完成：证据不足，不生成新标签。')

    def test_real_raid_backoff_is_terminal_without_http_or_ai(self):
        from botend.services.player_analysis_recheck import enqueue, run
        from botend.services.player_raiderio_activity import MODULES, validate_payload
        from botend.tests.test_player_raiderio_activity import payload
        now = timezone.now()
        data = payload('Member0')
        data['mythic_plus_best_runs'] = []
        facts, updated = validate_payload(data, player_identity(self.player), self.season)
        facts['raid_first_kills'] = {'status': 'error', 'last_error': 'transport_error',
            'failure_count': 3, 'visible': False, 'next_attempt_at': (now+timedelta(hours=1)).isoformat()}
        RaiderIOCharacterActivity.objects.filter(pk=self.activity.pk).update(status='partial',
            last_error='', failure_count=0, payload_json=facts, fetched_at=now,
            source_updated_at=updated, last_attempt_at=now,
            module_fetched_at={key: now.isoformat() for key in MODULES.values()})
        PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(recheck_json={})
        with patch('botend.services.player_analysis_recheck.subprocess.Popen'):
            token = enqueue(self.row.pk, {'revision': 7})['token']
        before, source = self.snapshot(), self.source_snapshot()
        with patch('requests.sessions.Session.request', side_effect=AssertionError('no HTTP')) as http, \
                patch('botend.services.player_observation.PlayerObservationGLMClient',
                      side_effect=AssertionError('no AI')) as ai:
            self.assertEqual(run(self.row.pk, token)['state'], 'insufficient_evidence')
            http.assert_not_called()
            ai.assert_not_called()
        after = self.snapshot()
        self.assertEqual(after['recheck_json']['sources']['status'], 'ready')
        self.assertEqual(after['recheck_json']['sources']['api_requests'], 0)
        self.assertEqual({k: v for k, v in after.items() if k != 'recheck_json'},
                         {k: v for k, v in before.items() if k != 'recheck_json'})
        self.assertEqual(self.source_snapshot(), source)

    def test_safety_outcomes_are_not_insufficient_evidence(self):
        from botend.services.player_analysis_recheck import require_sources, Blocked, InsufficientEvidence
        for status in ('busy', 'rate_limited', 'database_busy_or_error', 'superseded',
                       'cas_non_target_change', 'cas_claim_mismatch', 'cas_finalize_mismatch'):
            for value in (
                    {'status': status},
                    {'status': 'error', 'source_failed': True,
                     'investigation': {'requests': [{'status': status}]}},
                    {'status': 'partial', 'source_failed': True,
                     'investigation': {'outcomes': [{'status': 'source_failed', 'request_status': status}]}}):
                with self.subTest(status=status, sources=value):
                    with self.assertRaises(Blocked) as caught:
                        require_sources(value)
                    self.assertNotIsInstance(caught.exception, InsufficientEvidence)

    def test_record_only_control_metadata_and_repeat_requires_fresh_snapshot(self):
        from botend.services.raiderio_run_roster import _snapshot_json
        before, source = self.snapshot(), self.source_snapshot()
        self.assertEqual(self.record(before)['state'], 'insufficient_evidence')
        after = self.snapshot()
        self.assertEqual({k: v for k, v in after.items() if k != 'recheck_json'},
                         {k: v for k, v in before.items() if k != 'recheck_json'})
        self.assertEqual(self.source_snapshot(), source)
        data = after['recheck_json']
        self.assertEqual(data['phase'], 'complete')
        self.assertEqual(data['reason'], 'source_unavailable')
        self.assertIs(data['ai_called'], False)
        self.assertIs(data['auto_retry'], False)
        self.assertEqual(data['binding']['activity_id'], self.activity.pk)
        self.assertEqual(data['collection'], before['recheck_json']['collection'])
        for key in ('token', 'queued_at', 'started_at', 'expires_at', 'retry_at'):
            self.assertNotIn(key, data)
        self.assertEqual(self.record(before)['status'], 'cas_claim_mismatch')
        self.assertEqual(self.snapshot(), after)
        # Exact current snapshot is an idempotent readback, not a new check.
        self.assertEqual(self.record(_snapshot_json(after))['state'], 'insufficient_evidence')
        self.assertEqual(self.snapshot(), after)

    def test_record_rejects_incomplete_snapshot_bad_reason_and_any_row_drift(self):
        before = self.snapshot()
        for snapshot in ({}, {'id': self.row.pk}, {**before, 'extra': 1},
                         {**before, 'id': True}, {**before, 'revision': True},
                         {**before, 'evidence_json': {'old': 'drift'}}):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(self.record(snapshot)['status'], 'cas_claim_mismatch')
                self.assertEqual(self.snapshot(), before)
        for reason in ('', 'unknown_exception', 'rate_limited'):
            with self.subTest(reason=reason):
                self.assertEqual(self.record(before, reason)['status'], 'invalid_reason')
                self.assertEqual(self.snapshot(), before)
        # A post-preview edit to any field must refuse the whole update.
        for field, value in (('manual_json', {'conclusion': 'changed'}),
                             ('result_json', {'new': 'result'}), ('revision', 8),
                             ('recheck_json', {'state': 'failed'})):
            with self.subTest(field=field):
                PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(**{field: value})
                drift = self.snapshot()
                self.assertEqual(self.record(before)['status'], 'cas_claim_mismatch')
                self.assertEqual(self.snapshot(), drift)
                PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(**{field: before[field]})

    def test_record_refuses_active_jobs_even_expired_and_live_analysis_lease(self):
        for state in ('queued', 'running'):
            for expires in (None, timezone.now()-timedelta(hours=1), timezone.now()+timedelta(hours=1)):
                with self.subTest(state=state, expires=expires):
                    PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(
                        recheck_json={'state': state, 'expires_at': expires.isoformat() if expires else None})
                    before = self.snapshot()
                    self.assertEqual(self.record(before)['status'], 'busy')
                    self.assertEqual(self.snapshot(), before)
        PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(recheck_json={},
            lease_token='a'*32, lease_expires_at=timezone.now()+timedelta(hours=1))
        before = self.snapshot()
        self.assertEqual(self.record(before)['status'], 'busy')
        self.assertEqual(self.snapshot(), before)

    def test_record_explicit_identity_or_scope_reason_does_not_require_generation_eligibility(self):
        PlayerObservationAnalysis.objects.filter(pk=self.row.pk).update(player=None,
            next_attempt_at=timezone.now()+timedelta(hours=1))
        for reason in ('identity_unresolved', 'outside_scope'):
            with self.subTest(reason=reason):
                before, source = self.snapshot(), self.source_snapshot()
                self.assertEqual(self.record(before, reason)['state'], 'insufficient_evidence')
                after = self.snapshot()
                self.assertEqual(after['recheck_json']['reason'], reason)
                self.assertEqual({k: v for k, v in before.items() if k != 'recheck_json'},
                                 {k: v for k, v in after.items() if k != 'recheck_json'})
                self.assertEqual(self.source_snapshot(), source)

    def test_source_gate_rejections_remain_strict_but_busy_and_rate_limit_stay_blocked(self):
        from botend.services.player_analysis_recheck import require_sources, Blocked, InsufficientEvidence
        base = {'status': 'ready', 'activity': {'status': 'ready'}}
        for changes in ({'source_failed': True}, {'status': 'backoff'},
                        {'activity': {'status': 'partial'}}, {'identity_conflicts': 1},
                        {'invalid_references': 1}, {'runs_truncated': True},
                        {'runs': [{'status': 'backoff'}]}, {'characters': [{'status': 'error'}]},
                        {'followup_triggered': True, 'followup_budget_reason': 'budget_exhausted'},
                        {'status': 'partial'}):
            with self.subTest(changes=changes), self.assertRaises(InsufficientEvidence):
                require_sources({**base, **changes})
        for changes in ({'status': 'busy'}, {'status': 'rate_limited'},
                        {'runs': [{'status': 'busy'}]}, {'characters': [{'status': 'rate_limited'}]},
                        {'followup_budget_reason': 'rate_limited'}):
            with self.subTest(changes=changes), self.assertRaises(Blocked) as caught:
                require_sources({**base, **changes})
            self.assertNotIsInstance(caught.exception, InsufficientEvidence)
