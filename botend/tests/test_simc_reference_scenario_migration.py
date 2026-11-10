"""Real 0271 -> 0272 migration: identities/frozen inputs and lossless rollback."""
from copy import deepcopy

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ReferenceScenarioMigrationTests(TransactionTestCase):
    old = [('botend', '0271_simc_reference_global_settings')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        targets = [node for node in executor.loader.graph.nodes
                   if node[0] == 'botend' and node[1].startswith('0272_')]
        self.assertEqual(len(targets), 1, 'Independent scenarios require a real 0272 migration')
        self.new = targets
        executor.migrate(self.old)
        self.apps = executor.loader.project_state(self.old).apps
        Settings = self.apps.get_model('botend', 'SimcReferenceSimulationSettings')
        self.settings, _ = Settings.objects.update_or_create(use_ptr=False, defaults={
            'duration': 300, 'targets': [1, 5], 'iterations': 10000, 'is_enabled': True,
        })
        Config = self.apps.get_model('botend', 'SimcReferenceSimulationConfig')
        self.config = Config.objects.create(spec_key='warrior_fury', settings_id=self.settings.pk)

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def migrate(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.new)
        return executor.loader.project_state(self.new).apps

    @staticmethod
    def params(targets=1, duration=300, iterations=10000):
        return {'desired_targets': targets, 'max_time': duration, 'iterations': iterations,
                'fight_style': 'Patchwerk', 'vary_combat_length': 0,
                'raid_buffs': [], 'use_class_raid_buff': False, 'extra_options': []}

    def test_forward_preserves_config_publication_hashes_task_and_run_frozen_inputs(self):
        Backend = self.apps.get_model('botend', 'SimcBackendBinary')
        backend, _ = Backend.objects.get_or_create(identifier='production', defaults={'name': 'retail'})
        Task = self.apps.get_model('botend', 'SimcTask')
        Run = self.apps.get_model('botend', 'SimulationRun')
        Result = self.apps.get_model('botend', 'SimcReferenceSimulation')
        Profile = self.apps.get_model('botend', 'SimcProfile')
        Publication = self.apps.get_model('botend', 'SimcReferencePublication')
        profile = Profile.objects.create(name='kept public profile', spec='warrior_fury')
        publication = Publication.objects.create(spec_key='warrior_fury', is_published=True)
        publication.profiles.add(profile)
        params = self.params()
        task = Task.objects.create(user_id=1, simc_profile_id=profile.pk, name='legacy frozen',
                                   backend_id=backend.pk, simulation_params=params,
                                   mode_params={'frozen': 'unchanged'}, current_status=2)
        run = Run.objects.create(task_id=task.pk, sequence=1, status='completed',
                                 resource_manifest={'backend_version': 'a' * 40, 'frozen': ['kept']},
                                 result_summary={'dps': 123, 'valid': True})
        snapshot = {'simulation_params': params, 'resources': {'frozen': 'unchanged'}}
        matched = Result.objects.create(config_id=self.config.pk, task_id=task.pk, requested_by_id=1,
                                        profile_id=profile.pk, signature='a' * 64, snapshot=snapshot,
                                        status='completed', dps=123)
        unmatched_snapshot = deepcopy(snapshot)
        unmatched_snapshot['simulation_params']['max_time'] = 40
        unmatched = Result.objects.create(config_id=self.config.pk, requested_by_id=1,
                                          signature='b' * 64, snapshot=unmatched_snapshot,
                                          status='completed', dps=456)
        extended_snapshot = deepcopy(snapshot)
        extended_snapshot['simulation_params']['fight_style'] = 'HecticAddCleave'
        extended = Result.objects.create(config_id=self.config.pk, requested_by_id=1,
                                         signature='c' * 64, snapshot=extended_snapshot,
                                         status='completed', dps=789)
        before_task = Task.objects.values().get(pk=task.pk)
        before_run = Run.objects.values().get(pk=run.pk)
        before_results = list(Result.objects.order_by('pk').values())
        config_ids = list(self.apps.get_model('botend', 'SimcReferenceSimulationConfig').objects.order_by('pk').values_list('pk', flat=True))
        apps = self.migrate()
        Scene = apps.get_model('botend', 'SimcReferenceSimulationScenario')
        scenes = list(Scene.objects.filter(settings_id=self.settings.pk).order_by('display_order', 'pk'))
        self.assertEqual([scene.simulation_params for scene in scenes], [self.params(1), self.params(5)])
        self.assertTrue(all(scene.name and scene.is_enabled for scene in scenes))
        self.assertEqual(list(apps.get_model('botend', 'SimcReferenceSimulationConfig').objects.order_by('pk').values_list('pk', flat=True)), config_ids)
        restored = apps.get_model('botend', 'SimcReferenceSimulation')
        self.assertEqual(restored.objects.get(pk=matched.pk).scenario_id, scenes[0].pk)
        self.assertIsNone(restored.objects.get(pk=unmatched.pk).scenario_id)
        self.assertIsNone(restored.objects.get(pk=extended.pk).scenario_id)
        after_results = list(restored.objects.order_by('pk').values())
        for row in after_results:
            row.pop('scenario_id')
        self.assertEqual(after_results, before_results)
        self.assertEqual(apps.get_model('botend', 'SimcTask').objects.values().get(pk=task.pk), before_task)
        self.assertEqual(apps.get_model('botend', 'SimulationRun').objects.values().get(pk=run.pk), before_run)
        kept_publication = apps.get_model('botend', 'SimcReferencePublication').objects.get(pk=publication.pk)
        self.assertTrue(kept_publication.is_published)
        self.assertEqual(list(kept_publication.profiles.values_list('pk', flat=True)), [profile.pk])
        # Existing uniform conditions can still be reversed without rewriting results.
        MigrationExecutor(connection).migrate(self.old)
        legacy = self.apps.get_model('botend', 'SimcReferenceSimulationSettings').objects.get(pk=self.settings.pk)
        self.assertEqual((legacy.duration, legacy.targets, legacy.iterations), (300, [1, 5], 10000))
        self.assertEqual(list(Result.objects.order_by('pk').values()), before_results)

    def assert_reverse_rejected_before_ddl(self, extra_params):
        apps = self.migrate()
        Scene = apps.get_model('botend', 'SimcReferenceSimulationScenario')
        extra = Scene.objects.create(settings_id=self.settings.pk, name='独立扩展场景',
                                     simulation_params=extra_params, display_order=2)
        before = list(Scene.objects.order_by('pk').values())
        tables_before = connection.introspection.table_names()
        Settings = apps.get_model('botend', 'SimcReferenceSimulationSettings')
        with connection.cursor() as cursor:
            columns_before = [col.name for col in connection.introspection.get_table_description(cursor, Settings._meta.db_table)]
        with self.assertRaises(RuntimeError):
            MigrationExecutor(connection).migrate(self.old)
        self.assertEqual(connection.introspection.table_names(), tables_before)
        with connection.cursor() as cursor:
            self.assertEqual([col.name for col in connection.introspection.get_table_description(cursor, Settings._meta.db_table)], columns_before)
        self.assertEqual(list(Scene.objects.order_by('pk').values()), before)
        self.assertTrue(Scene.objects.filter(pk=extra.pk).exists())
        self.assertIn(self.new[0], MigrationExecutor(connection).loader.applied_migrations)

    def test_reverse_rejects_same_targets_with_independent_duration_before_ddl(self):
        self.assert_reverse_rejected_before_ddl(self.params(5, duration=40))

    def test_reverse_rejects_independent_iterations_before_ddl(self):
        self.assert_reverse_rejected_before_ddl(self.params(20, iterations=2000))
