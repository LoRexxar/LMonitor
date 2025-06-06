#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: RssMonitor.py
@time: 2023/6/16 17:57
@desc:

'''

from utils.log import logger

from botend.models import RssArticle, RssMonitorTask
from botend.controller.BaseScan import BaseScan

import re
import json
import time
import pytz
import random
import datetime
import feedparser
import socket


class RssArticleMonitor(BaseScan):
    """
    rss监控监控
    """

    def __init__(self, req, task):
        super().__init__(req, task)

        self.task = task
        self.hint = ""

        # 从rss表获取任务
        self.rmts = RssMonitorTask.objects.filter(is_active=1)

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        logger.info("[Rss Monitor] Start Rss check.")
        try:
            self.parse_rss_article_list()
        except OverflowError:
            logger.error("[Rss Monitor] bad timestamp.")
            return False

        return True

    def parse_rss_article_list(self):

        for rmt in self.rmts:
            logger.info("[Rss Monitor] Try to get {} article list".format(rmt.name))

            local_tz = pytz.timezone('Asia/Shanghai')
            rmt.last_spider_time = datetime.datetime.now(local_tz)
            rmt.save()

            socket.setdefaulttimeout(20)
            f = feedparser.parse(rmt.link)

            for msg in f.entries:
                title = msg.title
                url = msg.link
                author = rmt.name

                # check time
                publish_time = "2000-01-01 02:44:46"
                if "published_parsed" in msg:
                    dt = datetime.datetime.fromtimestamp(time.mktime(msg.published_parsed))
                    publish_time = dt.strftime("%Y-%m-%d %H:%M:%S.%f%Z")
                elif "updated_date" in msg:
                    publish_time = msg.updated_date

                content = ""
                if "summary" in msg:
                    content = re.sub('<[^<]+?>', '', msg.summary)
                elif "content" in msg:
                    content = msg.content[0].value
                    content = re.sub('<[^<]+?>', '', content)

                ra = RssArticle.objects.filter(title=title).first()

                if ra:
                    continue

                obj = RssArticle(rss_id=rmt.id, title=title, url=url, author=author,
                                 publish_time=publish_time, content_html=content)
                obj.save()
                logger.info("[Rss Monitor] Found new Rss article.{}".format(title))

