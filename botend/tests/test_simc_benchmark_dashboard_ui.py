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
            'task_bindings', '成功', '失败', '进行中', 'background:true',
            'BENCHMARK_POLL_MS', 'forceDiscoveryUntil', 'listFetchInFlight',
            '子任务（Case）', '候选 Run', 'run_counts', 'renderRunProgress',
        ):
            self.assertIn(contract, JS)
        for selector in (
            '.benchmark-progress-track', '.benchmark-status-counts',
            '.benchmark-current-case', '.benchmark-metadata',
        ):
            self.assertIn(selector, CSS)

    def test_cross_execution_results_are_a_selectable_dimensioned_ranking(self):
        """The aggregate is a result list, not a flattened coordinate preview."""
        aggregate = JS[JS.index('function renderAggregatedResults('):JS.index('function renderRunProgress(')]
        for contract in (
            "'模拟结果'", 'buildAggregateMatrix', 'collectAggregateDimension',
            "['spec_key','spec','专精']", "['scenario_key','scenario','战斗场景']",
            "['profile_key','profile','Profile']", 'benchmark-aggregate-filters',
            'benchmark-aggregate-list-title', 'sort((left,right)=>right.dps-left.dps)',
            'candidate.label||candidate.key||\'候选方案\'', 'baseline_dps',
            'delta_percent', '相对基准', 'selectedCoordinates',
            'const initial=values.size===1?values.keys().next().value:String(coordinates[0]?.[key]||\'\');',
        ):
            self.assertIn(contract, aggregate)
        self.assertIn('页面打开时，按已完成模拟的不可变结果即时生成；不创建额外模拟或聚合任务。', aggregate)
        self.assertNotIn('其余 ${rows.length-limit} 项结果已折叠显示', aggregate)
        # Execution 的“查看结果”不是跨批次完整聚合结果入口，不能拿它误导用户。
        self.assertNotIn('完整对比请打开“查看结果”', aggregate)
        self.assertNotIn('聚合结果待生成', JS)
        self.assertNotIn('聚合结果已保存', JS)
        self.assertNotIn('无聚合结果', JS)
        for selector in (
            '.benchmark-aggregate-filters', '.benchmark-aggregate-filter',
            '.benchmark-aggregate-list-title', '.benchmark-aggregate-row',
            '.benchmark-aggregate-candidate', '.benchmark-aggregate-delta',
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

    def test_history_detail_click_makes_loading_and_errors_visible(self):
        """A failed detail request must not look like an inert history button."""
        detail = JS[JS.index('async function executionDetail('):JS.index('function renderExecution(', JS.index('async function executionDetail('))]
        self.assertIn("state.textContent='正在加载执行详情…'", detail)
        self.assertIn("host.scrollIntoView({behavior:'smooth',block:'nearest'})", detail)
        self.assertIn("const message=`详情加载失败：${e.message}`", detail)
        self.assertIn("state.textContent=message", detail)
        self.assertIn("notify(message,'error')", detail)

    def test_compact_gear_fields_preserve_canonical_slot_in_payload(self):
        self.assertIn("slot:swap?.slot||slotMatch?.[1]||'trinket1'", JS)
        self.assertIn("fallback=`${meta.slot||'trinket1'}=id=${itemId},ilevel=${itemLevel}`", JS)

    def test_compact_gear_fields_preserve_hidden_candidate_identity_and_params(self):
        self.assertIn("key:data.key||''", JS)
        self.assertIn("params:data.params", JS)
        self.assertIn('candidateParams(meta,itemId,itemLevel)', JS)
        self.assertIn("if(meta.key)candidate.key=levels.length>1", JS)

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
        for text in ('面板概览', '高级配置', '定时策略', '专精配置', '场景', '候选装备'):
            self.assertIn(text, CONFIG_PAGE)
        self.assertIn("actionButton('history','子任务状态'", JS)
        self.assertIn("actionButton('results','查看结果'", JS)
        self.assertIn("actionButton('rerun-failed','批量失败重跑'", JS)
        self.assertIn("['failed','partial','cancelled'].includes(execution.status)", JS)
        self.assertIn("benchmarkFetch(`${API}executions/${id}/rerun-failed/`", JS)
        self.assertIn('function executionUrl(id)', JS)
        self.assertIn('function executionDetail(id)', JS)
        self.assertIn('function loadExecutionPage()', JS)
        self.assertIn('function rerunFailedPage(id,button)', JS)
        self.assertIn("dataset:{rerunFailed:data.id}", JS)
        self.assertIn('?v=20260731b', INDEX)
        self.assertIn("if(!configPage){document.body.classList.add", JS)
        self.assertIn("data-benchmark-notification", JS)
        self.assertNotIn('data-create-only', CONFIG_PAGE)

    def test_config_page_keeps_the_primary_save_action_in_the_sticky_header(self):
        soup = BeautifulSoup(CONFIG_PAGE, "html.parser")
        header = cast(Tag, soup.select_one('.simc-benchmark-config-toolbar'))
        self.assertIsNotNone(header)
        self.assertIsNotNone(header.select_one('[data-editor-save]'))
        self.assertIsNotNone(header.select_one('[data-save-status][aria-live="polite"]'))
        self.assertIsNone(soup.select_one('.simc-benchmark-editor-footer [data-editor-save]'))
        self.assertIn('position: sticky', CSS)
        self.assertIn('.simc-benchmark-config-toolbar', CSS)
        self.assertIn('savedPayloadFingerprint', JS)
        self.assertIn("'配置已保存'", JS)
        self.assertIn("'有未保存的修改'", JS)
        self.assertIn('const requestFingerprint=JSON.stringify(payload)', JS)
        self.assertIn('JSON.stringify(collectPayload())===requestFingerprint', JS)
        self.assertIn('savedPayloadFingerprint=requestFingerprint', JS)
        self.assertIn('.simc-benchmark-config-page *', CSS)
        self.assertIn('box-sizing: border-box', CSS)

    def test_config_page_keeps_only_frequent_fields_open_and_folds_advanced_fields(self):
        soup = BeautifulSoup(CONFIG_PAGE, "html.parser")
        form = cast(Tag, soup.select_one('[data-benchmark-form]'))
        primary = cast(Tag, form.select_one('[data-primary-settings]'))
        advanced = cast(Tag, form.select_one('details[data-advanced-section]'))
        self.assertIsNotNone(primary.select_one('input[name="panel_name"]'))
        self.assertIsNotNone(primary.select_one('input[name="is_active"]'))
        for field_name in ('slug', 'description', 'is_public', 'schedule_enabled', 'interval_seconds', 'next_run_at'):
            self.assertIsNone(primary.select_one(f'[name="{field_name}"]'))
            self.assertIsNotNone(advanced.select_one(f'[name="{field_name}"]'))
        self.assertFalse(advanced.has_attr('open'))
        self.assertIn('高级配置', cast(Tag, advanced.select_one('summary')).get_text(' ', strip=True))

    def test_nested_resources_and_simulation_metadata_are_collapsed_per_card(self):
        self.assertIn("advancedGroup('资源与 Profiles'", JS)
        self.assertIn("advancedGroup('SimC 参数'", JS)
        self.assertIn("class:'config-card-primary'", JS)
        self.assertIn("class:'config-card-advanced'", JS)

    def test_spec_cards_only_expose_spec_and_enabled_before_expansion(self):
        segment = JS[JS.index('function addSpec('):JS.index('function updateSpecResources(')]
        self.assertIn("selectField('专精 *','spec_key'", segment)
        self.assertIn("checkbox('启用','is_enabled'", segment)
        self.assertNotIn("field('显示名", segment)
        self.assertIn("advancedGroup('资源与 Profiles'", segment)
        self.assertIn("spec?.spec_label||spec?.label", JS)

    def test_spec_configuration_uses_compact_table_rows(self):
        segment = JS[JS.index('function addSpec('):JS.index('function updateSpecResources(')]
        self.assertIn("class:'config-card spec-config-row'", segment)
        self.assertIn('.spec-config-list', CSS)
        self.assertIn(".spec-config-row .config-card-primary", CSS)
        self.assertIn("专精", CSS)

    def test_candidate_cards_support_optional_rows_item_lookup_and_multiple_levels(self):
        segment = JS[JS.index('function addCandidate('):JS.index('function localDate(')]
        self.assertIn("field('装备 ID','item_id','number'", segment)
        self.assertIn("field('装等（逗号分隔多个） *','item_level','text'", segment)
        self.assertIn('item-lookup/?item_id=', segment)
        self.assertIn(".split(',').map(x=>x.trim()).filter(Boolean)", JS)
        self.assertIn("if(!itemId)return []", JS)
        for removed_field in (
            "field('key", "field('label", "field('单条装备行",
            "field('来源标签", "field('图标 URL", 'candidateSpecPicker(',
        ):
            self.assertNotIn(removed_field, segment)

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
