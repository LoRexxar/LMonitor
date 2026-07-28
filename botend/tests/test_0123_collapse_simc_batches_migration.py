"""Data-loss regression tests for the SimC Batch -> request Task migration."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class CollapseSimcBatchesMigrationTests(TransactionTestCase):
    migrate_from = [('botend', '0122_simcaplsymbol_trait_id')]
    migrate_to = [('botend', '0123_collapse_simc_batches_into_tasks')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        Batch = old_apps.get_model('botend', 'SimcTaskBatch')
        Task = old_apps.get_model('botend', 'SimcTask')
        Run = old_apps.get_model('botend', 'SimulationRun')
        Artifact = old_apps.get_model('botend', 'SimcTaskArtifact')

        self.manifest = '{"input_params":{"iterations":12345},"raw":true}'
        batch = Batch.objects.create(
            user_id=77,
            name='Historical comparison',
            batch_type='comparison',
            request_manifest=self.manifest,
            status=2,
            error_detail='batch detail',
            is_active=False,
        )
        # Lowest id is deliberately not the representative: is_base wins.
        first = Task.objects.create(
            user_id=77,
            name='Crit candidate',
            simc_profile_id=100,
            batch=batch,
            candidate_label='crit+1000',
            mode='comparison',
            mode_params={'round': 2, 'stats': {'crit': 1000}},
            simulation_params={'iterations': 12345},
            current_status=2,
            result_summary='{"dps": 101}',
            result_file='crit.html',
        )
        baseline = Task.objects.create(
            user_id=77,
            name='Baseline candidate',
            simc_profile_id=100,
            batch=batch,
            candidate_label='baseline',
            mode='comparison',
            mode_params={'is_base': True, 'round_number': 0},
            simulation_params={'iterations': 12345},
            current_status=2,
            result_summary='{"dps": 100}',
            result_file='base.html',
        )
        no_run = Task.objects.create(
            user_id=77,
            name='No old run candidate',
            simc_profile_id=100,
            batch=batch,
            candidate_label='haste+1000',
            mode='comparison',
            mode_params={'round_no': 3, 'stats': {'haste': 1000}},
            current_status=3,
            result_summary='legacy non-json result',
            result_file='haste.html',
            error_detail='old failure',
        )
        self.first_id = first.pk
        self.baseline_id = baseline.pk
        self.no_run_id = no_run.pk

        # Same sequence on different member tasks exercises collision handling.
        first_run = Run.objects.create(
            task=first,
            sequence=1,
            candidate_label='crit run',
            status='completed',
            result_summary={'dps': 101},
        )
        baseline_run = Run.objects.create(
            task=baseline,
            sequence=1,
            candidate_label='baseline run',
            status='completed',
            result_summary={'dps': 100},
        )
        negative_run = Run.objects.create(
            task=first,
            sequence=-1,
            candidate_label='legacy negative sequence',
            status='failed',
            error_detail='legacy sequence must not collide during reparenting',
        )
        second_negative_run = Run.objects.create(
            task=first,
            sequence=-2,
            candidate_label='second legacy negative sequence',
            status='failed',
        )
        self.first_run_id = first_run.pk
        self.baseline_run_id = baseline_run.pk
        self.negative_run_id = negative_run.pk
        self.second_negative_run_id = second_negative_run.pk

        self.run_artifact_id = Artifact.objects.create(
            task=first,
            run=first_run,
            artifact_type='html_report',
            file_path='simc/crit.html',
        ).pk
        self.task_artifact_id = Artifact.objects.create(
            task=no_run,
            run=None,
            artifact_type='log',
            file_path='simc/haste.log',
        ).pk

    def tearDown(self):
        # Leave the connection at the repository leaf state for other tests.
        MigrationExecutor(connection).migrate(self.migrate_to)
        super().tearDown()

    def test_forward_fold_is_lossless_and_uses_baseline_representative(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps

        Task = apps.get_model('botend', 'SimcTask')
        Run = apps.get_model('botend', 'SimulationRun')
        Artifact = apps.get_model('botend', 'SimcTaskArtifact')

        with self.assertRaises(LookupError):
            apps.get_model('botend', 'SimcTaskBatch')

        tasks = list(Task.objects.filter(pk__in=[
            self.first_id, self.baseline_id, self.no_run_id,
        ]))
        self.assertEqual([task.pk for task in tasks], [self.baseline_id])
        representative = tasks[0]

        analysis = representative.analysis_result
        self.assertEqual(analysis['legacy_batch']['request_manifest'], self.manifest)
        self.assertEqual(analysis['legacy_batch']['name'], 'Historical comparison')
        self.assertEqual(analysis['legacy_batch']['status'], 2)
        self.assertFalse(analysis['legacy_batch']['is_active'])
        self.assertEqual(
            {item['task_id'] for item in analysis['legacy_member_results']},
            {self.first_id, self.baseline_id, self.no_run_id},
        )
        self.assertEqual(representative.name, 'Historical comparison')
        self.assertEqual(representative.mode, 'comparison')
        self.assertEqual(representative.current_status, 2)
        self.assertEqual(representative.error_detail, 'batch detail')
        self.assertFalse(representative.is_active)
        self.assertEqual(representative.candidate_label, '')
        self.assertEqual(representative.mode_params['legacy_batch_id'], 1)
        self.assertEqual(
            representative.mode_params['request_manifest']['input_params']['iterations'],
            12345,
        )

        runs = list(Run.objects.filter(task_id=self.baseline_id).order_by('sequence'))
        self.assertEqual(len(runs), 5)  # four old runs plus no-run synthetic history
        self.assertEqual([run.sequence for run in runs], [1, 2, 3, 4, 5])
        self.assertEqual(
            {run.candidate_params['legacy_task_id'] for run in runs},
            {self.first_id, self.baseline_id, self.no_run_id},
        )
        negative_run = Run.objects.get(pk=self.negative_run_id)
        self.assertEqual(negative_run.task_id, self.baseline_id)
        self.assertEqual(negative_run.candidate_label, 'legacy negative sequence')
        self.assertEqual(
            negative_run.error_detail,
            'legacy sequence must not collide during reparenting',
        )

        first_run = Run.objects.get(pk=self.first_run_id)
        self.assertEqual(first_run.task_id, self.baseline_id)
        self.assertEqual(first_run.candidate_key, 'crit run')
        self.assertEqual(first_run.round_number, 2)
        self.assertEqual(first_run.candidate_params['mode_params']['stats']['crit'], 1000)
        self.assertEqual(first_run.candidate_params['legacy_result_file'], 'crit.html')

        baseline_run = Run.objects.get(pk=self.baseline_run_id)
        self.assertEqual(baseline_run.task_id, self.baseline_id)
        self.assertEqual(baseline_run.round_number, 0)

        synthetic = Run.objects.get(candidate_params__legacy_task_id=self.no_run_id)
        self.assertEqual(synthetic.status, 'failed')
        self.assertEqual(synthetic.result_summary, {'legacy_value': 'legacy non-json result'})
        self.assertEqual(synthetic.error_detail, 'old failure')

        artifacts = list(Artifact.objects.order_by('id'))
        self.assertEqual(len(artifacts), 2)
        self.assertEqual({artifact.task_id for artifact in artifacts}, {self.baseline_id})
        run_artifact = Artifact.objects.get(pk=self.run_artifact_id)
        self.assertEqual(run_artifact.run_id, self.first_run_id)
        self.assertEqual(run_artifact.task_id, run_artifact.run.task_id)
        self.assertIsNone(Artifact.objects.get(pk=self.task_artifact_id).run_id)
