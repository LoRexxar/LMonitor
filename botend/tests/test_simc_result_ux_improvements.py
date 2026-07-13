from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.models import SimcTask


class SimcResultUXTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='result_ux_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def _attribute_task(self, name, filenames):
        return SimcTask.objects.create(
            user_id=self.user.id,
            name=name,
            task_type=2,
            simc_profile_id=0,
            current_status=2,
            result_file=','.join(filenames),
        )

    def _render_attribute_page(self, task, dps_by_filename):
        def mock_get(url, *args, **kwargs):
            filename = url.rsplit('/', 1)[-1]
            return SimpleNamespace(
                status_code=200,
                text=f'<html><body>测试角色: {dps_by_filename[filename]:,} dps</body></html>',
            )

        with patch('botend.dashboard.dashboard.settings.OSS_CONFIG', {'base_url': 'https://oss.example/'}, create=True), \
             patch('requests.get', side_effect=mock_get):
            return self.client.get(f'/simc-attribute-analysis-ssr/?task_id={task.id}')

    def test_attribute_ssr_uses_chinese_labels_and_honest_delta(self):
        first = f'{self.user.id}_gear_crit_1000_gear_haste_2000.html'
        second = f'{self.user.id}_gear_crit_1050_gear_haste_1950.html'
        task = self._attribute_task('属性标签与差距', [first, second])

        response = self._render_attribute_page(task, {first: 100000, second: 101000})

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('暴击', html)
        self.assertIn('急速', html)
        self.assertIn('距最佳', html)
        self.assertNotIn('相对性能', html)
        self.assertNotIn('"attr1Name: "gear_crit"', html)
        self.assertNotIn('"attr2Name: "gear_haste"', html)

    def test_attribute_ssr_marks_near_optimal_and_fixed_budget(self):
        filenames = [
            f'{self.user.id}_gear_crit_1000_gear_haste_2000.html',
            f'{self.user.id}_gear_crit_1050_gear_haste_1950.html',
            f'{self.user.id}_gear_crit_1100_gear_haste_1900.html',
        ]
        task = self._attribute_task('近似最优', filenames)

        response = self._render_attribute_page(task, {
            filenames[0]: 100000,
            filenames[1]: 100150,
            filenames[2]: 100200,
        })

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('近似最优', html)
        self.assertIn('固定总量', html)
        self.assertIn('最高DPS', html)
        self.assertIn('差异极小', html)

    def test_result_templates_keep_discrete_attribute_chart_and_safe_proxy(self):
        with open('templates/simc_attribute_analysis_ssr.html', encoding='utf-8') as f:
            ssr = f.read()
        with open('templates/simc_attribute_analysis.html', encoding='utf-8') as f:
            client = f.read()
        with open('templates/simc_result_view.html', encoding='utf-8') as f:
            result = f.read()

        self.assertIn('budget_is_fixed', ssr)
        self.assertIn("type: 'scatter'", ssr)
        self.assertIn('showLine: false', ssr)
        self.assertIn('距最佳不超过', client)
        self.assertIn('maxDeltaPercent > 0 && deltaPercent > maxDeltaPercent', client)
        self.assertNotIn('最低相对性能', client)
        self.assertIn("replace(/<[^>]*>/g, '')", result)
        self.assertIn('结果摘要可用，但没有可识别的技能明细', result)
        self.assertIn('/api/simc-result-proxy/', result)
        self.assertNotIn('raw_simc_code', result)
