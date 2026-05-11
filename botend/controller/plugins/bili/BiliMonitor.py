#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: BiliMonitor.py
@time: 2023/5/12 14:24
@desc:

'''


import time
import DrissionPage
from utils.log import logger
from botend.controller.BaseScan import BaseScan
from botend.models import WowArticle
from botend.interface.xxxbot import xxxbotInterface
from django.utils import timezone
from botend.alerting import upsert_system_alert


class BiliMonitor(BaseScan):
    """
    bili 视频更新监控
    """
    def __init__(self, req, task):
        super().__init__(req, task)

        self.video_desp = ""
        self.task = task

    def scan(self, url):
        """
        扫描
        :param url:
        :return:
        """
        cookies = ""
        driver = self.req.get(url, 'RespByChrome', 0, cookies, is_origin=1)

        # 处理返回内容
        self.resolve_data(driver)

        return True

    def resolve_data(self, driver):

        try:
            if not driver:
                return

            time.sleep(5)
            videos = driver.eles('.:fakeDanmu-item')

            for video in videos:
                video_time = video.ele('.time')
                if video_time:
                    video_dic = video.ele('.title')
                    video_link = video_dic.attr("href")
                    video_name = video_dic.text

                    # 检查视频是否更新
                    wa = WowArticle.objects.filter(url=video_link).first()

                    if wa:
                        continue

                    current_time = timezone.localtime(timezone.now())
                    formatted_time = current_time.strftime("%Y-%m-%d %H:%M")

                    obj = WowArticle(title=video_name, url=video_link, author="lorexxarbilibili",
                                     publish_time=formatted_time, description=video_name)
                    obj.save()
                    logger.info("[Bili Monitor] Found new Bilibili.{}".format(video_name))

                    self.video_desp = """你关注的up主更新视频啦！！
《{}》
{}
""".format(video_name, video_link)

                    self.trigger_webhook()

        except DrissionPage.errors.ElementNotFoundError:
            logger.error("[BiliMonitor] bad request.")
            upsert_system_alert(
                category='BILIBILI_SCRAPE_FAILED',
                subject='space.bilibili.com',
                level=2,
                title='B站页面解析失败',
                content='页面元素未找到（可能需要登录或页面结构已变更）'
            )

        except DrissionPage.errors.PageDisconnectedError:
            logger.error("[BiliMonitor] PageDisconnectedError.")
            upsert_system_alert(
                category='BILIBILI_SCRAPE_FAILED',
                subject='space.bilibili.com',
                level=2,
                title='B站页面解析失败',
                content='浏览器页面连接断开'
            )

        except DrissionPage.errors.ContextLostError:
            logger.error("[BiliMonitor] ContextLostError.")
            upsert_system_alert(
                category='BILIBILI_SCRAPE_FAILED',
                subject='space.bilibili.com',
                level=2,
                title='B站页面解析失败',
                content='页面刷新导致上下文丢失'
            )

        except TimeoutError:
            logger.error("[BiliMonitor] TimeoutError.")
            upsert_system_alert(
                category='BILIBILI_SCRAPE_FAILED',
                subject='space.bilibili.com',
                level=2,
                title='B站页面解析失败',
                content='页面脚本执行超时'
            )

        except AttributeError:
            logger.error("[BiliMonitor] Can't find videos.")
            upsert_system_alert(
                category='BILIBILI_SCRAPE_FAILED',
                subject='space.bilibili.com',
                level=2,
                title='B站页面解析失败',
                content='无法解析视频列表（页面结构可能已变更）'
            )
        except:
            raise

    def trigger_webhook(self):
        """
        触发企业微信推送
        :return:
        """
        xi = xxxbotInterface()

        xi.send_msg(self.video_desp)
