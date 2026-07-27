"""Regression tests for the Benchmark aggregate schema/data migration split."""

from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class SimcBenchmarkResultMigrationTests(TransactionTestCase):
    migrate_from = [('botend', '0131_simctask_queue_indexes')]
    migrate_to = [('botend', '0133_backfill_simc_benchmark_results')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        Panel = old_apps.get_model('botend', 'SimcBenchmarkPanel')
        Execution = old_apps.get_model('botend', 'SimcBenchmarkExecution')

        panel = Panel.objects.create(
            name='Historical panel', slug='historical-panel', created_by_id=77,
        )
        older = Execution.objects.create(
            panel=panel, trigger='manual', config_snapshot={}, config_hash='old',
        )
        newer = Execution.objects.create(
            panel=panel, trigger='manual', config_snapshot={}, config_hash='new',
        )
        now = timezone.now()
        Execution.objects.filter(pk=older.pk).update(created_at=now - timedelta(minutes=2))
        Execution.objects.filter(pk=newer.pk).update(created_at=now - timedelta(minutes=1))
        self.panel_id = panel.pk
        self.older_id = older.pk
        self.newer_id = newer.pk

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.migrate_to)
        super().tearDown()

    def test_forward_migration_claims_newest_execution_and_terminalizes_duplicate(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        Panel = apps.get_model('botend', 'SimcBenchmarkPanel')
        Execution = apps.get_model('botend', 'SimcBenchmarkExecution')

        panel = Panel.objects.get(pk=self.panel_id)
        older = Execution.objects.get(pk=self.older_id)
        newer = Execution.objects.get(pk=self.newer_id)

        self.assertEqual(panel.active_execution_id, newer.pk)
        self.assertEqual(older.status, 'failed')
        self.assertIsNotNone(older.completed_at)
        self.assertEqual(older.result_hash, '')
        self.assertIsNone(older.results_finalized_at)
        self.assertEqual(newer.status, 'pending')
        self.assertIsNone(newer.completed_at)
