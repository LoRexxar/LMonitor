#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: lhfszsMonitor.py
@time: 2024/2/22 16:30
@desc:

'''


from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.models import MonitorTask
from botend.webhook.qiyeWechat import QiyeWechatWebhook
from botend.webhook.aibotkWechat import AibotkWechatWebhook


class LhfszsMonitor(BaseScan):
    """
    Lhfszs监控
    """
    def __init__(self, req, task):
        super().__init__(req, task)

        self.video_desp = ""
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

                if "最近更新" in update_state:
                    video_dic = post.ele('.post-excerpt').ele('tag:p').text
                    video_link = post_time.link
                    video_name = post.ele('.post-title').text

                    # 检查视频是否更新
                    if self.task.flag == video_link:
                        return

                    logger.info("[wow Monitor] Task {} found update.".format(self.task.id))
                    self.task.flag = video_link
                    self.task.save()

                    self.video_desp = """检测到最新的魔兽世界新闻：
《{}》
{}
------------------
{}
""".format(video_name, video_link, video_dic)

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
        aw.publish_text(self.video_desp)