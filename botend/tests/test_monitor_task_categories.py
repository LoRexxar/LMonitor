"""Long tasks and ordinary short polling; daily cadence is independent."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from django.test import TestCase
from botend.models import MonitorTask, MonitorTaskLease
from botend.plugin_sync import (claim_next_monitor_task, claim_monitor_task,
    monitor_default_wait_time, monitor_task_due_at,
    release_monitor_task_lease)


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

    def long_running(self):
        task = self.task('SpecDetailRankingMonitor')
        self.auto_running(task)

    def test_daily_is_claimed_by_normal_polling_while_long_task_runs(self):
        self.long_running()
        self.task('WowDataVersionMonitor')
        daily = self.task('WowDailyReportMonitor')
        got = claim_next_monitor_task(now=self.now)
        self.assertEqual(got.pk, daily.pk)
        self.assertFalse(got._monitor_task_is_long)

    def test_short_polling_also_runs_at_midnight_with_long_task_active(self):
        self.now = self.now.replace(hour=1)
        self.long_running()
        short = self.task('ngaMonitor')
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, short.pk)

    def test_daily_waits_for_shared_short_capacity(self):
        self.long_running()
        self.auto_running(self.task('PortalMplusRunMonitor'))
        self.auto_running(self.task('ngaMonitor'))
        self.task('WowDailyReportMonitor')
        self.assertIsNone(claim_next_monitor_task(now=self.now))

    def test_daily_consumes_a_normal_short_slot(self):
        self.auto_running(self.task('WowDailyReportMonitor'))
        other = self.task('ngaMonitor')
        self.auto_running(other)
        short = self.task('PortalMplusRunMonitor')
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, short.pk)
        fourth = self.task('PortalPostMonitor')
        self.assertIsNone(claim_next_monitor_task(now=self.now))
        self.assertTrue(release_monitor_task_lease(other.pk, 'automatic'))
        self.assertEqual(claim_next_monitor_task(now=self.now, task_names={fourth.name}).pk, fourth.pk)

    def test_different_daily_scopes_have_no_special_publication_quota(self):
        first = self.task('WowDailyReportMonitor', target='one')
        second = self.task('WowDailyReportMonitor', target='two')
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, first.pk)
        self.assertIsNone(claim_monitor_task(first.pk, now=self.now))
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, second.pk)

    def test_disabled_alias_keeps_same_scope_mutex_but_no_publication_quota(self):
        first = self.task('WowDailyReportMonitor', target='one')
        old = self.task('old daily alias')
        MonitorTask.objects.filter(pk=old.pk).update(type=first.type, target=first.target, is_active=False)
        self.auto_running(old)
        second = self.task('WowDailyReportMonitor', target='two')
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, second.pk)

    def test_daily_cadence_remains_daily(self):
        daily = self.task('WowDailyReportMonitor')
        self.assertEqual(monitor_default_wait_time(daily.name), 86400)
        self.assertEqual(monitor_task_due_at(daily, now=self.now).hour, 0)
        MonitorTask.objects.filter(pk=daily.pk).update(last_scan_time=self.now)
        daily.refresh_from_db()
        self.assertIsNone(monitor_task_due_at(daily, now=self.now+timedelta(hours=1)))

    def test_daily_uses_normal_short_admission_while_long_work_drains(self):
        self.long_running()
        daily = self.task('WowDailyReportMonitor')
        got = claim_next_monitor_task(now=self.now, allow_long_tasks=False)
        self.assertEqual(got.pk, daily.pk)
        self.assertFalse(got._monitor_task_is_long)


    def test_real_type_alias_keeps_its_long_class_even_without_canonical_label(self):
        from LMonitor.config import Monitor_Type_BaseObject_List
        from botend.plugin_sync import _night_lane_scopes
        index = next(i for i, cls in enumerate(Monitor_Type_BaseObject_List)
                     if cls.__name__ == 'WagoSkillDiffMonitor')
        alias = self.task('legacy display label')
        alias.type = index
        alias.save(update_fields=['type'])
        got = claim_monitor_task(alias.pk, now=self.now, advance_schedule=False)
        self.assertIsNotNone(got)
        self.assertTrue(got._monitor_task_is_long)

    def test_resource_class_uses_executed_type_not_a_misleading_long_label(self):
        from LMonitor.config import Monitor_Type_BaseObject_List
        index = next(i for i, cls in enumerate(Monitor_Type_BaseObject_List)
                     if cls.__name__ == 'PortalPeakSpecRankMonitor')
        task = self.task('WagoSkillDiffMonitor')
        task.type = index
        task.save(update_fields=['type'])
        got = claim_monitor_task(task.pk, now=self.now, advance_schedule=False)
        self.assertIsNotNone(got)
        self.assertFalse(got._monitor_task_is_long)
