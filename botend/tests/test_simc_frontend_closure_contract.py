import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "templates/dashboard/index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "static/dashboard/js/main.js").read_text(encoding="utf-8")
WB = (ROOT / "static/dashboard/js/simc-workbench.js").read_text(encoding="utf-8")
DETAIL = (ROOT / "static/dashboard/js/simc-detail.js").read_text(encoding="utf-8")
SIM = MAIN[MAIN.index("/* === 发起模拟 (新 SimC 模拟面板) === */"):MAIN.index("// 全局表格变量")]
WORKFLOW = HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]


class SimcFrontendClosureContractTests(unittest.TestCase):
    def test_dashboard_bootstrap_dependencies_are_preserved(self):
        """SimC 重构不能删除首页及数据库导航依赖的全局初始化函数。"""
        for function_name in (
            "initSubmenuToggle",
            "initTableSelection",
            "calculateTotalRecords",
        ):
            self.assertIn(f"function {function_name}(", MAIN)
        for declaration in (
            "let currentPage = 1;",
            "let pageSize = 50;",
            "let totalPages = 1;",
            "let totalCount = 0;",
        ):
            self.assertIn(declaration, MAIN)

    def test_run_forms_are_reference_only_and_validate_all_three_references(self):
        for token in ("simc_profile_id", "base_template_id", "selected_apl_id"):
            self.assertIn(token, SIM)
        for token in ("base_template_content", "override_action_list", "profile_name"):
            self.assertNotIn(token, SIM)
        self.assertIn("requireSimcRunReferences", SIM)
        self.assertNotIn('id="base-template-content"', WORKFLOW)
        self.assertNotIn('id="apl-override"', WORKFLOW)
        self.assertNotIn('id="simc-sim-save-profile-btn"', WORKFLOW)

    def test_home_creation_success_returns_to_unified_history(self):
        creation = SIM[SIM.index("async function startSelectedSimcCandidateComparisons"):SIM.index("function bindSimcWorkbenchSimulationControls")]
        self.assertGreaterEqual(creation.count("switchSimcWorkbenchL1Tab('history')"), 3)
        self.assertNotIn("window.location.assign(`/dashboard/simc/tasks/", creation)
        self.assertNotIn("window.simcWorkbenchShowTaskDetail('tasks',", creation)
        self.assertGreaterEqual(creation.count("/api/simc-task/comparison/"), 2)
        self.assertNotIn("switchSimcWorkbenchTab('artifacts')", creation)
        self.assertNotIn("loadArtifacts", creation)

    def test_candidate_and_attribute_requests_use_their_current_reference_contracts(self):
        candidate = SIM[SIM.index("async function startSelectedSimcCandidateComparisons"):SIM.index("function stopSimcCandidateComparisonPolling")]
        attribute = SIM[SIM.index("function simcAttributeSearchRequestBody"):SIM.index("async function submitSimcAttributeSearch")]
        for token in ("simc_profile_id", "base_template_id", "selected_apl_id"):
            self.assertIn(token, candidate)
        for token in ("player_source", "spec"):
            self.assertIn(token, candidate)
            self.assertIn(token, attribute)
        self.assertIn("...references", attribute)
        for body in (candidate, attribute):
            for token in ("player_equipment", "profile_name", "base_template_content", "override_action_list"):
                self.assertNotIn(token, body)

    def test_comparison_editor_lives_in_simulation_column_and_supports_slot_groups_and_manual_rows(self):
        mode_index = WORKFLOW.index('id="simc-sim-mode"')
        editor_index = WORKFLOW.index('id="simc-sim-comparison-candidates"')
        player_detail_index = WORKFLOW.index('id="simc-sim-player-detail"')
        self.assertLess(mode_index, editor_index)
        self.assertLess(editor_index, player_detail_index)
        for token in (
            'id="simc-comparison-add-slot"',
            'id="simc-comparison-add-line"',
            'id="simc-comparison-add-btn"',
            'class="simc-comparison-current',
            'id="simc-comparison-simulation-count"',
            'data-candidate-slot-group',
            'data-candidate-card',
            'addSimcManualComparisonCandidate',
            'updateSimcComparisonSimulationCount',
        ):
            self.assertIn(token, WORKFLOW + SIM)

    def test_comparison_rows_are_compact_and_baseline_is_optional(self):
        editor = SIM[SIM.index("function updateSimcComparisonSimulationCount"):SIM.index("function addSimcManualComparisonCandidate")]
        submit = SIM[SIM.index("async function startSelectedSimcCandidateComparisons"):SIM.index("function stopSimcCandidateComparisonPolling")]
        self.assertIn('simc-comparison-current', editor)
        self.assertNotIn('disabled class="simc-comparison-current', editor)
        self.assertNotIn('当前配置</strong>', editor)
        self.assertIn('data-candidate-item-row', editor)
        self.assertIn('include_base', submit)
        self.assertIn(".simc-comparison-current:checked", submit)

    def test_comparison_count_is_beside_submit_button(self):
        submit_index = WORKFLOW.index('id="simc-sim-submit-btn"')
        count_index = WORKFLOW.index('id="simc-comparison-simulation-count"')
        self.assertLess(abs(submit_index - count_index), 900)
        render = SIM[SIM.index("function updateSimcComparisonSimulationCount"):SIM.index("function renderSimcComparisonCandidates")]
        self.assertIn("baseSelected", render)
        self.assertIn("baseSelected + selected.length", render)

    def test_default_and_candidate_talent_codes_open_the_portal_simulator(self):
        editor = SIM[SIM.index("function simcTalentSimulatorUrl"):SIM.index("function addSimcManualComparisonCandidate")]
        self.assertIn("new URLSearchParams()", editor)
        self.assertIn("params.set('class'", editor)
        self.assertIn("params.set('spec'", editor)
        self.assertIn("params.set('code', buildCode)", editor)
        self.assertIn("/portal/talents/?${params.toString()}", editor)
        self.assertIn("simcTalentSimulatorLink(defaultTalent.talent)", editor)
        self.assertIn("simcTalentSimulatorLink(row.talent)", editor)
        self.assertIn('data-talent-simulator-link', editor)
        self.assertNotIn('<label data-candidate-card="default-talent"', editor)
        self.assertNotIn('<label data-candidate-card="talent"', editor)
        self.assertGreaterEqual(editor.count('target=\"_blank\"'), 1)
        self.assertGreaterEqual(editor.count('rel=\"noopener noreferrer\"'), 1)
        self.assertIn('grid-cols-1', editor)
        self.assertNotIn('md:grid-cols-2', editor)
        self.assertIn('truncate font-mono', editor)
        self.assertIn('data-copy-talent-code', editor)
        self.assertIn('navigator.clipboard.writeText', editor)
        self.assertIn('复制', editor)
        self.assertIn("dashboard/js/main.js' %}?v=", HTML)

    def test_comparison_detail_uses_lowest_dps_relative_bar_chart_and_frozen_context(self):
        for token in (
            '最低 DPS 基准',
            'comparison-relative-bar-chart',
            'comparison-simulation-context',
            'candidate_icon_url',
        ):
            self.assertIn(token, DETAIL)

    def test_task_detail_has_visual_comparison_and_attribute_analysis(self):
        for token in (
            'comparison-hero', 'comparison-winner', 'comparison-delta',
            'comparison-baseline', 'deltaPercent', '结果不完整',
            'comparison_baseline', '实际变化', '保持不变',
            'changeDetail', 'unchangedDetail', 'baseline-facts',
            'baseline-content', 'baseline-stats', 'baseline-talent', 'baseline-equipment',
            '基础属性', '基准天赋', '基准装备',
            'attribute-landscape', 'attribute-stat-delta',
            '搜索轨迹', '推荐属性',
        ):
            self.assertIn(token, DETAIL)
        self.assertNotIn('dps-bar', DETAIL)
        self.assertIn('Number.isFinite(deltaPercent)', DETAIL)
        self.assertIn(': NaN;\n      const deltaPercent', DETAIL)
        self.assertNotIn("Number.isFinite(Number(delta)) ?", DETAIL)

    def test_comparison_prioritizes_lowest_dps_relative_bars_before_baseline_details(self):
        for token in ('comparison-relative-bars', 'comparison-relative-bar-chart', '最低 DPS 基准', '候选差异'):
            self.assertIn(token, DETAIL)
        # The display reference is the lowest complete candidate, while fill width
        # remains relative to the best candidate so every bar fits its track.
        for contract in ('minimumDps = chartItems.length', 'maximumDps = chartItems.length',
                         'dps / minimumDps * 100', 'dps / maximumDps * 100',
                         'comparison-simulation-context'):
            self.assertIn(contract, DETAIL)
        return_block = DETAIL[DETAIL.index('return `<section class="hero ${isAttribute'):]
        baseline_token = "${isAttribute ? '' : baselinePanel}"
        self.assertLess(return_block.index('${isAttribute ? \'\' : comparisonChartPanel}'), return_block.index(baseline_token))
        self.assertLess(return_block.index("card(isAttribute ? '候选测量排名' : '候选差异与 DPS 排名'"), return_block.index(baseline_token))
        self.assertLess(return_block.index(baseline_token), return_block.index("card('任务进度'"))

    def test_rerun_form_explains_frozen_task_copy_without_edit_controls(self):
        start = WB.index("async function renderTaskRerunForm")
        end = WB.index("async function submitTaskRerun", start)
        form = WB[start:end]
        self.assertIn('完整复制该任务的冻结请求', form)
        for token in ('name="name"', 'name="iterations"', 'name="fight_style"',
                      'name="simc_profile_id"', 'name="base_template_id"', 'name="selected_apl_id"'):
            self.assertNotIn(token, form)
        submit_start = WB.index("async function submitTaskRerun")
        submit_end = WB.index("async function", submit_start + len("async function submitTaskRerun"))
        submit = WB[submit_start:submit_end]
        self.assertIn("JSON.stringify({ action: 'rerun' })", submit)
        for forbidden in ("prompt(", "alert(", "confirm(", "window.open("):
            self.assertNotIn(forbidden, submit)

    def test_task_detail_is_permanent_result_home(self):
        for token in ("任务进度", "候选 Runs", "DPS 排名", "Artifact"):
            self.assertIn(token, DETAIL)
        self.assertIn('row.runs', DETAIL)
        self.assertNotIn("function loadArtifacts", WB)
        self.assertNotIn("artifactPage", WB)

    def test_task_structured_result_never_parses_html_report_url_as_json(self):
        comparison = WB[WB.index("async function showTaskComparison"):WB.index("async function resourceOptions")]
        self.assertIn("/api/simc-regular-compare/?task_id=", comparison)
        self.assertIn("summary=1", comparison)
        self.assertIn("data.data?.runs", comparison)
        self.assertIn("data.data?.attribute_report", comparison)
        self.assertIn("renderAttributeReport", comparison)
        self.assertNotIn("report_url", comparison)
        self.assertNotIn("window.location", comparison)
        self.assertNotIn("response.json", comparison)

    def test_task_run_audit_is_complete_and_does_not_render_raw_error_detail(self):
        detail = WB[WB.index("async function showTaskDetail"):WB.index("async function showTaskComparison")]
        for token in ("run.sequence", "run.status", "run.result_summary?.dps", "run.input_hash",
                      "run.started_at", "run.completed_at", "safeRunErrorSummary(run)"):
            self.assertIn(token, detail)
        self.assertNotIn("run.error_detail", detail)

    def test_rerun_never_sends_frozen_request_overrides(self):
        form = WB[WB.index("async function renderTaskRerunForm"):WB.index("async function submitTaskRerun")]
        submit = WB[WB.index("async function submitTaskRerun"):WB.index("async function", WB.index("async function submitTaskRerun") + 20)]
        self.assertNotIn("data-original-id", form)
        for token in ("allowedPatch", "simulation_params", "profile_id", "template_id", "apl_id"):
            self.assertNotIn(token, submit)

    def test_profile_quick_switch_aborts_and_ignores_stale_resource_and_detail_results(self):
        profile = SIM[SIM.index("let simcProfileSwitchGeneration"):SIM.index("function renderSimcComparisonCandidates")]
        for token in ("simcProfileSwitchGeneration", "simcProfileSwitchAbortController", "new AbortController()",
                      "controller.abort()", "isCurrentSimcProfileSwitch", "error.name === 'AbortError'"):
            self.assertIn(token, profile)
        self.assertIn("signal: control.controller.signal", profile)
        self.assertIn("renderSimcComparisonCandidates({}, [])", profile)
        self.assertIn("simc-sim-player-detail", profile)
        detail = SIM[SIM.index("async function refreshSimcPlayerDetail"):SIM.index("let simcCandidatePollControl")]
        self.assertIn("simcPlayerDetailAbortController", detail)
        self.assertIn("selectedSimcReferenceValue('#simc-sim-profile-select') !== simc_profile_id", detail)

    def test_saved_profile_detail_renders_complete_parsed_player_configuration(self):
        detail = SIM[SIM.index("function renderSimcSavedProfileDetail"):SIM.index("let simcCandidatePollControl")]
        for token in (
            "detail.equipment",
            "item.slot_label",
            "item.display_name",
            "item.item_level",
            "item.enchant",
            "item.gems",
            "detail.talents",
            "talents.build_code",
            "stats.primary",
            "stats.secondary",
            "detail.missing_fields",
        ):
            self.assertIn(token, detail)
        self.assertIn("renderSimcSavedProfileDetail(detail)", detail)
        self.assertNotIn("JSON.stringify(stats.secondary", detail)


if __name__ == "__main__":
    unittest.main()
