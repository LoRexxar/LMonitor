#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: LMonitorCoreBackend.py
@time: 2020/6/11 15:14
@desc:
'''


from django.core.management.base import BaseCommand
from django.conf import settings
from botend.backend_lifecycle import BackendLifecycle
from botend.views import LMonitorCoreBackend

from utils.log import logger

import signal
import traceback


class Command(BaseCommand):
    help = 'Monitor backend'

    def handle(self, *args, **options):
        lifecycle = BackendLifecycle(
            max_runtime=getattr(settings, 'MONITOR_TASK_MAX_RUNTIME_SECONDS', 172800),
            cleanup_seconds=getattr(settings, 'MONITOR_RETIRE_CLEANUP_SECONDS', 60),
        )
        previous = lifecycle.install_signal_handlers()
        try:
            LMonitorCoreBackend(lifecycle=lifecycle)
        except Exception:
            logger.error("[Bot] something error, {}".format(traceback.format_exc()))
            raise
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
