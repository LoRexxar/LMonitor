import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.dashboard.api import SimcAplCandidatesAPIView, SimcTaskAPIView, SimcWorkbenchAPIView
from botend.models import (SimcApl, SimcContentTemplate, SimcProfile, SimcTask,
                           SimcTaskArtifact, SimcTaskBatch, SimulationRun)
from botend.services.simc_task_service import create_task
from botend.services.task_rerun import create_rerun, TaskRerunError


class SimcCoreClosureTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='closure', password='x')
        self.other = get_user_model().objects.create_user(username='other-closure', password='x')
        self.profile = SimcProfile.objects.create(user_id=self.user.id, name='P', spec='fury', player_config_mode='manual_equipment', player_equipment='warrior="x"\nspec=fury', is_active=True)
        self.template = SimcContentTemplate.objects.create(name='T', template_type='base_template', spec='fury', content='{simulation_options}\n{player_config}\n{action_list}\n{output_options}', is_active=True, is_selectable=True)
        self.apl = SimcApl.objects.create(name='A', spec='fury', content='actions=/bloodthirst', is_system=True, is_active=True, is_selectable=True)
        self.factory = RequestFactory()

    def request(self, path, payload):
        request = self.factory.post(path, data=json.dumps(payload), content_type='application/json')
        request.user = self.user
        return request

    def test_task_post_only_selects_existing_profile_without_mutating_it(self):
        before = {f: getattr(self.profile, f) for f in ('name', 'spec', 'talent', 'gear_crit')}
        response = SimcTaskAPIView.as_view()(self.request('/api/simc-tasks/', {
            'name': 'new task', 'simc_profile_id': self.profile.id,
            'base_template_id': self.template.id, 'selected_apl_id': self.apl.id,
            'profile_name': 'MUTATE', 'spec': 'arms', 'talent': 'MUTATE', 'gear_crit': 999,
            'time': 180, 'target_count': 2,
        }))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.content)['success'])
        self.profile.refresh_from_db()
        self.assertEqual(before, {f: getattr(self.profile, f) for f in before})
        task = SimcTask.objects.get(name='new task')
        self.assertEqual((task.profile_id, task.template_id, task.apl_id), (self.profile.id, self.template.id, self.apl.id))
        self.assertTrue(task.profile_version_id and task.template_version_id and task.apl_version_id)

    def test_task_post_requires_existing_owner_profile(self):
        count = SimcProfile.objects.count()
        response = SimcTaskAPIView.as_view()(self.request('/api/simc-tasks/', {
            'name': 'bad', 'base_template_id': self.template.id, 'selected_apl_id': self.apl.id,
            'profile_name': 'must not create',
        }))
        self.assertFalse(json.loads(response.content)['success'])
        self.assertEqual(SimcProfile.objects.count(), count)

    @patch.object(SimcAplCandidatesAPIView, '_start_compare_preprocess_async')
    def test_apl_candidates_create_real_batch_and_complete_reference_tasks(self, start):
        response = SimcAplCandidatesAPIView.as_view()(self.request('/api/simc-apl-candidates/', {
            'profile_id': self.profile.id, 'base_template_id': self.template.id,
            'selected_apl_id': self.apl.id, 'candidate_count': 5, 'include_base': True,
        }))
        body = json.loads(response.content)
        self.assertTrue(body['success'], body)
        batch = SimcTaskBatch.objects.get(id=body['data']['batch_id'])
        tasks = list(batch.simctask_set.all())
        self.assertEqual(len(tasks), 6)
        self.assertTrue(all(t.profile_id and t.template_id and t.apl_id and t.profile_version_id and t.template_version_id and t.apl_version_id for t in tasks))
        self.assertTrue(all(t.mode == 'comparison' and not t.ext for t in tasks))

    def test_worker_manifest_combines_resolver_and_composition_metadata(self):
        task = create_task(user_id=self.user.id, name='run', profile_id=self.profile.id, template_id=self.template.id, apl_id=self.apl.id)
        composition = {'composer': {'version': 7}, 'sections': ['profile', 'apl']}
        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer.compose', return_value=('warrior="x"', composition, None)), patch.object(SimcMonitor, 'execute_simc_command', return_value=True):
            monitor = SimcMonitor(None, task); monitor.result_path = '/tmp'
            self.assertTrue(monitor.process_simc_task(task))
        manifest = SimulationRun.objects.get(task=task).resource_manifest
        self.assertIn('profile', manifest)
        self.assertEqual(manifest['composition_manifest'], composition)

    def test_workbench_task_detail_returns_safe_runs(self):
        task = create_task(user_id=self.user.id, name='detail', profile_id=self.profile.id, template_id=self.template.id, apl_id=self.apl.id)
        task.mode_params = {
            'candidate_type': 'gear_swap', 'is_base': False, 'batch_index': 1,
            'gear_swap': {'slot': 'head', 'raw_value': 'secret frozen input'},
            'talent_override': 'secret talent body',
        }
        task.save(update_fields=['mode_params'])
        SimulationRun.objects.create(
            task=task, sequence=1, status='failed', input_hash='a' * 64,
            result_summary={'dps': 12, 'secret': 'drop'},
            resource_manifest={'profile': {'id': self.profile.id}, 'content': 'drop'},
            error_detail='Traceback: command=/private/path stderr=secret',
        )
        request = self.factory.get('/api/simc-workbench/tasks/%s/' % task.id); request.user = self.user
        body = json.loads(SimcWorkbenchAPIView.as_view()(request, resource='tasks', object_id=task.id).content)['data']
        self.assertEqual(len(body['runs']), 1)
        self.assertEqual(body['runs'][0]['result_summary'], {'dps': 12})
        self.assertNotIn('resource_manifest', body['runs'][0])
        self.assertNotIn('error_detail', body['runs'][0])
        self.assertEqual(body['runs'][0]['error_summary'], '任务执行失败')
        self.assertEqual(body['mode_summary'], {
            'candidate_type': 'gear_swap', 'is_base': False, 'batch_index': 1,
        })
        self.assertNotIn('mode_params', body)

    def test_artifact_can_be_bound_to_specific_run(self):
        task = create_task(user_id=self.user.id, name='artifact', profile_id=self.profile.id, template_id=self.template.id, apl_id=self.apl.id)
        run = SimulationRun.objects.create(task=task, sequence=1)
        artifact = SimcTaskArtifact.objects.create(task=task, run=run, artifact_type='html_report', file_path='simc_results/x.html')
        self.assertEqual(artifact.run_id, run.id)

    @patch('botend.services.simc_artifacts._validated_result')
    def test_new_run_artifact_does_not_reassign_historical_run(self, validated_result):
        from pathlib import Path
        from tempfile import NamedTemporaryFile
        from botend.services.simc_artifacts import upsert_task_html_artifact

        task = create_task(user_id=self.user.id, name='artifact-history', profile_id=self.profile.id, template_id=self.template.id, apl_id=self.apl.id)
        old_run = SimulationRun.objects.create(task=task, sequence=1)
        new_run = SimulationRun.objects.create(task=task, sequence=2)
        with NamedTemporaryFile() as report:
            validated_result.return_value = (Path(report.name), 'simc_results/simc_task_%s.html' % task.id)
            old_artifact = upsert_task_html_artifact(task, 'ignored.html', run=old_run)
            new_artifact = upsert_task_html_artifact(task, 'ignored.html', run=new_run)
        old_artifact.refresh_from_db()
        self.assertEqual(old_artifact.run_id, old_run.id)
        self.assertNotEqual(new_artifact.id, old_artifact.id)
        self.assertEqual(new_artifact.run_id, new_run.id)

    def test_rerun_whitelist_and_resource_switch(self):
        task = create_task(user_id=self.user.id, name='old', profile_id=self.profile.id, template_id=self.template.id, apl_id=self.apl.id, simulation_params={'iterations': 1000})
        task.current_status = 2; task.save(update_fields=['current_status'])
        new_apl = SimcApl.objects.create(name='new', spec='fury', content='actions=/rampage', owner_user_id=self.user.id, is_active=True, is_selectable=True)
        rerun = create_rerun(task.id, self.user.id, {'name': 'new name', 'apl_id': new_apl.id, 'simulation_params': {'iterations': 2222, 'evil': 1}})
        self.assertEqual(rerun.name, 'new name')
        self.assertEqual(rerun.apl_id, new_apl.id)
        self.assertEqual(rerun.simulation_params, {'iterations': 2222})
        self.assertIsNone(rerun.batch_id)
        task.refresh_from_db(); self.assertEqual(task.name, 'old')
        with self.assertRaises(TaskRerunError):
            create_rerun(task.id, self.user.id, {'evil': 'field'})
