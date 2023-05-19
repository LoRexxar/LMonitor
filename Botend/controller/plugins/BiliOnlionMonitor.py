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
from Botend.controller.BaseScan import BaseScan
from Botend.models import MonitorTask
from Botend.webhook.qiyeWechat import QiyeWechatWebhook
from Botend.webhook.aibotkWechat import AibotkWechatWebhook

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
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "live-status")))

            status = driver.find_elements(By.CLASS_NAME, 'live-status')[0].text
            title = driver.find_elements(By.CLASS_NAME, 'live-skin-main-text')[0].text

            if "直播" in status:
                # 检查当前直播状态
                if self.task.flag == "1":
                    return

                self.video_desp = """你关注的up主LoRexxar开启直播啦！！
{}
{}
                """.format(self.task.target, title)
                self.task.flag = "1"

                self.trigger_webhook()
                return

            if "轮播" in status:
                self.task.flag = "0"

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
