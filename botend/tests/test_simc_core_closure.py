import hashlib
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.dashboard.api import SimcAplCandidatesAPIView, SimcTaskAPIView, SimcWorkbenchAPIView
from botend.models import (SimcApl, SimcContentTemplate, SimcProfile, SimcTask,
                           SimcTaskArtifact, SimulationRun)
from botend.services.simc_task_service import (
    append_candidate_runs,
    create_task,
    initialize_task_runs,
)
from botend.services.task_rerun import create_rerun, TaskRerunError


TEST_VALIDATION_IDENTITY = ('test-simc-revision', 'test-game-build')


def mark_apl_valid(apl):
    values = {'validation_status': SimcApl.VALIDATION_VALID,
              'validated_content_hash': hashlib.sha256(apl.content.encode()).hexdigest(),
              'validation_revision': TEST_VALIDATION_IDENTITY[0],
              'validation_game_build': TEST_VALIDATION_IDENTITY[1], 'is_selectable': True}
    SimcApl.objects.filter(pk=apl.pk).update(**values)
    for key, value in values.items(): setattr(apl, key, value)


def setUpModule():
    from django.test import override_settings
    global _validation_settings, _validation_mock
    _validation_settings = override_settings(SIMC_APL_CURRENT_IDENTITY=TEST_VALIDATION_IDENTITY)
    _validation_settings.enable()
    _validation_mock = patch('botend.services.simc_task_service.validate_apl_for_profile', side_effect=lambda _p, apl: {
        'valid': True, 'content_hash': hashlib.sha256(apl.content.encode()).hexdigest(),
        'revision': TEST_VALIDATION_IDENTITY[0], 'game_build': TEST_VALIDATION_IDENTITY[1]})
    _validation_mock.start()


def tearDownModule():
    _validation_mock.stop(); _validation_settings.disable()


class SimcCoreClosureTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='closure', password='x')
        self.other = get_user_model().objects.create_user(username='other-closure', password='x')
        self.profile = SimcProfile.objects.create(user_id=self.user.id, name='P', spec='fury', player_config_mode='manual_equipment', player_equipment='warrior="x"\nspec=fury', is_active=True)
        self.template = SimcContentTemplate.objects.create(name='T', spec='fury', content='{simulation_options}\n{player_config}\n{action_list}\n{output_options}', is_active=True, is_selectable=True)
        self.apl = SimcApl.objects.create(name='A', spec='fury', content='actions=/bloodthirst', is_system=True, is_active=True, is_selectable=True)
        mark_apl_valid(self.apl)
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
            'profile_name': 'MUTATE', 'spec': 'warrior_fury', 'talent': 'MUTATE', 'gear_crit': 999,
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

    @patch.object(SimcAplCandidatesAPIView, '_generate_glm_candidates')
    def test_apl_candidates_freeze_ready_plans_before_creating_task(self, generate):
        self.apl.content = 'actions=/bloodthirst\nactions+=/rampage'
        self.apl.save(update_fields=['content'])
        mark_apl_valid(self.apl)
        generate.return_value = [
            {
                'name': f'候选方案{i}',
                'apl_list': 'actions+=/rampage\nactions=/bloodthirst',
                'reason': f'reason-{i}',
            }
            for i in range(5)
        ]
        response = SimcAplCandidatesAPIView.as_view()(self.request('/api/simc-apl-candidates/', {
            'profile_id': self.profile.id, 'base_template_id': self.template.id,
            'selected_apl_id': self.apl.id, 'candidate_count': 5, 'include_base': True,
        }))
        body = json.loads(response.content)
        self.assertTrue(body['success'], body)
        task = SimcTask.objects.get(id=body['data']['task_id'])
        frozen = task.mode_params['initial_candidates']
        self.assertEqual(SimcTask.objects.filter(mode='comparison').count(), 1)
        self.assertEqual(task.simulation_runs.count(), 0)
        self.assertEqual(len(frozen), 6)
        self.assertTrue(task.profile_id and task.template_id and task.apl_id)
        self.assertTrue(task.profile_version_id and task.template_version_id and task.apl_version_id)
        self.assertEqual(task.mode, 'comparison')
        self.assertEqual(body['data']['run_ids'], [])
        self.assertFalse(body['data']['preprocessing_started'])
        self.assertTrue(all(
            candidate['candidate_params']['search']['preprocess_stage'] == 'ready'
            and candidate['candidate_params'].get('apl_override')
            for candidate in frozen
        ))

    def test_worker_manifest_combines_resolver_and_composition_metadata(self):
        task = create_task(user_id=self.user.id, name='run', profile_id=self.profile.id, template_id=self.template.id, apl_id=self.apl.id)
        composition = {'composer': {'version': 7}, 'sections': ['profile', 'apl']}
        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer.compose', return_value=('warrior="x"', composition, None)), patch.object(SimcMonitor, 'execute_simc_command', return_value=True):
            monitor = SimcMonitor(None, task); monitor.result_path = '/tmp'
            self.assertTrue(monitor.process_simc_task(task))
        manifest = SimulationRun.objects.get(task=task).resource_manifest
        self.assertIn('profile', manifest)
        self.assertEqual(manifest['composition_manifest'], composition)

    def test_backend_does_not_resurrect_task_after_stale_recovery(self):
        task = create_task(
            user_id=self.user.id,
            name='stale claim',
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )
        task.current_status = 1
        task.started_at = timezone.now()
        task.save(update_fields=['current_status', 'started_at', 'modified_time'])
        monitor = SimcMonitor(None, task)

        def recover_during_run(_task, run):
            run.status = 'completed'
            run.result_summary = {'dps': 100}
            run.completed_at = timezone.now()
            run.save(update_fields=['status', 'result_summary', 'completed_at'])
            SimcTask.objects.filter(pk=task.pk).update(
                current_status=3,
                error_detail='Worker 心跳超时，执行已中断',
                completed_at=timezone.now(),
            )
            return True

        with patch.object(monitor, 'process_reference_run', side_effect=recover_during_run):
            self.assertFalse(monitor.process_reference_task(task))

        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)
        self.assertIn('心跳超时', task.error_detail)

    def test_attribute_search_advance_requires_worker_lease(self):
        task = create_task(
            user_id=self.user.id,
            name='unclaimed attribute',
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
            mode='attribute_sweep',
            candidates=[{'candidate_key': 'round-1', 'candidate_label': 'round 1'}],
        )
        from botend.services.simc_attribute_search import advance_attribute_search

        with self.assertRaisesRegex(ValueError, '执行租约'):
            advance_attribute_search(task.id)

        self.assertEqual(task.simulation_runs.count(), 0)

    def test_stale_attribute_claim_cannot_append_runs_or_reopen_failed_task(self):
        task = create_task(
            user_id=self.user.id,
            name='stale attribute',
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
            mode='attribute_sweep',
            candidates=[{'candidate_key': 'round-1', 'candidate_label': 'round 1'}],
        )
        claimed_at = timezone.now()
        SimcTask.objects.filter(pk=task.pk).update(
            current_status=1, started_at=claimed_at,
        )
        SimcTask.objects.filter(pk=task.pk).update(
            current_status=3, error_detail='Worker 心跳超时，执行已中断',
            completed_at=timezone.now(),
        )

        with self.assertRaisesRegex(ValueError, '执行租约已失效'):
            initialize_task_runs(task, expected_started_at=claimed_at)
        with self.assertRaisesRegex(ValueError, '执行租约已失效'):
            append_candidate_runs(
                task,
                [{'candidate_key': 'round-2', 'candidate_label': 'round 2'}],
                round_number=2,
                expected_started_at=claimed_at,
            )
        from botend.services.simc_attribute_search import advance_attribute_search
        with self.assertRaisesRegex(ValueError, '执行租约已失效'):
            advance_attribute_search(task.id, expected_started_at=claimed_at)

        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)
        self.assertIn('心跳超时', task.error_detail)
        self.assertEqual(task.simulation_runs.count(), 0)

    def test_workbench_task_detail_returns_safe_runs(self):
        task = create_task(user_id=self.user.id, name='detail', profile_id=self.profile.id, template_id=self.template.id, apl_id=self.apl.id)
        task.mode_params = {
            'candidate_type': 'gear_swap', 'is_base': False, 'batch_index': 1,
            'gear_swap': {'slot': 'head', 'raw_value': 'secret frozen input'},
            'talent_override': 'secret talent body',
        }
        task.save(update_fields=['mode_params'])
        from botend.services.simc_task_service import initialize_task_runs
        run = initialize_task_runs(task)[0]
        run.status = 'failed'
        run.input_hash = 'a' * 64
        run.result_summary = {'dps': 12, 'secret': 'drop'}
        run.resource_manifest = {'profile': {'id': self.profile.id}, 'content': 'drop'}
        run.error_detail = 'Traceback: command=/private/path stderr=secret'
        run.save()
        request = self.factory.get('/api/simc-workbench/tasks/%s/' % task.id); request.user = self.user
        body = json.loads(SimcWorkbenchAPIView.as_view()(request, resource='tasks', object_id=task.id).content)['data']
        self.assertEqual(len(body['runs']), 1)
        self.assertEqual(body['runs'][0]['result_summary'], {'dps': 12})
        self.assertNotIn('resource_manifest', body['runs'][0])
        self.assertNotIn('error_detail', body['runs'][0])
        self.assertEqual(body['runs'][0]['error_summary'], '任务执行失败')
        self.assertEqual(body['mode_summary'], {
            'candidate_type': 'gear_swap', 'is_base': False,
        })
        self.assertNotIn('mode_params', body)

    def test_artifact_can_be_bound_to_specific_run(self):
        task = create_task(user_id=self.user.id, name='artifact', profile_id=self.profile.id, template_id=self.template.id, apl_id=self.apl.id)
        from botend.services.simc_task_service import initialize_task_runs
        run = initialize_task_runs(task)[0]
        artifact = SimcTaskArtifact.objects.create(task=task, run=run, artifact_type='html_report', file_path='simc_results/x.html')
        self.assertEqual(artifact.run_id, run.id)

    @patch('botend.services.simc_artifacts._validated_result')
    def test_new_run_artifact_does_not_reassign_historical_run(self, validated_result):
        from pathlib import Path
        from tempfile import NamedTemporaryFile
        from botend.services.simc_artifacts import upsert_task_html_artifact

        task = create_task(user_id=self.user.id, name='artifact-history', profile_id=self.profile.id, template_id=self.template.id, apl_id=self.apl.id)
        from botend.services.simc_task_service import initialize_task_runs
        old_run = initialize_task_runs(task)[0]
        new_run = SimulationRun.objects.create(task=task, sequence=2)
        with NamedTemporaryFile() as report:
            validated_result.return_value = (Path(report.name), 'simc_results/simc_task_%s.html' % task.id)
            old_artifact = upsert_task_html_artifact(task, 'ignored.html', run=old_run)
            new_artifact = upsert_task_html_artifact(task, 'ignored.html', run=new_run)
        old_artifact.refresh_from_db()
        self.assertEqual(old_artifact.run_id, old_run.id)
        self.assertNotEqual(new_artifact.id, old_artifact.id)
        self.assertEqual(new_artifact.run_id, new_run.id)

    def test_rerun_rejects_all_request_overrides_and_copies_frozen_task(self):
        task = create_task(user_id=self.user.id, name='old', profile_id=self.profile.id, template_id=self.template.id, apl_id=self.apl.id, simulation_params={'iterations': 1000})
        task.current_status = 2; task.save(update_fields=['current_status'])
        new_apl = SimcApl.objects.create(name='new', spec='fury', content='actions=/rampage', owner_user_id=self.user.id, is_active=True, is_selectable=True)
        mark_apl_valid(new_apl)
        with self.assertRaises(TaskRerunError):
            create_rerun(task.id, self.user.id, {'apl_id': new_apl.id})
        with self.assertRaises(TaskRerunError):
            create_rerun(task.id, self.user.id, {'simulation_params': {'iterations': 2222}})
        rerun = create_rerun(task.id, self.user.id)
        self.assertEqual(rerun.name, 'old (rerun)')
        self.assertEqual(rerun.apl_id, task.apl_id)
        self.assertEqual(rerun.simulation_params, {'iterations': 1000})
        self.assertEqual(rerun.mode, 'normal')
        self.assertEqual(rerun.simulation_runs.count(), 0)
        task.refresh_from_db(); self.assertEqual(task.name, 'old')
        with self.assertRaises(TaskRerunError):
            create_rerun(task.id, self.user.id, {'evil': 'field'})
