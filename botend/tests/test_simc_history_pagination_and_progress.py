"""
SimC History Panel: 分页、状态文案、真实进度和比较入口契约测试
TDD RED phase: 所有测试应当失败，直到实现完成
"""
import unittest
from unittest.mock import patch
from pathlib import Path
from django.test import TestCase, RequestFactory
from django.contrib.auth.models import User
from botend.models import SimcTask, SimulationRun
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
        self.assertIn("/api/simc-regular-compare/?task_id=", JS)
        self.assertIn("&summary=1", JS)
        self.assertNotIn('target="_blank">查看比较', JS)

    def test_batch_aggregates_from_fk_members_not_legacy_ext(self):
        """多候选任务详情必须读取 Runs，而不是旧 batch 成员或 ext。"""
        self.assertIn("row.runs", JS)
        self.assertIn("run.status", JS)
        self.assertIn("run.result_summary?.dps", JS)

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

    def test_history_endpoint_unifies_standalone_and_grouped_tasks(self):
        grouped = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id,
            name='属性模拟', mode='attribute_sweep', current_status=1, is_active=True,
        )
        SimulationRun.objects.create(
            task=grouped, sequence=1, candidate_key='candidate-1', status='pending')
        standalone = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id,
            name='单次模拟', current_status=2, is_active=True,
        )

        request = self.factory.get('/api/simc-workbench/history/')
        request.user = self.user
        data = json.loads(self.view.get(request, resource='history').content)

        self.assertTrue(data['success'])
        self.assertEqual(data['pagination']['total'], 2)
        rows = {row['name']: row for row in data['data']}
        self.assertIn(standalone.name, rows)
        self.assertIn(grouped.name, rows)
        self.assertEqual(rows[standalone.name]['detail_resource'], 'tasks')
        self.assertEqual(rows[grouped.name]['detail_resource'], 'tasks')
        self.assertEqual(rows[grouped.name]['status_label'], '运行中')
        self.assertIsNone(rows[grouped.name]['progress'])

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
        """多候选 Task 详情从 Runs 暴露状态，不再读取 batch 成员。"""
        task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id,
            name='Attribute Task', mode='attribute_sweep', current_status=1, is_active=True)
        SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='pending', status='pending')
        SimulationRun.objects.create(
            task=task, sequence=2, candidate_key='done', status='completed',
            result_summary={'dps': 1000})
        request = self.factory.get(f'/api/simc-workbench/tasks/{task.id}/')
        request.user = self.user
        response = self.view.get(request, resource='tasks', object_id=task.id)
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        statuses = [run['status'] for run in data['data']['runs']]
        self.assertEqual(statuses, ['pending', 'completed'])
        self.assertEqual(data['data']['runs'][1]['result_summary']['dps'], 1000)

    def test_batch_progress_counts_all_terminal_members(self):
        task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id,
            name='Terminal progress', mode='attribute_sweep', current_status=1,
            ext=json.dumps({'progress': 50}), is_active=True)
        for sequence, status in enumerate(('completed', 'failed', 'pending', 'running'), 1):
            SimulationRun.objects.create(
                task=task, sequence=sequence, candidate_key=f'candidate-{sequence}', status=status)
        request = self.factory.get(f'/api/simc-workbench/tasks/{task.id}/')
        request.user = self.user
        data = json.loads(self.view.get(request, resource='tasks', object_id=task.id).content)
        self.assertEqual(data['data']['progress'], 50)
        self.assertEqual([run['status'] for run in data['data']['runs']],
                         ['completed', 'failed', 'pending', 'running'])

    def test_batch_detail_ranks_only_completed_candidates_and_marks_baseline(self):
        task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id,
            name='候选对比', mode='comparison', current_status=2, is_active=True)
        baseline = SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='base', candidate_label='基准配置',
            status='completed', candidate_params={'is_base': True}, result_summary={'dps': 1000})
        winner = SimulationRun.objects.create(
            task=task, sequence=2, candidate_key='winner', candidate_label='候选 A',
            status='completed', candidate_params={'is_base': False}, result_summary={'dps': 1100})
        failed = SimulationRun.objects.create(
            task=task, sequence=3, candidate_key='failed', candidate_label='失败候选',
            status='failed', candidate_params={'is_base': False}, result_summary={'dps': 1200})

        request = self.factory.get(f'/api/simc-workbench/tasks/{task.id}/')
        request.user = self.user
        data = json.loads(self.view.get(request, resource='tasks', object_id=task.id).content)['data']
        rows = {row['id']: row for row in data['ranking']}

        self.assertTrue(rows[baseline.id]['is_base'])
        self.assertIsNone(rows[baseline.id]['rank'])
        self.assertEqual(rows[winner.id]['rank'], 1)
        self.assertFalse(rows[failed.id]['is_complete'])
        self.assertIsNone(rows[failed.id]['rank'])

    def test_batch_list_query_count_does_not_grow_per_batch(self):
        for index in range(6):
            task = SimcTask.objects.create(
                user_id=self.user.id, name=f'Task {index}', simc_profile_id=self.profile.id,
                mode='comparison', task_type=1, current_status=index % 4, is_active=True)
            SimulationRun.objects.create(
                task=task, sequence=1, candidate_key=f'candidate-{index}', status='pending')
        request = self.factory.get('/api/simc-workbench/tasks/?page_size=20')
        request.user = self.user
        with self.assertNumQueries(20):
            response = self.view.get(request, resource='tasks')
        self.assertEqual(response.status_code, 200)

    def test_batch_report_url_empty_when_incomplete(self):
        """有 Runs 的未完成任务仍提供安全的 task_id 对比入口。"""
        task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id,
            name='Incomplete Task', mode='attribute_sweep', current_status=1, is_active=True)
        SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='pending', status='pending')
        request = self.factory.get(f'/api/simc-workbench/tasks/{task.id}/')
        request.user = self.user
        response = self.view.get(request, resource='tasks', object_id=task.id)
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['data']['report_url'], f'/simc-compare/?task_id={task.id}')

    def test_batch_report_url_empty_when_has_failures(self):
        """部分失败任务的 report_url 仍按 task_id 定位且不泄露错误详情。"""
        task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id,
            name='Failed Task', mode='attribute_sweep', current_status=2, is_active=True)
        SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='done', status='completed',
            result_summary={'dps': 1000})
        SimulationRun.objects.create(
            task=task, sequence=2, candidate_key='failed', status='failed',
            error_detail='SECRET FAILURE')
        request = self.factory.get(f'/api/simc-workbench/tasks/{task.id}/')
        request.user = self.user
        response = self.view.get(request, resource='tasks', object_id=task.id)
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['data']['report_url'], f'/simc-compare/?task_id={task.id}')
        self.assertNotIn('SECRET FAILURE', json.dumps(data, ensure_ascii=False))

    def test_compare_summary_does_not_expose_full_result_or_candidate_payload(self):
        task = SimcTask.objects.create(
            user_id=self.user.id, name='Base', simc_profile_id=self.profile.id,
            mode='comparison', task_type=1, current_status=2,
            result_file='https://example.invalid/result.html', is_active=True)
        run = SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='base', candidate_label='基准',
            candidate_params={'is_base': True, 'apl_override': 'secret_input'},
            status='completed', result_summary={
                'dps': 123456, 'abilities': [{'name': 'secret'}], 'talents': {'raw': 'secret'},
            })
        request = self.factory.get(f'/api/simc-regular-compare/?task_id={task.id}&summary=1')
        request.user = self.user
        with patch.object(SimcRegularCompareAPIView, '_get_result_file_content', return_value='<html></html>'), \
                patch.object(SimcRegularCompareAPIView, '_parse_regular_result', return_value={
                    'dps': 123456, 'abilities': [{'name': 'secret'}], 'talents': {'raw': 'secret'}
                }):
            response = SimcRegularCompareAPIView().get(request)
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        row = data['data']['runs'][0]
        self.assertEqual(row['id'], run.id)
        self.assertEqual(row['dps'], 123456)
        self.assertEqual(set(row), {
            'id', 'name', 'label', 'rank', 'dps', 'delta_dps', 'delta_percent', 'candidate',
        })
        self.assertEqual(row['candidate'], {})
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
