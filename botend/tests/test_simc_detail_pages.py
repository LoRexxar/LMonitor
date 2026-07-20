from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from botend.models import SimcTask, SimcTaskBatch


ROOT = Path(__file__).resolve().parents[2]


class SimcDetailPageRoutingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='detail-owner', password='pwd')
        self.other = User.objects.create_user(username='detail-other', password='pwd')
        self.task = SimcTask.objects.create(
            user_id=self.owner.id, name='Owned task', simc_profile_id=0,
            task_type=1, current_status=2,
        )
        self.batch = SimcTaskBatch.objects.create(
            user_id=self.owner.id, name='Owned batch', batch_type='comparison', status=2,
        )

    def test_pages_require_login(self):
        for name, object_id in (('simc_task_detail_page', self.task.id), ('simc_batch_detail_page', self.batch.id)):
            response = self.client.get(reverse(name, args=[object_id]))
            self.assertEqual(response.status_code, 302)
            self.assertIn('/auth/login/', response.url)

    def test_owner_can_open_task_and_batch_shells(self):
        self.client.force_login(self.owner)
        task_response = self.client.get(reverse('simc_task_detail_page', args=[self.task.id]))
        batch_response = self.client.get(reverse('simc_batch_detail_page', args=[self.batch.id]))
        self.assertEqual(task_response.status_code, 200)
        self.assertContains(task_response, 'data-simc-detail-kind="tasks"')
        self.assertContains(task_response, f'data-simc-detail-id="{self.task.id}"')
        self.assertEqual(batch_response.status_code, 200)
        self.assertContains(batch_response, 'data-simc-detail-kind="batches"')
        self.assertContains(batch_response, f'data-simc-detail-id="{self.batch.id}"')

    def test_foreign_objects_are_not_disclosed(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('simc_task_detail_page', args=[self.task.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse('simc_batch_detail_page', args=[self.batch.id])).status_code, 404)


class SimcDetailPageFrontendContractTests(TestCase):
    def test_battlenet_source_can_load_class_top_players_and_fill_armory_fields(self):
        template = (ROOT / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        main = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')

        for token in ('simc-sim-bnet-class', 'simc-sim-bnet-top-player'):
            self.assertIn(token, template)
            self.assertIn(token, main)
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
        self.assertIn('percentNumber(item.dps_percent)', script)
        self.assertIn('report.talents', script)
        self.assertIn('simulation.timestamp', script)
        self.assertIn('/api/simc-workbench/${kind}/${objectId}/', script)
        self.assertIn('/dashboard/simc/tasks/${member.id}/', script)
        self.assertNotIn('error_detail', script)
        self.assertNotIn('file_path', script)
        self.assertNotIn('request_manifest', script)
        self.assertNotIn('.content', script)

    def test_history_results_and_batch_members_open_in_new_browser_page(self):
        workbench = (ROOT / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')
        history_start = workbench.index('async function loadTasks')
        history_end = workbench.index('\n    function scheduleTaskRefresh', history_start)
        history = workbench[history_start:history_end]
        batch_start = workbench.index("if (resource === 'batches')")
        batch_end = workbench.index("\n        const params =", batch_start)
        batch_detail = workbench[batch_start:batch_end]

        self.assertIn('href="/dashboard/simc/${resource}/${idOf(row.id)}/"', history)
        self.assertIn('target="_blank"', history)
        self.assertIn('rel="noopener noreferrer"', history)
        self.assertIn('>查看结果</a>', history)
        self.assertNotIn('data-wb-action="detail"', history)
        self.assertIn('href="/dashboard/simc/tasks/${idOf(member.id)}/"', batch_detail)
        self.assertIn('target="_blank"', batch_detail)
        self.assertIn('rel="noopener noreferrer"', batch_detail)
        self.assertNotIn('data-wb-action="detail"', batch_detail)
