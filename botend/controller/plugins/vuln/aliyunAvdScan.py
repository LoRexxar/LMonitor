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
import selenium
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


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
                wait = WebDriverWait(driver, 25)
                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "text-detail")))

                detail_driver = driver.find_elements(By.CLASS_NAME, 'text-detail')

                details = detail_driver[0].text
                solutions = ""
                if len(detail_driver) > 1:
                    solutions = detail_driver[1].text
                reference = driver.find_elements(By.CLASS_NAME, 'reference')[0].text
                score = driver.find_elements(By.CLASS_NAME, 'cvss-breakdown__score')[0].text

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

                time.sleep(random.randint(10, 30))

            except selenium.common.exceptions.NoSuchElementException:
                logger.warning("[Aliyun Avd Monitor] Aliyun Avd Monitor can't get target element.")
                return

            except selenium.common.exceptions.TimeoutException:
                logger.warning("[Aliyun Avd Monitor] Aliyun Avd Monitor Scan timeout.")
                return

            except:
                raise

            time.sleep(random.randint(10, 30))

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = AibotkWechatWebhook()
        aw.publish_admin(self.hint)
