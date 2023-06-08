#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: aliyunAvdMonitor.py
@time: 2023/6/5 16:14
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


class AliyunAvdMonitor(BaseScan):
    """
    阿里云漏洞监控
    """

    def __init__(self, req, task):
        super().__init__(req, task)

        self.task = task
        self.task_name = "avd"
        self.hint = ""

        # 从表获取任务
        self.vmt = VulnMonitorTask.objects.filter(task_name=self.task_name, is_active=1)
        self.avd_url = "https://avd.aliyun.com/high-risk/list"

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        self.parse_vuln_list()

        return True

    def parse_vuln_list(self):

        if self.vmt:
            logger.info("[Aliyun Avd Monitor] Monitor aliyun avd start.")

            driver = self.req.get(self.avd_url, 'RespByChrome', 0, "", is_origin=1)

            try:
                wait = WebDriverWait(driver, 25)
                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "table")))

                tr_list = driver.find_elements(By.TAG_NAME, 'tr')

                for tr in tr_list:
                    tds = tr.find_elements(By.TAG_NAME, 'td')

                    if not tds:
                        continue

                    link = tds[0].find_elements(By.TAG_NAME, 'a')[0].get_attribute("href")
                    avid = tds[0].text
                    title = tds[1].text
                    type = tds[2].find_elements(By.TAG_NAME, 'button')[0].get_attribute("data-original-title")
                    publish_time = tds[3].text

                    status = tds[4].find_elements(By.TAG_NAME, 'button')
                    cve = status[0].get_attribute("data-original-title")
                    poc_status = status[1].get_attribute("data-original-title")

                    is_cve = 0
                    is_poc = 0
                    is_exp = 0
                    cveid = ""

                    if "无CVE" not in cve:
                        cveid = cve
                        is_cve = 1

                    if "POC 已公开" in poc_status:
                        is_poc = 1

                    elif "EXP 已公开" in poc_status:
                        is_exp = 1

                    # check exist
                    va = VulnData.objects.filter(sid=avid).first()
                    vc = False
                    if is_cve:
                        vc = VulnData.objects.filter(cveid=cveid)

                    if va or vc:
                        continue

                    logger.info("[Aliyun Avd Monitor] Found new Vuln {}".format(title))
                    vn = VulnData(sid=avid, cveid=cveid, title=title, type=type, publish_time=publish_time,
                                  link=link, source="avd",
                                  is_poc=is_poc, is_exp=is_exp, is_active=1, state=0)
                    vn.save()

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
