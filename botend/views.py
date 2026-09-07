#!/usr/bin/env python
# encoding: utf-8


import os
import threading
import time
import traceback
from uuid import uuid4
from django.core.exceptions import ImproperlyConfigured
from django.db.utils import OperationalError
from django.db import close_old_connections, connections
from django.conf import settings as django_settings
from utils.LReq import LReq
from utils.log import logger
from core.threadingpool import ThreadPool

from botend.alerting import upsert_system_alert
from botend.models import MonitorTask, MonitorWebhook
from botend.monitor_env import filter_runnable_tasks
from botend.plugin_sync import (
    MONITOR_TASK_LEASE_SECONDS,
    claim_next_monitor_task,
    complete_monitor_task_lease,
    release_monitor_task_lease,
    renew_monitor_task_lease,
    sync_monitortasks_from_plugin_list,
)
from LMonitor.config import Monitor_Type_BaseObject_List

THREAD_LIMIT_NUM = int(getattr(django_settings, 'THREAD_LIMIT_NUM', 10))
MONITOR_TASK_LEASE_HEARTBEAT_SECONDS = int(
    getattr(django_settings, 'MONITOR_TASK_LEASE_HEARTBEAT_SECONDS', 60)
)
if not 0 < MONITOR_TASK_LEASE_HEARTBEAT_SECONDS < MONITOR_TASK_LEASE_SECONDS:
    raise ImproperlyConfigured(
        'monitor task lease settings require '
        'MONITOR_TASK_LEASE_SECONDS > MONITOR_TASK_LEASE_HEARTBEAT_SECONDS > 0'
    )


class _MonitorTaskLeaseHeartbeat:
    """Renew one execution lease without sharing the scan thread's DB connection."""

    def __init__(self, task_id, lease_owner):
        self.task_id = int(task_id)
        self.lease_owner = str(lease_owner)
        self._stop_event = threading.Event()
        self.lease_lost = threading.Event()
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name='monitor-lease-{}'.format(self.task_id),
            daemon=True,
        )

    def start(self):
        self._thread.start()
        self._started = True

    def stop(self):
        self._stop_event.set()
        if self._started:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self.lease_lost.set()
                logger.error(
                    '[MonitorTask Lease] heartbeat thread did not stop task_id={}'.format(
                        self.task_id,
                    )
                )

    def _run(self):
        close_old_connections()
        last_successful_renewal = time.monotonic()
        try:
            while not self._stop_event.wait(MONITOR_TASK_LEASE_HEARTBEAT_SECONDS):
                try:
                    renewed = renew_monitor_task_lease(
                        self.task_id,
                        self.lease_owner,
                    )
                except Exception:
                    logger.warning(
                        '[MonitorTask Lease] heartbeat error task_id={}, {}'.format(
                            self.task_id,
                            traceback.format_exc(),
                        )
                    )
                    close_old_connections()
                    if time.monotonic() - last_successful_renewal >= MONITOR_TASK_LEASE_SECONDS:
                        self.lease_lost.set()
                        logger.error(
                            '[MonitorTask Lease] heartbeat unavailable past TTL '
                            'task_id={}, owner={}'.format(self.task_id, self.lease_owner)
                        )
                        return
                    continue

                if not renewed:
                    self.lease_lost.set()
                    logger.error(
                        '[MonitorTask Lease] ownership lost task_id={}, owner={}'.format(
                            self.task_id,
                            self.lease_owner,
                        )
                    )
                    return
                last_successful_renewal = time.monotonic()
        finally:
            connections.close_all()


def _truncate_text(value, limit=20000):
    text = str(value or '')
    if len(text) <= limit:
        return text
    return text[:limit] + '\n...(truncated)'


def _record_monitor_task_alert(task, exc=None, error_message=''):
    try:
        if exc is not None:
            error_type = exc.__class__.__name__
            message = str(exc)
            detail = traceback.format_exc()
        else:
            error_type = 'MonitorFailed'
            message = error_message or 'scan returned False'
            detail = ''

        content = '\n'.join([
            'task_name: {}'.format(getattr(task, 'name', '')),
            'task_type: {}'.format(getattr(task, 'type', '')),
            'target: {}'.format(getattr(task, 'target', '') or ''),
            'flag: {}'.format(getattr(task, 'flag', '') or ''),
            'error_type: {}'.format(error_type),
            'error_message: {}'.format(message),
            'traceback:',
            detail,
        ])
        upsert_system_alert(
            category='MONITOR_TASK_FAILED',
            subject=getattr(task, 'name', '') or str(getattr(task, 'id', '')),
            level=3,
            title='Monitor 执行失败：{}'.format(getattr(task, 'name', '')),
            content=_truncate_text(content, 20000),
        )
    except Exception:
        logger.warning('[Scan] failed to record monitor task alert, {}'.format(traceback.format_exc()))


