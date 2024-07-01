#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: BiliMonitor.py
@time: 2023/5/12 14:24
@desc:

'''


from DrissionPage import errors
from datetime import datetime
from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.models import WowArticle
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

            videos = driver.wait.eles_loaded('.:fakeDanmu-item')

            for video in videos:
                video_time = video.ele('.time')
                if video_time:
                    video_dic = video.ele('.title')
                    video_link = video_dic.attr("href")
                    video_name = video_dic.text

                    # 检查视频是否更新
                    wa = WowArticle.objects.filter(url=video_link).first()

                    if wa:
                        continue

                    current_time = datetime.now()

                    # 将时间格式化为 "%Y-%m-%d %H:%M" 格式
                    formatted_time = current_time.strftime("%Y-%m-%d %H:%M")

                    obj = WowArticle(title=video_name, url=video_link, author="lorexxarbilibili",
                                     publish_time=formatted_time, description=video_name)
                    obj.save()
                    logger.info("[Bili Monitor] Found new Bilibili.{}".format(video_name))

                    self.video_desp = """你关注的up主更新视频啦！！
《{}》
{}
""".format(video_name, video_link)

                    self.trigger_webhook()

        except errors.ContextLostError:
            self.resolve_data(driver)
        except:
            raise

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = AibotkWechatWebhook()
        aw.publish_text(self.video_desp)