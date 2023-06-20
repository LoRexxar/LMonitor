#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: qaxMonitor.py
@time: 2023/6/15 15:59
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


class QaxMonitor(BaseScan):
    """
    qax监控
    """

    def __init__(self, req, task):
        super().__init__(req, task)

        self.task = task
        self.task_name = "qax"
        self.hint = ""

        # 从表获取任务
        self.vmt = VulnMonitorTask.objects.filter(task_name=self.task_name, is_active=1).first()
        self.url = "https://ti.qianxin.com/alpha-api/v2/vuln/vuln-list"

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        self.parse_qax_list()

        return True

    def parse_qax_list(self):

        if self.vmt:
            logger.info("[qax Monitor] Monitor qax vuln start.")

            local_tz = pytz.timezone('Asia/Shanghai')
            self.vmt.last_spider_time = datetime.datetime.now(local_tz)
            self.vmt.save()

            params = {
                "page_no": 1,
                "page_size": 20,
                "rating_flag": "true"
            }

            headers = {
                "Origin": "https://ti.qianxin.com",
                "Referer": "https://ti.qianxin.com/vulnerability",
                "Content-Type": "application/json",
            }

            url = self.url
            content = self.req.post(url, 'JsonResp', 0, params, "", headers)
            r = json.loads(content)

            if "data" not in r['data']:
                logger.warning("[qax Monitor] error: {}".format(r['data']))
                return

            for msg in r['data']['data']:
                sid = msg['qvd_id']
                create_time = msg['publish_date']
                url = "https://ti.qianxin.com/vulnerability/detail/{}".format(msg['id'])
                title = msg['vuln_name_cn']
                cve_id = msg['cve_id']

                # check level
                level = msg['rating_level']
                if level == "极危":
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

                if not type:
                    type = ",".join(msg['threat_category_cn'])

                # # chek tag
                # tag_list = msg['tag']
                # tag_name_list = [d['name'] for d in tag_list]
                # tag = ','.join(tag_name_list)

                # check poc exp
                is_poc = msg['public_poc']
                is_exp = msg['public_exp']

                # check exist
                va = VulnData.objects.filter(sid=sid).first()
                vc = False
                if cve_id:
                    vc = VulnData.objects.filter(cveid=cve_id)

                if va or vc:
                    continue

                logger.info("[qax Monitor] Found new Vuln {}".format(title))
                vn = VulnData(sid=sid, cveid=cve_id, title=title, type=type, publish_time=create_time,
                              link=url, source="qax", score=score, severity=severity,
                              is_poc=is_poc, is_exp=is_exp, is_active=1, state=0)
                vn.save()

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = AibotkWechatWebhook()
        aw.publish_admin(self.hint)
