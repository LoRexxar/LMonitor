"""Regression coverage for assigning every historical SimC Task to production."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class SimcMultipleBackendsMigrationTests(TransactionTestCase):
    migrate_from = [('botend', '0124_complete_simc_secondary_stat_rules')]
    migrate_to = [('botend', '0125_simc_multiple_backends')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        Backend = old_apps.get_model('botend', 'SimcBackendBinary')
        Task = old_apps.get_model('botend', 'SimcTask')
        existing = Backend.objects.order_by('id').first()
        existing.platform = 'linux64'
        existing.simc_path = '/opt/simc/production'
        existing.current_version = 'a' * 40
        existing.save()
        self.legacy_backend_id = existing.pk
        self.other_backend_id = Backend.objects.create(
            platform='linuxarm64',
            simc_path='/opt/simc/other',
            current_version='b' * 40,
        ).pk
        self.task_ids = [
            Task.objects.create(
                user_id=7,
                name=f'Historical task {index}',
                simc_profile_id=100 + index,
                task_type=1,
            ).pk
            for index in range(2)
        ]

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.migrate_to)
        super().tearDown()

    def test_forward_migration_normalizes_production_and_backfills_all_tasks(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps

        Backend = apps.get_model('botend', 'SimcBackendBinary')
        Task = apps.get_model('botend', 'SimcTask')
        production = Backend.objects.get(identifier='production')

        self.assertEqual(production.pk, self.legacy_backend_id)
        self.assertEqual(production.name, '正式服')
        self.assertTrue(production.is_active)
        self.assertEqual(production.simc_path, '/opt/simc/production')
        other = Backend.objects.get(pk=self.other_backend_id)
        self.assertEqual(other.identifier, f'backend-{self.other_backend_id}')
        self.assertEqual(other.name, f'SimC 后端 #{self.other_backend_id}')
        self.assertEqual(
            set(Task.objects.filter(pk__in=self.task_ids).values_list('backend_id', flat=True)),
            {production.pk},
        )
        backend_field = Task._meta.get_field('backend')
        self.assertFalse(backend_field.null)
        self.assertEqual(backend_field.remote_field.on_delete.__name__, 'PROTECT')
