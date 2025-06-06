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

from botend.models import VulnData

from botend.controller.BaseScan import BaseScan
from botend.interface.xxxbot import xxxbotInterface

import json
import time
import random


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
        self.url = "https://ti.qianxin.com/alpha-api/v2/vuln/vuln-detail"
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
                "vuln_ids": id,
            }

            headers = {
                "Origin": "https://ti.qianxin.com",
                "Referer": "https://ti.qianxin.com/vulnerability",
                "Content-Type": "application/json",
            }

            content = self.req.post(self.url, 'JsonResp', 0, params, "", headers)
            r = json.loads(content)

            if "data" not in r or len(r['data']) == 0:
                logger.warning("[qax Scan] error: {}".format(r))
                return

            msg = r['data'][0]
            vd.description = msg['vuln_description_cn']

            reference = ""
            reference_list = [d['url'] for d in msg['reference']['other']]
            vd.reference = ','.join(reference_list)

            score = msg['risk']['qvc']['base_score']
            vd.score = score

            vd.solutions = msg['residence_info']['fix_method_cn']

            vd.state = 2
            vd.save()

            time.sleep(random.randint(10, 30))

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        xi = xxxbotInterface()

        xi.publish_admin(self.hint)
