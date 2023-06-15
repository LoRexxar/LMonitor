#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: qaxScan.py.py
@time: 2023/6/15 16:25
@desc:

'''


from utils.log import logger

from botend.models import VulnData, VulnMonitorTask
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


class QaxScan(BaseScan):
    """
    qax 扫描
    """

    def __init__(self, req, task):
        super().__init__(req, task)

        self.task = task
        self.task_name = "qax"
        self.hint = ""

        # 从表获取任务
        self.vds = VulnData.objects.filter(source="qax", state=0)
        self.url = "https://ti.qianxin.com/alpha-api/v2/nox/api/web/portal/vuln/residence/temp/show"
        self.url2 = "https://ti.qianxin.com/alpha-api/v2/nox/api/web/portal/vuln_repo/show"

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
            logger.info("[qax Scan] qax Scan details {}.".format(vd.title))
            vd.state = 1
            vd.save()

            # split id
            id = vd.link.split('/')[-1]

            params = {
                "id": id,
            }

            headers = {
                "Origin": "https://ti.qianxin.com",
                "Referer": "https://ti.qianxin.com/vulnerability",
                "Content-Type": "application/json",
            }

            content = self.req.post(self.url, 'JsonResp', 0, params, "", headers)
            r = json.loads(content)

            for tab in r['data']['residence_latest']:
                if tab['key'] == 'description':
                    vd.description = tab['value']

                elif tab['key'] == 'fix_method':
                    vd.solutions = tab['value']

                elif tab['key'] == 'related_links':
                    vd.reference = tab['value']

            content = self.req.post(self.url2, 'JsonResp', 0, params, "", headers)
            r2 = json.loads(content)

            score = r2['data']['qvc_score']
            vd.score = score

            for tab in r2['data']['info']:
                if tab['label'] == '公开POC | EXP':
                    if not tab['value'] == 1:
                        vd.is_poc = 1

            vd.state = 2
            vd.save()

            time.sleep(random.randint(10, 30))

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = AibotkWechatWebhook()
        aw.publish_admin(self.hint)
