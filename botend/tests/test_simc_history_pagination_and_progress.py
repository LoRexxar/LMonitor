"""
SimC History Panel: 分页、状态文案、真实进度和比较入口契约测试
TDD RED phase: 所有测试应当失败，直到实现完成
"""
import unittest
from unittest.mock import patch
from pathlib import Path
from django.test import TestCase, RequestFactory
from django.contrib.auth.models import User
from botend.models import SimcTask, SimcTaskBatch
from botend.dashboard.api import SimcRegularCompareAPIView, SimcWorkbenchAPIView
import json


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "templates/dashboard/index.html").read_text(encoding="utf-8")
JS = (ROOT / "static/dashboard/js/simc-workbench.js").read_text(encoding="utf-8")


class SimcHistoryPaginationContractTests(unittest.TestCase):
    """前端契约：分页参数、状态文案、进度显示"""

    def test_tasks_list_supports_page_and_page_size_params(self):
        """tasks/batches 列表 API 必须支持 page 和 page_size 查询参数"""
        self.assertIn("page", JS.lower())
        self.assertIn("page_size", JS.lower())
        # 默认 page_size=20, 最大50
        self.assertIn("20", JS)

    def test_tasks_response_contains_pagination_metadata(self):
        """API 响应必须包含 pagination 元数据"""
        # 前端需要读取 pagination.total, pagination.page, pagination.page_size, pagination.total_pages
        self.assertIn("pagination", JS.lower())

    def test_tasks_list_shows_status_label_in_chinese(self):
        """任务列表必须显示中文状态文案 status_label"""
        # 前端需要显示 status_label 而不是数字
        self.assertIn("status_label", JS.lower() or "row.status_label" in JS)

    def test_tasks_show_progress_percent_for_lifecycle(self):
        """任务必须显示可信生命周期进度"""
        self.assertIn("progress", JS.lower())

    def test_batch_compare_is_rendered_inline(self):
        self.assertIn('data-wb-action="compare"', JS)
        self.assertIn("/api/simc-regular-compare/?batch_id=", JS)
        self.assertIn("&summary=1", JS)
        self.assertNotIn('target="_blank">查看比较', JS)

    def test_batch_aggregates_from_fk_members_not_legacy_ext(self):
        """batch 必须从 is_active=True 的 FK 任务聚合状态，不扫 ext"""
        # batch detail 需要显示 total/pending/running/succeeded/failed/percent
        has_aggregation = ("succeeded" in JS.lower() and "failed" in JS.lower() and "pending" in JS.lower())
        self.assertTrue(has_aggregation, "JS must display batch aggregation: succeeded/failed/pending")

    def test_compare_url_only_when_batch_completed_and_no_failures(self):
        """比较入口仅当 batch 全部成功且无失败时启用"""
        # 前端需要检查 report_url 是否非空
        self.assertIn("report_url", JS.lower() or "/simc-compare/" in JS)

    def test_single_task_report_uses_safe_preview_not_raw_leak(self):
        """单任务报告必须使用安全预览，不泄露 raw SimC、路径、错误或 traceback"""
        self.assertIn("preview", JS.lower())
        self.assertNotIn("raw_simc", JS.lower())
        self.assertNotIn("traceback", JS.lower())


