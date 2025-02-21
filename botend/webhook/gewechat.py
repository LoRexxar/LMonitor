#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: gewechat.py
@time: 2024/03/19
@desc: GeWechat Webhook Implementation
'''

from django.views import View
from django.http import HttpResponse, JsonResponse

import json
import time
import random
import traceback
import xml.etree.ElementTree as ET

from utils.log import logger
from botend.interface.gewechat import GeWechatInterface
from botend.models import GeWechatTask


class GeWechatWebhookView(View):
    """
    处理GeWechat的回调消息
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.gewechat = GeWechatWebhook()
        self.special_task = [
            "#hexagram"
        ]

    def post(self, request):
        """
        处理GeWechat的回调消息
        :param request: HTTP请求对象
        :return: HTTP响应
        """
        try:
            # 获取请求体中的JSON数据
            data = json.loads(request.body).get('Data', {})
            # 获取消息类型
            msgtype = data.get('MsgType')

            if msgtype == 1:
                # 处理文本消息
                return self._handle_text_message(data)
            elif msgtype == 37:
                # 处理好友请求
                return self._handle_friend_request(data)
            else:
                logger.warning(f"[GeWechatWebhook] 未知的消息类型: {msgtype}")
                return JsonResponse({"code": 200, "msg": "success"})

        except json.JSONDecodeError:
            logger.error(f"[GeWechatWebhook] JSON解析失败: {traceback.format_exc()}")
            return JsonResponse({"code": 400, "msg": "无效的JSON数据"})
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 处理消息失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"code": 500, "msg": "服务器内部错误"})

    def _handle_text_message(self, data):
        """
        处理文本消息
        :param data: 消息数据
        :return: HTTP响应
        """
        try:
            msg_data = data.get('Data', {})
            from_user = msg_data.get('FromUserName', {}).get('string')
            to_user = msg_data.get('ToUserName', {}).get('string')
            content = msg_data.get('Content', {}).get('string')

            logger.info(f"[GeWechatWebhook] 收到文本消息: from={from_user}, to={to_user}, content={content}")

            # 获取所有活跃的任务
            active_tasks = GeWechatTask.objects.filter(msg_type=1, is_active=True)

            # 遍历所有活跃任务，尝试匹配消息内容
            for task in active_tasks:
                try:
                    if task.content_regex and re.search(task.content_regex, content):
                        logger.info(f"[GeWechatWebhook] 消息匹配成功: task_id={task.id}, regex={task.content_regex}")
                        
                        # 随机延迟1秒内再回复
                        time.sleep(random.random())
                        # 发送回复消息
                        response = self.gewechat.send_text_message(from_user, task.response)
                        if response:
                            logger.info(f"[GeWechatWebhook] 发送回复成功: to={from_user}, response={task.response}")
                        else:
                            logger.error(f"[GeWechatWebhook] 发送回复失败: to={from_user}, response={task.response}")
                except re.error as e:
                    logger.error(f"[GeWechatWebhook] 正则表达式错误: task_id={task.id}, regex={task.content_regex}, error={str(e)}")
                except Exception as e:
                    logger.error(f"[GeWechatWebhook] 处理任务失败: task_id={task.id}, error={str(e)}")

            return JsonResponse({"code": 200, "msg": "success"})
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 处理文本消息失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"code": 500, "msg": "处理文本消息失败"})

    def _handle_friend_request(self, data):
        """
        处理好友请求消息
        :param data: 消息数据
        :return: HTTP响应
        """
        try:
            msg_data = data.get('Data', {})
            from_user = msg_data.get('FromUserName', {}).get('string')
            content = msg_data.get('Content', {}).get('string')

            # 解析XML格式的content字段
            root = ET.fromstring(content)
            
            # 获取好友请求信息
            fromusername = root.get('fromusername', '')
            fromnickname = root.get('fromnickname', '')
            content = root.get('content', '')
            v3 = root.get('encryptusername', '')
            v4 = root.get('ticket', '')
            scene = root.get('scene', '')

            logger.info(f"[GeWechatWebhook] 收到好友请求: from={fromusername}, nickname={fromnickname}, content={content}, scene={scene}")

            # 检查是否存在有效的自动添加好友任务
            active_task = GeWechatTask.objects.filter(msg_type=37, is_active=True).first()
            if not active_task:
                logger.info(f"[GeWechatWebhook] 没有找到有效的自动添加好友任务，所以不添加")
                return JsonResponse({"code": 200, "msg": "success"})

            # 自动通过好友请求
            if v3 and v4:
                # 随机延迟1秒内再回复
                time.sleep(random.random())
                response = self.gewechat.add_contact(v3, v4, scene=scene, content=active_task.response)
                if response:
                    logger.info(f"[GeWechatWebhook] 自动通过好友请求成功: nickname={fromnickname}")
                else:
                    logger.error(f"[GeWechatWebhook] 自动通过好友请求失败: nickname={fromnickname}")
            else:
                logger.warning(f"[GeWechatWebhook] 缺少必要的好友请求参数: v3={v3}, v4={v4}")

            return JsonResponse({"code": 200, "msg": "success"})
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 处理好友请求失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"code": 500, "msg": "处理好友请求失败"})

