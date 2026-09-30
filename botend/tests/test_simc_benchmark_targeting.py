"""定向重跑的范围、权限、基准配对和原子替换回归。"""
from copy import deepcopy
from unittest.mock import patch
import json

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from botend.models import (DashboardUserGroup, DashboardUserGroupMembership,
    SimcBenchmarkCandidate, SimcBenchmarkExecution, SimcBenchmarkScenario, SimcTask, WowItemSnapshot)
from botend.services.simc_benchmark_config import build_execution_plan
from botend.services.simc_benchmark_execution import (BenchmarkExecutionConflict,
    reconcile_execution, reconcile_execution_case, rerun_failed_cases,
    serialize_incremental_panel_results, summarize_panel_coverage_counts,
    _reusable_candidate_tasks_by_coordinate)
from botend.services.simc_benchmark_targeting import rerun_options, rerun_preview, select_rerun_coordinates
from botend.services.simc_task_service import initialize_task_runs
from botend.tests import test_simc_benchmark_execution as fixtures
from simc_equipment_control import control_key


class SimcBenchmarkTargetedRerunTests(TestCase):
    user_id = 701
    _create = fixtures.SimcBenchmarkExecutionTests._create
    _run = fixtures.SimcBenchmarkExecutionTests._run

    def setUp(self):
        fixtures.SimcBenchmarkExecutionTests.setUp(self)
        for item_id, inventory in ((123,12),(456,11)):
            WowItemSnapshot.objects.create(item_id=item_id, item_class_id=4, inventory_type=inventory,
                item_subclass_id=0, metadata={'primary_stat_options':['strength']})
        candidate = self.panel.candidates.get(key='trinket')
        candidate.params['gear_swap'].update(item_level=321, raw_value=',id=123,ilevel=321')
        candidate.label = '测试饰品 · 321'
        candidate.save()
        params = deepcopy(candidate.params)
        params['gear_swap'].update(item_level=334, raw_value=',id=123,ilevel=334')
        SimcBenchmarkCandidate.objects.create(panel=self.panel, key='trinket-334', label='测试饰品 · 334', candidate_type='gear_swap', params=params)
        SimcBenchmarkCandidate.objects.create(panel=self.panel, key='ring', label='测试戒指', candidate_type='gear_swap', params={
            'candidate_type':'gear_swap','is_base':False,
            'gear_swap':{'slot':'finger1','raw_value':',id=456,ilevel=321','item_id':456,'item_level':321,'source':'manual'}})
        SimcBenchmarkScenario.objects.create(panel=self.panel, key='second', name='第二场景', simulation_params={'desired_targets':2})
        self.user = User.objects.create_user(id=self.user_id, username='定向重跑验收', is_staff=True)
        group = DashboardUserGroup.objects.create(name='定向重跑权限', permission_codes=['simc.benchmarks'])
        DashboardUserGroupMembership.objects.create(user=self.user, group=group)
        self.client.force_login(self.user)
        self.url = f'/api/simc-benchmarks/panels/{self.panel.pk}/run/'

    def options(self):
        return rerun_options(build_execution_plan(self.panel))

    def selection(self, label='测试饰品', one_case=False):
        options = self.options()
        return {'equipment_keys':[next(row['key'] for row in options['equipment'] if row['label']==label)],
                'coordinate_keys':[options['coordinates'][0]['key']] if one_case else []}

    def finish_case(self, case, dps=2000, fail=None):
        task = case.task
        for index, candidate in enumerate(task.mode_params['initial_candidates'],1):
            key = candidate['candidate_key']
            self._run(task,index,'failed' if key==fail else 'completed',key,dps=None if key==fail else dps+index)
        task.current_status = 3 if fail else 2
        task.save(update_fields=['current_status'])

    def finish(self, execution, dps=2000, fail=None):
        for case in execution.cases.select_related('task'):
            self.finish_case(case,dps,fail)
        reconcile_execution(execution)
        execution.refresh_from_db()

    def projection(self):
        return {(row['scenario_key'],c['key']):c['task_id'] for row in serialize_incremental_panel_results(self.panel)['coordinates'] for c in row['candidates']}

    def test_equipment_forces_all_levels_and_baseline_despite_existing_success(self):
        original = self._create(execution_mode='full')
        self.finish(original)
        target = self._create(execution_mode='targeted',selection=self.selection())
        self.assertEqual(target.config_snapshot['run_count'],6)
        for case in target.cases.select_related('task'):
            initialize_task_runs(case.task)
            self.assertEqual(set(case.task.simulation_runs.values_list('candidate_key',flat=True)),{'baseline','trinket','trinket-334'})
        self.assertEqual(original.cases.count(),2)
        self.assertEqual(target.config_snapshot['result_publication'],'atomic_targeted')

    def test_task_scope_and_nontrinket_control_are_preserved(self):
        selection = self.selection('测试戒指',True)
        target = self._create(execution_mode='targeted',selection=selection)
        self.assertEqual(target.cases.count(),1)
        self.assertEqual({row['candidate_key'] for row in target.cases.get().task.mode_params['initial_candidates']},
                         {'baseline','ring',control_key('ring')})
        self.assertEqual(target.config_snapshot['run_count'],3)

    def test_only_selected_tasks_are_created_with_all_candidates(self):
        options = self.options()
        self.assertIn(f'天赋：{self.talent.name}（#{self.talent.pk}）',options['coordinates'][1]['label'])
        target = self._create(execution_mode='targeted',selection={'coordinate_keys':[options['coordinates'][1]['key']]})
        self.assertEqual(target.cases.count(),1)
        self.assertEqual(target.config_snapshot['run_count'],5)

    def test_invalid_empty_stale_and_inapplicable_selection_creates_nothing(self):
        for selection in ({},{'equipment_keys':['unknown']},{'coordinate_keys':'bad'},{'extra':[]},None):
            with self.subTest(selection=selection), self.assertRaises(ValidationError):
                self._create(execution_mode='targeted',selection=selection)
        with self.assertRaises(BenchmarkExecutionConflict):
            self._create(execution_mode='targeted',selection=self.selection(),expected_plan_hash='0'*64)
        self.assertFalse(SimcTask.objects.exists())
        plan=build_execution_plan(self.panel)
        plan['cases'][0]['candidates']=[row for row in plan['cases'][0]['candidates'] if row['candidate_key']=='baseline']
        with self.assertRaises(ValidationError):
            select_rerun_coordinates(plan,self.selection(one_case=True))

    def test_active_execution_and_foreign_owner_are_rejected(self):
        from botend.services.simc_benchmark_execution import create_execution
        with self.assertRaises(PermissionDenied):
            create_execution(self.panel,requested_by=999,execution_mode='targeted',selection=self.selection())
        self._create()
        with self.assertRaises(BenchmarkExecutionConflict):
            self._create(execution_mode='targeted',selection=self.selection())
        self.assertEqual(SimcBenchmarkExecution.objects.count(),1)

    def test_new_results_replace_selected_equipment_only_after_whole_batch_succeeds(self):
        original = self._create(execution_mode='full')
        self.finish(original)
        old = self.projection()
        self.panel.refresh_from_db()
        boundary = self.panel.aggregate_baseline_execution_id
        target = self._create(execution_mode='targeted',selection=self.selection())
        cases=list(target.cases.select_related('task'))
        self.finish_case(cases[0],3000)
        reconcile_execution_case(target,cases[0].pk)
        self.assertEqual(self.projection(),old)
        self.assertNotIn(target.pk,{row['execution_id'] for row in summarize_panel_coverage_counts([self.panel])[self.panel.pk]['source_executions']})
        fast=_reusable_candidate_tasks_by_coordinate(self.panel,summary_only=True)
        self.assertNotIn(cases[0].task_id,{match['task_id'] for values in fast.values() for match in values.values()})
        self.finish_case(cases[1],3000)
        reconcile_execution(target)
        target.refresh_from_db()
        self.assertEqual(target.status,'success')
        new=self.projection()
        for coordinate,key in old:
            if key=='ring': self.assertEqual(new[(coordinate,key)],old[(coordinate,key)])
            else: self.assertNotEqual(new[(coordinate,key)],old[(coordinate,key)])
        self.panel.refresh_from_db()
        self.assertEqual(self.panel.aggregate_baseline_execution_id,boundary)

    def test_failed_targeted_batch_keeps_old_results_and_retry_rebuilds_whole_selection(self):
        self.finish(self._create(execution_mode='full'))
        old=self.projection()
        target=self._create(execution_mode='targeted',selection=self.selection())
        self.finish(target,3000,fail='trinket-334')
        self.assertNotEqual(target.status,'success')
        self.assertEqual(self.projection(),old)
        with patch('botend.services.simc_task_service.current_validation_identity',return_value=('a'*40,'12.0.1')), patch(
                'botend.services.simc_task_service.validate_apl_for_profile',return_value=self.validation):
            retry=rerun_failed_cases(target,requested_by=self.user)
        self.assertEqual(retry.config_snapshot['execution_mode'],'targeted')
        self.assertEqual(retry.config_snapshot['run_count'],6)
        self.finish(retry,4000)
        self.assertEqual(retry.status,'success')
        self.assertNotEqual(self.projection(),old)

    def test_api_preview_is_read_only_and_submission_matches_preview(self):
        options=self.client.get(self.url)
        self.assertEqual(options.status_code,200,options.content)
        payload={'mode':'targeted','selection':self.selection(),'plan_hash':options.json()['data']['plan_hash'],'preview':True}
        preview=self.client.post(self.url,data=json.dumps(payload),content_type='application/json')
        self.assertEqual(preview.status_code,200,preview.content)
        self.assertEqual(preview.json()['data']['run_count'],6)
        self.assertFalse(SimcTask.objects.exists())
        payload.pop('preview')
        with patch('botend.services.simc_task_service.current_validation_identity',return_value=('a'*40,'12.0.1')), patch(
                'botend.services.simc_task_service.validate_apl_for_profile',return_value=self.validation):
            response=self.client.post(self.url,data=json.dumps(payload),content_type='application/json')
        self.assertEqual(response.status_code,202,response.content)
        self.assertEqual(response.json()['data']['run_count'],6)
        self.assertEqual(SimcTask.objects.count(),2)

    def test_api_requires_permission_and_refuses_preview_for_changed_plan(self):
        payload={'mode':'targeted','selection':self.selection(),'plan_hash':'0'*64,'preview':True}
        response=self.client.post(self.url,data=json.dumps(payload),content_type='application/json')
        self.assertEqual(response.status_code,409,response.content)
        self.client.force_login(User.objects.create_user(username='无权限'))
        self.assertEqual(self.client.get(self.url).status_code,403)
        self.assertEqual(self.client.post(self.url,data=json.dumps(payload),content_type='application/json').status_code,403)
        self.assertFalse(SimcTask.objects.exists())

    def test_current_plan_change_during_preflight_aborts_without_partial_tasks(self):
        plan=build_execution_plan(self.panel)
        changed=deepcopy(plan)
        changed['cases'][0]['scenario_label']='预检期间已修改'
        with patch('botend.services.simc_benchmark_execution.build_execution_plan',side_effect=[plan,changed]):
            with self.assertRaises(BenchmarkExecutionConflict):
                self._create(execution_mode='targeted',selection=self.selection())
        self.assertFalse(SimcTask.objects.exists())
        self.assertFalse(SimcBenchmarkExecution.objects.exists())

    def test_equipment_combination_groups_all_levels_and_keeps_its_controls(self):
        from botend.services.simc_benchmark_config import _normalize_candidate_params
        WowItemSnapshot.objects.create(item_id=457,item_class_id=4,inventory_type=11,item_subclass_id=0,
            metadata={'primary_stat_options':['strength']})
        for level in (321,334):
            SimcBenchmarkCandidate.objects.create(panel=self.panel,key=f'pair-{level}',
                label=f'戒指组合 · {level}',candidate_type='gear_swap',
                params=_normalize_candidate_params('gear_swap',f'finger1=,id=456,ilevel={level}\nfinger2=,id=457,ilevel={level}'))
        selection=self.selection('戒指组合',True)
        preview=rerun_preview(build_execution_plan(self.panel),selection)
        self.assertEqual(preview['run_count'],5)
        target=self._create(execution_mode='targeted',selection=selection)
        self.assertEqual({row['candidate_key'] for row in target.cases.get().task.mode_params['initial_candidates']},
            {'baseline','pair-321','pair-334',control_key('pair-321'),control_key('pair-334')})

    def test_cancelled_targeted_batch_does_not_replace_successful_old_results(self):
        from botend.services.simc_benchmark_execution import cancel_execution
        self.finish(self._create(execution_mode='full'))
        old=self.projection()
        target=self._create(execution_mode='targeted',selection=self.selection())
        first=target.cases.first()
        self.finish_case(first,3000)
        reconcile_execution_case(target,first.pk)
        cancel_execution(target,requested_by=self.user)
        self.assertEqual(self.projection(),old)
