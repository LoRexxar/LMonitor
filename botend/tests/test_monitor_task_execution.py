from datetime import timedelta
from threading import Event
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from botend.models import MonitorTask, MonitorTaskLease
from botend.plugin_sync import claim_next_monitor_task
from botend.views import LMonitorCore


class _FakeHeartbeat:
    def __init__(self, task_id, lease_owner):
        self.task_id = task_id
        self.lease_owner = lease_owner
        self.lease_lost = Event()
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class MonitorTaskExecutionLeaseTests(TestCase):
    def _claim_task(self, owner):
        now = timezone.now()
        task = MonitorTask.objects.create(
            name='LeaseExecutionTest',
            target='',
            type=0,
            last_scan_time=now - timedelta(hours=1),
            wait_time=0,
            is_active=True,
        )
        claimed = claim_next_monitor_task(now=now, lease_owner=owner)
        self.assertEqual(claimed.id, task.id)
        return claimed

    def test_lease_is_released_when_browser_construction_fails(self):
        task = self._claim_task('browser-failure-owner')

        with (
            patch('botend.views._MonitorTaskLeaseHeartbeat', _FakeHeartbeat),
            patch('botend.views.LReq', side_effect=RuntimeError('browser failed')),
        ):
            with self.assertRaisesRegex(RuntimeError, 'browser failed'):
                LMonitorCore()._execute_claimed_task(task, 'browser-failure-owner')

        self.assertFalse(MonitorTaskLease.objects.filter(task=task).exists())

    def test_plugin_failure_closes_browser_and_releases_lease(self):
        task = self._claim_task('plugin-failure-owner')
        request_client = MagicMock()

        class FailingPlugin:
            def __init__(self, _request_client, _task):
                pass

            def scan(self, _target):
                raise ValueError('scan failed')

        with (
            patch('botend.views._MonitorTaskLeaseHeartbeat', _FakeHeartbeat),
            patch('botend.views.LReq', return_value=request_client),
            patch('botend.views.Monitor_Type_BaseObject_List', [FailingPlugin]),
            patch('botend.views._record_monitor_task_alert') as record_alert,
        ):
            LMonitorCore()._execute_claimed_task(task, 'plugin-failure-owner')

        record_alert.assert_called_once()
        request_client.set_current_task.assert_any_call(None)
        request_client.close_driver.assert_called_once_with()
        self.assertFalse(MonitorTaskLease.objects.filter(task=task).exists())
