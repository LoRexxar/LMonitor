from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

from botend.models import SimcBenchmarkResult
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
