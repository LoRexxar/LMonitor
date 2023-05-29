#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: wechatArticleScan.py
@time: 2023/5/26 19:36
@desc:

'''

from utils.log import logger

from Botend.models import MonitorTask, WechatArticle, WechatAccountTask, TargetAuth

from Botend.controller.BaseScan import BaseScan
from Botend.webhook.aibotkWechat import AibotkWechatWebhook

import re
import json
import time
import pytz
import random
import datetime
import urllib.parse
from urllib.parse import urlparse, parse_qs
import selenium
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


class WechatArticleScan(BaseScan):
    """
    微信公众号扫描
    """
    def __init__(self, req, task):
        super().__init__(req, task)

        self.task = task
        self.hint = ""
        self.cookie = ""

        # 从wechat表获取任务
        self.was = WechatArticle.objects.filter(state=0)

        # 获取列表
        self.base_url = "http://mp.weixin.qq.com/"

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        # 去base页面在继续请求
        cookies = ""
        driver = self.req.get(self.base_url, 'RespByChrome', 0, cookies, is_origin=1)

        # 处理返回内容
        self.parse_wechat_article(driver)

        return True

    def parse_wechat_article(self, driver):

        for wa in self.was:
            logger.info("[WechatArticleScan] Try to get article {}".format(wa.title))
            wa.state = 1
            wa.save()

            local_tz = pytz.timezone('Asia/Shanghai')

            try:
                driver.get(wa.url)
                wait = WebDriverWait(driver, 25)
                wait.until(EC.presence_of_element_located((By.ID, "js_content")))

                # title = driver.find_elements(By.CLASS_NAME, 'rich_media_title')[0].text

                author = driver.find_elements(By.CLASS_NAME, 'rich_media_meta_text')[0].text
                if "202" in author:
                    author = ""

                account = driver.find_elements(By.ID, 'js_name')[0].text
                create_time = driver.find_elements(By.ID, 'publish_time')[0].text
                content = driver.find_elements(By.ID, 'js_content')[0].get_attribute('innerHTML')

                # 正则
                page_source = driver.page_source

                source_url = ""
                source_url_regex = r"var msg_source_url = '(.*?)';"
                match = re.search(source_url_regex, page_source)
                if match:
                    source_url = match.group(1)

                summary_regex = r'class="profile_meta_value">(.*?)</span>'
                results = re.findall(summary_regex, page_source, re.M|re.I)
                summary = results[1]

                # 检查account的信息是否获得
                waccount = WechatAccountTask.objects.filter(biz=wa.biz).first()
                if not waccount.account:
                    waccount.account = account
                    waccount.summary = summary
                    waccount.last_publish_time = datetime.datetime.strptime(create_time, "%Y-%m-%d %H:%M")

                # 更新扫描时间
                elif datetime.datetime.strptime(create_time, "%Y-%m-%d %H:%M").replace(tzinfo=local_tz) > waccount.last_publish_time:
                    waccount.last_publish_time = create_time

                # 检查是否超过半年没更新
                if datetime.datetime.now() - waccount.last_publish_time > datetime.timedelta(days=180):
                    waccount.is_zombie = 1

                waccount.last_spider_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                waccount.save()

                # 更新
                wa.account = account
                wa.author = author
                wa.publish_time = create_time
                wa.content_html = content
                wa.source_url = source_url
                wa.state = 2
                wa.save()

                time.sleep(random.randint(10, 30))

            except selenium.common.exceptions.NoSuchElementException:
                logger.warning("[WechatArticleScan] Wechat Article Scan can't get target element.")
                return False

            except selenium.common.exceptions.TimeoutException:
                logger.warning("[WechatArticleScan] Wechat Article Scan timeout.")
                return False

            except:
                raise

            time.sleep(random.randint(120, 300))

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = AibotkWechatWebhook()
        aw.publish_text(self.hint)
