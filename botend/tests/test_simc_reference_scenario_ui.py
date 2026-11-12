"""Independent scene formset persistence and complete UI projections."""
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from botend.models import (SimcBackendBinary, SimcContentTemplate, SimcReferenceSimulationSettings,
                           SimcReferenceSimulationScenario, SimcReferenceSimulationConfig,
                           SimcReferenceSimulation, SimcProfile, SimcTalentString, SimcApl, SimcTask)
from botend.services import simc_reference_simulations as service
from botend.services.simc_reference_management import scheme_results
from botend.services.simc_reference_rankings import ranking_page_context
from botend.tests.simc_reference_test_fixtures import publish_resources


class ScenarioUITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('scenario-admin', 'scene@example.test', 'test-password')
        self.client.force_login(self.user)
        self.backend, _ = SimcBackendBinary.objects.update_or_create(identifier='production', defaults={
            'name': 'Production', 'current_version': 'a' * 40, 'game_build': '12.0.1', 'is_active': True})
        self.template = SimcContentTemplate.objects.create(name='Shared', spec='default', content='optimal_raid=0')
        self.settings, _ = SimcReferenceSimulationSettings.objects.update_or_create(use_ptr=False, defaults={
            'backend': self.backend, 'template': self.template, 'is_enabled': True})
        self.settings.scenarios.all().delete()
        self.url = reverse('simc_reference_settings')

    def payload(self, *, existing=(), extra=()):
        rows = [dict(id=s.pk, name=s.name, **s.simulation_params, is_enabled=s.is_enabled) for s in existing] + list(extra)
        data = {'action': 'save', 'branch': 'retail', 'backend': self.backend.pk, 'template': self.template.pk,
                'is_enabled': 'on', 'scenarios-TOTAL_FORMS': len(rows), 'scenarios-INITIAL_FORMS': len(existing),
                'scenarios-MIN_NUM_FORMS': 0, 'scenarios-MAX_NUM_FORMS': 100}
        for index, row in enumerate(rows):
            for key, value in row.items():
                if key == 'is_enabled':
                    if value:
                        data[f'scenarios-{index}-{key}'] = 'on'
                else:
                    data[f'scenarios-{index}-{key}'] = str(value)
        return data

    def create_scene(self, name='Short', duration=40, settings=None):
        return SimcReferenceSimulationScenario.objects.create(settings=settings or self.settings, name=name,
            simulation_params={'desired_targets': 20, 'max_time': duration, 'iterations': 1000})

    def resources(self):
        apl = SimcApl.objects.create(name='APL', spec='warrior_fury', class_name='warrior',
            is_system=True, is_selectable=True, content='actions=/auto_attack')
        talent = SimcTalentString.objects.create(name='Talent', spec='warrior_fury', talent='code',
            is_system=True, default_apl=apl)
        profile = SimcProfile.objects.create(name='Profile', spec='warrior_fury', class_name='warrior',
            player_config_mode='manual_equipment', player_equipment='warrior="test"\nspec=fury')
        publish_resources('warrior_fury', profiles=[profile], talents=[talent], apls=[apl])
        config = SimcReferenceSimulationConfig.objects.create(spec_key='warrior_fury', settings=self.settings)
        return config, profile, talent, apl

    def test_two_twenty_target_scenes_save_and_project_separate_columns(self):
        response = self.client.post(self.url, self.payload(extra=[
            dict(name='Burst', desired_targets=20, max_time=40, iterations=1000, is_enabled=True),
            dict(name='Sustained', desired_targets=20, max_time=300, iterations=2000, is_enabled=True)]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.settings.scenarios.count(), 2)
        config, profile, talent, apl = self.resources()
        for scene in service.active_scenarios(config):
            frozen = service.snapshot(config, profile, talent, apl, scene)
            SimcReferenceSimulation.objects.create(config=config, scenario=scene, profile=profile, talent=talent,
                apl=apl, requested_by_id=self.user.pk, snapshot=frozen, signature=service.tasks._compute_content_hash(frozen),
                status='completed', dps=1000 + scene.simulation_params['max_time'])
        scenes = scheme_results(config, 1).object_list[0]['scenes']
        self.assertEqual([s['duration'] for s in scenes], [40, 300])
        self.assertEqual([s['result'].dps for s in scenes], [1040, 1300])
        self.assertEqual(len(service.public_results(config.spec_key, profile, talent, apl)), 2)
        response = self.client.get(reverse('simc_reference_spec_results', args=[config.spec_key]))
        self.assertContains(response, 'Burst')
        self.assertContains(response, 'Sustained')
        self.assertContains(response, 'Burst · 20 目标 · 40 秒')
        self.assertContains(response, 'Sustained · 20 目标 · 300 秒')
        self.assertNotContains(response, '次迭代')
        board = ranking_page_context(config, scenes[0]['signature'], 'overall')
        self.assertEqual(len(board['scene_options']), 2)
        self.assertEqual(sum(s['selected'] for s in board['scene_options']), 1)
        self.assertEqual(len(board['entries']), 1)
        self.assertEqual([s['label'] for s in board['scene_options']],
                         ['Burst · 20 目标 · 40 秒', 'Sustained · 20 目标 · 300 秒'])
        for route in ('portal_simc_reference_rankings', 'simc_reference_rankings'):
            result = self.client.get(reverse(route, args=[config.pk, scenes[0]['signature'], 'overall']))
            self.assertContains(result, 'Burst · 20 目标 · 40 秒')
            self.assertNotContains(result, '次迭代')
            self.assertNotContains(result, '20 目标 · 20 目标')
        self.assertContains(self.client.get(self.url), '迭代次数')
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_tampered_initial_count_cannot_turn_existing_scene_into_new_record(self):
        scene = self.create_scene()
        for initial in (0, 2):
            with self.subTest(initial_forms=initial):
                data = self.payload(existing=[scene])
                data['scenarios-INITIAL_FORMS'] = initial
                data['scenarios-0-name'] = 'Replacement'
                data['scenarios-0-max_time'] = 300
                data.pop('is_enabled')
                response = self.client.post(self.url, data)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.settings.scenarios.count(), 1)
                scene.refresh_from_db()
                self.settings.refresh_from_db()
                self.assertEqual(scene.name, 'Short')
                self.assertEqual(scene.simulation_params['max_time'], 40)
                self.assertTrue(self.settings.is_enabled)
                self.assertEqual(SimcTask.objects.count(), 0)

    def test_invalid_formset_is_atomic_and_keeps_input(self):
        scene = self.create_scene()
        data = self.payload(existing=[scene], extra=[dict(name='Bad input', desired_targets=20, max_time=0, iterations=1000)])
        data['is_enabled'] = ''
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 400)
        self.settings.refresh_from_db()
        self.assertTrue(self.settings.is_enabled)
        self.assertEqual(self.settings.scenarios.count(), 1)
        self.assertContains(response, 'Bad input', status_code=400)
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_missing_formset_and_cross_environment_ids_rejected(self):
        scene = self.create_scene()
        response = self.client.post(self.url, {'action': 'save', 'branch': 'retail', 'backend': self.backend.pk,
                                              'template': self.template.pk, 'is_enabled': 'on'})
        self.assertEqual(response.status_code, 400)
        ptr, _ = SimcReferenceSimulationSettings.objects.get_or_create(use_ptr=True)
        foreign = self.create_scene(settings=ptr)
        data = self.payload(existing=[scene])
        data['scenarios-0-id'] = foreign.pk
        self.assertEqual(self.client.post(self.url, data).status_code, 400)
        self.assertEqual(self.settings.scenarios.count(), 1)

    def test_disable_save_delete_is_soft_and_duplicate_conditions_rejected(self):
        scene = self.create_scene()
        data = self.payload(existing=[scene], extra=[dict(name='Duplicate', desired_targets=20, max_time=40, iterations=1000)])
        self.assertEqual(self.client.post(self.url, data).status_code, 400)
        data = self.payload(existing=[scene])
        data.pop('is_enabled')
        data['scenarios-0-DELETE'] = 'on'
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        scene.refresh_from_db()
        self.settings.refresh_from_db()
        self.assertFalse(scene.is_enabled)
        self.assertFalse(self.settings.is_enabled)
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_noop_and_metadata_save_preserve_hash_and_fractional_seconds(self):
        scene = self.create_scene(duration=300)
        config, profile, talent, apl = self.resources()
        original = service.snapshot(config, profile, talent, apl, scene)
        original_hash = service.tasks._compute_content_hash(original)
        data = self.payload(existing=[scene])
        data['scenarios-0-max_time'] = '300.0'
        data['scenarios-0-name'] = 'Renamed only'
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        scene.refresh_from_db()
        config.refresh_from_db()
        self.assertIs(type(scene.simulation_params['max_time']), int)
        self.assertEqual(service.tasks._compute_content_hash(service.snapshot(config, profile, talent, apl, scene)), original_hash)
        data = self.payload(existing=[scene])
        data['scenarios-0-max_time'] = '40.5'
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        scene.refresh_from_db()
        self.assertEqual(scene.simulation_params['max_time'], 40.5)
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_permission_and_csrf_remain_enforced(self):
        user = User.objects.create_user('normal-user', password='test-password')
        self.client.force_login(user)
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 403)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(self.url, self.payload()).status_code, 403)
