"""Durability contracts for benchmark scheduler and reconciliation maintenance."""
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from botend.models import (
    SimcBackendBinary, SimcBenchmarkCase, SimcBenchmarkExecution,
    SimcBenchmarkPanel, SimcTask,
)
from botend.services.simc_benchmark_execution import BenchmarkExecutionConflict
from botend.services.simc_benchmark_scheduler import (
    reconcile_execution_for_task, reconcile_pending_executions, schedule_due_panels,
)


class SimcBenchmarkSchedulerTests(TestCase):
    def panel(self, slug, slot, **values):
        defaults = {
            'name': slug, 'slug': slug, 'created_by_id': 1,
            'is_active': True, 'schedule_enabled': True,
            'interval_seconds': 60, 'next_run_at': slot,
        }
        defaults.update(values)
        return SimcBenchmarkPanel.objects.create(**defaults)

    def test_due_filter_batch_order_phase_advance_and_single_execution_per_pass(self):
        now = timezone.now().replace(microsecond=0)
        first = self.panel('first', now - timedelta(seconds=190))
        self.panel('future', now + timedelta(seconds=1))
        self.panel('disabled', now, schedule_enabled=False)
        self.panel('inactive', now, is_active=False)

        with patch(
            'botend.services.simc_benchmark_scheduler.create_execution'
        ) as create:
            result = schedule_due_panels(now=now, batch_size=20)

        self.assertEqual(result['selected'], 1)
        self.assertEqual(result['scheduled'], 1)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs['scheduled_slot'], now - timedelta(seconds=190))
        first.refresh_from_db()
        self.assertEqual(first.last_scheduled_at, now - timedelta(seconds=190))
        self.assertEqual(first.next_run_at, now + timedelta(seconds=50))

    def test_existing_winner_crash_window_still_advances_slot(self):
        now = timezone.now().replace(microsecond=0)
        panel = self.panel('winner', now)
        winner = SimcBenchmarkExecution.objects.create(
            panel=panel, trigger='schedule', scheduled_slot=now, config_hash='0' * 64,
        )
        with patch(
            'botend.services.simc_benchmark_scheduler.create_execution', return_value=winner,
        ):
            schedule_due_panels(now=now)
        panel.refresh_from_db()
        self.assertEqual(panel.next_run_at, now + timedelta(seconds=60))

    def test_concurrent_admin_slot_change_is_not_overwritten_and_latest_interval_is_used(self):
        now = timezone.now().replace(microsecond=0)
        changed = self.panel('changed', now)
        latest = self.panel('latest', now)

        def create(panel, **_kwargs):
            if panel.pk == changed.pk:
                SimcBenchmarkPanel.objects.filter(pk=panel.pk).update(
                    next_run_at=now + timedelta(hours=1),
                )
            else:
                SimcBenchmarkPanel.objects.filter(pk=panel.pk).update(interval_seconds=17)
            return object()

        with patch('botend.services.simc_benchmark_scheduler.create_execution', side_effect=create):
            schedule_due_panels(now=now)
        changed.refresh_from_db()
        latest.refresh_from_db()
        self.assertEqual(changed.next_run_at, now + timedelta(hours=1))
        self.assertEqual(latest.next_run_at, now + timedelta(seconds=17))

    def test_validation_advances_but_system_failure_does_not_and_next_panel_continues(self):
        now = timezone.now().replace(microsecond=0)
        invalid = self.panel('invalid', now)
        broken = self.panel('broken', now + timedelta(seconds=1))
        later = self.panel('later', now + timedelta(seconds=2))
        run_at = now + timedelta(seconds=2)

        def create(panel, **_kwargs):
            if panel.pk == invalid.pk:
                raise ValidationError('/private/config actions=secret')
            if panel.pk == broken.pk:
                raise OperationalError('database unavailable')
            return object()

        with patch('botend.services.simc_benchmark_scheduler.create_execution', side_effect=create):
            result = schedule_due_panels(now=run_at)
        invalid.refresh_from_db()
        broken.refresh_from_db()
        later.refresh_from_db()
        self.assertGreater(invalid.next_run_at, run_at)
        self.assertEqual(broken.next_run_at, now + timedelta(seconds=1))
        self.assertGreater(later.next_run_at, run_at)
        self.assertEqual(result['failed'], 2)
        self.assertNotIn('/private', repr(result))
        self.assertNotIn('actions=', repr(result))

    def test_retryable_configuration_conflict_does_not_advance_slot(self):
        now = timezone.now().replace(microsecond=0)
        panel = self.panel('conflict', now)
        with patch(
            'botend.services.simc_benchmark_scheduler.create_execution',
            side_effect=BenchmarkExecutionConflict('configuration raced'),
        ):
            result = schedule_due_panels(now=now)
        panel.refresh_from_db()
        self.assertEqual(panel.next_run_at, now)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['advanced'], 0)

    def test_reconcile_cursor_reaches_rows_beyond_a_permanently_pending_batch(self):
        now = timezone.now().replace(microsecond=0)
        panel = self.panel('fair', now)
        executions = [SimcBenchmarkExecution.objects.create(
            panel=panel, config_hash=f'{number:064x}',
        ) for number in range(5)]
        calls = []
        with patch('botend.services.simc_benchmark_scheduler._reconcile_cursor', 0), patch(
            'botend.services.simc_benchmark_scheduler.reconcile_execution',
            side_effect=lambda execution: calls.append(execution.pk),
        ):
            first = reconcile_pending_executions(batch_size=3)
            second = reconcile_pending_executions(batch_size=3)
        self.assertEqual(calls[:3], [row.pk for row in executions[:3]])
        self.assertEqual(calls[3:], [row.pk for row in executions[3:]])
        self.assertEqual(first['next_cursor'], executions[2].pk)
        self.assertEqual(second['next_cursor'], executions[-1].pk)

    def test_reconcile_sweep_isolates_failures_and_target_lookup(self):
        now = timezone.now().replace(microsecond=0)
        panel = self.panel('reconcile', now)
        first = SimcBenchmarkExecution.objects.create(panel=panel, config_hash='1' * 64)
        second = SimcBenchmarkExecution.objects.create(panel=panel, config_hash='2' * 64)
        calls = []

        def reconcile(execution):
            calls.append(execution.pk)
            if execution.pk == first.pk:
                raise RuntimeError('/secret/path')
            return execution

        with patch(
            'botend.services.simc_benchmark_scheduler.reconcile_execution', side_effect=reconcile,
        ):
            result = reconcile_pending_executions()
        self.assertEqual(calls, [first.pk, second.pk])
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['reconciled'], 1)
        self.assertNotIn('/secret', repr(result))

        backend = SimcBackendBinary.objects.create(identifier='scheduler', name='scheduler')
        task = SimcTask.objects.create(
            user_id=1, name='benchmark task', simc_profile_id=0, task_type=1,
            mode='comparison', backend=backend,
        )
        SimcBenchmarkCase.objects.create(
            execution=second, task=task, spec_key='s', scenario_key='c', profile_key='p',
            spec_label='s', scenario_label='c', profile_label='p', coordinate_hash='3' * 64,
        )
        with patch(
            'botend.services.simc_benchmark_scheduler.reconcile_execution', return_value=second,
        ) as targeted:
            self.assertEqual(reconcile_execution_for_task(task.pk), second)
            self.assertIsNone(reconcile_execution_for_task(task.pk + 999))
        targeted.assert_called_once()
