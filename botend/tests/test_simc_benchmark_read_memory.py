"""Bounded JSON reads preserve benchmark coverage and retry provenance."""
from copy import deepcopy
import re

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import (
    SimcApl, SimcBackendBinary, SimcBenchmarkCandidate, SimcBenchmarkCase,
    SimcBenchmarkExecution, SimcBenchmarkPanel, SimcBenchmarkProfile,
    SimcBenchmarkResult, SimcBenchmarkScenario, SimcBenchmarkSpec,
    SimcContentTemplate, SimcProfile, SimcTalentString, SimcTask, SimulationRun, WowItemSnapshot,
)
from botend.services import simc_benchmark_execution as service
from botend.services.simc_benchmark_config import build_execution_plan


class BenchmarkReadMemoryTests(TestCase):
    def setUp(self):
        self.backend = SimcBackendBinary.objects.create(identifier='read-memory', name='Test')
        self.apl = SimcApl.objects.create(name='APL', spec='warrior_fury', content='actions=/auto_attack', owner_user_id=1)
        self.template = SimcContentTemplate.objects.create(name='Template', spec='warrior_fury', content='iterations=1000', owner_user_id=1)
        self.profile = SimcProfile.objects.create(user_id=1, name='Profile', class_name='warrior', spec='warrior_fury')
        self.panel = SimcBenchmarkPanel.objects.create(name='Memory', slug='read-memory', created_by_id=1)
        spec = SimcBenchmarkSpec.objects.create(panel=self.panel, class_name='warrior', spec_key='warrior_fury', label='Fury', apl=self.apl, template=self.template, backend=self.backend)
        talent = SimcTalentString.objects.create(name='Talents', spec='warrior_fury', talent='CYEAoonA', owner_user_id=1)
        SimcBenchmarkProfile.objects.create(panel_spec=spec, profile=self.profile, talent_string=talent, label='Profile')
        SimcBenchmarkCandidate.objects.create(panel=self.panel, key='item', label='Item', candidate_type='gear_swap', params={'candidate_type': 'gear_swap', 'is_base': False, 'gear_swap': {'slot': 'trinket1', 'raw_value': ',id=123', 'item_id': 123, 'source': 'manual'}})
        for index in range(25):
            SimcBenchmarkScenario.objects.create(panel=self.panel, key=f'scenario-{index}', name=f'Scenario {index}', simulation_params={'iterations': 1000})
        WowItemSnapshot.objects.create(item_id=123, name='Item', item_class_id=4, item_subclass_id=0, inventory_type=12, metadata={'primary_stat_options': ['strength']})
        self.plan = build_execution_plan(self.panel, validate_for_execution=False, lock=False)
        self.base = SimcBenchmarkExecution.objects.create(panel=self.panel, config_hash='a' * 64, status='success', config_snapshot={'case_count': 25, 'run_count': 50, 'large_unused': 'x' * 100000})
        self.retry = SimcBenchmarkExecution.objects.create(panel=self.panel, config_hash='b' * 64, status='partial', config_snapshot={'case_count': 25, 'run_count': 25, 'large_unused': 'y' * 100000})
        self.task_ids = []
        for coordinate in self.plan['cases']:
            old_candidates = deepcopy(coordinate['candidates'])
            old_candidates[-1]['candidate_params']['unused_identity_field'] = 'old'
            source = self.task(coordinate, old_candidates)
            task = self.task(coordinate, [coordinate['candidates'][-1]], source)
            self.task_ids.append(task.pk)
            for execution, current, candidates in ((self.base, source, old_candidates), (self.retry, task, coordinate['candidates'])):
                case = SimcBenchmarkCase.objects.create(execution=execution, task=current, status='success', spec_key=coordinate['spec_key'], scenario_key=coordinate['scenario_key'], profile_key=coordinate['profile_key'], coordinate_hash=service._coordinate_input_identity(coordinate))
                for candidate in candidates:
                    SimcBenchmarkResult.objects.create(case=case, candidate_key=candidate['candidate_key'], dps=1000)

    def task(self, coordinate, candidates, source=None):
        return SimcTask.objects.create(user_id=1, name='Frozen', simc_profile_id=self.profile.pk, mode='comparison', profile=self.profile, apl=self.apl, template=self.template, backend=self.backend, simulation_params=coordinate['simulation_params'], source_task=source, mode_params={'large_unused': 'z' * 100000, 'request_manifest': {'candidates': candidates, 'large_unused': 'w' * 100000}})

    def assert_compact_task_reads(self, queries):
        reads = [q['sql'] for q in queries if re.search(r'FROM ["`]simc_task["`]', q['sql'])]
        self.assertTrue(reads)
        for sql in reads:
            self.assertNotIn('ORDER BY', sql.upper())
            self.assertIn('candidates', sql)
            self.assertNotRegex(sql, r'(?:SELECT |, )["`]simc_task["`]\.["`]mode_params["`](?: AS ["`][^"`]+["`])?(?:,| FROM)')
            ids = re.search(r'["`]id["`] IN \(([^)]+)\)', sql)
            self.assertIsNotNone(ids, sql)
            self.assertLessEqual(len(ids.group(1).split(',')), 20, sql)
        self.assertLessEqual(len(reads), 5, reads)

    def test_summary_reads_bounded_candidates_and_scalar_snapshot_counts(self):
        with CaptureQueriesContext(connection) as queries:
            summary = service.summarize_panel_coverage_counts([self.panel])[self.panel.pk]
        self.assertEqual(summary, {'aggregate_baseline_execution_id': self.base.pk, 'coordinates': 25, 'candidate_runs': 50, 'current_plan_runs': 50, 'plan_delta_runs': 0, 'available_results': 50, 'missing_results': 0, 'source_executions': [{'execution_id': self.retry.pk, 'results': 50}]})
        self.assert_compact_task_reads(queries)
        snapshot_reads = [q['sql'] for q in queries if re.search(r'FROM ["`]simc_benchmark_execution["`]', q['sql']) and 'case_count' in q['sql']]
        self.assertEqual(len(snapshot_reads), 1)
        self.assertNotRegex(snapshot_reads[0], r'(?:SELECT |, )["`]simc_benchmark_execution["`]\.["`]config_snapshot["`](?: AS ["`][^"`]+["`])?(?:,| FROM)')
        self.assertIn('run_count', snapshot_reads[0])
        self.panel.aggregate_baseline_execution = self.retry
        self.panel.save(update_fields=['aggregate_baseline_execution'])
        explicit = service.summarize_panel_coverage_counts([self.panel])[self.panel.pk]
        self.assertEqual((explicit['aggregate_baseline_execution_id'], explicit['candidate_runs'], explicit['plan_delta_runs']), (self.retry.pk, 25, 25))

    def test_identity_loader_has_compact_schema_complete_chain_and_cycle_safety(self):
        loader = getattr(service, '_load_task_candidate_identity_rows', None)
        self.assertIsNotNone(loader)
        with CaptureQueriesContext(connection) as queries:
            rows = loader(self.task_ids)
        self.assertEqual(len(rows), 50)
        self.assert_compact_task_reads(queries)
        task_id = self.task_ids[0]
        row = rows[task_id]
        self.assertEqual(set(row), {'source_task_id', 'identities'})
        candidate = self.plan['cases'][0]['candidates'][-1]
        self.assertEqual(row['identities'], {candidate['candidate_key']: service._candidate_input_identity(candidate)})
        self.assertTrue(all(isinstance(value, str) and len(value) == 64 for value in row['identities'].values()))
        source_id = row['source_task_id']
        SimcTask.objects.filter(pk=source_id).update(source_task_id=task_id)
        # Coverage must still apply source then retry, including same-key overrides.
        counts = service._reusable_result_counts_for_plans([self.panel], {self.panel.pk: self.plan})
        self.assertEqual(counts[self.panel.pk], {'available': 50, 'sources': {self.retry.pk: 50}})

    def test_result_projection_batches_history_and_keeps_retry_overlay_atomic(self):
        with CaptureQueriesContext(connection) as queries:
            matches = service._reusable_candidate_tasks_by_coordinate(
                self.panel, coordinate_plans=self.plan['cases'],
            )
        self.assertEqual(len(matches), 25)
        for coordinate in self.plan['cases']:
            available = matches[service._coordinate_input_identity(coordinate)]
            for candidate in coordinate['candidates']:
                match = available[service._candidate_input_identity(candidate)]
                self.assertIn(match['task'].pk, self.task_ids)
                self.assertEqual(match['result'].case.execution_id, self.retry.pk)
        case_reads = [q['sql'] for q in queries if re.search(
            r'FROM ["`]simc_benchmark_case["`]', q['sql'],
        )]
        self.assertGreater(len(case_reads), 2)
        self.assertTrue(all(re.search(r'LIMIT 20(?:$| )', sql) for sql in case_reads))
        self.assert_compact_task_reads(queries)
        # An unfinished targeted replacement must expose only the old baseline;
        # its newer candidate identity is not available until the batch succeeds.
        self.retry.config_snapshot['result_publication'] = 'atomic_targeted'
        self.retry.save(update_fields=['config_snapshot'])
        matches = service._reusable_candidate_tasks_by_coordinate(
            self.panel, coordinate_plans=self.plan['cases'],
        )
        for coordinate in self.plan['cases']:
            available = matches[service._coordinate_input_identity(coordinate)]
            baseline, changed = coordinate['candidates']
            self.assertEqual(
                available[service._candidate_input_identity(baseline)]['result'].case.execution_id,
                self.base.pk,
            )
            self.assertNotIn(service._candidate_input_identity(changed), available)

    def test_full_detail_audit_reads_retry_ancestor_without_large_payloads(self):
        coordinate = self.plan['cases'][0]
        task_id = self.task_ids[0]
        source_id = SimcTask.objects.values_list('source_task_id', flat=True).get(pk=task_id)
        large = {'unused': 'x' * 1024 * 1024}
        SimulationRun.objects.create(
            task_id=source_id, sequence=1, candidate_key='baseline', status='completed',
            resource_manifest={'backend_version': 'a' * 40, **large},
            candidate_params=large, display_metadata=large, result_summary=large,
        )
        with CaptureQueriesContext(connection) as queries:
            output = service.serialize_incremental_panel_results(
                self.panel, include_details=True,
                coordinate_filter={key: coordinate[key] for key in (
                    'spec_key', 'scenario_key', 'profile_key',
                )},
            )
        self.assertEqual(output['coordinates'][0]['audit']['backend_version'], 'a' * 40)
        self.assertEqual(len(output['coordinates'][0]['candidates']), 2)
        projections = [q['sql'].split(' FROM ', 1)[0] for q in queries
                       if q['sql'].startswith('SELECT')]
        for field in ('mode_params', 'analysis_result', 'result_summary', 'ext'):
            self.assertFalse(any(re.search(
                rf'(?:SELECT |, )["`]simc_task["`]\.["`]{field}["`](?:,| FROM| AS)', sql,
            ) for sql in projections), f'full detail read task {field}')
        for field in ('resource_manifest', 'candidate_params', 'display_metadata', 'result_summary'):
            self.assertFalse(any(re.search(
                rf'(?:SELECT |, )["`]simc_simulation_run["`]\.["`]{field}["`](?:,| FROM| AS)', sql,
            ) for sql in projections), f'full detail read run {field}')

    def test_legacy_baseline_ties_use_case_count_then_execution_id(self):
        self.base.config_snapshot = {'case_count': 1, 'run_count': 25}
        self.base.save(update_fields=['config_snapshot'])
        newest = SimcBenchmarkExecution.objects.create(panel=self.panel, config_hash='c' * 64, config_snapshot={'case_count': 25, 'run_count': 25})
        summary = service.summarize_panel_coverage_counts([self.panel])[self.panel.pk]
        self.assertEqual((summary['aggregate_baseline_execution_id'], summary['coordinates'], summary['candidate_runs']), (newest.pk, 25, 25))
