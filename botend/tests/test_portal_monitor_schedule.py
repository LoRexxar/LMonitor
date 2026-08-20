from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from botend.plugin_sync import portal_data_task_is_due


class PortalMonitorScheduleTests(SimpleTestCase):
    def _task(self, name, last_scan_time):
        return SimpleNamespace(name=name, last_scan_time=last_scan_time)

    def test_portal_data_tasks_run_once_for_each_early_morning_and_afternoon_slot(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        task = self._task(
            'SpecDetailAggregationMonitor',
            datetime(2026, 8, 20, 3, 10, tzinfo=shanghai),
        )

        self.assertTrue(portal_data_task_is_due(task, datetime(2026, 8, 20, 15, 0, tzinfo=shanghai)))
        task.last_scan_time = datetime(2026, 8, 20, 15, 20, tzinfo=shanghai)
        self.assertFalse(portal_data_task_is_due(task, datetime(2026, 8, 20, 16, 0, tzinfo=shanghai)))
        self.assertTrue(portal_data_task_is_due(task, datetime(2026, 8, 21, 3, 0, tzinfo=shanghai)))

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
