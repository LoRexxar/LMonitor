"""File-backed Benchmark read model: real serializer, IO and database contracts."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from botend.models import SimcBenchmarkPanel


class BenchmarkResultSnapshotTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(self.tmp.cleanup)
        self.settings_override = override_settings(
            SIMC_BENCHMARK_RESULT_SNAPSHOT_ROOT=self.tmp.name,
            ALLOWED_HOSTS=['testserver'],
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        # Reuse only the real DB fixture, not its simulation/validation mocks.
        from botend.tests.test_simc_benchmark_execution import SimcBenchmarkExecutionTests
        fixture = SimcBenchmarkExecutionTests()
        fixture.setUp()
        self.panel = fixture.panel

    def test_cold_read_queues_without_planning_or_scanning_history(self):
        from botend.services import simc_benchmark_result_snapshot as snapshots
        with patch('botend.services.simc_benchmark_execution.build_execution_plan',
                   side_effect=AssertionError('read must not build a plan')):
            with self.assertNumQueries(0):
                result = snapshots.read_panel_result_snapshot(self.panel, coordinate_filter={})
        self.assertEqual(result['coordinates'], [])
        self.assertEqual(result['snapshot']['state'], 'building')
        self.assertTrue((Path(self.tmp.name) / str(self.panel.pk) / 'pending.json').exists())

    def test_generated_file_matches_real_projection_and_hot_read_is_sql_free(self):
        from botend.services import simc_benchmark_result_snapshot as snapshots
        from botend.services.simc_benchmark_execution import serialize_incremental_panel_results
        expected = serialize_incremental_panel_results(
            self.panel, coordinate_filter={}, include_coordinate_options=True, include_details=False,
        )
        snapshots.request_snapshot_refresh(self.panel.pk)
        self.assertTrue(snapshots.rebuild_panel_result_snapshot(self.panel.pk))
        with patch('botend.services.simc_benchmark_execution.build_execution_plan',
                   side_effect=AssertionError('hot read must not plan')):
            with self.assertNumQueries(0):
                actual = snapshots.read_panel_result_snapshot(self.panel, coordinate_filter={})
        self.assertEqual(actual.pop('snapshot')['state'], 'ready')
        self.assertEqual(actual, json.loads(json.dumps(expected)))
        self.assertFalse(snapshots.snapshot_refresh_pending(self.panel.pk))
        response = self.client.get(
            f'/portal/api/simc-benchmarks/panels/{self.panel.pk}/', {'selected': '1'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results']['coordinates'], expected['coordinates'])
        self.assertEqual(response.json()['results']['snapshot']['state'], 'ready')

    def test_dirty_snapshot_stays_readable_and_failed_rebuild_keeps_old_file(self):
        from botend.services import simc_benchmark_result_snapshot as snapshots
        snapshots.request_snapshot_refresh(self.panel.pk)
        snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        previous = snapshots.read_panel_result_snapshot(self.panel, coordinate_filter={})
        snapshots.request_snapshot_refresh(self.panel.pk)
        with patch('botend.services.simc_benchmark_execution.serialize_incremental_panel_results',
                   side_effect=RuntimeError('interrupted build')):
            with self.assertRaises(RuntimeError):
                snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        current = snapshots.read_panel_result_snapshot(self.panel, coordinate_filter={})
        self.assertEqual(current['coordinates'], previous['coordinates'])
        self.assertEqual(current['snapshot']['generation'], previous['snapshot']['generation'])
        self.assertEqual(current['snapshot']['state'], 'updating')
        self.assertTrue(snapshots.snapshot_refresh_pending(self.panel.pk))

    def test_new_event_during_build_is_not_lost(self):
        from botend.services import simc_benchmark_result_snapshot as snapshots
        from botend.services.simc_benchmark_execution import serialize_incremental_panel_results
        snapshots.request_snapshot_refresh(self.panel.pk)
        def racing_projection(*args, **kwargs):
            snapshots.request_snapshot_refresh(self.panel.pk)
            return serialize_incremental_panel_results(*args, **kwargs)
        with patch('botend.services.simc_benchmark_execution.serialize_incremental_panel_results',
                   side_effect=racing_projection):
            snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        self.assertTrue(snapshots.snapshot_refresh_pending(self.panel.pk))
        snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        self.assertFalse(snapshots.snapshot_refresh_pending(self.panel.pk))

    def test_invalidation_happens_after_commit_not_rollback(self):
        from botend.services import simc_benchmark_result_snapshot as snapshots
        with self.captureOnCommitCallbacks(execute=True):
            self.panel.name = 'Updated panel'
            self.panel.save(update_fields=['name', 'updated_at'])
            self.assertFalse(snapshots.snapshot_refresh_pending(self.panel.pk))
        self.assertTrue(snapshots.snapshot_refresh_pending(self.panel.pk))

    def test_coordinate_refresh_reuses_other_files_and_builds_plan_once(self):
        from botend.models import SimcBenchmarkScenario
        from botend.services import simc_benchmark_result_snapshot as snapshots
        from botend.services.simc_benchmark_execution import build_execution_plan
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='aoe', name='AoE', simulation_params={'iterations': 1000},
        )
        snapshots.request_snapshot_refresh(self.panel.pk)
        snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        previous = snapshots._index(self.panel.pk)
        coordinate = previous['coordinate_options'][0]
        snapshots.request_snapshot_refresh(self.panel.pk, [coordinate])
        with patch('botend.services.simc_benchmark_execution.build_execution_plan',
                   wraps=build_execution_plan) as planner:
            snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        self.assertEqual(planner.call_count, 1)
        current = snapshots._index(self.panel.pk)
        key = snapshots._coordinate_key(coordinate)
        for item in previous['files']:
            if item == key:
                self.assertNotEqual(previous['files'][item], current['files'][item])
            else:
                self.assertEqual(previous['files'][item], current['files'][item])
        chosen = snapshots.read_panel_result_snapshot(
            self.panel, coordinate_filter={'spec_key': '../../etc', 'scenario_key': 'aoe'},
        )
        self.assertEqual(chosen['coordinates'][0]['scenario_key'], 'aoe')

    def test_rolled_back_save_does_not_invalidate(self):
        from django.db import transaction
        from botend.services import simc_benchmark_result_snapshot as snapshots
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.panel.name = 'Not committed'
                    self.panel.save(update_fields=['name'])
                    raise RuntimeError('rollback')
            except RuntimeError:
                pass
        self.assertFalse(snapshots.snapshot_refresh_pending(self.panel.pk))

    def test_missing_coordinate_file_queues_repair_without_live_fallback(self):
        from botend.services import simc_benchmark_result_snapshot as snapshots
        snapshots.request_snapshot_refresh(self.panel.pk)
        snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        directory = Path(self.tmp.name) / str(self.panel.pk)
        index = snapshots._index(self.panel.pk)
        (directory / 'coordinates' / next(iter(index['files'].values()))).unlink()
        with self.assertNumQueries(0):
            result = snapshots.read_panel_result_snapshot(self.panel, coordinate_filter={})
        self.assertEqual(result['snapshot']['state'], 'building')
        snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        self.assertEqual(snapshots.read_panel_result_snapshot(
            self.panel, coordinate_filter={},
        )['snapshot']['state'], 'ready')

    def test_empty_projection_cannot_replace_previous_complete_index(self):
        from botend.services import simc_benchmark_result_snapshot as snapshots
        snapshots.request_snapshot_refresh(self.panel.pk)
        snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        previous = snapshots._index(self.panel.pk)
        snapshots.request_snapshot_refresh(self.panel.pk)
        with patch('botend.services.simc_benchmark_execution.serialize_incremental_panel_results',
                   return_value={'coordinates': []}):
            with self.assertRaises(ValueError):
                snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        self.assertEqual(previous, snapshots._index(self.panel.pk))

    def test_resource_and_terminal_execution_changes_enqueue_after_commit(self):
        from botend.models import SimcBenchmarkExecution
        from django.utils import timezone
        from botend.services import simc_benchmark_result_snapshot as snapshots
        profile = self.panel.specs.get().profiles.get().profile
        with self.captureOnCommitCallbacks(execute=True):
            profile.name = 'Renamed profile'
            profile.save(update_fields=['name'])
        self.assertTrue(snapshots.snapshot_refresh_pending(self.panel.pk))
        snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        with self.captureOnCommitCallbacks(execute=True):
            SimcBenchmarkExecution.objects.create(
                panel=self.panel, status='failed', completed_at=timezone.now(),
                config_hash='test-only',
            )
        self.assertTrue(snapshots.snapshot_refresh_pending(self.panel.pk))

    def test_config_change_during_build_keeps_previous_generation(self):
        from botend.services import simc_benchmark_result_snapshot as snapshots
        from botend.services.simc_benchmark_execution import serialize_incremental_panel_results
        snapshots.request_snapshot_refresh(self.panel.pk)
        snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        previous = snapshots._index(self.panel.pk)
        snapshots.request_snapshot_refresh(self.panel.pk)
        def change_config(*args, **kwargs):
            snapshots.request_snapshot_refresh(self.panel.pk)
            return serialize_incremental_panel_results(*args, **kwargs)
        with patch('botend.services.simc_benchmark_execution.serialize_incremental_panel_results',
                   side_effect=change_config):
            snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        self.assertEqual(snapshots._index(self.panel.pk), previous)
        self.assertTrue(snapshots.snapshot_refresh_pending(self.panel.pk))

    def test_terminal_commit_without_callback_cannot_publish_mixed_generation(self):
        from botend.models import SimcBenchmarkExecution, SimcBenchmarkScenario
        from django.utils import timezone
        from botend.services import simc_benchmark_result_snapshot as snapshots
        from botend.services.simc_benchmark_execution import serialize_incremental_panel_results
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='aoe', name='AoE', simulation_params={'iterations': 1000},
        )
        execution = SimcBenchmarkExecution.objects.create(panel=self.panel, config_hash='test-only')
        snapshots.request_snapshot_refresh(self.panel.pk)
        snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        previous = snapshots._index(self.panel.pk)
        snapshots.request_snapshot_refresh(self.panel.pk)
        count = 0
        def terminal_between_coordinates(*args, **kwargs):
            nonlocal count
            result = serialize_incremental_panel_results(*args, **kwargs)
            count += 1
            if count == 1:
                # Deliberately bypass callbacks: models the DB-commit -> enqueue gap.
                SimcBenchmarkExecution.objects.filter(pk=execution.pk).update(
                    completed_at=timezone.now(), status='success',
                )
            return result
        with patch('botend.services.simc_benchmark_execution.serialize_incremental_panel_results',
                   side_effect=terminal_between_coordinates):
            snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        self.assertEqual(snapshots._index(self.panel.pk), previous)
        snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        self.assertNotEqual(snapshots._index(self.panel.pk)['generation'], previous['generation'])

    def test_missed_terminal_notification_forces_full_rebuild(self):
        from botend.models import SimcBenchmarkExecution, SimcBenchmarkScenario
        from django.utils import timezone
        from botend.services import simc_benchmark_result_snapshot as snapshots
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='aoe', name='AoE', simulation_params={'iterations': 1000},
        )
        execution = SimcBenchmarkExecution.objects.create(panel=self.panel, config_hash='test-only')
        snapshots.request_snapshot_refresh(self.panel.pk)
        snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        previous = snapshots._index(self.panel.pk)
        SimcBenchmarkExecution.objects.filter(pk=execution.pk).update(
            completed_at=timezone.now(), status='success',
        )
        self.assertFalse(snapshots.snapshot_refresh_pending(self.panel.pk))
        # A stale single-coordinate event must not limit this terminal publication.
        snapshots.request_snapshot_refresh(self.panel.pk, [previous['coordinate_options'][0]])
        self.assertEqual(snapshots.refresh_pending_result_snapshots(), [self.panel.pk])
        current = snapshots._index(self.panel.pk)
        self.assertNotEqual(current['generation'], previous['generation'])
        self.assertEqual(len(current['files']), 2)
        for key in previous['files']:
            self.assertNotEqual(current['files'][key], previous['files'][key])

    def test_selected_api_cold_and_hot_never_calls_heavy_reader(self):
        from botend.services import simc_benchmark_result_snapshot as snapshots
        for hot in (False, True):
            if hot:
                snapshots.rebuild_panel_result_snapshot(self.panel.pk)
            with patch('botend.services.simc_benchmark_execution.build_execution_plan',
                       side_effect=AssertionError('planner forbidden on read')), patch(
                'botend.portal.simc_benchmark_api.serialize_incremental_panel_results',
                side_effect=AssertionError('live serializer forbidden on read'),
            ), patch.object(snapshots, '_publication_revision',
                            side_effect=AssertionError('revision SQL forbidden on read')):
                response = self.client.get(
                    f'/portal/api/simc-benchmarks/panels/{self.panel.pk}/', {'selected': '1'},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['results']['snapshot']['state'],
                             'ready' if hot else 'building')

    def test_disabled_panel_never_exposes_existing_snapshot(self):
        from botend.services import simc_benchmark_result_snapshot as snapshots
        snapshots.request_snapshot_refresh(self.panel.pk)
        snapshots.rebuild_panel_result_snapshot(self.panel.pk)
        SimcBenchmarkPanel.objects.filter(pk=self.panel.pk).update(is_active=False)
        response = self.client.get(
            f'/portal/api/simc-benchmarks/panels/{self.panel.pk}/', {'selected': '1'},
        )
        self.assertEqual(response.json()['status'], 'not_ready')
        self.assertEqual(response.json()['results']['coordinates'], [])
