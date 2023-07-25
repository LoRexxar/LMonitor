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


class GetHexagramView(View):
    """
    算一卦吧，再别说了
    """
    @staticmethod
    def get_hexagram():
        datalist = []
        hexalist = {
            "上上签-鸿运当头\n开天辟地作良缘　吉日良时万物全\n若得此签非小可　人行忠正帝王宣\nwoc，你无敌了，恭喜你本签概率为1/64，你就是气运之子，快去开低保吧。": 1,
            "上签-强运萦绕\n恭喜你本签概率为1/16,今日宜低保，宜大米。": 4,
            "中上签-运转时来\n啥也别说了，这中上签不是无敌了吗": 17,
            "中签-顺水顺风\n一锥草地要求泉　努力求之得最难\n无意俄然遇知己　相逢携手上青天\n比上不如，比下绰绰有余呀，今天一切都会顺利哒": 32,
            "中下签-苏秦不第\n鲸鱼未变守江河　不可升腾更望高\n异日峥嵘身变化　许君一跃跳龙门\n别慌别慌，此卦说明要时来运转了，忍忍就过去了": 7,
            "下签-苏娘走难\n奔波阻隔重重险　带水拖坭去度山\n更望他乡求用事　千乡万里未回还\n此卦说明今天不太顺利，凡事守旧会逢凶化吉": 2,
            "下下签-否极泰来\n兄弟，还说啥呢，好的马上就要来了呀": 1,
        }

        for hexa in hexalist:
            for _ in range(hexalist[hexa]):
                datalist.append(hexa)

        return random.choice(datalist)

    @staticmethod
    def get(request):
        return HttpResponse("online..")

    def post(self, request):
        message = self.get_hexagram()

        mess = """
此算卦与任何玄学无关，仅供娱乐:>,你的卦象如下：
{}
    """.format(message)

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