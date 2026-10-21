"""Regression coverage for authoritative empties, raid fallback and frozen scope."""
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.db import connection, transaction
from django.test import TransactionTestCase

from botend.models import (MonitorTask, PlayerSpecTopPlayer, SeasonMeta,
                           SpecDungeonRanking, SpecRaidRanking, WclCollectionCycle)
from botend.plugin_sync import claim_next_monitor_task, release_monitor_task_lease
from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor
from botend.services.wcl_collection_cycle import begin_collection, checkpoint_scope, freeze_samples, finish_collection

RANKING = 'botend.controller.plugins.portal.SpecDetailRankingMonitor'
AGGREGATION = 'botend.controller.plugins.portal.SpecDetailAggregationMonitor'


class WclAggregationPublicationRetryTests(TransactionTestCase):
    def setUp(self):
        MonitorTask.objects.update(is_active=False)
        self.now = datetime(2026, 10, 12, 8, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.season = SeasonMeta.objects.create(season_key='publication-retry',
            season_name='Publication retry', is_active=True, mplus_zone_id=1, raid_zone_id=2)
        self.task = MonitorTask.objects.create(name='SpecDetailAggregationMonitor',
            target='', type=889, is_active=True, last_scan_time=self.now - timedelta(days=1))
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk,
            class_name='Mage', spec_name='Fire', character_name='Player',
            realm='Realm', region='eu', rank=1, score=1234)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'aggregated' / str(self.season.pk) / 'Mage' / 'Fire' / 'leaderboard.json'
        for p in (self.settings(MEDIA_ROOT=self.directory.name),
                  patch(AGGREGATION + '.CLASS_SPEC_MAP', {'Mage': ['Fire']}),
                  patch('django.utils.timezone.now', return_value=self.now)):
            p.enable() if hasattr(p, 'enable') else p.start()
            self.addCleanup(p.disable if hasattr(p, 'disable') else p.stop)

    def test_leaderboard_write_failure_defers_claimed_cycle_then_recovers(self):
        cycle = begin_collection(self.season)
        freeze_samples(cycle, self.season, [('Mage', 'Fire')])
        self.assertTrue(finish_collection(cycle, {}))
        task = claim_next_monitor_task(now=self.now, lease_owner='publication-first')
        self.assertEqual(task.pk, self.task.pk)
        with self.assertLogs('LSpider', level='ERROR') as logs, \
                patch(AGGREGATION + '.atomic_dump_json',
                      side_effect=OSError('access_token=fixture-secret')) as write:
            self.assertFalse(SpecDetailAggregationMonitor(None, task).scan(''))
        write.assert_called_once()
        self.assertEqual(Path(write.call_args.args[0]), self.path)
        self.assertEqual(write.call_args.args[1]['players'][0]['id'], self.player.pk)
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'aggregating')
        self.assertIn('OSError', cycle.error)
        self.assertNotIn('fixture-secret', cycle.error)
        self.assertNotIn('fixture-secret', '\n'.join(logs.output))
        self.assertGreater(cycle.next_retry_at, self.now)
        self.assertIsNone(cycle.published_at)
        self.assertFalse(self.path.exists())
        self.assertTrue(release_monitor_task_lease(task.pk, task._monitor_task_lease_owner))
        self.assertIsNone(claim_next_monitor_task(now=self.now + timedelta(seconds=1)))
        self.assertIsNone(claim_next_monitor_task(now=cycle.next_retry_at - timedelta(microseconds=1)))
        with patch('django.utils.timezone.now', return_value=cycle.next_retry_at):
            retry = claim_next_monitor_task(now=cycle.next_retry_at, lease_owner='publication-retry')
            self.assertEqual(retry.pk, task.pk)
            self.assertTrue(SpecDetailAggregationMonitor(None, retry).scan(''))
        cycle.refresh_from_db()
        self.assertEqual(WclCollectionCycle.objects.count(), 1)
        self.assertEqual(cycle.phase, 'published')
        self.assertEqual(cycle.error, '')
        self.assertIsNone(cycle.next_retry_at)
        self.assertIsNotNone(cycle.published_at)
        self.assertEqual(json.loads(self.path.read_text())['players'][0]['id'], self.player.pk)

    def test_daily_leaderboard_without_ready_cycle_does_not_defer_collection(self):
        cycle = begin_collection(self.season)
        before = WclCollectionCycle.objects.filter(pk=cycle.pk).values().get()
        task = claim_next_monitor_task(now=self.now, lease_owner='daily-leaderboard')
        self.assertEqual(task.pk, self.task.pk)
        with self.assertLogs('LSpider', level='ERROR'), \
                patch(AGGREGATION + '.atomic_dump_json', side_effect=OSError('disk full')):
            self.assertFalse(SpecDetailAggregationMonitor(None, task).scan(''))
        self.assertEqual(WclCollectionCycle.objects.filter(pk=cycle.pk).values().get(), before)
        self.assertTrue(release_monitor_task_lease(task.pk, task._monitor_task_lease_owner))
        self.assertIsNone(claim_next_monitor_task(now=self.now + timedelta(seconds=1)))
        tomorrow = self.now + timedelta(days=1)
        with patch('django.utils.timezone.now', return_value=tomorrow):
            retry = claim_next_monitor_task(now=tomorrow, lease_owner='next-daily-leaderboard')
            self.assertEqual(retry.pk, task.pk)
            self.assertTrue(SpecDetailAggregationMonitor(None, retry).scan(''))
        self.assertEqual(WclCollectionCycle.objects.filter(pk=cycle.pk).values().get(), before)
        self.assertEqual(json.loads(self.path.read_text())['players'][0]['id'], self.player.pk)


