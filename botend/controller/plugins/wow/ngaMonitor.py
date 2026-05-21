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
import DrissionPage
from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.interface.xxxbot import xxxbotInterface

from botend.models import WowArticle


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
        cookies = ""

        for title in self.target_list:
            url = self.target_list[title]["url"]
            driver = self.req.get(url, 'RespByChrome', 0, cookies, is_origin=1)
            # 处理返回内容
            self.resolve_data(driver, title, self.target_list[title]["limit"])

        return True

    def resolve_data(self, driver, title="", limit=10):

        try:
            time.sleep(3)
            try:
                driver.run_js("g()")
            except DrissionPage.errors.JavaScriptError:
                pass
            except DrissionPage.errors.ContextLostError:
                logger.error("[ngaMonitor] page refresh. return back")
                return

            posts = driver.ele('#topicrows').eles('tag:tbody')

            for post in posts:
                tds = post.eles('tag:td')

                if not tds:
                    continue

                is_bad = False
                post_count_raw = tds[0].text
                m = re.search(r'(\d+)', str(post_count_raw or ''))
                post_count = int(m.group(1)) if m else 0
                post_head = tds[1].ele('.:topic')
                post_link = post_head.link
                post_name = post_head.texts()
                post_date = tds[2].ele('.silver postdate').text

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

        except DrissionPage.errors.ElementNotFoundError:
            logger.error("[ngaMonitor] bad request.")

        except DrissionPage.errors.PageDisconnectedError:
            logger.error("[ngaMonitor] PageDisconnectedError.")

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
