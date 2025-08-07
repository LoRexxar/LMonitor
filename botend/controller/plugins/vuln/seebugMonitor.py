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
from botend.controller.plugins.vuln import Vul_List

from botend.controller.BaseScan import BaseScan
from botend.interface.xxxbot import xxxbotInterface

import time
import random


# import selenium


class SeebugMonitor(BaseScan):
    """
    seebug漏洞监控
    """

    def __init__(self, req, task):
        super().__init__(req, task)

        self.task = task
        self.task_name = "seebug"
        self.hint = ""

        # 从表获取任务
        self.vmt = VulnMonitorTask.objects.filter(task_name=self.task_name, is_active=1)
        self.seebug = "https://www.seebug.org"
        self.seebug_url = "https://www.seebug.org/vuldb/vulnerabilities"

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
            driver = self.req.get(self.seebug_url, 'RespByChrome', 0, "", is_origin=1)

            try:
                tr_list = driver.eles('tag:tr')

                for tr in tr_list:
                    tds = tr.eles('tag:td')

                    if not tds:
                        continue

                    sid = tds[0].text
                    publish_time = tds[1].text
                    level = tds[2].eles('tag:div')[0].attr("data-original-title")

                    link_tag = tds[3].eles('tag:a')[0]
                    title = link_tag.text
                    link = link_tag.link

                    tag_list = tds[4].eles('tag:i')

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

                    if "无 CVE" not in tag_list[0].attr("data-original-title"):
                        cveid = tag_list[0].attr("data-original-title")
                        is_cve = 1

                    if "有 PoC" in tag_list[1].attr("data-original-title"):
                        is_poc = 1

                    elif "有 ExP" in tag_list[1].attr("data-original-title"):
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

            except AttributeError:
                logger.info("[seebug Monitor] bad requests.")

            except:
                raise

            time.sleep(random.randint(10, 30))

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        xi = xxxbotInterface()

        xi.publish_admin(self.hint)