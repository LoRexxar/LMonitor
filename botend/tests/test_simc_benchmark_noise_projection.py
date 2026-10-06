"""Real ORM regression for presentation-only Benchmark item-level ties."""
from copy import deepcopy
import hashlib

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import (
    SimcBenchmarkCandidate, SimcBenchmarkExecution, SimcBenchmarkResult,
    SimcTask, SimulationRun,
)
from botend.services.simc_benchmark_execution import (
    reconcile_execution, serialize_incremental_panel_results, serialize_public_execution,
)
from simc_equipment_control import control_key


class BenchmarkNoiseProjectionTests(TestCase):
    def setUp(self):
        from botend.tests.test_simc_equipment_control import EquipmentControlBenchmarkTests
        self.fixtures = EquipmentControlBenchmarkTests()
        self.fixtures.setUp()
        self.panel = self.fixtures.panel
        self.panel.is_public = True
        self.panel.save(update_fields=['is_public'])
        for base in list(self.panel.candidates.all()):
            for index, level in enumerate((289, 302, 315)):
                params = deepcopy(base.params)
                swap = params['gear_swap']
                swap.update(item_level=level, raw_value=f",id={swap['item_id']},ilevel={level}")
                if index == 0:
                    base.params = params
                    base.save(update_fields=['params'])
                else:
                    SimcBenchmarkCandidate.objects.create(
                        panel=self.panel, key=f'{base.key}-{level}', label=f'{base.label} {level}',
                        candidate_type=base.candidate_type, params=params,
                    )
        self.execution = self.fixtures._create(execution_mode='full')
        self.task = self.execution.cases.get().task
        self.task.current_status = 2
        self.task.save(update_fields=['current_status'])
        slots = {name: {'content_hash': hashlib.sha256(f'fixture:{name}'.encode()).hexdigest()}
                 for name in ('talents', 'action_list', 'simulation_options', 'player_identity')}
        values = {'baseline': 1000}
        for index, suffix in enumerate(('', '-302', '-315')):
            values[f'trinket{suffix}'] = 1200.6 - index * 0.3
            values[f'ring{suffix}'] = 1600.6 - index * 0.3
            values[control_key(f'ring{suffix}')] = 1500
        for index, candidate in enumerate(self.task.mode_params['initial_candidates'], 1):
            key = candidate['candidate_key']
            self.fixtures._run(self.task, index, 'completed', key, dps=values[key])
            run = SimulationRun.objects.get(task=self.task, candidate_key=key)
            run.result_summary = {**run.result_summary, 'dps_error': 1.0, 'dps_error_pct': 0.1,
                                  'equipment_effect_validation': {'schema_version': 1, 'status': 'valid', 'valid': True}}
            run.resource_manifest = {'backend_version': 'fixture-declaration-not-build',
                                     'composition_manifest': {'slots': slots}}
            run.save(update_fields=['result_summary', 'resource_manifest'])
        reconcile_execution(self.execution)

    def snapshot(self):
        return [list(model.objects.order_by('pk').values()) for model in (
            SimulationRun, SimcTask, SimcBenchmarkResult, SimcBenchmarkExecution)]

    def projections(self):
        full = serialize_incremental_panel_results(self.panel)['coordinates'][0]['candidates']
        light = serialize_incremental_panel_results(self.panel, include_details=False)['coordinates'][0]['candidates']
        public = serialize_public_execution(self.execution)
        self.assertEqual(public['status'], 'ready')
        public = public['execution']['cases'][0]['candidates']
        for rows in (light, public):
            self.assertTrue(all('task_id' not in row for row in rows))
        return [{row['key']: row for row in rows} for rows in (full, light, public)]

    def test_three_levels_tie_in_both_projections_without_mutating_facts_or_seal(self):
        before = self.snapshot()
        projections = self.projections()
        for rows in projections:
            self.assertEqual(rows['baseline']['dps'], 1000)
            self.assertNotIn('noise_adjustment', rows['baseline'])
            for index, suffix in enumerate(('', '-302', '-315')):
                trinket, ring = rows[f'trinket{suffix}'], rows[f'ring{suffix}']
                self.assertAlmostEqual(trinket['display_dps'], 1200.3)
                self.assertAlmostEqual(trinket['dps'], 1200.6 - index * 0.3)
                self.assertAlmostEqual(ring['effect_delta_percent'], 100.3 / 1500 * 100)
                self.assertAlmostEqual(ring['gain_dps'], 100.6 - index * 0.3)
                self.assertAlmostEqual(ring['gain_percent'], (100.6 - index * 0.3) / 1500 * 100)
                for item, expected_id in ((trinket, 123), (ring, 456)):
                    self.assertEqual(item['item_id'], expected_id)
                    self.assertEqual(item['item_level'], (289, 302, 315)[index])
                    self.assertTrue(item['item_variant_key'])
                    self.assertEqual(item['noise_adjustment']['kind'], 'within_reported_error')
                    self.assertNotIn('composition_slot_hashes', item)
        for key in projections[0]:
            for field in ('display_dps', 'effect_delta_percent', 'noise_adjustment', 'item_variant_key'):
                self.assertEqual([rows[key].get(field) for rows in projections],
                                 [projections[0][key].get(field)] * 3)
        self.assertEqual(self.snapshot(), before)
        self.execution.result_hash = '0' * 64
        self.execution.save(update_fields=['result_hash'])
        self.assertEqual(serialize_public_execution(self.execution)['status'], 'not_ready')

    def test_unknown_middle_error_is_visible_and_blocks_ties(self):
        for key in ('trinket-302', control_key('ring-302')):
            run = SimulationRun.objects.get(task=self.task, candidate_key=key)
            run.result_summary.pop('dps_error')
            run.save(update_fields=['result_summary'])
        before = self.snapshot()
        for rows in self.projections():
            self.assertEqual(len(rows), 7)
            self.assertTrue(all('noise_adjustment' not in row for row in rows.values()))
            self.assertAlmostEqual(rows['ring-302']['effect_delta_percent'], 100.3 / 1500 * 100)
        self.assertEqual(self.snapshot(), before)

    def test_inherited_middle_actual_task_does_not_merge_with_projection_task(self):
        keys = {'trinket-302', 'ring-302', control_key('ring-302')}
        self.inherit_runs(keys)
        before = self.snapshot()
        for rows in self.projections():
            self.assertEqual(len(rows), 7)
            self.assertTrue(all('noise_adjustment' not in row for row in rows.values()))
        self.assertEqual(self.snapshot(), before)

    def inherit_runs(self, keys):
        source = SimcTask.objects.get(pk=self.task.pk)
        source.pk = None
        source.mode_params = deepcopy(source.mode_params)
        source.mode_params['initial_candidates'] = [candidate for candidate in source.mode_params['initial_candidates']
                                                     if candidate['candidate_key'] in keys]
        source.save()
        SimulationRun.objects.filter(task=self.task, candidate_key__in=keys).update(task=source)
        self.task.source_task = source
        self.task.mode_params = deepcopy(self.task.mode_params)
        self.task.mode_params['initial_candidates'] = [candidate for candidate in self.task.mode_params['initial_candidates']
                                                      if candidate['candidate_key'] not in keys]
        self.task.save(update_fields=['source_task', 'mode_params'])

    def test_normal_control_from_different_actual_tasks_cannot_tie(self):
        self.inherit_runs({control_key('ring-302')})
        before = self.snapshot()
        for rows in self.projections():
            for suffix in ('', '-302', '-315'):
                self.assertNotIn('noise_adjustment', rows[f'ring{suffix}'])
                self.assertIn('noise_adjustment', rows[f'trinket{suffix}'])
        self.assertEqual(self.snapshot(), before)

    def test_single_levels_skip_evidence_and_public_uses_frozen_not_live_candidates(self):
        self.panel.candidates.exclude(key__in=['trinket', 'ring']).delete()
        with CaptureQueriesContext(connection) as queries:
            rows = serialize_incremental_panel_results(
                self.panel, include_details=False)['coordinates'][0]['candidates']
        self.assertEqual({row['key'] for row in rows}, {'baseline', 'trinket', 'ring'})
        self.assertTrue(all('noise_adjustment' not in row for row in rows))
        self.assertTrue(all('evidence_summary' not in query['sql'] for query in queries))
        public = serialize_public_execution(self.execution)
        self.assertEqual(public['status'], 'ready')
        rows = {row['key']: row for row in public['execution']['cases'][0]['candidates']}
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows['trinket-302']['item_id'], 123)
        self.assertEqual(rows['trinket-302']['item_level'], 302)
        self.assertAlmostEqual(rows['trinket-302']['display_dps'], 1200.3)

    def test_reader_batches_only_numeric_multilevel_gear_without_n_plus_one(self):
        with CaptureQueriesContext(connection) as queries:
            self.projections()
        evidence = [query['sql'] for query in queries if 'evidence_summary' in query['sql']]
        self.assertEqual(len(evidence), 3)  # one batch per serializer, not per candidate
        self.assertTrue(all('baseline' not in sql for sql in evidence))
        self.assertTrue(all(not query['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                            for query in queries))
