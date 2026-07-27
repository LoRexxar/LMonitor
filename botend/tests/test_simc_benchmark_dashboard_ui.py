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
    def test_staff_entry_section_partial_and_assets_are_wired(self):
        self.assertIn('data-section="simc-benchmarks"', INDEX)
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
        self.assertIn("if(!configPage)document.body.classList.add", JS)
        self.assertIn("data-benchmark-notification", JS)
        self.assertNotIn('data-create-only', CONFIG_PAGE)

    def test_mobile_full_screen_and_local_table_overflow(self):
        self.assertIn('@media (max-width: 640px)', CSS)
        self.assertIn('width: 100vw', CSS)
        self.assertIn('height: 100dvh', CSS)
        self.assertIn('overflow-x: auto', CSS)
        self.assertIn('min-height: 44px', CSS)


if __name__ == '__main__':
    unittest.main()
