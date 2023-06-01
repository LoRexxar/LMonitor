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

import selenium
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


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
            wait = WebDriverWait(driver, 15)
            wait.until(EC.presence_of_element_located((By.ID, "video-list-style")))

            videos = driver.find_elements(By.CLASS_NAME, 'fakeDanmu-item')

            for video in videos:
                video_time = video.find_elements(By.CLASS_NAME, 'time')[0]
                if "分钟" in video_time.text:
                    video_dic = video.find_elements(By.CLASS_NAME, 'title')[0]
                    video_link = video_dic.get_attribute("href")
                    video_name = video_dic.text

                    # 检查视频是否更新
                    if self.task.flag == video_link:
                        return

                    logger.info("[Bili Monitor] Task {} found update.".format(self.task.id))
                    self.task.flag = video_link

                    self.video_desp = """你关注的up主更新视频啦！！
《{}》
{}
""".format(video_name, video_link)

                    self.trigger_webhook()
                    return

        except selenium.common.exceptions.NoSuchElementException:
            logger.warning("[BiliMonitor] BiliMonitor can't get target element.")
            return False

        except selenium.common.exceptions.TimeoutException:
            logger.warning("[BiliMonitor] BiliMonitor timeout.")
            return False

        except:
            raise

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = AibotkWechatWebhook()
        aw.publish_text(self.video_desp)