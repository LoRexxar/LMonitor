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
from botend.controller.plugins.vuln import Vul_List, Vul_link_Type_Dict

from botend.controller.BaseScan import BaseScan
from botend.interface.gewechat import GeWechatInterface

import time
import random
import DrissionPage


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

            try:
                driver = self.req.get(self.avd_url, 'RespByChrome', 0, "", is_origin=1)

                tr_list = driver.eles('tag:tr')

                for tr in tr_list:
                    tds = tr.eles('tag:td')

                    if not tds:
                        continue

                    link = tds[0].eles('tag:a')[0].attr("href")
                    avid = tds[0].text
                    title = tds[1].text
                    publish_time = tds[3].text
                    type = ""

                    # check type
                    for vtype in Vul_List:
                        if vtype in title:
                            type = vtype
                            break

                    if not type:
                        type = tds[2].eles('tag:button')[0].attr("data-original-title")

                        if type in Vul_link_Type_Dict:
                            type = Vul_link_Type_Dict[type]

                    status = tds[4].eles('tag:button')
                    cve = status[0].attr("data-original-title")
                    poc_status = status[1].attr("data-original-title")

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

            except AttributeError:
                logger.error("[Aliyun Avd Monitor] Monitor aliyun avd start error.")
                return

            except DrissionPage.errors.ContextLostError:
                logger.error("[Aliyun Avd Monitor] page refresh. return back")
                return

            except:
                raise

            time.sleep(random.randint(10, 20))

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = GeWechatInterface()
        aw.init()
        aw.publish_admin(self.hint)
