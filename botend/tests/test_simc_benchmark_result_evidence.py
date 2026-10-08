"""Real ORM contracts for the bounded, read-only candidate evidence reader."""
from types import SimpleNamespace

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import (
    SimcBackendBinary, SimcBenchmarkCase, SimcBenchmarkExecution,
    SimcBenchmarkPanel, SimcBenchmarkResult, SimcTask, SimulationRun,
)
from botend.services.simc_benchmark_execution import _expected_candidate_keys


class CandidateResultEvidenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.backend = SimcBackendBinary.objects.create(identifier='result-evidence', name='Evidence')
        cls.panel = SimcBenchmarkPanel.objects.create(
            name='Evidence', slug='result-evidence', created_by_id=701,
        )
        cls.execution = SimcBenchmarkExecution.objects.create(panel=cls.panel, config_hash='a' * 64)

    def task(self, keys=('a',), source=None, mode_params=None):
        return SimcTask.objects.create(
            user_id=701, name='Evidence', simc_profile_id=1, backend=self.backend,
            mode='comparison', current_status=2, source_task=source,
            mode_params=mode_params if mode_params is not None else {
                'initial_candidates': [{'candidate_key': key, 'params': {'large': 'x' * 10000}}
                                       for key in keys],
            },
        )

    def make_run(self, task, key='a', *, status='completed', summary=None, sequence=1, manifest=None):
        return SimulationRun.objects.create(
            task=task, sequence=sequence, candidate_key=key, status=status,
            result_summary=summary if summary is not None else {
                'dps': 1000.0, 'dps_error': 2.5, 'dps_error_pct': 0.25,
                'native_proof': {'unused': 'x' * 10000},
            }, resource_manifest=manifest,
        )

    def load(self, requests):
        from botend.services.simc_benchmark_result_evidence import load_candidate_result_evidence
        return load_candidate_result_evidence(requests)

    def test_owned_missing_or_unfinished_run_never_borrows_ancestor(self):
        ancestor = self.task()
        self.make_run(ancestor)
        for status in (None, 'pending', 'running', 'failed', 'cancelled'):
            with self.subTest(status=status):
                current = self.task(source=ancestor)
                if status:
                    self.make_run(current, status=status)
                key = (current.pk, 'a')
                self.assertEqual(self.load({key: 1000.0}), {key: None})

    def test_unowned_inherits_actual_run_provenance_and_immutable_dps(self):
        ancestor = self.task()
        case = SimcBenchmarkCase.objects.create(
            execution=self.execution, task=ancestor, spec_key='fury',
            scenario_key='single', profile_key='raid', coordinate_hash='c' * 64,
        )
        result = SimcBenchmarkResult.objects.create(case=case, candidate_key='a', dps=1000.0)
        slots = {name: {'content_hash': str(index) * 64} for index, name in enumerate((
            'talents', 'action_list', 'simulation_options', 'additional_simc_input',
            'player_identity', 'stat_overrides', 'equipment', 'output_options',
        ))}
        run = self.make_run(ancestor, manifest={
            'backend_version': 'frozen-declaration-not-attestation',
            'composition_manifest': {'slots': slots}, 'unused': 'x' * 10000,
        })
        current = self.task(keys=('b',), source=ancestor)
        key = (current.pk, 'a')
        before = (ancestor.mode_params, run.result_summary, result.dps)
        evidence = self.load({key: result.dps})[key]
        self.assertEqual(evidence['run_id'], run.pk)
        self.assertEqual(evidence['task_id'], ancestor.pk)
        self.assertEqual(evidence['case_id'], case.pk)
        self.assertEqual(evidence['execution_id'], self.execution.pk)
        self.assertEqual(evidence['dps_error'], 2.5)
        self.assertEqual(evidence['dps_error_pct'], 0.25)
        self.assertEqual(evidence['frozen_backend_version'], 'frozen-declaration-not-attestation')
        self.assertEqual(evidence['composition_slot_hashes'], {
            name: value['content_hash'] for name, value in slots.items()
            if name not in ('equipment', 'output_options')
        })
        self.assertNotIn('actual_build', evidence)
        ancestor.refresh_from_db()
        run.refresh_from_db()
        result.refresh_from_db()
        self.assertEqual((ancestor.mode_params, run.result_summary, result.dps), before)

    def test_error_absence_invalid_values_and_real_zero_remain_distinct(self):
        values = (None, -1, 'NaN', 'Infinity', '-Infinity', True, {}, [], 'bad', 0, 2.5)
        requests = {}
        for value in values:
            task = self.task()
            self.make_run(task, summary={'dps': 1000, 'dps_error': value, 'dps_error_pct': value})
            requests[(task.pk, 'a')] = 1000
        result = self.load(requests)
        for key, value in zip(requests, values):
            expected = value if type(value) in (int, float) and value >= 0 else None
            self.assertEqual(result[key]['dps_error'], expected)
            self.assertEqual(result[key]['dps_error_pct'], expected)
        task = self.task()
        self.make_run(task, summary={'dps': 1000})
        self.assertIsNone(self.load({(task.pk, 'a'): 1000})[(task.pk, 'a')]['dps_error'])

    def test_conflicting_dps_invalid_summary_and_duplicate_runs_fail_closed(self):
        summaries = ({'dps': 1001}, {'dps': True}, {'dps': 'NaN'}, {},
                     {'dps': 1000, 'valid': False}, {'dps': -1000})
        requests = {}
        for summary in summaries:
            task = self.task()
            self.make_run(task, summary=summary)
            requests[(task.pk, 'a')] = 1000
        duplicate = self.task()
        self.make_run(duplicate)
        self.make_run(duplicate, sequence=2)
        requests[(duplicate.pk, 'a')] = 1000
        self.assertEqual(self.load(requests), dict.fromkeys(requests))

    def test_candidate_ownership_matches_existing_contract_and_rejects_corruption(self):
        ancestor = self.task()
        self.make_run(ancestor)
        modes = (
            {'initial_candidates': [], 'request_manifest': {'candidates': [{'candidate_key': 'b'}]}},
            {'initial_candidates': 'bad', 'request_manifest': {'candidates': [{'candidate_key': 'b'}]}},
            {'initial_candidates': [{'candidate_key': 'a'}],
             'request_manifest': {'candidates': [{'candidate_key': 'b'}]}},
            {'request_manifest': {'candidates': [{'candidate_key': 'b'}]}},
            {'initial_candidates': [{'candidate_key': 'b'}, {}]},
            {'initial_candidates': [{'candidate_key': 'b'}, {'candidate_key': 'b'}]},
            {'initial_candidates': [None]}, {'initial_candidates': ['b']},
            {'initial_candidates': [{'candidate_key': True}]}, {},
        )
        for mode in modes:
            with self.subTest(mode=mode):
                keys = _expected_candidate_keys(SimpleNamespace(mode_params=mode))
                current = self.task(source=ancestor, mode_params=mode)
                key = (current.pk, 'a')
                evidence = self.load({key: 1000})[key]
                if keys is not None and 'a' not in keys:
                    self.assertEqual(evidence['task_id'], ancestor.pk)
                else:
                    self.assertIsNone(evidence)

    def test_cycles_missing_tasks_and_excess_depth_fail_closed(self):
        from botend.services.simc_benchmark_result_evidence import MAX_SOURCE_DEPTH
        first = self.task(keys=('b',))
        second = self.task(keys=('b',), source=first)
        SimcTask.objects.filter(pk=first.pk).update(source_task=second)
        requests = {(first.pk, 'a'): 1000, (987654321, 'a'): 1000}
        root = self.task()
        self.make_run(root)
        current = root
        for _ in range(MAX_SOURCE_DEPTH):
            current = self.task(keys=('b',), source=current)
        requests[(current.pk, 'a')] = 1000
        self.assertEqual(self.load(requests), dict.fromkeys(requests))

    def test_normal_batch_has_constant_query_budget_and_no_json_sort_or_writes(self):
        requests = {}
        for _ in range(20):
            task = self.task(keys=tuple(f'k{i}' for i in range(10)))
            for i in range(10):
                self.make_run(task, key=f'k{i}', sequence=i + 1)
                requests[(task.pk, f'k{i}')] = 1000
        with CaptureQueriesContext(connection) as queries:
            result = self.load(requests)
        self.assertEqual(len(result), 200)
        self.assertTrue(all(result.values()))
        self.assertLessEqual(len(queries), 3)
        for query in queries:
            sql = query['sql'].upper()
            self.assertTrue(sql.startswith('SELECT'), sql)
            self.assertNotIn('ORDER BY', sql)
            self.assertNotIn('CANDIDATE_PARAMS', sql)
            self.assertNotIn('NATIVE_PROOF', sql)
            self.assertNotIn('SIMC_RESOURCE_VERSION', sql)
        with self.assertNumQueries(0):
            self.assertEqual(self.load({}), {})

    def test_more_than_a_thousand_requested_keys_are_chunked_without_truncation(self):
        from botend.services.simc_benchmark_result_evidence import MAX_CANDIDATE_KEYS
        keys = [f'k{i}' for i in range(MAX_CANDIDATE_KEYS)]
        task = self.task(mode_params={'initial_candidates': [{'candidate_key': key} for key in keys]})
        SimulationRun.objects.bulk_create([
            SimulationRun(task=task, sequence=i + 1, candidate_key=key,
                          status='completed', result_summary={'dps': 1000, 'dps_error': 2})
            for i, key in enumerate(keys)
        ])
        other = self.task()
        self.make_run(other)
        requests = {(task.pk, key): 1000 for key in keys}
        requests[(other.pk, 'a')] = 1000
        with CaptureQueriesContext(connection) as queries:
            result = self.load(requests)
        self.assertEqual(set(result), set(requests))
        self.assertTrue(all(result.values()))
        self.assertEqual(len(queries), 6)

    def test_input_and_frozen_candidate_limits_are_bounded(self):
        from botend.services.simc_benchmark_result_evidence import MAX_CANDIDATE_KEYS
        task = self.task(keys=tuple(f'k{i}' for i in range(MAX_CANDIDATE_KEYS + 1)))
        self.make_run(task, key='k0')
        self.assertIsNone(self.load({(task.pk, 'k0'): 1000})[(task.pk, 'k0')])
        requests = {}
        for _ in range(21):
            task = self.task()
            self.make_run(task)
            requests[(task.pk, 'a')] = 1000
        with CaptureQueriesContext(connection) as queries:
            result = self.load(requests)
        self.assertTrue(all(result.values()))
        self.assertLessEqual(len(queries), 6)
