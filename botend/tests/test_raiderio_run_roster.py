"""Run-detail contracts; synthetic small fixtures, never profile gear snapshots."""
import copy
import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.core.management import call_command
from django.db import connection, IntegrityError, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import RaiderIOCharacterActivity, RaiderIORunRoster
from botend.services.player_raiderio_activity import ActivityCollector, project_player_activity
from botend.services.raiderio_run_roster import RosterCollector, project_run_rosters, run_reference
from botend.tests import test_player_raiderio_activity as activity_tests
from botend.tests.test_player_raiderio_activity import payload


def details(run_id=123):
    return {'season': 'season-mn-2', 'keystone_run_id': run_id,
            'dungeon': {'id': 1, 'name': "Kings' Rest", 'slug': 'kings-rest', 'map_challenge_mode_id': 586},
            'mythic_level': 20, 'clear_time_ms': 1234567, 'num_chests': 0,
            'completed_at': '2026-10-10T12:00:00Z', 'runPrivacyMode': 'public',
            'roster': [{'role': 'healer' if i == 0 else 'dps', 'items': {'unwanted': True},
                'character': {'name': f'Member{i}', 'class': {'id': 5, 'name': 'Priest', 'slug': 'priest'},
                    'spec': {'id': 257, 'name': 'Holy', 'slug': 'holy', 'role': 'healer'},
                    'path': f'/characters/eu/test-realm/Member{i}',
                    'realm': {'slug': 'test-realm'}, 'region': {'slug': 'eu'},
                    'talentLoadout': {'unwanted': True}}} for i in range(5)]}


