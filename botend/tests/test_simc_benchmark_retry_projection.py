"""Automatic retry authority/projection regression and real row-lock races."""
from datetime import timedelta
import queue
import threading
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from botend.models import SimcBenchmarkResult, SimcTask, SimulationRun
from botend.services import simc_benchmark_execution as execution_service
from botend.services.simc_worker import SimcWorker
from botend.tests import test_simc_benchmark_execution as fixtures


@override_settings(SIMC_WORKER_STALE_SECONDS=60, SIMC_WORKER_MAX_ATTEMPTS=3)
class BenchmarkRetryProjectionTests(TransactionTestCase):
    # Reuse only the real frozen-resource fixture, not its inherited test methods.
    user_id = fixtures.SimcBenchmarkExecutionTests.user_id
    _create = fixtures.SimcBenchmarkExecutionTests._create

    def setUp(self):
        fixtures.SimcBenchmarkExecutionTests.setUp(self)
        fixtures.WowItemSnapshot.objects.create(
            item_id=123, name='Test trinket', item_class_id=4,
            inventory_type=12, metadata={'primary_stat_options': ['strength']},
        )
        self.execution = self._create()
        self.case = self.execution.cases.get()
        self.task = self.case.task
        self.keys = [c['candidate_key'] for c in self.task.mode_params['initial_candidates']]
        self.assertGreaterEqual(len(self.keys), 2, repr((self.keys, self.execution.config_snapshot)))
        for sequence, key in enumerate(self.keys, 1):
            SimulationRun.objects.create(
                task=self.task, sequence=sequence, candidate_key=key,
                status='completed' if sequence == 1 else 'running',
                result_summary={'dps': 100.0} if sequence == 1 else {},
            )
        SimcTask.objects.filter(pk=self.task.pk).update(current_status=1)
        execution_service.reconcile_execution_case(self.execution, self.case.pk)
        self.assertEqual(self.case.results.get().dps, 100.0)
        self.make_stale()
        self.worker = SimcWorker(monitor=object())

    def make_stale(self):
        SimcTask.objects.filter(pk=self.task.pk).update(
            modified_time=timezone.now() - timedelta(minutes=5),
        )

    def recover(self):
        self.assertEqual(self.worker.recover_stale_tasks(), 1)
        self.case.refresh_from_db()
        self.assertNotEqual(self.case.task_id, self.task.pk)
        return self.case.task

    def test_rebind_preserves_success_and_retries_only_unfinished(self):
        source_run = self.task.simulation_runs.get(candidate_key=self.keys[0])
        frozen = SimulationRun.objects.filter(pk=source_run.pk).values().get()
        result_id = self.case.results.get().pk
        retry = self.recover()
        self.assertEqual(self.case.status, 'pending')
        self.assertEqual(self.case.error_detail, '')
        self.assertEqual(self.case.results.get().pk, result_id)
        self.assertEqual(retry.source_task_id, self.task.pk)
        self.assertEqual([c['candidate_key'] for c in retry.mode_params['initial_candidates']], self.keys[1:])
        self.assertEqual(retry.mode_params['request_manifest']['candidates'], retry.mode_params['initial_candidates'])
        self.assertEqual(list(retry.simulation_runs.values_list('candidate_key', flat=True)), self.keys[1:])
        execution_service.reconcile_execution_case(self.execution, self.case.pk)
        self.assertEqual(self.case.results.get().dps, 100)
        retry.simulation_runs.update(status='completed', result_summary={'dps': 200.0})
        SimcTask.objects.filter(pk=retry.pk).update(current_status=2)
        execution_service.reconcile_execution_case(self.execution, self.case.pk)
        self.assertEqual(self.case.results.get(candidate_key=self.keys[0]).dps, 100)
        self.assertEqual(self.case.results.count(), len(self.keys))
        self.assertEqual(SimulationRun.objects.filter(pk=source_run.pk).values().get(), frozen)
        self.assertEqual(execution_service._runs_through_source_chain(retry)[self.keys[0]].pk, source_run.pk)

    def test_pending_retry_reprojects_unowned_source_results(self):
        self.recover()
        self.case.results.all().delete()
        execution_service.reconcile_execution(self.execution)
        self.assertEqual(self.case.results.get().dps, 100)

    def test_missing_run_preserves_exact_frozen_candidate(self):
        from copy import deepcopy
        candidate = deepcopy(self.task.mode_params['initial_candidates'][-1])
        candidate.update(candidate_label='Frozen label', round_number=7,
                         display_metadata={'frozen': ['identity']})
        candidate['candidate_params']['frozen_nested'] = {'bonus_ids': [11, 22]}
        self.task.mode_params['initial_candidates'][-1] = candidate
        self.task.save(update_fields=['mode_params'])
        self.task.simulation_runs.filter(candidate_key=candidate['candidate_key']).delete()
        self.make_stale()
        retry = self.recover()
        self.assertEqual(retry.mode_params['initial_candidates'][-1], candidate)
        run = retry.simulation_runs.get(candidate_key=candidate['candidate_key'])
        self.assertEqual(run.candidate_params, candidate['candidate_params'])
        self.assertEqual(run.display_metadata, candidate['display_metadata'])
        self.assertEqual((run.candidate_label, run.round_number), ('Frozen label', 7))

    def test_all_completed_stale_task_finishes_without_replacement(self):
        self.task.simulation_runs.update(status='completed', result_summary={'dps': 100.0})
        self.assertEqual(self.worker.recover_stale_tasks(), 1)
        self.task.refresh_from_db()
        self.case.refresh_from_db()
        self.assertEqual(self.case.task_id, self.task.pk)
        self.assertEqual(self.task.current_status, 2)
        self.assertEqual(self.task.error_detail, '')
        self.assertFalse(self.task.reruns.exists())
        execution_service.reconcile_execution_case(self.execution, self.case.pk)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, 'success')
        self.assertEqual(self.case.results.count(), len(self.keys))

    def test_manual_full_retry_still_masks_completed_source_before_runs_exist(self):
        from botend.services.task_rerun import create_rerun
        retry = create_rerun(self.task.pk, self.user_id)
        self.assertEqual(execution_service._runs_through_source_chain(retry), {})

    def test_conditional_success_half_is_inherited_but_pair_binding_stays_strict(self):
        import copy
        import gzip
        import json
        from pathlib import Path
        from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report
        from botend.tests.test_simc_conditional_core import fixture

        evidence = json.loads(gzip.decompress((Path(__file__).with_name('fixtures') /
            'conditional_run2_evidence.json.gz').read_bytes()))['sides']
        _, _, authorization = fixture()
        sides = {}
        for mode in ('normal', 'control'):
            data = evidence[mode]
            sides[mode] = validate_equipment_effect_report(
                data['html'], data['params'], native_proof=data['proof'],
                prepared_input=data['prepared'], report_json=data['report'],
                conditional_authorization=authorization,
            )
            self.assertEqual(sides[mode]['status'], 'pair_pending')
        normal_key, control_key = self.keys[:2]
        self.task.simulation_runs.filter(candidate_key=normal_key).update(
            result_summary={'dps': 100, 'equipment_effect_validation': sides['normal']},
        )
        source_id = self.task.simulation_runs.get(candidate_key=normal_key).pk
        retry = self.recover()
        self.assertFalse(retry.simulation_runs.filter(candidate_key=normal_key).exists())
        requests = {(retry.pk, normal_key), (retry.pk, control_key)}
        values = execution_service._equipment_effect_validations(requests)
        self.assertIsNot(execution_service._paired_effect_validation(
            values[(retry.pk, normal_key)], values[(retry.pk, control_key)],
        )['valid'], True)
        retry.simulation_runs.filter(candidate_key=control_key).update(
            status='completed', result_summary={'dps': 90, 'equipment_effect_validation': sides['control']},
        )
        values = execution_service._equipment_effect_validations(requests)
        self.assertTrue(execution_service._paired_effect_validation(
            values[(retry.pk, normal_key)], values[(retry.pk, control_key)],
        )['valid'])
        wrong = copy.deepcopy(sides['control'])
        wrong['conditional_witness']['context_hash'] = 'different-frozen-context'
        retry.simulation_runs.filter(candidate_key=control_key).update(
            result_summary={'dps': 90, 'equipment_effect_validation': wrong},
        )
        values = execution_service._equipment_effect_validations(requests)
        invalid = execution_service._paired_effect_validation(
            values[(retry.pk, normal_key)], values[(retry.pk, control_key)],
        )
        self.assertFalse(invalid['valid'])
        self.assertEqual(invalid['reason'], 'pair binding mismatch')
        self.assertEqual(execution_service._runs_through_source_chain(retry)[normal_key].pk, source_id)

    def test_same_attempt_dps_conflict_still_rejected(self):
        self.task.simulation_runs.filter(candidate_key=self.keys[0]).update(result_summary={'dps': 201})
        with self.assertRaisesMessage(ValidationError, '已有结果与冻结 Run DPS 不一致'):
            execution_service.reconcile_execution_case(self.execution, self.case.pk)
        self.assertEqual(self.case.results.get().dps, 100)

    def test_projection_reset_failure_rolls_back_task_copy_and_rebind(self):
        original_delete = type(SimcBenchmarkResult.objects.all()).delete

        def fail_projection_delete(qs, *args, **kwargs):
            if qs.model is SimcBenchmarkResult:
                raise RuntimeError('injected projection reset failure')
            return original_delete(qs, *args, **kwargs)

        with patch.object(type(SimcBenchmarkResult.objects.all()), 'delete', fail_projection_delete):
            self.assertEqual(self.worker.recover_stale_tasks(), 0)
        self.case.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.case.task_id, self.task.pk)
        self.assertEqual(self.task.current_status, 1)
        self.assertFalse(SimcTask.objects.filter(source_task=self.task).exists())
        self.assertEqual(self.case.results.get().dps, 100)

    def test_failed_only_retry_keeps_unretried_source_candidates(self):
        from botend.services.task_rerun import create_rerun
        retry = create_rerun(self.task.pk, self.user_id)
        retry.mode_params['initial_candidates'] = retry.mode_params['initial_candidates'][1:]
        retry.save(update_fields=['mode_params'])
        runs = execution_service._runs_through_source_chain(retry)
        self.assertEqual(set(runs), {self.keys[0]})
        self.assertEqual(runs[self.keys[0]].result_summary['dps'], 100)

    def _race(self, first, second, hook_target, hook):
        """Hold a real transaction at its hook, then run the competing connection."""
        entered, release, second_started, second_done = (threading.Event() for _ in range(4))
        errors, values = queue.Queue(), queue.Queue()
        original = hook

        def pause(*args, **kwargs):
            value = original(*args, **kwargs)
            if threading.current_thread().name == 'projection-first':
                entered.set()
                if not release.wait(10):
                    raise AssertionError('race release timed out')
            return value

        def run(fn, is_second=False):
            close_old_connections()
            try:
                if is_second:
                    second_started.set()
                values.put(fn())
            except BaseException as exc:
                errors.put(exc)
            finally:
                connection.close()
                if is_second:
                    second_done.set()

        with patch(hook_target, pause):
            a = threading.Thread(target=run, args=(first,), name='projection-first', daemon=True)
            b = threading.Thread(target=run, args=(second, True), name='recovery-second', daemon=True)
            a.start()
            try:
                self.assertTrue(entered.wait(10), 'first transaction did not reach hook')
                b.start()
                self.assertTrue(second_started.wait(5))
                self.assertFalse(second_done.wait(.3), 'competing mutation escaped row-lock fence')
            finally:
                release.set()
                a.join(15)
                if b.ident is not None:
                    b.join(15)
            self.assertFalse(a.is_alive() or b.is_alive(), 'deadlock/hung concurrent transaction')
        if not errors.empty():
            raise errors.get()
        return list(values.queue)

    @skipUnlessDBFeature('has_select_for_update')
    def test_innodb_projection_snapshot_then_recovery_is_serialized(self):
        for single_case in (True, False):
            with self.subTest(single_case=single_case):
                project = (lambda: execution_service.reconcile_execution_case(self.execution, self.case.pk)) \
                    if single_case else (lambda: execution_service.reconcile_execution(self.execution))
                values = self._race(
                    project, self.worker.recover_stale_tasks,
                    'botend.services.simc_benchmark_execution._summarize_live_execution',
                    execution_service._summarize_live_execution,
                )
                self.assertIn(1, values)
                self.case.refresh_from_db()
                self.assertEqual(self.case.results.get().dps, 100)
                self.assertEqual(self.case.status, 'pending')
                # Prepare the replacement for the reverse/full-path iteration.
                self.task = self.case.task
                SimcTask.objects.filter(pk=self.task.pk).update(current_status=1)
                self.make_stale()

    @skipUnlessDBFeature('has_select_for_update')
    def test_innodb_recovery_then_projection_reads_new_authority(self):
        from botend.services.task_rerun import create_rerun
        self._race(
            self.worker.recover_stale_tasks,
            lambda: execution_service.reconcile_execution_case(self.execution, self.case.pk),
            'botend.services.simc_worker.create_rerun', create_rerun,
        )
        self.case.refresh_from_db()
        self.assertNotEqual(self.case.task_id, self.task.pk)
        self.assertEqual(self.case.status, 'pending')
        self.assertEqual(self.case.results.get().dps, 100)

    @skipUnlessDBFeature('has_select_for_update')
    def test_innodb_cancel_then_recovery_does_not_deadlock_or_retry(self):
        self._race(
            lambda: execution_service.cancel_execution(self.execution, requested_by=self.user_id),
            self.worker.recover_stale_tasks,
            'botend.services.simc_benchmark_execution._ensure_panel_not_purging',
            execution_service._ensure_panel_not_purging,
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.task_id, self.task.pk)
        self.assertFalse(SimcTask.objects.filter(source_task=self.task).exists())
        self.assertEqual(self.case.status, 'cancelled')
