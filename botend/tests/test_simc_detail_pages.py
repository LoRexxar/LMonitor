from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from botend.models import SimcTask, SimulationRun


ROOT = Path(__file__).resolve().parents[2]


class SimcDetailPageRoutingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='detail-owner', password='pwd')
        self.other = User.objects.create_user(username='detail-other', password='pwd')
        self.task = SimcTask.objects.create(
            user_id=self.owner.id, name='Owned task', simc_profile_id=0,
            task_type=1, current_status=2,
        )
        self.comparison_task = SimcTask.objects.create(
            user_id=self.owner.id, name='Owned comparison', simc_profile_id=0,
            mode='comparison', current_status=2,
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
        self.assertIn('方案内容', detail)
        self.assertIn('item.candidate?.talent', detail)

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
        self.assertIn('<span>查看结果</span></a>', history)
        self.assertNotIn('data-wb-action="detail"', history)
        self.assertIn('row.runs', task_detail)
        self.assertIn('run.sequence', task_detail)
        self.assertIn('run.result_summary?.dps', task_detail)
        self.assertNotIn('data-wb-action="detail"', task_detail)
