import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "templates/dashboard/index.html").read_text(encoding="utf-8")
JS = (ROOT / "static/dashboard/js/simc-workbench.js").read_text(encoding="utf-8")
MAIN = (ROOT / "static/dashboard/js/main.js").read_text(encoding="utf-8")

# Scope safety assertions to the complete SimC surfaces. The dashboard template
# and main.js also contain unrelated legacy modules with their own navigation UI.
SIMC_HTML = (
    HTML[HTML.index('<div class="content-section" id="simc-workbench"'):HTML.index('<!-- Tools内容区域 -->')]
    + HTML[HTML.index('<!-- SimC Workbench Unified Dialog -->'):]
)
SIMC_MAIN = MAIN[
    MAIN.index('/* ===== SimC Workbench Dialog ===== */'):
    MAIN.index('// 全局表格变量')
]


class SimcWorkbenchFrontendContractTests(unittest.TestCase):
    def test_high_risk_resource_navigation_and_aria_contract(self):
        self.assertIn("syncTaskSubtabs(requestedResource)", JS)
        self.assertIn("state.taskResource = normalized", JS)
        self.assertIn("resourceUrl(requestedResource)", JS)
        self.assertIn("aria-selected", JS)
        self.assertIn('data-task-subtab="tasks"', HTML)
        self.assertIn('aria-selected="true"', HTML)
        self.assertIn("data.ruleSubtab", MAIN)
        self.assertIn("switchRuleSubtab(model)", MAIN)

    def test_batch_detail_members_have_safe_reachable_task_detail(self):
        self.assertIn("Array.isArray(row.tasks)", JS)
        self.assertIn('data-wb-action="detail" data-resource="tasks"', JS)
        self.assertIn("can_view", JS)
        self.assertIn("批次详情", JS)

    def test_template_permissions_and_type_round_trip(self):
        form_start = JS.index("function renderTemplateForm")
        form_end = JS.index("function closeTemplateForm", form_start)
        form_body = JS[form_start:form_end]
        self.assertNotIn("default_player", form_body)
        self.assertIn("payload.template_type", JS)
        self.assertIn("!readOnly", JS)
        self.assertIn("我的模板可编辑", JS)
        self.assertIn("系统内置只读", JS)
        self.assertIn("上游同步只读", JS)
        for template_type in ("base_template", "default_apl", "custom_apl", "custom_player"):
            self.assertIn(f"value: '{template_type}'", form_body)
        self.assertNotIn("report_template", form_body)
        self.assertNotIn("command_fragment", form_body)
        template_panel = HTML[HTML.index('id="simc-workbench-templates-panel"'):HTML.index('id="simc-workbench-apl-panel"')]
        self.assertNotIn("can_write", template_panel)

    def test_keyword_detail_and_immutable_edit_contract(self):
        self.assertIn("showAplKeywordDetail", JS)
        self.assertIn('data-wb-action="keyword-detail"', JS)
        self.assertIn("if (!id) payload.apl_keyword", JS)
        self.assertIn("row ? 'readonly' : ''", JS)
        self.assertIn("openDialog('keyword-detail')", JS)
        self.assertIn("openDialog('keyword-form')", JS)
        self.assertNotIn('id="simc-wb-apl-keyword-detail"', HTML)
        self.assertNotIn('id="simc-wb-apl-keyword-form"', HTML)

    def test_profiles_always_offer_detail_and_inactive_has_no_run_actions(self):
        self.assertIn('data-profile-row-action="detail"', MAIN)
        self.assertIn("simcWbShowProfileDetail", MAIN)
        self.assertIn("已停用，不可加载或运行", MAIN)
        self.assertNotIn('id="simc-wb-profile-detail"', HTML)
        self.assertIn("openSimcWorkbenchDialog('profile-detail'", MAIN)
        detail_start = MAIN.index("function simcWbShowProfileDetail")
        detail_end = MAIN.index("function bindSimcWorkbenchProfilesControls", detail_start)
        detail_body = MAIN[detail_start:detail_end]
        self.assertIn("'/api/simc-workbench/profiles/'", detail_body)
        self.assertNotIn("'/api/simc-profile/'", detail_body)
        for token in (
            "simcWbProfileDetailRequestSerial",
            "simcWbProfileDetailAbortController",
            "simcWbProfileDetailId",
            "new AbortController()",
            "error.name === 'AbortError'",
            "simcWbCancelProfileDetail",
        ):
            self.assertIn(token, MAIN)
        self.assertIn("requestSerial !== simcWbProfileDetailRequestSerial", detail_body)
        self.assertIn("simcWbProfileDetailId !== String(id)", detail_body)

    def test_task_dialog_owns_artifact_preview_states(self):
        self.assertNotIn('id="simc-workbench-artifacts-panel"', HTML)
        self.assertNotIn('data-artifact-filter="task_id"', HTML)
        self.assertNotIn('data-artifact-filter="artifact_type"', HTML)
        start = JS.index('async function showTaskDetail')
        end = JS.index('\n    async function', start + 20)
        detail = JS[start:end]
        self.assertIn('row.artifacts', detail)
        self.assertIn('data-artifact-preview', detail)
        self.assertIn('data-artifact-preview-action="retry"', JS)
        self.assertIn('data-artifact-preview-action="close"', JS)

    def test_artifact_preview_is_only_rendered_for_supported_rows(self):
        self.assertIn("row.can_preview === true", JS)
        self.assertIn("a.can_preview === true", JS)
        self.assertIn("仅供下载结果记录", JS)

    def test_profile_list_ignores_aborted_and_stale_responses(self):
        start = MAIN.index("function loadSimcWorkbenchProfiles")
        end = MAIN.index("function simcWbShowProfileDetail", start)
        body = MAIN[start:end]
        for token in (
            "simcWbProfileListRequestSerial",
            "simcWbProfileListAbortController",
            "new AbortController()",
            "signal: abortController.signal",
            "error.name === 'AbortError'",
            "requestSerial !== simcWbProfileListRequestSerial",
            "requestedFilter",
            "requestedPage",
        ):
            self.assertIn(token, body)

    def test_shared_details_abort_and_ignore_stale_responses(self):
        for token in (
            "detailRequestSerial",
            "detailAbortController",
            "beginDetailRequest",
            "isCurrentDetailRequest",
            "cancelDetailRequest",
        ):
            self.assertIn(token, JS)
        for function_name in ("showTaskDetail", "showBatchComparison", "showTemplateDetail", "showAplKeywordDetail"):
            start = JS.index(f"async function {function_name}")
            body = JS[start:JS.index("\n    }", start) + 6]
            self.assertIn("beginDetailRequest", body)
            self.assertIn("isCurrentDetailRequest", body)

    def test_loading_empty_error_retry_and_no_fake_pagination(self):
        self.assertIn("renderState(host, 'loading'", JS)
        self.assertIn('data-wb-retry=', JS)
        self.assertNotIn('id="simc-wb-rules-pagination"', HTML)
        self.assertNotIn('id="simc-wb-mastery-pagination"', HTML)

    def test_compact_mobile_structure_and_business_groups(self):
        self.assertIn('@media (max-width: 640px)', HTML)
        self.assertIn('.simc-responsive-row', HTML)
        self.assertIn('.simc-touch-action', HTML)
        self.assertIn('class="simc-workflow-step"', HTML)
        self.assertIn('<details', HTML)
        for group in ("模拟工作流", "历史任务", "高级设置", "执行后端"):
            self.assertIn(group, HTML)
        workflow = HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
        self.assertNotIn('p-5 h-full', workflow)

    def test_advanced_only_has_system_capabilities(self):
        advanced_start = HTML.index('data-simc-l1-panel="advanced"')
        advanced_end = HTML.index('<!-- End L1 Panel: 高级设置 -->')
        advanced = HTML[advanced_start:advanced_end]
        self.assertIn('aria-label="SimC 系统模型入口"', advanced)
        for resource in ("secondary-rules", "mastery-rules", "backend", "apl-keywords"):
            self.assertIn(f'data-simc-model="{resource}"', advanced)
        for resource in ("batches", "tasks", "artifacts", "profiles", "apl-storage"):
            self.assertNotIn(f'data-simc-model="{resource}"', advanced)

    def test_advanced_capabilities_use_same_tab_navigation_as_workflow(self):
        advanced_start = HTML.index('data-simc-l1-panel="advanced"')
        advanced_end = HTML.index('<!-- End L1 Panel: 高级设置 -->')
        advanced = HTML[advanced_start:advanced_end]
        self.assertIn('<nav class="mb-4 flex flex-wrap gap-2" aria-label="SimC 系统模型入口">', advanced)
        self.assertNotIn('simc-compact-panel', advanced)
        for resource in ("backend", "secondary-rules", "mastery-rules", "apl-keywords"):
            self.assertIn(f'data-simc-model="{resource}"', advanced)
        self.assertEqual(advanced.count('data-rule-subtab="secondary-rules"'), 1)
        self.assertEqual(advanced.count('data-rule-subtab="mastery-rules"'), 1)
        self.assertNotIn('aria-label="规则类型"', advanced)
        self.assertIn('updateSimcAdvancedEntryState(activeL1Tab, activeChildPanel, activeRuleSubtab)', MAIN)

    def test_all_l1_panels_share_the_same_padded_container(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(HTML, "html.parser")
        panels = [soup.select_one(f'[data-simc-l1-panel="{name}"]') for name in ("workflow", "history", "advanced")]
        self.assertTrue(all(panel is not None for panel in panels))
        self.assertTrue(all(panel.parent is panels[0].parent for panel in panels))
        self.assertIn("p-5", panels[0].parent.get("class", []))

    def test_workflow_is_default_l1_with_history_and_advanced(self):
        self.assertIn('data-simc-l1-tab="workflow"', HTML)
        self.assertIn('data-simc-l1-tab="history"', HTML)
        self.assertIn('data-simc-l1-tab="advanced"', HTML)
        self.assertIn('data-simc-l1-panel="workflow"', HTML)
        self.assertIn('data-simc-l1-panel="history"', HTML)
        self.assertIn('data-simc-l1-panel="advanced"', HTML)
        self.assertIn("switchSimcWorkbenchL1Tab('workflow')", MAIN)
        workflow_panel_start = HTML.index('data-simc-l1-panel="workflow"')
        workflow_end = HTML.index('<!-- End L1 Panel: 模拟工作流 -->')
        self.assertIn('id="simc-workbench-import-panel"', HTML[workflow_panel_start:workflow_end])
        history_panel_start = HTML.index('data-simc-l1-panel="history"')
        history_end = HTML.index('<!-- End L1 Panel: 历史任务 -->')
        self.assertIn('id="simc-workbench-tasks-panel"', HTML[history_panel_start:history_end])
        advanced_panel_start = HTML.index('data-simc-l1-panel="advanced"')
        advanced_end = HTML.index('<!-- End L1 Panel: 高级设置 -->')
        advanced = HTML[advanced_panel_start:advanced_end]
        self.assertNotIn('id="simc-workbench-profiles-panel"', advanced)
        self.assertNotIn('id="simc-workbench-artifacts-panel"', advanced)
        workflow = HTML[workflow_panel_start:workflow_end]
        self.assertIn('id="simc-workbench-profiles-panel"', workflow)
        self.assertIn('id="simc-workbench-templates-panel"', workflow)
        self.assertIn('id="simc-workbench-apl-panel"', workflow)

    def test_history_panel_has_task_batch_subtabs(self):
        self.assertIn('data-simc-panel="tasks"', HTML)
        self.assertIn('data-task-subtab="tasks"', HTML)
        self.assertIn('data-task-subtab="batches"', HTML)
        self.assertIn("window.simcWorkbenchLoadPanel = activate", JS)

    def test_history_polling_is_cancelled_and_stale_responses_are_ignored(self):
        self.assertIn("window.simcWorkbenchDeactivatePanel = deactivate", JS)
        self.assertIn("scheduleTaskRefresh(false)", JS)
        self.assertIn("state.taskRequestSerial += 1", JS)
        self.assertIn("requestSerial !== state.taskRequestSerial || state.activePanel !== 'tasks'", JS)
        self.assertIn("resource !== state.taskResource || page !== state.taskPage", JS)
        self.assertIn("window.simcWorkbenchDeactivatePanel(activeChildPanel)", MAIN)

    def test_history_fetch_is_aborted_on_deactivation(self):
        self.assertIn("taskAbortController: null", JS)
        self.assertIn("const controller = new AbortController()", JS)
        self.assertIn("{ signal: controller.signal }", JS)
        self.assertIn("state.taskAbortController.abort()", JS)
        self.assertIn("error.name === 'AbortError'", JS)

    def test_leaving_workbench_stops_all_history_and_candidate_polling(self):
        self.assertIn("function deactivateSimcWorkbench()", MAIN)
        self.assertIn("if (sectionId !== 'simc-workbench') deactivateSimcWorkbench();", MAIN)
        self.assertIn("window.simcWorkbenchDeactivatePanel('')", MAIN)
        self.assertIn("stopSimcCandidateComparisonPolling()", MAIN)
        self.assertIn("clearTimeout(simcCandidatePollControl.timer)", MAIN)
        self.assertIn("control.resolve(false)", MAIN)

    def test_candidate_comparison_post_and_poll_share_cancellation(self):
        self.assertIn("let simcCandidateGeneration = 0", MAIN)
        self.assertIn("controller: new AbortController()", MAIN)
        self.assertIn("signal: control.controller.signal", MAIN)
        self.assertIn("control.controller.abort()", MAIN)
        self.assertIn("isCurrentSimcCandidateControl(control)", MAIN)
        self.assertIn("control.button.disabled = false", MAIN)
        self.assertIn("control.button.innerHTML = control.oldLabel", MAIN)

    def test_attribute_search_generation_cancels_get_and_continue_post(self):
        self.assertIn("let simcAttributeSearchGeneration = 0", MAIN)
        self.assertIn("function stopSimcAttributeSearch()", MAIN)
        self.assertIn("isCurrentSimcAttributeSearch(generation)", MAIN)
        self.assertIn("submitSimcAttributeSearch(payload, signal)", MAIN)
        self.assertIn("signal: simcAttributeSearchControl.controller.signal", MAIN)
        self.assertIn("stopSimcAttributeSearch()", MAIN)
        self.assertIn("oldLabel: button?.innerHTML", MAIN)
        self.assertIn("control.button.disabled = false", MAIN)
        self.assertIn("control.button.innerHTML = control.oldLabel", MAIN)

    def test_legacy_task_loader_is_fully_removed(self):
        self.assertNotIn("fetchSimcTaskData", MAIN)
        self.assertIn("window.simcWorkbenchLoadTaskResource('batches')", MAIN)

    def test_profile_mode_sync_defines_form_wrapper(self):
        start = MAIN.index("function simcWbSyncProfileFormMode()")
        body = MAIN[start:MAIN.index("\n}", start) + 2]
        self.assertIn("const formWrap = document.getElementById('simc-wb-profile-form')", body)

    def test_artifact_uses_actual_sandbox_helper(self):
        self.assertNotIn('id="simc-workbench-artifacts-panel"', HTML)
        self.assertIn("openSimcWorkbenchDialog('task-detail'", JS)
        self.assertIn("window.renderSimcArtifactFrame(url", JS)
        self.assertIn('sandbox=""', MAIN)
        self.assertIn("/api/simc-workbench/", JS)
        self.assertIn("artifacts", JS)

    def test_dedicated_api_and_inline_sections(self):
        self.assertIn("const apiRoot = '/api/simc-workbench/'", JS)
        for template_type in ("base_template", "default_apl", "custom_apl", "custom_player"):
            self.assertIn(f'data-template-type="{template_type}"', HTML)
        self.assertNotIn('data-template-type="report_template"', HTML)
        self.assertNotIn('data-template-type="command_fragment"', HTML)
        self.assertIn("UserAplStorage", HTML)
        self.assertIn("AplKeywordPair", HTML)
        self.assertIn('data-rule-subtab="secondary-rules"', HTML)
        self.assertIn('data-rule-subtab="mastery-rules"', HTML)
        self.assertIn('data-rule-panel="secondary-rules"', HTML)
        self.assertIn('data-rule-panel="mastery-rules"', HTML)
        for panel in ("tasks", "templates", "apl", "backend"):
            marker = f'id="simc-workbench-{panel}-panel"'
            start = HTML.index(marker)
            self.assertNotIn('></div>', HTML[start:start + len(marker) + 20])

    def test_template_filters_include_default_player_and_default_apl(self):
        """Template type filters must include both default_player (default player config) and default_apl."""
        filters_start = HTML.index('id="simc-template-type-filters"')
        filters_end = HTML.index('</div>', filters_start)
        filters_section = HTML[filters_start:filters_end]
        self.assertIn('data-template-type="default_player"', filters_section,
                      "Template filters must include default_player button for default player configurations")
        self.assertIn('data-template-type="default_apl"', filters_section,
                      "Template filters must include default_apl button")

    def test_workbench_controller_has_no_unsafe_or_legacy_navigation(self):
        forbidden = (
            "window.open(", 'target="_blank"', "alert(", "prompt(", "confirm(",
            "modal", "appendChild", "开发中", "stub", "'/dashboard/'",
        )
        lowered = JS.lower()
        for token in forbidden:
            self.assertNotIn(token.lower(), lowered)
        self.assertIn("Number.parseInt", JS)
        self.assertIn("window.escapeHtml", JS)
        self.assertIn("startsWith('/')", JS)
        self.assertEqual(MAIN.count("function escapeHtml"), 1)

    def test_apl_storage_has_dialog_crud_and_simulation_loading(self):
        self.assertNotIn('id="simc-wb-apl-storage-form"', HTML)
        self.assertIn("openSimcWorkbenchDialog('apl-form'", JS)
        self.assertIn('data-inline-create="apl-storage"', HTML)
        self.assertIn("'/api/apl-storage/'", JS)
        self.assertIn('data-my-apl-action="edit"', JS)
        self.assertIn('data-my-apl-action="archive"', JS)
        self.assertIn('data-my-apl-action="restore"', JS)
        self.assertIn('data-my-apl-action="use"', JS)
        self.assertIn("window.loadSimcWorkbenchApl", JS)
        self.assertNotIn("confirm(", JS)
        self.assertNotIn("onclick=", JS.lower())

    def test_apl_converter_is_independent_workflow_panel_not_in_my_apl(self):
        """APL converter must be independent workflow panel, not nested in 我的APL section."""
        apl_panel_start = HTML.index('id="simc-workbench-apl-panel"')
        apl_panel_end = HTML.index('<!-- End L1 Panel: 模拟工作流', apl_panel_start)
        apl_panel_section = HTML[apl_panel_start:apl_panel_end]
        self.assertNotIn('APL 双向转换器', apl_panel_section)
        self.assertNotIn('simc-wb-convert-', apl_panel_section)

        workflow_start = HTML.index('data-simc-l1-panel="workflow"')
        workflow_end = HTML.index('<!-- End L1 Panel: 模拟工作流', workflow_start)
        workflow_section = HTML[workflow_start:workflow_end]
        self.assertIn('id="simc-workbench-apl-converter-panel"', workflow_section)
        self.assertIn('data-simc-panel="apl-converter"', workflow_section)
        self.assertIn('data-simc-workflow-entry="apl-converter"', workflow_section)

    def test_apl_converter_has_full_control_and_mobile_safe_layout(self):
        """Independent converter must have direction switch, copy output, clear, status, char/line counts."""
        converter_start = HTML.index('id="simc-workbench-apl-converter-panel"')
        next_panel = HTML.index('id="simc-workbench-profiles-panel"', converter_start)
        converter_section = HTML[converter_start:next_panel]
        self.assertIn('data-converter-action="switch"', converter_section)
        self.assertIn('data-converter-action="execute"', converter_section)
        self.assertIn('data-converter-action="copy-output"', converter_section)
        self.assertIn('data-converter-action="clear"', converter_section)
        self.assertIn('id="simc-converter-status"', converter_section)
        self.assertIn('id="simc-converter-input"', converter_section)
        self.assertIn('id="simc-converter-output"', converter_section)
        self.assertIn('max-width: 640px', HTML)

    def test_my_apl_has_search_and_all_crud_in_dialog(self):
        """My APL must have search input, detail/edit dialogs, archive/restore actions."""
        apl_panel_start = HTML.index('id="simc-workbench-apl-panel"')
        apl_panel_end = HTML.index('<!-- End L1 Panel: 模拟工作流', apl_panel_start)
        my_apl_section = HTML[apl_panel_start:apl_panel_end]
        self.assertIn('我的 APL', my_apl_section)
        self.assertIn('id="simc-my-apl-search"', my_apl_section)
        self.assertIn('placeholder="搜索', my_apl_section)
        self.assertIn('showMyAplDetail', JS)
        self.assertIn("openDialog('apl-detail'", JS)
        self.assertIn("openSimcWorkbenchDialog('apl-form'", JS)
        self.assertIn('data-my-apl-action="detail"', JS)
        self.assertIn('data-my-apl-action="edit"', JS)
        self.assertIn('data-my-apl-action="use"', JS)
        self.assertIn('data-my-apl-action="archive"', JS)
        self.assertIn('data-my-apl-action="restore"', JS)
        detail_start = JS.index('async function showMyAplDetail')
        detail_end = JS.index('\n    async function', detail_start + 20)
        detail_body = JS[detail_start:detail_end]
        self.assertIn("`/api/apl-storage/${id}/`", detail_body)
        self.assertIn('beginDetailRequest', detail_body)
        self.assertIn('isCurrentDetailRequest', detail_body)

    def test_default_apl_library_shows_active_selectable_templates_with_spec(self):
        """Default APL library must show active+selectable default_apl templates with class/spec display."""
        apl_panel_start = HTML.index('id="simc-workbench-apl-panel"')
        apl_panel_end = HTML.index('<!-- End L1 Panel: 模拟工作流', apl_panel_start)
        apl_section = HTML[apl_panel_start:apl_panel_end]
        self.assertIn('默认 APL 库', apl_section)
        self.assertIn('id="simc-default-apl-list"', apl_section)
        self.assertIn('id="simc-default-apl-search"', apl_section)
        self.assertIn('loadDefaultAplLibrary', JS)
        self.assertIn("library: 'default_apl'", JS)
        self.assertNotIn("template_type: 'default_apl'", JS)
        self.assertNotIn('is_active: true', JS)
        self.assertNotIn('is_selectable: true', JS)
        self.assertIn('data-default-apl-action="view"', JS)
        self.assertIn('data-default-apl-action="copy"', JS)
        self.assertIn('.class_name', JS)
        self.assertIn('.spec', JS)

    def test_default_apl_copy_uses_backend_api_not_client_content(self):
        """Copy default APL must POST copy_template_id to backend, not send content from browser."""
        self.assertIn('copy_template_id', JS)
        self.assertIn("'/api/apl-storage/'", JS)
        self.assertIn("method: 'POST'", JS)
        copy_handler_start = JS.index('data-default-apl-action="copy"')
        copy_section = JS[copy_handler_start:copy_handler_start + 2000]
        self.assertNotIn('content:', copy_section)
        self.assertNotIn('apl_code:', copy_section)

    def test_default_apl_library_view_shows_readonly_detail(self):
        """View default APL must show readonly detail in dialog with source/spec info."""
        self.assertIn('showDefaultAplDetail', JS)
        self.assertIn("openDialog('default-apl-detail'", JS)
        detail_start = JS.index('async function showDefaultAplDetail')
        detail_end = JS.index('\n    async function', detail_start + 20)
        detail_body = JS[detail_start:detail_end]
        self.assertIn("`${resourceUrl('templates', id)}?library=default_apl`", detail_body)
        self.assertIn('readonly', detail_body)
        self.assertIn('.source', detail_body)
        self.assertIn('.spec', detail_body)

    def test_script_is_really_loaded(self):
        self.assertIn("{% static 'dashboard/js/simc-workbench.js' %}", HTML)
        self.assertNotIn("moveSimcToolIntoWorkbench", MAIN)

    def test_profile_inline_form_uses_delegated_actions_not_inline_handlers(self):
        start = HTML.index('id="simc-workbench-profiles-panel"')
        end = HTML.index('id="simc-workbench-templates-panel"', start)
        profile_panel = HTML[start:end]
        self.assertNotIn('onclick=', profile_panel)
        for action in ('create', 'close', 'save'):
            self.assertIn(f'data-profile-form-action="{action}"', profile_panel)
        bind_start = MAIN.index("function bindSimcWorkbenchProfilesControls()")
        bind_end = MAIN.index("\n\n/* ===== SimC 工具台 — 绿字规则", bind_start)
        bind_body = MAIN[bind_start:bind_end]
        self.assertIn("closest('[data-profile-form-action]')", bind_body)
        self.assertIn("closest('[data-profile-row-action]')", bind_body)
        self.assertIn("'/api/simc-profile/?include_inactive=1'", MAIN)
        self.assertIn('data-profile-row-action="deactivate"', MAIN)
        self.assertIn('data-profile-row-action="restore"', MAIN)
        self.assertIn('status_only: true', MAIN)
        self.assertNotIn('function simcWbDeleteProfile', MAIN)

    def test_workflow_import_mode_uses_delegated_change_handler(self):
        start = HTML.index('id="simc-workbench-import-panel"')
        end = HTML.index('<!-- End L1 Panel: 模拟工作流 -->', start)
        workflow_panel = HTML[start:end]
        self.assertNotIn('onchange=', workflow_panel)
        self.assertIn('name="simc-player-import-mode"', workflow_panel)
        self.assertIn("closest('input[name=\"simc-player-import-mode\"]')", MAIN)

    def test_workbench_profile_and_rule_actions_do_not_use_native_dialogs(self):
        start = MAIN.index('/* --- Profile CRUD --- */')
        end = MAIN.index('async function simcWbEditMastery', start)
        workbench_crud = MAIN[start:end]
        for token in ('prompt(', 'confirm(', 'alert('):
            self.assertNotIn(token, workbench_crud)
        save_start = MAIN.index('async function simcWbSaveCurrentSimulatorProfile()')
        save_end = MAIN.index('\n\n/* --- Rule CRUD --- */', save_start)
        save_body = MAIN[save_start:save_end]
        self.assertIn("switchSimcWorkbenchL1Tab('workflow', 'profiles')", save_body)
        self.assertIn("simcWbToggleProfileForm('create')", save_body)
        self.assertNotIn("fetch('/api/simc-profile/'", save_body)

    def test_mobile_sidebar_toggle_opens_and_closes(self):
        toggle_start = MAIN.index("function toggleSidebar()")
        toggle_end = MAIN.index("function openSidebar()", toggle_start)
        toggle_body = MAIN[toggle_start:toggle_end]
        self.assertIn("closeSidebar();", toggle_body)
        self.assertIn("openSidebar();", toggle_body)

    def test_mobile_sidebar_closes_after_actionable_navigation(self):
        sidebar_start = MAIN.index("function initSidebarToggle()")
        sidebar_end = MAIN.index("function toggleSidebar()", sidebar_start)
        sidebar_body = MAIN[sidebar_start:sidebar_end]
        self.assertIn("sidebar.addEventListener('click'", sidebar_body)
        self.assertIn(".nav-item:not(.has-submenu), .submenu-item", sidebar_body)
        self.assertIn("window.innerWidth < 1024", sidebar_body)
        self.assertIn("closeSidebar();", sidebar_body)

    def test_desktop_resize_restores_body_scrolling(self):
        resize_start = MAIN.index("window.addEventListener('resize'")
        resize_end = MAIN.index("    });", resize_start) + 7
        self.assertIn("document.body.style.overflow = '';", MAIN[resize_start:resize_end])

    def test_navigation_unified_entry_point(self):
        """Navigation must use single unified L1 switching function."""
        self.assertIn("function switchSimcWorkbenchL1Tab(", MAIN)
        self.assertNotIn("window.switchSimcWorkbenchTab", JS)
        self.assertIn("switchSimcWorkbenchL1Tab('workflow')", MAIN)
        init_start = MAIN.index("function initSimcWorkbench(")
        init_end = MAIN.index("function switchSimcWorkbenchL1Tab(")
        init_body = MAIN[init_start:init_end]
        self.assertIn("switchSimcWorkbenchL1Tab('workflow')", init_body)

    def test_l1_active_tab_does_not_keep_touch_hover_background(self):
        switch_start = MAIN.index("function switchSimcWorkbenchL1Tab(")
        switch_end = MAIN.index("\n\nfunction ", switch_start + 50)
        switch_body = MAIN[switch_start:switch_end]
        self.assertIn("tab.classList.toggle('hover:bg-gray-50', !isActive);", switch_body)

    def test_navigation_l1_to_panel_mapping_explicit(self):
        """Each L1 tab must explicitly map to its child panels."""
        switch_l1_start = MAIN.index("function switchSimcWorkbenchL1Tab(")
        switch_l1_end = MAIN.index("\n\nfunction ", switch_l1_start + 50)
        switch_l1_body = MAIN[switch_l1_start:switch_l1_end]
        self.assertIn("workflow: 'import'", switch_l1_body)
        self.assertIn("history: 'tasks'", switch_l1_body)
        self.assertIn("advanced: 'backend'", switch_l1_body)
        self.assertIn("window.simcWorkbenchLoadPanel", switch_l1_body)
        self.assertNotIn("fetchSimcTaskData", switch_l1_body)

    def test_navigation_child_panel_always_selects_its_parent(self):
        switch_start = MAIN.index("function switchSimcWorkbenchTab(")
        switch_end = MAIN.index("\n\n/* ===== SimC", switch_start)
        switch_body = MAIN[switch_start:switch_end]
        self.assertIn("import: 'workflow'", switch_body)
        self.assertIn("tasks: 'history'", switch_body)
        self.assertIn("profiles: 'workflow'", switch_body)
        self.assertIn("artifacts: 'history'", switch_body)
        self.assertIn("'apl-keywords': 'advanced'", switch_body)
        self.assertIn("switchSimcWorkbenchL1Tab(parentTab, activeTab)", switch_body)

    def test_workbench_data_loader_has_no_duplicate_model_navigation_handler(self):
        self.assertNotIn("const tab = event.target.closest('[data-simc-tab]')", JS)

    def test_navigation_task_creation_switches_to_history(self):
        """After task creation, navigation must switch to history L1 panel."""
        self.assertIn("switchSimcWorkbenchL1Tab('history')", MAIN)
        submit_lines = [line for line in MAIN.split("\n") if "simc-sim-submit-btn" in line or "submitSimcSimulation" in line]
        self.assertTrue(len(submit_lines) > 0, "Submit button handler must exist")

    def test_navigation_profile_load_switches_to_workflow(self):
        """Profile load must return to workflow L1 panel."""
        profile_load_lines = [line for line in MAIN.split("\n") if "loadSimcProfile" in line or "simc-sim-saved-profiles" in line]
        self.assertTrue(len(profile_load_lines) > 0, "Profile load handler must exist")

    def test_navigation_no_orphaned_switchSimcWorkbenchTab_calls(self):
        """Old switchSimcWorkbenchTab calls without L1 coordination are forbidden."""
        switch_tab_calls = []
        for i, line in enumerate(MAIN.split("\n"), 1):
            if "switchSimcWorkbenchTab(" in line and "function switchSimcWorkbenchTab(" not in line:
                switch_tab_calls.append((i, line.strip()))
        forbidden_contexts = []
        for line_no, line in switch_tab_calls:
            if any(trigger in line for trigger in ["onClick", "addEventListener", "simc-sim-submit", "Profile", "batch"]):
                start_idx = max(0, line_no - 20)
                end_idx = min(len(MAIN.split("\n")), line_no + 5)
                context = "\n".join(MAIN.split("\n")[start_idx:end_idx])
                if "switchSimcWorkbenchL1Tab" not in context:
                    forbidden_contexts.append(f"Line {line_no}: {line}")
        self.assertEqual(len(forbidden_contexts), 0, f"Found switchSimcWorkbenchTab without L1 coordination: {forbidden_contexts[:3]}")

    def test_navigation_model_entry_must_open_advanced_first(self):
        """Model entry buttons must switch to advanced L1 before opening specific panel."""
        model_entry_start = MAIN.index("'.simc-model-entry'")
        model_entry_end = MAIN.index("});", model_entry_start) + 3
        model_entry_section = MAIN[model_entry_start:model_entry_end]
        self.assertIn("switchSimcWorkbenchL1Tab('advanced')", model_entry_section)

    def test_navigation_simc_workbench_js_has_no_global_navigation(self):
        """simc-workbench.js must not call global L1 navigation functions."""
        self.assertNotIn("switchSimcWorkbenchL1Tab", JS)
        self.assertNotIn("window.switchSimcWorkbenchTab(", JS)

    def test_navigation_default_state_workflow_and_import_visible(self):
        """Initial state: workflow L1 active, import panel visible."""
        self.assertIn('data-simc-l1-tab="workflow"', HTML)
        workflow_tab = HTML[HTML.index('data-simc-l1-tab="workflow"'):HTML.index('data-simc-l1-tab="workflow"') + 300]
        self.assertIn("bg-blue-600", workflow_tab)
        self.assertIn("text-white", workflow_tab)
        workflow_panel = HTML[HTML.index('data-simc-l1-panel="workflow"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
        self.assertNotIn('class="simc-l1-panel hidden"', workflow_panel[:200])
        self.assertIn('id="simc-workbench-import-panel"', workflow_panel)

    def test_rules_management_uses_event_delegation_no_inline_onclick(self):
        """Rules management must use event delegation with data-* attributes, not inline onclick."""
        self.assertNotIn("onclick=\"simcWbEditRule", MAIN)
        self.assertNotIn("onclick=\"simcWbDeleteRule", MAIN)
        self.assertNotIn("onclick=\"simcWbEditMastery", MAIN)
        self.assertNotIn("onclick=\"simcWbDeleteMastery", MAIN)
        self.assertIn("data-rule-action=", MAIN)
        self.assertIn("data-mastery-action=", MAIN)
        self.assertNotIn('querySelector.*onclick', MAIN)

    def test_rules_forms_use_data_attributes_not_onclick(self):
        """Rule form close/save/cancel buttons must use data-* attributes."""
        self.assertNotIn('onclick="simcWbToggleRuleForm', HTML)
        self.assertNotIn('onclick="simcWbSaveRule', HTML)
        self.assertNotIn('onclick="simcWbToggleMasteryForm', HTML)
        self.assertNotIn('onclick="simcWbSaveMastery', HTML)

    def test_rules_buttons_hidden_for_regular_users_via_is_staff_check(self):
        """Regular users should not see rule create/edit/delete buttons."""
        self.assertIn("can_write", MAIN)
        self.assertIn("data-simc-inline-create", HTML)

    def test_template_create_uses_shared_dialog_form(self):
        """Templates panel keeps the entry; the form is rendered in the shared dialog."""
        self.assertIn('data-inline-create="templates"', HTML)
        self.assertNotIn('id="simc-wb-template-form"', HTML)
        self.assertIn("openSimcWorkbenchDialog('template-form'", JS)

    def test_apl_keyword_create_uses_shared_dialog_form(self):
        """APL keywords keep the create entry and render its form in the shared dialog."""
        self.assertIn('data-inline-create="apl-keywords"', HTML)
        self.assertNotIn('id="simc-wb-apl-keyword-form"', HTML)
        self.assertIn("openDialog('keyword-form')", JS)
        self.assertIn("'keyword-form': 'APL 关键词管理'", MAIN)
        self.assertIn("'keyword-detail': 'APL 关键词详情'", MAIN)

    def test_template_click_handlers_exist(self):
        """Template edit/archive/restore/detail handlers must exist."""
        self.assertIn('data-wb-action="template-edit"', JS)
        self.assertIn('data-wb-action="template-detail"', JS)
        self.assertIn('data-template-action="cancel"', JS)
        self.assertIn('data-template-action="close-detail"', JS)

    def test_apl_keyword_click_handlers_exist(self):
        """APL keyword edit/archive/restore/cancel handlers must exist."""
        self.assertIn('data-apl-keyword-action="cancel"', JS)
        self.assertIn('data-apl-keyword-action=', JS)

    def test_apl_keyword_table_has_search_count_and_responsive_columns(self):
        """Large keyword lists must be searchable and remain readable on desktop/mobile."""
        keyword_panel = HTML[
            HTML.index('id="simc-workbench-apl-keywords-panel"'):
            HTML.index('id="simc-workbench-rules-panel"')
        ]
        self.assertIn('id="simc-wb-apl-keyword-search"', keyword_panel)
        self.assertIn('id="simc-wb-apl-keyword-summary"', keyword_panel)
        self.assertIn('aria-label="搜索 APL 关键词"', keyword_panel)
        self.assertIn('function renderAplKeywordTable(', JS)
        self.assertIn("row.apl_keyword, row.cn_keyword, row.description", JS)
        self.assertIn("closest('#simc-wb-apl-keyword-search')", JS)
        self.assertIn('class="simc-responsive-table', JS)
        for heading in ('APL 关键词', '中文关键词', '说明', '状态', '操作'):
            self.assertIn(heading, JS)
        self.assertIn('筛选后', JS)
        self.assertIn('无匹配结果', JS)

    def test_template_submit_handler_exists(self):
        """Template form submission must be handled."""
        self.assertIn('data-template-form', JS)

    def test_apl_keyword_submit_handler_exists(self):
        """APL keyword form submission must be handled."""
        self.assertIn('data-apl-keyword-form', JS)

    def test_activate_does_not_duplicate_load_templates_or_apl(self):
        """activate() must not call loadTemplates or loadApl twice for same tab."""
        activate_start = JS.index('function activate(')
        activate_end = JS.index('\n    window.simcWorkbenchLoadPanel')
        activate_body = JS[activate_start:activate_end]
        self.assertEqual(activate_body.count("if (tab === 'templates')"), 1)
        self.assertEqual(activate_body.count("if (tab === 'apl')"), 1)

    def test_template_detail_calls_showTemplateDetail_not_inline_html(self):
        """template-detail action must call showTemplateDetail function."""
        self.assertIn('function showTemplateDetail(', JS)
        detail_handler = JS[JS.index('data-wb-action'):JS.index('data-wb-action') + 1000]
        self.assertIn('showTemplateDetail', JS)

    def test_backend_controls_post_real_actions_with_csrf(self):
        """Backend check/update/auto-update controls must POST to the dedicated API."""
        self.assertIn('async function runBackendAction(', JS)
        self.assertIn("'/api/simc-backend-binary/'", JS)
        self.assertIn("'X-CSRFToken': window.getCSRFToken()", JS)
        self.assertIn("action: 'set_auto_update'", JS)

    def test_backend_controls_have_delegated_click_and_change_handlers(self):
        """Rendered backend controls must be connected through delegated safe handlers."""
        self.assertIn("closest('[data-backend-action]')", JS)
        self.assertIn("closest('[data-backend-auto-update]')", JS)
        self.assertNotIn('onclick=', JS)

    def test_backend_panel_renders_operational_status_not_only_versions(self):
        """Backend panel must expose availability, progress, status and safe error state."""
        for field in ('available', 'need_update', 'is_updating', 'update_progress',
                      'update_status', 'has_error', 'auto_update'):
            self.assertIn(f'info.{field}', JS)

    def test_old_simc_task_modals_removed_from_html(self):
        """Old SimC task modals (add/edit/view) must be removed."""
        self.assertNotIn('id="add-simc-task-modal"', HTML)
        self.assertNotIn('id="edit-simc-task-modal"', HTML)
        self.assertNotIn('id="view-simc-task-modal"', HTML)
        self.assertNotIn('id="add-simc-task-btn"', HTML)
        self.assertNotIn('id="cancel-add-simc-task"', HTML)
        self.assertNotIn('id="confirm-add-simc-task"', HTML)
        self.assertNotIn('id="cancel-edit-simc-task"', HTML)
        self.assertNotIn('id="confirm-edit-simc-task"', HTML)
        self.assertNotIn('id="close-view-simc-task"', HTML)

    def test_old_simc_profile_modals_removed_from_html(self):
        """Old SimC profile modals (add/edit) must be removed."""
        self.assertNotIn('id="add-simc-profile-modal"', HTML)
        self.assertNotIn('id="edit-simc-profile-modal"', HTML)

    def test_old_simc_modal_functions_removed_from_main_js(self):
        """Old SimC modal open/close/update/delete functions must be removed."""
        forbidden_functions = (
            'function openAddSimcTaskModal',
            'function submitAddSimcTask',
            'function openEditSimcTaskModal',
            'function updateSimcTask',
            'function deleteSimcTask',
            'function deleteSimcProfile',
            'add-simc-task-modal',
            'edit-simc-task-modal',
            'view-simc-task-modal',
            'add-simc-profile-modal',
            'edit-simc-profile-modal',
        )
        for token in forbidden_functions:
            self.assertNotIn(token, MAIN)

    def test_old_simc_modal_event_listeners_removed(self):
        """Old model-specific modal listeners stay removed; one shared dialog replaces them."""
        self.assertNotIn('add-simc-task-btn', MAIN)
        self.assertNotIn('cancel-add-simc-task', MAIN)
        self.assertNotIn('confirm-add-simc-task', MAIN)
        self.assertNotIn('cancel-edit-simc-task', MAIN)
        self.assertNotIn('confirm-edit-simc-task', MAIN)
        self.assertNotIn('close-view-simc-task', MAIN)


class SimcContinuousWorkflowDialogContractTests(unittest.TestCase):
    """Current product contract: main-flow resources and results use one workbench dialog."""

    def _l1_section(self, name, end_marker):
        start = HTML.index(f'data-simc-l1-panel="{name}"')
        end = HTML.index(end_marker, start)
        return HTML[start:end]

    def test_workflow_owns_profiles_user_apl_and_editable_templates(self):
        workflow = self._l1_section('workflow', '<!-- End L1 Panel: 模拟工作流 -->')
        for panel_id in (
            'simc-workbench-profiles-panel',
            'simc-workbench-templates-panel',
            'simc-workbench-apl-panel',
        ):
            self.assertIn(f'id="{panel_id}"', workflow)

    def test_advanced_excludes_user_workflow_and_result_resources(self):
        advanced = self._l1_section('advanced', '<!-- End L1 Panel: 高级设置 -->')
        for resource in ('tasks', 'batches', 'artifacts', 'profiles', 'apl-storage'):
            self.assertNotIn(f'data-simc-model="{resource}"', advanced)
        for panel_id in (
            'simc-workbench-profiles-panel',
            'simc-workbench-artifacts-panel',
            'simc-workbench-apl-panel',
        ):
            self.assertNotIn(f'id="{panel_id}"', advanced)
        for resource in ('secondary-rules', 'mastery-rules', 'apl-keywords', 'backend'):
            self.assertIn(f'data-simc-model="{resource}"', advanced)

    def test_one_accessible_workbench_dialog_exists(self):
        self.assertEqual(HTML.count('id="simc-workbench-dialog"'), 1)
        self.assertIn('role="dialog"', HTML)
        self.assertIn('aria-modal="true"', HTML)
        self.assertIn('id="simc-workbench-dialog-backdrop"', HTML)
        self.assertIn('id="simc-workbench-dialog-content"', HTML)
        self.assertIn('data-simc-dialog-close', HTML)

    def test_dialog_has_keyboard_focus_scroll_and_mobile_contract(self):
        for token in (
            'function openSimcWorkbenchDialog(',
            'function closeSimcWorkbenchDialog(',
            "event.key === 'Escape'",
            "event.key !== 'Tab'",
            'simcWorkbenchDialogPreviousFocus',
            "document.body.classList.add('simc-dialog-open')",
            "document.body.classList.remove('simc-dialog-open')",
        ):
            self.assertIn(token, MAIN)
        mobile = HTML[HTML.index('@media (max-width: 640px)'):]
        self.assertIn('.simc-workbench-dialog__viewport', mobile)
        self.assertIn('padding: 0 !important', mobile)
        self.assertIn('align-items: stretch !important', mobile)
        self.assertIn('.simc-workbench-dialog__panel', mobile)
        self.assertIn('width: 100vw !important', mobile)
        self.assertIn('height: 100dvh !important', mobile)

    def test_legacy_task_report_preview_uses_same_sandbox_allowlist(self):
        helper_start = MAIN.index('function renderSimcArtifactFrame(')
        helper_end = MAIN.index('function openSimcWorkbench(', helper_start)
        helper = MAIN[helper_start:helper_end]
        self.assertIn('tasks\\/\\d+\\/report-preview', helper)
        self.assertIn('sandbox=""', helper)
        self.assertNotIn('window.open', helper)

    def test_dialog_close_lifecycle_clears_stack_without_breaking_nested_replace(self):
        self.assertIn("new CustomEvent('simc-dialog-closing', { detail: { reason: 'replace' } })", MAIN)
        self.assertIn("new CustomEvent('simc-dialog-closing', { detail: { reason: 'close' } })", MAIN)
        self.assertIn("event.detail?.reason === 'close'", JS)
        self.assertIn('state.dialogStack = []', JS)

    def test_dialog_backdrop_receives_pointer_events_outside_panel(self):
        self.assertIn('fixed inset-0 overflow-y-auto pointer-events-none', HTML)
        panel_start = HTML.index('id="simc-workbench-dialog-content"')
        panel_end = HTML.index('>', panel_start)
        self.assertIn('simc-workbench-dialog__panel', HTML[panel_start:panel_end])
        self.assertIn('pointer-events-auto', HTML[panel_start:panel_end])

    def test_repeated_resource_loads_have_abort_or_sequence_guard(self):
        for token in ('beginResourceRequest(\'templates\')', "beginResourceRequest('apl')", "beginResourceRequest('backend')"):
            self.assertIn(token, JS)
        self.assertIn('resourceAbortControllers', JS)
        self.assertIn('resourceRequestSerials', JS)

    def test_profile_detail_and_form_use_dialog_not_bottom_slots(self):
        detail_start = MAIN.index('function simcWbShowProfileDetail')
        detail_end = MAIN.index('function bindSimcWorkbenchProfilesControls', detail_start)
        self.assertIn('openSimcWorkbenchDialog(', MAIN[detail_start:detail_end])
        self.assertNotIn('id="simc-wb-profile-detail"', HTML)
        self.assertNotIn('id="simc-wb-profile-form"', HTML)
        self.assertIn("openSimcWorkbenchDialog('profile-form'", MAIN)

    def test_template_and_apl_view_edit_use_dialog_not_bottom_slots(self):
        self.assertIn("openSimcWorkbenchDialog('template-detail'", JS)
        self.assertIn("openSimcWorkbenchDialog('template-form'", JS)
        self.assertIn("openSimcWorkbenchDialog('apl-form'", JS)
        for slot_id in (
            'simc-wb-template-detail', 'simc-wb-template-form',
            'simc-wb-apl-storage-form',
        ):
            self.assertNotIn(f'id="{slot_id}"', HTML)

    def test_task_dialog_renders_real_dps_and_report_artifact(self):
        start = JS.index('async function showTaskDetail')
        end = JS.index('\n    async function', start + 20)
        body = JS[start:end]
        self.assertIn("openSimcWorkbenchDialog('task-detail'", body)
        self.assertIn('result_summary', body)
        self.assertIn('.dps', body)
        self.assertIn('artifacts', body)
        self.assertIn('renderSimcArtifactFrame', body)
        self.assertNotIn('/simc-result/', body)

    def test_batch_dialog_renders_member_dps_and_delta_without_navigation(self):
        start = JS.index('async function showBatchComparison')
        end = JS.index('\n    async function', start + 20)
        body = JS[start:end]
        self.assertIn("openSimcWorkbenchDialog('batch-detail'", body)
        self.assertIn('.dps', body)
        self.assertIn('delta', body)
        self.assertNotIn('/simc-compare/', body)

    def test_workbench_does_not_use_external_or_native_dialogs(self):
        combined = SIMC_HTML + JS + SIMC_MAIN
        for token in ('window.open(', 'target="_blank"', 'alert(', 'prompt(', 'confirm(', 'onclick='):
            self.assertNotIn(token.lower(), combined.lower())

    def test_workflow_resources_are_reachable_and_keywords_stay_in_advanced(self):
        for resource in ('profiles', 'templates', 'apl'):
            self.assertIn(f'data-simc-workflow-entry="{resource}"', HTML)
        self.assertIn('id="simc-workbench-apl-keywords-panel" data-simc-panel="apl-keywords"', HTML)
        self.assertIn('data-simc-tab="apl-keywords"', HTML)
        workflow = self._l1_section('workflow', '<!-- End L1 Panel: 模拟工作流 -->')
        advanced = self._l1_section('advanced', '<!-- End L1 Panel: 高级设置 -->')
        self.assertNotIn('simc-wb-apl-keyword-list', workflow)
        self.assertIn('simc-wb-apl-keyword-list', advanced)
        self.assertIn("profiles: 'workflow'", MAIN)
        self.assertIn("'apl-keywords': 'advanced'", MAIN)

    def test_dialog_close_cancels_all_detail_requests(self):
        self.assertIn("new CustomEvent('simc-dialog-closing', { detail: { reason: 'close' } })", MAIN)
        self.assertIn("document.addEventListener('simc-dialog-closing'", JS)
        self.assertIn('simcWbCancelProfileDetail()', MAIN)
