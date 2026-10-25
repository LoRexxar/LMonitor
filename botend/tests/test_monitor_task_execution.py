import json
from datetime import datetime, timedelta
from contextlib import ExitStack

from threading import Event
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase
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


class MonitorTaskExecutionLeaseTests(TransactionTestCase):
    # The real cleanup path closes connections. It must not run inside the
    # synthetic outer transaction held by TestCase (breaks MySQL isolation).
    def setUp(self):
        MonitorTask.objects.update(is_active=False)

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

    def test_retirement_during_real_claim_does_not_consume_daily_task_without_execution(self):
        from botend.backend_lifecycle import BackendLifecycle
        from botend.plugin_sync import complete_monitor_task_lease
        old_time = timezone.now() - timedelta(days=2)
        task = MonitorTask.objects.create(name='RetirementClaimTest', target='', type=0,
            is_active=True, wait_time=86400, last_scan_time=old_time)
        lifecycle = BackendLifecycle(retire_after=0)
        calls = []
        def claim_then_retire(**kwargs):
            claimed = claim_next_monitor_task(**kwargs)
            self.assertIsNotNone(claimed)
            lifecycle.request_retirement()
            return claimed
        class Core(LMonitorCore):
            def _execute_claimed_task(self, claimed, owner):
                calls.append(claimed.pk)
                complete_monitor_task_lease(claimed.pk, owner,
                                           task_updates={'flag': 'actually-executed'})
        with patch('botend.views.claim_next_monitor_task', side_effect=claim_then_retire) as claim:
            Core(lifecycle).scan()
        self.assertEqual(claim.call_count, 1)
        self.assertEqual(calls, [task.pk])
        task.refresh_from_db()
        self.assertGreater(task.last_scan_time, old_time)
        self.assertEqual(task.flag, 'actually-executed')
        self.assertFalse(MonitorTaskLease.objects.filter(task=task).exists())

    def test_execution_logs_preserve_scan_and_lease_semantics(self):
        scenarios = [
            ('true', True, 'returned_true', 'completed'),
            ('false', False, 'returned_false', 'completed'),
            ('none', None, 'returned_none', 'completed'),
            ('scan_error', None, 'exception', 'completed'),
            ('setup_error', None, 'setup_failure', 'release_attempted'),
            ('start_error', None, 'setup_failure', 'release_attempted'),
            ('release_error', None, 'setup_failure', 'release_attempted'),
            ('lost', True, 'returned_true', 'ownership_lost'),
            ('cleanup_error', True, 'returned_true', 'exception'),
            ('browser_cleanup_error', True, 'returned_true', 'completed'),
        ]
        for scenario, result, outcome, lease_outcome in scenarios:
            with self.subTest(scenario=scenario):
                MonitorTaskLease.objects.all().delete()
                MonitorTask.objects.update(is_active=False)
                task = self._claim_task('private-owner')
                task.target = 'private-target'
                request = MagicMock()

                class Plugin:
                    requires_browser = False

                    def __init__(self, request, monitor_task):
                        self.task = monitor_task

                    def scan(self, target):
                        if scenario == 'scan_error':
                            raise ValueError('private-error-detail')
                        if scenario == 'lost':
                            MonitorTaskLease.objects.filter(task=task).update(owner='replacement')
                        self.task.flag = 'plugin flag'
                        return result

                with ExitStack() as stack:
                    stack.enter_context(patch('botend.views._MonitorTaskLeaseHeartbeat', _FakeHeartbeat))
                    stack.enter_context(patch('botend.views.Monitor_Type_BaseObject_List', [Plugin]))
                    stack.enter_context(patch('botend.views._record_monitor_task_alert'))
                    log = stack.enter_context(patch('botend.views.logger'))
                    # Isolate the application clock; patching time.monotonic
                    # globally also affects Django's real COMMIT/ROLLBACK timers.
                    clock = stack.enter_context(patch('botend.views.time', wraps=__import__('time')))
                    clock.monotonic.side_effect = [100.0, 102.75]
                    stack.enter_context(patch('botend.views.LReq', return_value=request,
                                              side_effect=RuntimeError('setup') if scenario in ('setup_error', 'release_error') else None))
                    if scenario == 'start_error':
                        stack.enter_context(patch.object(_FakeHeartbeat, 'start', side_effect=RuntimeError('setup')))
                    if scenario == 'release_error':
                        stack.enter_context(patch('botend.views.release_monitor_task_lease', side_effect=RuntimeError('release')))
                    if scenario == 'cleanup_error':
                        stack.enter_context(patch('botend.views.complete_monitor_task_lease', side_effect=RuntimeError('cleanup')))
                    if scenario == 'browser_cleanup_error':
                        request.close_driver.side_effect = RuntimeError('close')
                    if scenario in ('setup_error', 'start_error', 'release_error', 'cleanup_error'):
                        with self.assertRaisesRegex(RuntimeError, 'setup|cleanup'):
                            LMonitorCore()._execute_claimed_task(task, 'private-owner')
                    else:
                        self.assertIsNone(LMonitorCore()._execute_claimed_task(task, 'private-owner'))
                messages = [call.args[0] for call in log.info.call_args_list
                            if call.args[0].startswith('[MonitorTask Execution] ')]
                self.assertEqual(len(messages), 2)
                start, finish = [json.loads(message.split('] ', 1)[1]) for message in messages]
                self.assertEqual(start['event'], 'started')
                self.assertEqual(finish['event'], 'finished')
                self.assertEqual(finish['task_id'], task.pk)
                self.assertEqual(finish['task_name'], task.name)
                self.assertEqual(start['started_at'], finish['started_at'])
                self.assertTrue(timezone.is_aware(datetime.fromisoformat(finish['started_at'])))
                self.assertTrue(timezone.is_aware(datetime.fromisoformat(finish['finished_at'])))
                self.assertEqual(finish['elapsed_seconds'], 2.75)
                self.assertEqual(finish['scan_outcome'], outcome)
                self.assertEqual(finish['lease_outcome'], lease_outcome)
                self.assertNotIn('private-', ''.join(messages))
                if scenario == 'browser_cleanup_error':
                    self.assertEqual(finish['cleanup_errors'], ['RuntimeError'])
                task.refresh_from_db()
                if scenario == 'lost':
                    self.assertTrue(MonitorTaskLease.objects.filter(task=task, owner='replacement').exists())
                    self.assertNotEqual(task.flag, 'plugin flag')
                elif scenario == 'release_error':
                    self.assertTrue(MonitorTaskLease.objects.filter(task=task).exists())
                    self.assertIs(finish['release_result'], False)
                else:
                    self.assertFalse(MonitorTaskLease.objects.filter(task=task).exists())
                if scenario in ('true', 'false', 'none', 'browser_cleanup_error'):
                    self.assertEqual(task.flag, 'plugin flag')

    def test_logging_failure_does_not_mask_setup_error_or_prevent_release(self):
        task = self._claim_task('logging-failure-owner')
        with (
            patch('botend.views._MonitorTaskLeaseHeartbeat', _FakeHeartbeat),
            patch('botend.views.LReq', side_effect=RuntimeError('original setup error')),
            patch('botend.views.logger.info', side_effect=OSError('log unavailable')),
        ):
            with self.assertRaisesRegex(RuntimeError, 'original setup error'):
                LMonitorCore()._execute_claimed_task(task, 'logging-failure-owner')
        self.assertFalse(MonitorTaskLease.objects.filter(task=task).exists())

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

    def test_data_plugin_can_run_without_browser(self):
        task = self._claim_task('data-plugin-owner')

        class DataPlugin:
            requires_browser = False

            def __init__(self, request, monitor_task):
                self.task = monitor_task

            def scan(self, target):
                self.task.flag = '资料检查完成'
                return True

        with (
            patch('botend.views._MonitorTaskLeaseHeartbeat', _FakeHeartbeat),
            patch('botend.views.LReq') as request,
            patch('botend.views.Monitor_Type_BaseObject_List', [DataPlugin]),
        ):
            LMonitorCore()._execute_claimed_task(task, 'data-plugin-owner')
        request.assert_called_once_with(is_chrome=False)
        task.refresh_from_db()
        self.assertEqual(task.flag, '资料检查完成')
