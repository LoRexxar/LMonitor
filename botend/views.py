#!/usr/bin/env python
# encoding: utf-8


import time
import pytz
import datetime
import traceback
import threading
from django.db.utils import OperationalError
from utils.LReq import LReq
from utils.log import logger
from core.threadingpool import ThreadPool

from botend.models import MonitorTask, MonitorWebhook
from LMonitor.config import Monitor_Type_BaseObject_List
from LMonitor.settings import THREAD_LIMIT_NUM

is_Block = False
lock = threading.Lock()


class LMonitorCoreBackend:
    """
    monitor 守护线程
    """
    def __init__(self):
        # 任务与线程分发
        self.threadpool = ThreadPool()

        MonitorTasks = MonitorTask.objects.filter(is_active=1).count()
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
        Lreq = LReq(is_chrome=True)

        while 1:
            try:
                global is_Block

                lock.acquire()
                if is_Block:
                    time.sleep(20)
                    lock.release()
                    continue

                is_Block = True

                tasks = MonitorTask.objects.filter(is_active=1).order_by('-last_scan_time')
                local_tz = pytz.timezone('Asia/Shanghai')

                now_task = False

                for task in tasks:
                    # 扫描每10分钟只会扫一次
                    if (datetime.datetime.now(local_tz) - task.last_scan_time).total_seconds() < task.wait_time:
                        continue

                    logger.info("[Main] New Task {} start...".format(task.name))
                    now_task = task

                    # 更新扫描时间
                    task.last_scan_time = datetime.datetime.now(local_tz)
                    task.save()
                    break

                is_Block = False
                lock.release()

                if now_task:
                    task_type = now_task.type
                    task_url = now_task.target
                    task_class = Monitor_Type_BaseObject_List[task_type]

                    t = task_class(Lreq, now_task)
                    t.scan(task_url)

                    now_task.save()
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
                raise