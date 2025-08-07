#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: GewechatInit.py
@time: 2020/6/11 15:14
@desc:
'''


from django.core.management.base import BaseCommand
from botend.interface.gewechat import GeWechatInterface

from utils.log import logger

import sys
import traceback
from queue import Queue, Empty


class Command(BaseCommand):
    help = 'GeWechatInterface init'

    def handle(self, *args, **options):

        try:
            gw = GeWechatInterface()
            gw.init()

        except KeyboardInterrupt:
            logger.error("[gewechat] Stop init.")
            exit(0)

        except:
            logger.error("[gewechat] something error, {}".format(traceback.format_exc()))
