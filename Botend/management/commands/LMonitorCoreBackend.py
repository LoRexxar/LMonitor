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
from Botend.views import LMonitorCoreBackend

from utils.log import logger

import sys
import traceback
from queue import Queue, Empty


class Command(BaseCommand):
    help = 'Monitor backend'

    def handle(self, *args, **options):

        try:
            LMonitorCoreBackend()

        except KeyboardInterrupt:
            logger.error("[Scan] Stop Scaning.")
            exit(0)

        except:
            logger.error("[Bot] something error, {}".format(traceback.format_exc()))