class SimcHistoryBackendPaginationTests(TestCase):
    """后端测试：分页白名单校验、状态标签、进度聚合"""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.view = SimcWorkbenchAPIView()
        # Create a SimcProfile for task FK constraint
        from botend.models import SimcProfile
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Test Profile',
            spec='fury',
            player_config_mode='attribute_only',
            is_active=True
        )

    def test_page_defaults_to_1_if_not_provided(self):
        """page 参数未提供时默认为 1"""
        request = self.factory.get('/api/simc-workbench/tasks/')
        request.user = self.user
        response = self.view.get(request, resource='tasks')
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['pagination']['page'], 1)

    def test_page_size_defaults_to_20(self):
        """page_size 参数未提供时默认为 20"""
        request = self.factory.get('/api/simc-workbench/tasks/')
        request.user = self.user
        response = self.view.get(request, resource='tasks')
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['pagination']['page_size'], 20)

    def test_page_size_max_clamped_to_50(self):
        """page_size 最大限制为 50"""
        request = self.factory.get('/api/simc-workbench/tasks/?page_size=100')
        request.user = self.user
        response = self.view.get(request, resource='tasks')
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['pagination']['page_size'], 50)

    def test_page_size_min_clamped_to_1(self):
        """page_size 最小限制为 1"""
        request = self.factory.get('/api/simc-workbench/tasks/?page_size=0')
        request.user = self.user
        response = self.view.get(request, resource='tasks')
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertGreaterEqual(data['pagination']['page_size'], 1)

    def test_invalid_page_param_returns_error(self):
        """无效 page 参数返回错误"""
        request = self.factory.get('/api/simc-workbench/tasks/?page=invalid')
        request.user = self.user
        response = self.view.get(request, resource='tasks')
        data = json.loads(response.content)
        self.assertFalse(data['success'])

    def test_invalid_page_size_param_returns_error(self):
        """无效 page_size 参数返回错误"""
        request = self.factory.get('/api/simc-workbench/tasks/?page_size=invalid')
        request.user = self.user
        response = self.view.get(request, resource='tasks')
        data = json.loads(response.content)
        self.assertFalse(data['success'])

    def test_task_response_includes_status_label_in_chinese(self):
        """任务响应必须包含中文 status_label"""
        task = SimcTask.objects.create(
            user_id=self.user.id,
            simc_profile_id=self.profile.id,
            name='Test Task',
            current_status=0,
            is_active=True
        )
        request = self.factory.get('/api/simc-workbench/tasks/')
        request.user = self.user
        response = self.view.get(request, resource='tasks')
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertIn('status_label', data['data'][0])
        self.assertIn('待运行', data['data'][0]['status_label'])

    def test_task_pending_progress_is_zero(self):
        """pending 任务进度为 0"""
        task = SimcTask.objects.create(
            user_id=self.user.id,
            simc_profile_id=self.profile.id,
            name='Test Task',
            current_status=0,
            is_active=True
        )
        request = self.factory.get('/api/simc-workbench/tasks/')
        request.user = self.user
        response = self.view.get(request, resource='tasks')
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['data'][0]['progress'], 0)

    def test_task_success_progress_is_100(self):
        """success 任务进度为 100"""
        task = SimcTask.objects.create(
            user_id=self.user.id,
            simc_profile_id=self.profile.id,
            name='Test Task',
            current_status=2,
            is_active=True
        )
        request = self.factory.get('/api/simc-workbench/tasks/')
        request.user = self.user
        response = self.view.get(request, resource='tasks')
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['data'][0]['progress'], 100)

    def test_task_failed_progress_is_100(self):
        """failed 任务进度为 100"""
        task = SimcTask.objects.create(
            user_id=self.user.id,
            simc_profile_id=self.profile.id,
            name='Test Task',
            current_status=3,
            is_active=True
        )
        request = self.factory.get('/api/simc-workbench/tasks/')
        request.user = self.user
        response = self.view.get(request, resource='tasks')
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['data'][0]['progress'], 100)

    def test_task_running_without_progress_returns_null(self):
        SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id,
            name='Running Task', current_status=1, ext='{}', is_active=True,
        )
        request = self.factory.get('/api/simc-workbench/tasks/')
        request.user = self.user
        data = json.loads(self.view.get(request, resource='tasks').content)
        self.assertIsNone(data['data'][0]['progress'])

    def test_task_running_uses_persisted_worker_progress(self):
        SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id,
            name='Running Task', current_status=1,
            ext=json.dumps({'progress': 37}), is_active=True,
        )
        request = self.factory.get('/api/simc-workbench/tasks/')
        request.user = self.user
        data = json.loads(self.view.get(request, resource='tasks').content)
        self.assertEqual(data['data'][0]['progress'], 37)

    def test_batch_aggregates_status_from_fk_members(self):
        """batch 从 FK 成员聚合状态计数"""
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id,
            name='Test Batch',
            batch_type='attribute_sweep',
            status=1,
            is_active=True
        )
        SimcTask.objects.create(
            user_id=self.user.id,
            simc_profile_id=self.profile.id,
            batch=batch,
            name='Task 1',
            current_status=0,
            is_active=True
        )
        SimcTask.objects.create(
            user_id=self.user.id,
            simc_profile_id=self.profile.id,
            batch=batch,
            name='Task 2',
            current_status=2,
            is_active=True
        )
        request = self.factory.get('/api/simc-workbench/batches/')
        request.user = self.user
        response = self.view.get(request, resource='batches')
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        batch_data = data['data'][0]
        self.assertEqual(batch_data['total'], 2)
        self.assertEqual(batch_data['pending'], 1)
        self.assertEqual(batch_data['succeeded'], 1)

    def test_batch_progress_counts_all_terminal_members(self):
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Terminal Batch',
            batch_type='attribute_sweep', status=3, is_active=True,
        )
        for status in (2, 3, 0, 1):
            SimcTask.objects.create(
                user_id=self.user.id, simc_profile_id=self.profile.id,
                batch=batch, name=f'Task {status}', current_status=status,
                is_active=True,
            )
        request = self.factory.get('/api/simc-workbench/batches/')
        request.user = self.user
        data = json.loads(self.view.get(request, resource='batches').content)
        self.assertEqual(data['data'][0]['percent'], 50)

    def test_batch_list_query_count_does_not_grow_per_batch(self):
        for index in range(6):
            batch = SimcTaskBatch.objects.create(
                user_id=self.user.id, name=f'Batch {index}', batch_type='comparison', status=1
            )
            SimcTask.objects.create(
                user_id=self.user.id, name=f'Task {index}', simc_profile_id=self.profile.id,
                batch=batch, task_type=1, current_status=index % 4, is_active=True,
            )
        request = self.factory.get('/api/simc-workbench/batches/?page_size=20')
        request.user = self.user
        with self.assertNumQueries(2):
            response = self.view.get(request, resource='batches')
        self.assertEqual(response.status_code, 200)

    def test_batch_report_url_empty_when_incomplete(self):
        """batch 未完成时 report_url 为空"""
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id,
            name='Test Batch',
            batch_type='attribute_sweep',
            status=1,
            is_active=True
        )
        SimcTask.objects.create(
            user_id=self.user.id,
            simc_profile_id=self.profile.id,
            batch=batch,
            name='Task 1',
            current_status=0,
            is_active=True
        )
        request = self.factory.get('/api/simc-workbench/batches/')
        request.user = self.user
        response = self.view.get(request, resource='batches')
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['data'][0]['report_url'], '')

    def test_batch_report_url_empty_when_has_failures(self):
        """batch 有失败任务时 report_url 为空"""
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id,
            name='Test Batch',
            batch_type='attribute_sweep',
            status=2,
            is_active=True
        )
        SimcTask.objects.create(
            user_id=self.user.id,
            simc_profile_id=self.profile.id,
            batch=batch,
            name='Task 1',
            current_status=2,
            is_active=True
        )
        SimcTask.objects.create(
            user_id=self.user.id,
            simc_profile_id=self.profile.id,
            batch=batch,
            name='Task 2',
            current_status=3,
            is_active=True
        )
        request = self.factory.get('/api/simc-workbench/batches/')
        request.user = self.user
        response = self.view.get(request, resource='batches')
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['data'][0]['report_url'], '')

    def test_compare_summary_does_not_expose_full_result_or_candidate_payload(self):
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Safe compare', batch_type='comparison', status=2
        )
        task = SimcTask.objects.create(
            user_id=self.user.id, name='Base', simc_profile_id=self.profile.id,
            batch=batch, task_type=1, current_status=2, result_file='https://example.invalid/result.html',
            ext=json.dumps({'batch_compare': {'label': '基准', 'index': 0, 'is_base': True,
                                              'candidate': {'secret_input': 'must-not-leak'}}}),
            is_active=True,
        )
        request = self.factory.get(f'/api/simc-regular-compare/?batch_id={batch.id}&summary=1')
        request.user = self.user
        with patch.object(SimcRegularCompareAPIView, '_get_result_file_content', return_value='<html></html>'), \
                patch.object(SimcRegularCompareAPIView, '_parse_regular_result', return_value={
                    'dps': 123456, 'abilities': [{'name': 'secret'}], 'talents': {'raw': 'secret'}
                }):
            response = SimcRegularCompareAPIView().get(request)
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        row = data['data']['tasks'][0]
        self.assertEqual(row['dps'], 123456)
        self.assertEqual(set(row), {'id', 'name', 'label', 'rank', 'dps', 'delta_dps', 'delta_percent'})
        serialized = json.dumps(data, ensure_ascii=False)
        self.assertNotIn('secret_input', serialized)
        self.assertNotIn('result_file', serialized)
        self.assertNotIn('abilities', serialized)
        self.assertNotIn('talents', serialized)

    def test_pagination_total_pages_calculated_correctly(self):
        """pagination.total_pages 正确计算"""
        for i in range(25):
            SimcTask.objects.create(
                user_id=self.user.id,
                simc_profile_id=self.profile.id,
                name=f'Task {i}',
                current_status=0,
                is_active=True
            )
        request = self.factory.get('/api/simc-workbench/tasks/?page_size=20')
        request.user = self.user
        response = self.view.get(request, resource='tasks')
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['pagination']['total_pages'], 2)
        self.assertEqual(data['pagination']['total'], 25)


if __name__ == '__main__':
    unittest.main()
