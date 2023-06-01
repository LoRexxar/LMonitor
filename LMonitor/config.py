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
from botend.controller.plugins.BiliMonitor import BiliMonitor
from botend.controller.plugins.BiliOnlionMonitor import BiliOnlionMonitor
from botend.controller.plugins.wechatMonitor import WechatMonitor
from botend.controller.plugins.wechatArticleScan import WechatArticleScan

Monitor_Type_BaseObject_List = [
    BaseScan,
    BiliMonitor,
    BiliOnlionMonitor,
    WechatMonitor,
    WechatArticleScan,
]
