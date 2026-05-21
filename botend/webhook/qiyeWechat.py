#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: qiyeWechat.py
@time: 2023/5/12 18:12
@desc:

'''

import requests
import json

from LMonitor.settings import QIYE_WECHAT_SECRET_LIST


class QiyeWechatWebhook:
    """
    企业微信的推送
    """
    def __init__(self):
        self.secret = QIYE_WECHAT_SECRET_LIST[0]
        self.access = {
        }

        self.s = requests.Session()
        self.get_accesstoken()

    def get_accesstoken(self):
        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        result = self.s.get(url, params=self.secret)
        r = result.json()
        self.access["access_token"] = r["access_token"]

    def publish_text(self, text):
        url = "https://qyapi.weixin.qq.com/cgi-bin/externalcontact/add_msg_template"
        content = {
            "chat_type": "group",
            "sender": "GuoYinQi",
            "allow_select": True,
            "text": {
                "content": text
            },
            "attachments": [
            ]
        }

        result = self.s.post(url, params=self.access, json=content)
        r = result.text

        return True


if __name__ == "__main__":
    qw = QiyeWechatWebhook()
    qw.publish_text("ttt")

