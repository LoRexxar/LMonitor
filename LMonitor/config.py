#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: utils.py.py
@time: 2023/5/10 18:34
@desc:

'''

from Botend.controller.BaseScan import BaseScan
from Botend.controller.plugins.BiliMonitor import BiliMonitor
from Botend.controller.plugins.BiliOnlionMonitor import BiliOnlionMonitor

Monitor_Type_BaseObject_List = [
    BaseScan,
    BiliMonitor,
    BiliOnlionMonitor,
]
