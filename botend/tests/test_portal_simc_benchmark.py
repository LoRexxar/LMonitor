import json
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.test import TestCase, override_settings

from botend.models import SimcBenchmarkPanel


@override_settings(ALLOWED_HOSTS=['testserver'])
class PortalSimcBenchmarkAPITests(TestCase):
    def setUp(self):
        self.public = SimcBenchmarkPanel.objects.create(
            name='Public panel', slug='public-panel', description='Public description',
            created_by_id=10, is_public=True,
        )
        self.private = SimcBenchmarkPanel.objects.create(
            name='Secret panel', slug='secret-panel', description='Secret config',
            created_by_id=99, is_public=False,
        )
        self.disabled = SimcBenchmarkPanel.objects.create(
            name='Disabled panel', slug='disabled-panel', created_by_id=10,
            is_public=True, is_active=False,
        )

    @patch('botend.portal.simc_benchmark_api.serialize_incremental_panel_results')
    def test_list_contains_only_public_fields_and_public_panels(self, serializer):
        serializer.return_value = {'coordinates': []}
        response = self.client.get('/portal/api/simc-benchmarks/panels/')
        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload, {
            'status': 'ready',
            'panels': [{
                'slug': 'public-panel', 'name': 'Public panel',
                'description': 'Public description', 'status': 'not_ready',
            }],
        })
        serializer.assert_called_once_with(self.public)
        serialized = json.dumps(payload)
        for forbidden in ('created_by_id', 'schedule_enabled', 'interval_seconds',
                          'published_execution_id', 'config', 'task', 'run'):
            self.assertNotIn(forbidden, serialized.lower())

    @patch('botend.portal.simc_benchmark_api.serialize_incremental_panel_results')
    def test_detail_returns_immediate_result_projection(self, serializer):
        projection = {
            'panel_id': self.public.id,
            'coordinates': [{
                'spec_key': 'fury', 'scenario_key': 'st', 'profile_key': 'raid',
                'labels': {'spec': 'Fury', 'scenario': 'Single target', 'profile': 'Raid'},
                'candidates': [{'key': 'baseline', 'label': 'Baseline', 'type': 'baseline',
                                'icon_url': '', 'source_label': '', 'dps': 100, 'task_id': 1}],
            }],
        }
        serializer.return_value = projection
        response = self.client.get('/portal/api/simc-benchmarks/panels/public-panel/')
        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload, {
            'status': 'ready',
            'panel': {'slug': 'public-panel', 'name': 'Public panel', 'description': 'Public description'},
            'results': {'coordinates': projection['coordinates']},
        })
        serializer.assert_called_once_with(self.public)
        self.assertNotIn('execution', payload)

    @patch('botend.portal.simc_benchmark_api.serialize_incremental_panel_results')
    def test_private_panel_is_hidden_and_not_openable_by_slug(self, serializer):
        serializer.return_value = {'coordinates': [{'spec_key': 'private'}]}
        list_response = self.client.get('/portal/api/simc-benchmarks/panels/')
        private_response = self.client.get('/portal/api/simc-benchmarks/panels/secret-panel/')
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(private_response.status_code, 200)
        self.assertNotIn('secret-panel', json.dumps(json.loads(list_response.content)))
        self.assertEqual(json.loads(private_response.content), {'status': 'not_ready', 'results': {'coordinates': []}})
        serializer.assert_called_once_with(self.public)

    @patch('botend.portal.simc_benchmark_api.serialize_incremental_panel_results')
    def test_disabled_and_missing_slugs_are_not_ready(self, serializer):
        disabled_response = self.client.get('/portal/api/simc-benchmarks/panels/disabled-panel/')
        missing_response = self.client.get('/portal/api/simc-benchmarks/panels/missing-panel/')
        expected = {'status': 'not_ready', 'results': {'coordinates': []}}
        self.assertEqual(json.loads(disabled_response.content), expected)
        self.assertEqual(json.loads(missing_response.content), expected)
        serializer.assert_not_called()

    def test_public_endpoints_are_read_only(self):
        self.assertEqual(self.client.post('/portal/api/simc-benchmarks/panels/').status_code, 405)
        self.assertEqual(self.client.post('/portal/api/simc-benchmarks/panels/public-panel/').status_code, 405)


class PortalSimcBenchmarkUIContractTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[2]
    TEMPLATE = (ROOT / 'templates/portal/index.html').read_text(encoding='utf-8')
    RESULTS_TEMPLATE = (ROOT / 'templates/portal/simc_benchmark_results.html').read_text(encoding='utf-8')
    JS = (ROOT / 'static/portal/js/simc-benchmarks.js').read_text(encoding='utf-8')
    CSS = (ROOT / 'static/portal/css/simc-benchmarks.css').read_text(encoding='utf-8')

    def test_results_page_owns_the_benchmark_region_and_assets(self):
        soup = BeautifulSoup(self.TEMPLATE, 'html.parser')
        results_soup = BeautifulSoup(self.RESULTS_TEMPLATE, 'html.parser')
        self.assertIsNone(soup.select_one('#section-simc-benchmarks'))
        self.assertIsNone(soup.select_one('#simc-benchmark-root'))
        self.assertIsNotNone(results_soup.select_one('#simc-benchmark-root[aria-live="polite"]'))
        self.assertIn('portal/css/simc-benchmarks.css', self.RESULTS_TEMPLATE)
        self.assertIn('portal/js/simc-benchmarks.js', self.RESULTS_TEMPLATE)
        self.assertIn('/portal/simc-benchmarks/', self.TEMPLATE)

    def test_public_renderer_uses_dimensioned_immediate_projection(self):
        for contract in ('payload?.results?.coordinates', 'spec_key', 'scenario_key', 'profile_key',
                         'renderCoordinate', 'sortCandidates', 'relative', 'baseline'):
            self.assertIn(contract, self.JS)
        self.assertNotIn('innerHTML', self.JS)
        self.assertNotIn('results_finalized_at', self.JS)

    def test_dense_chart_and_mobile_rows_have_explicit_layout_contracts(self):
        for contract in ('simc-benchmark-candidate-grid', 'simc-benchmark-candidate-source',
                         'max-height: min(70vh, 64rem)', 'overflow-y: auto',
                         '@media (max-width: 640px)', 'grid-template-areas'):
            self.assertIn(contract, self.CSS)

    def test_mobile_layout_and_no_scroll_snap(self):
        self.assertIn('@media (max-width: 640px)', self.CSS)
        self.assertIn('grid-template-columns: minmax(0, 1fr)', self.CSS)
        self.assertNotIn('scroll-snap', self.CSS.lower())
        section = BeautifulSoup(self.RESULTS_TEMPLATE, 'html.parser').select_one('#simc-benchmark-root')
        self.assertIsNotNone(section)
        self.assertNotIn('snap-section', section.get('class', []))


if __name__ == '__main__':
    unittest.main()
