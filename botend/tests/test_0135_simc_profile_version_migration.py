from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class SimcProfileVersionMigrationTests(TransactionTestCase):
    migrate_from = [('botend', '0134_merge_20260728_1055')]
    migrate_to = [('botend', '0135_simcprofile_version')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        SimcProfile = old_apps.get_model('botend', 'SimcProfile')
        self.profile_ids = [
            SimcProfile.objects.create(
                user_id=None,
                name='系统内置 Profile',
                source='simc_upstream',
                system_key='simc_upstream:warrior_fury',
                spec='warrior_fury',
            ).pk,
            SimcProfile.objects.create(
                user_id=42,
                name='现有用户 Profile',
                source='user',
                spec='warrior_fury',
            ).pk,
        ]

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.migrate_to)
        super().tearDown()

    def test_existing_profiles_are_backfilled_to_version_12_0(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        SimcProfile = apps.get_model('botend', 'SimcProfile')

        versions = list(
            SimcProfile.objects.filter(pk__in=self.profile_ids)
            .order_by('pk')
            .values_list('version', flat=True)
        )
        self.assertEqual(versions, ['12.0', '12.0'])
