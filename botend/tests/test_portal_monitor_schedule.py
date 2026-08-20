from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from botend.plugin_sync import monitor_default_wait_time, portal_data_task_is_due, portal_monitor_task_priority


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

    def test_portal_data_task_missed_slot_runs_later_without_waiting_for_next_slot(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        task = self._task(
            'SpecDetailRankingMonitor',
            datetime(2026, 8, 19, 15, 5, tzinfo=shanghai),
        )

        self.assertTrue(portal_data_task_is_due(task, datetime(2026, 8, 20, 7, 45, tzinfo=shanghai)))

    def test_unrelated_task_is_not_controlled_by_portal_data_schedule(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        task = self._task(
            'PortalPeakSpecRankMonitor',
            datetime(2026, 8, 20, 14, 50, tzinfo=shanghai),
        )

        self.assertIsNone(portal_data_task_is_due(task, datetime(2026, 8, 20, 15, 0, tzinfo=shanghai)))