def _release_monitor_task_lease_safely(task_id, lease_owner, *, log_rejection=True):
    try:
        released = release_monitor_task_lease(task_id, lease_owner)
        if not released and log_rejection:
            logger.error(
                '[MonitorTask Lease] release rejected task_id={}, owner={}'.format(
                    task_id,
                    lease_owner,
                )
            )
        return released
    except Exception:
        logger.warning(
            '[MonitorTask Lease] release error task_id={}, {}'.format(
                task_id,
                traceback.format_exc(),
            )
        )
        close_old_connections()
        return False


class LMonitorCoreBackend:
    """
    monitor 守护线程
    """
    def __init__(self):
        if getattr(django_settings, 'MONITOR_TASK_AUTO_SYNC_PLUGINS', True):
            try:
                sync_monitortasks_from_plugin_list(
                    Monitor_Type_BaseObject_List,
                    default_is_active=False,
                    default_target="",
                    skip_indexes={0},
                )
            except Exception:
                logger.warning('[MonitorTask Sync] error, {}'.format(traceback.format_exc()))

        # 任务与线程分发
        self.threadpool = ThreadPool()

        MonitorTasks = filter_runnable_tasks(MonitorTask.objects.filter(is_active=1)).count()
        left_tasks = MonitorTasks

        logger.info("[LMonitor Main] Monitor Backend Start...now {} targets in monitor.".format(left_tasks))

        # 获取线程池然后分发信息对象
        # 当有空闲线程时才继续
        i = 0

        while 1:
            while self.threadpool.get_free_num():

                if i > THREAD_LIMIT_NUM:
                    logger.warning("[LMonitor Core] More than {} thread init. stop new Thread.".format(THREAD_LIMIT_NUM))
                    self.threadpool.wait_all_thread()
                    break

                else:
                    i += 1
                    botcore = LMonitorCore()
                    logger.info("[LMonitor Core] New Thread {} for LMonitor Core.".format(i))

                    self.threadpool.new(botcore.scan)
                    time.sleep(30)

            # self.threadpool.wait_all_thread()
            time.sleep(10)


class LMonitorCore:
    """
    bot 主线程
    """

    def _execute_claimed_task(self, now_task, lease_owner):
        Lreq = None
        task_runner_ready = False
        original_flag = now_task.flag
        heartbeat = _MonitorTaskLeaseHeartbeat(now_task.id, lease_owner)
        try:
            heartbeat.start()
            logger.info("[Main] New Task {} start...".format(now_task.name))
            task_type = now_task.type
            task_url = now_task.target
            task_class = Monitor_Type_BaseObject_List[task_type]

            Lreq = LReq(is_chrome=True)
            try:
                Lreq.set_current_task(now_task)
            except Exception:
                pass
            task_runner = task_class(Lreq, now_task)
            task_runner_ready = True
            try:
                scan_result = task_runner.scan(task_url)
            except Exception as scan_exc:
                logger.warning('[Scan] task error, {}'.format(traceback.format_exc()))
                _record_monitor_task_alert(now_task, exc=scan_exc)
            else:
                if scan_result is False:
                    detail = getattr(task_runner, 'last_error_detail', '') or 'scan returned False'
                    _record_monitor_task_alert(now_task, error_message=detail)
        finally:
            if Lreq is not None:
                try:
                    Lreq.set_current_task(None)
                except Exception:
                    pass
                try:
                    Lreq.close_driver()
                except Exception:
                    pass
            heartbeat.stop()

            if not task_runner_ready:
                _release_monitor_task_lease_safely(now_task.id, lease_owner)
            else:
                task_updates = {}
                if now_task.flag != original_flag:
                    task_updates['flag'] = now_task.flag
                try:
                    completed = complete_monitor_task_lease(
                        now_task.id,
                        lease_owner,
                        task_updates=task_updates,
                    )
                except Exception:
                    logger.warning(
                        '[MonitorTask Lease] completion error task_id={}, {}'.format(
                            now_task.id,
                            traceback.format_exc(),
                        )
                    )
                    _release_monitor_task_lease_safely(now_task.id, lease_owner)
                    raise
                if not completed:
                    logger.error(
                        '[MonitorTask Lease] completion rejected after ownership loss '
                        'task_id={}, owner={}'.format(now_task.id, lease_owner)
                    )
                    _release_monitor_task_lease_safely(
                        now_task.id,
                        lease_owner,
                        log_rejection=False,
                    )

    def scan(self):
        os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')
        while 1:
            try:
                close_old_connections()
                lease_owner = uuid4().hex
                now_task = claim_next_monitor_task(lease_owner=lease_owner)
                if not now_task:
                    time.sleep(10)
                    continue

                self._execute_claimed_task(now_task, lease_owner)
                time.sleep(10)

            except KeyboardInterrupt:
                logger.error("[Scan] Stop Scaning.")
                exit(0)

            except OperationalError:
                logger.error("[Scan] mysql link timeout. wait start.")
                time.sleep(600)
                continue

            except:
                logger.warning('[Scan] something error, {}'.format(traceback.format_exc()))
                time.sleep(5)
                continue
