#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: aliyunAvdScan.py.py
@time: 2023/6/6 17:20
@desc:

'''

from utils.log import logger

from botend.models import VulnMonitorTask, VulnData

from botend.controller.BaseScan import BaseScan
from botend.webhook.aibotkWechat import AibotkWechatWebhook

import json
import time
import pytz
import random
import datetime
import urllib.parse
from urllib.parse import urlparse, parse_qs
from DrissionPage.common import By


class AliyunAvdScan(BaseScan):
    """
    阿里云漏洞扫描
    """

    def __init__(self, req, task):
        super().__init__(req, task)

        self.task = task
        self.task_name = "avd"
        self.hint = ""

        # 从表获取任务
        self.vds = VulnData.objects.filter(source="avd", state=0)

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        self.parse_vuln_list()

        return True

    def parse_vuln_list(self):

        for vd in self.vds:
            logger.info("[Aliyun Avd Scan] avd Scan details {}.".format(vd.title))
            vd.state = 1
            vd.save()

            driver = self.req.get(vd.link, 'RespByChrome', 0, "", is_origin=1)

            try:
                detail_driver = driver.eles('.:text-detail')

                details = detail_driver[0].text
                solutions = ""
                if len(detail_driver) > 1:
                    solutions = detail_driver[1].text
                reference = driver.eles('.:reference')[0].text
                score = driver.eles('.:cvss-breakdown__score')[0].text

                if float(score) > 9:
                    severity = 4
                elif float(score) >= 7:
                    severity = 3
                elif float(score) >= 4:
                    severity = 2
                else:
                    severity = 1

                vd.score = score
                vd.severity = severity
                vd.description = details
                vd.solutions = solutions
                vd.reference = reference
                vd.state = 2
                vd.save()

                time.sleep(random.randint(10, 20))

            except AttributeError:
                logger.error("[Aliyun Avd Scan] bad request.")

            except:
                raise

            time.sleep(random.randint(10, 20))

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = AibotkWechatWebhook()
        aw.publish_admin(self.hint)
