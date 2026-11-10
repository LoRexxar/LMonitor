"""Independent reference scenarios: identity, frozen input and dispatch fencing."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from botend.models import (
    SimcApl, SimcBackendBinary, SimcContentTemplate, SimcProfile,
    SimcReferenceSimulation, SimcReferenceSimulationConfig,
    SimcReferenceSimulationScenario, SimcReferenceSimulationSettings, SimcTalentString, SimcTask,
)
from botend.services import simc_reference_simulations as service
from botend.tests.simc_reference_test_fixtures import publish_resources


class ReferenceScenarioTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('scenarios', 'scenario@example.test', 'test-password')
        backend, _ = SimcBackendBinary.objects.update_or_create(identifier='production', defaults={
            'name': '正式服', 'current_version': 'a' * 40, 'game_build': '12.0.1'})
        template = SimcContentTemplate.objects.create(name='共享模板', spec='default', content='optimal_raid=0')
        self.apl = SimcApl.objects.create(name='APL', spec='warrior_fury', class_name='warrior', is_system=True,
                                        is_selectable=True, content='actions=/auto_attack', source='user')
        self.talent = SimcTalentString.objects.create(name='天赋', spec='warrior_fury', talent='CYEAoonA',
                                                     is_system=True, default_apl=self.apl)
        self.profile = SimcProfile.objects.create(name='装备', spec='warrior_fury', class_name='warrior',
                                                  player_config_mode='manual_equipment',
                                                  player_equipment='warrior="测试"\nspec=fury')
        self.settings = service.initialize_settings()
        self.settings.backend, self.settings.template = backend, template
        self.settings.save()
        self.config = SimcReferenceSimulationConfig.objects.create(spec_key='warrior_fury', settings=self.settings)
        publish_resources('warrior_fury', profiles=[self.profile], talents=[self.talent], apls=[self.apl])
        self.settings.scenarios.all().update(is_enabled=False)

    def scene(self, seconds=40, targets=20, **kwargs):
        return SimcReferenceSimulationScenario.objects.create(settings=self.settings, name=f'{targets}/{seconds}',
            simulation_params={'desired_targets': targets, 'max_time': seconds, 'iterations': 10000}, **kwargs)

    def frozen(self, scene):
        return service.snapshot(self.config, self.profile, self.talent, self.apl, scene)

    def enqueue(self):
        return service.enqueue_configs([self.config.pk], self.user.pk)

    def test_same_targets_different_duration_have_independent_identity_results_and_hash(self):
        short, long = self.scene(), self.scene(300)
        self.assertEqual(self.enqueue()['created'], 2)
        rows = list(SimcReferenceSimulation.objects.order_by('pk'))
        self.assertEqual({r.scenario_id for r in rows}, {short.pk, long.pk})
        self.assertNotEqual(rows[0].signature, rows[1].signature)
        self.assertEqual(service.scenario_for_snapshot(self.config, rows[0].snapshot), short)
        with self.assertRaises(ValueError):
            self.frozen(20)
        SimcReferenceSimulation.objects.update(status='completed', dps=123.0)
        with patch('botend.services.simc_reference_rankings.current_rankings', return_value={}):
            result = service.public_results('warrior_fury', self.profile, self.talent, self.apl)
        self.assertEqual({r['scenario_id'] for r in result}, {short.pk, long.pk})
        self.assertEqual({r['duration'] for r in result}, {40, 300})
        original = self.frozen(short)
        short.name = 'renamed'
        short.save()
        self.assertEqual(original, self.frozen(short))

    def test_authoritative_ranges_and_extended_parameters_not_arbitrary_scene_limits(self):
        for target in range(21, 33):
            self.scene(targets=target)
        scene = self.scene()
        scene.simulation_params['fight_style'] = 'DungeonSlice'
        scene.save()
        self.assertEqual(len(service.active_scenarios(self.config)), 13)
        self.assertEqual(self.frozen(scene)['simulation_params']['fight_style'], 'DungeonSlice')
        scene.simulation_params['desired_targets'] = 1001
        with self.assertRaises(ValueError):
            self.frozen(scene)

    def test_disabled_or_changed_scene_during_prepare_never_submits(self):
        for change in ({'is_enabled': False}, {'simulation_params': {'desired_targets': 20, 'max_time': 300, 'iterations': 10000}}):
            with self.subTest(change=change):
                self.settings.scenarios.all().update(is_enabled=False)
                scene = self.scene()
                self.assertEqual(self.enqueue()['created'], 1)
                original = service.tasks.prepare_task_creation
                def mutate(**kwargs):
                    SimcReferenceSimulationScenario.objects.filter(pk=scene.pk).update(**change)
                    return original(**kwargs)
                with patch.object(service.tasks, 'prepare_task_creation', side_effect=mutate), patch(
                    'botend.services.simc_task_service.current_validation_identity', return_value=('a' * 40, '12.0.1')):
                    service.maintain_simulations()
                self.assertFalse(SimcTask.objects.exists())
                self.assertFalse(SimcReferenceSimulation.objects.filter(status__in=('queued', 'preparing', 'submitted')).exists())

    def test_legacy_snapshot_match_uses_all_parameters_and_defaults_preserve_hash(self):
        scene = self.scene(targets=1, seconds=300)
        frozen = self.frozen(scene)
        self.assertEqual(frozen['simulation_params'], {'desired_targets': 1, 'max_time': 300, 'iterations': 10000,
            'fight_style': 'Patchwerk', 'vary_combat_length': 0, 'raid_buffs': [],
            'use_class_raid_buff': False, 'extra_options': []})
        self.assertNotIn('scenario_id', frozen)
        self.assertEqual(self.frozen(1), frozen)
        self.scene(targets=1, seconds=40)
        self.assertEqual(service.scenario_for_snapshot(self.config, frozen), scene)
        frozen['simulation_params']['iterations'] = 123
        self.assertIsNone(service.scenario_for_snapshot(self.config, frozen))

    def test_saving_settings_and_scenarios_does_not_enqueue(self):
        self.scene()
        self.settings.save()
        self.assertFalse(SimcReferenceSimulation.objects.exists())

    def test_submitted_tasks_keep_independent_frozen_inputs_after_scene_edit(self):
        self.scene()
        self.scene(300)
        self.enqueue()
        # Legacy queued rows have no scene FK; exact full-input matching backfills it.
        SimcReferenceSimulation.objects.update(scenario=None)
        with patch('botend.services.simc_task_service.current_validation_identity', return_value=('a' * 40, '12.0.1')):
            service.maintain_simulations()
        rows = list(SimcReferenceSimulation.objects.select_related('task').order_by('pk'))
        self.assertEqual([row.status for row in rows], ['submitted', 'submitted'])
        self.assertEqual(len({row.scenario_id for row in rows}), 2)
        self.assertTrue(all(row.scenario_id for row in rows))
        self.assertEqual({row.task.simulation_params['max_time'] for row in rows}, {40, 300})
        original = {row.pk: (row.snapshot, row.signature, row.task.simulation_params) for row in rows}
        self.settings.scenarios.all().update(is_enabled=False)
        service.maintain_simulations()
        for row in SimcReferenceSimulation.objects.select_related('task'):
            self.assertEqual((row.snapshot, row.signature, row.task.simulation_params), original[row.pk])


class ReferenceScenarioMigrationTests(TransactionTestCase):
    migrate_from = [('botend', '0271_simc_reference_global_settings')]
    migrate_to = [('botend', '0272_simc_reference_scenarios')]

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def tearDown(self):
        self.migrate(self.migrate_to)
        super().tearDown()

    def test_migration_preserves_hashes_backfills_exact_matches_and_preflights_reverse_ddl(self):
        apps = self.migrate(self.migrate_from)
        Settings = apps.get_model('botend', 'SimcReferenceSimulationSettings')
        Config = apps.get_model('botend', 'SimcReferenceSimulationConfig')
        Result = apps.get_model('botend', 'SimcReferenceSimulation')
        settings, _ = Settings.objects.update_or_create(use_ptr=False, defaults={
            'duration': 300, 'targets': [1, 5], 'iterations': 10000})
        config = Config.objects.create(spec_key='warrior_fury', settings_id=settings.pk)
        params = {'desired_targets': 1, 'max_time': 300, 'iterations': 10000,
                  'fight_style': 'Patchwerk', 'vary_combat_length': 0, 'raid_buffs': [],
                  'use_class_raid_buff': False, 'extra_options': []}
        frozen = {'simulation_params': params}
        old = Result.objects.create(config_id=config.pk, requested_by_id=1, signature='a' * 64, snapshot=frozen)
        stale = Result.objects.create(config_id=config.pk, requested_by_id=1, signature='b' * 64,
                                      snapshot={'simulation_params': dict(params, max_time=40)})
        apps = self.migrate(self.migrate_to)
        Scene = apps.get_model('botend', 'SimcReferenceSimulationScenario')
        Result = apps.get_model('botend', 'SimcReferenceSimulation')
        scenes = list(Scene.objects.filter(settings_id=settings.pk).order_by('display_order', 'pk'))
        self.assertEqual([s.simulation_params['desired_targets'] for s in scenes], [1, 5])
        migrated = Result.objects.get(pk=old.pk)
        self.assertEqual((migrated.signature, migrated.snapshot, migrated.scenario_id), ('a' * 64, frozen, scenes[0].pk))
        self.assertIsNone(Result.objects.get(pk=stale.pk).scenario_id)
        mixed = Scene.objects.create(settings_id=settings.pk, name='20/40', simulation_params=dict(params, desired_targets=20, max_time=40))
        try:
            with self.assertRaisesRegex(RuntimeError, '无法无损回滚'):
                self.migrate(self.migrate_from)
            with connection.cursor() as cursor:
                columns = {column.name for column in connection.introspection.get_table_description(cursor, 'botend_simcreferencesimulationsettings')}
            self.assertNotIn('duration', columns)
        finally:
            Scene.objects.filter(pk=mixed.pk).delete()
        apps = self.migrate(self.migrate_from)
        restored = apps.get_model('botend', 'SimcReferenceSimulationSettings').objects.get(pk=settings.pk)
        self.assertEqual((restored.duration, restored.targets, restored.iterations), (300, [1, 5], 10000))
