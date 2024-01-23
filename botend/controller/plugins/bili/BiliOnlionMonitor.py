#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: BiliOnlionMonitor.py
@time: 2023/5/12 19:08
@desc:

'''

from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.models import MonitorTask
from botend.webhook.qiyeWechat import QiyeWechatWebhook
from botend.webhook.aibotkWechat import AibotkWechatWebhook

import json
import selenium
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


class BiliOnlionMonitor(BaseScan):
    """
    bili 直播状态监控
    """
    def __init__(self, req, task):
        super().__init__(req, task)

        self.video_desp = ""
        self.title = ""
        self.task = task

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        cookies = ""

        # 通过live页面检测
        self.url1 = "https://live.bilibili.com/{}".format(url)
        driver = self.req.get(self.url1, 'RespByChrome', 0, cookies, is_origin=1)

        # 处理返回内容
        self.resolve_data_live(driver)

        # 通过api检测
        url2 = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?room_id={}".format(url)
        self.resolve_data(url2)

        return True

    def resolve_data_live(self, driver):

        try:
            wait = WebDriverWait(driver, 25)
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "live-skin-main-text")))

            # status = driver.find_elements(By.CLASS_NAME, 'live-status')[0].text
            self.title = driver.find_elements(By.CLASS_NAME, 'live-skin-main-text')[0].text

        except selenium.common.exceptions.NoSuchElementException:
            logger.warning("[BiliOnlionMonitor] BiliOnlionMonitor can't get target element.")
            return False

        except selenium.common.exceptions.TimeoutException:
            logger.warning("[BiliOnlionMonitor] BiliOnlionMonitor timeout.")
            return False

        except:
            raise

    def resolve_data(self, url):

        r = self.req.get(url, 'Resp', 0, "")
        status = json.loads(r)
        status_code = status['code']
        # print(self.title)

        if status_code != 0:
            # 检查当前直播状态
            if self.task.flag == "1":
                return

            self.video_desp = """你关注的up主LoRexxar开启直播啦！！
        B站：{}
        Douyu: https://www.douyu.com/499738
        {}
                        """.format(self.url1, self.title)
            self.task.flag = "1"

            self.trigger_webhook()
            return

        if status_code == 0:
            self.task.flag = "0"
            # print(status_code)

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = AibotkWechatWebhook()
        aw.publish_text(self.video_desp)
