import json
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from botend.models import (
    DashboardUserGroup,
    DashboardUserGroupMembership,
    SimcBenchmarkExecution,
    SimcBenchmarkPanel,
)


class SimcBenchmarkConfigPageTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('benchmark-config-staff', is_staff=True)
        self.regular = User.objects.create_user('benchmark-config-regular')
        self.authorized = User.objects.create_user('benchmark-config-authorized')
        group = DashboardUserGroup.objects.create(
            name='SimC 基准组', permission_codes=['simc.benchmarks'],
        )
        DashboardUserGroupMembership.objects.create(user=self.staff, group=group)
        DashboardUserGroupMembership.objects.create(user=self.authorized, group=group)
        self.panel = SimcBenchmarkPanel.objects.create(
            name='Detailed benchmark', slug='detailed-benchmark',
            created_by_id=self.staff.id,
        )

    def url(self, panel_id=None):
        return reverse('simc_benchmark_config_page', args=[panel_id or self.panel.id])

    def panel_edit_url(self, panel_id=None):
        return reverse('simc_benchmark_panel_edit_page', args=[panel_id or self.panel.id])

    def test_page_requires_login_and_benchmark_permission(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.regular)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.client.force_login(self.authorized)
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_staff_can_open_dedicated_full_configuration_page(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dashboard/simc_benchmark_config.html')
        self.assertContains(response, 'data-benchmark-config-page')
        self.assertContains(response, f'data-benchmark-panel-id="{self.panel.id}"')
        self.assertContains(response, '候选装备')
        self.assertContains(response, 'href="/dashboard/?section=simc-benchmarks"', html=False)

    def test_staff_can_open_separate_panel_editor(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.panel_edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dashboard/simc_benchmark_panel_edit.html')
        self.assertContains(response, 'data-benchmark-panel-edit-page')
        self.assertContains(response, '支持 Markdown 和多行文本')
        self.assertContains(response, 'textarea name="description" rows="10"', html=False)
        self.assertContains(response, '维护面板身份、说明、公开边界和定时策略')

    def test_panel_edit_patch_persists_queue_priority(self):
        self.client.force_login(self.staff)
        response = self.client.patch(
            f'/api/simc-benchmarks/panels/{self.panel.id}/',
            data=json.dumps({'name': 'Detailed benchmark', 'queue_priority': 30}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['queue_priority'], 30)
        self.panel.refresh_from_db()
        self.assertEqual(self.panel.queue_priority, 30)

    def test_unknown_panel_is_not_disclosed(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url(999999)).status_code, 404)

    def test_benchmark_permission_controls_private_execution_result_page(self):
        execution = SimcBenchmarkExecution.objects.create(
            panel=self.panel, config_snapshot={}, config_hash='a' * 64,
        )
        url = reverse('simc_benchmark_execution_page', args=[execution.id])
        self.client.force_login(self.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dashboard/simc_benchmark_execution.html')
        self.assertContains(response, f'data-benchmark-execution-id="{execution.id}"')
        self.assertContains(response, '私有执行结果')

        self.client.force_login(self.regular)
        self.assertEqual(self.client.get(url).status_code, 403)

        self.client.force_login(self.authorized)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(f'/api/simc-benchmarks/executions/{execution.id}/').status_code, 200)

    def test_variant_apl_option_selection_uses_dom_boolean_property(self):
        """A saved non-default variant APL must not be replaced by the last option."""
        script = Path('static/dashboard/js/simc-benchmark-dashboard.js').read_text()
        self.assertIn("else if(key==='selected') node.selected=!!value", script)
        self.assertIn("selected:String(apl.id)===String(explicitApl)", script)

    def test_existing_candidate_keeps_key_when_another_item_level_is_added(self):
        script = Path('static/dashboard/js/simc-benchmark-dashboard.js').read_text()
        self.assertIn('originalItemId:parts.itemId', script)
        self.assertIn('originalItemLevel:parts.itemLevel', script)
        self.assertIn('sameOriginalIdentity', script)
        self.assertIn('keysByLevel', script)
        self.assertNotIn("levels.length>1?`${meta.key}-${itemLevel}`:meta.key", script)

    def test_saved_item_level_variants_are_coalesced_back_into_one_editor_row(self):
        script = Path('static/dashboard/js/simc-benchmark-dashboard.js').read_text()
        self.assertIn('function coalesceCandidateEditorRows', script)
        self.assertIn('coalesceCandidateEditorRows(data.candidates||[]).forEach(addCandidate)', script)
        self.assertIn("editorLevels.join(', ')", script)
        self.assertIn('editorKeys', script)

    def test_candidate_name_is_the_first_primary_field_and_execution_size_has_no_fixed_cap(self):
        script = Path('static/dashboard/js/simc-benchmark-dashboard.js').read_text()
        self.assertIn('primary.append(name,itemId,itemLevel)', script)
        self.assertNotIn('limits.max_candidates', script)
        self.assertNotIn('limits.max_cases', script)
        self.assertNotIn('limits.max_runs_per_task', script)
        self.assertNotIn('/ ${limits.max_cases} cases', script)
        self.assertNotIn('/ ${limits.max_runs_per_task} runs', script)

    def test_scenario_editor_exposes_core_simc_parameters_without_opening_advanced_settings(self):
        script = Path('static/dashboard/js/simc-benchmark-dashboard.js').read_text()
        self.assertIn("const SCENARIO_PRIMARY_PARAMS = ['desired_targets','max_time','iterations','fight_style'];", script)
        self.assertIn("desired_targets:{label:'目标数'", script)
        self.assertIn("max_time:{label:'战斗时间（秒）'", script)
        self.assertIn("iterations:{label:'迭代次数'", script)
        self.assertIn("fight_style:{label:'战斗类型'", script)
        self.assertIn("class:'scenario-essential-params'", script)
        self.assertIn("placeholder:'SimC 默认 300'", script)

    def test_scenario_editor_uses_published_fight_style_options_and_generated_read_only_key(self):
        script = Path('static/dashboard/js/simc-benchmark-dashboard.js').read_text()
        self.assertIn("resources?.fight_styles", script)
        self.assertIn("view.type==='select'", script)
        self.assertIn("fight_style:{label:'战斗类型',type:'select'", script)
        self.assertIn("field('显示名称 *','name'", script)
        self.assertIn('generatedScenarioKey(', script)
        self.assertIn("type:'hidden',name:'key'", script)
        self.assertIn('标识由系统自动生成', script)
        self.assertIn('仅用于界面展示，可随时修改', script)
        self.assertNotIn("field('稳定标识 *','key'", script)
        self.assertNotIn("field('key *','key'", script)

    def test_scenario_advanced_editor_has_server_catalog_raid_buff_bulk_controls(self):
        script = Path('static/dashboard/js/simc-benchmark-dashboard.js').read_text()
        self.assertIn("resources?.raid_buffs", script)
        self.assertIn("dataset:{raidBuff", script)
        self.assertIn("dataset:{raidBuffToggle", script)
        self.assertIn('全选 Raid Buffs', script)
        self.assertIn('清空 Raid Buffs', script)
        self.assertIn('indeterminate', script)
        self.assertIn('simulation_params.raid_buffs', script)
        self.assertIn('useClassRaidBuff', script)
        self.assertIn('自动启用各 Profile 职业自身团队增益', script)
        self.assertIn('simulation_params.use_class_raid_buff', script)

        styles = Path('static/dashboard/css/simc-benchmark-dashboard.css').read_text()
        self.assertIn('.raid-buff-toggle input,.raid-buff-choice input', styles)
        self.assertIn('.simc-benchmark-config-page .raid-buff-toggle,.simc-benchmark-config-page .raid-buff-choice', styles)
        self.assertIn('display:flex !important', styles)
        self.assertIn('width:1rem', styles)
        self.assertIn('flex:0 0 auto', styles)
