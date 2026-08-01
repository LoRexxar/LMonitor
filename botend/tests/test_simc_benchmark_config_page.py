from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from botend.models import SimcBenchmarkExecution, SimcBenchmarkPanel


class SimcBenchmarkConfigPageTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('benchmark-config-staff', is_staff=True)
        self.regular = User.objects.create_user('benchmark-config-regular')
        self.panel = SimcBenchmarkPanel.objects.create(
            name='Detailed benchmark', slug='detailed-benchmark',
            created_by_id=self.staff.id,
        )

    def url(self, panel_id=None):
        return reverse('simc_benchmark_config_page', args=[panel_id or self.panel.id])

    def panel_edit_url(self, panel_id=None):
        return reverse('simc_benchmark_panel_edit_page', args=[panel_id or self.panel.id])

    def test_page_requires_login_but_logged_in_users_can_open_configuration(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.regular)
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_staff_can_open_dedicated_full_configuration_page(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dashboard/simc_benchmark_config.html')
        self.assertContains(response, 'data-benchmark-config-page')
        self.assertContains(response, f'data-benchmark-panel-id="{self.panel.id}"')
        self.assertContains(response, '专精配置')
        self.assertContains(response, '候选装备')

    def test_staff_can_open_separate_panel_editor(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.panel_edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dashboard/simc_benchmark_panel_edit.html')
        self.assertContains(response, 'data-benchmark-panel-edit-page')
        self.assertContains(response, '维护面板身份、说明、公开边界和定时策略')

    def test_unknown_panel_is_not_disclosed(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url(999999)).status_code, 404)

    def test_logged_in_user_can_open_private_execution_result_page_without_portal_publication(self):
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
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(f'/api/simc-benchmarks/executions/{execution.id}/').status_code, 200)

    def test_existing_candidate_keeps_key_when_another_item_level_is_added(self):
        script = Path('static/dashboard/js/simc-benchmark-dashboard.js').read_text()
        self.assertIn('originalItemId:parts.itemId', script)
        self.assertIn('originalItemLevel:parts.itemLevel', script)
        self.assertIn('sameOriginalIdentity', script)
        self.assertNotIn("levels.length>1?`${meta.key}-${itemLevel}`:meta.key", script)
