#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: oscsScan.py.py
@time: 2023/6/14 20:40
@desc:

'''


from utils.log import logger

from botend.models import VulnData

from botend.controller.BaseScan import BaseScan
from botend.interface.aibotkWechat import AibotkWechatWebhook

import json
import time
import random


class OscsScan(BaseScan):
    """
    oscs 扫描
    """

    def __init__(self, req, task):
        super().__init__(req, task)

        self.task = task
        self.task_name = "oscs"
        self.hint = ""

        # 从表获取任务
        self.vds = VulnData.objects.filter(source="oscs", state=0)
        self.url = "https://www.oscs1024.com/oscs/v1/vdb/info"

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
            logger.info("[Oscs Scan] Oscs Scan details {}.".format(vd.title))
            vd.state = 1
            vd.save()

            params = {
                "vuln_no": vd.sid,
            }

            headers = {
                "Origin": "https://www.oscs1024.com",
                "Referer": "https://www.oscs1024.com/cm",
                "Content-Type": "application/json",
            }

            url = self.url

            content = self.req.post(url, 'JsonResp', 0, params, "", headers)

            r = json.loads(content)
            msg = r['data'][0]
            cve_id = msg['cve_id']
            score = msg['cvss_score']
            description = msg['description']

            references_list = msg['references']
            references_url_list = [d['url'] for d in references_list]
            references = '\n '.join(references_url_list)

            solutions = '\n '.join(msg['soulution_data'])

            # check cveid
            if cve_id:
                tempvd = VulnData.objects.filter(cveid=cve_id).first()
                if tempvd:
                    vd.cveid = cve_id

            # check score
            if score:
                vd.score = score

            vd.description = description
            vd.solutions = solutions
            vd.reference = references
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
