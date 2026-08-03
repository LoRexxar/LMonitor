import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import call, patch

from bs4 import BeautifulSoup
from django.test import TestCase, override_settings

from botend.models import (
    SimcApl, SimcBackendBinary, SimcBenchmarkCandidate, SimcBenchmarkPanel,
    SimcBenchmarkProfile, SimcBenchmarkScenario, SimcBenchmarkSpec,
    SimcContentTemplate, SimcProfile,
)


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
        with self.assertNumQueries(1):
            response = self.client.get('/portal/api/simc-benchmarks/panels/')
        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload, {
            'status': 'ready',
            'panels': [{
                'id': self.public.id, 'slug': 'public-panel', 'name': 'Public panel',
                'description': 'Public description', 'status': 'not_ready',
                'result_view': 'spec_comparison',
            }],
        })
        serializer.assert_not_called()
        serialized = json.dumps(payload)
        for forbidden in ('created_by_id', 'schedule_enabled', 'interval_seconds',
                          'published_execution_id', 'config', 'task', 'run'):
            self.assertNotIn(forbidden, serialized.lower())

    def test_list_marks_baseline_only_configured_panel_ready(self):
        backend = SimcBackendBinary.objects.create(
            identifier='portal-baseline-only', name='Portal baseline',
            current_version='a' * 40, is_active=True,
        )
        apl_content = 'actions=/auto_attack'
        apl = SimcApl.objects.create(
            name='Portal baseline APL', spec='warrior_fury', content=apl_content,
            owner_user_id=10, is_active=True, is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=hashlib.sha256(apl_content.encode()).hexdigest(),
            validation_revision='a' * 40, validation_game_build='12.0.1',
        )
        template = SimcContentTemplate.objects.create(
            name='Portal baseline template', spec='warrior_fury',
            content='iterations=1000', owner_user_id=10,
        )
        profile = SimcProfile.objects.create(
            user_id=10, name='Portal baseline profile', class_name='warrior',
            spec='warrior_fury', is_active=True,
        )
        panel_spec = SimcBenchmarkSpec.objects.create(
            panel=self.public, class_name='warrior', spec_key='warrior_fury',
            label='Fury', apl=apl, template=template, backend=backend,
        )
        SimcBenchmarkProfile.objects.create(
            panel_spec=panel_spec, profile=profile, label='Raid profile',
        )
        SimcBenchmarkScenario.objects.create(
            panel=self.public, key='patchwerk', name='Patchwerk',
            simulation_params={'iterations': 1000},
        )

        with self.assertNumQueries(1):
            response = self.client.get('/portal/api/simc-benchmarks/panels/')

        public_panel = next(
            panel for panel in json.loads(response.content)['panels']
            if panel['id'] == self.public.id
        )
        self.assertEqual(public_panel['status'], 'ready')
        self.assertEqual(public_panel['result_view'], 'spec_comparison')

    def test_list_marks_panel_with_enabled_candidates_as_benchmark_result(self):
        SimcBenchmarkCandidate.objects.create(
            panel=self.public, key='trinket', label='Trinket',
            candidate_type='gear_swap', params={'candidate_type': 'gear_swap'},
        )

        response = self.client.get('/portal/api/simc-benchmarks/panels/')
        public_panel = json.loads(response.content)['panels'][0]

        self.assertEqual(public_panel['result_view'], 'benchmark')

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
    def test_detail_supports_on_demand_coordinate_projection(self, serializer):
        SimcBenchmarkCandidate.objects.create(
            panel=self.public, key='trinket', label='Trinket',
            candidate_type='gear_swap', params={'candidate_type': 'gear_swap'},
        )
        projection = {
            'panel_id': self.public.id,
            'coordinate_options': [
                {'spec_key': 'fury', 'scenario_key': 'st', 'profile_key': 'raid',
                 'labels': {'spec': 'Fury', 'scenario': 'Single target', 'profile': 'Raid'},
                 'scenario_detail': {'desired_targets': 1, 'max_time': 300}},
                {'spec_key': 'fury', 'scenario_key': 'aoe', 'profile_key': 'raid',
                 'labels': {'spec': 'Fury', 'scenario': 'AoE', 'profile': 'Raid'},
                 'scenario_detail': {'desired_targets': 5, 'max_time': 60}},
            ],
            'coordinates': [{'spec_key': 'fury', 'scenario_key': 'st', 'profile_key': 'raid'}],
        }
        serializer.return_value = projection

        for panel_ref in (str(self.public.id), self.public.slug):
            with self.subTest(panel_ref=panel_ref):
                response = self.client.get(
                    f'/portal/api/simc-benchmarks/panels/{panel_ref}/',
                    {'selected': '1', 'spec': 'fury', 'profile': 'raid', 'scenario': 'st'},
                )
                payload = json.loads(response.content)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(payload['results'], {
                    'coordinate_options': projection['coordinate_options'],
                    'coordinates': projection['coordinates'],
                })
        self.assertEqual(serializer.call_count, 2)
        serializer.assert_has_calls([
            call(
                self.public,
                coordinate_filter={'spec_key': 'fury', 'profile_key': 'raid', 'scenario_key': 'st'},
                include_coordinate_options=True,
            ),
            call(
                self.public,
                coordinate_filter={'spec_key': 'fury', 'profile_key': 'raid', 'scenario_key': 'st'},
                include_coordinate_options=True,
            ),
        ])

    @patch('botend.portal.simc_benchmark_api.serialize_incremental_panel_results')
    def test_baseline_only_detail_projects_all_specs_for_selected_scenario(self, serializer):
        serializer.return_value = {
            'panel_id': self.public.id,
            'coordinate_options': [
                {'spec_key': 'warrior_fury', 'scenario_key': 'st', 'profile_key': 'fury'},
                {'spec_key': 'mage_fire', 'scenario_key': 'st', 'profile_key': 'fire'},
            ],
            'coordinates': [
                {'spec_key': 'warrior_fury', 'scenario_key': 'st', 'profile_key': 'fury'},
                {'spec_key': 'mage_fire', 'scenario_key': 'st', 'profile_key': 'fire'},
            ],
        }

        response = self.client.get(
            f'/portal/api/simc-benchmarks/panels/{self.public.id}/',
            {'selected': '1', 'scenario': 'st'},
        )
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload['result_view'], 'spec_comparison')
        self.assertEqual(len(payload['results']['coordinates']), 2)
        serializer.assert_called_once_with(
            self.public,
            scenario_filter='st',
            include_coordinate_options=True,
        )

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
        serializer.assert_called_once_with(self.private)

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
    PORTAL_JS = (ROOT / 'static/portal/js/main.js').read_text(encoding='utf-8')
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

    def test_home_lists_public_baseline_tasks_below_wago_monitoring(self):
        soup = BeautifulSoup(self.TEMPLATE, 'html.parser')
        sections = soup.select('.snap-section')
        wago_section = soup.select_one('#section-wow-skill-diff')
        baseline_section = soup.select_one('#section-simc-baselines')
        self.assertIsNotNone(baseline_section)
        self.assertLess(sections.index(wago_section), sections.index(baseline_section))
        title_link = baseline_section.select_one(
            'a[href="/portal/simc-benchmarks/"]'
        )
        self.assertIsNotNone(title_link)
        self.assertEqual(title_link.get_text(' ', strip=True), 'SimC 基线任务')
        self.assertIsNotNone(baseline_section.select_one('#simc-baseline-list'))

        for contract in (
            'loadPublicBaselines',
            '/portal/api/simc-benchmarks/panels/',
            'panel.result_view === "spec_comparison"',
            '/portal/simc-benchmarks/${encodeURIComponent(String(panel.id))}/',
        ):
            self.assertIn(contract, self.PORTAL_JS)

    def test_public_renderer_uses_spec_driven_profile_and_scenario_filters(self):
        for contract in ('payload?.results?.coordinate_options', 'spec_key', 'scenario_key', 'profile_key',
                         'syncFilterOptions', 'availableCoordinates', 'profile_key',
                         'renderCoordinate', 'sortCandidates', 'relative', 'baseline'):
            self.assertIn(contract, self.JS)
        self.assertNotIn('innerHTML', self.JS)
        self.assertNotIn('results_finalized_at', self.JS)

    def test_baseline_only_renderer_compares_specs_on_vertical_axis(self):
        for contract in (
            'spec_comparison', 'renderSpecComparison', 'simc-benchmark-spec-chart',
            'simc-benchmark-spec-row', 'simc-benchmark-spec-name',
            'simc-benchmark-spec-bar', '相对最高',
        ):
            self.assertIn(contract, self.JS + self.CSS)
        self.assertIn('params.set("scenario"', self.JS)
        self.assertIn('纵轴：职业专精', self.JS)

    def test_baseline_only_rows_expand_the_corresponding_frozen_profile(self):
        spec_start = self.JS.index('function renderSpecComparison')
        spec_end = self.JS.index('\n  function renderResults', spec_start)
        renderer = self.JS[spec_start:spec_end]

        self.assertIn('renderProfileDetails(', renderer)
        self.assertIn('coordinate?.profile_detail', renderer)
        self.assertIn('simc-benchmark-spec-row-toggle', renderer)
        self.assertIn('aria-expanded', renderer)
        self.assertIn('simc-benchmark-spec-profile-details', renderer)
        self.assertIn('query.set("spec", coordinate?.spec_key', renderer)
        self.assertIn('query.set("profile", coordinate?.profile_key', renderer)
        self.assertIn('nextCoordinate?.profile_detail', renderer)
        self.assertIn('.simc-benchmark-spec-profile-details', self.CSS)
        self.assertNotIn('展开本次模拟 Profile', renderer)

    def test_baseline_only_rows_show_spec_icons(self):
        self.assertIn('coordinate?.spec_icon_url', self.JS)
        self.assertIn('simc-benchmark-spec-icon', self.JS)
        self.assertIn('.simc-benchmark-spec-icon', self.CSS)

    def test_single_panel_fetches_only_the_selected_coordinate_on_filter_changes(self):
        for contract in (
            'params.set("selected", "1")',
            'params.set("spec"', 'params.set("profile"', 'params.set("scenario"',
            'AbortController', 'coordinateRequestController.abort()',
            'await requestJson(`${detailUrl}?${params.toString()}`',
        ):
            self.assertIn(contract, self.JS)
        self.assertIn('payload?.results?.coordinates', self.JS)

    def test_selected_coordinate_response_reconciles_filters_after_config_change(self):
        for contract in (
            'nextPayload?.results?.coordinate_options',
            'coordinateOptions = nextOptions',
            'syncFilterOptions("spec_key", String(resolved?.spec_key || ""))',
            'syncDependentFilters(',
            'syncUrl()',
        ):
            self.assertIn(contract, self.JS)

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
            'const position = (dps)', '最高 DPS',
        ):
            self.assertIn(contract, self.JS)

    def test_result_renderer_shows_candidate_item_level_next_to_item_name(self):
        self.assertIn('candidate.item_level', self.JS)
        self.assertIn('装等', self.JS)

    def test_result_renderer_groups_gear_levels_into_shared_colored_rows(self):
        for contract in (
            'groupGearCandidates', 'candidate.item_id', 'candidate.item_variant_key',
            'buildItemLevelColorMap', 'renderGearResultChart',
            'simc-benchmark-gear-level-legend', 'simc-benchmark-gear-segment',
            'pointerenter', 'pointermove', 'focus', 'blur',
            'simc-benchmark-gear-hover-guide', 'simc-benchmark-gear-tooltip',
        ):
            self.assertIn(contract, self.JS + self.CSS)
        self.assertIn('?v=20260803_raw_report', self.RESULTS_TEMPLATE)

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

    def test_profile_talent_code_is_read_only_with_separate_ptr_simulator_button(self):
        for contract in (
            'profileTalentSimulatorUrl', "params.set('class'", "params.set('spec'",
            "params.set('code'", '/portal/talents/?${params.toString()}',
            'simc-benchmark-profile-talent-code', 'simc-benchmark-profile-talent-link',
            'profileDetail?.talent_version', "params.set('version'", 'target',
            'noopener noreferrer', '打开天赋模拟器',
        ):
            self.assertIn(contract, self.JS)
        self.assertIn('node("code", "simc-benchmark-profile-talent-code", talentCode)', self.JS)
        self.assertIn('node("a", "simc-benchmark-profile-talent-link", "打开天赋模拟器")', self.JS)
        self.assertNotIn('node("a", "simc-benchmark-profile-talent-code', self.JS)

    def test_selected_scenario_profile_has_dedicated_oss_raw_report_button(self):
        for contract in (
            'candidate?.raw_report_url',
            'simc-benchmark-profile-report-link',
            '查看 SimC 原始报告',
            'target = "_blank"',
            'noopener noreferrer',
        ):
            self.assertIn(contract, self.JS + self.CSS)
        self.assertIn('nextRows.find(', self.JS)
        self.assertIn('row?.spec_key === coordinate?.spec_key', self.JS)

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
