import copy
import io
import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.db import connection, IntegrityError, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import PlayerSpecTopPlayer, SeasonMeta, RaiderIOCharacterActivity
from botend.services.player_raiderio_activity import (
    ActivityCollector, activity_fields, project_player_activity, top20_candidates,
)


def payload(name='Tester'):
    run = {'dungeon': "Kings' Rest", 'mythic_level': 20, 'clear_time_ms': 1234567,
           'score': 401.2, 'num_keystone_upgrades': 0,
           'completed_at': '2026-10-10T12:00:00Z',
           'url': 'https://raider.io/mythic-plus-runs/season-mn-2/123-20-kings-rest',
           'spec': None, 'role': None}
    return {'name': name, 'region': 'eu', 'realm': 'Test Realm',
            'profile_url': f'https://raider.io/characters/eu/test-realm/{name}',
            'last_crawled_at': '2026-10-10T13:00:00Z',
            'mythic_plus_scores_by_season': [{'season': 'season-mn-2'}],
            'mythic_plus_best_runs': [run], 'mythic_plus_recent_runs': [],
            'mythic_plus_dungeon_run_counts': [
                {'zone_id': 99999, 'dungeon': "Kings' Rest", 'season_runs_total': 10, 'season_runs_timed': 0}],
            'raid_progression': {'the-venomous-abyss': {'total_bosses': 8, 'normal_bosses_killed': 0,
                                'heroic_bosses_killed': 7, 'mythic_bosses_killed': None}}}


class PlayerActivityTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='S2',
            rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        self.player = self.make_player('Holy')

    def make_player(self, spec, name='Tester', rank=1):
        return PlayerSpecTopPlayer.objects.create(season_id=self.season.pk,
            region='eu', realm='Test Realm', character_name=name, class_name='Priest',
            spec_name=spec, rank=rank, profile_url=f'https://raider.io/characters/eu/test-realm/{name}',
            gear_json=[{'id': 123}], stats_json={'haste': 123}, talents_json=[{'id': 7}],
            last_updated=timezone.now())

    def transport(self, data=None, status=200):
        return Mock(return_value=Mock(status_code=status, headers={}, json=Mock(return_value=data or payload())))

    def test_single_request_four_modules_shared_identity_and_local_projection(self):
        transport = self.transport()
        original = PlayerSpecTopPlayer.objects.values().get(pk=self.player.pk)
        collector = ActivityCollector(transport=transport)
        self.assertEqual(collector.refresh(self.player, self.season)['status'], 'ready')
        sibling = self.make_player('Discipline', name='TESTER')
        self.assertEqual(collector.refresh(sibling, self.season)['status'], 'fresh')
        self.assertEqual(transport.call_count, 1)
        fields = transport.call_args.kwargs['params']['fields']
        self.assertEqual(fields, activity_fields(self.season.rio_season))
        self.assertEqual(RaiderIOCharacterActivity.objects.count(), 1)
        self.assertEqual(PlayerSpecTopPlayer.objects.values().get(pk=self.player.pk), original)
        with patch('requests.get', side_effect=AssertionError('page must stay local')):
            view = project_player_activity(self.player)
        self.assertEqual(view['status'], 'ready')
        self.assertEqual(view['best_runs']['rows'][0]['dungeon_name'], '诸王之眠')
        self.assertEqual(view['best_runs']['rows'][0]['clear_time'], '20:34')
        self.assertEqual(view['best_runs']['rows'][0]['spec_label'], '')
        self.assertIs(view['best_runs']['rows'][0]['timed'], False)
        self.assertEqual(view['recent_runs'], {'status': 'ready', 'rows': []})
        self.assertEqual(view['dungeon_counts']['timed_percent'], 0.0)
        self.assertEqual(view['raid_progression']['rows'][0]['total_bosses'], 8)
        self.assertIsNone(view['raid_progression']['rows'][0]['mythic'])
        self.assertEqual(view['raid_progression']['rows'][0]['name'], '烈毒之渊')
        from botend.services.spec_stats_service import SpecStatsService
        with patch('requests.get', side_effect=AssertionError('page must stay local')) as network:
            detail = SpecStatsService.get_player_detail(self.player.pk)
        network.assert_not_called()
        self.assertEqual(detail['raiderio_activity'], view)

    def test_partial_missing_and_errors_preserve_last_good(self):
        collector = ActivityCollector(transport=self.transport())
        collector.refresh(self.player, self.season)
        old = RaiderIOCharacterActivity.objects.get().payload_json
        partial = payload()
        del partial['mythic_plus_best_runs']
        partial['mythic_plus_dungeon_run_counts'] = []
        collector.transport = self.transport(partial)
        self.assertEqual(collector.refresh(self.player, self.season, force=True)['status'], 'partial')
        row = RaiderIOCharacterActivity.objects.get()
        self.assertEqual(row.payload_json['mythic_plus_best_runs'], old['mythic_plus_best_runs'])
        self.assertEqual(row.payload_json['mythic_plus_dungeon_run_counts'], [])
        saved = copy.deepcopy(row.payload_json)
        bad = payload(); bad['mythic_plus_recent_runs'] = 'not-a-list'
        collector.transport = self.transport(bad)
        self.assertEqual(collector.refresh(self.player, self.season, force=True)['status'], 'invalid_payload')
        row.refresh_from_db()
        self.assertEqual(row.payload_json, saved)
        self.assertEqual(project_player_activity(self.player)['status'], 'stale')
        self.assertEqual(collector.refresh(self.player, self.season)['status'], 'backoff')

    def test_wrong_identity_season_unsafe_run_url_and_bad_counts_rejected(self):
        bad_payloads = []
        bad = payload('Wrong'); bad_payloads.append(bad)
        bad = payload(); bad['region'] = 'us'; bad_payloads.append(bad)
        bad = payload(); bad['mythic_plus_scores_by_season'][0]['season'] = 'season-old'; bad_payloads.append(bad)
        bad = payload(); bad['mythic_plus_best_runs'][0]['url'] = 'https://evil.test/mythic-plus-runs/season-mn-2/1'; bad_payloads.append(bad)
        bad = payload(); bad['mythic_plus_best_runs'][0]['url'] = 'https://raider.io/mythic-plus-runs/season-old/1'; bad_payloads.append(bad)
        bad = payload(); bad['mythic_plus_dungeon_run_counts'][0]['season_runs_total'] = True; bad_payloads.append(bad)
        for bad in bad_payloads:
            with self.subTest(payload=bad):
                result = ActivityCollector(transport=self.transport(bad)).refresh(self.player, self.season, force=True)
                self.assertEqual(result['status'], 'invalid_payload')
        self.assertEqual(RaiderIOCharacterActivity.objects.get().payload_json, {})

    def test_missing_and_empty_are_distinct_unknown_totals_not_zero(self):
        data = payload(); del data['mythic_plus_recent_runs']
        del data['mythic_plus_dungeon_run_counts'][0]['season_runs_total']
        collector = ActivityCollector(transport=self.transport(data))
        self.assertEqual(collector.refresh(self.player, self.season)['status'], 'partial')
        view = project_player_activity(self.player)
        self.assertEqual(view['recent_runs']['status'], 'missing')
        self.assertIsNone(view['dungeon_counts']['total'])
        self.assertIsNone(view['dungeon_counts']['timed_percent'])
        self.assertEqual(view['dungeon_counts']['timed'], 0)

    def test_rate_limit_stops_batch_even_force_and_saves_retry(self):
        transport = self.transport(status=429)
        collector = ActivityCollector(transport=transport)
        other = self.make_player('Shadow', 'Other')
        self.assertEqual(collector.refresh(self.player, self.season)['status'], 'rate_limited')
        self.assertEqual(collector.refresh(other, self.season, force=True)['status'], 'batch_stopped')
        self.assertEqual(transport.call_count, 1)
        self.assertGreater(RaiderIOCharacterActivity.objects.get().next_attempt_at, timezone.now())

    def test_exact_identity_digest_direct_create_and_save(self):
        fields = {'season': self.season, 'region': 'eu', 'realm_slug': 'test-realm'}
        plain = RaiderIOCharacterActivity.objects.create(**fields, character_name='Tester')
        accent = RaiderIOCharacterActivity.objects.create(**fields, character_name='Téstér')
        self.assertRegex(plain.identity_digest, r'^[0-9a-f]{64}$')
        self.assertNotEqual(plain.identity_digest, accent.identity_digest)
        original_digest = plain.identity_digest
        plain.identity_digest = 'incorrect'
        plain.save(update_fields=['identity_digest'])
        plain.refresh_from_db()
        self.assertEqual(plain.identity_digest, original_digest)
        for equivalent in ('TÉSTÉR', 'Te\u0301ste\u0301r'):
            with self.assertRaises(IntegrityError), transaction.atomic():
                RaiderIOCharacterActivity.objects.create(**fields, character_name=equivalent)
        old_digest = accent.identity_digest
        accent.character_name = 'Other'
        accent.save(update_fields=['character_name'])
        accent.refresh_from_db()
        self.assertNotEqual(accent.identity_digest, old_digest)
        self.assertEqual(accent.character_name, 'other')
        with self.assertRaises(IntegrityError), transaction.atomic():
            RaiderIOCharacterActivity.objects.create(**fields, character_name='OTHER')
        for changed in ({'region': 'us'}, {'realm_slug': 'other-realm'}):
            other = RaiderIOCharacterActivity.objects.create(**{**fields, **changed}, character_name='Tester')
            self.assertNotEqual(other.identity_digest, plain.identity_digest)

    def test_accent_identity_collector_and_projection_use_exact_key(self):
        collector = ActivityCollector(transport=self.transport())
        with CaptureQueriesContext(connection) as queries:
            plain = collector.refresh(self.player, self.season)
        lookup = next(q['sql'] for q in queries if q['sql'].startswith('SELECT')
                      and 'wow_raiderio_character_activity' in q['sql'])
        self.assertIn('identity_digest', lookup.split('WHERE', 1)[1])
        accented = self.make_player('Discipline', name='Téstér')
        data = payload('Téstér')
        data['mythic_plus_best_runs'][0]['mythic_level'] = 25
        collector.transport = self.transport(data)
        accent = collector.refresh(accented, self.season)
        self.assertEqual(accent['status'], 'ready')
        self.assertNotEqual(plain['activity_id'], accent['activity_id'])
        self.assertEqual(RaiderIOCharacterActivity.objects.count(), 2)
        for player, level in ((self.player, 20), (accented, 25)):
            with CaptureQueriesContext(connection) as queries:
                view = project_player_activity(player)
            lookup = next(q['sql'] for q in queries if 'wow_raiderio_character_activity' in q['sql'])
            self.assertIn('identity_digest', lookup.split('WHERE', 1)[1])
            self.assertEqual(view['best_runs']['rows'][0]['level'], level)

    def test_unique_key_and_expiring_lease(self):
        collector = ActivityCollector(transport=self.transport())
        collector.refresh(self.player, self.season)
        row = RaiderIOCharacterActivity.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            RaiderIOCharacterActivity.objects.create(season=self.season, region=row.region,
                realm_slug=row.realm_slug, character_name=row.character_name)
        row.status = 'fetching'; row.next_attempt_at = timezone.now() + timedelta(minutes=1)
        row.save()
        self.assertEqual(collector.refresh(self.player, self.season, force=True)['status'], 'busy')
        row.next_attempt_at = timezone.now() - timedelta(seconds=1); row.save()
        self.assertEqual(collector.refresh(self.player, self.season)['status'], 'ready')

    def test_candidate_discovery_is_bounded_scalar_then_identity_only(self):
        self.make_player('Holy', 'Historical', rank=None)
        self.make_player('Holy', 'Beyond', rank=21)
        self.make_player('Discipline', name='TESTER')
        with CaptureQueriesContext(connection) as queries:
            candidates = top20_candidates(self.season, class_name='Priest')
        self.assertEqual(len(candidates), 1)
        self.assertEqual(set(candidates[0]['player_ids']), set(PlayerSpecTopPlayer.objects.filter(rank=1).values_list('pk', flat=True)))
        first = queries[0]['sql']
        self.assertIn('LIMIT 20', first)
        self.assertNotIn('gear_json', ' '.join(q['sql'] for q in queries))
        self.assertNotIn('stats_json', ' '.join(q['sql'] for q in queries))

    def test_monitor_combines_profile_request_and_rank_refresh_only_writes_activity(self):
        import tempfile
        from django.test import override_settings
        from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
        monitor = SpecDetailPlayerMonitor(None, None)
        data = payload(); data['gear'] = {'item_level_equipped': 300, 'items': {}}
        data['talents'] = []
        transport = self.transport(data)
        original = PlayerSpecTopPlayer.objects.values().get(pk=self.player.pk)
        with patch('requests.get', transport):
            combined = monitor._fetch_profile_with_activity(self.player, self.season)
        self.assertIn('gear', combined)
        self.assertEqual(transport.call_count, 1)
        self.assertIn('gear,talents,mythic_plus_best_runs:all', transport.call_args.kwargs['params']['fields'])
        RaiderIOCharacterActivity.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        rankings = [{'score': 4000, 'character': {'name': 'Tester',
            'class': {'name': 'Priest'}, 'spec': {'name': 'Holy', 'role': 'healer'},
            'realm': {'name': 'Test Realm', 'slug': 'test-realm'}, 'region': {'slug': 'eu'}}}]
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root), patch('requests.get', transport):
            monitor.preload_peak_rankings(rio_season=self.season.rio_season,
                class_name='Priest', spec_name='Holy', rankings=rankings)
        self.assertEqual(transport.call_count, 2)
        current = PlayerSpecTopPlayer.objects.values().get(pk=self.player.pk)
        for field in ('gear_json', 'talents_json', 'stats_json', 'last_updated'):
            self.assertEqual(current[field], original[field])
        self.assertEqual(RaiderIOCharacterActivity.objects.get().status, 'ready')

    def _scan_profile_response(self, data):
        """Exercise scan -> ranking parser -> enrichment -> ORM, not mocked services."""
        from django.test import override_settings
        from botend.models import MonitorTask
        from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
        task = MonitorTask.objects.create(name='activity regression', target='')
        monitor = SpecDetailPlayerMonitor(None, task)
        monitor.PLAYER_RANKING_LIMIT = 1
        ranking = {'score': 4000, 'character': {'name': 'Tester',
            'path': '/characters/eu/test-realm/Tester',
            'realm': {'name': 'Test Realm'}, 'region': {'slug': 'eu'}}}
        profile_requests = []

        def transport(url, **kwargs):
            if url.endswith('/characters/profile'):
                profile_requests.append(kwargs['params']['fields'])
                body = data
            else:
                self.assertIn('raider.io', url)
                body = {'rankings': {'rankedCharacters': [ranking]}}
            return Mock(status_code=200, headers={}, json=Mock(return_value=body))

        module = 'botend.controller.plugins.portal.SpecDetailPlayerMonitor'
        with override_settings(BATTLENET_CONFIG={}), patch(module + '.CLASS_SPEC_MAP', {'Priest': ['Holy']}), \
                patch('requests.get', side_effect=transport), patch('time.sleep'):
            self.assertTrue(monitor.scan(''))
        task.refresh_from_db()
        self.assertIn('@players=1@', task.flag)
        self.player.refresh_from_db()
        return profile_requests

    @staticmethod
    def _profile_payload(name='Tester'):
        data = payload(name)
        data['gear'] = {'item_level_equipped': 300,
                        'items': {'head': {'item_id': 999, 'item_level': 300}}}
        data['talents'] = {'selected': [{'id': 888, 'spell': {'id': 777, 'name': 'Test talent'}}]}
        return data

    def test_daily_scan_invalid_activity_still_updates_valid_profile(self):
        ActivityCollector(transport=self.transport()).refresh(self.player, self.season)
        row = RaiderIOCharacterActivity.objects.get()
        saved = copy.deepcopy(row.payload_json)
        fetched_at = row.fetched_at
        row.next_attempt_at = timezone.now() - timedelta(seconds=1)
        row.save()
        data = self._profile_payload()
        data['mythic_plus_recent_runs'] = 'invalid-module'
        requests = self._scan_profile_response(data)
        self.assertEqual([item['id'] for item in self.player.gear_json], [999])
        self.assertEqual(self.player.talents_json[0]['talent_id'], 888)
        self.assertEqual(len(requests), 1)
        self.assertIn('gear,talents,mythic_plus_best_runs:all', requests[0])
        row.refresh_from_db()
        self.assertEqual(row.payload_json, saved)
        self.assertEqual(row.fetched_at, fetched_at)
        self.assertEqual(row.last_error, 'invalid_payload')
        self.assertEqual(row.status, 'error')

    def test_daily_scan_busy_activity_refreshes_profile_without_touching_lease(self):
        ActivityCollector(transport=self.transport()).refresh(self.player, self.season)
        row = RaiderIOCharacterActivity.objects.get()
        row.status = 'fetching'
        row.next_attempt_at = timezone.now() + timedelta(minutes=1)
        row.save()
        original = RaiderIOCharacterActivity.objects.values().get(pk=row.pk)
        requests = self._scan_profile_response(self._profile_payload())
        self.assertEqual([item['id'] for item in self.player.gear_json], [999])
        self.assertEqual(self.player.talents_json[0]['talent_id'], 888)
        self.assertEqual(requests, ['gear,talents'])
        self.assertEqual(RaiderIOCharacterActivity.objects.values().get(pk=row.pk), original)

    def test_daily_scan_wrong_identity_preserves_existing_profile(self):
        self.player.stats_crawl_status = 1
        self.player.save()
        original = PlayerSpecTopPlayer.objects.values().get(pk=self.player.pk)
        self._scan_profile_response(self._profile_payload('Wrong'))
        for field in ('gear_json', 'talents_json', 'talent_build_code', 'item_level',
                      'avatar_url', 'profile_url', 'stats_json', 'last_updated'):
            self.assertEqual(getattr(self.player, field), original[field], field)
        self.assertEqual(RaiderIOCharacterActivity.objects.get().last_error, 'invalid_payload')

    def test_deferred_profile_also_requires_verified_identity(self):
        collector = ActivityCollector(transport=self.transport())
        collector.refresh(self.player, self.season)
        row = RaiderIOCharacterActivity.objects.get()
        for status in ('ready', 'error', 'fetching'):
            with self.subTest(status=status):
                row.status = status
                row.next_attempt_at = timezone.now() + timedelta(minutes=1)
                row.save()
                original = RaiderIOCharacterActivity.objects.values().get(pk=row.pk)
                collector.transport = self.transport(self._profile_payload('Wrong'))
                result = collector.refresh(self.player, self.season, include_profile=True)
                self.assertNotIn('profile_payload', result)
                self.assertEqual(result['status'], 'invalid_payload')
                self.assertEqual(RaiderIOCharacterActivity.objects.values().get(pk=row.pk), original)

    def test_anonymous_command_is_unresolved_without_request_or_row(self):
        self.player.profile_url = ''; self.player.realm = 'anonymous'; self.player.save()
        out = io.StringIO()
        with patch('requests.get', side_effect=AssertionError('anonymous identity is private')):
            call_command('sync_player_raiderio_activity', apply=True, class_name='Priest', spec='Holy', stdout=out)
        self.assertEqual(json.loads(out.getvalue())['counts'], {'unresolved': 1})
        self.assertEqual(project_player_activity(self.player)['status'], 'unavailable')
        self.assertFalse(RaiderIOCharacterActivity.objects.exists())

    def test_command_default_dry_run_then_apply_json_outcomes(self):
        out = io.StringIO()
        with patch('requests.get', side_effect=AssertionError('dry run is local')):
            call_command('sync_player_raiderio_activity', class_name='Priest', spec='Holy', stdout=out)
        report = json.loads(out.getvalue())
        self.assertFalse(report['apply'])
        self.assertEqual(report['results'][0]['player_ids'], [self.player.pk])
        self.assertFalse(RaiderIOCharacterActivity.objects.exists())
        out = io.StringIO()
        with patch('requests.get', self.transport()):
            call_command('sync_player_raiderio_activity', apply=True, class_name='Priest', spec='Holy', stdout=out)
        report = json.loads(out.getvalue())
        self.assertEqual(report['results'][0]['status'], 'ready')
        self.assertNotIn('Tester', out.getvalue())
        self.assertEqual(RaiderIOCharacterActivity.objects.count(), 1)
