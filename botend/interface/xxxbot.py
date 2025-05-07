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

from LMonitor.settings import XXXBOT_CONFIG
from utils.log import logger


class xxxbotInterface:
    """
    xxxbot的推送实现
    """
    def __init__(self):
        self.config = XXXBOT_CONFIG
        self.base_url = self.config["base_url"]
        self.active_roomlist = self.config["active_roomlist"]
        self.wxid = self.config["wxid"]
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
                print(r.text)
                if r.status_code == 200:
                    logger.info("send msg to {} success".format(room))
                else:
                    logger.error("send msg to {} failed".format(room))
            except Exception as e:
                logger.error("send msg to {} failed".format(room))

if __name__ == "__main__":
    xi = xxxbotInterface()
    xi.send_msg("321321")
