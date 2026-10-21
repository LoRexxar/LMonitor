import json
import os
import time
from pathlib import Path
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from botend.models import SimcBenchmarkCase
from botend.models import SimcBenchmarkExecution
from botend.models import SimcBenchmarkResult
from botend.models import SimcBenchmarkScenario
from botend.models import SimcTask
from botend.services import simc_benchmark_execution as execution
from botend.services import simc_benchmark_result_snapshot as snapshots
from botend.tests import test_simc_benchmark_result_snapshot as fixtures
from botend.services.simc_benchmark_execution import serialize_incremental_panel_results


"""Full coordinate snapshots use real ORM projections and private JSON files."""




class BenchmarkDetailSnapshotTests(TestCase):
    def setUp(self):
        # Reuse the DB-only fixture, never its mocked simulation helpers.
        from botend.tests.test_simc_benchmark_result_snapshot import BenchmarkResultSnapshotTests
        BenchmarkResultSnapshotTests.setUp(self)
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='aoe', name='AoE',
            simulation_params={'iterations': 1000, 'desired_targets': 5},
        )
        self.directory = Path(self.tmp.name) / str(self.panel.pk)

    def build(self):
        snapshots.request_snapshot_refresh(self.panel.pk)
        self.assertTrue(snapshots.rebuild_panel_result_snapshot(self.panel.pk))
        return snapshots._index(self.panel.pk)

    def publish_results(self):
        """Persist finalized input/result fixtures; do not mock the read pipeline."""
        plan = execution.build_execution_plan(self.panel, lock=False)
        published = SimcBenchmarkExecution.objects.create(
            panel=self.panel, status='success', config_hash='detail-snapshot-fixture',
            completed_at=timezone.now(), results_finalized_at=timezone.now(),
        )
        for coordinate in plan['cases']:
            task = SimcTask.objects.create(
                user_id=701, name='Frozen snapshot fixture',
                simc_profile_id=coordinate['profile_id'],
                profile_id=coordinate['profile_id'], apl_id=coordinate['apl_id'],
                template_id=coordinate['template_id'], backend_id=coordinate['backend_id'],
                mode='comparison', current_status=2,
                simulation_params=coordinate['simulation_params'],
                mode_params={'request_manifest': {'candidates': coordinate['candidates']}},
            )
            case = SimcBenchmarkCase.objects.create(
                execution=published, task=task, status='success',
                coordinate_hash=execution._coordinate_hash(coordinate),
                **{key: coordinate[key] for key in (
                    'spec_key', 'profile_key', 'scenario_key',
                    'spec_label', 'profile_label', 'scenario_label',
                )},
            )
            for candidate in coordinate['candidates']:
                SimcBenchmarkResult.objects.create(
                    case=case, candidate_key=candidate['candidate_key'], dps=1234,
                    hero_talent_names=['屠戮者'],
                )

    def legacy_index(self):
        index = self.build()
        index.pop('details_files', None)
        snapshots._write(self.directory / 'index.json', index)
        return index

    def test_full_projection_matches_real_results_and_reads_only_one_file_without_sql(self):
        self.publish_results()
        expected = {
            details: execution.serialize_incremental_panel_results(
                self.panel, coordinate_filter={'scenario_key': 'aoe'},
                include_details=details, include_coordinate_options=True,
            ) for details in (False, True)
        }
        self.assertTrue(expected[True]['coordinates'][0]['candidates'])
        self.assertIn('source_result_id', expected[True]['coordinates'][0]['candidates'][0])
        self.assertIn('profile_detail', expected[True]['coordinates'][0])
        self.assertIn('simulation_detail', expected[True]['coordinates'][0])
        self.assertIn('audit', expected[True]['coordinates'][0])
        with patch.object(execution, 'build_execution_plan', wraps=execution.build_execution_plan) as planner:
            index = self.build()
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(set(index['details_files']), set(index['files']))
        self.assertTrue(set(index['details_files'].values()).isdisjoint(index['files'].values()))
        generations = []
        for details in (False, True):
            with patch.object(execution, 'build_execution_plan', side_effect=AssertionError('no planning')), patch.object(
                execution, 'serialize_incremental_panel_results', side_effect=AssertionError('no live projection'),
            ), patch.object(snapshots, '_load', wraps=snapshots._load) as reader:
                with self.assertNumQueries(0):
                    result = snapshots.read_panel_result_snapshot(
                        self.panel, coordinate_filter={'scenario_key': 'aoe'}, include_details=details,
                    )
            metadata = result.pop('snapshot')
            generations.append(metadata['generation'])
            self.assertEqual(metadata['state'], 'ready')
            self.assertEqual(result, json.loads(json.dumps(expected[details])))
            files_read = [call.args[0] for call in reader.call_args_list
                          if call.args[0].parent.name == 'coordinates']
            key = snapshots._coordinate_key(result['coordinates'][0])
            mapping = 'details_files' if details else 'files'
            self.assertEqual(files_read, [self.directory / 'coordinates' / index[mapping][key]])
        self.assertEqual(generations, [index['generation'], index['generation']])

    def test_details_require_coordinate_filter_before_any_io(self):
        with patch.object(snapshots, '_load', side_effect=AssertionError('must reject before IO')):
            with self.assertNumQueries(0), self.assertRaisesRegex(ValueError, 'coordinate_filter'):
                snapshots.read_panel_result_snapshot(self.panel, include_details=True)

    def test_cold_detail_only_queues_without_planning(self):
        with patch.object(execution, 'build_execution_plan', side_effect=AssertionError('no planning')), patch.object(
            execution, 'serialize_incremental_panel_results', side_effect=AssertionError('no live projection'),
        ):
            with self.assertNumQueries(0):
                result = snapshots.read_panel_result_snapshot(
                    self.panel, coordinate_filter={}, include_details=True,
                )
        self.assertEqual(result['coordinates'], [])
        self.assertEqual(result['snapshot']['state'], 'building')
        self.assertTrue(snapshots.snapshot_refresh_pending(self.panel.pk))

    def test_legacy_summary_remains_readable_and_detail_queues_full_upgrade(self):
        previous = self.legacy_index()
        with self.assertNumQueries(0):
            summary = snapshots.read_panel_result_snapshot(self.panel)
            details = snapshots.read_panel_result_snapshot(self.panel, coordinate_filter={}, include_details=True)
            dirty_summary = snapshots.read_panel_result_snapshot(self.panel)
        self.assertEqual(len(summary['coordinates']), 2)
        self.assertEqual(summary['snapshot']['state'], 'ready')
        self.assertEqual(details['snapshot']['state'], 'building')
        self.assertEqual(details['coordinates'], [])
        self.assertEqual(dirty_summary['coordinates'], summary['coordinates'])
        self.assertEqual(dirty_summary['snapshot']['state'], 'updating')
        self.assertTrue(snapshots.rebuild_panel_result_snapshot(self.panel.pk))
        current = snapshots._index(self.panel.pk)
        self.assertEqual(set(current['details_files']), set(previous['files']))

    def test_legacy_index_upgrades_without_dirty_events(self):
        previous = self.legacy_index()
        self.assertFalse(snapshots.snapshot_refresh_pending(self.panel.pk))
        self.assertTrue(snapshots.rebuild_panel_result_snapshot(self.panel.pk))
        current = snapshots._index(self.panel.pk)
        self.assertEqual(set(current['details_files']), set(previous['files']))
        self.assertNotEqual(current['generation'], previous['generation'])

    def test_maintenance_repairs_legacy_and_missing_detail_files(self):
        self.legacy_index()
        self.assertEqual(snapshots.refresh_pending_result_snapshots(), [self.panel.pk])
        index = snapshots._index(self.panel.pk)
        (self.directory / 'coordinates' / next(iter(index['details_files'].values()))).unlink()
        self.assertFalse(snapshots.snapshot_refresh_pending(self.panel.pk))
        self.assertEqual(snapshots.refresh_pending_result_snapshots(), [self.panel.pk])
        repaired = snapshots._index(self.panel.pk)
        self.assertTrue(all((self.directory / 'coordinates' / name).is_file()
                            for name in repaired['details_files'].values()))

    def test_missing_detail_does_not_read_summary_or_replan(self):
        index = self.build()
        selected = index['coordinate_options'][0]
        key = snapshots._coordinate_key(selected)
        (self.directory / 'coordinates' / index['details_files'][key]).unlink()
        with patch.object(execution, 'build_execution_plan', side_effect=AssertionError('no planning')), patch.object(
            snapshots, '_load', wraps=snapshots._load,
        ) as reader:
            with self.assertNumQueries(0):
                detail = snapshots.read_panel_result_snapshot(
                    self.panel, coordinate_filter=selected, include_details=True,
                )
        self.assertEqual(detail['snapshot']['state'], 'building')
        self.assertFalse(any(call.args[0].name in index['files'].values() for call in reader.call_args_list))
        with self.assertNumQueries(0):
            summary = snapshots.read_panel_result_snapshot(self.panel, coordinate_filter=selected)
        self.assertEqual(len(summary['coordinates']), 1)
        self.assertEqual(summary['snapshot']['state'], 'updating')

    def test_failed_detail_write_preserves_both_old_mappings_and_pending_event(self):
        previous = self.build()
        snapshots.request_snapshot_refresh(self.panel.pk)
        original_write = snapshots._write

        def fail_detail(path, payload):
            if path.parent.name == 'coordinates' and 'profile_detail' in payload['coordinates'][0]:
                raise OSError('detail disk failure')
            original_write(path, payload)

        with patch.object(snapshots, '_write', side_effect=fail_detail):
            with self.assertRaisesRegex(OSError, 'detail disk failure'):
                snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        self.assertEqual(snapshots._index(self.panel.pk), previous)
        self.assertTrue(snapshots.snapshot_refresh_pending(self.panel.pk))
        for details in (False, True):
            result = snapshots.read_panel_result_snapshot(self.panel, coordinate_filter={}, include_details=details)
            self.assertEqual(result['snapshot']['generation'], previous['generation'])
            self.assertEqual(result['snapshot']['state'], 'updating')
        self.assertTrue(snapshots.rebuild_panel_result_snapshot(self.panel.pk))

    def test_incremental_rebuild_and_gc_keep_both_current_and_previous_files(self):
        first = self.build()
        selected = first['coordinate_options'][0]
        key = snapshots._coordinate_key(selected)
        orphan = self.directory / 'coordinates' / ('f' * 32 + '.json')
        snapshots._write(orphan, {'orphan': True})
        stale_time = time.time() - 600
        snapshots.request_snapshot_refresh(self.panel.pk, [selected])
        self.assertTrue(snapshots.rebuild_panel_result_snapshot(self.panel.pk))
        second = snapshots._index(self.panel.pk)
        for mapping in ('files', 'details_files'):
            self.assertEqual(set(second[mapping]), set(first[mapping]))
            for item in first[mapping]:
                if item == key:
                    self.assertNotEqual(second[mapping][item], first[mapping][item])
                else:
                    self.assertEqual(second[mapping][item], first[mapping][item])
        for path in (self.directory / 'coordinates').glob('*.json'):
            os.utime(path, (stale_time, stale_time))
        snapshots.request_snapshot_refresh(self.panel.pk, [selected])
        self.assertTrue(snapshots.rebuild_panel_result_snapshot(self.panel.pk))
        third = snapshots._index(self.panel.pk)
        self.assertFalse(orphan.exists())
        for mapping in ('files', 'details_files'):
            self.assertFalse((self.directory / 'coordinates' / first[mapping][key]).exists())
            for index in (second, third):
                self.assertTrue(all((self.directory / 'coordinates' / name).exists()
                                    for name in index[mapping].values()))