class RunRosterTests(TestCase):
    setUp = activity_tests.PlayerActivityTests.setUp
    make_player = activity_tests.PlayerActivityTests.make_player

    def setUp(self):
        activity_tests.PlayerActivityTests.setUp(self)
        self.run = payload()['mythic_plus_best_runs'][0]
        self.run['keystone_run_id'] = 123

    def collect(self, data=None, status=200):
        transport = Mock(return_value=Mock(status_code=status, headers={}, json=Mock(return_value=data or details())))
        collector = RosterCollector(transport=transport)
        return collector, transport

    def activity(self, runs=None, player=None):
        """Seed pre-existing central activity, independent of automatic maintenance."""
        from botend.services.player_raiderio_activity import player_identity, validate_payload
        player = player or self.player
        data = payload(player.character_name)
        data['mythic_plus_best_runs'] = runs if runs is not None else [self.run]
        identity = player_identity(player)
        facts, updated = validate_payload(data, identity, self.season)
        RaiderIOCharacterActivity.objects.create(season=self.season, **identity,
            payload_json=facts, source_updated_at=updated, fetched_at=timezone.now(), status='ready')

    def test_collect_once_shared_fact_and_one_query_readonly_projection(self):
        self.activity([self.run] * 8)
        collector, transport = self.collect()
        self.assertEqual(collector.refresh(self.run, self.season.rio_season)['status'], 'ready')
        self.assertEqual(collector.refresh(self.run, self.season.rio_season)['status'], 'fresh')
        self.assertEqual(transport.call_count, 1)
        row = RaiderIORunRoster.objects.get()
        self.assertNotIn('items', json.dumps(row.payload_json))
        self.assertNotIn('talentLoadout', json.dumps(row.payload_json))
        with patch('requests.get', side_effect=AssertionError('local only')), CaptureQueriesContext(connection) as queries:
            view = project_player_activity(self.player)
        self.assertEqual(sum('wow_raiderio_run_roster' in q['sql'] for q in queries), 1)
        self.assertEqual(len(view['best_runs']['rows']), 8)
        run = view['best_runs']['rows'][0]
        self.assertIs(run['timed'], False)
        self.assertEqual(run['roster_status'], 'ready')
        self.assertEqual(len(run['roster']), 5)
        self.assertEqual(run['roster'][0]['class_label'], '牧师')
        self.assertEqual(run['roster'][0]['spec_label'], '神圣')
        self.assertEqual(run['roster'][1]['role_label'], '输出')  # NOT spec.role
        self.assertTrue(run['roster'][0]['profile_url'].startswith('https://raider.io/characters/'))
        with self.assertRaises(IntegrityError), transaction.atomic():
            RaiderIORunRoster.objects.create(rio_season=row.rio_season, run_id=row.run_id)

    def test_strict_reference_rejects_conflicting_and_malformed_tokens(self):
        self.assertEqual(run_reference(self.run, 'season-mn-2'), 123)
        for changes in ({'keystone_run_id': 124}, {'keystone_run_id': True}, {'keystone_run_id': '123'},
                        {'season': 'season-old'}, {'url': self.run['url'].replace('/123-', '/0123-')},
                        {'url': self.run['url'].replace('season-mn-2', 'season-old')}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                run_reference({**self.run, **changes}, 'season-mn-2')

    def test_invalid_details_preserve_last_good_backoff_and_stale(self):
        self.activity()
        collector, transport = self.collect()
        collector.refresh(self.run, self.season.rio_season)
        saved = RaiderIORunRoster.objects.get().payload_json
        for changes in ({'keystone_run_id': 124}, {'season': 'season-old'}, {'mythic_level': 21},
                        {'dungeon': {'name': 'Other'}}, {'roster': details()['roster'][:4]}):
            transport.return_value.json.return_value = {**details(), **changes}
            self.assertEqual(collector.refresh(self.run, self.season.rio_season, force=True)['status'], 'invalid_payload')
            self.assertEqual(RaiderIORunRoster.objects.get().payload_json, saved)
        self.assertEqual(collector.refresh(self.run, self.season.rio_season)['status'], 'backoff')
        self.assertEqual(project_player_activity(self.player)['best_runs']['rows'][0]['roster_status'], 'stale')

    def test_privacy_empty_and_missing_do_not_invent_members(self):
        self.activity()
        for changes in ({'runPrivacyMode': 'private'}, {'roster': []}, {'roster': None}):
            collector, _ = self.collect({**details(), **changes})
            self.assertEqual(collector.refresh(self.run, self.season.rio_season, force=True)['status'], 'unavailable')
            view = project_player_activity(self.player)['best_runs']['rows'][0]
            self.assertEqual(view['roster_status'], 'unavailable')
            self.assertEqual(view['roster'], [])

    def test_busy_force_and_lost_lease_never_overwrite_other_owner(self):
        row = RaiderIORunRoster.objects.create(rio_season='season-mn-2', run_id=123,
            status='fetching', lease_token='other', next_attempt_at=timezone.now() + timedelta(minutes=1))
        collector, transport = self.collect()
        before = RaiderIORunRoster.objects.values().get()
        self.assertEqual(collector.refresh(self.run, 'season-mn-2', force=True)['status'], 'busy')
        transport.assert_not_called()
        self.assertEqual(RaiderIORunRoster.objects.values().get(), before)
        row.next_attempt_at = timezone.now() - timedelta(seconds=1); row.save()
        def lose_lease(*args, **kwargs):
            RaiderIORunRoster.objects.update(lease_token='new-owner')
            return Mock(status_code=200, json=lambda: details())
        transport.side_effect = lose_lease
        self.assertEqual(collector.refresh(self.run, 'season-mn-2')['status'], 'superseded')
        self.assertEqual(RaiderIORunRoster.objects.get().lease_token, 'new-owner')

    def test_429_stops_batch_even_force(self):
        collector, transport = self.collect(status=429)
        self.assertEqual(collector.refresh(self.run, 'season-mn-2')['status'], 'rate_limited')
        self.assertEqual(collector.refresh(self.run, 'season-mn-2', force=True)['status'], 'batch_stopped')
        self.assertEqual(transport.call_count, 1)

    def test_daily_profile_bounds_rosters_and_isolates_failure(self):
        data = payload()
        data['gear'] = {'items': {'head': {'item_id': 99}}}
        data['talents'] = []
        data['mythic_plus_best_runs'] = [{**self.run, 'keystone_run_id': i,
            'url': self.run['url'].replace('/123-', f'/{i}-')} for i in range(1, 11)]
        def get(url, **kwargs):
            return Mock(status_code=200, json=lambda: data if url.endswith('/profile') else details(kwargs['params']['id']))
        transport = Mock(side_effect=get)
        collector = ActivityCollector(transport=transport)
        result = collector.refresh(self.player, self.season, include_profile=True)
        self.assertEqual(result['status'], 'ready')
        self.assertIn('gear', result['profile_payload'])
        self.assertEqual(RaiderIORunRoster.objects.count(), 8)
        self.assertEqual(transport.call_count, 9)
        collector.refresh(self.player, self.season)
        self.assertEqual(transport.call_count, 9)
        self.assertEqual(len(RaiderIOCharacterActivity.objects.get().payload_json['mythic_plus_best_runs']), 10)

    def test_private_after_success_keeps_fact_but_never_reexposes_on_failure(self):
        self.activity()
        collector, transport = self.collect()
        collector.refresh(self.run, 'season-mn-2')
        before = RaiderIORunRoster.objects.get().payload_json
        transport.return_value.json.return_value = {**details(), 'runPrivacyMode': 'private'}
        collector.refresh(self.run, 'season-mn-2', force=True)
        self.assertEqual(RaiderIORunRoster.objects.get().payload_json, before)
        transport.return_value.status_code = 500
        collector.refresh(self.run, 'season-mn-2', force=True)
        self.assertEqual(project_player_activity(self.player)['best_runs']['rows'][0]['roster'], [])

    def test_daily_roster_429_preserves_profile_and_activity_and_stops(self):
        data = payload()
        data['gear'], data['talents'] = {'items': {'head': {'item_id': 99}}}, [{'id': 7}]
        transport = Mock(side_effect=[Mock(status_code=200, json=lambda: data), Mock(status_code=429, headers={})])
        collector = ActivityCollector(transport=transport)
        result = collector.refresh(self.player, self.season, include_profile=True)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['profile_payload']['gear'], data['gear'])
        self.assertEqual(result['roster_results'][0]['status'], 'rate_limited')
        self.assertTrue(collector.stopped)
        self.assertEqual(RaiderIOCharacterActivity.objects.get().status, 'ready')
        self.assertEqual(collector.refresh(self.player, self.season, force=True)['status'], 'batch_stopped')
        self.assertEqual(transport.call_count, 2)

    def test_command_unique_top20_dry_run_checkpoint_apply_without_profile(self):
        self.activity([self.run, self.run])
        self.make_player('Discipline', 'TESTER')
        historical = self.make_player('Holy', 'Historical', rank=None)
        historical_data = payload('Historical')
        historical_data['mythic_plus_best_runs'][0]['url'] = self.run['url'].replace('/123-', '/999-')
        self.activity(historical_data['mythic_plus_best_runs'], player=historical)
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'result.json')
            out = io.StringIO()
            with patch('requests.Session.get', side_effect=AssertionError('dryrun')):
                call_command('sync_player_raiderio_rosters', season=self.season.pk, result_json=path, stdout=out)
            report = json.loads(Path(path).read_text())
            self.assertEqual(report['candidate_count'], 1)
            self.assertEqual(report['results'][0]['run_id'], 123)
            self.assertFalse(RaiderIORunRoster.objects.exists())
            self.assertNotIn('Tester', out.getvalue())
            with patch('requests.Session.get', return_value=Mock(status_code=200, json=lambda: details())) as get:
                call_command('sync_player_raiderio_rosters', season=self.season.pk, apply=True, limit=1, stdout=io.StringIO())
                resumed = io.StringIO()
                call_command('sync_player_raiderio_rosters', season=self.season.pk, apply=True, limit=1, stdout=resumed)
                self.assertEqual(json.loads(resumed.getvalue())['counts'], {'fresh': 1})
            self.assertEqual(get.call_count, 1)
            self.assertTrue(get.call_args.args[0].endswith('/run-details'))
            self.assertEqual(RaiderIORunRoster.objects.get().status, 'ready')


