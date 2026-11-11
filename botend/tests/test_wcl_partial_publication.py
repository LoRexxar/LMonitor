"""采集一轮即发布，失败项保持退避并在补齐后重建受影响统计。"""
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone

from botend.models import MonitorTask, SeasonMeta, SpecDungeonRanking, WclCollectionCycle
from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor
from botend.services.wcl_collection_cycle import (begin_collection, begin_aggregation,
    finish_publication, scheduled_cycle_due_at, CycleOwnershipLost, fenced_cycle, frozen_season)
from botend.services.spec_stats_snapshot import read_log_projection
from botend.tests.aggregation_cache_helpers import isolate_work_cache
from botend.tests.test_wcl_combatant_snapshot import event
from botend.tests.test_wcl_raw_staging import RawStagingIsolationMixin


class WclPartialPublicationTests(RawStagingIsolationMixin, TestCase):
    def setUp(self):
        super().setUp()
        isolate_work_cache(self)
        self.now = datetime(2026, 11, 11, 8, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.enterContext(patch('django.utils.timezone.now', side_effect=lambda: self.now))
        for module in ('SpecDetailRankingMonitor', 'SpecDetailAggregationMonitor'):
            self.enterContext(patch(f'botend.controller.plugins.portal.{module}.CLASS_SPEC_MAP',
                                    {'DeathKnight': ['Frost']}))
        self.enterContext(patch('time.sleep'))
        self.enterContext(patch('requests.sessions.Session.request',
                                side_effect=AssertionError('测试不可访问外部服务')))
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.enterContext(self.settings(MEDIA_ROOT=self.directory.name))
        self.season = SeasonMeta.objects.create(season_key='先发布', season_name='测试', is_active=True,
            mplus_zone_id=1, raid_zone_id=2,
            mplus_encounters=[{'id': 1, 'name': 'One'}, {'id': 2, 'name': 'Two'}])
        self.task = MonitorTask.objects.create(name='SpecDetailRankingMonitor', target='', last_scan_time=self.now)
        self.aggregate_task = MonitorTask.objects.create(name='SpecDetailAggregationMonitor', target='',
                                                        last_scan_time=self.now)
        self.monitor = SpecDetailRankingMonitor(None, self.task)
        self.path = Path(self.directory.name) / 'aggregated' / str(self.season.pk) / 'DeathKnight/Frost/dungeon.json'

    def ranking(self, encounter, *args, **kwargs):
        return {'rankings': [{'name': 'Player232', 'server': {'name': 'BurningLegion', 'region': 'eu'},
            'report': {'code': f'Report{encounter}', 'fightID': 1},
            'amount': 100, 'hardModeLevel': 20}], 'hasMorePages': False}

    def collect(self, fetch, rankings=None):
        with patch.object(self.monitor, 'fetch_wcl_rankings', side_effect=rankings or self.ranking), \
                patch.object(self.monitor, 'fetch_wcl_combatant_info', side_effect=fetch):
            return self.monitor.scan('')

    def publish(self):
        return SpecDetailAggregationMonitor(None, self.aggregate_task).scan('')

    def test_partial_round_publishes_now_and_repair_refreshes_only_changed_scope(self):
        def fetch(code, fights):
            if code == 'Report2':
                self.monitor._wcl_last_error = 'rate_limit'
                self.monitor._wcl_retry_at = self.now + timedelta(hours=2)
                return None
            return [event(1)]
        self.assertTrue(self.collect(fetch))
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual((cycle.phase, cycle.counts['pending']), ('ready', 1))
        self.assertIsNone(cycle.next_retry_at)
        self.assertEqual(scheduled_cycle_due_at(self.aggregate_task, self.now), cycle.collected_at)
        self.assertTrue(self.publish())
        first = read_log_projection(self.path)
        self.assertEqual(first['_data_contract'], 'wcl-log-v2')
        self.assertEqual([row['sample_size'] for row in first['dungeons']], [1, 1])
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'combatants')
        self.assertIsNotNone(cycle.published_at)
        self.assertIsNone(scheduled_cycle_due_at(self.task, self.now + timedelta(hours=1)))
        retry = cycle.next_retry_at
        self.now = retry
        with patch.object(self.monitor, 'fetch_wcl_rankings', side_effect=AssertionError('补采不能重抓榜单')), \
                patch.object(self.monitor, 'fetch_wcl_combatant_info', return_value=[event(1)]) as fetch:
            self.assertTrue(self.monitor.scan(''))
        fetch.assert_called_once_with('Report2', [1])
        from botend.services.spec_stats_service import SpecStatsService
        with patch.object(SpecStatsService, '_compute_dungeon_records',
                          wraps=SpecStatsService._compute_dungeon_records) as compute:
            self.assertTrue(self.publish())
        # 单本1复用已有工作结果，只重建单本2和依赖它的汇总。
        self.assertEqual([call.args[2] for call in compute.call_args_list], [2, 'all'])
        cycle.refresh_from_db()
        self.assertEqual((cycle.phase, cycle.counts['pending'], cycle.counts['failed']), ('published', 0, 0))
        self.assertIsNone(cycle.next_retry_at)

    def test_even_all_details_failed_publishes_and_preserves_retry(self):
        def fetch(*args):
            self.monitor._wcl_last_error = 'invalid_payload'
            return None
        self.assertTrue(self.collect(fetch))
        self.assertTrue(self.publish())
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual((cycle.phase, cycle.counts['failed']), ('combatants', 2))
        self.assertIsNotNone(cycle.published_at)
        self.assertGreater(cycle.next_retry_at, self.now)
        self.assertEqual(read_log_projection(self.path)['_data_contract'], 'wcl-log-v2')

    def test_ranking_failure_does_not_prevent_later_scope_or_publication(self):
        calls = []
        def ranking(enc, *args, **kwargs):
            calls.append(enc)
            return None if enc == 1 else self.ranking(enc)
        self.assertTrue(self.collect(lambda *args: [event(1)], ranking))
        self.assertEqual(calls, [1, 1, 1, 2])  # 保留原有单项短重试，但继续后续范围。
        self.assertTrue(self.publish())
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual((cycle.phase, cycle.counts['_ranking_pending']), ('rankings', ['dungeon:1']))
        self.assertEqual(cycle.completed_scopes, ['dungeon:2'])
        self.now = cycle.next_retry_at
        calls.clear()
        def repaired(enc, *args, **kwargs):
            calls.append(enc)
            return self.ranking(enc)
        self.assertTrue(self.collect(lambda *args: [event(1)], repaired))
        self.assertEqual(calls, [1])
        self.assertTrue(self.publish())
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'published')

    def test_all_rankings_failed_still_publish_empty_valid_projection(self):
        self.assertTrue(self.collect(lambda *args: None, lambda *args, **kwargs: None))
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual((cycle.phase, cycle.counts['selected']), ('ready', 0))
        self.assertTrue(self.publish())
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'rankings')
        self.assertEqual(read_log_projection(self.path)['_data_contract'], 'wcl-log-v2')
        self.assertEqual(set(cycle.counts['_ranking_pending']), {'dungeon:1', 'dungeon:2'})

    def test_publication_error_retries_publication_before_more_collection(self):
        self.assertTrue(self.collect(lambda *args: None))
        with patch.object(SpecDetailAggregationMonitor, '_aggregate_dungeon', side_effect=OSError('模拟写入失败')):
            self.assertFalse(self.publish())
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual(cycle.phase, 'aggregating')
        self.assertIsNone(scheduled_cycle_due_at(self.task, self.now + timedelta(hours=3)))
        self.now = cycle.next_retry_at
        self.assertTrue(self.publish())
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'combatants')

    def test_previous_day_partial_publication_does_not_block_new_daily_cohort(self):
        self.assertTrue(self.collect(lambda *args: None))
        self.assertTrue(self.publish())
        old = WclCollectionCycle.objects.get()
        self.now += timedelta(days=1)
        self.assertIsNotNone(scheduled_cycle_due_at(self.task, self.now))
        new = begin_collection(self.season, self.task)
        self.assertNotEqual(new.pk, old.pk)
        self.assertEqual(new.phase, 'rankings')
        old.refresh_from_db()
        self.assertEqual(old.phase, 'published')
        self.assertGreater(old.counts['failed'], 0)

    def test_published_partial_owner_cannot_write_after_repair_claim(self):
        self.assertTrue(self.collect(lambda *args: None))
        cycle = begin_aggregation(self.season)
        finish_publication(cycle)
        self.now = cycle.next_retry_at
        begin_collection(self.season)
        with self.assertRaises(CycleOwnershipLost), fenced_cycle(cycle, phases=('combatants',)):
            self.fail('过期持有者不应写入')

    def test_rankings_backoff_does_not_block_due_details_repair(self):
        row = SpecDungeonRanking.objects.create(season_id=self.season.pk, dungeon_id=1,
            class_name='DeathKnight', spec_name='Frost', character_name='Player232',
            realm='BurningLegion', region='eu', report_code='Report1', fight_id=1, dps=100)
        cycle = WclCollectionCycle.objects.create(season=self.season, slot=self.now,
            phase='rankings', selected_samples={'DeathKnight/Frost': {'1': [row.pk], '2': []}},
            counts={'_ranking_pending': ['dungeon:2'],
                    '_ranking_retry_at': (self.now + timedelta(hours=2)).isoformat()})
        with patch.object(self.monitor, 'fetch_wcl_rankings', side_effect=AssertionError('榜单仍在退避')), \
                patch.object(self.monitor, 'fetch_wcl_combatant_info', return_value=[event(1)]) as fetch:
            self.assertTrue(self.monitor.scan(''))
        fetch.assert_called_once_with('Report1', [1])
        cycle.refresh_from_db()
        self.assertEqual((cycle.phase, cycle.counts['matched']), ('ready', 1))
        self.assertEqual(cycle.counts['_ranking_pending'], ['dungeon:2'])

    def test_frozen_raid_order_does_not_change_with_repair_completion_order(self):
        self.season.raid_encounters = [{'id': 201, 'name': 'A'}, {'id': 202, 'name': 'B'}]
        cycle = WclCollectionCycle.objects.create(season=self.season, slot=self.now,
            completed_scopes=['raid:5:202'], selected_samples={
                'raid:DeathKnight/Frost': {'5:201': [], '5:202': [], '4:201': [], '4:202': []}})
        before = frozen_season(cycle, self.season).raid_encounters
        self.assertEqual([row['id'] for row in before], [201, 202])
        cycle.completed_scopes.append('raid:5:201')
        self.assertEqual(frozen_season(cycle, self.season).raid_encounters, before)
