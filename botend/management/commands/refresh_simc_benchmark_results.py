"""Regenerate result JSON only. Never creates or reruns a simulation."""
from django.core.management.base import BaseCommand, CommandError
from botend.models import SimcBenchmarkPanel
from botend.services.simc_benchmark_result_snapshot import (
    _lock, rebuild_panel_result_snapshot, refresh_pending_result_snapshots,
    request_snapshot_refresh, snapshot_root,
)


class Command(BaseCommand):
    help = 'Rebuild Benchmark result JSON (no Task/Run creation).'

    def add_arguments(self, parser):
        parser.add_argument('--panel', type=int, action='append', default=[])
        parser.add_argument('--pending', action='store_true')

    def handle(self, *args, **options):
        if bool(options['panel']) == options['pending']:
            raise CommandError('Choose --panel ID (repeatable) or --pending')
        if options['pending']:
            built = refresh_pending_result_snapshots(batch_size=20)
        else:
            built = []
            # Share the maintenance mutex: never duplicate expensive builders.
            with _lock(snapshot_root() / 'worker.lock'):
                for panel_id in options['panel']:
                    if not SimcBenchmarkPanel.objects.filter(pk=panel_id, is_active=True).exists():
                        raise CommandError(f'Active panel {panel_id} not found')
                    request_snapshot_refresh(panel_id)
                    if rebuild_panel_result_snapshot(panel_id):
                        built.append(panel_id)
                    else:
                        self.stdout.write(f'Panel {panel_id}: already rebuilding; refresh remains queued')
        self.stdout.write(f'Benchmark JSON rebuilt: {built}')
