#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: BaseScan.py
@time: 2023/5/10 18:48
@desc:

'''


from utils.LReq import LReq
from Botend.models import TargetAuth

from urllib.parse import urlparse


class BaseScan:
    def __init__(self, req, task_id):
        self.req = req
        self.task_id = task_id

    def scan(self, url):
        # check cookie
        domain = urlparse(url).netloc
        cookies = TargetAuth.objects.filter(domain=domain).first()

        result = self.req.get(url, 'RespByChrome', 0, cookies)

        if self.check_status(result):
            # 处理返回内容
            self.resolve_data(result)
            self.trigger_webhook()

        return True

    def check_status(self, result):
        # 检查请求之后的状态，根据状态处理
        return True

    def resolve_data(self, result):
        # 处理返回的内容
        return True

    def trigger_webhook(self):
        return True

    def stop(self):
        self.req.close_driver()
