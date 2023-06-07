#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: utils.py.py
@time: 2023/5/10 18:34
@desc:

'''

from botend.controller.BaseScan import BaseScan
from botend.controller.plugins.bili.BiliMonitor import BiliMonitor
from botend.controller.plugins.bili.BiliOnlionMonitor import BiliOnlionMonitor
from botend.controller.plugins.wechat.wechatMonitor import WechatMonitor
from botend.controller.plugins.wechat.wechatArticleScan import WechatArticleScan
from botend.controller.plugins.vuln.aliyunAvdMonitor import AliyunAvdMonitor
from botend.controller.plugins.vuln.aliyunAvdScan import AliyunAvdScan

Monitor_Type_BaseObject_List = [
    BaseScan,
    BiliMonitor,
    BiliOnlionMonitor,
    WechatMonitor,
    WechatArticleScan,
    AliyunAvdMonitor,
    AliyunAvdScan
]
