#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: wechatMonitor.py
@time: 2023/5/26 15:15
@desc:

'''

from utils.log import logger

from botend.models import WechatArticle, WechatAccountTask, TargetAuth

from botend.controller.BaseScan import BaseScan
from botend.interface.gewechat import GeWechatInterface

import json
import time
import pytz
import random
import datetime
import urllib.parse
from urllib.parse import urlparse, parse_qs


class WechatMonitor(BaseScan):
    """
    微信公众号监控监控
    """

    def __init__(self, req, task):
        super().__init__(req, task)

        self.task = task
        self.hint = ""

        # 从wechat表获取任务
        self.wats = WechatAccountTask.objects.filter(is_zombie=0)

        # 获取auth配置
        auth = TargetAuth.objects.filter(domain="wechat").first()
        self.cookie = auth.cookie
        self.rfcode = auth.ext

        # 获取列表
        self.url1 = "https://mp.weixin.qq.com/cgi-bin/appmsg"

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        self.parse_wechat_article_list()

        return True

    def parse_wechat_article_list(self):

        for wat in self.wats:
            logger.info("[Wechat Monitor] Try to get {} article list".format(wat.account))

            local_tz = pytz.timezone('Asia/Shanghai')
            wat.last_spider_time = datetime.datetime.now(local_tz)
            wat.save()

            params = {
                "token": self.rfcode,
                "lang": "zh_CN",
                "f": "json",
                "ajax": "1",
                "action": "list_ex",
                "begin": 0,
                "count": 5,
                "query": "",
                "fakeid": wat.biz,
                "type": "9",
            }
            params_str = urllib.parse.urlencode(params)
            url = self.url1 + '?' + params_str

            content = self.req.get(url, 'Resp', 0, self.cookie)

            if "invalid session" in str(content):
                logger.warning("[Wechat Monitor] Wechat api session invalid. need login.")
                self.hint = "Wechat api session invalid. need login."

                self.trigger_webhook()
                return

            r = json.loads(content)
            if 'app_msg_list' in r:
                for msg in r['app_msg_list']:
                    cover = msg['cover']
                    create_time = datetime.datetime.fromtimestamp(msg['create_time'])
                    digest = msg['digest']
                    link = msg['link']
                    title = msg['title']

                    parsed_url = urlparse(link)
                    query_params = parse_qs(parsed_url.query)
                    sn = query_params.get('sn')[0]

                    waa = WechatArticle.objects.filter(sn=sn).first()

                    if waa:
                        continue

                    obj = WechatArticle(title=title, url=link, publish_time=create_time,
                                        biz=wat.biz, digest=digest, cover=cover,
                                        sn=sn, state=0)
                    obj.save()
                    logger.info("[Wechat Monitor] Found new Wechat article.")

            time.sleep(random.randint(120, 300))

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = GeWechatInterface()
        aw.init()
        aw.publish_admin(self.hint)
