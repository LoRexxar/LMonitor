"""失败结束后的退避不会抹掉进度，也不会阻挡新的来源区间。"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
from botend.models import WowWagoMonitorState, WowWagoBuildEvent, WowWagoHotfixEvent


class ReachedSource(BaseException):
    """在实际来源调用边界停止，测试不得访问上游。"""


class WagoFailureAdmissionTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.monitor = WagoSkillDiffMonitor(None, None)
        self.state = WowWagoMonitorState.objects.create(branch='wow', locale='enUS',
            build='12.1.5.70575', hotfix_region_id=3, hotfix_push_id=99)

    def hotfix_failure(self):
        event = WowWagoHotfixEvent.objects.create(branch='wow', locale='enUS', region_id=3,
            from_push=99, to_push=101, push_id=101, status='generate_failed_fallback_report',
            last_attempt_at=self.now - timedelta(minutes=57), error_message='测试报告失败')
        WowWagoHotfixEvent.objects.filter(pk=event.pk).update(updated_at=self.now)
        return event

    def test_long_failed_hotfix_does_not_immediately_repeat_the_interval(self):
        event = self.hotfix_failure()
        with patch.object(self.monitor, '_fetch_latest_hotfix_push_id', return_value=101), \
             patch.object(self.monitor, '_collect_hotfix_interval_rows') as collect:
            self.assertFalse(self.monitor._scan_hotfix_locked(self.state, 'wow', self.state.build))
        collect.assert_not_called()
        event.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual(event.updated_at, self.now)
        self.assertEqual(event.error_message, '测试报告失败')
        self.assertEqual(event.status, 'generate_failed_fallback_report')
        self.assertEqual(self.state.hotfix_push_id, 99)

    def test_expired_backoff_retries_without_extending_failed_event(self):
        event = self.hotfix_failure()
        event.refresh_from_db()
        self.assertTrue(self.monitor._event_retry_deferred(event, now=self.now + timedelta(minutes=29)))
        self.assertFalse(self.monitor._event_retry_deferred(event, now=self.now + timedelta(minutes=30)))

    def test_new_push_is_not_blocked_by_previous_interval_failure(self):
        self.hotfix_failure()
        with patch.object(self.monitor, '_fetch_latest_hotfix_push_id', return_value=102), \
             patch.object(self.monitor, '_collect_hotfix_interval_rows', side_effect=ReachedSource) as collect:
            with self.assertRaises(ReachedSource):
                self.monitor._scan_hotfix_locked(self.state, 'wow', self.state.build)
        collect.assert_called_once_with(99, 102, region_id=3, locale='enUS')

    def test_other_region_failure_does_not_block_current_region(self):
        event = self.hotfix_failure()
        WowWagoHotfixEvent.objects.filter(pk=event.pk).update(region_id=1)
        with patch.object(self.monitor, '_fetch_latest_hotfix_push_id', return_value=101), \
             patch.object(self.monitor, '_collect_hotfix_interval_rows', side_effect=ReachedSource):
            with self.assertRaises(ReachedSource):
                self.monitor._scan_hotfix_locked(self.state, 'wow', self.state.build)

    def test_manual_and_explicit_backfill_can_retry_immediately(self):
        event = self.hotfix_failure()
        self.monitor.task = SimpleNamespace(_monitor_task_is_manual=True)
        self.assertFalse(self.monitor._event_retry_deferred(event, now=self.now))
        self.monitor.task = None
        with patch.object(self.monitor, '_collect_hotfix_interval_rows', side_effect=ReachedSource):
            with self.assertRaises(ReachedSource):
                self.monitor._scan_hotfix_locked(self.state, 'wow', self.state.build,
                                                backfill_interval=(99, 101))

    def test_build_failure_waits_from_completion_and_fresh_event_is_runnable(self):
        event = WowWagoBuildEvent.objects.create(branch='wow', locale='enUS',
            from_build=self.state.build, to_build='12.1.5.70576', status='diff_unavailable',
            last_attempt_at=self.now - timedelta(hours=1))
        with patch.object(self.monitor, '_process_build_event', return_value=True) as run:
            self.assertFalse(self.monitor._process_pending_build_events(self.state))
            run.assert_not_called()
            WowWagoBuildEvent.objects.filter(pk=event.pk).update(updated_at=self.now - timedelta(minutes=31))
            self.assertTrue(self.monitor._process_pending_build_events(self.state))
            run.assert_called_once()
            run.reset_mock()
            WowWagoBuildEvent.objects.filter(pk=event.pk).update(status='detected', updated_at=self.now)
            self.assertTrue(self.monitor._process_pending_build_events(self.state))
            run.assert_called_once()

    def test_hotfix_failure_is_not_reported_as_successful_scan(self):
        with patch.object(self.monitor, '_fetch_current_build', return_value=self.state.build), \
             patch.object(self.monitor, '_process_pending_build_events', return_value=True), \
             patch.object(self.monitor, '_latest_discovered_build', return_value=self.state.build), \
             patch.object(self.monitor, '_repair_state_if_needed'), \
             patch.object(self.monitor, '_scan_hotfix_if_needed', return_value=False):
            self.assertFalse(self.monitor._scan_state(self.state))
