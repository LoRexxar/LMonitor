import signal
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Case, IntegerField, Value, When
from django.utils import timezone

from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.models import SimcBenchmarkCase, SimcTask
from botend.services import simc_benchmark_scheduler
from botend.services.task_rerun import create_rerun, TaskRerunError
from utils.log import logger


class SimcWorker:
    """持久化 SimC 队列的单进程消费者。"""

    def __init__(self, monitor=None, poll_interval=None, maintenance_interval=None):
        self.monitor = monitor or SimcMonitor(None, None)
        self.poll_interval = float(
            poll_interval if poll_interval is not None
            else getattr(settings, 'SIMC_WORKER_POLL_INTERVAL', 5)
        )
        self.stale_seconds = int(getattr(settings, 'SIMC_WORKER_STALE_SECONDS', 900) or 900)
        self.max_attempts = int(getattr(settings, 'SIMC_WORKER_MAX_ATTEMPTS', 3) or 3)
        self.maintenance_interval = float(
            maintenance_interval if maintenance_interval is not None
            else getattr(settings, 'SIMC_WORKER_MAINTENANCE_INTERVAL', 30)
        )
        self._stop = threading.Event()

    def request_stop(self, *_args):
        self._stop.set()

    def recover_stale_tasks(self):
        """Fail stale Tasks and retry by copying the frozen Task request."""
        threshold = timezone.now() - timedelta(seconds=self.stale_seconds)
        recovered = 0
        task_ids = list(SimcTask.objects.filter(
            is_active=True, current_status=1,
        ).filter(modified_time__lt=threshold).values_list('id', flat=True))
        for task_id in task_ids:
            is_benchmark = False
            try:
                with transaction.atomic():
                    task = SimcTask.objects.select_for_update().filter(
                        id=task_id, is_active=True, current_status=1,
                        modified_time__lt=threshold,
                    ).first()
                    if task is None:
                        continue
                    benchmark_case = SimcBenchmarkCase.objects.select_for_update().filter(
                        task_id=task.id,
                    ).first()
                    is_benchmark = benchmark_case is not None

                    attempts = 1
                    ancestor_id = task.source_task_id
                    seen = {task.id}
                    while ancestor_id and ancestor_id not in seen:
                        seen.add(ancestor_id)
                        attempts += 1
                        ancestor_id = SimcTask.objects.filter(
                            id=ancestor_id,
                        ).values_list('source_task_id', flat=True).first()

                    task.current_status = 3
                    task.completed_at = timezone.now()
                    if attempts >= self.max_attempts:
                        task.error_detail = f'Worker 重试次数上限（{self.max_attempts}）'
                    else:
                        task.error_detail = 'Worker 心跳超时，执行已中断；已复制 Task 重试'
                    task.save(update_fields=[
                        'current_status', 'error_detail', 'completed_at', 'modified_time',
                    ])

                    if attempts < self.max_attempts:
                        try:
                            new_task = create_rerun(task.id, task.user_id)
                            if benchmark_case is not None:
                                rebound = SimcBenchmarkCase.objects.filter(
                                    pk=benchmark_case.pk, task_id=task.id,
                                ).update(task_id=new_task.id)
                                if rebound != 1:
                                    raise RuntimeError(
                                        'benchmark Case authority changed during stale retry'
                                    )
                        except TaskRerunError as exc:
                            task.error_detail = f'Worker 心跳超时，Task 重试复制失败: {exc}'
                            task.save(update_fields=['error_detail', 'modified_time'])
                    recovered += 1
            except Exception:
                # A failed benchmark rebind rolls back stale marking and Task creation,
                # so no retry can be orphaned from its Execution. Preserve historical
                # propagation for unrelated Tasks.
                if not is_benchmark:
                    raise
                logger.exception('[SimC Worker] stale task %s recovery failed', task_id)
        return recovered

    def _mark_unexpected_failure(self, task, exc, claimed_at):
        SimcTask.objects.filter(
            pk=task.pk,
            current_status=1,
            started_at=claimed_at,
        ).update(
            current_status=3,
            error_detail='Unexpected worker error.',
            completed_at=timezone.now(),
            modified_time=timezone.now(),
        )

    def _perform_maintenance(self):
        """Run isolated queue maintenance from idle and long-running task paths."""
        try:
            self.recover_stale_tasks()
        except Exception:
            logger.exception('[SimC Worker] stale task recovery failed')
        try:
            simc_benchmark_scheduler.schedule_due_panels()
        except Exception:
            logger.exception('[SimC Worker] benchmark scheduler failed')
        try:
            simc_benchmark_scheduler.reconcile_pending_executions()
        except Exception:
            logger.exception('[SimC Worker] benchmark reconcile sweep failed')

    def _heartbeat_cycle(self, task_id, claimed_at):
        """Refresh the active lease and keep maintenance alive while SimC blocks."""
        SimcTask.objects.filter(
            pk=task_id,
            current_status=1,
            started_at=claimed_at,
        ).update(modified_time=timezone.now())
        self._perform_maintenance()

    def _start_heartbeat(self, task_id, claimed_at):
        """Refresh the task lease while one long SimC subprocess is blocking."""
        stopped = threading.Event()
        interval = max(1.0, min(float(self.stale_seconds) / 3.0, 30.0))

        def beat():
            while not stopped.wait(interval):
                close_old_connections()
                try:
                    self._heartbeat_cycle(task_id, claimed_at)
                except Exception:
                    logger.exception('[SimC Worker] task %s heartbeat failed', task_id)
                finally:
                    close_old_connections()

        thread = threading.Thread(target=beat, name=f'simc-heartbeat-{task_id}', daemon=True)
        thread.start()
        return stopped, thread

    def consume_once(self):
        """只领取一个 pending Task；返回是否发现任务。"""
        close_old_connections()
        task = SimcTask.objects.filter(
            is_active=True, current_status=0,
        ).annotate(
            queue_priority=Case(
                When(benchmark_case__isnull=True, then=Value(0)),
                default=Value(1), output_field=IntegerField(),
            ),
        ).order_by('queue_priority', 'create_time', 'id').first()
        if task is None:
            return False
        claimed_at = None
        claimed = 0
        try:
            claimed_at = timezone.now()
            claimed = SimcTask.objects.filter(
                id=task.id,
                is_active=True,
                current_status=0,
            ).update(
                current_status=1,
                started_at=claimed_at,
                completed_at=None,
                modified_time=claimed_at,
            )
            if claimed != 1:
                return True
            task.refresh_from_db()
            heartbeat_stop, heartbeat_thread = self._start_heartbeat(task.id, claimed_at)
            try:
                result = self.monitor.process_simc_task(task, already_claimed=True)
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=2)
            # 测试替身或旧实现若只返回成功，不应让队列永久停在 running。
            if result:
                SimcTask.objects.filter(
                    pk=task.pk,
                    current_status=1,
                    started_at=claimed_at,
                ).update(
                    current_status=2,
                    completed_at=timezone.now(),
                    modified_time=timezone.now(),
                )
        except Exception as exc:
            logger.exception('[SimC Worker] task %s failed', task.id)
            self._mark_unexpected_failure(task, exc, claimed_at)
        if claimed == 1:
            try:
                simc_benchmark_scheduler.reconcile_execution_for_task(task.id)
            except Exception:
                # Reconciliation is projection/metadata maintenance and can never
                # alter the already committed Task result.
                logger.exception('[SimC Worker] task %s benchmark reconcile failed', task.id)
        return True

    def run(self):
        next_maintenance = time.monotonic()
        while not self._stop.is_set():
            current = time.monotonic()
            if current >= next_maintenance:
                self._perform_maintenance()
                next_maintenance = current + max(self.maintenance_interval, 0)
            try:
                if not self.consume_once():
                    self._stop.wait(self.poll_interval)
            except Exception:
                logger.exception('[SimC Worker] consume loop error')
                self._stop.wait(min(max(self.poll_interval, 1), 10))
