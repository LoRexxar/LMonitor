"""Real ORM contracts for the reconcile-only compact read path."""
import json
import re

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import (
    SimcBackendBinary, SimcBenchmarkCase, SimcBenchmarkExecution,
    SimcBenchmarkPanel, SimcTask, SimulationRun,
)
from botend.services import simc_benchmark_execution as service


class BenchmarkReconcileMemoryTests(TestCase):
    def setUp(self):
        self.backend = SimcBackendBinary.objects.create(identifier='reconcile-memory', name='Test')
        self.panel = SimcBenchmarkPanel.objects.create(
            name='Memory', slug='reconcile-memory', created_by_id=1,
        )
        self.large = {'unused': 'x' * 100000}
        self.task = self.make_task(['baseline', 'item'])
        snapshot = {
            'version': 2, 'case_count': 1, 'run_count': 2,
            'cases': [{'spec_key': 'warrior_fury', 'scenario_key': 'raid',
                       'profile_key': 'profile', 'candidate_keys': ['baseline', 'item']}],
        }
        self.execution = SimcBenchmarkExecution.objects.create(
            panel=self.panel, config_snapshot=snapshot,
            config_hash=service._canonical_hash(snapshot),
        )
        self.case = SimcBenchmarkCase.objects.create(
            execution=self.execution, task=self.task, spec_key='warrior_fury',
            scenario_key='raid', profile_key='profile', spec_label='Fury',
            scenario_label='Raid', profile_label='Profile',
        )
        self.baseline = self.make_run(self.task, 'baseline', 1, 1000)
        self.item = self.make_run(self.task, 'item', 2, 1200)

    def make_task(self, keys, source=None):
        return SimcTask.objects.create(
            user_id=1, simc_profile_id=1, backend=self.backend, name='Frozen',
            current_status=1, source_task=source, mode='comparison',
            mode_params={'initial_candidates': [{'candidate_key': key} for key in keys],
                         'rounds': [{'status': 'running'}], 'request_manifest': self.large},
            ext=json.dumps({'progress': 37.9}), error_detail='',
            analysis_result=self.large, result_summary=json.dumps(self.large),
        )

    def make_run(self, task, key, sequence, dps):
        return SimulationRun.objects.create(
            task=task, candidate_key=key, candidate_label=key.title(),
            sequence=sequence, status='completed',
            result_summary={'dps': dps, 'valid': True, **self.large},
            resource_manifest={'hero_talent_names': [' Slayer ', 'Slayer'], **self.large},
            candidate_params=self.large, display_metadata=self.large,
        )

    def assert_compact_reads(self, queries):
        projections = [q['sql'].split(' FROM ', 1)[0] for q in queries
                       if q['sql'].startswith('SELECT')]
        self.assertTrue(projections)
        for table, fields in (
            ('simc_task', ('analysis_result', 'result_summary')),
            ('simc_simulation_run', ('result_summary', 'resource_manifest',
                                     'candidate_params', 'display_metadata')),
        ):
            for field in fields:
                self.assertFalse(any(re.search(
                    rf'(?:SELECT |, )["`]{table}["`]\.["`]{field}["`](?:,|$| AS)', sql,
                ) for sql in projections), f'full {table}.{field} selected')

    def test_full_and_incremental_reconcile_do_not_hydrate_unused_payloads(self):
        with CaptureQueriesContext(connection) as queries:
            live = service._summarize_live_execution(self.execution)
            single = service._summarize_live_execution(self.execution, case_id=self.case.pk)
            service.reconcile_execution(self.execution)
        self.assertEqual(live, single)
        self.assertEqual(live['status'], 'running')
        self.assertEqual(live['cases'][0]['task_progress'], 37)
        self.assertEqual([r['_raw_dps'] for r in live['cases'][0]['runs']], [1000, 1200])
        self.assertEqual(live['cases'][0]['runs'][0]['_hero_talent_names'], ['Slayer'])
        self.assertEqual(list(self.case.results.order_by('candidate_key').values_list(
            'candidate_key', 'dps', 'hero_talent_names',
        )), [('baseline', 1000.0, ['Slayer']), ('item', 1200.0, ['Slayer'])])
        self.assert_compact_reads(queries)

    def test_json_types_and_non_object_payloads_keep_live_semantics(self):
        cases = [
            ({'valid': False, 'reason': 'invalid', 'dps': 99}, 'failed', None, 'invalid'),
            ({'valid': False, 'error': 'fallback', 'dps': 99}, 'failed', None, 'fallback'),
            ({'valid': False}, 'failed', None, 'SimC 结果语义校验未通过'),
            ({'valid': 'false', 'dps': '1000'}, 'success', '1000', None),
            ({'valid': 0, 'dps': False}, 'success', False, None),
            ({'valid': None, 'dps': True}, 'success', True, None),
            ({'dps': 1234.567891234567}, 'success', 1234.567891234567, None),
            ({'dps': 9223372036854775809}, 'success', 9223372036854775809, None),
            ({'dps': {'bad': 1}}, 'success', {'bad': 1}, None),
            ({'dps': [1]}, 'success', [1], None),
            ({}, 'success', None, None),
            (None, 'success', None, None),
            ([{'valid': False, 'dps': 99}], 'success', None, None),
            ('{"valid":false,"dps":99}', 'success', None, None),
            (False, 'success', None, None),
        ]
        for summary, status, dps, error in cases:
            with self.subTest(summary=summary):
                SimulationRun.objects.filter(pk=self.item.pk).update(
                    result_summary=summary, resource_manifest=['not an object'],
                )
                row = service._summarize_live_execution(self.execution)['cases'][0]
                actual = row['runs'][1]
                self.assertEqual((actual['status'], actual['_raw_dps'], row['error']),
                                 (status, dps, error))
                self.assertIs(type(actual['_raw_dps']), type(dps))
                self.assertEqual(actual['_hero_talent_names'], [])

    def test_retry_sources_are_compact_and_unmaterialized_replacements_hide_ancestors(self):
        retry = self.make_task(['item'], self.task)
        self.case.task = retry
        self.case.save(update_fields=['task'])
        with CaptureQueriesContext(connection) as queries:
            live = service._summarize_live_execution(self.execution)
        self.assertEqual([r['key'] for r in live['cases'][0]['runs']], ['baseline'])
        self.assert_compact_reads(queries)
        replacement = self.make_run(retry, 'item', 1, 1400)
        # A deeper retry owns baseline, while inheriting the intermediate item.
        latest = self.make_task(['baseline'], retry)
        self.case.task = latest
        self.case.save(update_fields=['task'])
        with CaptureQueriesContext(connection) as queries:
            live = service._summarize_live_execution(self.execution)
        self.assertEqual([(r['key'], r['_raw_dps']) for r in live['cases'][0]['runs']],
                         [('item', 1400)])
        self.assert_compact_reads(queries)
        # The shared, non-reconcile helper must still return full Run objects.
        inherited = service._runs_through_source_chain(latest)['item']
        self.assertEqual(inherited.pk, replacement.pk)
        self.assertEqual(inherited.result_summary['unused'], self.large['unused'])
        self.assertEqual(inherited.candidate_params, self.large)
        # Frozen manifest fallback and source cycles keep the same overlay order.
        SimcTask.objects.filter(pk=latest.pk).update(mode_params={
            'initial_candidates': [],
            'request_manifest': {'candidates': [{'candidate_key': 'baseline'}]},
        })
        SimcTask.objects.filter(pk=self.task.pk).update(source_task=latest)
        cycle = service._summarize_live_execution(self.execution)
        self.assertEqual(cycle, live)

    def test_valid_terminal_results_publish_once_with_unchanged_seal(self):
        SimcTask.objects.filter(pk=self.task.pk).update(current_status=2)
        with CaptureQueriesContext(connection) as queries:
            final = service.reconcile_execution(self.execution)
        self.assertEqual(final.status, 'success')
        self.assertEqual(len(final.result_hash), 64)
        self.assertIsNotNone(final.results_finalized_at)
        self.panel.refresh_from_db()
        self.assertEqual(self.panel.published_execution_id, final.pk)
        persisted = list(self.case.results.order_by('candidate_key').values())
        again = service.reconcile_execution(final)
        self.assertEqual(again.result_hash, final.result_hash)
        self.assertEqual(list(self.case.results.order_by('candidate_key').values()), persisted)
        self.assert_compact_reads(queries)

    def test_terminal_validation_and_unknown_states_are_not_bypassed(self):
        SimcTask.objects.filter(pk=self.task.pk).update(current_status=2)
        SimulationRun.objects.filter(pk=self.item.pk).update(result_summary={'dps': True})
        final = service.reconcile_execution(self.execution)
        self.assertEqual(final.status, 'failed')
        self.assertIsNotNone(final.completed_at)
        self.assertEqual(final.result_hash, '')
        self.assertFalse(self.case.results.exists())
        self.panel.refresh_from_db()
        self.assertIsNone(self.panel.published_execution_id)
        SimcTask.objects.filter(pk=self.task.pk).update(current_status=99)
        SimulationRun.objects.filter(pk=self.item.pk).update(status='unknown')
        row = service._summarize_live_execution(self.execution)['cases'][0]
        self.assertEqual(row['task_status'], 'failed')
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['runs'][1]['status'], 'failed')
