from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase

from botend.models import MonitorTask
from botend.plugin_sync import (
    monitor_default_wait_time,
    monitor_task_sort_key,
    portal_data_task_is_due,
    portal_monitor_task_priority,
    claim_next_monitor_task,
)


class PortalMonitorScheduleTests(SimpleTestCase):
    def _task(self, name, last_scan_time):
        return SimpleNamespace(name=name, last_scan_time=last_scan_time)

    def test_portal_data_tasks_use_daily_staggered_slots_and_ten_minute_peak_refresh(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        player = self._task(
            'SpecDetailPlayerMonitor',
            datetime(2026, 8, 20, 2, 10, tzinfo=shanghai),
        )
        ranking = self._task(
            'SpecDetailRankingMonitor',
            datetime(2026, 8, 20, 3, 10, tzinfo=shanghai),
        )
        aggregation = self._task(
            'SpecDetailAggregationMonitor',
            datetime(2026, 8, 20, 6, 10, tzinfo=shanghai),
        )
        peak = self._task(
            'PortalPeakSpecRankMonitor',
            datetime(2026, 8, 20, 14, 0, tzinfo=shanghai),
        )

        afternoon = datetime(2026, 8, 20, 18, 0, tzinfo=shanghai)
        self.assertFalse(portal_data_task_is_due(player, afternoon))
        self.assertFalse(portal_data_task_is_due(ranking, afternoon))
        self.assertFalse(portal_data_task_is_due(aggregation, afternoon))
        self.assertTrue(portal_data_task_is_due(player, datetime(2026, 8, 21, 2, 0, tzinfo=shanghai)))
        self.assertEqual(monitor_default_wait_time('PortalPeakSpecRankMonitor'), 600)
        self.assertLess(portal_monitor_task_priority(peak), portal_monitor_task_priority(player))
        self.assertLess(portal_monitor_task_priority(player), portal_monitor_task_priority(ranking))
        self.assertLess(portal_monitor_task_priority(ranking), portal_monitor_task_priority(aggregation))

    def test_global_oldest_last_scan_time_wins_before_portal_priority(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        stale_unrelated = self._task(
            'wowheadMonitor',
            datetime(1999, 12, 31, 16, 0, tzinfo=shanghai),
        )
        recent_peak = self._task(
            'PortalPeakSpecRankMonitor',
            datetime(2026, 8, 28, 10, 0, tzinfo=shanghai),
        )

        ordered = sorted([recent_peak, stale_unrelated], key=monitor_task_sort_key)

        self.assertIs(ordered[0], stale_unrelated)

    def test_portal_data_task_missed_slot_runs_later_without_waiting_for_next_slot(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        task = self._task(
            'SpecDetailRankingMonitor',
            datetime(2026, 8, 19, 15, 5, tzinfo=shanghai),
        )

        self.assertTrue(portal_data_task_is_due(task, datetime(2026, 8, 20, 7, 45, tzinfo=shanghai)))

    def test_earlier_due_news_wins_over_older_daily_task(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        now = datetime(2026, 8, 29, 3, 21, tzinfo=shanghai)
        news = self._task(
            'PortalPostMonitor',
            datetime(2026, 8, 28, 23, 29, tzinfo=shanghai),
        )
        news.wait_time = 600
        ranking = self._task(
            'SpecDetailRankingMonitor',
            datetime(2026, 8, 28, 3, 0, tzinfo=shanghai),
        )
        ranking.wait_time = 86400

        self.assertLess(
            monitor_task_sort_key(news, now=now),
            monitor_task_sort_key(ranking, now=now),
        )

    def test_unrelated_task_is_not_controlled_by_portal_data_schedule(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        task = self._task(
            'PortalPeakSpecRankMonitor',
            datetime(2026, 8, 20, 14, 50, tzinfo=shanghai),
        )

        self.assertIsNone(portal_data_task_is_due(task, datetime(2026, 8, 20, 15, 0, tzinfo=shanghai)))


class MonitorTaskClaimTests(TestCase):
    def test_two_worker_claims_reserve_distinct_oldest_runnable_tasks(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        now = datetime(2026, 8, 29, 9, 0, tzinfo=shanghai)
        oldest = MonitorTask.objects.create(
            name='wowheadMonitor', target='', type=16,
            last_scan_time=datetime(2026, 8, 28, 20, 0, tzinfo=shanghai),
            wait_time=600, is_active=True,
        )
        second = MonitorTask.objects.create(
            name='PortalPostMonitor', target='', type=18,
            last_scan_time=datetime(2026, 8, 28, 21, 0, tzinfo=shanghai),
            wait_time=600, is_active=True,
        )

        first_claim = claim_next_monitor_task(now=now)
        second_claim = claim_next_monitor_task(now=now)

        self.assertEqual(first_claim.id, oldest.id)
        self.assertEqual(second_claim.id, second.id)
