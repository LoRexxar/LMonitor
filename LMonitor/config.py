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
from botend.controller.plugins.info.wechatMonitor import WechatMonitor
from botend.controller.plugins.info.wechatArticleScan import WechatArticleScan
from botend.controller.plugins.vuln.aliyunAvdMonitor import AliyunAvdMonitor
from botend.controller.plugins.vuln.aliyunAvdScan import AliyunAvdScan
from botend.controller.plugins.vuln.oscsMonitor import OscsMonitor
from botend.controller.plugins.vuln.oscsScan import OscsScan
from botend.controller.plugins.vuln.qaxMonitor import QaxMonitor
from botend.controller.plugins.vuln.qaxScan import QaxScan
from botend.controller.plugins.vuln.seebugMonitor import SeebugMonitor
from botend.controller.plugins.info.RssMonitor import RssArticleMonitor
from botend.controller.plugins.wow.lhfszsMonitor import LhfszsMonitor

Monitor_Type_BaseObject_List = [
    BaseScan,
    BiliMonitor,
    BiliOnlionMonitor,
    WechatMonitor,
    WechatArticleScan,
    AliyunAvdMonitor,
    AliyunAvdScan,
    OscsMonitor,
    OscsScan,
    QaxMonitor,
    QaxScan,
    SeebugMonitor,
    RssArticleMonitor,
    LhfszsMonitor,
]