class WclCycleEdgeCasesTests(TransactionTestCase):
    # Exercise real WCL transport admission: TestCase's outer atomic block
    # correctly forbids HTTP, including the burst events added to aggregation.
    def setUp(self):
        self.now = datetime(2026, 10, 12, 8, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.season = SeasonMeta.objects.create(season_key='edges', season_name='Edges',
            is_active=True, mplus_zone_id=1, raid_zone_id=2,
            mplus_encounters=[], raid_encounters=[{'id': 201, 'name': 'Old boss'}])
        self.monitor = SpecDetailRankingMonitor(None, None)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for p in (self.settings(MEDIA_ROOT=self.directory.name),
                  patch(RANKING + '.CLASS_SPEC_MAP', {'Mage': ['Fire']}),
                  patch(AGGREGATION + '.CLASS_SPEC_MAP', {'Mage': ['Fire']}),
                  patch(RANKING + '.time.sleep'),
                  patch(RANKING + '.timezone.now', return_value=self.now)):
            p.enable() if hasattr(p, 'enable') else p.start()
            self.addCleanup(p.disable if hasattr(p, 'disable') else p.stop)

    def old_raid(self):
        return SpecRaidRanking.objects.create(season_id=self.season.pk, boss_id=201,
            boss_name='Old boss', difficulty=5, class_name='Mage', spec_name='Fire',
            character_name='Player', realm='Realm', region='eu', report_code='Report',
            fight_id=1, dps=55, talents_json=[{'talentID': 999}])

    def page(self):
        return {'rankings': [{'name': 'Player', 'amount': 123,
            'server': {'name': 'Realm', 'region': 'eu'},
            'report': {'code': 'Report', 'fightID': 1}, 'talents': []}], 'hasMorePages': False}

    def test_raid_report_talent_fallback_is_preserved(self):
        old = self.old_raid()
        events = [{'fight': 1, 'sourceID': 7, 'specID': 63,
                   'source': {'id': 7, 'name': 'Player', 'server': 'Realm', 'type': 'Player'},
                   'talentTree': [{'id': 1234, 'rank': 1}]}]
        with patch.object(self.monitor, 'fetch_wcl_rankings', return_value=self.page()), \
             patch.object(self.monitor, 'fetch_wcl_combatant_info', return_value=events) as fetch:
            self.assertTrue(self.monitor.scan(''))
        old.refresh_from_db()
        self.assertEqual(old.talents_json, self.monitor.parse_wcl_talent_tree(events[0]['talentTree']))
        self.assertTrue(old.talents_json)
        self.assertEqual(fetch.call_count, 2)  # one bounded cache per difficulty

    def test_raid_quota_preserves_entire_boss_then_resumes(self):
        old = self.old_raid()
        before = SpecRaidRanking.objects.filter(pk=old.pk).values().get()
        def quota(*args):
            self.monitor._wcl_retry_at = self.now + timedelta(hours=1)
            self.monitor._wcl_last_error = 'budget_wait'
            return None
        with patch(RANKING + '.CLASS_SPEC_MAP', {'Mage': ['Fire', 'Frost']}), \
             patch.object(self.monitor, 'fetch_wcl_rankings', return_value=self.page()) as rankings, \
             patch.object(self.monitor, 'fetch_wcl_combatant_info', side_effect=quota):
            self.assertFalse(self.monitor.scan(''))
        rankings.assert_called_once()
        self.assertEqual(SpecRaidRanking.objects.filter(pk=old.pk).values().get(), before)
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual(cycle.phase, 'rankings')
        self.assertEqual(cycle.completed_scopes, [])
        self.assertEqual(cycle.next_retry_at, self.now + timedelta(hours=1))
        with patch(RANKING + '.timezone.now', return_value=cycle.next_retry_at), \
             patch.object(self.monitor, 'fetch_wcl_rankings', return_value=self.page()), \
             patch.object(self.monitor, 'fetch_wcl_combatant_info', return_value=[]):
            self.assertTrue(self.monitor.scan(''))
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'ready')

    def test_raid_transient_failure_is_not_cached_as_empty(self):
        for error in ('http_403', 'authentication', 'invalid_payload', 'network'):
            with self.subTest(error=error):
                cache = {}
                self.monitor._wcl_last_error = error
                with patch.object(self.monitor, 'fetch_wcl_combatant_info', return_value=None):
                    with self.assertRaises(ValueError):
                        self.monitor._parse_rank_talents(self.page()['rankings'][0], cache)
                self.assertEqual(cache, {})
        self.monitor._wcl_last_error = 'permission'
        with patch.object(self.monitor, 'fetch_wcl_combatant_info', return_value=None):
            self.assertEqual(self.monitor._parse_rank_talents(self.page()['rankings'][0], {}), [])

    def test_authoritative_empty_scopes_preserve_history_and_reach_ready(self):
        old = self.old_raid()
        self.season.mplus_encounters = [{'id': 101, 'name': 'Old dungeon'}]
        self.season.save()
        dungeon = SpecDungeonRanking.objects.create(season_id=self.season.pk, dungeon_id=101,
            dungeon_name='Old dungeon', class_name='Mage', spec_name='Fire',
            character_name='Old player', dps=44)
        old_values = SpecRaidRanking.objects.filter(pk=old.pk).values().get()
        dungeon_values = SpecDungeonRanking.objects.filter(pk=dungeon.pk).values().get()
        with patch.object(self.monitor, 'fetch_wcl_rankings', return_value={
                'rankings': [], 'hasMorePages': False}) as fetch:
            self.assertTrue(self.monitor.scan(''))
            self.assertFalse(self.monitor.scan(''))
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(SpecRaidRanking.objects.filter(pk=old.pk).values().get(), old_values)
        self.assertEqual(SpecDungeonRanking.objects.filter(pk=dungeon.pk).values().get(), dungeon_values)
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual(cycle.phase, 'ready')
        self.assertEqual(set(cycle.completed_scopes), {'dungeon:101', 'raid:5:201', 'raid:4:201'})
        self.assertEqual(cycle.selected_samples, {'Mage/Fire': {'101': [dungeon.pk]}})

    def test_invalid_raid_combatant_payload_preserves_old_boss(self):
        old = self.old_raid()
        before = SpecRaidRanking.objects.filter(pk=old.pk).values().get()
        for events in ([{}], [{'fight': 1, 'sourceID': 7, 'specID': 63,
                              'source': {'id': 7, 'name': 'Player', 'server': 'Realm'},
                              'talentTree': {}}]):
            with self.subTest(events=events):
                WclCollectionCycle.objects.all().delete()
                with patch.object(self.monitor, 'fetch_wcl_rankings', return_value=self.page()), \
                     patch.object(self.monitor, 'fetch_wcl_combatant_info', return_value=events):
                    self.assertFalse(self.monitor.scan(''))
                self.assertEqual(SpecRaidRanking.objects.filter(pk=old.pk).values().get(), before)
                self.assertEqual(WclCollectionCycle.objects.get().completed_scopes, [])

    def test_bad_raid_empty_container_never_checkpoints(self):
        for page in ({}, {'rankings': []}, {'rankings': [], 'hasMorePages': True},
                     {'rankings': 'invalid', 'hasMorePages': False}):
            with self.subTest(page=page):
                WclCollectionCycle.objects.all().delete()
                with patch.object(self.monitor, 'fetch_wcl_rankings', return_value=page):
                    self.assertFalse(self.monitor.scan(''))
                cycle = WclCollectionCycle.objects.get()
                self.assertEqual(cycle.phase, 'rankings')
                self.assertEqual(cycle.completed_scopes, [])

    def test_nonempty_raid_invalid_pagination_preserves_boss_and_resumes(self):
        for pagination in ({}, {'hasMorePages': None}, {'hasMorePages': 'false'},
                           {'hasMorePages': 0}, {'hasMorePages': 1}):
            with self.subTest(pagination=pagination):
                WclCollectionCycle.objects.all().delete()
                SpecRaidRanking.objects.all().delete()
                old = self.old_raid()
                old.pk = None
                old.spec_name = 'Frost'
                old.save()
                boss = SpecRaidRanking.objects.filter(season_id=self.season.pk, boss_id=201)
                before = list(boss.order_by('pk').values())
                page = self.page()
                page['rankings'][0]['talents'] = [{'talentID': 1234, 'points': 1}]
                invalid = {'rankings': page['rankings'], **pagination}
                valid_response = {'worldData': {'encounter': {'characterRankings': page}}}
                invalid_response = {'worldData': {'encounter': {'characterRankings': invalid}}}
                with patch(RANKING + '.CLASS_SPEC_MAP', {'Mage': ['Fire', 'Frost']}), \
                     patch.object(self.monitor, '_wcl_graphql',
                                  side_effect=lambda query, variables: (
                                      invalid_response if 'specName: "Frost"' in query
                                      and 'difficulty: 5,' in query else valid_response)) as fetch:
                    self.assertFalse(self.monitor.scan(''))
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(list(boss.order_by('pk').values()), before)
                cycle = WclCollectionCycle.objects.get()
                self.assertEqual(cycle.phase, 'rankings')
                self.assertEqual(cycle.completed_scopes, [])
                self.assertGreater(cycle.next_retry_at, self.now)

                with patch(RANKING + '.CLASS_SPEC_MAP', {'Mage': ['Fire', 'Frost']}), \
                     patch(RANKING + '.timezone.now', return_value=cycle.next_retry_at), \
                     patch.object(self.monitor, '_wcl_graphql', return_value=valid_response) as fetch:
                    self.assertTrue(self.monitor.scan(''))
                self.assertEqual(fetch.call_count, 4)
                cycle.refresh_from_db()
                self.assertEqual(WclCollectionCycle.objects.count(), 1)
                self.assertEqual(cycle.phase, 'ready')
                self.assertEqual(set(cycle.completed_scopes), {'raid:5:201', 'raid:4:201'})
                self.assertEqual(boss.count(), 4)
                self.assertEqual(set(boss.values_list('dps', flat=True)), {123})

    def test_nonempty_raid_has_more_true_keeps_single_page_sampling(self):
        self.old_raid()
        page = self.page()
        page['rankings'][0]['talents'] = [{'talentID': 1234, 'points': 1}]
        page['hasMorePages'] = True
        response = {'worldData': {'encounter': {'characterRankings': page}}}
        with patch.object(self.monitor, '_wcl_graphql', return_value=response) as fetch:
            self.assertTrue(self.monitor.scan(''))
        self.assertEqual(fetch.call_count, 2)
        for call, difficulty in zip(fetch.call_args_list, (5, 4)):
            self.assertIn('characterRankings(', call.args[0])
            self.assertIn('page: 1', call.args[0])
            self.assertIn(f'difficulty: {difficulty},', call.args[0])
        self.assertEqual(list(SpecRaidRanking.objects.order_by('difficulty').values_list(
            'difficulty', 'character_name', 'dps')), [(4, 'Player', 123), (5, 'Player', 123)])
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual(cycle.phase, 'ready')
        self.assertEqual(set(cycle.completed_scopes), {'raid:5:201', 'raid:4:201'})

    def test_frozen_scope_survives_season_changes_and_manual_replay(self):
        from botend.services.raid_burst_snapshot import load_published
        from botend.services.wcl_api_budget import graphql_request

        settings = self.settings(
            MEDIA_ROOT=str(Path(self.directory.name) / 'media'),
            RAID_BURST_SNAPSHOT_ROOT=str(Path(self.directory.name) / 'private'),
            WCL_V2_CONFIG={'client_id': 'freeze-fixture', 'client_secret': 'fixture'})
        settings.enable()
        self.addCleanup(settings.disable)
        event_kinds = []

        def post(url, **kwargs):
            # Keep real budget admission, event fetching, fencing and publication.
            # Only the external HTTP boundary is replaced with a valid log.
            self.assertFalse(connection.in_atomic_block)
            if url.endswith('/oauth/token'):
                return Mock(status_code=200, json=lambda: {
                    'access_token': 'fixture', 'expires_in': 3600})
            variables = kwargs['json']['variables'] or {}
            quota = dict(limitPerHour=18000, pointsSpentThisHour=20,
                         pointsResetIn=3600, currentRequest={'total': 1})
            data = {'rateLimitData': quota, '_wclBudget': quota}
            if variables.get('code'):
                self.assertEqual(variables['code'], 'Report')
                self.assertEqual(variables['ids'], [1])
                kind = variables.get('kind')
                event_kinds.append(kind)
                if kind is None:
                    report = {'fights': [{'id': 1, 'encounterID': 201,
                        'difficulty': 5, 'kill': True, 'startTime': 10000,
                        'endTime': 310000, 'phaseTransitions': []}],
                        'masterData': {'actors': [{'id': 7, 'name': 'Player',
                            'server': 'Realm', 'subType': 'Mage'}]}}
                else:
                    events = ([{'fight': 1, 'sourceID': 7, 'specID': 63,
                                'auras': [{'ability': 2825}]}]
                              if kind == 'CombatantInfo' else [])
                    report = {'page': {'data': events, 'nextPageTimestamp': None}}
                data['reportData'] = {'report': report}
            return Mock(status_code=200, json=lambda: {'data': data})

        transport = patch('botend.services.wcl_api_budget.requests.post', side_effect=post)
        http = transport.start()
        self.addCleanup(transport.stop)
        # Explicitly retain the prohibition: nested transactions may not send HTTP.
        with transaction.atomic():
            result = graphql_request('freeze-fixture', 'fixture', 'query { x }', {})
        self.assertEqual(result.error, 'transaction_context')
        http.assert_not_called()
        self.old_raid()
        self.season.mplus_encounters = [{'id': 101, 'name': 'Old dungeon'}]
        self.season.save()
        SpecDungeonRanking.objects.create(season_id=self.season.pk, dungeon_id=101,
            dungeon_name='Old dungeon', class_name='Mage', spec_name='Fire', character_name='Old', dps=44)
        cycle = begin_collection(self.season)
        for scope in ('dungeon:101', 'raid:5:201', 'raid:4:201'):
            checkpoint_scope(cycle, scope)
        freeze_samples(cycle, self.season, [('Mage', 'Fire')])
        finish_collection(cycle, {})
        self.season.mplus_encounters = [{'id': 102, 'name': 'New dungeon'}]
        self.season.raid_encounters = [{'id': 202, 'name': 'New boss'}]
        self.season.raid_zones = [{'id': 9, 'name': 'New zone', 'encounters': self.season.raid_encounters}]
        self.season.save()
        monitor = SpecDetailAggregationMonitor(None, None)
        with patch.object(monitor, '_aggregate_leaderboard'), \
             patch.object(monitor, '_aggregate_dungeon', wraps=monitor._aggregate_dungeon) as dungeon, \
             patch.object(monitor, '_aggregate_raid', wraps=monitor._aggregate_raid) as raid:
            self.assertTrue(monitor.scan(''))
        self.assert_frozen_season(dungeon.call_args.args[0])
        self.assert_frozen_season(raid.call_args.args[0])
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'published')
        self.assertEqual(event_kinds, [None, 'Casts', 'Buffs', 'CombatantInfo'])
        payload, options = load_published(f'{self.season.pk}/Mage/Fire/5/201')
        self.assertEqual(payload['attempted'], 1)
        self.assertEqual(payload['samples'][0]['player']['name'], 'Player')
        self.assertEqual({item['key'].split('/')[-1] for item in options}, {'201'})
        http.reset_mock()
        MonitorTask.objects.create(name='SpecDetailAggregationMonitor', target='', type=889)
        from botend.management.commands.aggregate_spec_stats import Command
        with patch.object(Command, '_aggregate_leaderboard'), \
             patch.object(Command, '_aggregate_dungeon', wraps=Command._aggregate_dungeon) as dungeon, \
             patch.object(Command, '_aggregate_raid', wraps=Command._aggregate_raid) as raid:
            call_command('aggregate_spec_stats', class_name='Mage', spec_name='Fire')
        self.assert_frozen_season(dungeon.call_args.args[0])
        self.assert_frozen_season(raid.call_args.args[0])
        http.assert_not_called()  # replay reuses the complete, validated snapshot
        self.assertEqual(load_published(f'{self.season.pk}/Mage/Fire/5/201')[0], payload)
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'published')
        self.season.refresh_from_db()
        self.assertEqual(self.season.mplus_encounters[0]['id'], 102)

    def assert_frozen_season(self, season):
        self.assertEqual(season.mplus_encounters, [{'id': 101, 'name': 'Old dungeon'}])
        self.assertEqual(season.raid_encounters, [{'id': 201, 'name': 'Old boss'}])
        self.assertNotIn(202, [enc['id'] for zone in season.raid_zones for enc in zone['encounters']])
