from datetime import datetime, timedelta
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import CommandError
from botend.tests.aggregation_cache_helpers import isolate_work_cache
from django.test import TestCase, override_settings
from django.utils import timezone

from botend.models import MonitorTask, SeasonMeta, SpecDungeonRanking, WclCollectionCycle
from botend.plugin_sync import claim_next_monitor_task, release_monitor_task_lease
from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor

RANKING = 'botend.controller.plugins.portal.SpecDetailRankingMonitor'
AGGREGATION = 'botend.controller.plugins.portal.SpecDetailAggregationMonitor'
COMBATANTS = 'botend.services.wcl_combatant_snapshot'


class WclCollectionCycleTests(TestCase):
    def setUp(self):
        isolate_work_cache(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.media = directory.name
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        MonitorTask.objects.update(is_active=False)
        self.now = datetime(2026, 10, 12, 8, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.season = SeasonMeta.objects.create(season_key='cycle-test', season_name='Test',
            mplus_zone_id=1, raid_zone_id=2, is_active=True,
            mplus_encounters=[{'id': 101, 'name': 'One'}, {'id': 102, 'name': 'Two'}])
        self.ranking = MonitorTask.objects.create(name='SpecDetailRankingMonitor', target='', type=881,
            is_active=True, last_scan_time=self.now)
        self.aggregation = MonitorTask.objects.create(name='SpecDetailAggregationMonitor', target='', type=882,
            is_active=True, last_scan_time=self.now)

    def cycle(self, **kwargs):
        defaults = dict(season=self.season, slot=self.now.replace(hour=3), owner='initial')
        defaults.update(kwargs)
        return WclCollectionCycle.objects.create(**defaults)

    def monitor(self):
        return SpecDetailRankingMonitor(None, self.ranking)

    def test_real_scheduler_resumes_quota_wait_despite_last_scan(self):
        cycle = self.cycle(next_retry_at=self.now + timedelta(hours=1))
        self.assertIsNone(claim_next_monitor_task(now=self.now))
        task = claim_next_monitor_task(now=cycle.next_retry_at, lease_owner='next')
        self.assertIsNotNone(task)
        self.assertEqual(task.pk, self.ranking.pk)

    def test_ready_after_six_is_immediately_due_and_crash_reclaims(self):
        self.cycle(phase='ready', collected_at=self.now)
        first = claim_next_monitor_task(now=self.now, lease_owner='first', lease_seconds=60)
        self.assertIsNotNone(first)
        self.assertEqual(first.pk, self.aggregation.pk)
        WclCollectionCycle.objects.update(phase='aggregating')
        again = claim_next_monitor_task(now=self.now + timedelta(seconds=61), lease_owner='second')
        self.assertEqual(again.pk, self.aggregation.pk)

    def test_next_day_rankings_cannot_replace_unpublished_cycle(self):
        self.cycle(phase='ready', collected_at=self.now)
        self.aggregation.is_active = False
        self.aggregation.save()
        self.assertIsNone(claim_next_monitor_task(now=self.now + timedelta(days=1)))
        with patch(RANKING + '.timezone.now', return_value=self.now + timedelta(days=1)), \
                patch.object(SpecDetailRankingMonitor, '_collect_dungeon_rankings') as collect:
            self.assertFalse(self.monitor().scan(''))
        collect.assert_not_called()
        self.assertEqual(WclCollectionCycle.objects.count(), 1)

    def test_quota_stops_short_retries(self):
        monitor = self.monitor()
        def quota(*args, **kwargs):
            monitor._wcl_retry_at = self.now + timedelta(hours=1)
            monitor._wcl_last_error = 'rate_limit'
            return None
        with patch.object(monitor, 'fetch_wcl_rankings', side_effect=quota) as fetch, \
                patch(RANKING + '.time.sleep') as sleep:
            self.assertIsNone(monitor._fetch_rankings_with_retry(101, 'Mage', 'Fire'))
        self.assertEqual(fetch.call_count, 1)
        sleep.assert_not_called()

    def test_checkpoint_resume_freezes_once_after_all_rankings(self):
        monitor = self.monitor()
        requests = []
        def fetch(enc_id, *args, **kwargs):
            requests.append(enc_id)
            if enc_id == 102 and requests.count(102) == 1:
                monitor._wcl_retry_at = self.now + timedelta(hours=1)
                return None
            monitor._wcl_retry_at = None
            return {'rankings': [{'name': 'Player', 'amount': 1234, 'hardModeLevel': 12,
                'server': {'name': 'Realm', 'region': 'EU'}, 'report': {'code': 'report', 'fightID': enc_id}}],
                'hasMorePages': False}
        with patch(RANKING + '.CLASS_SPEC_MAP', {'Mage': ['Fire']}), \
                patch(RANKING + '.time.sleep'), \
                patch(RANKING + '.timezone.now', return_value=self.now), \
                patch.object(monitor, 'fetch_wcl_rankings', side_effect=fetch), \
                patch(COMBATANTS + '.collect_dungeon_combatants', return_value={'failed': 0, 'pending': 0, 'missing': 2}) as logs:
            self.assertFalse(monitor.scan(''))
            cycle = WclCollectionCycle.objects.get()
            self.assertEqual(cycle.phase, 'rankings')
            self.assertEqual(cycle.completed_scopes, ['dungeon:101'])
            self.assertEqual(cycle.next_retry_at, self.now + timedelta(hours=1))
            logs.assert_not_called()
            with patch(RANKING + '.timezone.now', return_value=self.now + timedelta(hours=1)):
                self.assertTrue(monitor.scan(''))
            cycle.refresh_from_db()
            self.assertEqual(cycle.phase, 'ready')
            self.assertEqual(requests, [101, 102, 102])
            self.assertEqual(set(cycle.selected_samples['Mage/Fire']), {'101', '102'})
            self.assertEqual(len(logs.call_args.args[0]), 2)

    def test_waiting_wcl_does_not_block_leaderboard(self):
        self.cycle(phase='combatants', next_retry_at=self.now + timedelta(hours=1))
        with patch(AGGREGATION + '.CLASS_SPEC_MAP', {'Mage': ['Fire']}), \
                patch.object(SpecDetailAggregationMonitor, '_aggregate_dungeon') as dungeon, \
                patch.object(SpecDetailAggregationMonitor, '_aggregate_raid') as raid, \
                patch.object(SpecDetailAggregationMonitor, '_aggregate_leaderboard') as leaderboard:
            SpecDetailAggregationMonitor(None, self.aggregation).scan('')
        dungeon.assert_not_called()
        raid.assert_not_called()
        leaderboard.assert_called_once()

    def test_old_owner_cannot_checkpoint_or_finish(self):
        from botend.services.wcl_collection_cycle import CycleOwnershipLost, fenced_cycle, finish_collection
        cycle = self.cycle(phase='combatants')
        WclCollectionCycle.objects.filter(pk=cycle.pk).update(owner='replacement')
        with self.assertRaises(CycleOwnershipLost), fenced_cycle(cycle, phases=('combatants',)):
            self.fail('stale owner entered write section')
        with self.assertRaises(CycleOwnershipLost):
            finish_collection(cycle, {'failed': 0, 'pending': 0})

    def test_manual_command_refuses_incomplete_collection(self):
        self.cycle()
        with self.assertRaises(CommandError), patch.object(SpecDetailAggregationMonitor, '_aggregate_dungeon') as publish:
            call_command('aggregate_spec_stats', **{'class_name': 'Mage', 'spec_name': 'Fire'})
        publish.assert_not_called()

    def test_claim_without_cycle_is_not_completion_and_claim_fences_old_worker(self):
        self.ranking.last_scan_time = self.now - timedelta(days=1)
        self.ranking.save(update_fields=['last_scan_time'])
        first = claim_next_monitor_task(now=self.now, lease_owner='first', lease_seconds=60)
        self.assertEqual(first.pk, self.ranking.pk)
        cycle = WclCollectionCycle.objects.get(season=self.season)
        self.assertEqual(cycle.owner, first._wcl_cycle_owner)
        second = claim_next_monitor_task(now=self.now + timedelta(seconds=61), lease_owner='second')
        self.assertEqual(second.pk, self.ranking.pk)
        from botend.services.wcl_collection_cycle import CycleOwnershipLost, checkpoint_scope
        with self.assertRaises(CycleOwnershipLost):
            checkpoint_scope(cycle, 'dungeon:101')
        cycle.refresh_from_db()
        self.assertEqual(cycle.completed_scopes, [])

    def test_rankings_include_both_raid_difficulties_before_global_logs(self):
        self.season.raid_encounters = [{'id': 201, 'name': 'Boss'}]
        self.season.save()
        trace = []
        monitor = self.monitor()
        def rankings(enc, cls, spec, metric, **kwargs):
            trace.append(('rank', enc, kwargs.get('difficulty')))
            return {'rankings': [{'name': 'Player', 'server': {'name': 'Realm', 'region': 'eu'},
                'talents': [{'talentID': 1234, 'points': 1}] if kwargs.get('difficulty') else [],
                'report': {'code': 'Shared', 'fightID': enc}, 'amount': 100, 'hardModeLevel': 12}],
                'hasMorePages': False}
        def logs(code, fights):
            trace.append(('logs', code, fights))
            return []  # Authoritative missing is a terminal gap, not a retry.
        with patch(RANKING + '.CLASS_SPEC_MAP', {'Mage': ['Fire']}), \
                patch(RANKING + '.timezone.now', return_value=self.now), \
                patch(RANKING + '.time.sleep'), \
                patch.object(monitor, 'fetch_wcl_rankings', side_effect=rankings), \
                patch.object(monitor, 'fetch_wcl_combatant_info', side_effect=logs):
            self.assertTrue(monitor.scan(''))
        self.assertEqual(trace, [('rank', 101, None), ('rank', 102, None),
            ('rank', 201, 5), ('rank', 201, 4), ('logs', 'Shared', [101, 102])])
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual(cycle.phase, 'ready')
        self.assertEqual(cycle.counts['missing'], 2)

    def test_combatant_quota_retry_keeps_frozen_ids_and_never_reselects(self):
        row = SpecDungeonRanking.objects.create(season_id=self.season.pk, dungeon_id=101,
            dungeon_name='One', class_name='Mage', spec_name='Fire', character_name='Player',
            realm='Realm', region='eu', report_code='Shared', fight_id=1, dps=123, keystone_level=12)
        selected = {'Mage/Fire': {'101': [row.pk], '102': []}}
        cycle = self.cycle(phase='combatants', selected_samples=selected)
        monitor = self.monitor()
        def quota(*args):
            monitor._wcl_retry_at = self.now + timedelta(hours=1)
            monitor._wcl_last_error = 'rate_limit'
            return None
        with patch(RANKING + '.timezone.now', return_value=self.now), \
                patch.object(monitor, 'fetch_wcl_combatant_info', side_effect=quota):
            self.assertFalse(monitor.scan(''))
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'combatants')
        self.assertEqual(cycle.counts['pending'], 1)
        self.assertEqual(cycle.counts['failed'], 0)
        self.assertEqual(cycle.next_retry_at, self.now + timedelta(hours=1))
        # A new high-ranked row must not enter the frozen retry cohort.
        row.pk = None
        row.character_name = 'NewPlayer'
        row.dps = 9999
        row.save()
        with patch(RANKING + '.timezone.now', return_value=self.now + timedelta(hours=1)), \
                patch.object(monitor, 'fetch_wcl_rankings') as ranks, \
                patch('botend.services.spec_stats_service._select_dungeon_sample_records') as selector, \
                patch.object(monitor, 'fetch_wcl_combatant_info', return_value=[]):
            self.assertTrue(monitor.scan(''))
        ranks.assert_not_called()
        selector.assert_not_called()
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'ready')
        self.assertEqual(cycle.selected_samples, selected)

    def test_scope_replacement_and_checkpoint_rollback_together(self):
        old = SpecDungeonRanking.objects.create(season_id=self.season.pk, dungeon_id=101,
            dungeon_name='One', class_name='Mage', spec_name='Fire', character_name='Previous', dps=55)
        monitor = self.monitor()
        with patch(RANKING + '.CLASS_SPEC_MAP', {'Mage': ['Fire']}), \
                patch(RANKING + '.timezone.now', return_value=self.now), \
                patch(RANKING + '.time.sleep'), \
                patch.object(monitor, '_checkpoint', side_effect=RuntimeError('storage failed')), \
                patch.object(monitor, 'fetch_wcl_rankings', return_value={
                    'rankings': [{'name': 'New', 'amount': 999}], 'hasMorePages': False}):
            self.assertFalse(monitor.scan(''))
        old.refresh_from_db()
        self.assertEqual(old.character_name, 'Previous')
        self.assertEqual(WclCollectionCycle.objects.get().completed_scopes, [])

    def test_crash_after_real_dungeon_publication_reuses_generation(self):
        row = SpecDungeonRanking.objects.create(season_id=self.season.pk, dungeon_id=101,
            dungeon_name='One', class_name='Mage', spec_name='Fire', character_name='Player',
            realm='Realm', region='eu', report_code='Shared', fight_id=1, dps=123, keystone_level=12)
        cycle = self.cycle(phase='ready', collected_at=self.now,
            selected_samples={'Mage/Fire': {'101': [row.pk], '102': []}})
        monitor = SpecDetailAggregationMonitor(None, self.aggregation)
        with patch(AGGREGATION + '.CLASS_SPEC_MAP', {'Mage': ['Fire']}), \
                patch(AGGREGATION + '.timezone.now', return_value=self.now), \
                patch.object(monitor, '_aggregate_leaderboard'), \
                patch.object(monitor, '_aggregate_raid', side_effect=RuntimeError('crash after dungeon')):
            self.assertFalse(monitor.scan(''))
        path = Path(self.media) / 'aggregated' / str(self.season.pk) / 'Mage' / 'Fire' / 'dungeon.json'
        before = path.read_bytes()
        generation = json.loads(before)['_snapshot']['generation']
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'aggregating')
        self.assertIsNone(cycle.published_at)
        with patch(AGGREGATION + '.CLASS_SPEC_MAP', {'Mage': ['Fire']}), \
                patch(AGGREGATION + '.timezone.now', return_value=cycle.next_retry_at), \
                patch.object(monitor, '_aggregate_leaderboard'):
            self.assertTrue(monitor.scan(''))
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'published')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads(path.read_bytes())['_snapshot']['generation'], generation)

    def test_old_owner_cannot_publish_files(self):
        from botend.services.wcl_collection_cycle import CycleOwnershipLost
        from botend.controller.plugins.portal.SpecDetailAggregationMonitor import publish_stats
        cycle = self.cycle(phase='aggregating', collected_at=self.now)
        WclCollectionCycle.objects.filter(pk=cycle.pk).update(owner='new-owner')
        target = Path(self.media) / 'dungeon.json'
        with self.assertRaises(CycleOwnershipLost):
            publish_stats(target, {'dungeons': [], 'summary': None}, 'Mage', 'Fire', cycle=cycle)
        self.assertFalse(target.exists())

    def test_published_history_does_not_hide_current_incomplete_cycle(self):
        self.cycle(phase='published', slot=self.now.replace(hour=3) - timedelta(days=1),
                   collected_at=self.now - timedelta(days=1), published_at=self.now - timedelta(days=1))
        self.cycle(phase='rankings')
        with patch(AGGREGATION + '.CLASS_SPEC_MAP', {'Mage': ['Fire']}), \
                patch.object(SpecDetailAggregationMonitor, '_aggregate_dungeon') as dungeon, \
                patch.object(SpecDetailAggregationMonitor, '_aggregate_leaderboard'):
            SpecDetailAggregationMonitor(None, self.aggregation).scan('')
        dungeon.assert_not_called()


    def test_metadata_published_during_aggregation_requires_another_replay(self):
        from botend.models import WowDataUpdateState
        from botend.services.wcl_collection_cycle import _metadata_projection_due
        cycle = self.cycle(phase='published', collected_at=self.now,
            last_attempt_at=self.now, published_at=self.now + timedelta(minutes=10))
        WowDataUpdateState.objects.update_or_create(branch='retail', defaults={
            'published_at': self.now + timedelta(minutes=5)})
        self.assertTrue(_metadata_projection_due(cycle))
        cycle.last_attempt_at = self.now + timedelta(minutes=11)
        cycle.save(update_fields=['last_attempt_at'])
        self.assertFalse(_metadata_projection_due(cycle))


    def test_partial_manual_replay_does_not_acknowledge_other_specs(self):
        from botend.models import WowDataUpdateState
        from botend.services.wcl_collection_cycle import finish_publication, _metadata_projection_due
        old = self.now - timedelta(minutes=30)
        cycle = self.cycle(phase='aggregating', collected_at=old, last_attempt_at=self.now)
        WowDataUpdateState.objects.update_or_create(branch='retail', defaults={'published_at': old + timedelta(minutes=5)})
        finish_publication(cycle, metadata_checked_at=old)
        cycle.refresh_from_db()
        self.assertTrue(_metadata_projection_due(cycle))
