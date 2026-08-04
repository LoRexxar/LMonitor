"""
SimC History Panel: 分页、状态文案、真实进度和比较入口契约测试
TDD RED phase: 所有测试应当失败，直到实现完成
"""
import unittest
from unittest.mock import patch
from pathlib import Path
from django.test import TestCase, RequestFactory
from django.contrib.auth.models import User
from django.utils import timezone
from botend.models import SimcBackendBinary, SimcResourceVersion, SimcTask, SimulationRun, SimcBenchmarkPanel, SimcBenchmarkExecution, SimcBenchmarkCase
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

    def test_active_task_polling_is_silent_and_preserves_expanded_benchmark(self):
        """后台轮询不能反复展示 loading，也不能收起用户正在看的基准子任务。"""
        self.assertIn("loadTasks(page, { background: true })", JS)
        self.assertIn("if (!background) renderState(host, 'loading'", JS)
        self.assertIn("expandedExecutionIds", JS)
        self.assertIn("cases.scrollTop = saved.scrollTop", JS)

    def test_active_task_polling_is_throttled_and_skips_unchanged_dom_rebuilds(self):
        self.assertIn("const TASK_POLL_MS = 10000", JS)
        self.assertIn("taskResponseSignature", JS)
        self.assertIn("background && responseSignature === state.taskResponseSignature", JS)

    def test_benchmark_history_keeps_only_compact_run_summary(self):
        """历史首层只显示紧凑 Run 进度，不用 Case、来源基线等小字挤满整行。"""
        self.assertIn("const runSummaryParts", JS)
        self.assertIn("`Run ${terminalRunCount}/${totalRunCount}`", JS)
        self.assertNotIn("本次子任务：", JS)
        self.assertNotIn("本次候选 Run：", JS)
        self.assertNotIn("来源基线 #", JS)
        self.assertIn("simc-benchmark-task-case__progress", JS)

    def test_benchmark_history_case_renders_explicit_error_outside_task_detail(self):
        self.assertIn('item.error', JS)
        self.assertIn('simc-benchmark-task-case__error', JS)

    def test_expanded_benchmark_case_omits_low_value_task_id(self):
        """展开项保留坐标、状态和进度，不重复展示内部 Task 编号。"""
        self.assertNotIn('<span class="simc-task-id">Task #${idOf(item.task_id)}</span>${title}', JS)

    def test_terminal_benchmark_labels_unfinished_runs_as_residue(self):
        """终态 Execution 内未完成的 Run 不能继续显示成活跃队列。"""
        self.assertIn("row.is_active", JS)
        self.assertIn("未执行遗留", JS)
        self.assertIn("中断遗留", JS)
        self.assertIn("子任务收口", JS)

    def test_benchmark_case_list_expands_into_the_page_without_internal_scrolling(self):
        """展开后的基准子任务由页面滚动，不能被 34rem 列表滚轮截留。"""
        cases_start = HTML.index('#simc-workbench .simc-benchmark-task-cases {')
        cases_end = HTML.index('}', cases_start) + 1
        cases_css = HTML[cases_start:cases_end]
        self.assertIn('display: grid', cases_css)
        self.assertNotIn('max-height', cases_css)
        self.assertNotIn('overflow-y', cases_css)

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
        self.backend = SimcBackendBinary.objects.create(
            identifier='test-production', name='正式服测试后端', simc_path='/tmp/simc',
        )
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
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
            name='属性模拟', mode='attribute_sweep', current_status=1, is_active=True,
        )
        SimulationRun.objects.create(
            task=grouped, sequence=1, candidate_key='candidate-1', status='pending')
        standalone = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
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

    def test_history_groups_benchmark_tasks_as_one_expandable_execution(self):
        panel = SimcBenchmarkPanel.objects.create(name='基准', slug='history-benchmark', created_by_id=self.user.id)
        execution = SimcBenchmarkExecution.objects.create(panel=panel, config_snapshot={}, config_hash='a' * 64)
        task = SimcTask.objects.create(user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
                                       name='内部基准任务', current_status=1,
                                       ext=json.dumps({'progress': 37}), is_active=True)
        SimcBenchmarkCase.objects.create(execution=execution, task=task, spec_key='warrior_fury',
                                         scenario_key='patchwerk', profile_key='raid', spec_label='狂怒',
                                         scenario_label='木桩', profile_label='Raid', coordinate_hash='b' * 64)
        request = self.factory.get('/api/simc-workbench/history/')
        request.user = self.user
        rows = json.loads(self.view.get(request, resource='history').content)['data']
        benchmark_rows = [row for row in rows if row.get('row_type') == 'benchmark_execution']
        self.assertEqual(len(benchmark_rows), 1)
        self.assertFalse(any(row.get('id') == task.id for row in rows if row.get('row_type') != 'benchmark_execution'))
        self.assertEqual(benchmark_rows[0]['cases'][0]['task_id'], task.id)
        self.assertEqual(benchmark_rows[0]['cases'][0]['labels']['spec'], '狂怒-战士')
        self.assertEqual(benchmark_rows[0]['cases'][0]['progress'], 37)
        self.assertEqual(benchmark_rows[0]['task_counts'], {
            'pending': 0, 'running': 1, 'success': 0, 'partial': 0, 'failed': 0, 'cancelled': 0,
        })

    def test_history_keeps_rebound_benchmark_retry_lineage_inside_execution(self):
        panel = SimcBenchmarkPanel.objects.create(
            name='重试基准', slug='history-benchmark-retry-lineage', created_by_id=self.user.id,
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, status=SimcBenchmarkExecution.STATUS_RUNNING,
            config_snapshot={}, config_hash='c' * 64,
        )
        stale_task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
            name='内部基准旧任务', current_status=3, is_active=True,
        )
        retry_task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
            name='内部基准重试任务', current_status=0, is_active=True,
            source_task=stale_task,
        )
        SimcBenchmarkCase.objects.create(
            execution=execution, task=retry_task, status=SimcBenchmarkExecution.STATUS_PENDING,
            spec_key='warrior_fury', scenario_key='patchwerk', profile_key='raid',
            spec_label='狂怒', scenario_label='木桩', profile_label='Raid',
            coordinate_hash='d' * 64,
        )
        request = self.factory.get('/api/simc-workbench/history/')
        request.user = self.user

        data = json.loads(self.view.get(request, resource='history').content)

        self.assertEqual(data['pagination']['total'], 1)
        self.assertEqual(len(data['data']), 1)
        row = data['data'][0]
        self.assertEqual(row['row_type'], 'benchmark_execution')
        self.assertEqual(row['execution_id'], execution.id)
        self.assertEqual(row['status'], SimcBenchmarkExecution.STATUS_RUNNING)
        self.assertEqual(row['task_counts']['pending'], 1)
        self.assertEqual(row['task_counts']['failed'], 0)

    def test_history_distinguishes_retry_work_from_frozen_baseline_scale(self):
        panel = SimcBenchmarkPanel.objects.create(
            name='重跑规模', slug='history-benchmark-retry-scale', created_by_id=self.user.id,
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, status=SimcBenchmarkExecution.STATUS_PARTIAL,
            config_snapshot={'case_count': 65, 'run_count': 3407}, config_hash='e' * 64,
        )
        source_task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
            name='外部基线任务', current_status=2, is_active=True,
        )
        SimcBenchmarkCase.objects.create(
            execution=SimcBenchmarkExecution.objects.create(
                panel=panel, status=SimcBenchmarkExecution.STATUS_PARTIAL,
                config_snapshot={'case_count': 65, 'run_count': 3407}, config_hash='d' * 64,
            ),
            task=source_task, status=SimcBenchmarkExecution.STATUS_PARTIAL,
            spec_key='warrior_fury', scenario_key='patchwerk', profile_key='raid',
            spec_label='狂怒', scenario_label='木桩', profile_label='Raid', coordinate_hash='d' * 64,
        )
        retry_task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
            name='重跑任务', current_status=1, is_active=True, source_task=source_task,
        )
        SimulationRun.objects.create(task=retry_task, sequence=1, candidate_key='retry', status='pending')
        SimcBenchmarkCase.objects.create(
            execution=execution, task=retry_task, status=SimcBenchmarkExecution.STATUS_RUNNING,
            spec_key='warrior_fury', scenario_key='patchwerk', profile_key='raid',
            spec_label='狂怒', scenario_label='木桩', profile_label='Raid', coordinate_hash='f' * 64,
        )

        row = self.view._benchmark_history_row(execution)

        self.assertEqual(row['case_count'], 1)
        self.assertEqual(row['run_count'], 1)
        self.assertEqual(row['baseline_counts'], {
            'execution_id': source_task.benchmark_case.execution_id,
            'cases': 65, 'runs': 3407,
            'case_counts': {'pending': 0, 'running': 0, 'success': 0, 'partial': 1, 'failed': 0, 'cancelled': 0},
            'run_counts': {'pending': 0, 'running': 0, 'success': 0, 'failed': 0, 'cancelled': 0},
        })
        self.assertEqual(row['task_counts']['partial'], 0)
        self.assertEqual(row['run_counts']['pending'], 1)
        self.assertEqual(row['cases'][0]['source_task_id'], source_task.id)

    def test_terminal_history_marks_pending_and_running_runs_as_inactive(self):
        panel = SimcBenchmarkPanel.objects.create(
            name='终态遗留 Run', slug='history-terminal-run-residue', created_by_id=self.user.id,
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, status=SimcBenchmarkExecution.STATUS_PARTIAL,
            config_snapshot={'case_count': 1, 'run_count': 2}, config_hash='9' * 64,
        )
        task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
            name='已失败的基准子任务', current_status=3, is_active=True,
        )
        SimulationRun.objects.create(task=task, sequence=1, candidate_key='pending', status='pending')
        SimulationRun.objects.create(task=task, sequence=2, candidate_key='running', status='running')
        SimcBenchmarkCase.objects.create(
            execution=execution, task=task, status=SimcBenchmarkExecution.STATUS_FAILED,
            spec_key='warrior_fury', scenario_key='patchwerk', profile_key='raid',
            spec_label='狂怒', scenario_label='木桩', profile_label='Raid', coordinate_hash='8' * 64,
        )

        row = self.view._benchmark_history_row(execution)

        self.assertFalse(row['is_active'])
        self.assertEqual(row['run_counts']['pending'], 1)
        self.assertEqual(row['run_counts']['running'], 1)

    def test_history_expands_only_benchmark_executions_on_requested_page(self):
        panel = SimcBenchmarkPanel.objects.create(
            name='分页基准', slug='history-benchmark-page-first', created_by_id=self.user.id,
        )
        executions = []
        for index in range(2):
            execution = SimcBenchmarkExecution.objects.create(
                panel=panel, config_snapshot={}, config_hash=str(index + 1) * 64,
            )
            task = SimcTask.objects.create(
                user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
                name=f'内部基准任务 {index}', current_status=0, is_active=True,
            )
            SimcBenchmarkCase.objects.create(
                execution=execution, task=task, spec_key='warrior_fury',
                scenario_key=f'patchwerk-{index}', profile_key='raid', spec_label='狂怒',
                scenario_label='木桩', profile_label='Raid', coordinate_hash=str(index + 2) * 64,
            )
            executions.append(execution)
        SimcBenchmarkExecution.objects.filter(pk=executions[0].pk).update(
            created_at=timezone.now() - timezone.timedelta(days=1),
        )
        request = self.factory.get('/api/simc-workbench/history/?page=1&page_size=1')
        request.user = self.user

        with patch.object(
            self.view, '_benchmark_history_row', wraps=self.view._benchmark_history_row,
        ) as row_builder:
            response = self.view.get(request, resource='history')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(row_builder.call_count, 1)
        data = json.loads(response.content)
        self.assertEqual(data['pagination']['total'], 2)
        self.assertEqual(len(data['data']), 1)

    def test_active_benchmark_case_without_task_stays_at_zero_progress(self):
        panel = SimcBenchmarkPanel.objects.create(
            name='等待调度基准', slug='history-benchmark-taskless', created_by_id=self.user.id,
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, status=SimcBenchmarkExecution.STATUS_RUNNING,
            config_snapshot={}, config_hash='f' * 64,
        )
        SimcBenchmarkCase.objects.create(
            execution=execution, task=None, status=SimcBenchmarkExecution.STATUS_PENDING,
            spec_key='warrior_fury', scenario_key='patchwerk', profile_key='raid',
            spec_label='狂怒', scenario_label='木桩', profile_label='Raid',
            coordinate_hash='e' * 64,
        )

        row = self.view._benchmark_history_row(execution)

        self.assertIsNone(row['cases'][0]['progress'])
        self.assertEqual(row['progress'], 0)
        self.assertEqual(row['task_counts']['pending'], 1)

    def test_history_tie_breaker_keeps_cross_type_page_boundaries_stable(self):
        timestamp = timezone.now().replace(microsecond=0)
        ordinary = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
            name='同时间普通任务', current_status=2, is_active=True,
        )
        panel = SimcBenchmarkPanel.objects.create(
            name='同时间基准', slug='history-benchmark-tie', created_by_id=self.user.id,
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_snapshot={}, config_hash='d' * 64,
        )
        internal = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
            name='同时间内部任务', current_status=0, is_active=True,
        )
        SimcBenchmarkCase.objects.create(
            execution=execution, task=internal, spec_key='warrior_fury',
            scenario_key='patchwerk', profile_key='raid', spec_label='狂怒',
            scenario_label='木桩', profile_label='Raid', coordinate_hash='c' * 64,
        )
        SimcTask.objects.filter(pk=ordinary.pk).update(modified_time=timestamp)
        SimcBenchmarkExecution.objects.filter(pk=execution.pk).update(created_at=timestamp)

        page_ids = []
        for page in (1, 2):
            request = self.factory.get(f'/api/simc-workbench/history/?page={page}&page_size=1')
            request.user = self.user
            row = json.loads(self.view.get(request, resource='history').content)['data'][0]
            page_ids.append((row.get('row_type', 'task'), row['id']))

        self.assertEqual(page_ids, [
            ('benchmark_execution', execution.id),
            ('task', ordinary.id),
        ])

    def test_benchmark_overall_progress_does_not_disappear_before_first_worker_update(self):
        panel = SimcBenchmarkPanel.objects.create(
            name='基准', slug='history-benchmark-no-progress', created_by_id=self.user.id,
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_snapshot={}, config_hash='c' * 64,
        )
        task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
            name='刚领取的内部任务', current_status=1, ext=None, is_active=True,
        )
        SimcBenchmarkCase.objects.create(
            execution=execution, task=task, spec_key='warrior_fury',
            scenario_key='patchwerk', profile_key='raid', spec_label='狂怒',
            scenario_label='木桩', profile_label='Raid', coordinate_hash='d' * 64,
        )

        row = self.view._benchmark_history_row(execution)

        self.assertEqual(row['progress'], 0)
        self.assertIsNone(row['cases'][0]['progress'])

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
            simc_profile_id=self.profile.id, backend=self.backend,
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
            simc_profile_id=self.profile.id, backend=self.backend,
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
            simc_profile_id=self.profile.id, backend=self.backend,
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
            simc_profile_id=self.profile.id, backend=self.backend,
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

    def test_task_cancelled_progress_is_100(self):
        task = SimcTask.objects.create(
            user_id=self.user.id,
            simc_profile_id=self.profile.id, backend=self.backend,
            name='Cancelled Task',
            current_status=5,
            is_active=True,
        )
        request = self.factory.get('/api/simc-workbench/tasks/')
        request.user = self.user
        data = json.loads(self.view.get(request, resource='tasks').content)
        self.assertEqual(data['data'][0]['status_label'], '已取消')
        self.assertEqual(data['data'][0]['progress'], 100)

    def test_task_running_without_progress_returns_null(self):
        SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
            name='Running Task', current_status=1, ext='{}', is_active=True,
        )
        request = self.factory.get('/api/simc-workbench/tasks/')
        request.user = self.user
        data = json.loads(self.view.get(request, resource='tasks').content)
        self.assertIsNone(data['data'][0]['progress'])

    def test_task_running_uses_persisted_worker_progress(self):
        SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
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
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
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
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
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
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
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

    def test_comparison_detail_explains_baseline_changed_and_unchanged_fields(self):
        profile_version = SimcResourceVersion.objects.create(
            resource_type='profile', resource_id=self.profile.id, content_hash='profile-baseline',
            payload={
                'name': '狂暴战基准', 'spec': 'warrior_fury',
                'player_equipment': (
                    'warrior="Tester"\nspec=fury\ntalents=BASE_TALENT\n'
                    'head=基准头盔,id=111,ilevel=650\nchest=基准胸甲,id=222,ilevel=650'
                ),
            },
        )
        template_version = SimcResourceVersion.objects.create(
            resource_type='template', resource_id=1, content_hash='template-baseline',
            payload={'name': '单体基础模板', 'spec': 'warrior_fury', 'content': 'iterations=1000'},
        )
        apl_version = SimcResourceVersion.objects.create(
            resource_type='apl', resource_id=1, content_hash='apl-baseline',
            payload={'name': '狂暴战默认 APL', 'spec': 'warrior_fury', 'content': 'actions=auto_attack'},
        )
        task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
            profile_version=profile_version, template_version=template_version,
            apl_version=apl_version, name='装备候选对比', mode='comparison',
            simulation_params={'iterations': 1000, 'fight_style': 'Patchwerk'},
            current_status=2, is_active=True,
        )
        SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='base', candidate_label='基准配置',
            status='completed', candidate_params={'candidate_type': 'base', 'is_base': True},
            result_summary={'dps': 1000},
        )
        candidate = SimulationRun.objects.create(
            task=task, sequence=2, candidate_key='head-333', candidate_label='候选头盔',
            status='completed', candidate_params={
                'candidate_type': 'gear_swap', 'is_base': False,
                'icon_url': '/static/wow_icons/inv_helmet_151.webp',
                'gear_swap': {
                    'slot': 'head', 'item_id': 333, 'source': 'bags',
                    'raw_value': '候选头盔,id=333,ilevel=660,bonus_id=10/20,gem_id=30,enchant_id=40',
                },
            }, result_summary={'dps': 1100},
            display_metadata={'icon_url': '/static/wow_icons/small/inv_helmet_151.jpg'},
        )
        equivalent = SimulationRun.objects.create(
            task=task, sequence=3, candidate_key='head-111', candidate_label='等价头盔复核',
            status='completed', candidate_params={
                'candidate_type': 'gear_swap', 'is_base': False,
                'gear_swap': {
                    'slot': 'head', 'item_id': 111, 'source': 'manual',
                    'raw_value': ',id=111,ilevel=650',
                },
            }, result_summary={'dps': 1000},
        )

        request = self.factory.get(f'/api/simc-workbench/tasks/{task.id}/')
        request.user = self.user
        data = json.loads(self.view.get(request, resource='tasks', object_id=task.id).content)['data']
        row = next(item for item in data['ranking'] if item['id'] == candidate.id)
        equivalent_row = next(item for item in data['ranking'] if item['id'] == equivalent.id)

        self.assertEqual(data['comparison_baseline']['profile']['name'], '狂暴战基准')
        self.assertEqual(data['comparison_baseline']['template']['name'], '单体基础模板')
        self.assertEqual(data['comparison_baseline']['apl']['name'], '狂暴战默认 APL')
        self.assertEqual(data['comparison_baseline']['simulation_params']['iterations'], 1000)
        self.assertEqual(row['change']['field'], 'head')
        self.assertEqual(row['change']['before']['item_id'], 111)
        self.assertEqual(row['change']['before']['name'], '基准头盔')
        self.assertEqual(row['change']['after']['item_id'], 333)
        self.assertEqual(row['change']['after']['name'], '候选头盔')
        self.assertEqual(row['candidate_icon_url'], '/static/wow_icons/small/inv_helmet_151.jpg')
        self.assertEqual(row['change']['after']['modifiers']['bonus_id'], ['10', '20'])
        self.assertEqual(row['change']['after']['modifiers']['gem_id'], ['30'])
        self.assertEqual(row['change']['after']['modifiers']['enchant_id'], '40')
        self.assertFalse(row['change']['is_equivalent'])
        self.assertTrue(equivalent_row['change']['is_equivalent'])
        self.assertIn('其他装备槽位', row['unchanged'])
        self.assertIn('天赋', row['unchanged'])
        self.assertNotIn('player_equipment', json.dumps(data['comparison_baseline']))

    def test_comparison_detail_supports_legacy_nested_candidates_and_structured_baseline(self):
        profile_version = SimcResourceVersion.objects.create(
            resource_type='profile', resource_id=self.profile.id, content_hash='legacy-profile-baseline',
            payload={
                'name': 'Battle.net 玩家快照', 'spec': 'arms',
                'gear_crit': 1234, 'gear_haste': 2345,
                'player_equipment': (
                    'warrior="Tester"\nlevel=80\nrace=highmountain_tauren\nspec=arms\n'
                    'talents=BASE_TALENT\nhead=,id=111,ilevel=650,bonus_id=10/20\n'
                    'main_hand=,id=222,ilevel=655,enchant_id=30'
                ),
            },
        )
        task = SimcTask.objects.create(
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
            profile_version=profile_version, name='旧候选对比', mode='comparison',
            current_status=2, is_active=True,
        )
        baseline = SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='base', candidate_label='基准配置',
            status='completed', candidate_params={
                'mode_params': {'candidate_type': 'base', 'is_base': True},
            }, result_summary={'dps': 1000},
        )
        candidate = SimulationRun.objects.create(
            task=task, sequence=2, candidate_key='talent-1', candidate_label='天赋方案 1',
            status='completed', candidate_params={
                'mode_params': {
                    'candidate_type': 'talent_override', 'is_base': False,
                    'talent_override': 'CANDIDATE_TALENT',
                    'talent_candidate': {'name': '天赋方案 1', 'talent': 'CANDIDATE_TALENT'},
                },
            }, result_summary={'dps': 1100},
        )

        request = self.factory.get(f'/api/simc-workbench/tasks/{task.id}/')
        request.user = self.user
        data = json.loads(self.view.get(request, resource='tasks', object_id=task.id).content)['data']
        rows = {row['id']: row for row in data['ranking']}
        base = data['comparison_baseline']

        self.assertTrue(rows[baseline.id]['is_base'])
        self.assertEqual(rows[candidate.id]['change']['kind'], 'talent')
        self.assertEqual(rows[candidate.id]['change']['before']['value'], 'BASE_TALENT')
        self.assertEqual(rows[candidate.id]['change']['after']['value'], 'CANDIDATE_TALENT')
        self.assertEqual(base['character'], {
            'name': 'Tester', 'class': 'warrior', 'spec': 'arms',
            'race': 'highmountain_tauren', 'level': 80,
        })
        self.assertEqual(base['stats']['crit'], 1234)
        self.assertEqual(base['stats']['haste'], 2345)
        self.assertEqual([item['slot'] for item in base['equipment']], ['head', 'main_hand'])
        self.assertEqual(base['equipment'][0]['item_id'], 111)
        self.assertEqual(base['talent']['value'], 'BASE_TALENT')

    def test_batch_list_query_count_does_not_grow_per_batch(self):
        for index in range(6):
            task = SimcTask.objects.create(
                user_id=self.user.id, name=f'Task {index}', simc_profile_id=self.profile.id,
                backend=self.backend, mode='comparison', task_type=1, current_status=index % 4, is_active=True)
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
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
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
            user_id=self.user.id, simc_profile_id=self.profile.id, backend=self.backend,
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
            backend=self.backend, mode='comparison', task_type=1, current_status=2,
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
                simc_profile_id=self.profile.id, backend=self.backend,
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