"""Public detail reads must consume the background file, never build plans."""


class BenchmarkDetailReadAPITests(TestCase):
    setUp = fixtures.BenchmarkResultSnapshotTests.setUp

    def test_cold_detail_only_reads_panel_and_enqueues_without_planning(self):
        with self.assertNumQueries(1):
            response = self.client.get(f'/portal/api/simc-benchmarks/panels/{self.panel.pk}/',
                                       {'detail': '1', 'spec': 'warrior_fury'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results']['coordinates'], [])
        self.assertEqual(response.json()['results']['snapshot']['state'], 'building')
        self.assertTrue(snapshots.snapshot_refresh_pending(self.panel.pk))

    def test_published_detail_matches_real_projection_with_only_panel_query(self):
        expected = serialize_incremental_panel_results(self.panel, coordinate_filter={}, include_details=True)
        snapshots.request_snapshot_refresh(self.panel.pk)
        self.assertTrue(snapshots.rebuild_panel_result_snapshot(self.panel.pk))
        with self.assertNumQueries(1):
            response = self.client.get(f'/portal/api/simc-benchmarks/panels/{self.panel.pk}/', {'detail': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results']['coordinates'], json.loads(json.dumps(expected['coordinates'])))
        self.assertEqual(response.json()['results']['snapshot']['state'], 'ready')

    def test_unselected_public_read_also_uses_snapshot(self):
        snapshots.request_snapshot_refresh(self.panel.pk)
        self.assertTrue(snapshots.rebuild_panel_result_snapshot(self.panel.pk))
        expected = snapshots.read_panel_result_snapshot(self.panel)
        with self.assertNumQueries(1):
            response = self.client.get(f'/portal/api/simc-benchmarks/panels/{self.panel.pk}/')
        self.assertEqual(response.json()['results']['coordinates'], expected['coordinates'])
        self.assertEqual(response.json()['results']['snapshot']['state'], 'ready')
