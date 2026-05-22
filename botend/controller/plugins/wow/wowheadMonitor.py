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
import re
import html
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

        driver = None
        try:
            driver = self.req.get(self.target_url, 'RespByChrome', 0, cookies, is_origin=1)
        except Exception as e:
            logger.warning("[wowheadMonitor] Chrome request init failed: {}".format(str(e)))

        if not driver or not hasattr(driver, 'eles'):
            logger.error("[wowheadMonitor] Chrome request failed.")
            resp = self.req.getResponse(self.target_url, cookies)
            if resp is False or resp is None:
                logger.error("[wowheadMonitor] Request failed.")
                return False
            status_code = getattr(resp, 'status_code', 200)
            if int(status_code or 0) >= 400:
                logger.error("[wowheadMonitor] Request bad status: {}".format(status_code))
                return False
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
                    post_type = ""
                    type_eles = post.eles('.news-card-simple-text-type .meta-text') or post.eles('.news-card-simple-text-type') or []
                    if type_eles:
                        post_type = (type_eles[0].text or "").strip()

                    post_title = ""
                    post_link = ""
                    title_link_eles = post.eles('.news-card-simple-text-title a') or []
                    if title_link_eles:
                        post_title = (title_link_eles[0].text or "").strip()
                        post_link = (getattr(title_link_eles[0], "link", "") or "").strip() or (title_link_eles[0].attr("href") or "").strip()

                    post_preview = ""
                    preview_eles = post.eles('.news-card-simple-text-preview') or []
                    if preview_eles:
                        post_preview = (preview_eles[0].text or "").strip()

                    post_date = ""
                    date_eles = post.eles('.news-card-simple-text-byline-posted') or post.eles('.news-card-simple-text-byline span') or []
                    if date_eles:
                        post_date = (date_eles[0].attr('title') or "").strip()

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
                    if post_link.startswith('/'):
                        post_link = "https://www.wowhead.com{}".format(post_link)

                    wa = WowArticle.objects.filter(url=post_link).first()
                    if wa:
                        if not (getattr(wa, "description", "") or "").strip() or len((getattr(wa, "description", "") or "")) < 800:
                            body = self._fetch_article_body(post_link, cookies="")
                            if body:
                                wa.description = body
                                wa.save(update_fields=["description"])
                        continue

                    body = self._fetch_article_body(post_link, cookies="")
                    obj = WowArticle(
                        title="[{}]{}".format(post_type, post_title),
                        url=post_link,
                        publish_time=django_date_time,
                        author="wowhead",
                        description=body or post_preview,
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

    def _fetch_article_body(self, url, cookies=""):
        try:
            resp = self.req.getResponse(url, cookies)
            if not resp:
                return ""
            status_code = getattr(resp, 'status_code', 200)
            if int(status_code or 0) >= 400:
                return ""
            html_text = getattr(resp, 'text', '') or ''
            if not html_text:
                return ""
            return self._extract_body_from_html(html_text)
        except Exception:
            return ""

    def _extract_body_from_html(self, html_text):
        t = html_text or ""
        blocks = []
        for pat in (
            r'<div[^>]+class="[^"]*(?:news-post-content|news-post-text|content-body)[^"]*"[^>]*>([\s\S]*?)</div>',
            r'<article[^>]*>([\s\S]*?)</article>',
        ):
            m = re.search(pat, t, flags=re.I)
            if m:
                blocks.append(m.group(1) or "")
        if not blocks:
            return ""
        raw = max(blocks, key=lambda x: len(x or ""))
        raw = re.sub(r'<(script|style|noscript)[^>]*>[\s\S]*?</\1>', '', raw, flags=re.I)
        raw = re.sub(r'(?i)<br\s*/?>', '\n', raw)
        raw = re.sub(r'(?i)</p\s*>', '\n\n', raw)
        raw = re.sub(r'(?i)</div\s*>', '\n\n', raw)
        raw = re.sub(r'(?i)</li\s*>', '\n', raw)
        raw = re.sub(r'<[^>]+>', '', raw)
        raw = html.unescape(raw)
        raw = raw.replace('\r\n', '\n').replace('\r', '\n')
        lines = [ln.strip() for ln in raw.split('\n')]
        out = []
        blank = 0
        for ln in lines:
            if not ln:
                blank += 1
                if blank <= 1:
                    out.append("")
                continue
            blank = 0
            out.append(ln)
        text = "\n".join(out).strip()
        return text

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        xi = xxxbotInterface()

        xi.send_msg(self.post_desp)
