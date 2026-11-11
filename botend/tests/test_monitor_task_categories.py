"""Publication cadence, resource quota and source dependencies are separate."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from django.test import TestCase
from botend.models import MonitorTask, MonitorTaskLease, SeasonMeta, WclCollectionCycle
from botend.plugin_sync import (claim_next_monitor_task, claim_monitor_task,
    monitor_default_wait_time, monitor_task_due_at)


class MonitorTaskCategoryTests(TestCase):
    def setUp(self):
        MonitorTask.objects.all().delete()
        self.now = datetime(2026, 11, 11, 10, 0, tzinfo=ZoneInfo('Asia/Shanghai'))

    def task(self, name, **extra):
        return MonitorTask.objects.create(name=name, type=900 + MonitorTask.objects.count(),
            target=extra.pop('target', ''), is_active=True,
            last_scan_time=self.now - timedelta(days=1), **extra)

    def auto_running(self, task):
        MonitorTaskLease.objects.create(task=task, owner='automatic', is_manual=False,
            claimed_at=self.now - timedelta(hours=1), expires_at=self.now + timedelta(hours=1))

    def old_wcl(self):
        season = SeasonMeta.objects.create(season_key='categories', season_name='Categories',
            is_active=True, mplus_zone_id=1, raid_zone_id=2)
        WclCollectionCycle.objects.create(season=season, slot=self.now-timedelta(days=1),
            owner='old', phase='combatants', last_attempt_at=self.now-timedelta(hours=1))
        ranking = self.task('SpecDetailRankingMonitor')
        self.auto_running(ranking)

    def test_daily_publication_does_not_wait_for_old_wcl_or_pending_preparation(self):
        self.old_wcl()
        self.task('WowDataVersionMonitor')
        daily = self.task('WowDailyReportMonitor')
        got = claim_next_monitor_task(now=self.now, task_names={daily.name})
        self.assertIsNotNone(got)
        self.assertEqual(got.pk, daily.pk)
        self.assertFalse(got._monitor_task_is_long)

    def test_midnight_heavy_priority_does_not_suppress_daily_publication(self):
        self.now = self.now.replace(hour=1)
        self.old_wcl()
        daily = self.task('WowDailyReportMonitor')
        got = claim_next_monitor_task(now=self.now, task_names={daily.name})
        self.assertIsNotNone(got)
        self.assertEqual(got.pk, daily.pk)

    def test_daily_publication_has_its_own_slot_when_short_pool_is_full(self):
        self.old_wcl()
        self.auto_running(self.task('PortalMplusRunMonitor'))
        self.auto_running(self.task('ngaMonitor'))
        daily = self.task('WowDailyReportMonitor')
        self.assertEqual(claim_next_monitor_task(now=self.now, task_names={daily.name}).pk, daily.pk)

    def test_publication_is_serial_across_targets_but_manual_scope_rules_remain(self):
        first = self.task('WowDailyReportMonitor', target='one')
        second = self.task('WowDailyReportMonitor', target='two')
        claimed = claim_next_monitor_task(now=self.now, task_names={first.name})
        self.assertEqual(claimed.pk, first.pk)
        self.assertIsNone(claim_next_monitor_task(now=self.now, task_names={first.name}))
        self.assertIsNone(claim_monitor_task(first.pk, now=self.now))
        self.assertEqual(claim_monitor_task(second.pk, now=self.now).pk, second.pk)

    def test_publication_does_not_take_a_short_task_slot(self):
        daily = self.task('WowDailyReportMonitor')
        self.auto_running(daily)
        self.auto_running(self.task('ngaMonitor'))
        short = self.task('PortalMplusRunMonitor')
        self.assertEqual(claim_next_monitor_task(now=self.now, task_names={short.name}).pk, short.pk)

    def test_disabled_old_alias_lease_still_reserves_publication_quota(self):
        first = self.task('WowDailyReportMonitor', target='one')
        old = self.task('old daily alias')
        MonitorTask.objects.filter(pk=old.pk).update(type=first.type, target=first.target, is_active=False)
        self.auto_running(old)
        other = self.task('WowDailyReportMonitor', target='two')
        self.assertIsNone(claim_next_monitor_task(now=self.now, task_names={other.name}))

    def test_daily_cadence_does_not_become_hourly_after_reclassification(self):
        daily = self.task('WowDailyReportMonitor')
        self.assertEqual(monitor_default_wait_time(daily.name), 86400)
        self.assertEqual(monitor_task_due_at(daily, now=self.now).hour, 0)
        MonitorTask.objects.filter(pk=daily.pk).update(last_scan_time=self.now)
        daily.refresh_from_db()
        self.assertIsNone(monitor_task_due_at(daily, now=self.now+timedelta(hours=1)))

    def test_rotation_stops_new_publication_while_an_existing_operation_drains(self):
        from botend.backend_lifecycle import BackendLifecycle
        daily = self.task('WowDailyReportMonitor')
        lifecycle = BackendLifecycle()
        with lifecycle.monitor_admission() as existing:
            existing.classify(is_long=True)
            lifecycle.request_retirement()
            self.assertFalse(lifecycle.retiring)
            with lifecycle.monitor_admission() as incoming:
                self.assertFalse(incoming.allow_long_tasks)
                self.assertIsNone(claim_next_monitor_task(now=self.now, task_names={daily.name},
                    allow_long_tasks=incoming.allow_long_tasks))
        self.assertTrue(lifecycle.retiring)

    def test_claimed_publication_holds_rotation_without_using_heavy_quota(self):
        from botend.backend_lifecycle import BackendLifecycle
        daily = self.task('WowDailyReportMonitor')
        lifecycle = BackendLifecycle()
        with lifecycle.monitor_admission() as permit:
            task = claim_next_monitor_task(now=self.now, task_names={daily.name})
            self.assertFalse(task._monitor_task_is_long)
            permit.classify(is_long=task._monitor_task_holds_rotation)
            lifecycle.request_retirement()
            self.assertFalse(lifecycle.retiring)
        self.assertTrue(lifecycle.retiring)
