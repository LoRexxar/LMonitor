from django.core.management.base import BaseCommand, CommandError

from botend.models import SimcBenchmarkExecution
from botend.services.simc_benchmark_execution import backfill_completed_case_results


class Command(BaseCommand):
    help = 'Backfill missing immutable results for successful Cases in completed Benchmark Executions.'

    def add_arguments(self, parser):
        parser.add_argument('--execution-id', type=int, required=True)

    def handle(self, *args, **options):
        execution_id = options['execution_id']
        try:
            execution = SimcBenchmarkExecution.objects.get(pk=execution_id)
        except SimcBenchmarkExecution.DoesNotExist as exc:
            raise CommandError(f'Benchmark Execution {execution_id} does not exist') from exc
        rows = backfill_completed_case_results(execution)
        self.stdout.write(self.style.SUCCESS(
            f'Execution {execution.pk}: backfilled {rows} result rows.'
        ))
