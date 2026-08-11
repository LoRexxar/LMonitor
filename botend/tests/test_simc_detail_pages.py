from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from botend.models import (
    DashboardUserGroup,
    DashboardUserGroupMembership,
    SimcBackendBinary,
    SimcTask,
    SimulationRun,
)


ROOT = Path(__file__).resolve().parents[2]


class SimcDetailPageRoutingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='detail-owner', password='pwd')
        self.other = User.objects.create_user(username='detail-other', password='pwd')
        history_group = DashboardUserGroup.objects.create(
            name='SimC detail history group', permission_codes=['simc.history'],
        )
        DashboardUserGroupMembership.objects.bulk_create([
            DashboardUserGroupMembership(user=self.owner, group=history_group),
            DashboardUserGroupMembership(user=self.other, group=history_group),
        ])
        self.backend = SimcBackendBinary.objects.create(
            identifier='detail-test', name='详情页测试后端', simc_path='/tmp/simc',
        )
        self.task = SimcTask.objects.create(
            user_id=self.owner.id, name='Owned task', simc_profile_id=0,
            task_type=1, current_status=2, backend=self.backend,
        )
        self.comparison_task = SimcTask.objects.create(
            user_id=self.owner.id, name='Owned comparison', simc_profile_id=0,
            mode='comparison', current_status=2, backend=self.backend,
        )
        SimulationRun.objects.create(
            task=self.comparison_task, sequence=1, status='completed',
            candidate_key='baseline', candidate_label='基准',
            candidate_params={'is_base': True}, result_summary={'dps': 1000},
        )

    def test_pages_require_login(self):
        for name, object_id in (('simc_task_detail_page', self.task.id), ('simc_task_detail_page', self.comparison_task.id)):
            response = self.client.get(reverse(name, args=[object_id]))
            self.assertEqual(response.status_code, 302)
            self.assertIn('/auth/login/', response.url)

    def test_owner_can_open_regular_and_comparison_task_shells(self):
        self.client.force_login(self.owner)
        task_response = self.client.get(reverse('simc_task_detail_page', args=[self.task.id]))
        comparison_response = self.client.get(reverse('simc_task_detail_page', args=[self.comparison_task.id]))
        self.assertEqual(task_response.status_code, 200)
        self.assertContains(task_response, 'data-simc-detail-kind="tasks"')
        self.assertContains(task_response, f'data-simc-detail-id="{self.task.id}"')
        self.assertEqual(comparison_response.status_code, 200)
        self.assertContains(comparison_response, 'data-simc-detail-kind="tasks"')
        self.assertContains(comparison_response, f'data-simc-detail-id="{self.comparison_task.id}"')

    def test_foreign_objects_are_not_disclosed(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('simc_task_detail_page', args=[self.task.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse('simc_task_detail_page', args=[self.comparison_task.id])).status_code, 404)


class SimcDetailPageFrontendContractTests(TestCase):
    def test_battlenet_source_can_load_spec_top_players_and_fill_armory_fields(self):
        template = (ROOT / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        main = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')

        for token in ('simc-sim-bnet-spec', 'simc-sim-bnet-top-player'):
            self.assertIn(token, template)
            self.assertIn(token, main)
        self.assertNotIn('simc-sim-bnet-class', template)
        self.assertIn('?spec=${encodeURIComponent(spec)}', main)
        self.assertIn('/api/simc-battlenet-top-players/', main)
        self.assertIn('loadSimcBattlenetTopPlayers', main)
        self.assertIn('applySimcBattlenetTopPlayer', main)

    def test_battlenet_source_marks_cn_unavailable_and_does_not_offer_cn_region(self):
        template = (ROOT / 'templates/dashboard/index.html').read_text(encoding='utf-8')

        self.assertIn('国服角色无法通过 Battle.net 加载', template)
        self.assertNotIn('<option value="cn">中国</option>', template)

    def test_manual_talent_candidate_input_and_report_show_name_and_full_build(self):
        main = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        detail = (ROOT / 'static/dashboard/js/simc-detail.js').read_text(encoding='utf-8')

        for token in ('simc-comparison-add-talent-name', 'simc-comparison-add-talent-build',
                      'addSimcManualTalentCandidate'):
            self.assertIn(token, main)
        self.assertIn("source: 'manual'", main)
        self.assertIn('候选方案', detail)
        self.assertIn('row.mode_summary?.talent_candidate', detail)
        self.assertIn('talentCandidate?.talent', detail)

    def test_profile_form_submits_stat_overrides_for_every_mode(self):
        main = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        save_profile = main[main.index('async function simcWbSaveProfile()'):main.index(
            'async function simcWbDeleteProfile',
        )]

        self.assertNotIn("if (payload.player_config_mode === 'attribute_only')", save_profile)
        for field in ('gear_strength', 'gear_crit', 'gear_haste', 'gear_mastery', 'gear_versatility'):
            self.assertIn(f"{field}: gv('{field}')", save_profile)

    def test_battlenet_comparison_shows_default_talent_and_checkable_loadouts(self):
        main = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')

        self.assertIn("const defaultTalent = comparison?.default_talent", main)
        self.assertIn('默认天赋', main)
        self.assertIn('data-candidate-card="default-talent"', main)
        self.assertIn('data-kind="talent_candidates"', main)

    def test_dedicated_template_and_script_prioritize_safe_result_information(self):
        template = (ROOT / 'templates/dashboard/simc_detail.html').read_text(encoding='utf-8')
        script = (ROOT / 'static/dashboard/js/simc-detail.js').read_text(encoding='utf-8')
        self.assertIn('simc-detail.js', template)
        self.assertIn('@media (max-width: 720px)', template)
        for token in ('角色', 'DPS', '模拟参数', '技能伤害与触发明细', '动态 Buff / Proc', '常驻 Buff', '天赋与套装', '执行轮次', 'Artifact', '引用版本'):
            self.assertIn(token, script)
        for token in ('primary-link', 'share-track', 'talent-code', 'status-dot'):
            self.assertIn(token, template)
        self.assertIn('查看完整原生报告', script)
        hero_start = script.index('root.innerHTML = `<section class="hero">')
        hero_end = script.index('</section>', hero_start)
        hero = script[hero_start:hero_end]
        self.assertIn('class="hero-primary-column"', hero)
        self.assertIn('class="hero-resource-line"', hero)
        self.assertIn('.hero-primary-column{', template)
        self.assertIn('flex-direction:column', template)
        self.assertIn('.hero-resource-line b{', template)
        self.assertIn('overflow-wrap:anywhere', template)
        self.assertLess(hero.index('<h1>'), hero.index('<span>APL</span>'))
        self.assertLess(hero.index('<span>APL</span>'), hero.index('<span>Profile</span>'))
        self.assertIn("row.apl_name", hero)
        self.assertIn("row.profile_name", hero)
        self.assertIn('percentNumber(item.dps_percent)', script)
        self.assertIn('report.talents', script)
        self.assertIn('simulation.timestamp', script)
        self.assertIn('/api/simc-workbench/${kind}/${objectId}/', script)
        self.assertIn('row.runs', script)
        self.assertNotIn('error_detail', script)
        self.assertNotIn('file_path', script)
        self.assertNotIn('request_manifest', script)
        self.assertNotIn('.content', script)

    def test_complete_report_sections_use_one_shared_safe_renderer(self):
        detail_template = (ROOT / 'templates/dashboard/simc_detail.html').read_text(encoding='utf-8')
        dashboard_template = (ROOT / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        detail = (ROOT / 'static/dashboard/js/simc-detail.js').read_text(encoding='utf-8')
        workbench = (ROOT / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')
        component_path = ROOT / 'static/dashboard/js/simc-result-report.js'
        stylesheet_path = ROOT / 'static/dashboard/css/simc-result-report.css'

        self.assertTrue(component_path.exists())
        self.assertTrue(stylesheet_path.exists())
        component = component_path.read_text(encoding='utf-8')
        for template in (detail_template, dashboard_template):
            self.assertIn('dashboard/css/simc-result-report.css', template)
            self.assertIn('dashboard/js/simc-result-report.js', template)
        for token in ('report.sections', 'text_blocks', 'colspan', 'rowspan',
                      'simc-report-nav', 'Profile / 可复现配置'):
            self.assertIn(token, component)
        for entry in (detail, workbench):
            self.assertIn('window.SimcResultReport.renderSummary(report)', entry)
            self.assertIn('window.SimcResultReport.renderDetails(report)', entry)

    def test_result_page_prioritizes_conclusion_and_defers_non_repeating_evidence(self):
        detail_template = (ROOT / 'templates/dashboard/simc_detail.html').read_text(encoding='utf-8')
        dashboard_template = (ROOT / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        detail = (ROOT / 'static/dashboard/js/simc-detail.js').read_text(encoding='utf-8')
        workbench = (ROOT / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')
        component = (ROOT / 'static/dashboard/js/simc-result-report.js').read_text(encoding='utf-8')

        for token in (
            'renderSummary(report)', 'renderDetails(report)',
            'simc-report-result-summary', 'simc-report-damage-profile',
            'simc-report-evidence', '完整 SimC 数据',
            'DPS 误差', 'DPS 波动范围', '每点资源伤害（DPR）',
            '每次执行间隔', '平均值分布', '获取速率', '动作列表：',
        ):
            self.assertIn(token, component)

        details = component[component.index('function renderDetails(report)'):]
        self.assertIn('return `<details class="simc-report-evidence">', details)
        self.assertNotIn('${renderResultSummary(report)}', details)

        detail_output = detail[detail.index('root.innerHTML = `<section class="hero">'):]
        self.assertLess(detail_output.index('${resultSummary}'), detail_output.index('${renderTaskComparison(row)}'))
        self.assertLess(detail_output.index('${renderTaskComparison(row)}'), detail_output.index('${completeReport}'))
        for duplicate in ("card('结果概览'", "card('角色'", "card('模拟参数'"):
            self.assertNotIn(duplicate, detail)

        workbench_output = workbench[workbench.index('host.innerHTML = `<div class="flex flex-wrap justify-between gap-2">'):]
        self.assertLess(workbench_output.index('${resultSummary}'), workbench_output.index('${comparisonSections}'))
        self.assertLess(workbench_output.index('${comparisonSections}'), workbench_output.index('${completeReport}'))
        self.assertNotIn('analysisDocument', workbench)

        cache_token = '20260811_result_architecture_zh'
        self.assertIn(cache_token, detail_template)
        self.assertIn(cache_token, dashboard_template)

    def test_run_input_preview_is_available_from_workbench_and_dedicated_detail(self):
        template = (ROOT / 'templates/dashboard/simc_detail.html').read_text(encoding='utf-8')
        detail = (ROOT / 'static/dashboard/js/simc-detail.js').read_text(encoding='utf-8')
        workbench = (ROOT / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')

        endpoint = '/api/simc-workbench/tasks/${taskId}/runs/${runId}/input/'
        self.assertIn(endpoint, workbench)
        self.assertIn(endpoint, detail)
        self.assertIn('data-run-input', workbench)
        self.assertIn('data-run-input', detail)
        self.assertIn("code.textContent = payload['content']", workbench)
        self.assertIn("code.textContent = payload['content']", detail)
        self.assertIn('正在生成 SimC 输入', workbench)
        self.assertIn('正在生成 SimC 输入', detail)
        self.assertIn('它不是历史执行输入的复原或校验', detail)
        self.assertNotIn('重建哈希与执行哈希一致', workbench)
        self.assertNotIn('重建哈希与执行哈希一致', detail)
        self.assertIn('id="simc-input-dialog"', template)
        self.assertIn('@media (max-width: 720px)', template)

    def test_history_results_open_in_new_browser_page_and_task_detail_lists_runs(self):
        workbench = (ROOT / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')
        history_start = workbench.index('async function loadTasks')
        history_end = workbench.index('\n    function scheduleTaskRefresh', history_start)
        history = workbench[history_start:history_end]
        task_start = workbench.index("const runs = Array.isArray(row.runs)")
        task_end = workbench.index("\n    }", task_start)
        task_detail = workbench[task_start:task_end]

        self.assertIn('href="/dashboard/simc/${resource}/${idOf(row.id)}/"', history)
        self.assertIn('target="_blank"', history)
        self.assertIn('rel="noopener noreferrer"', history)
        self.assertIn('<span>查看详情</span></a>', history)
        self.assertNotIn('data-wb-action="detail"', history)
        self.assertIn('row.runs', task_detail)
        self.assertIn('run.sequence', task_detail)
        self.assertIn('run.result_summary?.dps', task_detail)
        self.assertNotIn('data-wb-action="detail"', task_detail)
