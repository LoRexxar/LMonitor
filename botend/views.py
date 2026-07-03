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

from botend.alerting import upsert_system_alert
from botend.models import MonitorTask, MonitorWebhook
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
        while 1:
            Lreq = None
            acquired_scan_lock = False
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
                        acquired_scan_lock = True
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

                    if now_task:
                        task_type = now_task.type
                        task_url = now_task.target
                        task_class = Monitor_Type_BaseObject_List[task_type]

                        Lreq = LReq(is_chrome=True)
                        try:
                            Lreq.set_current_task(now_task)
                        except Exception:
                            pass
                        t = task_class(Lreq, now_task)
                        try:
                            scan_result = t.scan(task_url)
                        except Exception as scan_exc:
                            logger.warning('[Scan] task error, {}'.format(traceback.format_exc()))
                            _record_monitor_task_alert(now_task, exc=scan_exc)
                        else:
                            if scan_result is False:
                                detail = getattr(t, 'last_error_detail', '') or 'scan returned False'
                                _record_monitor_task_alert(now_task, error_message=detail)
                        try:
                            Lreq.set_current_task(None)
                        except Exception:
                            pass

                        now_task.save()
                        time.sleep(10)
                finally:
                    if Lreq is not None:
                        try:
                            Lreq.close_driver()
                        except Exception:
                            pass
                    if acquired_scan_lock:
                        lock.acquire()
                        try:
                            is_Block = False
                        finally:
                            lock.release()

            except KeyboardInterrupt:
                logger.error("[Scan] Stop Scaning.")
                if Lreq is not None:
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
