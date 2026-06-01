# -*- coding: utf-8 -*-

"""
    log
    ~~~

    Implements color logger

    :author:    LoRexxar <LoRexxar@gmail.com>
    :homepage:  https://github.com/wufeifei/cobra
    :license:   MIT, see LICENSE for more details.
    :copyright: Copyright (c) 2017 Feei. All rights reserved
"""
import os
import logging
import colorlog
import time
import datetime
import threading

# stream handle
#
# Copyright (C) 2010-2012 Vinay Sajip. All rights reserved. Licensed under the new BSD license.
#
logger = logging.getLogger('LSpider')
log_path = 'logs'


class DatabaseLogHandler(logging.Handler):
    """
    自定义日志处理器，将 ERROR/CRITICAL 级别的日志自动写入数据库。
    使用去重机制，避免相同错误短时间内重复写入。
    """

    _lock = threading.Lock()
    _recent_keys = {}
    _dedup_window = 300

    def __init__(self, level=logging.ERROR):
        super().__init__(level)
        self._enabled = True

    def emit(self, record):
        if not self._enabled:
            return
        try:
            self._do_emit(record)
        except Exception:
            pass

    def _do_emit(self, record):
        try:
            from django.db import connection
            if not hasattr(connection, 'ensure_connection'):
                return
            connection.ensure_connection()
        except Exception:
            return

        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()

        source = getattr(record, 'filename', '') or ''
        line_no = getattr(record, 'lineno', 0)
        thread_name = getattr(record, 'threadName', '')

        category = 'ERROR_LOG'
        subject = '{}:{} [{}]'.format(source, line_no, thread_name) if source else thread_name
        dedup_key = '{}@{}'.format(category, msg[:200])

        now = time.time()
        with self._lock:
            last_seen = self._recent_keys.get(dedup_key, 0)
            if now - last_seen < self._dedup_window:
                self._recent_keys[dedup_key] = now
                return
            self._recent_keys[dedup_key] = now
            if len(self._recent_keys) > 1000:
                cutoff = now - self._dedup_window
                self._recent_keys = {
                    k: v for k, v in self._recent_keys.items() if v > cutoff
                }

        try:
            from botend.alerting import upsert_system_alert
            upsert_system_alert(
                category=category,
                subject=subject[:128],
                level=3,
                title=msg[:200],
                content=msg,
            )
        except Exception:
            pass


def log(loglevel, log_name):
    handler = colorlog.StreamHandler()
    handler.setFormatter(
        colorlog.ColoredFormatter(
            fmt='%(log_color)s[%(levelname)s] [%(threadName)s] [%(asctime)s] [%(filename)s:%(lineno)d] %(message)s',
            datefmt="%H:%M:%S",
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'green',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'red,bg_white',
            },
        )
    )
    f = open(log_name, 'a+')
    handler2 = logging.StreamHandler(f)
    formatter = logging.Formatter(
        "[%(levelname)s] [%(threadName)s] [%(asctime)s] [%(filename)s:%(lineno)d] %(message)s")
    handler2.setFormatter(formatter)
    logger.addHandler(handler2)
    logger.addHandler(handler)

    db_handler = DatabaseLogHandler(level=logging.ERROR)
    db_handler.setFormatter(formatter)
    logger.addHandler(db_handler)

    logger.setLevel(loglevel)


if os.path.isdir(log_path) is not True:
    os.mkdir(log_path, 0o755)

day_time = int(time.mktime(datetime.date.today().timetuple()))
logfile = os.path.join(log_path, str(day_time)+'.log')

# log
log(logging.INFO, logfile)
