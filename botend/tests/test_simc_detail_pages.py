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
    def test_dedicated_template_and_script_prioritize_safe_result_information(self):
        template = (ROOT / 'templates/dashboard/simc_detail.html').read_text(encoding='utf-8')
        script = (ROOT / 'static/dashboard/js/simc-detail.js').read_text(encoding='utf-8')
        self.assertIn('simc-detail.js', template)
        self.assertIn('@media (max-width: 720px)', template)
        for token in ('角色', 'DPS', '模拟参数', '主要技能', '天赋与套装', '执行轮次', 'Artifact', '引用版本'):
            self.assertIn(token, script)
        self.assertIn('report.talents', script)
        self.assertIn('simulation.timestamp', script)
        self.assertIn('/api/simc-workbench/${kind}/${objectId}/', script)
        self.assertIn('/dashboard/simc/tasks/${member.id}/', script)
        self.assertNotIn('error_detail', script)
        self.assertNotIn('file_path', script)
        self.assertNotIn('request_manifest', script)
        self.assertNotIn('.content', script)

    def test_history_and_batch_members_are_links_and_rerun_navigates(self):
        workbench = (ROOT / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')
        main = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        self.assertIn('href="/dashboard/simc/${resource}/${idOf(row.id)}/"', workbench)
        self.assertIn('href="/dashboard/simc/tasks/${idOf(member.id)}/"', workbench)
        self.assertIn("window.location.assign(`/dashboard/simc/tasks/${idOf(result.data?.id)}/`)", workbench)
        self.assertNotIn("window.simcWorkbenchShowTaskDetail('tasks',", main)
        self.assertNotIn("window.simcWorkbenchShowTaskDetail('batches',", main)
