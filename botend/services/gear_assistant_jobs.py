"""Persistent, owner-scoped jobs for the online gear assistant."""
import logging
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.db.models import Subquery
from django.utils import timezone

from botend.backend_lifecycle import worker_cycles
from botend.models import GearAssistantJob
from botend.services.gear_assistant import optimization_inputs, optimize_loadouts
from botend.services.gear_builder import GearBuilderError

logger = logging.getLogger(__name__)
TIMEOUT = 240
QUEUE_LIFETIME = timedelta(minutes=10)
RUN_LIFETIME = timedelta(seconds=TIMEOUT + 30)
RETENTION = timedelta(days=1)
STATUS_FIELDS = ('request_id', 'state', 'class_name', 'spec_name', 'error')


class ActiveGearJob(GearBuilderError):
    pass


def latest_job(owner, *, request_id=None):
    owned = GearAssistantJob.objects.filter(owner=owner)
    if request_id is None:
        job = owned.only('request_id', 'state', 'updated_at').order_by('-pk').first()
        return job if job and job.updated_at >= timezone.now() - RETENTION else None
    # Match only the newest row, not the newest row that matches the supplied UUID.
    newest_pk = owned.order_by('-pk').values('pk')[:1]
    rows = owned.filter(pk=Subquery(newest_pk), request_id=request_id,
                        updated_at__gte=timezone.now() - RETENTION).only(*STATUS_FIELDS, 'result').order_by()
    return next(iter(rows[:1]), None)


def enqueue(owner, request_id, payload):
    jobs = GearAssistantJob.objects.filter(owner=owner)
    existing = jobs.only(*STATUS_FIELDS).filter(request_id=request_id).first()
    if existing:
        return existing
    class_name, spec_name, target, allow_last_two, preference = optimization_inputs(payload)
    payload = {**payload, 'class_name': class_name, 'spec_name': spec_name, 'target': target,
               'allow_mythic_last_two': allow_last_two, 'source_preference': preference}
    try:
        with transaction.atomic():
            return jobs.create(owner=owner, request_id=request_id, class_name=class_name,
                               spec_name=spec_name, payload=payload)
    except IntegrityError:
        existing = jobs.only(*STATUS_FIELDS).filter(request_id=request_id).first()
        if existing:
            return existing
        if jobs.filter(active_slot=1).exists():
            raise ActiveGearJob('已有辅助配装任务正在执行，请等待该任务结束后再提交。') from None
        raise


def _finish(running, *, result=None, error=''):
    return running.update(state='failed' if error else 'done', worker_slot=None, active_slot=None,
                          updated_at=timezone.now(), payload={}, result=result or {}, error=error)


def run_job(pk):
    jobs = GearAssistantJob.objects.filter(pk=pk, active_slot=1)
    try:
        with transaction.atomic():
            claimed = jobs.filter(state='queued', created_at__gte=timezone.now() - QUEUE_LIFETIME).update(
                state='running', worker_slot=1, updated_at=timezone.now())
    except IntegrityError:
        if GearAssistantJob.objects.filter(worker_slot=1).exists():
            return False
        raise
    if not claimed:
        return False
    running = jobs.filter(state='running', worker_slot=1)
    job = running.select_related('owner').only('payload', 'owner').first()
    if not job:
        return False
    try:
        result = optimize_loadouts(job.owner, job.payload)
    except GearBuilderError as exc:
        return bool(_finish(running, error=str(exc)[:300] or '无法生成辅助配装方案。'))
    except Exception as exc:
        logger.warning('辅助配装执行失败 job=%s error=%s', pk, type(exc).__name__)
        return bool(_finish(running, error='辅助配装暂时失败，请稍后重试。'))
    return bool(_finish(running, result=result))


def _maintain(now):
    jobs = GearAssistantJob.objects
    _finish(jobs.filter(state='running', worker_slot=1, updated_at__lt=now - RUN_LIFETIME),
            error='后台任务已中断，请重新生成。')
    _finish(jobs.filter(state='queued', created_at__lt=now - QUEUE_LIFETIME),
            error='排队时间过长，请重新生成。')
    expired = jobs.filter(active_slot__isnull=True, updated_at__lt=now - RETENTION)
    expired.filter(pk__in=list(expired.order_by('updated_at').values_list('pk', flat=True)[:50])).delete()


def work_once(*, maintenance=False):
    now = timezone.now()
    if maintenance:
        _maintain(now)
    jobs = GearAssistantJob.objects
    if jobs.filter(worker_slot=1).exists():
        return
    pk = jobs.filter(state='queued', created_at__gte=now - QUEUE_LIFETIME).order_by(
        'created_at', 'pk').values_list('pk', flat=True).first()
    if not pk:
        return
    try:
        process = subprocess.run([sys.executable, str(Path(settings.BASE_DIR) / 'manage.py'),
                                  'run_gear_assistant_job', str(pk)], cwd=settings.BASE_DIR,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        # The child has been killed and reaped. Do not clear another worker's claim.
        logger.warning('辅助配装进程超时 job=%s', pk)
    except OSError as exc:
        logger.warning('辅助配装进程无法启动 job=%s error=%s', pk, type(exc).__name__)
        _finish(jobs.filter(pk=pk, state='queued'), error='无法启动辅助配装任务，请稍后重试。')
    else:
        if process.returncode:
            logger.warning('辅助配装进程异常退出 job=%s status=%s', pk, process.returncode)
    # The running claim expires independently if a process died outside its normal exit.


def start_worker(*, lifecycle):
    def loop():
        next_maintenance = None
        try:
            for _ in worker_cycles(lifecycle, interval=2, label='gear-assistant-jobs'):
                close_old_connections()
                try:
                    now = timezone.now()
                    maintain = next_maintenance is None or now >= next_maintenance
                    if maintain:
                        next_maintenance = now + timedelta(minutes=1)
                    work_once(maintenance=maintain)
                except Exception as exc:
                    logger.warning('辅助配装队列检查失败 error=%s', type(exc).__name__)
        finally:
            connections.close_all()

    worker = threading.Thread(target=loop, name='gear-assistant-jobs', daemon=True)
    worker.start()
    return worker
