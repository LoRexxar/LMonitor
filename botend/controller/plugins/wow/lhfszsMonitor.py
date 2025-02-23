#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: lhfszsMonitor.py
@time: 2024/2/22 16:30
@desc:

'''

from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.interface.gewechat import GeWechatInterface

from botend.models import WowArticle


class LhfszsMonitor(BaseScan):
    """
    Lhfszs监控
    """
    def __init__(self, req, task):
        super().__init__(req, task)

        self.post_desp = ""
        self.post_img = ""
        self.task = task

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        cookies = ""
        url = "https://www.lhfszs.com/"
        driver = self.req.get(url, 'RespByChrome', 0, cookies, is_origin=1)

        # 处理返回内容
        self.resolve_data(driver)

        return True

    def resolve_data(self, driver):

        try:
            posts = driver.eles('.posts-item card ajax-item')

            for post in posts:
                post_title = post.ele('.item-thumbnail')

                # post_dic = post.ele('.post-excerpt').text
                post_name = post.ele('.item-heading').text
                post_link = post_title.ele('tag:a').link
                post_img = post_title.ele('tag:img').attr("data-src")

                # original_datetime = datetime.strptime(post_time.text, "%H:%M %Y/%m/%d")
                # django_date_time = original_datetime.strftime("%Y-%m-%d %H:%M")

                wa = WowArticle.objects.filter(url=post_link).first()

                if wa:
                    continue

                obj = WowArticle(title=post_name, url=post_link, author="lhfszs", description=post_img)
                obj.save()
                logger.info("[wow Monitor] Found new wow article.{}".format(post_name))

                self.task.flag = post_link
                self.task.save()

                self.post_desp = """检测到最新的魔兽世界新闻：
《{}》
{}""".format(post_name, post_link)
                self.post_img = post_img

                self.trigger_webhook()

        except AttributeError:
            logger.error("[wow Monitor] No posts found.")

        except:
            raise

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        aw = GeWechatInterface()
        aw.init()
        aw.publish_text(self.post_desp)