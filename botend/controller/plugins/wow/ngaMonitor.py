#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: ngaMonitor.py
@time: 2024/2/22 16:30
@desc:

'''

import re
from urllib.parse import urljoin
from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.interface.xxxbot import xxxbotInterface

from botend.models import TargetAuth, WowArticle


class ngaMonitor(BaseScan):
    """
    nga监控
    """
    def __init__(self, req, task):
        super().__init__(req, task)

        self.post_desp = ""
        self.black_list = ["公益", "代工", "支持跨服"]
        self.target_list = {
            "前瞻区": {
                "url": "https://nga.178.com/thread.php?fid=310&ff=7",
                "limit": 10
            },
            # "cos区": {
            #     "url": "https://nga.178.com/thread.php?fid=472",
            #     "limit": 10
            # },
            "水区": {
                "url": "https://nga.178.com/thread.php?fid=7",
                "limit": 200
            }
        }
        self.task = task

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        auth = TargetAuth.objects.filter(domain="nga.178.com", is_login=True).first()
        cookies = auth.cookie if auth and auth.cookie else ""

        for title in self.target_list:
            url = self.target_list[title]["url"]
            html = self.req.get(url, 'Resp', 0, cookies)
            # 处理返回内容
            self.resolve_data(html, title, self.target_list[title]["limit"])

        return True

    def resolve_data(self, html, title="", limit=10):

        try:
            if not html:
                logger.error("[ngaMonitor] empty html.")
                return
            if isinstance(html, (bytes, bytearray)):
                raw_html = bytes(html)
                charset_match = re.search(br'charset\s*=\s*["\']?([A-Za-z0-9._-]+)', raw_html[:4096], re.I)
                charset = charset_match.group(1).decode("ascii", "ignore") if charset_match else "utf-8"
                try:
                    html = raw_html.decode(charset, "replace")
                except (LookupError, UnicodeDecodeError):
                    html = raw_html.decode("utf-8", "replace")

            try:
                from bs4 import BeautifulSoup
            except Exception:
                logger.error("[ngaMonitor] BeautifulSoup not available.")
                return

            soup = BeautifulSoup(str(html), 'html.parser')
            topicrows = soup.find(id='topicrows')
            if not topicrows:
                logger.error("[ngaMonitor] topicrows not found.")
                return

            posts = topicrows.find_all('tbody')

            for post in posts:
                tds = post.find_all('td')

                if not tds:
                    continue

                is_bad = False
                post_count_raw = tds[0].get_text(" ", strip=True)
                m = re.search(r'(\d+)', str(post_count_raw or ''))
                post_count = int(m.group(1)) if m else 0
                post_head = tds[1].select_one('.topic') if len(tds) > 1 else None
                if not post_head:
                    continue
                post_link = post_head.get('href')
                post_link = urljoin('https://nga.178.com/', str(post_link or '').strip())
                post_name = post_head.get_text(" ", strip=True)
                post_date_ele = tds[2].select_one('.silver.postdate') if len(tds) > 2 else None
                post_date = post_date_ele.get_text(" ", strip=True) if post_date_ele else ""

                if not post_count or int(post_count) <= 20:
                    continue

                for black in self.black_list:
                    if black in str(post_name):
                        is_bad = True

                wa = WowArticle.objects.filter(url=post_link).first()

                if wa:
                    try:
                        cur = int(getattr(wa, "reply_count", 0) or 0)
                    except Exception:
                        cur = 0
                    if int(post_count or 0) > 0 and int(post_count) != cur:
                        wa.reply_count = int(post_count or 0)
                        wa.save(update_fields=["reply_count"])
                    continue

                if is_bad:
                    continue

                obj = WowArticle(
                    title=post_name,
                    url=post_link,
                    author="nga{}".format(title),
                    description="",
                    reply_count=int(post_count or 0),
                    source="nga",
                    category="nga",
                )
                obj.save()
                logger.info("[wow Monitor] Found new wow article.{}".format(post_name))

                self.task.flag = post_link
                self.task.save()

                self.post_desp = """NGA带逛<{}>，回帖数{}，发帖时间{}
《{}》
{}""".format(title, post_count, post_date, post_name, post_link)

                self.trigger_webhook()

        except AttributeError:
            logger.error("[ngaMonitor] No posts found.")

        except:
            raise

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        xi = xxxbotInterface()

        xi.send_msg(self.post_desp)
