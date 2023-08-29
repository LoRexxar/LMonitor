#!/usr/bin/env python
# encoding: utf-8


import time
import pytz
import datetime
import traceback

from utils.LReq import LReq
from utils.log import logger
from core.threadingpool import ThreadPool

from botend.models import MonitorTask, MonitorWebhook
from LMonitor.config import Monitor_Type_BaseObject_List
from LMonitor.settings import THREAD_LIMIT_NUM


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
                    logger.debug("[LMonitor Core] New Thread {} for LMonitor Core.".format(i))

                    self.threadpool.new(botcore.scan)
                    time.sleep(3)

            # self.threadpool.wait_all_thread()
            time.sleep(3)


class LMonitorCore:
    """
    bot 主线程
    """

    def scan(self):

        try:
            Lreq = LReq(is_chrome=True)
            while 1:
                # sleep
                time.sleep(20)

                tasks = MonitorTask.objects.filter(is_active=1).order_by('-last_scan_time')
                local_tz = pytz.timezone('Asia/Shanghai')

                for task in tasks:
                    # 扫描每10分钟只会扫一次
                    if (datetime.datetime.now(local_tz) - task.last_scan_time).total_seconds() < task.wait_time:
                        continue

                    # 更新扫描时间
                    task.last_scan_time = datetime.datetime.now(local_tz)
                    task.save()

                    task_type = task.type
                    task_url = task.target
                    task_class = Monitor_Type_BaseObject_List[task_type]

                    t = task_class(Lreq, task)
                    t.scan(task_url)

                    task.save()
                    time.sleep(3)

        except KeyboardInterrupt:
            logger.error("[Scan] Stop Scaning.")
            Lreq.close_driver()
            exit(0)

        except:
            logger.warning('[Scan] something error, {}'.format(traceback.format_exc()))
            raise