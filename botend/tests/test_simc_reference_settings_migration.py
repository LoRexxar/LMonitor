"""Exercise the real 0270 -> 0271 schema/data migration and conflict guard."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class GlobalSettingsMigrationTests(TransactionTestCase):
    old = [('botend', '0270_simc_reference_publication')]
    new = [('botend', '0271_simc_reference_global_settings')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.old)
        self.apps = executor.loader.project_state(self.old).apps

    def tearDown(self):
        # A deliberately conflicting fixture must be resolved before restoring schema.
        Config = self.apps.get_model('botend', 'SimcReferenceSimulationConfig')
        if 'settings_id' not in {col.name for col in connection.introspection.get_table_description(connection.cursor(), Config._meta.db_table)}:
            Config.objects.update(duration=300)
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def migrate(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.new)
        return executor.loader.project_state(self.new).apps

    def test_forty_equal_configs_preserve_ids_conditions_and_frozen_history(self):
        Config = self.apps.get_model('botend', 'SimcReferenceSimulationConfig')
        Simulation = self.apps.get_model('botend', 'SimcReferenceSimulation')
        Backend = self.apps.get_model('botend', 'SimcBackendBinary')
        Template = self.apps.get_model('botend', 'SimcContentTemplate')
        backend, _ = Backend.objects.update_or_create(identifier='production', defaults={'name': 'retail', 'current_version': 'a' * 40})
        template = Template.objects.create(name='global', spec='default', content='optimal_raid=0')
        ids = [Config.objects.create(spec_key=f'spec_{i}', backend_id=backend.pk, template_id=template.pk, duration=300, targets=[1, 5], iterations=10000).pk for i in range(40)]
        frozen = {'simulation_params': {'max_time': 300, 'iterations': 10000, 'desired_targets': 1}, 'resources': {'kept': 'unchanged'}}
        row = Simulation.objects.create(config_id=ids[0], requested_by_id=1, signature='a' * 64, snapshot=frozen, status='completed', dps=123)
        apps = self.migrate()
        Config = apps.get_model('botend', 'SimcReferenceSimulationConfig')
        Settings = apps.get_model('botend', 'SimcReferenceSimulationSettings')
        Simulation = apps.get_model('botend', 'SimcReferenceSimulation')
        self.assertEqual(list(Config.objects.order_by('pk').values_list('pk', flat=True)), ids)
        self.assertEqual(Config.objects.values('settings_id').distinct().count(), 1)
        settings = Settings.objects.get(use_ptr=False)
        self.assertEqual((settings.backend_id, settings.template_id), (backend.pk, template.pk))
        self.assertEqual((settings.duration, settings.targets, settings.iterations, settings.is_enabled), (300, [1, 5], 10000, True))
        restored = Simulation.objects.get(pk=row.pk)
        self.assertEqual((restored.config_id, restored.signature, restored.snapshot, restored.status, restored.dps), (ids[0], 'a' * 64, frozen, 'completed', 123))
        # Reverse migration is a data-preserving schema rollback, not code-only rollback.
        MigrationExecutor(connection).migrate(self.old)
        Legacy = self.apps.get_model('botend', 'SimcReferenceSimulationConfig')
        self.assertEqual(list(Legacy.objects.order_by('pk').values_list('pk', flat=True)), ids)
        self.assertEqual(set(Legacy.objects.values_list('duration', flat=True)), {300})

    def test_conflict_aborts_before_ddl_and_preserves_legacy_rows(self):
        Config = self.apps.get_model('botend', 'SimcReferenceSimulationConfig')
        a = Config.objects.create(spec_key='warrior_fury', duration=300)
        b = Config.objects.create(spec_key='mage_fire', duration=240)
        with self.assertRaisesRegex(RuntimeError, '不一致'):
            self.migrate()
        self.assertEqual(list(Config.objects.order_by('pk').values_list('pk', 'duration')), [(a.pk, 300), (b.pk, 240)])
        self.assertNotIn('botend_simcreferencesimulationsettings', connection.introspection.table_names())
