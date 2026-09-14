from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class DisableAdventureJournalMonitorMigrationTests(TransactionTestCase):
    migrate_from = [('botend', '0225_merge_journal_talent_versions')]
    migrate_to = [('botend', '0226_disable_adventure_journal_monitor')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        MonitorTask = old_apps.get_model('botend', 'MonitorTask')
        self.journal_task_id = MonitorTask.objects.create(
            name='AdventureJournalMonitor',
            target='https://wago.tools/journal',
            type=35,
            is_active=True,
        ).pk
        self.other_task_id = MonitorTask.objects.create(
            name='OtherMonitor',
            target='',
            type=34,
            is_active=True,
        ).pk

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.migrate_to)
        super().tearDown()

    def test_existing_journal_monitor_is_disabled_without_touching_other_tasks(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        MonitorTask = apps.get_model('botend', 'MonitorTask')

        self.assertFalse(MonitorTask.objects.get(pk=self.journal_task_id).is_active)
        self.assertTrue(MonitorTask.objects.get(pk=self.other_task_id).is_active)
