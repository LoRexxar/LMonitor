from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class SimcAgentLeaseMigrationTests(TransactionTestCase):
    migrate_from = [('botend', '0136_simc_agent_identity')]
    migrate_to = [('botend', '0137_simulationrun_agent_lease')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        apps = executor.loader.project_state(self.migrate_from).apps
        Backend = apps.get_model('botend', 'SimcBackendBinary')
        Task = apps.get_model('botend', 'SimcTask')
        Run = apps.get_model('botend', 'SimulationRun')
        Artifact = apps.get_model('botend', 'SimcTaskArtifact')

        backend = Backend.objects.create(identifier='migration-test', name='Migration test')
        task = Task.objects.create(
            user_id=1, name='Historical task', simc_profile_id=1, backend=backend,
        )
        run = Run.objects.create(task=task, sequence=1)
        Artifact.objects.create(
            task=task, run=run, artifact_type='html_report',
            file_path='simc_results/older.html', file_size=1,
        )
        newest = Artifact.objects.create(
            task=task, run=run, artifact_type='html_report',
            file_path='simc_results/newest.html', file_size=2,
        )
        self.run_id = run.pk
        self.newest_id = newest.pk

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.migrate_to)
        super().tearDown()

    def test_forward_migration_keeps_newest_duplicate_before_unique_constraint(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        Artifact = apps.get_model('botend', 'SimcTaskArtifact')
        artifacts = list(Artifact.objects.filter(
            run_id=self.run_id, artifact_type='html_report'))
        self.assertEqual([artifact.pk for artifact in artifacts], [self.newest_id])
        self.assertEqual(artifacts[0].file_path, 'simc_results/newest.html')
