from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.models import SimcTask, SimcTaskBatch, SimulationRun


class SimcWorkerTests(TestCase):
    def make_task(self, *, name='worker task', status=0, batch=None, started_at=None):
        return SimcTask.objects.create(
            user_id=9001,
            name=name,
            simc_profile_id=0,
            task_type=1,
            current_status=status,
            batch=batch,
            started_at=started_at,
            is_active=True,
        )

    def test_process_simc_task_claims_pending_task_once_and_records_start_time(self):
        task = self.make_task()
        stale_copy = SimcTask.objects.get(pk=task.pk)
        monitor = SimcMonitor(None, None)

        with patch.object(monitor, 'is_reference_task', return_value=True), \
             patch.object(monitor, 'process_reference_task', return_value=True) as process:
            self.assertTrue(monitor.process_simc_task(task))
            self.assertFalse(monitor.process_simc_task(stale_copy))

        task.refresh_from_db()
        self.assertEqual(task.current_status, 1)
        self.assertIsNotNone(task.started_at)
        process.assert_called_once()

    def test_consume_once_isolates_unexpected_task_failure_and_next_cycle_continues(self):
        from botend.services.simc_worker import SimcWorker

        first = self.make_task(name='first')
        second = self.make_task(name='second')
        monitor = MagicMock()
        monitor.process_simc_task.side_effect = [RuntimeError('broken candidate'), True]
        worker = SimcWorker(monitor=monitor, poll_interval=0)

        self.assertTrue(worker.consume_once())
        first.refresh_from_db()
        self.assertEqual(first.current_status, 3)
        self.assertIn('broken candidate', first.error_detail)

        self.assertTrue(worker.consume_once())
        second.refresh_from_db()
        self.assertEqual(second.current_status, 2)
        self.assertEqual(monitor.process_simc_task.call_count, 2)

    @override_settings(SIMC_WORKER_STALE_SECONDS=60, SIMC_WORKER_MAX_ATTEMPTS=2)
    def test_recover_stale_running_preserves_old_run_and_requeues_with_new_sequence(self):
        from botend.services.simc_worker import SimcWorker

        stale_at = timezone.now() - timedelta(minutes=5)
        task = self.make_task(status=1, started_at=stale_at)
        old_run = SimulationRun.objects.create(
            task=task,
            sequence=1,
            status='running',
            started_at=stale_at,
        )
        worker = SimcWorker(monitor=MagicMock(), poll_interval=0)

        self.assertEqual(worker.recover_stale_tasks(), 1)
        task.refresh_from_db()
        old_run.refresh_from_db()
        self.assertEqual(task.current_status, 0)
        self.assertIsNone(task.started_at)
        self.assertEqual(old_run.status, 'failed')
        self.assertIn('Worker', old_run.error_detail)
        self.assertIsNotNone(old_run.completed_at)

        monitor = SimcMonitor(None, task)
        monitor.result_path = '/tmp/simc_worker_results'
        with patch.object(monitor, 'is_reference_task', return_value=True), \
             patch.object(monitor, 'process_reference_task', side_effect=lambda claimed: SimulationRun.objects.create(
                 task=claimed,
                 sequence=2,
                 status='completed',
                 started_at=timezone.now(),
                 completed_at=timezone.now(),
             ) and True):
            self.assertTrue(monitor.process_simc_task(task))
        self.assertEqual(list(task.simulation_runs.order_by('sequence').values_list('sequence', flat=True)), [1, 2])

    @override_settings(SIMC_WORKER_STALE_SECONDS=60, SIMC_WORKER_MAX_ATTEMPTS=2)
    def test_recover_stale_running_stops_after_attempt_limit(self):
        from botend.services.simc_worker import SimcWorker

        stale_at = timezone.now() - timedelta(minutes=5)
        batch = SimcTaskBatch.objects.create(user_id=9001, name='retry batch', status=1)
        task = self.make_task(status=1, batch=batch, started_at=stale_at)
        SimulationRun.objects.create(task=task, sequence=1, status='failed', started_at=stale_at, completed_at=stale_at)
        active_run = SimulationRun.objects.create(task=task, sequence=2, status='running', started_at=stale_at)
        monitor = MagicMock()
        worker = SimcWorker(monitor=monitor, poll_interval=0)

        self.assertEqual(worker.recover_stale_tasks(), 1)
        task.refresh_from_db()
        active_run.refresh_from_db()
        self.assertEqual(task.current_status, 3)
        self.assertIn('重试次数上限', task.error_detail)
        self.assertEqual(active_run.status, 'failed')
        monitor.sync_batch_lifecycle.assert_called_once_with(batch.id)

    def test_run_stops_claiming_after_stop_request(self):
        from botend.services.simc_worker import SimcWorker

        worker = SimcWorker(monitor=MagicMock(), poll_interval=0)
        worker.request_stop()
        with patch.object(worker, 'consume_once') as consume:
            worker.run()
        consume.assert_not_called()

    def test_management_command_runs_worker_once(self):
        worker = MagicMock()
        stdout = StringIO()
        with patch('botend.management.commands.simc_worker.SimcWorker', return_value=worker):
            call_command('simc_worker', '--once', stdout=stdout)
        worker.recover_stale_tasks.assert_called_once_with()
        worker.consume_once.assert_called_once_with()
        worker.run.assert_not_called()
