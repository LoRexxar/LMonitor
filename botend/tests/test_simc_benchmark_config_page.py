from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from botend.models import SimcBenchmarkPanel


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

    def test_page_requires_login_and_staff_permission(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.regular)
        self.assertEqual(self.client.get(self.url()).status_code, 403)

    def test_staff_can_open_dedicated_full_configuration_page(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dashboard/simc_benchmark_config.html')
        self.assertContains(response, 'data-benchmark-config-page')
        self.assertContains(response, f'data-benchmark-panel-id="{self.panel.id}"')
        self.assertContains(response, '专精配置')
        self.assertContains(response, '候选装备')

    def test_unknown_panel_is_not_disclosed(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url(999999)).status_code, 404)
