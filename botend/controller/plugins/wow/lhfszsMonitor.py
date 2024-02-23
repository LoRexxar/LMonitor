#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: lhfszsMonitor.py
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


class LhfszsMonitor(BaseScan):
    """
    Lhfszs监控
    """
    def __init__(self, req, task):
        super().__init__(req, task)

        self.post_desp = ""
        self.task = task

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        cookies = ""
        url = "https://lhfszs.com/"
        driver = self.req.get(url, 'RespByChrome', 0, cookies, is_origin=1)

        # 处理返回内容
        self.resolve_data(driver)

        return True

    def resolve_data(self, driver):

        try:
            posts = driver.eles('.post-container')

            for post in posts:
                post_time = post.ele('.post-date')
                update_state = post.ele('.post-excerpt').text
                # print(update_state)

                post_dic = post.ele('.post-excerpt').ele('tag:p').text
                post_link = post_time.link
                post_name = post.ele('.post-title').text

                original_datetime = datetime.strptime(post_time.text, "%H:%M %Y/%m/%d")
                django_date_time = original_datetime.strftime("%Y-%m-%d %H:%M")

                wa = WowArticle.objects.filter(url=post_link).first()

                if wa:
                    continue

                obj = WowArticle(title=post_name, url=post_link, author="lhfszs",
                                 publish_time=django_date_time, description=post_dic)
                obj.save()
                logger.info("[wow Monitor] Found new wow article.{}".format(post_name))

                self.task.flag = post_link
                self.task.save()

                self.post_desp = """检测到最新的魔兽世界新闻：
《{}》
{}
------------------
{}
""".format(post_name, post_link, post_dic)

                self.trigger_webhook()
                return

        except:
            raise

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = AibotkWechatWebhook()
        aw.publish_text(self.post_desp)