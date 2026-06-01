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
import random
import datetime
import socket
from django.utils import timezone
from django.db import connection

try:
    import feedparser
except Exception:
    feedparser = None


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
        if feedparser is None:
            logger.error("[Rss Monitor] feedparser not installed")
            return

        def get_db_column_internal_size(model, field_name):
            try:
                table_name = model._meta.db_table
            except Exception:
                return None

            try:
                with connection.cursor() as cursor:
                    cols = connection.introspection.get_table_description(cursor, table_name)
            except Exception:
                return None

            for col in cols:
                col_name = getattr(col, "name", None)
                internal_size = getattr(col, "internal_size", None)
                if col_name is None:
                    try:
                        col_name = col[0]
                        internal_size = col[3]
                    except Exception:
                        continue
                if col_name == field_name:
                    return internal_size
            return None

        def get_field_max_length(model, field_name):
            try:
                return getattr(model._meta.get_field(field_name), "max_length", None)
            except Exception:
                return None

        title_max_length = get_db_column_internal_size(RssArticle, "title") or get_field_max_length(RssArticle, "title")
        url_max_length = get_db_column_internal_size(RssArticle, "url") or get_field_max_length(RssArticle, "url")

        for rmt in self.rmts:
            logger.info("[Rss Monitor] Try to get {} article list".format(rmt.name))

            rmt.last_spider_time = timezone.now()
            rmt.save()

            socket.setdefaulttimeout(20)
            try:
                f = feedparser.parse(rmt.link)
            except Exception as e:
                logger.warning("[Rss Monitor] Fetch rss failed: {} {}".format(rmt.link, str(e)))
                continue

            for msg in f.entries:
                title = (getattr(msg, "title", "") or "").strip()
                url = (getattr(msg, "link", "") or "").strip()

                if title_max_length and len(title) > title_max_length:
                    title = title[:title_max_length]
                if url_max_length and len(url) > url_max_length:
                    url = url[:url_max_length]
                author = rmt.name

                # check time
                publish_time = "2000-01-01 02:44:46"
                if "published_parsed" in msg and msg.published_parsed is not None:
                    try:
                        dt = datetime.datetime.fromtimestamp(time.mktime(msg.published_parsed))
                        publish_time = dt.strftime("%Y-%m-%d %H:%M:%S.%f%Z")
                    except (ValueError, OverflowError, TypeError, OSError):
                        logger.warning("[Rss Monitor] invalid published_parsed for article: {}".format(title))
                elif "updated_date" in msg:
                    publish_time = msg.updated_date

                content = ""
                if "summary" in msg:
                    content = re.sub('<[^<]+?>', '', msg.summary)
                elif "content" in msg:
                    content = msg.content[0].value
                    content = re.sub('<[^<]+?>', '', content)

                qs = RssArticle.objects.filter(rss_id=rmt.id)
                ra = None
                if url:
                    ra = qs.filter(url=url).first()
                if ra is None and title:
                    ra = qs.filter(title=title).first()

                if ra:
                    continue

                obj = RssArticle(rss_id=rmt.id, title=title, url=url, author=author,
                                 publish_time=publish_time, content_html=content)
                obj.save()
                logger.info("[Rss Monitor] Found new Rss article.{}".format(title))

