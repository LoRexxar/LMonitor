#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: seebugMonitor.py
@time: 2023/6/15 18:32
@desc:

'''


from utils.log import logger

from botend.models import VulnMonitorTask, VulnData
from botend.controller.plugins.vuln import Vul_List, Vul_link_Type_Dict

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


class SeebugMonitor(BaseScan):
    """
    阿里云漏洞监控
    """

    def __init__(self, req, task):
        super().__init__(req, task)

        self.task = task
        self.task_name = "seebug"
        self.hint = ""

        # 从表获取任务
        self.vmt = VulnMonitorTask.objects.filter(task_name=self.task_name, is_active=1)
        self.avd_url = "https://www.seebug.org/vuldb/vulnerabilities"

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
            logger.info("[seebug Monitor] Monitor seebug start.")

            driver = self.req.get(self.avd_url, 'RespByChrome', 0, "", is_origin=1)

            try:
                wait = WebDriverWait(driver, 25)
                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "table")))

                tr_list = driver.find_elements(By.TAG_NAME, 'tr')

                for tr in tr_list:
                    tds = tr.find_elements(By.TAG_NAME, 'td')

                    if not tds:
                        continue

                    sid = tds[0].text
                    publish_time = tds[1].text
                    level = tds[2].find_elements(By.TAG_NAME, 'div')[0].get_attribute("data-original-title")

                    link_tag = tds[3].find_elements(By.TAG_NAME, 'a')[0]
                    title = link_tag.text
                    link = link_tag.get_attribute("href")

                    tag_list = tds[4].find_elements(By.TAG_NAME, 'i')

                    if level == "严重":
                        severity = 4
                        score = 10
                    elif level == "高危":
                        severity = 3
                        score = 8
                    elif level == "中危":
                        severity = 2
                        score = 5
                    elif level == "低危":
                        severity = 1
                        score = 3
                    else:
                        severity = 0
                        score = 1

                    # check type
                    type = ""
                    for vtype in Vul_List:
                        if vtype in title:
                            type = vtype
                            break

                    is_cve = 0
                    is_poc = 0
                    is_exp = 0
                    cveid = ""

                    if "无 CVE" not in tag_list[0].get_attribute("data-original-title"):
                        cveid = tag_list[0].get_attribute("data-original-title")
                        is_cve = 1

                    if "有 PoC" in tag_list[1].get_attribute("data-original-title"):
                        is_poc = 1

                    elif "有 ExP" in tag_list[1].get_attribute("data-original-title"):
                        is_exp = 1

                    # check exist
                    va = VulnData.objects.filter(sid=sid).first()
                    vc = False
                    if is_cve:
                        vc = VulnData.objects.filter(cveid=cveid)

                    if va or vc:
                        continue

                    logger.info("[seebug Monitor] Found new Vuln {}".format(title))
                    vn = VulnData(sid=sid, cveid=cveid, title=title, type=type, publish_time=publish_time,
                                  link=link, source="seebug", score=score, severity=severity,
                                  is_poc=is_poc, is_exp=is_exp, is_active=1, state=0)
                    vn.save()

            except selenium.common.exceptions.NoSuchElementException:
                logger.warning("[seebug Monitor] seebug Monitor can't get target element.")
                return

            except selenium.common.exceptions.TimeoutException:
                logger.warning("[seebug Monitor] seebug Monitor Scan timeout.")
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