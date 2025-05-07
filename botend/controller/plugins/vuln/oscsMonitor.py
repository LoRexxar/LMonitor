#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: oscsMonitor.py
@time: 2023/6/13 17:16
@desc:

'''


from utils.log import logger

from botend.models import VulnData, VulnMonitorTask
from botend.controller.plugins.vuln import Vul_List

from botend.controller.BaseScan import BaseScan
from botend.interface.xxxbot import xxxbotInterface

import json
import pytz
import datetime


class OscsMonitor(BaseScan):
    """
    oscs监控
    """

    def __init__(self, req, task):
        super().__init__(req, task)

        self.task = task
        self.task_name = "oscs"
        self.hint = ""

        # 从表获取任务
        self.vmt = VulnMonitorTask.objects.filter(task_name=self.task_name, is_active=1).first()
        self.url = "https://www.oscs1024.com/oscs/v1/intelligence/list"

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        self.parse_oscs_list()

        return True

    def parse_oscs_list(self):

        if self.vmt:
            logger.info("[oscs Monitor] Monitor oscs vuln start.")

            local_tz = pytz.timezone('Asia/Shanghai')
            self.vmt.last_spider_time = datetime.datetime.now(local_tz)
            self.vmt.save()

            params = {
                "page": 1,
                "per_page": 30,
            }

            headers = {
                "Origin": "https://www.oscs1024.com",
                "Referer": "https://www.oscs1024.com/cm",
            }

            url = self.url

            content = self.req.post(url, 'JsonResp', 0, params, "", headers)

            r = json.loads(content)
            for msg in r['data']['data']:
                sid = msg['mps']
                create_time = msg['created_at']
                url = msg['url']
                title = msg['title']

                # check level
                level = msg['level']
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

                is_poc = msg['is_poc']
                is_exp = msg['is_exp']

                # check exist
                va = VulnData.objects.filter(sid=sid).first()
                if va:
                    continue

                logger.info("[Oscs Monitor] Found new Vuln {}".format(title))
                vn = VulnData(sid=sid, title=title, type=type, publish_time=create_time,
                              link=url, source="oscs", score=score, severity=severity,
                              is_poc=is_poc, is_exp=is_exp, is_active=1, state=0)
                vn.save()

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        xi = xxxbotInterface()

        xi.publish_admin(self.hint)
