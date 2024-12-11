#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: difyAi.py
@time: 2023/5/19 18:54
@desc:

'''

import requests
import json

from utils.log import logger
from LMonitor.settings import DIFY_API, DIFY_API_SECRET


class difyAiWebhook:
    """
    dify消息
    """
    def __init__(self):
        self.secret = DIFY_API_SECRET
        self.api = DIFY_API

        self.s = requests.Session()

    def publish_text(self, text):
        url = "{}/completion-messages".format(self.api)

        headers = {
            "Authorization": "Bearer " + self.secret,
            "Content-Type": "application/json",
        }

        content = {
            "inputs": {
                "query": text,
            },
            "response_mode": "blocking",
            "user": "lorexxar",
        }

        result = self.s.post(url, headers=headers, json=content)
        r = result.json()
        logger.info("[difyAi] difyAi return {}".format(r["answer"]))

        return r["answer"]


if __name__ == "__main__":
    aw = difyAiWebhook()
    aw.publish_text("这是一句简单的测试内容")
