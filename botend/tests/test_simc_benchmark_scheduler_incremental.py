"""Periodic reconciliation must bound live Panel lock work to one Case."""
import queue
import threading
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext

from botend.models import SimcBenchmarkCase, SimcBenchmarkExecution, SimcBenchmarkPanel, SimcTask, SimulationRun
from botend.services import simc_benchmark_execution as execution_service
from botend.services import simc_benchmark_scheduler as scheduler
from botend.tests import test_simc_benchmark_execution as fixtures


class IncrementalSchedulerTests(TransactionTestCase):
    user_id = fixtures.SimcBenchmarkExecutionTests.user_id
    _create = fixtures.SimcBenchmarkExecutionTests._create

    def setUp(self):
        fixtures.SimcBenchmarkExecutionTests.setUp(self)
        fixtures.WowItemSnapshot.objects.create(
            item_id=123, name='Test trinket', item_class_id=4,
            inventory_type=12, metadata={'primary_stat_options': ['strength']},
        )
        for number in range(4):
            fixtures.SimcBenchmarkScenario.objects.create(
                panel=self.panel, key=f'extra-{number}', name=f'Extra {number}',
                simulation_params={'iterations': 1000},
            )
        self.execution = self._create()
        self.cases = list(self.execution.cases.order_by('id'))
        scheduler._reconcile_cursor = 0
        if hasattr(scheduler, '_reconcile_case_cursors'):
            scheduler._reconcile_case_cursors.clear()

    def tick(self):
        result = scheduler.reconcile_pending_executions()
        self.assertEqual(result['failed'], 0, result)
        self.assertEqual(result['reconciled'], 1)
        return result

    def complete(self, case):
        task = case.task
        for sequence, candidate in enumerate(task.mode_params['initial_candidates'], 1):
            SimulationRun.objects.create(
                task=task, sequence=sequence, candidate_key=candidate['candidate_key'],
                status='completed', result_summary={'dps': 100.0},
            )
        SimcTask.objects.filter(pk=task.pk).update(current_status=2)

    def test_live_sweep_is_bounded_fair_and_releases_each_case_transaction(self):
        seen = []
        real = execution_service._summarize_live_execution
        def summarize(execution, case_id=None):
            self.assertIsNotNone(case_id, 'live maintenance traversed the whole Execution')
            self.assertTrue(connection.in_atomic_block)
            seen.append(case_id)
            return real(execution, case_id=case_id)
        original = scheduler.reconcile_execution_case
        def reconcile(execution, case_id):
            self.assertFalse(connection.in_atomic_block, 'scheduler held an outer transaction')
            self.assertEqual(execution.get_deferred_fields(), {
                f.attname for f in execution._meta.concrete_fields if f.attname != 'id'
            })
            result = original(execution, case_id)
            self.assertFalse(connection.in_atomic_block)
            return result
        with patch.object(execution_service, '_summarize_live_execution', side_effect=summarize), \
                patch.object(scheduler, 'reconcile_execution_case', side_effect=reconcile):
            with CaptureQueriesContext(connection) as queries:
                self.tick()
            self.assertLessEqual(len(seen), 2)
            for _ in range(3):
                self.tick()
        self.assertEqual(set(seen), {case.pk for case in self.cases})
        first_execution_select = next(q['sql'] for q in queries if 'simc_benchmark_execution' in q['sql'] and q['sql'].startswith('SELECT'))
        self.assertNotIn('config_snapshot', first_execution_select)

    def test_lagging_pending_cases_recover_terminal_tasks_and_publish(self):
        for case in self.cases:
            self.complete(case)
        self.assertEqual(set(self.execution.cases.values_list('status', flat=True)), {'pending'})
        for _ in range(3):
            self.tick()
        self.execution.refresh_from_db()
        self.panel.refresh_from_db()
        self.assertEqual(self.execution.status, 'success')
        self.assertIsNotNone(self.execution.completed_at)
        self.assertEqual(self.panel.published_execution_id, self.execution.pk)
        self.assertEqual(set(self.execution.cases.values_list('status', flat=True)), {'success'})
        self.assertTrue(all(case.results.exists() for case in self.cases))

    def test_queued_zero_run_retry_repairs_inherited_results(self):
        from botend.services.task_rerun import create_rerun
        case = self.cases[-1]
        self.complete(case)
        source = case.task
        candidates = source.mode_params['initial_candidates']
        retry = create_rerun(source.pk, self.user_id)
        params = retry.mode_params
        params['initial_candidates'] = candidates[1:]
        params['request_manifest']['candidates'] = candidates[1:]
        retry.mode_params = params
        retry.save(update_fields=['mode_params'])
        SimcBenchmarkCase.objects.filter(pk=case.pk).update(task=retry, status='pending')
        self.assertFalse(retry.simulation_runs.exists())
        for _ in range(3):
            self.tick()
        self.assertEqual(case.results.get().candidate_key, candidates[0]['candidate_key'])
        self.assertEqual(case.results.get().dps, 100)
        self.execution.refresh_from_db()
        self.assertIsNone(self.execution.completed_at)

    def test_terminal_case_gate_revalidates_and_noop_uses_original_finalizer(self):
        self.execution.cases.update(status='success')
        real = scheduler.reconcile_execution
        with patch.object(scheduler, 'reconcile_execution', wraps=real) as full:
            self.tick()
        full.assert_called_once()
        self.execution.refresh_from_db()
        self.assertIsNone(self.execution.completed_at)  # Tasks are still pending.
        self.execution.cases.all().delete()
        self.execution.config_snapshot = {'execution_mode': 'supplement', 'case_count': 0}
        self.execution.save(update_fields=['config_snapshot'])
        self.tick()
        self.execution.refresh_from_db()
        self.panel.refresh_from_db()
        self.assertEqual(self.execution.status, 'success')
        self.assertIsNotNone(self.execution.completed_at)
        self.assertIsNone(self.panel.published_execution_id)

    def test_discovery_races_rebind_and_purge_are_revalidated(self):
        from botend.models import SimcBenchmarkPurgeTask
        from botend.services.task_rerun import create_rerun
        case = self.cases[0]
        self.complete(case)
        retry = create_rerun(case.task_id, self.user_id)
        original = scheduler._pending_case_ids
        def rebind(execution_id):
            ids = original(execution_id)
            SimcBenchmarkCase.objects.filter(pk=case.pk).update(task=retry, status='pending')
            return ids
        with patch.object(scheduler, '_pending_case_ids', side_effect=rebind):
            self.tick()
        case.refresh_from_db()
        self.assertEqual(case.task_id, retry.pk)
        self.assertEqual(case.status, 'pending')
        self.assertFalse(case.results.exists())  # New ownership masks old success.
        before = list(self.execution.cases.values('id', 'status', 'task_id'))
        def purge(execution_id):
            ids = original(execution_id)
            SimcBenchmarkPurgeTask.objects.create(
                panel=self.panel, panel_id_snapshot=self.panel.pk,
                panel_name='test', requested_by_id=self.user_id,
                fingerprint='a' * 64, batch_id='scheduler-purge', status='pending',
                plan={'task_ids': [retry.pk]},
            )
            return ids
        with patch.object(scheduler, '_pending_case_ids', side_effect=purge):
            result = scheduler.reconcile_pending_executions()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['errors'][0]['error'], 'BenchmarkExecutionConflict')
        self.assertEqual(list(self.execution.cases.values('id', 'status', 'task_id')), before)
        self.panel.refresh_from_db()
        self.assertIsNone(self.panel.published_execution_id)

    def test_failed_case_does_not_starve_siblings_and_cursor_history_is_pruned(self):
        original = scheduler.reconcile_execution_case
        seen = []
        def reconcile(execution, case_id):
            seen.append(case_id)
            if case_id == self.cases[0].pk:
                raise RuntimeError('bad case')
            return original(execution, case_id)
        with patch.object(scheduler, 'reconcile_execution_case', side_effect=reconcile):
            result = scheduler.reconcile_pending_executions()
            self.assertEqual(result['failed'], 1)
            self.tick()
            self.tick()
        self.assertEqual(set(seen), {case.pk for case in self.cases})
        self.assertIn(self.execution.pk, scheduler._reconcile_case_cursors)
        from django.utils import timezone
        SimcBenchmarkExecution.objects.filter(pk=self.execution.pk).update(completed_at=timezone.now())
        result = scheduler.reconcile_pending_executions()
        self.assertEqual(result['selected'], 0)
        self.assertEqual(scheduler._reconcile_case_cursors, {})

    @skipUnlessDBFeature('has_select_for_update_nowait')
    def test_innodb_panel_mutex_available_between_cases(self):
        errors = queue.Queue()
        checked = []
        original = scheduler.reconcile_execution_case
        def probe():
            close_old_connections()
            try:
                with transaction.atomic():
                    SimcBenchmarkPanel.objects.select_for_update(nowait=True).get(pk=self.panel.pk)
                    checked.append(True)
            except BaseException as exc:
                errors.put(exc)
            finally:
                close_old_connections()
        def reconcile(execution, case_id):
            result = original(execution, case_id)
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            if not errors.empty():
                raise errors.get()
            return result
        with patch.object(scheduler, 'reconcile_execution_case', side_effect=reconcile):
            self.tick()
        self.assertEqual(len(checked), 2)
