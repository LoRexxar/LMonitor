from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.models import (
    SimcBackendBinary, SimcBenchmarkCase, SimcBenchmarkExecution,
    SimcBenchmarkPanel, SimcTask, SimulationRun,
)


class SimcWorkerTests(TestCase):
    def setUp(self):
        self.backend, _ = SimcBackendBinary.objects.update_or_create(
            identifier='production',
            defaults={
                'name': '正式服', 'platform': 'linux64',
                'simc_path': '/tmp/simc', 'current_version': 'a' * 40,
                'is_active': True,
            },
        )

    def make_task(self, *, name='worker task', status=0, started_at=None):
        return SimcTask.objects.create(
            user_id=9001,
            name=name,
            simc_profile_id=0,
            task_type=1,
            current_status=status,
            started_at=started_at,
            is_active=True,
            backend=self.backend,
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

    def test_consume_once_skips_cancelled_and_failed_tasks(self):
        from botend.services.simc_worker import SimcWorker

        cancelled = self.make_task(name='cancelled', status=5)
        failed = self.make_task(name='failed', status=3)
        pending = self.make_task(name='pending')
        monitor = MagicMock()
        monitor.process_simc_task.return_value = True
        worker = SimcWorker(monitor=monitor, poll_interval=0)

        self.assertTrue(worker.consume_once())

        cancelled.refresh_from_db()
        failed.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(cancelled.current_status, 5)
        self.assertEqual(failed.current_status, 3)
        self.assertEqual(pending.current_status, 2)
        monitor.process_simc_task.assert_called_once()
        self.assertEqual(monitor.process_simc_task.call_args.args[0].id, pending.id)

    def test_consume_once_isolates_unexpected_task_failure_and_next_cycle_continues(self):
        from botend.services.simc_worker import SimcWorker

        first = self.make_task(name='first')
        second = self.make_task(name='second')
        monitor = MagicMock()
        monitor.process_simc_task.side_effect = [
            RuntimeError('/srv/private/input.simc token=secret-value broken candidate'), True,
        ]
        worker = SimcWorker(monitor=monitor, poll_interval=0)

        self.assertTrue(worker.consume_once())
        first.refresh_from_db()
        self.assertEqual(first.current_status, 3)
        self.assertEqual(first.error_detail, 'Unexpected worker error.')
        self.assertNotIn('broken candidate', first.error_detail)
        self.assertNotIn('/srv/private', first.error_detail)
        self.assertNotIn('secret-value', first.error_detail)

        self.assertTrue(worker.consume_once())
        second.refresh_from_db()
        self.assertEqual(second.current_status, 2)
        self.assertEqual(monitor.process_simc_task.call_count, 2)

    def test_consume_once_does_not_complete_a_claim_recovered_as_stale(self):
        from botend.services.simc_worker import SimcWorker

        task = self.make_task()
        monitor = MagicMock()

        def recover_claim(*_args, **_kwargs):
            SimcTask.objects.filter(pk=task.pk).update(
                current_status=3,
                error_detail='Worker 心跳超时，执行已中断',
                completed_at=timezone.now(),
            )
            return True

        monitor.process_simc_task.side_effect = recover_claim
        worker = SimcWorker(monitor=monitor, poll_interval=0)

        self.assertTrue(worker.consume_once())
        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)
        self.assertIn('心跳超时', task.error_detail)

    @override_settings(SIMC_WORKER_STALE_SECONDS=60, SIMC_WORKER_MAX_ATTEMPTS=2)
    def test_recover_stale_running_fails_source_and_copies_pending_task_without_runs(self):
        from botend.services.simc_worker import SimcWorker

        stale_at = timezone.now() - timedelta(minutes=5)
        task = self.make_task(status=1, started_at=stale_at)
        old_run = SimulationRun.objects.create(
            task=task,
            sequence=1,
            status='running',
            started_at=stale_at,
        )
        SimcTask.objects.filter(pk=task.pk).update(modified_time=stale_at)
        worker = SimcWorker(monitor=MagicMock(), poll_interval=0)

        retry = self.make_task(name='retry', status=0)
        retry.source_task = task
        retry.save(update_fields=['source_task'])
        with patch('botend.services.simc_worker.create_rerun', return_value=retry) as rerun:
            self.assertEqual(worker.recover_stale_tasks(), 1)
        task.refresh_from_db()
        old_run.refresh_from_db()
        self.assertEqual(task.current_status, 3)
        self.assertIn('Worker', task.error_detail)
        self.assertEqual(old_run.status, 'running')
        rerun.assert_called_once_with(task.id, task.user_id)
        self.assertEqual(retry.current_status, 0)
        self.assertEqual(retry.simulation_runs.count(), 0)
        self.assertEqual(retry.backend_id, task.backend_id)

    @override_settings(SIMC_WORKER_STALE_SECONDS=60, SIMC_WORKER_MAX_ATTEMPTS=2)
    def test_recover_stale_running_stops_after_task_copy_attempt_limit(self):
        from botend.services.simc_worker import SimcWorker

        stale_at = timezone.now() - timedelta(minutes=5)
        task = self.make_task(status=1, started_at=stale_at)
        original = self.make_task(name='original', status=3)
        task.source_task = original
        task.save(update_fields=['source_task'])
        active_run = SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='same-candidate', status='running',
            started_at=stale_at,
        )
        SimcTask.objects.filter(pk=task.pk).update(modified_time=stale_at)
        monitor = MagicMock()
        worker = SimcWorker(monitor=monitor, poll_interval=0)

        self.assertEqual(worker.recover_stale_tasks(), 1)
        task.refresh_from_db()
        active_run.refresh_from_db()
        self.assertEqual(task.current_status, 3)
        self.assertIn('重试次数上限', task.error_detail)
        self.assertEqual(active_run.status, 'running')
        self.assertEqual(task.simulation_runs.count(), 1)

    @override_settings(SIMC_WORKER_STALE_SECONDS=60)
    def test_recover_stale_running_uses_lease_heartbeat_not_original_start_time(self):
        from botend.services.simc_worker import SimcWorker

        stale_at = timezone.now() - timedelta(minutes=5)
        task = self.make_task(status=1, started_at=stale_at)
        SimulationRun.objects.create(
            task=task, sequence=1, status='running', started_at=stale_at,
        )
        SimcTask.objects.filter(pk=task.pk).update(modified_time=timezone.now())

        worker = SimcWorker(monitor=MagicMock(), poll_interval=0)

        self.assertEqual(worker.recover_stale_tasks(), 0)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 1)
        self.assertEqual(task.simulation_runs.get().status, 'running')

    def test_run_stops_claiming_after_stop_request(self):
        from botend.services.simc_worker import SimcWorker

        worker = SimcWorker(monitor=MagicMock(), poll_interval=0)
        worker.request_stop()
        with patch.object(worker, 'consume_once') as consume:
            worker.run()
        consume.assert_not_called()

    def test_long_loop_maintenance_failures_are_isolated_from_sweep_and_consume(self):
        from botend.services.simc_worker import SimcWorker

        worker = SimcWorker(monitor=MagicMock(), poll_interval=0, maintenance_interval=30)
        with patch.object(worker, 'recover_stale_tasks') as recover, patch.object(
            worker, 'consume_once', side_effect=lambda: worker.request_stop() or False,
        ) as consume, patch(
            'botend.services.simc_worker.simc_benchmark_scheduler.schedule_due_panels',
            side_effect=RuntimeError('scheduler down'),
        ) as schedule, patch(
            'botend.services.simc_worker.simc_benchmark_scheduler.reconcile_pending_executions',
        ) as sweep:
            worker.run()
        recover.assert_called_once_with()
        schedule.assert_called_once_with()
        sweep.assert_called_once_with()
        consume.assert_called_once_with()

    def test_fast_consume_loop_throttles_all_maintenance_including_stale_recovery(self):
        from botend.services.simc_worker import SimcWorker

        worker = SimcWorker(monitor=MagicMock(), poll_interval=0, maintenance_interval=30)
        cycles = iter([True, True, False])

        def consume():
            found = next(cycles)
            if not found:
                worker.request_stop()
            return found

        with patch('botend.services.simc_worker.time.monotonic', side_effect=[0, 0, 1, 2]), patch.object(
            worker, 'recover_stale_tasks', side_effect=RuntimeError('recovery failed'),
        ) as recover, patch.object(worker, 'consume_once', side_effect=consume) as consume_mock, patch(
            'botend.services.simc_worker.simc_benchmark_scheduler.schedule_due_panels',
            side_effect=RuntimeError('scheduler failed'),
        ) as schedule, patch(
            'botend.services.simc_worker.simc_benchmark_scheduler.reconcile_pending_executions',
            side_effect=RuntimeError('sweep failed'),
        ) as sweep:
            worker.run()
        recover.assert_called_once_with()
        schedule.assert_called_once_with()
        sweep.assert_called_once_with()
        self.assertEqual(consume_mock.call_count, 3)

    def test_consume_terminal_task_targets_reconcile_without_changing_result_on_failure(self):
        from botend.services.simc_worker import SimcWorker

        task = self.make_task()
        monitor = MagicMock()
        monitor.process_simc_task.return_value = True
        worker = SimcWorker(monitor=monitor, poll_interval=0)
        with patch(
            'botend.services.simc_worker.simc_benchmark_scheduler.reconcile_execution_for_task',
            side_effect=RuntimeError('projection failed'),
        ) as reconcile:
            self.assertTrue(worker.consume_once())
        task.refresh_from_db()
        self.assertEqual(task.current_status, 2)
        reconcile.assert_called_once_with(task.id)

    @override_settings(SIMC_WORKER_STALE_SECONDS=60, SIMC_WORKER_MAX_ATTEMPTS=2)
    def test_benchmark_stale_retry_atomically_rebinds_case_authority(self):
        from botend.services.simc_worker import SimcWorker

        stale_at = timezone.now() - timedelta(minutes=5)
        old = self.make_task(status=1, started_at=stale_at)
        old.mode = 'comparison'
        old.save(update_fields=['mode'])
        SimcTask.objects.filter(pk=old.pk).update(modified_time=stale_at)
        panel = SimcBenchmarkPanel.objects.create(
            name='stale', slug='stale', created_by_id=old.user_id,
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_hash='0' * 64,
        )
        case = SimcBenchmarkCase.objects.create(
            execution=execution, task=old,
            spec_key='s', scenario_key='c', profile_key='p',
            spec_label='s', scenario_label='c', profile_label='p',
            coordinate_hash='1' * 64,
        )
        retry = self.make_task(name='retry')
        retry.mode = 'comparison'
        retry.source_task = old
        retry.save(update_fields=['mode', 'source_task'])

        worker = SimcWorker(monitor=MagicMock(), poll_interval=0)
        with patch('botend.services.simc_worker.create_rerun', return_value=retry):
            self.assertEqual(worker.recover_stale_tasks(), 1)
        old.refresh_from_db()
        case.refresh_from_db()
        self.assertEqual(old.current_status, 3)
        self.assertEqual(case.task_id, retry.id)
        self.assertEqual(retry.source_task_id, old.id)

    def test_management_command_runs_worker_once(self):
        worker = MagicMock()
        stdout = StringIO()
        with patch('botend.management.commands.simc_worker.SimcWorker', return_value=worker):
            call_command('simc_worker', '--once', stdout=stdout)
        worker.recover_stale_tasks.assert_called_once_with()
        worker.consume_once.assert_called_once_with()
        worker.run.assert_not_called()
