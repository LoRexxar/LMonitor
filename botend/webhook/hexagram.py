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
        mess = self.hexagram.get_hexagram_mess()
        

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