class RosterMaintenanceTests(TestCase):
    setUp = activity_tests.PlayerActivityTests.setUp
    make_player = activity_tests.PlayerActivityTests.make_player

    def collector(self, data=None):
        data = data or payload()
        def get(url, **kwargs):
            body = data if url.endswith('/profile') else details(kwargs['params']['id'])
            return Mock(status_code=200, headers={}, json=lambda: body)
        transport = Mock(side_effect=get)
        return ActivityCollector(transport=transport), transport

    def seed_activity(self):
        from botend.services.player_raiderio_activity import player_identity, validate_payload
        facts, updated = validate_payload(payload(), player_identity(self.player), self.season)
        return RaiderIOCharacterActivity.objects.create(
            season=self.season, **player_identity(self.player), payload_json=facts,
            status='ready', fetched_at=timezone.now(), source_updated_at=updated,
            next_attempt_at=timezone.now() + timedelta(hours=1))

    def test_source_healer_first_sorted_by_run_role_stably_not_spec(self):
        run = payload()['mythic_plus_best_runs'][0]
        data = details()
        for member, role in zip(data['roster'], ('healer', 'dps', None, 'tank', 'dps')):
            member['role'] = role
        collector = RosterCollector(transport=Mock(return_value=Mock(status_code=200, json=lambda: data)))
        self.assertEqual(collector.refresh(run, 'season-mn-2')['status'], 'ready')
        projected = project_run_rosters([run], 'season-mn-2')[0]['roster']
        self.assertEqual([item['name'] for item in projected],
                         ['Member3', 'Member0', 'Member1', 'Member4', 'Member2'])
        self.assertEqual([item['role_label'] for item in projected], ['坦克', '治疗', '输出', '输出', ''])
        self.assertEqual([item['character']['name'] for item in RaiderIORunRoster.objects.get().payload_json['roster']],
                         [f'Member{i}' for i in range(5)])

    def test_deleted_at_hides_last_good_even_after_transport_failure(self):
        run = payload()['mythic_plus_best_runs'][0]
        transport = Mock(return_value=Mock(status_code=200, json=lambda: details()))
        collector = RosterCollector(transport=transport)
        collector.refresh(run, 'season-mn-2')
        saved = RaiderIORunRoster.objects.get().payload_json
        transport.return_value.json = lambda: {**details(), 'deleted_at': '2026-10-11T12:00:00Z'}
        self.assertEqual(collector.refresh(run, 'season-mn-2', force=True)['status'], 'unavailable')
        row = RaiderIORunRoster.objects.get()
        self.assertFalse(row.roster_visible)
        self.assertEqual(row.payload_json, saved)
        self.assertEqual(project_run_rosters([run], 'season-mn-2')[0]['roster'], [])
        transport.side_effect = requests.Timeout('offline')
        self.assertEqual(collector.refresh(run, 'season-mn-2', force=True)['status'], 'transport_error')
        row.refresh_from_db()
        self.assertFalse(row.roster_visible)
        self.assertEqual(project_run_rosters([run], 'season-mn-2')[0], {'roster_status': 'unavailable', 'roster': []})
        self.assertIn('deleted_at', saved)
        self.assertIsNone(saved['deleted_at'])

    def test_activity_then_fresh_profile_repairs_missing_and_expired_rosters(self):
        collector, transport = self.collector(activity_tests.PlayerActivityTests._profile_payload())
        collector.refresh(self.player, self.season)
        original = RaiderIOCharacterActivity.objects.values().get()
        # Existing activities can predate the roster feature or lose a cached run.
        RaiderIORunRoster.objects.all().delete()
        transport.reset_mock()
        result = collector.refresh(self.player, self.season, include_profile=True)
        self.assertEqual(result['status'], 'fresh')
        self.assertIn('gear', result['profile_payload'])
        self.assertEqual(RaiderIORunRoster.objects.get().status, 'ready')
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(transport.call_args_list[0].kwargs['params']['fields'], 'gear,talents')
        RaiderIORunRoster.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        transport.reset_mock()
        self.assertEqual(collector.refresh(self.player, self.season)['status'], 'fresh')
        self.assertEqual(transport.call_count, 1)
        self.assertTrue(transport.call_args.args[0].endswith('/run-details'))
        self.assertEqual(RaiderIOCharacterActivity.objects.values().get(), original)

    def test_fresh_and_backoff_local_references_need_no_profile_request(self):
        row = self.seed_activity()
        for status, expected in (('ready', 'fresh'), ('error', 'backoff')):
            with self.subTest(status=status):
                RaiderIORunRoster.objects.all().delete()
                row.status = status
                row.save()
                original = RaiderIOCharacterActivity.objects.values().get()
                collector, transport = self.collector()
                self.assertEqual(collector.refresh(self.player, self.season)['status'], expected)
                self.assertEqual(RaiderIORunRoster.objects.get().status, 'ready')
                self.assertEqual(transport.call_count, 1)
                self.assertTrue(transport.call_args.args[0].endswith('/run-details'))
                self.assertEqual(RaiderIOCharacterActivity.objects.values().get(), original)

    def test_cn_activity_only_collects_roster_without_profile_gear_or_player_writes(self):
        self.player.region = 'cn'
        self.player.profile_url = self.player.profile_url.replace('/eu/', '/cn/')
        self.player.save()
        before = type(self.player).objects.values().get(pk=self.player.pk)
        data = payload()
        data['region'] = 'cn'
        data['profile_url'] = self.player.profile_url
        collector, transport = self.collector(data)
        result = collector.refresh(self.player, self.season)
        self.assertEqual(result['status'], 'ready')
        self.assertNotIn('profile_payload', result)
        self.assertEqual(RaiderIORunRoster.objects.get().status, 'ready')
        self.assertEqual(transport.call_count, 2)
        self.assertNotIn('gear', transport.call_args_list[0].kwargs['params']['fields'])
        self.assertEqual(type(self.player).objects.values().get(pk=self.player.pk), before)

    def test_busy_profile_preserves_lease_and_skips_rosters(self):
        row = self.seed_activity()
        row.status = 'fetching'
        row.save()
        before = RaiderIOCharacterActivity.objects.values().get()
        collector, transport = self.collector(activity_tests.PlayerActivityTests._profile_payload())
        self.assertEqual(collector.refresh(self.player, self.season, force=True)['status'], 'busy')
        transport.assert_not_called()
        result = collector.refresh(self.player, self.season, include_profile=True)
        self.assertEqual(result['status'], 'busy')
        self.assertIn('gear', result['profile_payload'])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.kwargs['params']['fields'], 'gear,talents')
        self.assertFalse(RaiderIORunRoster.objects.exists())
        self.assertEqual(RaiderIOCharacterActivity.objects.values().get(), before)

    def test_rejected_scope_never_collects_incoming_references_or_bad_profile(self):
        self.seed_activity()
        original = RaiderIOCharacterActivity.objects.get().payload_json
        bad = payload('Wrong')
        bad['mythic_plus_best_runs'][0]['url'] = bad['mythic_plus_best_runs'][0]['url'].replace('/123-', '/999-')
        collector, transport = self.collector(bad)
        result = collector.refresh(self.player, self.season, include_profile=True, force=True)
        self.assertEqual(result['status'], 'invalid_payload')
        self.assertNotIn('profile_payload', result)
        self.assertEqual(RaiderIOCharacterActivity.objects.get().payload_json, original)
        self.assertFalse(RaiderIORunRoster.objects.filter(run_id=999).exists())
        self.season.is_active = False
        transport.reset_mock()
        result = collector.refresh(self.player, self.season, include_profile=True, force=True)
        self.assertEqual(result['status'], 'wrong_season')
        self.assertNotIn('profile_payload', result)
        transport.assert_not_called()

    def test_invalid_activity_preserves_local_refs_and_valid_profile(self):
        self.seed_activity()
        original = RaiderIOCharacterActivity.objects.get().payload_json
        for fault in ('wrong_current_season', 'bad_module'):
            with self.subTest(fault=fault):
                RaiderIORunRoster.objects.all().delete()
                data = activity_tests.PlayerActivityTests._profile_payload()
                data['mythic_plus_best_runs'][0]['url'] = data['mythic_plus_best_runs'][0]['url'].replace('/123-', '/999-')
                if fault == 'wrong_current_season':
                    data['mythic_plus_scores_by_season'][0]['season'] = 'season-old'
                else:
                    data['mythic_plus_recent_runs'] = 'bad-module'
                collector, transport = self.collector(data)
                result = collector.refresh(self.player, self.season, force=True, include_profile=True)
                self.assertEqual(result['status'], 'invalid_payload')
                self.assertEqual(result['profile_payload']['gear'], data['gear'])
                self.assertEqual(RaiderIOCharacterActivity.objects.get().payload_json, original)
                self.assertEqual(list(RaiderIORunRoster.objects.values_list('run_id', flat=True)), [123])
                self.assertEqual(transport.call_count, 2)

    def test_fresh_roster_429_stops_whole_activity_batch(self):
        self.seed_activity()
        transport = Mock(return_value=Mock(status_code=429, headers={}))
        collector = ActivityCollector(transport=transport)
        result = collector.refresh(self.player, self.season)
        self.assertEqual(result['status'], 'fresh')
        self.assertEqual(result['roster_results'][0]['status'], 'rate_limited')
        self.assertTrue(collector.stopped)
        self.assertEqual(collector.refresh(self.player, self.season, include_profile=True, force=True)['status'], 'batch_stopped')
        self.assertEqual(transport.call_count, 1)
