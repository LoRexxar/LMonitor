#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: ngaMonitor.py
@time: 2024/2/22 16:30
@desc:

'''

from datetime import datetime
from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.models import MonitorTask
from botend.webhook.qiyeWechat import QiyeWechatWebhook
from botend.webhook.aibotkWechat import AibotkWechatWebhook

from botend.models import WowArticle


class ngaMonitor(BaseScan):
    """
    nga监控
    """
    def __init__(self, req, task):
        super().__init__(req, task)

        self.post_desp = ""
        self.post_img = ""
        self.task = task

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        cookies = ""
        url = "https://nga.178.com/thread.php?fid=7"
        driver = self.req.get(url, 'RespByChrome', 0, cookies, is_origin=1)

        # 处理返回内容
        self.resolve_data(driver)

        return True

    def resolve_data(self, driver):

        try:
            posts = driver.ele('#topicrows').eles('tag:tbody')

            for post in posts:
                tds = post.eles('tag:td')

                if not tds:
                    continue

                post_count = tds[0].text
                post_head = tds[1].ele('.:topic')
                post_link = post_head.link
                post_name = post_head.texts()
                post_date = tds[2].ele('.silver postdate').text

                # original_datetime = datetime.strptime(post_date, "%m-%d %H:%M")
                # django_date_time = original_datetime.strftime("%Y-%m-%d %H:%M")
                if not post_count or int(post_count) < 50:
                    continue

                wa = WowArticle.objects.filter(url=post_link).first()

                if wa:
                    continue

                obj = WowArticle(title=post_name, url=post_link, author="nga", description="")
                obj.save()
                logger.info("[wow Monitor] Found new wow article.{}".format(post_name))

                self.task.flag = post_link
                self.task.save()

                self.post_desp = """检测到NGA热门贴，回帖数{}，发帖时间{}
《{}》
{}""".format(post_count, post_date, post_name, post_link)

                self.trigger_webhook()

        except:
            raise

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = AibotkWechatWebhook()
        aw.publish_text(self.post_desp)
        # aw.publish_img(self.post_img)
        # aw.publish_card(self.post_desp, self.post_img)