"""Scheduler routing regression: slow sources must not consume ordinary slots."""
from contextlib import ExitStack
from datetime import datetime, timedelta
from threading import Event, Lock, current_thread
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings

from botend.models import MonitorTask, MonitorTaskLease, SeasonMeta, WclCollectionCycle
from botend.plugin_sync import claim_next_monitor_task, release_monitor_task_lease


class MonitorWorkerIsolationTests(TestCase):
    def setUp(self):
        MonitorTask.objects.all().delete()
        self.now = datetime(2026, 10, 26, 19, 0, tzinfo=ZoneInfo('Asia/Shanghai'))

    def task(self, name, days=1):
        return MonitorTask.objects.create(name=name, type=900 + MonitorTask.objects.count(),
            target='', is_active=True, wait_time=60,
            last_scan_time=self.now - timedelta(days=days))

    def test_normal_slots_exclude_both_due_long_tasks_and_resume_original_cycle(self):
        wago = self.task('WagoSkillDiffMonitor', days=3)
        peak = self.task('PortalPeakSpecRankMonitor', days=3)
        news = self.task('PortalPostMonitor', days=2)
        agg = self.task('SpecDetailAggregationMonitor')
        season = SeasonMeta.objects.create(season_key='isolation', season_name='Isolation',
            is_active=True, mplus_zone_id=1, raid_zone_id=2)
        cycle = WclCollectionCycle.objects.create(season=season, slot=self.now-timedelta(days=1),
            owner='interrupted', phase='aggregating', collected_at=self.now-timedelta(hours=1))
        first = claim_next_monitor_task(now=self.now, lease_owner='normal-1')
        self.assertEqual(first.pk, news.pk)
        second = claim_next_monitor_task(now=self.now, lease_owner='normal-2')
        self.assertEqual(second.pk, agg.pk)
        cycle.refresh_from_db()
        self.assertEqual(WclCollectionCycle.objects.count(), 1)
        self.assertEqual(cycle.phase, 'aggregating')
        self.assertEqual(cycle.owner, second._wcl_cycle_owner)
        self.assertNotEqual(cycle.owner, 'interrupted')
        for task in (wago, peak):
            claimed = claim_next_monitor_task(now=self.now, task_names={task.name},
                lease_owner=task.name, lease_seconds=7200)
            self.assertEqual(claimed.pk, task.pk)
            self.assertIsNone(claim_next_monitor_task(now=self.now+timedelta(hours=1),
                task_names={task.name}, lease_owner='duplicate'))
            self.assertFalse(release_monitor_task_lease(task.pk, 'wrong-owner'))
        self.assertEqual(MonitorTaskLease.objects.count(), 4)

    @override_settings(MONITOR_TASK_AUTO_SYNC_PLUGINS=False)
    def test_backend_starts_one_independent_bounded_worker_per_slow_source(self):
        from botend.views import LMonitorCore, LMonitorCoreBackend
        started, release, lock = Event(), Event(), Lock()
        calls = []

        def blocked_scan(core, *, task_names=None):
            with lock:
                calls.append((task_names, current_thread()))
                if len(calls) == 3:
                    started.set()
            release.wait(5)

        backend = LMonitorCoreBackend.__new__(LMonitorCoreBackend)
        backend.lifecycle = Mock(retiring=True)
        backend._workers = []
        backend.threadpool = Mock()
        maintenance = (
            ('gear_catalog_snapshot', 'start_catalog_snapshot_worker'),
            ('journal_loot_snapshot', 'start_journal_loot_snapshot_worker'),
            ('news_snapshot', 'start_news_snapshot_worker'),
            ('class_guide_snapshot', 'start_guide_snapshot_worker'),
            ('journal_snapshot', 'start_journal_snapshot_worker'),
            ('snapshot_cache_maintenance', 'start_snapshot_cache_maintenance_worker'),
        )
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(LMonitorCore, 'scan', blocked_scan))
                for module, function in maintenance:
                    stack.enter_context(patch('botend.services.'+module+'.'+function, return_value=None))
                backend._run()
                self.assertTrue(started.wait(2), 'all slow-source workers must start independently')
                self.assertCountEqual([names for names, _ in calls], [
                    {'WowDataVersionMonitor'}, {'WagoSkillDiffMonitor'}, {'PortalPeakSpecRankMonitor'}])
                self.assertEqual(len({thread.ident for _, thread in calls}), 3)
                self.assertTrue(all(thread.is_alive() for _, thread in calls))
                backend.threadpool.new.assert_not_called()
        finally:
            release.set()
            for thread in backend._workers:
                if thread is not None:
                    thread.join(3)
