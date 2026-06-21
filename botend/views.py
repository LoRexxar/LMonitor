#!/usr/bin/env python
# encoding: utf-8


import os
import time
import traceback
import threading
from django.db.utils import OperationalError
from django.db import close_old_connections
from django.utils import timezone
from django.conf import settings as django_settings
from utils.LReq import LReq
from utils.log import logger
from core.threadingpool import ThreadPool

from botend.models import MonitorTask, MonitorTaskLog, MonitorWebhook
from botend.monitor_env import filter_runnable_tasks
from botend.plugin_sync import sync_monitortasks_from_plugin_list
from LMonitor.config import Monitor_Type_BaseObject_List

THREAD_LIMIT_NUM = int(getattr(django_settings, 'THREAD_LIMIT_NUM', 10))

is_Block = False
lock = threading.Lock()


def _truncate_text(value, limit=20000):
    text = str(value or '')
    if len(text) <= limit:
        return text
    return text[:limit] + '\n...(truncated)'


def _create_monitor_task_log(task):
    try:
        return MonitorTaskLog.objects.create(
            task=task,
            task_name=task.name,
            task_type=task.type,
            target=task.target or '',
            status=MonitorTaskLog.STATUS_STARTED,
            started_at=timezone.now(),
            task_flag=task.flag,
            extra={'env_limit': task.env_limit, 'proxy_enabled': task.proxy_enabled},
        )
    except Exception:
        logger.warning('[Scan] failed to create monitor task log, {}'.format(traceback.format_exc()))
        return None


def _finish_monitor_task_log(log_row, task, status, exc=None, error_message=''):
    if not log_row:
        return
    try:
        finished_at = timezone.now()
        log_row.status = status
        log_row.finished_at = finished_at
        log_row.duration_ms = int((finished_at - log_row.started_at).total_seconds() * 1000)
        log_row.task_flag = getattr(task, 'flag', None)
        if exc is not None:
            log_row.error_type = exc.__class__.__name__[:200]
            log_row.error_message = _truncate_text(str(exc), 20000)
            log_row.traceback = _truncate_text(traceback.format_exc(), 20000)
        elif error_message:
            log_row.error_type = 'MonitorFailed'
            log_row.error_message = _truncate_text(error_message, 20000)
        log_row.save(update_fields=[
            'status', 'finished_at', 'duration_ms', 'task_flag',
            'error_type', 'error_message', 'traceback',
        ])
    except Exception:
        logger.warning('[Scan] failed to finish monitor task log, {}'.format(traceback.format_exc()))


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

    def scan(self):
        os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')
        req_cfg = getattr(django_settings, 'REQUEST_CONFIG', {}) or {}
        # 默认启用 cloak（需要时会自动 fallback 到 playwright 官方 chromium）
        disable_cloak = str(req_cfg.get('disable_cloak', '')).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
        if not disable_cloak:
            disable_cloak = str(os.getenv('LMONITOR_DISABLE_CLOAK', '')).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}

        Lreq = LReq(is_chrome=True, is_cloak=(not disable_cloak))
        recycle_every = int(req_cfg.get('chrome_recycle_every', 0) or 0)
        finished = 0

        while 1:
            try:
                close_old_connections()
                global is_Block
                now_task = False
                need_wait = False

                lock.acquire()
                try:
                    if is_Block:
                        need_wait = True
                    else:
                        is_Block = True
                finally:
                    lock.release()

                if need_wait:
                    time.sleep(20)
                    continue

                try:
                    tasks = filter_runnable_tasks(MonitorTask.objects.filter(is_active=1)).order_by('last_scan_time')

                    for task in tasks:
                        if (timezone.now() - task.last_scan_time).total_seconds() < task.wait_time:
                            continue

                        logger.info("[Main] New Task {} start...".format(task.name))
                        now_task = task

                        task.last_scan_time = timezone.now()
                        task.save()
                        break
                finally:
                    lock.acquire()
                    try:
                        is_Block = False
                    finally:
                        lock.release()

                if now_task:
                    task_type = now_task.type
                    task_url = now_task.target
                    task_class = Monitor_Type_BaseObject_List[task_type]

                    try:
                        Lreq.set_current_task(now_task)
                    except Exception:
                        pass
                    t = task_class(Lreq, now_task)
                    task_log = _create_monitor_task_log(now_task)
                    try:
                        scan_result = t.scan(task_url)
                    except Exception as scan_exc:
                        logger.warning('[Scan] task error, {}'.format(traceback.format_exc()))
                        _finish_monitor_task_log(task_log, now_task, MonitorTaskLog.STATUS_FAILED, scan_exc)
                    else:
                        if scan_result is False:
                            _finish_monitor_task_log(
                                task_log,
                                now_task,
                                MonitorTaskLog.STATUS_FAILED,
                                error_message='scan returned False',
                            )
                        else:
                            _finish_monitor_task_log(task_log, now_task, MonitorTaskLog.STATUS_SUCCESS)
                    try:
                        Lreq.set_current_task(None)
                    except Exception:
                        pass

                    now_task.save()
                    finished += 1
                    if recycle_every and Lreq.is_chrome and (finished % recycle_every == 0):
                        Lreq.reset_chrome()
                    time.sleep(10)

            except KeyboardInterrupt:
                logger.error("[Scan] Stop Scaning.")
                Lreq.close_driver()
                exit(0)

            except OperationalError:
                logger.error("[Scan] mysql link timeout. wait start.")
                time.sleep(600)
                continue

            except:
                logger.warning('[Scan] something error, {}'.format(traceback.format_exc()))
                time.sleep(5)
                continue
