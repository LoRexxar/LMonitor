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

        driver = self.req.get(self.target_url, 'RespByChrome', 0, cookies, is_origin=1, is_proxy=True)
        # 处理返回内容
        self.resolve_data(driver, title, self.target_list[title]["limit"])

        return True

    def resolve_data(self, driver, title="", limit=10):

        try:
            time.sleep(3)

            posts = driver.els('#news-card-simple')

            for post in posts:
                post_type = post.ele('.news-card-simple-text').text
                post_title = post.ele('.news-card-simple-text-title').text
                post_link = post.ele('.news-card-simple-text-title').link
                post_preview = post.ele('.news-card-simple-text-preview').text
                post_date = post.ele('.news-card-simple-text-byline').ele('tag:span').attr('title')

                original_datetime = datetime.strptime(post_date, "%Y/%m/%d at %H:%M")
                django_date_time = original_datetime.strftime("%Y-%m-%d %H:%M")

                wa = WowArticle.objects.filter(url=post_link).first()

                if wa:
                    continue

                obj = WowArticle(title="[{}]{}".format(post_type, post_title), url=post_link, publish_time=django_date_time, author="wowhead", description=post_preview)
                obj.save()
                logger.info("[wowhead Monitor] Found new wowhead article.{}".format(post_name))

                self.task.flag = post_link
                self.task.save()

                self.post_desp = """WowHead新闻<{}>，发帖时间{}
[{}]《{}》
{}""".format(title, post_date, post_type, post_title, post_link)

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