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

from botend.models import WechatArticle, WechatAccountTask

from botend.controller.BaseScan import BaseScan
from botend.interface.xxxbot import xxxbotInterface

import re
import time
import random
import datetime
from django.utils import timezone


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
            logger.info("[WechatArticleScan] Try to get article {}".format(wa.url))
            wa.state = 1
            wa.save()

            try:
                driver.get(wa.url)

                # title = driver.eles('.:rich_media_title')[0].text

                author = driver.eles('.:rich_media_meta_text')[0].text
                if "202" in author:
                    author = ""

                account = driver.eles('#js_name')[0].text
                create_time = driver.eles('#publish_time')[0].text
                content = driver.eles('#js_content')[0].inner_html

                # 正则
                page_source = driver.html

                source_url = ""
                source_url_regex = r"var msg_source_url = '(.*?)';"
                match = re.search(source_url_regex, page_source)
                if match:
                    source_url = match.group(1)

                summary_regex = r'class="profile_meta_value">(.*?)</span>'
                results = re.findall(summary_regex, page_source, re.M|re.I)
                summary = results[1] if len(results) > 1 else ""

                # 检查account的信息是否获得
                waccount = WechatAccountTask.objects.filter(biz=wa.biz).first()
                publish_dt = datetime.datetime.strptime(create_time, "%Y年%m月%d日 %H:%M")
                publish_dt = timezone.make_aware(publish_dt, timezone.get_default_timezone())
                last_publish_dt = waccount.last_publish_time
                if last_publish_dt and timezone.is_naive(last_publish_dt):
                    last_publish_dt = timezone.make_aware(last_publish_dt, timezone.get_default_timezone())

                if not waccount.account:
                    waccount.account = account
                    waccount.summary = summary
                    waccount.last_publish_time = publish_dt

                # 更新扫描时间
                elif (not last_publish_dt) or (publish_dt > last_publish_dt):
                    waccount.last_publish_time = publish_dt

                # 检查是否超过半年没更新
                last_check_dt = waccount.last_publish_time
                if last_check_dt and timezone.is_naive(last_check_dt):
                    last_check_dt = timezone.make_aware(last_check_dt, timezone.get_default_timezone())
                if last_check_dt and (timezone.now() - last_check_dt) > datetime.timedelta(days=180):
                    waccount.is_zombie = 1

                waccount.save()

                # 更新
                wa.account = account
                wa.author = author
                wa.content_html = content
                wa.source_url = source_url
                wa.state = 2
                wa.save()

                time.sleep(random.randint(10, 20))

            except AttributeError:
                logger.error("[WechatArticleScan] Fail to get article {}".format(wa.url))

            except:
                raise

            time.sleep(random.randint(120, 300))

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        xi = xxxbotInterface()

        xi.publish_admin(self.hint)
