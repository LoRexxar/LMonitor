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
        """回收无心跳的 running Task；历史 Run 保留，只重新排队有限次数。"""
        threshold = timezone.now() - timedelta(seconds=self.stale_seconds)
        recovered = 0
        tasks = SimcTask.objects.filter(
            is_active=True, current_status=1,
        ).filter(started_at__lt=threshold)
        for task in tasks:
            runs = SimulationRun.objects.filter(task=task).order_by('sequence')
            latest = runs.last()
            if latest is not None and latest.status == 'running':
                latest.status = 'failed'
                latest.error_detail = 'Worker 回收超时 running 任务，原执行已中断'
                latest.completed_at = timezone.now()
                latest.save(update_fields=['status', 'error_detail', 'completed_at'])
            attempts = runs.count()
            if attempts >= self.max_attempts:
                task.current_status = 3
                task.error_detail = f'Worker 重试次数上限（{self.max_attempts}）'
                task.completed_at = timezone.now()
                task.save(update_fields=['current_status', 'error_detail', 'completed_at', 'modified_time'])
                if task.batch_id:
                    self.monitor.sync_batch_lifecycle(task.batch_id)
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
            result = self.monitor.process_simc_task(task, already_claimed=True)
            # 测试替身或旧实现若只返回成功，不应让队列永久停在 running。
            if result and task.current_status == 1:
                task.current_status = 2
                task.completed_at = timezone.now()
                task.save(update_fields=['current_status', 'completed_at', 'modified_time'])
        except Exception as exc:
            logger.exception('[SimC Worker] task %s failed', task.id)
            self._mark_unexpected_failure(task, exc)
        finally:
            if task.batch_id:
                try:
                    self.monitor.sync_batch_lifecycle(task.batch_id)
                except Exception:
                    logger.exception('[SimC Worker] batch lifecycle sync failed')
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
