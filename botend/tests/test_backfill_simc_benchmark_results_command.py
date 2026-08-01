from io import StringIO
from copy import deepcopy

from django.core.management import call_command
from django.core.management.base import CommandError

from botend.models import (
    SimcBenchmarkExecution, SimcBenchmarkResult, SimcBenchmarkCandidate, SimulationRun,
    WowItemSnapshot,
)
from botend.tests.test_simc_benchmark_execution import SimcBenchmarkExecutionTests


class BackfillSimcBenchmarkResultsCommandTests(SimcBenchmarkExecutionTests):
    def test_requires_existing_execution(self):
        with self.assertRaises(CommandError):
            call_command('backfill_simc_benchmark_results', execution_id=99999)

    def test_backfills_successful_case_rows(self):
        execution = self._create()
        task = execution.cases.get().task
        task.current_status = 2
        task.save(update_fields=['current_status'])
        self._run(task, 1, 'completed', 'baseline', dps=1234)
        self._run(task, 2, 'completed', 'trinket', dps=1256)
        from botend.services.simc_benchmark_execution import reconcile_execution
        reconcile_execution(execution)
        SimcBenchmarkResult.objects.filter(case__execution=execution).delete()

        output = StringIO()
        call_command('backfill_simc_benchmark_results', execution_id=execution.pk, stdout=output)

        self.assertIn('backfilled 2 result rows', output.getvalue())
        self.assertEqual(execution.cases.get().results.count(), 2)

    def test_backfills_existing_candidate_and_run_display_metadata(self):
        execution = self._create()
        task = execution.cases.get().task
        candidate = SimcBenchmarkCandidate.objects.get(panel=execution.panel, key='trinket')
        gear_swap = {
            'item_id': 248583,
            'slot': 'trinket1',
            'source': 'manual',
            'raw_value': ',id=248583,ilevel=285,bonus_id=13183',
        }
        candidate.params['gear_swap'].update(gear_swap)
        candidate.save(update_fields=['params'])
        execution.config_snapshot['candidates'][1]['params']['gear_swap'].update(gear_swap)
        execution.save(update_fields=['config_snapshot'])
        run = SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='trinket', candidate_label='Trinket',
            candidate_params={
                'candidate_type': 'gear_swap', 'is_base': False,
                'gear_swap': gear_swap,
            },
        )
        # Stale recovery/retry atomically rebinds the Case to a replacement Task;
        # the original Task and its Runs remain valid Benchmark history.
        benchmark_case = execution.cases.get()
        benchmark_case.task = None
        benchmark_case.save(update_fields=['task'])
        self.assertEqual(candidate.label, 'Trinket')
        self.assertEqual(run.candidate_label, 'Trinket')
        self.assertFalse(run.candidate_params.get('icon_url'))
        self.assertFalse(run.display_metadata.get('icon_url'))
        WowItemSnapshot.objects.create(
            item_id=248583, name='Drum of Renewed Bonds', name_zh='焕新羁绊之鼓', icon='inv_trinket_raid_01',
        )

        snapshot_before = deepcopy(execution.config_snapshot)
        hash_before = execution.config_hash
        output = StringIO()
        call_command('backfill_simc_benchmark_display_metadata', stdout=output)

        candidate.refresh_from_db()
        run.refresh_from_db()
        execution.refresh_from_db()
        self.assertEqual(candidate.label, '焕新羁绊之鼓 · 暴击')
        self.assertEqual(candidate.icon_url, '/static/wow_icons/small/inv_trinket_raid_01.jpg')
        self.assertEqual(run.candidate_label, '焕新羁绊之鼓 · 暴击')
        self.assertEqual(
            run.display_metadata['icon_url'],
            '/static/wow_icons/small/inv_trinket_raid_01.jpg',
        )
        self.assertEqual(execution.config_snapshot, snapshot_before)
        self.assertEqual(execution.config_hash, hash_before)
        self.assertEqual(execution.display_metadata['trinket'], {
            'label': '焕新羁绊之鼓 · 暴击',
            'icon_url': '/static/wow_icons/small/inv_trinket_raid_01.jpg',
        })
        self.assertIn('updated 1 candidates, 1 runs, and 1 executions', output.getvalue())

        repeat = StringIO()
        call_command('backfill_simc_benchmark_display_metadata', stdout=repeat)
        self.assertIn('updated 0 candidates, 0 runs, and 0 executions', repeat.getvalue())
