#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: BiliMonitor.py
@time: 2023/5/12 14:24
@desc:

'''


from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.models import MonitorTask
from botend.webhook.qiyeWechat import QiyeWechatWebhook
from botend.webhook.aibotkWechat import AibotkWechatWebhook

from DrissionPage.common import By


class BiliMonitor(BaseScan):
    """
    bili 视频更新监控
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
        driver = self.req.get(url, 'RespByChrome', 0, cookies, is_origin=1)

        # 处理返回内容
        self.resolve_data(driver)

        return True

    def resolve_data(self, driver):

        try:
            if not driver:
                return

            videos = driver.eles('.:fakeDanmu-item')

            for video in videos:
                video_time = video.eles('.:time')[0]
                if "分钟" in video_time.text:
                    video_dic = video.eles('.:title')[0]
                    video_link = video_dic.attr("href")
                    video_name = video_dic.text

                    # 检查视频是否更新
                    if self.task.flag == video_link:
                        return

                    logger.info("[Bili Monitor] Task {} found update.".format(self.task.id))
                    self.task.flag = video_link
                    self.task.save()

                    self.video_desp = """你关注的up主更新视频啦！！
《{}》
{}
""".format(video_name, video_link)

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