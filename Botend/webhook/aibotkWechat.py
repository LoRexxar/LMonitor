#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: aibotkWechat.py
@time: 2023/5/19 18:54
@desc:

'''

import requests
import json

from LMonitor.settings import AIBOTK_WECHAT_SECRET, ACTIVE_WECHAT_GROUP


class AibotkWechatWebhook:
    """
    企业微信的推送
    """
    def __init__(self):
        self.secret = AIBOTK_WECHAT_SECRET
        self.active_wechat_group = ACTIVE_WECHAT_GROUP

        self.s = requests.Session()

    def publish_text(self, text):
        url = "https://api-bot.aibotk.com/openapi/v1/chat/room"

        for group_name in self.active_wechat_group:
            content = {
                "apiKey": self.secret["apikey"],
                "roomName": group_name,
                "message": {
                  "type": 1,
                  "content": text
                }
            }

            result = self.s.post(url, json=content)
            r = result.text

        return True


if __name__ == "__main__":
    aw = AibotkWechatWebhook()
    aw.publish_text("ttt")

