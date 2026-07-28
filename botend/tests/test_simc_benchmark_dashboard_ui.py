"""Small static contract for the isolated SimC Benchmark Dashboard UI."""
import unittest
from pathlib import Path
from typing import cast

from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parents[2]
INDEX = (ROOT / "templates/dashboard/index.html").read_text(encoding="utf-8")
PARTIAL = (ROOT / "templates/dashboard/_simc_benchmark.html").read_text(encoding="utf-8")
CONFIG_PATH = ROOT / "templates/dashboard/simc_benchmark_config.html"
CONFIG_PAGE = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ''
JS = (ROOT / "static/dashboard/js/simc-benchmark-dashboard.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/dashboard/css/simc-benchmark-dashboard.css").read_text(encoding="utf-8")


class SimcBenchmarkDashboardUIContractTests(unittest.TestCase):
    def test_simc_sidebar_entries_share_one_parent_group(self):
        soup = BeautifulSoup(INDEX, "html.parser")
        group = soup.select_one('.nav-item.has-submenu[data-section="simc"]')
        self.assertIsNotNone(group)
        children = cast(Tag, group).select('.submenu-item[data-dashboard-section]')
        self.assertEqual(
            [child.get('data-dashboard-section') for child in children],
            ['simc-workbench', 'simc-benchmarks'],
        )
        self.assertEqual(
            [child.get_text(' ', strip=True) for child in children],
            ['SimC 工具台', '基准面板'],
        )
        self.assertEqual(len(soup.select('.nav-item[data-section="simc-workbench"]')), 0)
        self.assertEqual(len(soup.select('.nav-item[data-section="simc-benchmarks"]')), 0)

        parent_link = cast(Tag, cast(Tag, group).select_one(':scope > a'))
        submenu = cast(Tag, cast(Tag, group).select_one(':scope > .submenu'))
        self.assertIn('open', ' '.join(cast(Tag, group).get_attribute_list('class')))
        self.assertEqual(parent_link.get('aria-expanded'), 'true')
        self.assertNotIn('max-h-0', ' '.join(submenu.get_attribute_list('class')))

        self.assertIn("dashboard-section-changed", (ROOT / "static/dashboard/js/main.js").read_text(encoding="utf-8"))
        self.assertIn("dashboard-section-changed", JS)
        self.assertIn("e.detail?.section==='simc-benchmarks'", JS)

    def test_staff_entry_section_partial_and_assets_are_wired(self):
        self.assertIn('data-dashboard-section="simc-benchmarks"', INDEX)
        self.assertIn('user.is_staff', INDEX)
        self.assertIn('dashboard/_simc_benchmark.html', INDEX)
        self.assertIn('simc-benchmark-dashboard.css', INDEX)
        self.assertIn('simc-benchmark-dashboard.js', INDEX)
        soup = BeautifulSoup(PARTIAL, "html.parser")
        self.assertIsNotNone(soup.select_one('#simc-benchmarks[data-simc-benchmark-root]'))
        title = cast(Tag, soup.select_one('#simc-benchmarks h2'))
        self.assertEqual(title.get_text(strip=True), 'SimC 基准面板')

    def test_editor_and_history_are_independent_accessible_dialogs(self):
        soup = BeautifulSoup(PARTIAL, "html.parser")
        editor = soup.select_one('[data-benchmark-editor][role="dialog"][aria-modal="true"]')
        history = soup.select_one('[data-benchmark-history][role="dialog"][aria-modal="true"]')
        self.assertIsNotNone(editor)
        self.assertIsNotNone(history)
        self.assertIsNone(cast(Tag, editor).select_one('[data-benchmark-history]'))
        self.assertIn('datetime-local', PARTIAL)

    def test_only_seven_structured_scenario_keys_and_no_json_editor(self):
        expected = {'iterations', 'target_error', 'fight_style', 'max_time',
                    'vary_combat_length', 'enemy_type', 'desired_targets'}
        marker = "const STRUCTURED_PARAMS = ["
        segment = JS[JS.index(marker):JS.index('];', JS.index(marker))]
        self.assertEqual({key for key in expected if f"'{key}'" in segment}, expected)
        self.assertNotIn('JSON textarea', PARTIAL)
        self.assertNotIn('name="simulation_params"', PARTIAL)
        self.assertNotIn('csrf_exempt', JS + PARTIAL)

    def test_fetch_contract_and_stale_request_protection(self):
        for contract in ("resolved.origin !== window.location.origin",
                         "credentials:'same-origin'", "X-CSRFToken",
                         "payload.success !== true", "response.status === 204",
                         'AbortController', 'generation'):
            self.assertIn(contract, JS)
        self.assertNotIn('.innerHTML', JS)

    def test_resource_numeric_profile_and_portal_contracts(self):
        self.assertIn("row.spec_key", JS)
        self.assertIn("resourceMatches(x,spec,true)", JS)
        self.assertIn("resourceMatches(x,spec)", JS)
        self.assertIn("dataset:{profileIncluded:p.id}", JS)
        self.assertIn("dataset:{profileEnabled:p.id}", JS)
        self.assertIn("is_enabled:enabled.checked", JS)
        self.assertIn("['iterations','desired_targets'].includes(key)", JS)
        self.assertIn("['target_error','max_time','vary_combat_length'].includes(key)", JS)
        self.assertIn("input.step='any'", JS)
        self.assertIn("encodeURIComponent(panel.slug)", JS)
        self.assertIn("'_blank','noopener'", JS)

    def test_failed_loads_cannot_restore_stale_data_or_enable_saving(self):
        self.assertIn("panels=[];listLoadError=", JS)
        self.assertIn("if(listLoadError)", JS)
        self.assertIn("editorReady=false;resources=null", JS)
        self.assertIn("if(!editorReady)return", JS)
        self.assertIn("!editorReady&&!x.matches('[data-editor-close]')", JS)

    def test_real_statuses_resource_empty_states_and_mobile_rows_are_supported(self):
        for status in ('pending', 'running', 'success', 'partial', 'failed', 'cancelled'):
            self.assertIn(f"{status}:[", JS)
        self.assertIn('当前专精无可用 Profile', JS)
        self.assertIn('当前专精无可用 ${kind}', JS)
        self.assertIn('.profile-choice { grid-template-columns:', CSS)
        self.assertIn('.history-runs-scroll', CSS)

    def test_panel_page_surfaces_batch_progress_statuses_and_metadata(self):
        soup = BeautifulSoup(PARTIAL, "html.parser")
        headers = [node.get_text(' ', strip=True) for node in soup.select('.simc-benchmark-table th')]
        self.assertIn('执行进度', headers)
        for contract in (
            'renderExecutionProgress', 'current_cases', 'config_frozen',
            'task_bindings', 'results_available', '聚合结果已保存',
            '成功', '失败', '进行中', 'background:true',
            'BENCHMARK_POLL_MS', 'forceDiscoveryUntil', 'listFetchInFlight',
        ):
            self.assertIn(contract, JS)
        for selector in (
            '.benchmark-progress-track', '.benchmark-status-counts',
            '.benchmark-current-case', '.benchmark-metadata',
        ):
            self.assertIn(selector, CSS)

    def test_history_has_independent_abort_and_generation_guards(self):
        for contract in ('historyListController', 'historyDetailController',
                         'historyReconcileController', 'historyGeneration',
                         'historyIsCurrent'):
            self.assertIn(contract, JS)
        self.assertIn("dialog.hidden=true", JS)
        self.assertIn("historyListController?.abort()", JS)
        self.assertIn("historyDetailController?.abort()", JS)
        self.assertIn("historyReconcileController?.abort()", JS)

    def test_gear_line_preserves_canonical_slot_and_raw_value_shape(self):
        self.assertIn("`${swap.slot}=${swap.raw_value}`", JS)

    def test_create_is_lightweight_and_defers_detailed_fields_to_configuration(self):
        soup = BeautifulSoup(PARTIAL, "html.parser")
        create_only = soup.select_one('[data-create-only] [data-editor-create-specs]')
        configuration_only = soup.select('[data-configuration-only]')
        self.assertIsNotNone(create_only)
        self.assertGreaterEqual(len(configuration_only), 4)
        create_default = cast(Tag, create_only).parent
        self.assertIsNotNone(create_default)
        self.assertIn('选择要启用的专精', cast(Tag, create_default).get_text(' ', strip=True))
        self.assertIn("'配置'", JS)
        self.assertIn('createDefaultSpec', JS)
        self.assertIn('renderCreateSpecPicker', JS)
        self.assertIn("editingId===null", JS)
        self.assertIn("key:'patchwerk'", JS)
        self.assertIn("schedule_enabled:false", JS)
        self.assertIn("resources?.create_defaults?.[specKey]", JS)
        self.assertIn("create-spec-unavailable", JS)
        self.assertIn("candidates:[]", JS)

    def test_configuration_uses_a_dedicated_page_with_every_detail_section(self):
        self.assertIn('dashboard/simc/benchmarks/', JS)
        self.assertNotIn("if(action.dataset.action==='edit')openEditor(id)", JS)
        self.assertIn('data-benchmark-config-page', CONFIG_PAGE)
        self.assertIn('data-benchmark-panel-id', CONFIG_PAGE)
        for text in ('基础信息', '定时', '专精配置', '场景', '候选装备'):
            self.assertIn(text, CONFIG_PAGE)
        self.assertIn('simc-benchmark-dashboard.js', CONFIG_PAGE)
        self.assertIn('?v=20260728a', CONFIG_PAGE)
        self.assertIn('?v=20260728a', INDEX)
        self.assertIn("if(!configPage)document.body.classList.add", JS)
        self.assertIn("data-benchmark-notification", JS)
        self.assertNotIn('data-create-only', CONFIG_PAGE)

    def test_panel_name_does_not_collide_with_nested_scenario_names(self):
        for markup in (PARTIAL, CONFIG_PAGE):
            soup = BeautifulSoup(markup, "html.parser")
            form = cast(Tag, soup.select_one('[data-benchmark-form]'))
            self.assertIsNotNone(form.select_one('input[name="panel_name"]'))
            self.assertIsNone(form.select_one('input[name="name"]'))
        self.assertIn('form.elements.panel_name.value', JS)
        self.assertNotIn('form.elements.name.value', JS)

    def test_mobile_full_screen_and_local_table_overflow(self):
        self.assertIn('@media (max-width: 640px)', CSS)
        self.assertIn('width: 100vw', CSS)
        self.assertIn('height: 100dvh', CSS)
        self.assertIn('overflow-x: auto', CSS)
        self.assertIn('min-height: 44px', CSS)


if __name__ == '__main__':
    unittest.main()
