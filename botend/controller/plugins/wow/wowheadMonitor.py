#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: ngaMonitor.py
@time: 2024/2/22 16:30
@desc:

'''

import time
import DrissionPage
from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.interface.xxxbot import xxxbotInterface

from botend.models import WowArticle
from datetime import datetime
from django.utils import timezone


class wowheadMonitor(BaseScan):
    """
    wowhead监控
    """
    def __init__(self, req, task):
        super().__init__(req, task)

        self.post_desp = ""
        self.target_url = "https://www.wowhead.com/wow/retail"
        self.task = task

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        cookies = ""

        resp = self.req.getResponse(self.target_url, cookies)
        if resp is False or resp is None:
            logger.error("[wowheadMonitor] Request failed.")
            return False
        status_code = getattr(resp, 'status_code', 200)
        if int(status_code or 0) >= 400:
            logger.error("[wowheadMonitor] Request bad status: {}".format(status_code))
            return False

        driver = self.req.get(self.target_url, 'RespByChrome', 0, cookies, is_origin=1, is_proxy=False)
        if not driver or not hasattr(driver, 'eles'):
            logger.error("[wowheadMonitor] Chrome request failed.")
            return False

        post_count, _ = self.resolve_data(driver, "wowhead", 10)
        if int(post_count or 0) <= 0:
            return False

        return True

    def resolve_data(self, driver, title="", limit=10):

        try:
            time.sleep(2)

            posts = []
            for _ in range(8):
                posts = driver.eles('.news-card-simple') or driver.eles('#news-card-simple') or []
                if posts:
                    break
                time.sleep(1)

            if not posts:
                logger.error("[wowheadMonitor] No posts found.")
                return 0, 0

            new_count = 0
            for post in posts[:int(limit or 10)]:
                try:
                    post_type = post.ele('.news-card-simple-text').text
                    post_title = post.ele('.news-card-simple-text-title').text
                    post_link = post.ele('.news-card-simple-text-title').link
                    post_preview = post.ele('.news-card-simple-text-preview').text
                    post_date = post.ele('.news-card-simple-text-byline').ele('tag:span').attr('title')

                    django_date_time = None
                    if post_date:
                        try:
                            original_datetime = datetime.strptime(post_date, "%Y/%m/%d at %H:%M")
                            django_date_time = original_datetime
                        except ValueError:
                            for fmt in ("%b %d, %Y at %H:%M", "%B %d, %Y at %H:%M"):
                                try:
                                    original_datetime = datetime.strptime(post_date, fmt)
                                    django_date_time = original_datetime
                                    break
                                except ValueError:
                                    pass
                    if not django_date_time:
                        django_date_time = timezone.localtime(timezone.now()).replace(tzinfo=None)
                    if timezone.is_naive(django_date_time):
                        django_date_time = timezone.make_aware(django_date_time, timezone.get_current_timezone())

                    if not post_link:
                        continue

                    wa = WowArticle.objects.filter(url=post_link).first()
                    if wa:
                        continue

                    obj = WowArticle(
                        title="[{}]{}".format(post_type, post_title),
                        url=post_link,
                        publish_time=django_date_time,
                        author="wowhead",
                        description=post_preview,
                        source="wowhead",
                        category="news",
                    )
                    obj.save()
                    new_count += 1
                    logger.info("[wowhead Monitor] Found new wowhead article.{}".format(post_title))

                    self.task.flag = post_link
                    self.task.save()

                    self.post_desp = """WowHead新闻<{}>，发帖时间{}
[{}]《{}》
{}""".format(title, post_date, post_type, post_title, post_link)

                    self.trigger_webhook()
                except Exception as e:
                    logger.warning("[wowheadMonitor] Parse post failed: {}".format(str(e)))
                    continue

            return len(posts), new_count

        except DrissionPage.errors.ElementNotFoundError:
            logger.error("[wowheadMonitor] bad request.")

        except DrissionPage.errors.PageDisconnectedError:
            logger.error("[wowheadMonitor] PageDisconnectedError.")

        except AttributeError:
            logger.error("[wowheadMonitor] No posts found.")

        except:
            raise

        return 0, 0

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        xi = xxxbotInterface()

        xi.send_msg(self.post_desp)
