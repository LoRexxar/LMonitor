import unittest
from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "templates/dashboard/index.html").read_text(encoding="utf-8")
JS = (ROOT / "static/dashboard/js/simc-workbench.js").read_text(encoding="utf-8")
MAIN = (ROOT / "static/dashboard/js/main.js").read_text(encoding="utf-8")
DETAIL_JS = (ROOT / "static/dashboard/js/simc-detail.js").read_text(encoding="utf-8")
APL_EDITOR_JS = (ROOT / "static/dashboard/js/simc-apl-editor.js").read_text(encoding="utf-8")
APL_EDITOR_CSS = (ROOT / "static/dashboard/css/simc-apl-editor.css").read_text(encoding="utf-8")

# Scope safety assertions to the complete SimC surfaces. The dashboard template
# and main.js also contain unrelated legacy modules with their own navigation UI.
SIMC_HTML = (
    HTML[HTML.index('<!-- SimC pages share one behavior root'):HTML.index('<!-- Tools内容区域 -->')]
    + HTML[HTML.index('<!-- SimC Workbench Unified Dialog -->'):]
)
SIMC_MAIN = MAIN[
    MAIN.index('/* ===== SimC Workbench Dialog ===== */'):
    MAIN.index('// 全局表格变量')
]


class SimcWorkbenchFrontendContractTests(unittest.TestCase):
    def test_local_worker_and_each_agent_have_independent_dispatch_switches(self):
        self.assertIn('data-local-worker-enabled', JS)
        self.assertIn("action: 'set_local_worker_enabled'", JS)
        self.assertIn('data-agent-accepting-toggle', JS)
        self.assertIn("JSON.stringify({ is_active: enabled })", JS)
        self.assertIn('data-agent-task-scope', JS)
        self.assertIn("task-scope/`", JS)

    def test_apl_assistant_follows_dialog_scroll_and_fills_visible_height(self):
        desktop_css = APL_EDITOR_CSS[:APL_EDITOR_CSS.index("@media (max-width: 900px)")]
        apl_form = JS[JS.index('<form data-apl-storage-form'):JS.index('setAplDialogLayout(true);')]
        self.assertIn('simc-editor-form simc-apl-editor-form', apl_form)
        self.assertIn('.simc-apl-editor-form {', desktop_css)
        self.assertIn('grid-template-columns: minmax(0, 1fr) minmax(20rem, 24rem)', desktop_css)
        self.assertIn('catalogPageSizeForHeight', APL_EDITOR_JS)
        self.assertIn('new ResizeObserver', APL_EDITOR_JS)
        self.assertIn('.simc-apl-editor-form > .simc-apl-assistant', desktop_css)
        self.assertIn("position: sticky", desktop_css)
        self.assertIn("top: 4.75rem", desktop_css)
        self.assertIn("height: calc(90dvh - 5.75rem)", desktop_css)
        self.assertIn("align-self: start", desktop_css)
        self.assertIn(".simc-apl-assistant > div { display: flex; height: 100%;", desktop_css)
        self.assertIn(".simc-apl-catalog { flex: 1; min-height: 0; overflow: auto;", desktop_css)
        self.assertIn(".simc-apl-catalog__pager", desktop_css)

    def test_new_frontend_uses_task_mode_vocabulary_only(self):
        self.assertNotIn('task_type:', SIMC_MAIN)
        self.assertNotIn('任务组', SIMC_MAIN)
        self.assertNotIn('基于 Batch', SIMC_MAIN)

    def test_dashboard_shell_contains_only_layout_and_shared_dialogs_are_body_level(self):
        soup = BeautifulSoup(HTML, "html.parser")
        shell = soup.select_one("body > .dashboard-shell")
        self.assertIsNotNone(shell)
        self.assertIsNotNone(shell.select_one(":scope > #sidebar"))
        self.assertIsNotNone(shell.select_one(":scope > .main-content"))
        self.assertIsNone(shell.find("footer"), "footer must not consume horizontal dashboard-shell space")
        for dialog_id in ("add-record-modal", "edit-record-modal", "simc-workbench-dialog"):
            dialog = soup.find(id=dialog_id)
            self.assertIsNotNone(dialog)
            self.assertIs(dialog.parent, soup.body, f"{dialog_id} must be a body-level overlay")

    def test_home_creation_flow_is_spec_driven_and_single_submit(self):
        workflow = HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
        source_panel = workflow[workflow.index('id="simc-sim-player-sources"'):workflow.index('id="simc-sim-apl-list"')]
        # Exactly three peer source entries; only specified_spec owns the explicit spec/Profile controls.
        self.assertEqual(source_panel.count('data-simc-player-source='), 3)
        for source in ('battlenet', 'simc_addon', 'specified_spec'):
            self.assertEqual(source_panel.count(f'data-simc-player-source="{source}"'), 1)
        self.assertNotIn('id="simc-sim-spec"', workflow[:workflow.index('id="simc-sim-player-sources"')])
        specified_panel = source_panel[source_panel.index('id="simc-sim-source-specified-spec"'):]
        self.assertEqual(specified_panel.count('id="simc-sim-spec"'), 1)
        self.assertIn('id="simc-sim-profile-select"', specified_panel)
        profile_select = specified_panel[specified_panel.index('id="simc-sim-profile-select"'):specified_panel.index('</select>', specified_panel.index('id="simc-sim-profile-select"'))]
        self.assertIn('value="default" selected', profile_select)
        self.assertIn('系统默认配置', profile_select)
        self.assertLess(workflow.index('id="simc-sim-apl-list"'), workflow.index('id="simc-sim-fight-style"'))
        self.assertIn('id="simc-sim-mode"', workflow)
        self.assertIn('value="normal"', workflow)
        self.assertIn('value="attribute"', workflow)
        self.assertIn('value="comparison"', workflow)
        self.assertEqual(workflow.count('id="simc-sim-submit-btn"'), 1)
        self.assertNotIn('id="simc-sim-attribute-optimize-btn"', workflow)
        self.assertNotIn('id="simc-sim-apl-candidates-btn"', workflow)
        self.assertNotIn('id="simc-sim-saved-profiles"', workflow)
        self.assertNotIn('id="base-template-select"', workflow)
        self.assertNotIn('引用型输入', workflow)
        self.assertNotIn('提交时即时来源会原子固化为 Profile 不可变版本', workflow)
        self.assertNotIn('id="simc-sim-attribute-search-status"', workflow)

    def test_home_apl_uses_the_same_dropdown_pattern_as_other_resources(self):
        workflow = BeautifulSoup(
            HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')],
            "html.parser",
        )
        apl_select = workflow.select_one("select#simc-sim-apl-list")
        self.assertIsNotNone(apl_select)
        self.assertIn("w-full", apl_select.get("class", []))
        loader = MAIN[MAIN.index("async function loadSimcAplCandidates("):MAIN.index("async function simcWbFetchProfilesForWorkbench(")]
        self.assertIn("<option value=", loader)
        self.assertNotIn('type="radio"', loader)
        self.assertIn("selectedSimcReferenceValue('#simc-sim-apl-list')", MAIN)

    def test_regular_simulation_fight_styles_use_the_shared_localized_catalog(self):
        workflow = HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
        select = BeautifulSoup(workflow, "html.parser").select_one('#simc-sim-fight-style')
        self.assertIsNotNone(select)
        self.assertEqual([(option.get('value'), option.get_text(strip=True)) for option in select.select('option')], [
            ('', '正在加载战斗模型…'),
        ])
        self.assertIn("fetch('/api/simc-fight-styles/options/')", SIMC_MAIN)
        self.assertIn('loadSimcFightStyleOptions()', SIMC_MAIN)

    def test_combat_advanced_raid_buffs_use_server_catalog_and_explicit_three_state_payload(self):
        workflow = HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
        self.assertIn('id="simc-sim-combat-advanced"', workflow)
        self.assertIn('id="simc-sim-raid-buffs"', workflow)
        self.assertIn("fetch('/api/simc-raid-buffs/options/')", SIMC_MAIN)
        self.assertIn('renderSimcRaidBuffOptions', SIMC_MAIN)
        self.assertIn('applyImplicitSimcRaidBuffDefaults', SIMC_MAIN)
        self.assertIn('option.default_classes', SIMC_MAIN)
        self.assertIn('dataset.raidBuffExplicit', SIMC_MAIN)
        self.assertIn('scenario.raid_buffs', SIMC_MAIN)
        self.assertIn('delete scenario.raid_buffs', SIMC_MAIN)
        self.assertIn('indeterminate', SIMC_MAIN)
        self.assertNotIn('const SIMC_RAID_BUFF', SIMC_MAIN)

    def test_profile_overrides_use_selects_for_consumables_but_keep_talents_as_text(self):
        workflow = HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
        for key in ('flask', 'potion', 'food', 'augmentation'):
            self.assertIn(f'data-simc-profile-override="{key}"', workflow)
            self.assertIn(f'<select data-simc-profile-override="{key}"', workflow)
        self.assertIn('data-simc-profile-override="temporary_enchant_main_hand"', workflow)
        self.assertIn('data-simc-profile-override="temporary_enchant_off_hand"', workflow)
        self.assertIn('loadSimcConsumableOptions', SIMC_MAIN)
        self.assertIn("/api/simc-profile/consumable-options/", SIMC_MAIN)
        self.assertIn('temporary_enchant_main_hand', SIMC_MAIN)
        self.assertIn('temporary_enchant_off_hand', SIMC_MAIN)
        self.assertIn('temporary_enchant', SIMC_MAIN)
        self.assertIn('<input data-simc-profile-override="talents"', workflow)

    def test_combat_raid_buffs_offer_class_buff_toggle_plus_extra_selection(self):
        workflow = HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
        self.assertIn('id="simc-sim-use-class-raid-buff"', workflow)
        self.assertIn('自动启用当前职业自身团队增益', workflow)
        self.assertIn('scenario.use_class_raid_buff', SIMC_MAIN)
        self.assertIn('额外团队增益', workflow)

    def test_four_piece_override_is_beside_class_buff_toggle_and_keeps_extra_options_payload(self):
        workflow = BeautifulSoup(
            HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')],
            "html.parser",
        )
        class_toggle = workflow.select_one('#simc-sim-use-class-raid-buff')
        four_piece_host = workflow.select_one('#simc-sim-force-current-tier-4pc')
        self.assertIsNotNone(class_toggle)
        self.assertIsNotNone(four_piece_host)
        self.assertIs(class_toggle.find_parent('div'), four_piece_host.parent)
        self.assertIn("option.value === 'force_current_tier_4pc'", SIMC_MAIN)
        self.assertIn('data-simc-extra-option', SIMC_MAIN)
        self.assertIn('[data-simc-extra-option]:checked', SIMC_MAIN)

    def test_system_default_profile_is_selected_as_a_real_profile_and_renders_detail(self):
        loader = MAIN[
            MAIN.index('async function loadSimcSimProfileSelect'):
            MAIN.index('async function resolveSimcPlayerSource')
        ]
        self.assertIn("profile.is_system === true", loader)
        self.assertIn("select.value = String(defaultSystemProfile.id)", loader)
        self.assertIn("await onSimcProfileSelect()", loader)
        self.assertNotIn("select.innerHTML = '<option value=\"default\">系统默认配置</option>'", loader)

    def test_profile_list_has_view_detail_action_and_renders_equipment_and_raw_block(self):
        loader = MAIN[MAIN.index('function loadSimcWorkbenchProfiles'):MAIN.index('function bindSimcWorkbenchProfilesControls')]
        self.assertIn('data-profile-row-action="view"', loader)
        self.assertIn('data-profile-row-action="edit"', loader)
        self.assertIn('data-profile-row-action="copy"', loader)
        self.assertIn('equipment_line_count', loader)
        self.assertIn('simcWbViewProfile', MAIN)
        self.assertIn('simcWbCopyProfile', MAIN)
        self.assertIn('renderSimcProfileDetailDialog', MAIN)
        self.assertIn('raw_player_equipment', MAIN)

    def test_profile_management_links_to_gear_builder(self):
        profile_panel = BeautifulSoup(
            HTML[HTML.index('id="simc-workbench-profiles-panel"'):HTML.index('id="simc-workbench-talent-strings-panel"')],
            "html.parser",
        )
        link = profile_panel.select_one('#simc-wb-open-gear-builder')
        self.assertIsNotNone(link)
        self.assertEqual(link.get('href'), "{% url 'portal_gear_builder' %}")
        self.assertEqual(link.get('target'), '_blank')
        self.assertIn('职业配装器', link.get_text())

    def test_profile_view_is_read_only_and_edit_form_shows_structured_equipment(self):
        detail_start = MAIN.index('function renderSimcProfileDetailDialog')
        detail_end = MAIN.index('async function simcWbViewProfile', detail_start)
        detail_renderer = MAIN[detail_start:detail_end]
        self.assertIn('renderSimcProfileEquipmentCards', detail_renderer)
        self.assertIn('detail.consumables', detail_renderer)
        self.assertIn('detail.talent_strings', detail_renderer)
        self.assertIn('detail.omnium_talents', detail_renderer)
        self.assertIn('renderSimcOmniumTalents', detail_renderer)
        self.assertIn('当前 Profile 未解析到', detail_renderer)
        self.assertIn('消耗品与临时附魔', detail_renderer)
        self.assertIn('天赋字符串拆解', detail_renderer)
        self.assertIn('万奥宝典', detail_renderer)
        self.assertNotIn('data-profile-equipment-slot', detail_renderer)
        self.assertNotIn('simcWbSaveProfileEquipment', detail_renderer)
        self.assertNotIn('保存装备修改', detail_renderer)

        profile_form = HTML[
            HTML.index('id="simc-wb-profile-form-source"'):
            HTML.index('id="simc-wb-profile-list"')
        ]
        self.assertIn('data-profile-equipment-preview', profile_form)

        edit_start = MAIN.index('async function simcWbEditProfile')
        edit_end = MAIN.index('async function simcWbSaveCurrentSimulatorProfile', edit_start)
        edit_flow = MAIN[edit_start:edit_end]
        self.assertIn('/api/simc-player-config-detail/?profile_id=', edit_flow)
        self.assertIn('renderSimcProfileFormEquipmentPreview', edit_flow)

        preview_start = MAIN.index('function renderSimcProfileFormEquipmentPreview')
        preview_end = MAIN.index('function renderSimcProfileDetailDialog', preview_start)
        preview_renderer = MAIN[preview_start:preview_end]
        self.assertIn('detail?.omnium_talents', preview_renderer)
        self.assertIn('data-profile-omnium-talents', preview_renderer)
        self.assertIn('当前 Profile 未解析到', preview_renderer)

    def test_profile_equipment_enchant_uses_enchantment_id_and_readable_name(self):
        renderer_start = MAIN.index('function renderSimcProfileEquipmentCards')
        renderer_end = MAIN.index('function renderSimcOmniumTalents', renderer_start)
        renderer = MAIN[renderer_start:renderer_end]
        self.assertIn('item.enchant?.enchantment_id', renderer)
        self.assertIn('item.enchant?.display_name', renderer)
        self.assertIn('附魔 #${enchantId}', renderer)
        self.assertIn("dashboard/js/main.js' %}?v=20260901b_simc_profile_omnium_enchant", HTML)

    def test_profile_list_renders_spec_icon_with_authoritative_class_color(self):
        badge = MAIN[MAIN.index('function renderSpecBadgeHtml'):MAIN.index('function syncSimcTaskInputMode')]
        loader = MAIN[MAIN.index('function loadSimcWorkbenchProfiles'):MAIN.index('function bindSimcWorkbenchProfilesControls')]
        self.assertIn('spec_icon_url', loader)
        self.assertIn('class_color', loader)
        self.assertIn('<img', badge)
        self.assertIn('--simc-class-color', badge)

    def test_profile_filter_uses_backend_canonical_identity_without_client_aliases(self):
        matcher = MAIN[
            MAIN.index('function simcProfileMatchesSpecFilter'):
            MAIN.index('function loadSimcWorkbenchProfiles')
        ]
        options = MAIN[
            MAIN.index('async function loadSimcSpecOptions'):
            MAIN.index('function bindSimcWorkbenchProfilesControls')
        ]
        self.assertIn('row.canonical_spec', matcher)
        self.assertIn('option.value = row.value', options)
        self.assertNotIn('disambiguatedSpecs', matcher)
        self.assertNotIn('simcProfileSpecFilterValue', MAIN)

    def test_all_profile_spec_selects_use_the_authoritative_catalog(self):
        """发起模拟、Top10、配置表单和筛选器必须共享后端职业专精目录。"""
        source_panel = HTML[
            HTML.index('id="simc-sim-player-sources"'):
            HTML.index('id="simc-sim-apl-list"')
        ]
        profile_form = HTML[
            HTML.index('id="simc-wb-profile-form-source"'):
            HTML.index('id="simc-wb-profile-list"')
        ]
        loader = MAIN[
            MAIN.index('async function loadSimcSpecOptions'):
            MAIN.index('function bindSimcWorkbenchProfilesControls')
        ]

        self.assertNotIn('value="demonhunter_devourer"', source_panel)
        self.assertNotIn('value="devourer"', profile_form)
        self.assertEqual(loader.count("fetch('/api/simc-spec-options/'"), 1)
        for selector_id in (
            'simc-sim-spec',
            'simc-sim-bnet-spec',
            'simc-wb-profile-spec-filter',
            'simc-profile-spec-filter',
        ):
            self.assertIn(selector_id, loader)
        self.assertIn("select[name=\"spec\"]", loader)
        self.assertIn("#simc-wb-mastery-form select[name=\"spec\"]", loader)
        self.assertIn('option.value = row.value', loader)
        self.assertIn("row.label || `${row.spec_label} · ${row.class_label}`", loader)
        mastery_form = HTML[HTML.index('id="simc-wb-mastery-form"'):HTML.index('id="simc-wb-mastery-list"')]
        self.assertIn('<select name="spec"', mastery_form)
        self.assertNotIn('<input name="spec"', mastery_form)
        mastery_editor = MAIN[
            MAIN.index('async function simcWbToggleMasteryForm'):
            MAIN.index('async function simcWbDeleteMastery')
        ]
        self.assertIn("specSelect.value = specOptions.some(row => row.value === data.spec) ? data.spec : '';", mastery_editor)
        self.assertIn("const spec = formWrap.querySelector('select[name=\"spec\"]')", mastery_editor)
        self.assertNotIn('row.spec === data.spec', mastery_editor)
        self.assertNotIn('selectedSpec.spec', mastery_editor)

    def test_profile_form_normalizes_legacy_spec_and_leaves_optional_attribute_overrides_blank(self):
        """编辑旧 class_spec 记录必须选中实际专精；未填写属性不得伪造覆盖值。"""
        form = MAIN[MAIN.index('function simcWbToggleProfileForm'):MAIN.index('function simcWbCloseProfileForm')]
        save = MAIN[MAIN.index('async function simcWbSaveProfile()'):MAIN.index('async function simcWbDeleteProfile')]
        self.assertIn('profileData.canonical_spec || simcProfileFormCanonicalSpec', form)
        self.assertIn('await loadSimcSpecOptions()', form)
        self.assertIn('specSel.value = profileSpec;', form)
        self.assertIn("profileData.gear_strength ?? ''", form)
        self.assertIn("profileData.gear_crit ?? ''", form)
        self.assertIn("profileData.gear_haste ?? ''", form)
        self.assertIn("profileData.gear_mastery ?? ''", form)
        self.assertIn("profileData.gear_versatility ?? ''", form)
        self.assertNotIn("if (payload.player_config_mode === 'attribute_only')", save)
        self.assertIn("gear_strength: gv('gear_strength')", save)
        self.assertIn("gear_strength: gv('gear_strength') === '' ? null : parseInt(gv('gear_strength'))", save)
        self.assertNotIn('simcWbAttributeOnlyConfig', MAIN)
        self.assertIn("cellText === null || cellText === undefined || cellText === '' ? '-' : cellText", MAIN)
        self.assertIn('name="gear_strength" type="number"', HTML)
        self.assertIn('placeholder="未填写则不覆盖"', HTML)
        self.assertNotIn('name="gear_strength" type="number" value=', HTML)

    def test_profile_list_resolves_saved_profile_source_without_refreshing_detail(self):
        resolver = MAIN[
            MAIN.index('async function resolveSimcPlayerSource'):
            MAIN.index('async function onSimcTargetSpecChange')
        ]
        self.assertIn('await loadSimcSimProfileSelect', resolver)
        self.assertNotIn('refreshSavedSimcPlayerDetail', resolver)

    def test_home_creation_flow_requires_and_defaults_an_execution_backend(self):
        workflow = HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
        self.assertEqual(workflow.count('id="simc-sim-backend"'), 1)
        self.assertIn("fetch('/api/simc-backend-binary/')", SIMC_MAIN)
        self.assertIn("backend.is_default ? 'selected' : ''", SIMC_MAIN)
        self.assertIn("selectedSimcReferenceValue('#simc-sim-backend')", SIMC_MAIN)
        self.assertIn("if (!backend_id) throw new Error('请选择 SimC 后端')", SIMC_MAIN)
        self.assertIn('const references = { base_template_id, selected_apl_id, backend_id,', SIMC_MAIN)
        self.assertIn('selected_apl_id, backend_id, candidates, include_base', SIMC_MAIN)
        self.assertIn('backend_id: references.backend_id', SIMC_MAIN)
        self.assertIn('loadSimcBackendOptions().catch', SIMC_MAIN)

    def test_home_creation_flow_uses_backend_defaults_filters_profiles_and_opens_history(self):
        workflow = HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
        self.assertIn('profile.canonical_spec', MAIN)
        self.assertIn("String(profile.canonical_spec || '')", MAIN)
        self.assertNotIn('normalizeSimcSpecKey(profile.spec) === normalizedSpec', MAIN)
        self.assertIn('row.is_default === true', MAIN)
        self.assertNotIn("${index === 0 ? 'checked' : ''}", MAIN)
        self.assertIn('payload.default_template_id', MAIN)
        self.assertIn("switchSimcWorkbenchL1Tab('history')", MAIN)
        self.assertIn('submitSimcHomeCreation', MAIN)
        self.assertIn("mode === 'normal'", MAIN)
        self.assertIn("mode === 'attribute'", MAIN)
        self.assertIn("mode === 'comparison'", MAIN)
        self.assertIn("['simc-sim-submit-btn', submitSimcHomeCreation]", MAIN)
        self.assertIn("spec.addEventListener('change'", MAIN)
        self.assertIn('player_source', MAIN)
        self.assertIn("type: 'saved_profile'", MAIN)
        self.assertIn("return { type: 'saved_profile', profile_id }", MAIN)
        self.assertIn("type: 'default'", MAIN)
        self.assertIn("type: 'battlenet'", MAIN)
        self.assertIn("type: 'simc_addon'", MAIN)
        attribute_body = MAIN.split('function simcAttributeSearchRequestBody()', 1)[1].split('async function submitSimcAttributeSearch', 1)[0]
        self.assertIn('...references', attribute_body)
        self.assertIn("spec: simcResolvedCanonicalSpec", attribute_body)
        self.assertIn("references.player_source?.type === 'default'", attribute_body)
        self.assertNotIn("throw new Error('请选择已有 Profile')", MAIN)
        self.assertEqual(workflow.count('id="simc-sim-player-detail-refresh-btn"'), 1)
        self.assertNotIn('simc-comparison-submit', MAIN)
        self.assertNotIn("batches: 'history'", SIMC_MAIN)

    def test_history_uses_one_task_list_without_batch_classification(self):
        history_start = HTML.index('data-simc-l1-panel="history"')
        history_end = HTML.index('<!-- End L1 Panel: 历史任务 -->')
        history = HTML[history_start:history_end]
        self.assertIn('>任务列表<', history)
        self.assertNotIn('data-task-subtab=', history)
        self.assertNotIn('data-task-subtab="comparison"', history)
        self.assertIn("row_type === 'benchmark_execution'", JS)
        self.assertIn('data-benchmark-task-toggle', JS)
        self.assertIn('查看独立任务状态', JS)
        self.assertNotIn('syncTaskSubtabs', JS)
        self.assertIn("data.ruleSubtab", MAIN)
        self.assertIn("switchRuleSubtab(model)", MAIN)

    def test_history_task_cards_use_status_badges_and_real_action_buttons(self):
        load_start = JS.index('async function loadTasks')
        load_end = JS.index('function scheduleTaskRefresh', load_start)
        load_tasks = JS[load_start:load_end]
        self.assertIn('simc-task-card', load_tasks)
        self.assertIn('simc-task-status', load_tasks)
        self.assertIn('simc-task-primary-action', load_tasks)
        self.assertIn('simc-task-secondary-action', load_tasks)
        self.assertIn('<i class="fas fa-chart-line', load_tasks)
        self.assertIn('<i class="fas fa-redo-alt', load_tasks)

    def test_all_task_states_can_open_frozen_copy_rerun_dialog(self):
        load_start = JS.index('async function loadTasks')
        load_end = JS.index('function scheduleTaskRefresh', load_start)
        load_tasks = JS[load_start:load_end]
        self.assertNotIn("[2, 3].includes(status)", load_tasks)
        self.assertIn('data-task-rerun=', load_tasks)
        self.assertNotIn('data-wb-action="rerun"', load_tasks)
        self.assertIn('renderTaskRerunForm(rerunAction.dataset.taskRerun)', JS)


    def test_template_editor_only_submits_content(self):
        form_start = JS.index("function renderTemplateForm")
        form_end = JS.index("function closeTemplateForm", form_start)
        form_body = JS[form_start:form_end]
        self.assertNotIn("default_player", form_body)
        self.assertNotIn("payload.template_type", JS)
        self.assertIn("!readOnly", JS)
        self.assertIn("系统内置", JS)
        self.assertIn("上游同步", JS)
        for field in ('name="name"', 'name="template_type"', 'name="spec"', 'name="class_name"'):
            self.assertNotIn(field, form_body)
        self.assertIn("content: String(formData.get('content') || '')", JS)
        self.assertIn("method: 'PUT'", JS)
        self.assertNotIn("report_template", form_body)
        self.assertNotIn("command_fragment", form_body)
        template_panel = HTML[HTML.index('id="simc-workbench-templates-panel"'):HTML.index('id="simc-workbench-apl-panel"')]
        self.assertNotIn("can_write", template_panel)

    def test_content_templates_use_single_structured_table(self):
        template_panel = HTML[HTML.index('id="simc-workbench-templates-panel"'):HTML.index('id="simc-workbench-apl-panel"')]
        load_start = JS.index('async function loadTemplates')
        load_end = JS.index('function renderTemplateForm', load_start)
        load_body = JS[load_start:load_end]
        self.assertIn('基础模板', template_panel)
        self.assertNotIn('data-template-type=', template_panel)
        self.assertNotIn('simc-template-filter', template_panel)
        self.assertIn('simc-template-table-wrap', load_body)
        self.assertIn('simc-template-table', load_body)
        for heading in ('模板名称', '类型', '职业', '专精', '来源', '状态', '操作'):
            self.assertIn(heading, load_body)
        self.assertNotIn('simc-template-card', load_body)
        self.assertIn('data-wb-action="template-edit"', load_body)
        self.assertNotIn('data-inline-create="templates"', template_panel)
        self.assertIn('data-template-filter-summary', template_panel)

    def test_apl_import_uses_external_select_and_explicit_load_button(self):
        start = JS.index('function renderAplStorageForm')
        end = JS.index('function closeAplStorageForm', start)
        form = JS[start:end]
        picker = form.index('data-apl-import-picker')
        editor_section = form.index('<h5 class="text-sm font-bold text-slate-900">APL 内容</h5>')
        self.assertLess(picker, editor_section)
        self.assertIn('data-apl-import-select', form)
        self.assertIn('data-apl-import-load', form)
        self.assertIn("importButton?.addEventListener('click'", form)
        self.assertNotIn("importSelect?.addEventListener('change', async", form)
        self.assertNotIn('data-apl-default-choice', form)
        self.assertNotIn("titleInput.value =", form)

    def test_content_template_editor_and_detail_use_code_workspace(self):
        form_start = JS.index('function renderTemplateForm')
        form_end = JS.index('function closeTemplateForm', form_start)
        form_body = JS[form_start:form_end]
        detail_start = JS.index('async function showTemplateDetail')
        detail_end = JS.index('async function showMyAplDetail', detail_start)
        detail_body = JS[detail_start:detail_end]
        for token in ('simc-editor-form', 'simc-editor-section', 'simc-code-editor', 'data-code-editor-stats', '保存内容模板'):
            self.assertIn(token, form_body)
        self.assertIn('template-code-preview', detail_body)
        self.assertIn('simc-template-detail-meta', detail_body)


    def test_profiles_offer_edit_and_delete_without_view_action(self):
        self.assertNotIn('data-profile-row-action="detail"', MAIN)
        self.assertNotIn("simcWbShowProfileDetail", MAIN)
        self.assertNotIn("'profile-detail': '配置详情'", MAIN)
        self.assertIn('data-profile-row-action="edit"', MAIN)
        self.assertIn('data-profile-row-action="delete"', MAIN)

    def test_task_dialog_links_artifacts_as_standalone_pages(self):
        self.assertNotIn('id="simc-workbench-artifacts-panel"', HTML)
        self.assertNotIn('data-artifact-filter="task_id"', HTML)
        self.assertNotIn('data-artifact-filter="artifact_type"', HTML)
        start = JS.index('async function showTaskDetail')
        end = JS.index('\n    async function', start + 20)
        detail = JS[start:end]
        self.assertIn('row.artifacts', detail)
        self.assertIn('href="${esc(artifact.preview_url)}"', detail)
        self.assertNotIn('data-artifact-preview', detail)
        self.assertNotIn('data-artifact-preview-action=', JS)

    def test_task_detail_renders_structured_report_summary(self):
        start = JS.index('async function showTaskDetail')
        end = JS.index('\n    async function', start + 20)
        detail = JS[start:end]
        for token in (
            'row.report_summary', 'report?.character', 'report?.simulation',
            'report?.top_abilities', '原生报告', '主要技能',
        ):
            self.assertIn(token, detail)

    def test_failed_task_detail_explains_missing_native_report(self):
        start = JS.index('async function showTaskDetail')
        end = JS.index('\n    async function', start + 20)
        detail = JS[start:end]
        self.assertIn('本次失败未生成原生报告', detail)
        self.assertIn('run.error_summary', detail)
        self.assertIn('reportArtifact?.can_preview === true', detail)
        self.assertIn('run.error_summary', DETAIL_JS)
        self.assertIn('本次失败未生成原生报告', DETAIL_JS)
        self.assertIn('模拟执行失败', DETAIL_JS)
        self.assertIn('simc-error-tooltip__content', detail)
        self.assertIn('simc-error-tooltip__content', DETAIL_JS)
        self.assertIn('aria-label="查看失败详情"', DETAIL_JS)
        self.assertNotIn('<th>失败详情</th>', DETAIL_JS)
        self.assertNotIn('错误摘要：${esc(errorSummary)}', detail)

    def test_standalone_task_detail_renders_sample_skill_sequence(self):
        for token in (
            'report.sample_sequence', '技能施放序列', 'action_list',
            'item.resources', 'item.buffs',
        ):
            self.assertIn(token, DETAIL_JS)


    def test_profile_list_ignores_aborted_and_stale_responses(self):
        start = MAIN.index("function loadSimcWorkbenchProfiles")
        end = MAIN.index("function bindSimcWorkbenchProfilesControls", start)
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
        for function_name in ("showTaskDetail", "showTaskComparison", "showTemplateDetail", "showManagedAplDetail"):
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
        for group in ("模拟工作流", "历史任务", "高级配置", "执行后端"):
            self.assertIn(group, HTML)
        workflow = HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
        self.assertNotIn('<details', workflow)
        self.assertNotIn('p-5 h-full', workflow)

    def test_advanced_only_has_system_capabilities(self):
        advanced_start = HTML.index('data-simc-l1-panel="advanced"')
        advanced_end = HTML.index('<!-- End L1 Panel: 高级设置 -->')
        advanced = HTML[advanced_start:advanced_end]
        self.assertIn('aria-label="SimC 系统模型入口"', advanced)
        for resource in ("secondary-rules", "mastery-rules", "backend"):
            self.assertIn(f'data-simc-model="{resource}"', advanced)
        for resource in ("batches", "tasks", "artifacts", "profiles", "apl-storage"):
            self.assertNotIn(f'data-simc-model="{resource}"', advanced)

    def test_advanced_capabilities_use_same_tab_navigation_as_workflow(self):
        advanced_start = HTML.index('data-simc-l1-panel="advanced"')
        advanced_end = HTML.index('<!-- End L1 Panel: 高级设置 -->')
        advanced = HTML[advanced_start:advanced_end]
        self.assertIn('<nav class="mb-4 flex flex-wrap gap-2" aria-label="SimC 系统模型入口">', advanced)
        self.assertNotIn('simc-compact-panel', advanced)
        for resource in ("backend", "secondary-rules", "mastery-rules"):
            self.assertIn(f'data-simc-model="{resource}"', advanced)
        self.assertEqual(advanced.count('data-rule-subtab="secondary-rules"'), 1)
        self.assertEqual(advanced.count('data-rule-subtab="mastery-rules"'), 1)
        self.assertNotIn('aria-label="规则类型"', advanced)
        self.assertIn('updateSimcAdvancedEntryState(activeL1Tab, activeChildPanel, activeRuleSubtab)', MAIN)

    def test_simc_pages_are_independent_sections_under_one_behavior_root(self):
        self.assertEqual(HTML.count('data-simc-l1-panel="workflow"'), 1)
        self.assertEqual(HTML.count('data-simc-l1-panel="history"'), 1)
        self.assertEqual(HTML.count('data-simc-l1-panel="advanced"'), 1)
        soup = BeautifulSoup(HTML, 'html.parser')
        root = soup.select_one('#simc-workbench')
        self.assertIsNotNone(root)
        self.assertEqual(
            [node.get('id') for node in soup.select('#simc-workbench > .content-section[data-simc-page]')],
            ['simc-workflow', 'simc-history', 'simc-advanced'],
        )

    def test_sidebar_owns_primary_navigation_while_panels_keep_resource_mapping(self):
        self.assertNotIn('data-simc-l1-tab=', HTML)
        self.assertIn('data-dashboard-section="simc-workflow"', HTML)
        self.assertIn('data-dashboard-section="simc-history"', HTML)
        self.assertIn('data-dashboard-section="simc-advanced"', HTML)
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

    def test_history_panel_has_one_unified_task_list(self):
        self.assertIn('data-simc-panel="tasks"', HTML)
        self.assertNotIn('data-task-subtab=', HTML)
        self.assertIn("window.simcWorkbenchLoadPanel = activate", JS)

    def test_history_polling_is_cancelled_and_stale_responses_are_ignored(self):
        self.assertIn("window.simcWorkbenchDeactivatePanel = deactivate", JS)
        self.assertIn("scheduleTaskRefresh(false)", JS)
        self.assertIn("state.taskRequestSerial += 1", JS)
        self.assertIn("requestSerial !== state.taskRequestSerial || state.activePanel !== 'tasks'", JS)
        self.assertIn("page !== state.taskPage", JS)
        self.assertIn("window.simcWorkbenchDeactivatePanel(activeChildPanel)", MAIN)

    def test_history_fetch_is_aborted_on_deactivation(self):
        self.assertIn("taskAbortController: null", JS)
        self.assertIn("const controller = new AbortController()", JS)
        self.assertIn("{ signal: controller.signal }", JS)
        self.assertIn("state.taskAbortController.abort()", JS)
        self.assertIn("error.name === 'AbortError'", JS)





    def test_profile_mode_sync_defines_form_wrapper(self):
        start = MAIN.index("function simcWbSyncProfileFormMode()")
        body = MAIN[start:MAIN.index("\n}", start) + 2]
        self.assertIn("const formWrap = document.getElementById('simc-wb-profile-form')", body)

    def test_profile_edit_mode_resolver_is_defined(self):
        self.assertIn("function getSimcProfileMode(profileData)", MAIN)
        self.assertIn("profileData?.player_config_mode || profileData?.player_import_mode", MAIN)
        self.assertIn("getSimcProfileMode(profileData)", MAIN)
        self.assertIn("clonedSelect.value = sourceSelect.value", MAIN)


    def test_dedicated_api_and_inline_sections(self):
        self.assertIn("const apiRoot = '/api/simc-workbench/'", JS)
        self.assertNotIn('data-template-type=', HTML)
        self.assertIn('id="simc-unified-apl-list"', HTML)
        self.assertNotIn("AplKeywordPair", HTML)
        self.assertIn('data-rule-subtab="secondary-rules"', HTML)
        self.assertIn('data-rule-subtab="mastery-rules"', HTML)
        self.assertIn('data-rule-panel="secondary-rules"', HTML)
        self.assertIn('data-rule-panel="mastery-rules"', HTML)
        for panel in ("tasks", "templates", "apl", "backend"):
            marker = f'id="simc-workbench-{panel}-panel"'
            start = HTML.index(marker)
            self.assertNotIn('></div>', HTML[start:start + len(marker) + 20])

    def test_content_templates_are_unfiltered_and_apl_uses_its_own_resource(self):
        self.assertNotIn('id="simc-template-type-filters"', HTML)
        self.assertNotIn("library: 'default_apl'", JS)
        self.assertIn("data = await json(resourceUrl('apls')", JS)

    def test_workbench_controller_has_no_scripted_or_legacy_navigation(self):
        forbidden = (
            "window.open(", "alert(", "prompt(", "confirm(",
            "modal", "appendChild", "开发中", "stub", "'/dashboard/'",
        )
        lowered = JS.lower()
        for token in forbidden:
            self.assertNotIn(token.lower(), lowered)
        self.assertIn("Number.parseInt", JS)
        self.assertIn("window.escapeHtml", JS)
        self.assertIn("startsWith('/')", JS)
        self.assertEqual(MAIN.count("function escapeHtml"), 1)

    def test_apl_list_uses_structured_table_with_class_spec_and_editor_actions(self):
        panel = HTML[HTML.index('id="simc-workbench-apl-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
        render_start = JS.index('function renderUnifiedAplList()')
        render_end = JS.index('function renderMyAplList()', render_start)
        render_body = JS[render_start:render_end]
        self.assertIn('APL 资源库', panel)
        self.assertIn('data-apl-list-summary', panel)
        self.assertIn('simc-apl-table-wrap', render_body)
        self.assertIn('simc-apl-table', render_body)
        for heading in ('APL 名称', '职业', '专精', '来源', '状态', '操作'):
            self.assertIn(heading, render_body)
        self.assertNotIn('<article', render_body)
        self.assertIn('data-apl-action="edit"', render_body)
        self.assertIn('data-default-apl-action="view"', render_body)
        self.assertIn("openSimcWorkbenchDialog('apl-form'", JS)

    def test_apl_library_supports_exact_spec_filter_and_copy_for_all_visible_rows(self):
        panel = HTML[HTML.index('id="simc-workbench-apl-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
        render_start = JS.index('function renderUnifiedAplList()')
        render_end = JS.index('function renderMyAplList()', render_start)
        render_body = JS[render_start:render_end]
        self.assertIn('id="simc-apl-spec-filter"', panel)
        self.assertIn('全部专精', panel)
        self.assertIn('state.aplSpecFilter', render_body)
        self.assertIn('row.spec === state.aplSpecFilter', render_body)
        self.assertIn('classLabel(row)', render_body)
        self.assertIn("`${classLabel(row)} · ${specLabel(row)}`", render_body)
        self.assertIn('data-apl-action="copy"', render_body)
        self.assertIn('copyAplToMy', JS)
        self.assertIn('copy_source_id: sourceId', JS)

    def test_apl_storage_has_dialog_crud_and_simulation_loading(self):
        self.assertNotIn('id="simc-wb-apl-storage-form"', HTML)
        self.assertIn("openSimcWorkbenchDialog('apl-form'", JS)
        self.assertIn('data-inline-create="apl-storage"', HTML)
        self.assertIn("resourceUrl('apls'", JS)
        self.assertNotIn("'/api/apl-storage/", JS)
        self.assertIn('data-apl-action="detail"', JS)
        self.assertIn('data-apl-action="edit"', JS)
        self.assertIn('data-apl-action="delete"', JS)
        self.assertIn('data-apl-action="confirm-delete"', JS)
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
        """Unified APL resources must have search and authenticated detail/edit/delete dialogs."""
        apl_panel_start = HTML.index('id="simc-workbench-apl-panel"')
        apl_panel_end = HTML.index('<!-- End L1 Panel: 模拟工作流', apl_panel_start)
        my_apl_section = HTML[apl_panel_start:apl_panel_end]
        self.assertIn('APL 资源库', my_apl_section)
        self.assertIn('id="simc-apl-search"', my_apl_section)
        self.assertIn('placeholder="搜索', my_apl_section)
        self.assertIn('showManagedAplDetail', JS)
        self.assertIn("openDialog('apl-detail'", JS)
        self.assertIn("openSimcWorkbenchDialog('apl-form'", JS)
        self.assertIn('data-apl-action="detail"', JS)
        self.assertIn('data-apl-action="edit"', JS)
        self.assertIn('data-apl-action="delete"', JS)
        detail_start = JS.index('async function fetchManagedAplDetail')
        detail_end = JS.index('\n    async function', detail_start + 20)
        detail_body = JS[detail_start:detail_end]
        self.assertIn("resourceUrl('apls', id)", detail_body)

    def test_apl_list_detail_supports_cached_chinese_translation(self):
        detail_start = JS.index('function renderManagedAplDetail')
        detail_end = JS.index('\n    async function', detail_start)
        detail_body = JS[detail_start:detail_end]
        self.assertIn('data-apl-detail-language="apl"', detail_body)
        self.assertIn('data-apl-detail-language="cn"', detail_body)
        self.assertIn('data-apl-detail-content', detail_body)
        self.assertIn('aplDetailTranslationCache', JS)
        self.assertIn("window.convertText(row.content, 'apl_to_cn', row.spec)", JS)
        self.assertIn("actionName === 'language'", JS)

    def test_apl_resources_share_one_list_with_spec_and_source_markers(self):
        """Personal and default APL resources belong in one searchable list, not side-by-side columns."""
        apl_panel_start = HTML.index('id="simc-workbench-apl-panel"')
        apl_panel_end = HTML.index('<!-- End L1 Panel: 模拟工作流', apl_panel_start)
        apl_section = HTML[apl_panel_start:apl_panel_end]
        self.assertIn('id="simc-apl-search"', apl_section)
        self.assertIn('id="simc-unified-apl-list"', apl_section)
        self.assertNotIn('xl:grid-cols-2', apl_section)
        self.assertNotIn('id="simc-my-apl-search"', apl_section)
        self.assertNotIn('id="simc-default-apl-search"', apl_section)
        self.assertNotIn('id="simc-default-apl-list"', apl_section)
        self.assertIn('renderUnifiedAplList', JS)
        self.assertIn("kind: 'personal'", JS)
        self.assertIn("kind: 'default'", JS)
        self.assertIn("row.kind === 'personal' ? row.apl_code : ''", JS)
        self.assertIn('专精', JS)
        self.assertIn("const sourceLabel = isPersonal ? '个人'", JS)
        self.assertIn("'SimC 上游' : '系统默认'", JS)
        self.assertIn('个人 APL 加载失败，已保留其他可用资源', JS)
        self.assertIn('系统默认 APL 加载失败，已保留其他可用资源', JS)

    def test_default_apl_library_shows_active_selectable_templates_with_spec(self):
        """Default APL library must use the independent APL resource and filter system rows."""
        self.assertIn('loadDefaultAplLibrary', JS)
        self.assertNotIn("library: 'default_apl'", JS)
        self.assertIn("data = await json(resourceUrl('apls')", JS)
        self.assertIn('row.is_system && row.is_active && row.is_selectable', JS)
        self.assertNotIn("template_type: 'default_apl'", JS)
        self.assertNotIn('is_active: true', JS)
        self.assertNotIn('is_selectable: true', JS)
        self.assertIn('data-default-apl-action="view"', JS)
        self.assertIn('data-default-apl-action="copy"', JS)
        self.assertIn('.class_name', JS)
        self.assertIn('.spec', JS)
        self.assertIn('data-apl-action="edit"', JS)

    def test_new_apl_waits_for_spec_options_and_unified_apl_rows_before_rendering(self):
        """Opening create must not snapshot empty async resources into the form."""
        self.assertIn('async function openNewAplStorageForm()', JS)
        start = JS.index('async function openNewAplStorageForm()')
        end = JS.index('\n    function ', start + 20)
        body = JS[start:end]
        self.assertIn("loadApl('apls', 'simc-unified-apl-list')", body)
        self.assertIn('loadSpecOptions()', body)
        self.assertIn('await Promise.all', body)
        self.assertLess(body.index('await Promise.all'), body.index('renderAplStorageForm()'))
        self.assertIn("if (aplCreate) openNewAplStorageForm().catch(notify)", JS)

    def test_default_apl_copy_button_obeys_api_can_copy_contract(self):
        render_start = JS.index('function renderUnifiedAplList()')
        render_end = JS.index('function renderMyAplList()', render_start)
        render_body = JS[render_start:render_end]
        self.assertIn('row.can_copy === true', render_body)
        self.assertNotIn('row.read_only ? `<button data-default-apl-action="copy"', render_body)

    def test_default_apl_copy_uses_backend_api_not_client_content(self):
        """Copy default APL must POST copy_template_id to backend, not send content from browser."""
        self.assertIn('copy_template_id', JS)
        self.assertIn("resourceUrl('apls')", JS)
        self.assertNotIn("'/api/apl-storage/", JS)
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
        self.assertIn("resourceUrl('apls', id)", detail_body)
        self.assertIn('readonly', detail_body)
        self.assertIn('.source', detail_body)
        self.assertIn('.spec', detail_body)

    def test_script_is_really_loaded(self):
        self.assertIn("{% static 'dashboard/js/main.js' %}?v=20260817_apl_select", HTML)
        self.assertIn("{% static 'dashboard/js/simc-workbench.js' %}?v=20260817_apl_select", HTML)
        self.assertIn("{% static 'dashboard/js/simc-apl-editor.js' %}?v=20260727b", HTML)
        self.assertNotIn("moveSimcToolIntoWorkbench", MAIN)

    def test_resource_list_simulate_actions_preselect_the_existing_workflow(self):
        """List shortcuts may only prefill the canonical form; task authorization stays server-side."""
        self.assertIn('data-profile-row-action="simulate"', MAIN)
        self.assertIn('data-profile-id="${id}"', MAIN)
        self.assertIn('data-profile-spec="${escapeHtml(row.canonical_spec || \'\')}"', MAIN)
        self.assertIn("window.startSimcSimulationFromResource({ profileId, spec: rowActionButton.dataset.profileSpec })", MAIN)
        self.assertIn("async function startSimcSimulationFromResource({ profileId = 0, aplId = 0, spec = '' } = {})", MAIN)
        shortcut_start = MAIN.index('async function startSimcSimulationFromResource(')
        shortcut_end = MAIN.index('\nlet simcResolvedBaseTemplateId', shortcut_start)
        shortcut = MAIN[shortcut_start:shortcut_end]
        self.assertIn("switchSimcWorkbenchL1Tab('workflow', 'import')", shortcut)
        self.assertIn('value="specified_spec"', shortcut)
        self.assertIn('await resolveSimcPlayerSource()', shortcut)
        self.assertIn('await onSimcProfileSelect()', shortcut)
        self.assertIn("document.getElementById('simc-sim-apl-list')", shortcut)
        self.assertIn('option.value === simcPendingAplId', shortcut)
        self.assertNotIn("fetch('/api/simc-task/", shortcut)
        self.assertIn('data-apl-action="simulate"', JS)
        self.assertIn('data-spec="${esc(row.spec || \'\')}"', JS)
        self.assertIn("window.startSimcSimulationFromResource({ aplId: id, spec: aplAction.dataset.spec })", JS)
        self.assertIn('can_use_for_task === true', JS)
        self.assertNotIn('需先校验发布', JS)

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
        self.assertIn("'/api/simc-profile/'", MAIN)
        self.assertIn('data-profile-row-action="delete"', MAIN)
        self.assertNotIn('data-profile-row-action="restore"', MAIN)
        self.assertIn("method: 'DELETE'", MAIN)
        self.assertIn('function simcWbDeleteProfile', MAIN)


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

    def test_primary_page_switch_updates_dashboard_section_and_sidebar_state(self):
        switch_start = MAIN.index("function switchSimcWorkbenchL1Tab(")
        switch_end = MAIN.index("\n\nfunction ", switch_start + 50)
        switch_body = MAIN[switch_start:switch_end]
        self.assertIn("activateSimcDashboardPage(activeL1Tab);", switch_body)
        self.assertNotIn(".simc-l1-tab", MAIN)
        self.assertIn("item.dataset.dashboardSection === targetSectionId", MAIN)

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
        self.assertNotIn("'apl-keywords': 'advanced'", switch_body)
        self.assertIn("switchSimcWorkbenchL1Tab(parentTab, activeTab)", switch_body)

    def test_workbench_data_loader_has_no_duplicate_model_navigation_handler(self):
        self.assertNotIn("const tab = event.target.closest('[data-simc-tab]')", JS)

    def test_task_creation_success_dialog_navigates_or_auto_closes_before_unlocking_submit(self):
        """A created task stays acknowledged in a modal before the unified submit button unlocks."""
        create_start = MAIN.index('async function createSimcSimulationTask()')
        create_end = MAIN.index('async function submitSimcHomeCreation()', create_start)
        create_body = MAIN[create_start:create_end]
        submit_start = MAIN.index('async function submitSimcHomeCreation()')
        submit_end = MAIN.index('async function loadSimcRaidBuffOptions()', submit_start)
        submit_body = MAIN[submit_start:submit_end]
        dialog_start = MAIN.index('function showSimcTaskCreatedDialog()')
        dialog_end = MAIN.index('function getTitleForDialogContent(', dialog_start)
        dialog_body = MAIN[dialog_start:dialog_end]

        self.assertIn('await showSimcTaskCreatedDialog()', create_body)
        self.assertNotIn("if (button) button.disabled = false", create_body)
        self.assertIn('button.disabled = true', submit_body)
        self.assertIn('button.disabled = false', submit_body)
        self.assertIn('任务已新建', dialog_body)
        self.assertIn('前往任务列表', dialog_body)
        self.assertIn('showDashboardSection(SIMC_DASHBOARD_SECTIONS.history)', dialog_body)
        self.assertIn('setTimeout', dialog_body)
        self.assertIn('1000', dialog_body)

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

    def test_navigation_default_state_does_not_force_open_a_simc_page(self):
        """Dashboard location chooses the page; initialization only selects its child panel."""
        self.assertIn('id="simc-workflow"', HTML)
        workflow_section = HTML[HTML.index('id="simc-workflow"'):HTML.index('id="simc-workflow"') + 260]
        self.assertIn('style="display: none;"', workflow_section)
        workflow_panel = HTML[HTML.index('data-simc-l1-panel="workflow"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]
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

    def test_template_edit_uses_shared_dialog_form(self):
        """The single system template can be edited in the shared dialog, but not created."""
        self.assertNotIn('data-inline-create="templates"', HTML)
        self.assertNotIn('id="simc-wb-template-form"', HTML)
        self.assertIn("openSimcWorkbenchDialog('template-form'", JS)
        self.assertIn('async function editTemplate(id)', JS)
        self.assertIn("resourceUrl('templates', id)", JS)


    def test_template_click_handlers_exist(self):
        """Template edit and detail handlers must exist without lifecycle controls."""
        self.assertIn('data-wb-action="template-edit"', JS)
        self.assertIn('data-wb-action="template-detail"', JS)
        load_start = JS.index('async function loadTemplates')
        load_end = JS.index('function renderTemplateForm', load_start)
        load_body = JS[load_start:load_end]
        self.assertNotIn('data-wb-action="archive"', load_body)
        self.assertNotIn('data-wb-action="restore"', load_body)
        self.assertIn('data-template-action="cancel"', JS)
        self.assertIn('function closeTemplateDetail()', JS)


    def test_template_submit_handler_exists(self):
        """Template form submission must be handled."""
        self.assertIn('data-template-form', JS)


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

    def test_agent_enrollment_codes_are_staff_only_one_time_ui(self):
        """Staff can create/copy/revoke codes, while list rendering never expects plaintext."""
        backend_start = HTML.index('id="simc-workbench-backend-panel"')
        backend_end = HTML.index('id="simc-workbench-rules-panel"', backend_start)
        backend_panel = HTML[backend_start:backend_end]
        self.assertIn('{% if request.user.is_staff %}', backend_panel)
        self.assertIn('id="simc-agent-enrollment-form"', backend_panel)
        self.assertIn('name="backend_identifier"', backend_panel)
        self.assertIn('name="expires_in_seconds"', backend_panel)
        self.assertIn('value="1800"', backend_panel)
        self.assertIn('首次注册窗口', backend_panel)
        self.assertIn('不会限制 Agent 的运行时间', backend_panel)
        self.assertIn('注册成功后使用长期凭据', backend_panel)
        self.assertIn('id="simc-agent-enrollment-reveal"', backend_panel)
        self.assertIn('id="simc-agent-enrollment-list"', backend_panel)
        for token in (
            "resourceUrl('agent-enrollment-codes')",
            "backend_identifier: backendIdentifier",
            "expires_in_seconds: expiresInSeconds",
            "payload.data?.enrollment_code",
            "data-agent-enrollment-action=\"copy\"",
            "data-agent-enrollment-action=\"revoke\"",
            "}revoke/`",
            "'X-CSRFToken': window.getCSRFToken()",
        ):
            self.assertIn(token, JS)
        list_start = JS.index('async function loadAgentEnrollmentCodes()')
        create_start = JS.index('async function createAgentEnrollmentCode(', list_start)
        list_body = JS[list_start:create_start]
        self.assertNotIn('enrollment_code', list_body)
        self.assertNotIn('localStorage', JS)
        self.assertNotIn('sessionStorage', JS)

    def test_backend_panel_loads_and_renders_registered_agent_instances(self):
        """Backend page must consume the Agent management projection, not stop at enrollment codes."""
        backend_start = HTML.index('id="simc-workbench-backend-panel"')
        backend_end = HTML.index('id="simc-workbench-rules-panel"', backend_start)
        backend_panel = HTML[backend_start:backend_end]
        self.assertIn('id="simc-agent-list"', backend_panel)
        self.assertIn("simc-workbench.js' %}?v=20260728a", HTML)
        self.assertIn('async function loadAgents()', JS)
        self.assertIn("resourceUrl('agents')", JS)
        self.assertIn('loadAgents().catch(notify)', JS)
        for field in ('row.backend', 'row.online', 'row.status', 'row.current_version',
                      'row.binary_available', 'row.last_seen_at', 'row.lease'):
            self.assertIn(field, JS)

    def test_backend_controls_post_real_actions_with_csrf(self):
        """Backend check/update/auto-update controls must POST to the dedicated API."""
        self.assertIn('async function runBackendAction(', JS)
        self.assertIn("'/api/simc-backend-binary/'", JS)
        self.assertIn("'X-CSRFToken': window.getCSRFToken()", JS)
        self.assertIn("action: 'set_auto_update'", JS)
        self.assertIn('data-backend-id="${idOf(info.id)}"', JS)
        self.assertIn('backend_id: idOf(backendAction.dataset.backendId)', JS)

    def test_legacy_backend_compile_tool_posts_supported_action(self):
        """The live compile button must use the API's explicit check/update action contract."""
        self.assertIn("action: checkOnly ? 'check' : 'update'", MAIN)

    def test_backend_controls_have_delegated_click_and_change_handlers(self):
        """Rendered backend controls must be connected through delegated safe handlers."""
        self.assertIn("closest('[data-backend-action]')", JS)
        self.assertIn("closest('[data-backend-auto-update]')", JS)
        self.assertNotIn('onclick=', JS)

    def test_backend_panel_renders_operational_status_not_only_versions(self):
        """Backend panel must expose availability, progress, status and safe error state."""
        for field in ('available', 'need_update', 'is_updating', 'update_progress',
                      'update_status', 'has_error', 'auto_update', 'game_version'):
            self.assertIn(f'info.{field}', JS)
        self.assertIn('魔兽世界版本', JS)

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
        for resource in ('secondary-rules', 'mastery-rules', 'backend'):
            self.assertIn(f'data-simc-model="{resource}"', advanced)

    def test_one_accessible_workbench_dialog_exists(self):
        self.assertEqual(HTML.count('id="simc-workbench-dialog"'), 1)
        self.assertIn('role="dialog"', HTML)
        self.assertIn('aria-modal="true"', HTML)
        self.assertIn('id="simc-workbench-dialog-backdrop"', HTML)
        self.assertIn('id="simc-workbench-dialog-content"', HTML)
        self.assertIn('data-simc-dialog-close', HTML)

    def test_simulation_apl_picker_only_marks_simc_sources(self):
        start = MAIN.index('async function loadSimcAplCandidates(')
        end = MAIN.index('async function simcWbFetchProfilesForWorkbench(', start)
        picker = MAIN[start:end]
        self.assertIn("['simc_upstream', 'simc_builtin'].includes(row.source)", picker)
        self.assertIn("`${name} · SimC`", picker)
        self.assertNotIn('row.spec_label', picker)

    def test_talent_string_editor_populates_the_visible_dialog_spec_select(self):
        self.assertNotIn('id="simc-talent-string-editor"', HTML)
        editor_start = MAIN.index('function simcTalentStringOpenEditor(')
        editor_end = MAIN.index('async function saveSimcTalentString()', editor_start)
        editor = MAIN[editor_start:editor_end]
        self.assertIn("body.querySelector('#simc-talent-string-spec')", editor)
        self.assertNotIn("document.getElementById('simc-talent-string-spec')", editor)

    def test_talent_string_editor_allows_auto_spec_detection(self):
        editor_start = MAIN.index('function simcTalentStringOpenEditor(')
        editor_end = MAIN.index('async function saveSimcTalentString()', editor_start)
        editor = MAIN[editor_start:editor_end]
        self.assertIn('专精（可选，留空自动识别）', editor)

    def test_talent_string_list_has_copy_code_action(self):
        list_start = MAIN.index('function loadSimcTalentStrings()')
        list_end = MAIN.index('function simcTalentStringOpenEditor(', list_start)
        listing = MAIN[list_start:list_end]
        self.assertIn('data-talent-string-action="copy-code"', listing)
        self.assertNotIn('data-talent-string-action="copy"', listing)
        self.assertIn('navigator.clipboard.writeText(row.talent)', MAIN)
        self.assertIn('parseSimcTalentStringResponse', MAIN)
        self.assertIn("response.status === 403 ? '请求被拒绝，请刷新页面后重试'", MAIN)
        self.assertNotIn("const result = await response.json();", MAIN[MAIN.index('async function saveSimcTalentString'):MAIN.index('function bindSimcTalentStringControls')])

    def test_talent_string_list_uses_shared_spec_catalog_labels(self):
        loader_start = MAIN.index('async function loadSimcSpecOptions()')
        loader_end = MAIN.index('window.loadSimcSpecOptions', loader_start)
        loader = MAIN[loader_start:loader_end]
        self.assertIn("document.getElementById('simc-talent-string-spec-filter')", loader)
        list_start = MAIN.index('function loadSimcTalentStrings()')
        list_end = MAIN.index('function simcTalentStringOpenEditor(', list_start)
        listing = MAIN[list_start:list_end]
        self.assertIn('row.label ||', listing)
        self.assertIn('row.spec_label} · ${row.class_label ||', listing)
        editor_start = MAIN.index('function simcTalentStringOpenEditor(')
        editor_end = MAIN.index('async function saveSimcTalentString()', editor_start)
        editor = MAIN[editor_start:editor_end]
        self.assertIn('item.label || `${item.spec_label} · ${item.class_label}`', editor)

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

    def test_simc_reports_open_as_standalone_authenticated_pages(self):
        detail_start = JS.index('async function showTaskDetail')
        detail_end = JS.index('async function showTaskComparison', detail_start)
        detail = JS[detail_start:detail_end]
        self.assertIn('href="${esc(artifact.preview_url)}"', detail)
        self.assertIn('查看原生报告', detail)
        self.assertNotIn('data-artifact-preview=', detail)
        self.assertNotIn('renderSimcArtifactFrame(', detail)
        self.assertNotIn('<iframe', detail)

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

    def test_profile_form_uses_dialog_not_bottom_slot(self):
        self.assertNotIn('id="simc-wb-profile-detail"', HTML)
        self.assertNotIn('id="simc-wb-profile-form"', HTML)
        self.assertIn("openSimcWorkbenchDialog('profile-form'", MAIN)

    def test_simc_management_uses_chinese_spec_labels(self):
        self.assertIn("row.spec_label || row.spec", MAIN)
        self.assertIn("specLabel(row", JS)
        self.assertIn("row.label || `${row.spec_label} · ${row.class_label}`", MAIN)
        self.assertNotIn('<option value="fury">狂怒</option>', HTML)
        self.assertNotIn('<option value="fury">fury</option>', HTML)

    def test_profile_filter_uses_authoritative_chinese_spec_labels(self):
        filter_start = MAIN.index('async function loadSimcSpecOptions()')
        filter_end = MAIN.index('\nfunction ', filter_start + 20)
        filter_body = MAIN[filter_start:filter_end]
        self.assertIn("fetch('/api/simc-spec-options/'", filter_body)
        self.assertIn('row.spec_label', filter_body)
        self.assertIn('option.value = row.value', filter_body)

    def test_profile_table_headers_sort_the_complete_filtered_result_before_pagination(self):
        profile_table = HTML[
            HTML.index('<table class="simc-responsive-table min-w-full text-sm">'):
            HTML.index('id="simc-wb-profile-pagination"')
        ]
        for key in ('id', 'name', 'spec', 'source', 'status'):
            self.assertIn(f'data-profile-sort="{key}"', profile_table)
        self.assertIn('aria-sort="none"', profile_table)

        load_start = MAIN.index('function loadSimcWorkbenchProfiles(page)')
        load_end = MAIN.index('function renderSimcProfileDetailDialog', load_start)
        load_body = MAIN[load_start:load_end]
        self.assertIn('rows = sortSimcProfileRows(rows, requestedSort)', load_body)
        self.assertLess(
            load_body.index('rows = sortSimcProfileRows(rows, requestedSort)'),
            load_body.index('rows.slice(startIdx, endIdx)'),
        )
        self.assertIn("event.target.closest('[data-profile-sort]')", MAIN)
        self.assertIn('loadSimcWorkbenchProfiles(1)', MAIN)

    def test_template_and_apl_view_edit_use_dialog_not_bottom_slots(self):
        self.assertIn("openSimcWorkbenchDialog('template-detail'", JS)
        self.assertIn("openSimcWorkbenchDialog('template-form'", JS)
        self.assertIn("openSimcWorkbenchDialog('apl-form'", JS)
        for slot_id in (
            'simc-wb-template-detail', 'simc-wb-template-form',
            'simc-wb-apl-storage-form',
        ):
            self.assertNotIn(f'id="{slot_id}"', HTML)

    def test_profile_and_apl_forms_use_structured_code_editors(self):
        profile = HTML[HTML.index('id="simc-wb-profile-form-source"'):HTML.index('id="simc-wb-profile-list"')]
        self.assertIn('simc-profile-section', profile)
        self.assertIn('simc-code-editor', profile)
        self.assertIn('simc-editor-actions', profile)
        apl_start = JS.index('function renderAplStorageForm')
        apl_end = JS.index('function closeAplStorageForm', apl_start)
        apl_form = JS[apl_start:apl_end]
        for token in ('simc-editor-section', 'simc-apl-editor-mount', 'data-code-editor-stats', 'data-apl-editor-diagnostics'):
            self.assertIn(token, apl_form)
        self.assertNotIn('<textarea name="apl_code"', apl_form)


    def test_task_dialog_renders_run_dps_and_delta_without_navigation(self):
        start = JS.index('async function showTaskComparison')
        end = JS.index('\n    async function', start + 20)
        body = JS[start:end]
        self.assertIn("openSimcWorkbenchDialog('task-comparison'", body)
        self.assertIn('.dps', body)
        self.assertIn('delta', body)
        self.assertNotIn('/simc-compare/', body)

    def test_workbench_does_not_use_scripted_navigation_or_native_dialogs(self):
        combined = SIMC_HTML + JS + SIMC_MAIN
        for token in ('window.open(', 'alert(', 'prompt(', 'confirm(', 'onclick='):
            self.assertNotIn(token.lower(), combined.lower())

    def test_workflow_resources_are_reachable_and_keywords_stay_in_advanced(self):
        for resource in ('profiles', 'templates', 'apl'):
            self.assertIn(f'data-simc-workflow-entry="{resource}"', HTML)
        workflow = self._l1_section('workflow', '<!-- End L1 Panel: 模拟工作流 -->')
        advanced = self._l1_section('advanced', '<!-- End L1 Panel: 高级设置 -->')
        self.assertNotIn('simc-wb-apl-keyword-list', workflow)
        self.assertNotIn('simc-wb-apl-keyword-list', advanced)
        self.assertIn("profiles: 'workflow'", MAIN)
        self.assertNotIn("'apl-keywords': 'advanced'", MAIN)

    def test_dialog_close_cancels_remaining_detail_requests(self):
        self.assertIn("new CustomEvent('simc-dialog-closing', { detail: { reason: 'close' } })", MAIN)
        self.assertIn("document.addEventListener('simc-dialog-closing'", JS)
        self.assertNotIn('simcWbCancelProfileDetail', MAIN)
