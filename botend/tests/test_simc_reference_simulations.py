"""独立公开配置模拟的矩阵、冻结输入、队列恢复及权限回归。"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from botend.models import (
    SimcApl, SimcBackendBinary, SimcContentTemplate, SimcProfile,
    SimcReferenceSimulation, SimcReferenceSimulationConfig, SimcTalentString,
    SimulationRun, SimcTask,
)
from botend.services import simc_reference_simulations as service
from botend.services.task_resolver import resolve_task
from botend.tests.simc_reference_test_fixtures import publish_resources


class ReferenceSimulationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('模拟管理员', 'admin@example.test', 'test-password')
        self.backend, _ = SimcBackendBinary.objects.update_or_create(identifier='production', defaults={'name': '正式服', 'current_version': 'a' * 40, 'game_build': '12.0.1'})
        self.template = SimcContentTemplate.objects.create(name='共享模板', spec='default', content='optimal_raid=0')
        self.apl = SimcApl.objects.create(name='共享优先级', spec='warrior_fury', class_name='warrior', is_system=True, is_selectable=True, content='actions=/auto_attack', source='user')
        self.talent = SimcTalentString.objects.create(name='共享天赋', spec='warrior_fury', talent='CYEAoonA', is_system=True, default_apl=self.apl)
        self.profile = SimcProfile.objects.create(name='共享装备', spec='warrior_fury', class_name='warrior', player_config_mode='manual_equipment', player_equipment='warrior="测试"\nspec=fury')
        service.initialize_configs()
        type(service.initialize_settings()).objects.filter(use_ptr=False).update(backend=self.backend, template=self.template)
        self.config = SimcReferenceSimulationConfig.objects.get(spec_key='warrior_fury')
        self.publication = publish_resources('warrior_fury', profiles=[self.profile], talents=[self.talent], apls=[self.apl])
        self.url = reverse('simc_reference_spec_results', kwargs={'spec_key': self.config.spec_key})

    def enqueue(self, **kwargs):
        return service.enqueue_configs([self.config.pk], self.user.pk, **kwargs)

    def dispatch(self):
        with patch('botend.services.simc_task_service.current_validation_identity', return_value=('a' * 40, '12.0.1')):
            service.maintain_simulations()

    def complete(self, *, dps=123456.75, version=None, valid=True):
        self.dispatch()
        for row in SimcReferenceSimulation.objects.filter(status='submitted'):
            SimcTask.objects.filter(pk=row.task_id).update(current_status=2)
            manifest = resolve_task(row.task).resource_metadata
            if version is not None:
                manifest['backend_version'] = version
            SimulationRun.objects.create(task_id=row.task_id, sequence=1, status='completed',
                                         result_summary={'dps': dps, 'valid': valid},
                                         resource_manifest=manifest)
        service.maintain_simulations()

    def results(self):
        self.profile.refresh_from_db()
        self.talent.refresh_from_db()
        self.apl.refresh_from_db()
        return service.public_results('warrior_fury', self.profile, self.talent, self.apl)

    def test_matrix_crosses_all_shared_profiles_and_talents_using_bound_apl(self):
        second = SimcProfile.objects.create(name='第二套共享装备', spec='warrior_fury', class_name='warrior', player_config_mode='manual_equipment')
        alternate = SimcApl.objects.create(name='另一优先级', spec='warrior_fury', is_system=True, is_selectable=True, content='actions=/bloodthirst', source='simc_upstream')
        other = SimcTalentString.objects.create(name='第二份天赋', spec='warrior_fury', talent='other', is_system=True, default_apl=alternate)
        publish_resources('warrior_fury', profiles=[second], talents=[other], apls=[alternate])
        result = self.enqueue()
        self.assertEqual(result, {'created': 8, 'skipped': 0, 'errors': []})
        self.assertEqual(set(SimcReferenceSimulation.objects.filter(talent=other).values_list('apl_id', flat=True)), {alternate.pk})
        self.assertEqual({r.snapshot['simulation_params']['desired_targets'] for r in SimcReferenceSimulation.objects.all()}, {1, 5})
        self.assertEqual(self.enqueue()['skipped'], 8)
        self.assertEqual(self.enqueue(force=True)['created'], 0)

    def test_private_unselectable_and_missing_default_are_excluded(self):
        SimcProfile.objects.create(name='个人装备', spec='warrior_fury', user_id=self.user.pk)
        SimcTalentString.objects.create(name='个人天赋', spec='warrior_fury', talent='private', owner_user_id=self.user.pk)
        SimcTalentString.objects.create(name='未发布天赋', spec='warrior_fury', talent='hidden', is_system=True, is_selectable=False)
        missing = SimcTalentString.objects.create(name='未绑定天赋', spec='warrior_fury', talent='missing', is_system=True)
        self.publication.talents.add(missing)
        result = self.enqueue()
        self.assertEqual(result['created'], 2)
        self.assertEqual(len(result['errors']), 1)
        self.assertIn('未绑定', result['errors'][0])

    def test_ptr_has_separate_config_and_requires_explicit_backend(self):
        SimcProfile.objects.create(name='测试服装备', spec='warrior_fury', class_name='warrior', use_ptr=True, player_config_mode='manual_equipment')
        service.initialize_configs()
        ptr = SimcReferenceSimulationConfig.objects.get(use_ptr=True)
        if ptr.backend_id:
            self.assertFalse(ptr.backend.current_version)
        result = service.enqueue_configs([ptr.pk], self.user.pk)
        self.assertEqual(result['created'], 0)
        self.assertIn('后端', result['errors'][0])

    def test_dispatch_freezes_resources_and_success_projects_conditions_and_dps(self):
        self.enqueue()
        self.complete()
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='completed').count(), 2)
        task = SimcTask.objects.first()
        self.assertEqual(task.talent_version.payload['talent'], self.talent.talent)
        self.assertEqual(task.profile_version.resource_id, self.profile.pk)
        self.assertTrue(task.is_benchmark_task)
        self.assertEqual(task.queue_priority, 20)
        self.assertEqual([r['desired_targets'] for r in self.results()], [1, 5])
        self.assertEqual(self.results()[0]['dps'], 123456.75)
        self.client.force_login(self.user)
        response = self.client.get(reverse('portal_simc_reference'), {'spec': 'warrior_fury'})
        self.assertContains(response, '123,456.75')
        self.assertContains(response, '5 目标')
        self.assertNotContains(response, '任务 #')

    def test_drift_hides_old_result_and_regeneration_preserves_history(self):
        self.enqueue()
        self.complete()
        for model, obj, field, changed in (
            (SimcProfile, self.profile, 'player_equipment', 'warrior="修改"\nspec=fury'),
            (SimcTalentString, self.talent, 'talent', 'new-talent'),
            (SimcApl, self.apl, 'content', 'actions=/bloodthirst'),
            (SimcContentTemplate, self.template, 'content', 'optimal_raid=1'),
            (SimcBackendBinary, self.backend, 'current_version', 'b' * 40),
        ):
            with self.subTest(field=field):
                original = getattr(obj, field)
                model.objects.filter(pk=obj.pk).update(**{field: changed})
                self.assertEqual(self.results(), [])
                model.objects.filter(pk=obj.pk).update(**{field: original})
        self.assertEqual(len(self.results()), 2)
        type(self.config.settings).objects.filter(pk=self.config.settings_id).update(duration=240)
        self.assertEqual(self.results(), [])
        self.assertEqual(self.enqueue()['created'], 2)
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='completed').count(), 2)

    def test_manual_rerun_keeps_last_success_until_replaced_and_deduplicates(self):
        self.enqueue()
        self.complete()
        self.assertEqual(self.enqueue()['created'], 0)
        self.assertEqual(self.enqueue(force=True)['created'], 2)
        self.assertEqual(len(self.results()), 2)
        self.assertEqual(self.enqueue(force=True)['created'], 0)

    def test_changed_resource_is_not_silently_dispatched(self):
        self.enqueue()
        SimcApl.objects.filter(pk=self.apl.pk).update(content='actions=/rampage')
        self.dispatch()
        self.assertEqual(SimcTask.objects.count(), 0)
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='failed').count(), 2)

    def test_changes_during_preparation_roll_back_created_task(self):
        self.enqueue()
        original = service.tasks.prepare_task_creation
        def mutate(**kwargs):
            SimcApl.objects.filter(pk=self.apl.pk).update(content='actions=/rampage')
            return original(**kwargs)
        with patch.object(service.tasks, 'prepare_task_creation', side_effect=mutate):
            self.dispatch()
        self.assertEqual(SimcTask.objects.count(), 0)
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='failed').count(), 2)

    def test_invalid_results_and_wrong_execution_version_are_not_published(self):
        for dps, version, valid in ((0, None, True), (True, None, True), (1200, 'b'*40, True), (1200, None, False)):
            with self.subTest(dps=dps, version=version, valid=valid):
                self.enqueue()
                self.complete(dps=dps, version=version, valid=valid)
                self.assertEqual(self.results(), [])

    def test_stale_preparation_is_recovered_once(self):
        self.enqueue()
        SimcReferenceSimulation.objects.update(status='preparing', updated_at=timezone.now()-timedelta(minutes=16))
        self.dispatch()
        self.dispatch()
        self.assertEqual(SimcTask.objects.count(), 2)

    def test_execution_checks_backend_again_and_requires_agent_measured_revision(self):
        from types import SimpleNamespace
        self.enqueue()
        self.dispatch()
        task = SimcTask.objects.first()
        self.assertEqual(resolve_task(task).resource_metadata['backend_version'], 'a' * 40)
        for capabilities in ({}, {'binary_revision': 'b' * 40}):
            with self.assertRaises(ValueError):
                service.execution_backend_metadata(task, agent=SimpleNamespace(capabilities=capabilities))
        self.assertEqual(service.execution_backend_metadata(
            task, agent=SimpleNamespace(capabilities={'binary_revision': 'a' * 40}),
        )['backend_version'], 'a' * 40)
        SimcBackendBinary.objects.filter(pk=self.backend.pk).update(current_version='b' * 40)
        with self.assertRaises(ValueError):
            resolve_task(task)

    def test_revoked_public_resource_and_different_apl_never_receive_results(self):
        self.enqueue()
        self.complete()
        SimcProfile.objects.filter(pk=self.profile.pk).update(user_id=self.user.pk)
        self.assertEqual(self.results(), [])
        SimcProfile.objects.filter(pk=self.profile.pk).update(user_id=None)
        SimcTalentString.objects.filter(pk=self.talent.pk).update(default_apl=None)
        self.assertEqual(self.results(), [])

    def test_admin_permissions_csrf_and_no_mutation_on_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 403)
        ordinary = User.objects.create_user('普通内测用户')
        self.client.force_login(ordinary)
        self.assertEqual(self.client.post(self.url, {'action': 'generate'}).status_code, 403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(SimcReferenceSimulation.objects.count(), 0)
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.user)
        self.assertEqual(protected.post(self.url, {'action': 'generate'}).status_code, 403)
        self.assertEqual(self.client.post(self.url, {'action': 'generate'}).status_code, 200)
        self.assertEqual(SimcReferenceSimulation.objects.count(), 2)

    def test_admin_edits_conditions_and_rejects_invalid_targets(self):
        self.client.force_login(self.user)
        self.url = reverse('simc_reference_settings')
        payload = {'action': 'save', 'branch': 'retail', 'backend': self.backend.pk, 'template': self.template.pk,
                   'duration': 240, 'iterations': 2000, 'target_values': '1,3,5', 'is_enabled': 'on'}
        self.assertEqual(self.client.post(self.url, payload).status_code, 302)
        self.config.refresh_from_db()
        self.assertEqual(self.config.targets, [1, 3, 5])
        payload['target_values'] = '1,1'
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['settings_form'].is_bound)
        self.assertEqual(response.context['settings_form']['target_values'].value(), '1,1')
        self.assertContains(response, '目标须为', status_code=400)
        self.config.refresh_from_db()
        self.assertEqual(self.config.targets, [1, 3, 5])
