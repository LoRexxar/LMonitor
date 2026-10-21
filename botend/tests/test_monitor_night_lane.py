from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from django.test import TestCase
from botend.models import MonitorTask, MonitorTaskLease, SeasonMeta, WclCollectionCycle
from botend.plugin_sync import (claim_next_monitor_task, claim_monitor_task,
    release_monitor_task_lease, monitor_default_wait_time)
from botend.services.wcl_collection_cycle import (ranking_slot, begin_aggregation,
    begin_collection, scheduled_cycle_due_at)


class NightLaneTests(TestCase):
    def setUp(self):
        MonitorTask.objects.all().delete()
        self.now = datetime(2026, 10, 21, 0, 1, tzinfo=ZoneInfo('Asia/Shanghai'))

    def task(self, name, **kwargs):
        return MonitorTask.objects.create(name=name, type=900 + MonitorTask.objects.count(),
            target='', is_active=True, last_scan_time=self.now - timedelta(hours=2), **kwargs)

    def test_lane_head_reads_only_phase_not_frozen_payloads(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from botend.plugin_sync import _night_lane_head
        with self.assertNumQueries(0):
            self.assertIsNone(_night_lane_head([]))
        season = SeasonMeta.objects.create(season_key='thin', season_name='Thin',
            is_active=True, mplus_zone_id=1, raid_zone_id=2)
        agg = self.task('SpecDetailAggregationMonitor')
        WclCollectionCycle.objects.create(season=season, slot=self.now,
            owner='old', phase='aggregating', collected_at=self.now,
            selected_samples={'large': list(range(1000))})
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(_night_lane_head([agg]).pk, agg.pk)
        sql = ' '.join(q['sql'] for q in queries).lower()
        self.assertNotIn('selected_samples', sql)
        self.assertNotIn('completed_scopes', sql)
        self.assertEqual(len(queries), 2)

    def test_global_head_cannot_be_skipped_by_separate_workers(self):
        season = self.task('SpecDetailSeasonMonitor')
        data = self.task('WowDataVersionMonitor')
        player = self.task('SpecDetailPlayerMonitor')
        self.assertIsNone(claim_next_monitor_task(now=self.now, task_names=[data.name]))
        first = claim_next_monitor_task(now=self.now, lease_owner='normal')
        self.assertEqual(first.pk, season.pk)
        self.assertIsNone(claim_next_monitor_task(now=self.now, task_names=[data.name]))
        self.assertIsNone(claim_monitor_task(player.pk, now=self.now))
        self.assertTrue(release_monitor_task_lease(first.pk, 'normal'))
        self.assertIsNone(claim_next_monitor_task(now=self.now))
        second = claim_next_monitor_task(now=self.now, task_names=[data.name], lease_owner='isolated')
        self.assertEqual(second.pk, data.pk)
        self.assertIsNone(claim_next_monitor_task(now=self.now))
        self.assertTrue(release_monitor_task_lease(second.pk, 'isolated'))
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, player.pk)

    def test_disabled_old_alias_lease_protects_lane_across_midnight(self):
        data = self.task('WowDataVersionMonitor')
        alias = self.task('legacy label')
        alias.type, alias.env_limit, alias.target = data.type, data.env_limit, data.target
        alias.is_active = False
        alias.save()
        player = self.task('SpecDetailPlayerMonitor')
        MonitorTaskLease.objects.create(task=alias, owner='old',
            claimed_at=self.now-timedelta(hours=2), expires_at=self.now+timedelta(hours=9))
        self.assertIsNone(claim_next_monitor_task(now=self.now))
        self.assertIsNone(claim_monitor_task(player.pk, now=self.now))
        self.assertEqual(MonitorTaskLease.objects.get(task=alias).owner, 'old')

    def test_manual_bypasses_switch_and_due_but_not_lane(self):
        first = self.task('MaxrollClassGuideMonitor')
        second = self.task('WowDailyReportMonitor')
        MonitorTask.objects.filter(pk=first.pk).update(is_active=False, last_scan_time=self.now)
        self.assertIsNotNone(claim_monitor_task(first.pk, now=self.now, lease_owner='manual'))
        self.assertIsNone(claim_monitor_task(second.pk, now=self.now))
        fast = self.task('PortalPeakSpecRankMonitor')
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, fast.pk)
        self.assertFalse(release_monitor_task_lease(first.pk, 'wrong'))

    def test_midnight_defaults_and_long_cycle_retry(self):
        for name in ('WowDataVersionMonitor', 'MaxrollClassGuideMonitor', 'WowDailyReportMonitor'):
            self.assertEqual(monitor_default_wait_time(name), 86400)
        season = SeasonMeta.objects.create(season_key='night', season_name='Night', is_active=True, mplus_zone_id=1, raid_zone_id=2)
        ranking = self.task('SpecDetailRankingMonitor')
        agg = self.task('SpecDetailAggregationMonitor')
        self.assertEqual(ranking_slot(self.now), self.now.replace(hour=0, minute=0))
        old = self.now - timedelta(days=2)
        cycle = WclCollectionCycle.objects.create(season=season, slot=old, owner='old',
            phase='aggregating', collected_at=old, last_attempt_at=old,
            next_retry_at=self.now+timedelta(minutes=5))
        self.assertIsNone(scheduled_cycle_due_at(ranking, self.now))
        self.assertIsNone(begin_collection(season, now=self.now))
        self.assertIsNone(begin_aggregation(season, now=self.now))
        self.assertIsNone(claim_next_monitor_task(now=self.now, task_names=[agg.name]))
        retry = cycle.next_retry_at
        self.assertEqual(claim_next_monitor_task(now=retry).pk, agg.pk)
        cycle.refresh_from_db()
        self.assertEqual(cycle.next_retry_at, retry)
        self.assertEqual(WclCollectionCycle.objects.count(), 1)

    def test_ready_cycle_aggregates_before_former_six_am_gate(self):
        season = SeasonMeta.objects.create(season_key='ready', season_name='Ready', is_active=True, mplus_zone_id=1, raid_zone_id=2)
        cycle = WclCollectionCycle.objects.create(season=season,
            slot=self.now.replace(hour=0, minute=0), owner='ready', phase='ready', collected_at=self.now)
        self.assertEqual(begin_aggregation(season, now=self.now).pk, cycle.pk)

    def test_legacy_published_three_am_is_same_day_not_a_new_cycle(self):
        season = SeasonMeta.objects.create(season_key='legacy', season_name='Legacy',
            is_active=True, mplus_zone_id=1, raid_zone_id=2)
        task = self.task('SpecDetailRankingMonitor')
        now = self.now.replace(hour=12)
        WclCollectionCycle.objects.create(season=season, slot=now.replace(hour=3),
            owner='old', phase='published', collected_at=now, published_at=now)
        self.assertIsNone(scheduled_cycle_due_at(task, now))
        self.assertIsNone(begin_collection(season, now=now))
        self.assertEqual(WclCollectionCycle.objects.count(), 1)

    def test_environment_ineligible_head_is_skipped_but_its_live_lease_blocks(self):
        from unittest.mock import patch
        season = self.task('SpecDetailSeasonMonitor', env_limit=1)
        data = self.task('WowDataVersionMonitor')
        with patch('botend.monitor_env.is_windows', return_value=False):
            self.assertEqual(claim_next_monitor_task(now=self.now,
                task_names=[data.name], lease_owner='data').pk, data.pk)
            release_monitor_task_lease(data.pk, 'data')
            MonitorTaskLease.objects.create(task=season, owner='other-env',
                claimed_at=self.now, expires_at=self.now+timedelta(hours=1))
            self.assertIsNone(claim_monitor_task(data.pk, now=self.now))


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.db import connection, connections
from django.test import TransactionTestCase


