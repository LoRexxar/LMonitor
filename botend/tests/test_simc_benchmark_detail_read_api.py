"""Public detail reads must consume the background file, never build plans."""
import json
from django.test import TestCase
from botend.tests import test_simc_benchmark_result_snapshot as fixtures
from botend.services import simc_benchmark_result_snapshot as snapshots
from botend.services.simc_benchmark_execution import serialize_incremental_panel_results


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
