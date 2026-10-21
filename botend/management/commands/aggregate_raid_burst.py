"""Manual burst adapter using the existing claim, heartbeat and completion fence."""
import json

from django.core.management.base import BaseCommand, CommandError

from botend.models import MonitorTask
from botend.plugin_sync import claim_monitor_task, complete_monitor_task_lease, release_monitor_task_lease
from botend.services.raid_burst_runner import TASK_NAME, run_raid_burst


class Command(BaseCommand):
    help = '独立手动爆发分析；复用已采集周期，不推进普通 WCL 发布状态'

    def add_arguments(self, parser):
        parser.add_argument('--season', type=int, dest='season_id')
        parser.add_argument('--cycle', type=int, dest='cycle_id')
        parser.add_argument('--class', dest='class_name')
        parser.add_argument('--spec', dest='spec_name')

    def handle(self, *args, **options):
        from botend.views import _MonitorTaskLeaseHeartbeat
        configured = MonitorTask.objects.filter(name=TASK_NAME).first()
        if configured is None:
            raise CommandError('RaidBurstAggregationMonitor is not configured; sync plugins first')
        task = claim_monitor_task(configured.pk)
        if task is None:
            raise CommandError('dedicated burst lease is busy')
        owner = task._monitor_task_lease_owner
        heartbeat = _MonitorTaskLeaseHeartbeat(task.pk, owner)
        try:
            heartbeat.start()
            result = run_raid_burst(task, **{key: options.get(key) for key in
                ('season_id', 'cycle_id', 'class_name', 'spec_name')})
            heartbeat.stop()
            if heartbeat.lease_lost.is_set() or not complete_monitor_task_lease(task.pk, owner):
                raise CommandError('dedicated burst lease lost before completion')
            self.stdout.write(json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            if isinstance(exc, CommandError):
                raise
            raise CommandError(f'burst aggregation failed: {type(exc).__name__}: {exc}') from exc
        finally:
            heartbeat.stop()
            release_monitor_task_lease(task.pk, owner)
