#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: hexagram.py
@time: 2023/7/25 17:34
@desc:

'''

from django.views import View
from django.http import HttpResponse, JsonResponse

import json
import random
import requests
from datetime import datetime

from botend.interface.hexagram import HexagramInterface

old_date = ""
now_user_list = []


class GetHexagramView(View):
    """
    算一卦吧，再别说了
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.hexagram = HexagramInterface()

    @staticmethod
    def get(request):
        return HttpResponse("online..")

    def post(self, request):
        message = self.hexagram.get_hexagram()
        mess = "此算卦与任何玄学无关，仅供娱乐:>,你的卦象如下：\n{}".format(message)

        params = json.loads(request.body)
        roomName = params['roomName']
        uname = params['uname']
        uid = params['uid']

        # if roomName != "英灵殿精英保安交流群":
        #     mess = "算卦功能暂时在本群关闭，下次一定开启~"
        #     return JsonResponse(
        #         {
        #             "code": 200,
        #             "msg": "success",
        #             "data": [
        #                 {
        #                     "type": 1,
        #                     "content": mess
        #                 }
        #             ]
        #         }
        #     )

        # add date check
        # now_user_list = []
        current_date = datetime.now().date()
        now_date = current_date.strftime('%Y-%m-%d')

        global old_date
        global now_user_list

        # 检查是不是星期4，如果不是则返回
        if current_date.weekday() != 3 and roomName:
            mess = "来一卦功能只在周四开放群聊，平日你可以私信机器人获取哦:>"

        else:
            if old_date == "":
                old_date = now_date
                now_user_list = [uid]
            elif old_date != now_date:
                old_date = now_date
                now_user_list = [uid]
            elif old_date == now_date:
                # 检查uname的存在性
                if uid in now_user_list:
                    mess = "你今天已经摇过签了，本签每日只能摇一次噢."
                else:
                    now_user_list.append(uid)

        return JsonResponse(
            {
                "code": 200,
                "msg": "success",
                "data": [
                    {
                        "type": 1,
                        "content": mess
                    }
                ]
            }
        )
