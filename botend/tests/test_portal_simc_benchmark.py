import json
import unittest
from pathlib import Path
from unittest.mock import call, patch

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
                'id': self.public.id, 'slug': 'public-panel', 'name': 'Public panel',
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
            'panel': {'id': self.public.id, 'slug': 'public-panel', 'name': 'Public panel', 'description': 'Public description'},
            'results': {'coordinates': projection['coordinates']},
        })
        serializer.assert_called_once_with(self.public)
        self.assertNotIn('execution', payload)

    @patch('botend.portal.simc_benchmark_api.serialize_incremental_panel_results')
    def test_private_panel_is_hidden_from_list_but_openable_by_known_slug(self, serializer):
        serializer.return_value = {'coordinates': [{'spec_key': 'private'}]}
        list_response = self.client.get('/portal/api/simc-benchmarks/panels/')
        private_response = self.client.get('/portal/api/simc-benchmarks/panels/secret-panel/')
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(private_response.status_code, 200)
        self.assertNotIn('secret-panel', json.dumps(json.loads(list_response.content)))
        self.assertEqual(json.loads(private_response.content), {
            'status': 'ready',
            'panel': {
                'id': self.private.id, 'slug': 'secret-panel', 'name': 'Secret panel', 'description': 'Secret config',
            },
            'results': {'coordinates': [{'spec_key': 'private'}]},
        })
        serializer.assert_has_calls([call(self.public), call(self.private)])

    @patch('botend.portal.simc_benchmark_api.serialize_incremental_panel_results')
    def test_detail_is_openable_by_stable_numeric_panel_id(self, serializer):
        serializer.return_value = {'coordinates': [{'spec_key': 'fury'}]}
        response = self.client.get(f'/portal/api/simc-benchmarks/panels/{self.public.id}/')
        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload['status'], 'ready')
        self.assertEqual(payload['panel']['id'], self.public.id)
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


