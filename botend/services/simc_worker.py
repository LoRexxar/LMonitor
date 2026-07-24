import signal
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.models import SimcTask, SimulationRun
from utils.log import logger


class SimcWorker:
    """持久化 SimC 队列的单进程消费者。"""

    def __init__(self, monitor=None, poll_interval=None):
        self.monitor = monitor or SimcMonitor(None, None)
        self.poll_interval = float(
            poll_interval if poll_interval is not None
            else getattr(settings, 'SIMC_WORKER_POLL_INTERVAL', 5)
        )
        self.stale_seconds = int(getattr(settings, 'SIMC_WORKER_STALE_SECONDS', 900) or 900)
        self.max_attempts = int(getattr(settings, 'SIMC_WORKER_MAX_ATTEMPTS', 3) or 3)
        self._stop = threading.Event()

    def request_stop(self, *_args):
        self._stop.set()

    def recover_stale_tasks(self):
        """回收无心跳的 Task，并按候选 Run 独立追加有限次重试。"""
        threshold = timezone.now() - timedelta(seconds=self.stale_seconds)
        recovered = 0
        tasks = SimcTask.objects.filter(
            is_active=True, current_status=1,
        ).filter(modified_time__lt=threshold)
        for task in tasks:
            runs = list(SimulationRun.objects.filter(task=task).order_by('sequence'))
            running = [run for run in runs if run.status == 'running']
            next_sequence = max((run.sequence for run in runs), default=0) + 1
            retry_rows = []
            for run in running:
                run.status = 'failed'
                run.error_detail = 'Worker 回收超时 running Run，原执行已中断'
                run.completed_at = timezone.now()
                run.save(update_fields=['status', 'error_detail', 'completed_at'])
                attempts = sum(1 for item in runs if item.candidate_key == run.candidate_key)
                if attempts < self.max_attempts:
                    retry_rows.append(SimulationRun(
                        task=task, sequence=next_sequence,
                        candidate_key=run.candidate_key,
                        candidate_label=run.candidate_label,
                        round_number=run.round_number,
                        candidate_params=run.candidate_params,
                        status='pending',
                    ))
                    next_sequence += 1
            if retry_rows:
                SimulationRun.objects.bulk_create(retry_rows)
            has_work = bool(retry_rows or any(run.status == 'pending' for run in runs))
            if not has_work:
                task.current_status = 3
                task.error_detail = f'Worker 重试次数上限（{self.max_attempts}）'
                task.completed_at = timezone.now()
                task.save(update_fields=['current_status', 'error_detail', 'completed_at', 'modified_time'])
            else:
                task.current_status = 0
                task.started_at = None
                task.error_detail = 'Worker 回收超时任务，准备重试'
                task.save(update_fields=['current_status', 'started_at', 'error_detail', 'modified_time'])
            recovered += 1
        return recovered

    def _mark_unexpected_failure(self, task, exc):
        reason = f'Worker 单任务异常: {exc}'
        try:
            self.monitor.mark_task_failed(task, reason, exc)
        except Exception:
            pass
        task.refresh_from_db()
        if task.current_status == 1:
            SimcTask.objects.filter(pk=task.pk).update(
                current_status=3, error_detail=reason, completed_at=timezone.now()
            )

    def _start_heartbeat(self, task_id):
        """Refresh the task lease while one long SimC subprocess is blocking."""
        stopped = threading.Event()
        interval = max(1.0, min(float(self.stale_seconds) / 3.0, 30.0))

        def beat():
            while not stopped.wait(interval):
                close_old_connections()
                try:
                    SimcTask.objects.filter(pk=task_id, current_status=1).update(
                        modified_time=timezone.now(),
                    )
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
        ).order_by('modified_time', 'id').first()
        if task is None:
            return False
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
            heartbeat_stop, heartbeat_thread = self._start_heartbeat(task.id)
            try:
                result = self.monitor.process_simc_task(task, already_claimed=True)
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=2)
            # 测试替身或旧实现若只返回成功，不应让队列永久停在 running。
            if result and task.current_status == 1:
                task.current_status = 2
                task.completed_at = timezone.now()
                task.save(update_fields=['current_status', 'completed_at', 'modified_time'])
        except Exception as exc:
            logger.exception('[SimC Worker] task %s failed', task.id)
            self._mark_unexpected_failure(task, exc)
        return True

    def run(self):
        while not self._stop.is_set():
            try:
                self.recover_stale_tasks()
                if not self.consume_once():
                    self._stop.wait(self.poll_interval)
            except Exception:
                logger.exception('[SimC Worker] recover/consume loop error')
                self._stop.wait(min(max(self.poll_interval, 1), 10))
