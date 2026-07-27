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

    @patch('botend.portal.simc_benchmark_api.serialize_public_execution')
    def test_list_contains_only_public_fields_and_public_panels(self, serializer):
        serializer.return_value = {'status': 'not_ready', 'execution': None}

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
        serializer.assert_called_once()
        self.assertEqual(serializer.call_args.args[0].pk, self.public.pk)
        serialized = json.dumps(payload)
        for forbidden in ('created_by_id', 'schedule_enabled', 'interval_seconds',
                          'published_execution_id', 'config', 'task', 'run'):
            self.assertNotIn(forbidden, serialized.lower())

    @patch('botend.portal.simc_benchmark_api.serialize_public_execution')
    def test_detail_returns_serializer_publication_verbatim(self, serializer):
        public_payload = {
            'status': 'ready',
            'panel': {'slug': 'public-panel', 'name': 'Frozen name', 'description': ''},
            'execution': {
                'status': 'success', 'completed_at': '2026-07-27T00:00:00Z',
                'total_cases': 1, 'total_runs': 2, 'success': 1, 'partial': 0,
                'failed': 0, 'cancelled': 0,
                'cases': [{
                    'coordinates': {'spec_key': 'fury', 'scenario_key': 'st', 'profile_key': 'raid'},
                    'labels': {'spec': 'Fury', 'scenario': 'Single target', 'profile': 'Raid'},
                    'status': 'success',
                    'candidates': [{'key': 'baseline', 'label': 'Baseline', 'type': 'baseline',
                                    'icon_url': '', 'source_label': '', 'status': 'success', 'dps': 100}],
                }],
            },
        }
        serializer.return_value = public_payload

        response = self.client.get('/portal/api/simc-benchmarks/panels/public-panel/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), public_payload)
        serializer.assert_called_once()
        self.assertEqual(serializer.call_args.args[0].pk, self.public.pk)

    @patch('botend.portal.simc_benchmark_api.serialize_public_execution')
    def test_private_and_missing_slugs_are_identical_not_ready(self, serializer):
        private_response = self.client.get('/portal/api/simc-benchmarks/panels/secret-panel/')
        disabled_response = self.client.get('/portal/api/simc-benchmarks/panels/disabled-panel/')
        missing_response = self.client.get('/portal/api/simc-benchmarks/panels/missing-panel/')

        expected = {'status': 'not_ready', 'execution': None}
        self.assertEqual(json.loads(private_response.content), expected)
        self.assertEqual(json.loads(disabled_response.content), expected)
        self.assertEqual(json.loads(missing_response.content), expected)
        serializer.assert_not_called()

    def test_public_endpoints_are_read_only(self):
        self.assertEqual(self.client.post('/portal/api/simc-benchmarks/panels/').status_code, 405)
        self.assertEqual(self.client.post('/portal/api/simc-benchmarks/panels/public-panel/').status_code, 405)


class PortalSimcBenchmarkUIContractTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[2]
    TEMPLATE = (ROOT / 'templates/portal/index.html').read_text(encoding='utf-8')
    JS = (ROOT / 'static/portal/js/simc-benchmarks.js').read_text(encoding='utf-8')
    CSS = (ROOT / 'static/portal/css/simc-benchmarks.css').read_text(encoding='utf-8')

    def test_portal_wires_an_independent_benchmark_region_and_assets(self):
        soup = BeautifulSoup(self.TEMPLATE, 'html.parser')
        self.assertIsNotNone(soup.select_one('#section-simc-benchmarks'))
        self.assertIsNotNone(soup.select_one('#simc-benchmark-root[aria-live="polite"]'))
        self.assertIn('portal/css/simc-benchmarks.css', self.TEMPLATE)
        self.assertIn('portal/js/simc-benchmarks.js', self.TEMPLATE)

    def test_ui_uses_safe_dom_and_supports_required_states(self):
        self.assertNotIn('innerHTML', self.JS)
        for contract in ('document.createElement', 'textContent', 'replaceChildren',
                         '正在加载', '暂无公开', '加载失败', 'not_ready',
                         'labels.spec', 'labels.scenario', 'labels.profile',
                         'candidate.dps', 'vs baseline', 'of highest'):
            self.assertIn(contract, self.JS)

    def test_each_panel_builds_independent_three_axis_filters(self):
        for contract in ('spec_key', 'scenario_key', 'profile_key',
                         'simc-benchmark-filters', 'renderFilteredCases',
                         '当前筛选条件下没有结果'):
            self.assertIn(contract, self.JS)
        self.assertIn('role", "meter', self.JS)
        self.assertIn('allOption.value = ""', self.JS)

    def test_mobile_layout_and_no_scroll_snap(self):
        self.assertIn('@media (max-width: 640px)', self.CSS)
        self.assertIn('grid-template-columns: minmax(0, 1fr)', self.CSS)
        self.assertNotIn('scroll-snap', self.CSS.lower())
        section = BeautifulSoup(self.TEMPLATE, 'html.parser').select_one('#section-simc-benchmarks')
        self.assertNotIn('snap-section', section.get('class', []))


if __name__ == '__main__':
    unittest.main()