@skipUnless(connection.vendor == 'mysql', 'Requires real MySQL row locks; SQLite is not concurrency evidence')
class NightLaneConcurrentClaimsTests(TransactionTestCase):
    def test_independent_connections_normal_isolated_and_manual_share_one_lane(self):
        MonitorTask.objects.all().delete()
        now = datetime(2026, 10, 21, 0, 1, tzinfo=ZoneInfo('Asia/Shanghai'))
        values = dict(target='', is_active=True, last_scan_time=now-timedelta(days=1))
        season = MonitorTask.objects.create(name='SpecDetailSeasonMonitor', type=901, **values)
        data = MonitorTask.objects.create(name='WowDataVersionMonitor', type=902, **values)
        player = MonitorTask.objects.create(name='SpecDetailPlayerMonitor', type=903, **values)
        barrier = Barrier(3)

        def claim(kind):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                if kind == 'manual':
                    task = claim_monitor_task(player.pk, now=now, lease_owner=kind)
                else:
                    task = claim_next_monitor_task(now=now, lease_owner=kind,
                        task_names=[data.name] if kind == 'isolated' else None)
                return task.pk if task else None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(claim, ('normal', 'isolated', 'manual')))
        self.assertEqual(sum(pk is not None for pk in results), 1)
        self.assertIsNone(results[1])  # isolated worker never bypasses global head
        self.assertEqual(MonitorTaskLease.objects.count(), 1)
        fast = MonitorTask.objects.create(name='PortalPeakSpecRankMonitor', type=904, **values)
        self.assertEqual(claim_next_monitor_task(now=now).pk, fast.pk)
        self.assertEqual(MonitorTaskLease.objects.count(), 2)
