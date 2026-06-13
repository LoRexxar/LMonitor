#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: gewechat.py
@time: 2024/03/19
@desc: GeWechat Webhook Implementation
'''

import requests
import json
import traceback

from django.conf import settings as django_settings
from utils.log import logger


class xxxbotInterface:
    """
    xxxbot的推送实现
    """
    def __init__(self):
        self.config = getattr(django_settings, 'XXXBOT_CONFIG', {}) or {}
        self.base_url = self.config.get("base_url", "")
        self.active_roomlist = self.config.get("active_roomlist", [])
        self.wxid = self.config.get("wxid", "")
        self.s = requests.Session()

    def send_msg(self, content="", at_str=""):
        """
        发送消息给群
        :param msg:
        :return:
        """
        url = "{}/VXAPI/Msg/SendTxt".format(self.base_url)

        for room in self.active_roomlist:
            data = {
                "Wxid": self.wxid, 
                "ToWxid": room, 
                "Content": content, 
                "Type": 1, 
                "At": at_str
                }

            try:
                r = self.s.post(url, json=data)
                if r.status_code == 200:
                    logger.info("send msg to {} success".format(room))
                else:
                    logger.error("send msg to {} failed, status={}, body={}".format(room, r.status_code, r.text[:200]))
            except Exception as e:
                logger.error("send msg to {} failed: {}".format(room, e))

    def publish_admin(self, content="", at_str=""):
        url = "{}/VXAPI/Msg/SendTxt".format(self.base_url)
        data = {
                "Wxid": self.wxid, 
                "ToWxid": "guoyingqi0", 
                "Content": content, 
                "Type": 1, 
                "At": at_str
                }
        try:
            r = self.s.post(url, json=data)
            if r.status_code == 200:
                logger.info("send admin msg success")
            else:
                logger.error("send admin msg failed, status={}, body={}".format(r.status_code, r.text[:200]))
        except Exception as e:
            logger.error("send admin msg failed: {}".format(e))


if __name__ == "__main__":
    xi = xxxbotInterface()
    xi.send_msg("321321")