@override_settings(ALLOWED_HOSTS=['testserver'])
class PortalSimcBenchmarkPageTests(TestCase):
    def test_numeric_panel_route_renders_panel_identity_for_javascript(self):
        response = self.client.get(
            '/portal/simc-benchmarks/1/?spec=fury&profile=raid&scenario=patchwerk',
        )
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, 'html.parser')
        root = soup.select_one('#simc-benchmark-root')
        self.assertEqual(root.get('data-panel-id'), '1')


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

    def test_public_renderer_uses_spec_driven_profile_and_scenario_filters(self):
        for contract in ('payload?.results?.coordinates', 'spec_key', 'scenario_key', 'profile_key',
                         'syncFilterOptions', 'availableCoordinates', 'profile_key',
                         'renderCoordinate', 'sortCandidates', 'relative', 'baseline'):
            self.assertIn(contract, self.JS)
        self.assertNotIn('innerHTML', self.JS)
        self.assertNotIn('results_finalized_at', self.JS)

    def test_numeric_panel_route_and_filter_query_are_synchronized(self):
        for contract in (
            'root.dataset.panelId',
            'params.get("spec")', 'params.get("profile")', 'params.get("scenario")',
            'params.set("spec"', 'params.set("profile"', 'params.set("scenario"',
            'window.history.replaceState',
            '${encodeURIComponent(String(panelId))}/',
        ):
            self.assertIn(contract, self.JS)
        self.assertNotIn('get("benchmark")', self.JS)

    def test_dashboard_opens_numeric_panel_route_instead_of_slug_query(self):
        dashboard_js = (self.ROOT / 'static/dashboard/js/simc-benchmark-dashboard.js').read_text(encoding='utf-8')
        self.assertIn('/portal/simc-benchmarks/${encodeURIComponent(id)}/', dashboard_js)
        self.assertNotIn('/portal/simc-benchmarks/?benchmark=', dashboard_js)

    def test_public_renderer_hides_baseline_candidates_but_keeps_comparison_data(self):
        self.assertIn('candidate.type === "base"', self.JS)
        self.assertIn('allCandidates.find(isBaseline)', self.JS)
        self.assertIn('allCandidates.filter((candidate) => !isBaseline(candidate))', self.JS)
        self.assertIn('[baseline, ...candidates].filter(Boolean)', self.JS)

    def test_result_renderer_shows_percentage_axis_and_selected_basic_info(self):
        for contract in (
            'simc-benchmark-basic-info', 'simc-benchmark-info-spec',
            'simc-benchmark-range-note', 'scale.range > 0', '((dps - scale.lowest) / scale.range)',
            'ratio.toFixed(1)', '最高 DPS',
        ):
            self.assertIn(contract, self.JS)

    def test_result_renderer_uses_frozen_target_count_and_duration_for_scenarios(self):
        for contract in (
            'scenario_detail', 'scenarioLabel', 'desired_targets', 'max_time',
            '${targets} 目标', '${numberFormat.format(maxTime)} 秒',
        ):
            self.assertIn(contract, self.JS)

    def test_result_renderer_has_collapsible_profile_detail_panel(self):
        for contract in (
            'profile_detail', 'simc-benchmark-profile-details', 'profile-details-toggle',
            'identity', 'equipment', 'talents',
        ):
            self.assertIn(contract, self.JS)

    def test_profile_talent_code_stays_read_only_with_separate_simulator_link(self):
        for contract in (
            'profileTalentSimulatorUrl', "params.set('class'", "params.set('spec'",
            "params.set('code'", '/portal/talents/?${params.toString()}',
            'simc-benchmark-profile-talent-code', 'simc-benchmark-profile-talent-link',
            '打开天赋模拟器', 'target', 'noopener noreferrer',
        ):
            self.assertIn(contract, self.JS)
        self.assertNotIn('node("a", "simc-benchmark-profile-talent-code"', self.JS)

    def test_result_styles_make_percentage_comparison_and_baseline_contrasting(self):
        for contract in (
            'simc-benchmark-axis-labels', 'simc-benchmark-axis-label',
            'simc-benchmark-bar--baseline', '#0f766e', '#1d4ed8',
            'background: #e2e8f0',
        ):
            self.assertIn(contract, self.CSS)

    def test_single_panel_uses_editable_panel_copy_for_page_heading(self):
        self.assertIn('applyPanelHeading', self.JS)
        self.assertIn("document.title", self.JS)
        self.assertIn('simc-benchmarks-description', self.RESULTS_TEMPLATE)

    def test_result_page_omits_redundant_projection_copy(self):
        for redundant_copy in (
            'SIMC BENCHMARK RESULTS',
            '已完成模拟坐标',
            '结果投影',
        ):
            self.assertNotIn(redundant_copy, self.RESULTS_TEMPLATE + self.JS)

    def test_result_list_uses_page_scroll_instead_of_an_internal_vertical_scroller(self):
        """候选结果必须自然展开，不能截留页面滚轮。"""
        chart_start = self.CSS.index('.simc-benchmark-chart {')
        chart_end = self.CSS.index('}', chart_start) + 1
        chart_css = self.CSS[chart_start:chart_end]
        mobile_start = self.CSS.index('@media (max-width: 640px)')
        mobile_css = self.CSS[mobile_start:]

        for contract in ('display: grid', 'simc-benchmark-candidate-grid',
                         'simc-benchmark-candidate-source', 'grid-template-areas'):
            self.assertIn(contract, self.CSS)
        self.assertNotIn('max-height', chart_css)
        self.assertNotIn('overflow-y', chart_css)
        self.assertNotIn('.simc-benchmark-chart { max-height', mobile_css)

    def test_mobile_layout_and_no_scroll_snap(self):
        self.assertIn('@media (max-width: 640px)', self.CSS)
        self.assertIn('grid-template-columns: minmax(0, 1fr)', self.CSS)
        self.assertNotIn('scroll-snap', self.CSS.lower())
        section = BeautifulSoup(self.RESULTS_TEMPLATE, 'html.parser').select_one('#simc-benchmark-root')
        self.assertIsNotNone(section)
        self.assertNotIn('snap-section', section.get('class', []))


if __name__ == '__main__':
    unittest.main()
