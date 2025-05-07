#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: ngaMonitor.py
@time: 2024/2/22 16:30
@desc:

'''

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
            driver.run_js("g()")
            posts = driver.ele('#topicrows').eles('tag:tbody')

            for post in posts:
                tds = post.eles('tag:td')

                if not tds:
                    continue

                is_bad = False

                post_count = tds[0].text
                post_head = tds[1].ele('.:topic')
                post_link = post_head.link
                post_name = post_head.texts()
                post_date = tds[2].ele('.silver postdate').text

                # original_datetime = datetime.strptime(post_date, "%m-%d %H:%M")
                # django_date_time = original_datetime.strftime("%Y-%m-%d %H:%M")
                if not post_count or int(post_count) < limit:
                    continue

                for black in self.black_list:
                    if black in str(post_name):
                        is_bad = True

                wa = WowArticle.objects.filter(url=post_link).first()

                if wa:
                    continue

                if is_bad:
                    continue

                obj = WowArticle(title=post_name, url=post_link, author="nga", description="")
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