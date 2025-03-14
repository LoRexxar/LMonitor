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
import re
import xml.etree.ElementTree as ET

from utils.log import logger
from botend.interface.gewechat import GeWechatInterface
from botend.interface.hexagram import HexagramInterface
from botend.models import GeWechatTask


class GeWechatWebhookView(View):
    """
    处理GeWechat的回调消息
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.gewechat = GeWechatInterface()
        self.hexagram = HexagramInterface()
        self.special_task = [
            "#hexagram",
            "#addgroup"
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
            elif msgtype == 10002:
                # 处理进群消息
                return self._join_group_request(data)
            else:
                logger.warning(f"[GeWechatWebhook] 未知的消息类型: {msgtype}")
                return JsonResponse({"code": 200, "msg": "success"})

        except json.JSONDecodeError:
            logger.error(f"[GeWechatWebhook] JSON解析失败: {traceback.format_exc()}")
            return JsonResponse({"code": 400, "msg": "无效的JSON数据"})
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 处理消息失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"code": 500, "msg": "服务器内部错误"})

    def _handle_text_message(self, msg_data):
        """
        处理文本消息
        :param data: 消息数据
        :return: HTTP响应
        """
        try:
            content_type = 0
            resp_target = ""

            from_user = msg_data.get('FromUserName', {}).get('string')
            to_user = msg_data.get('ToUserName', {}).get('string')

            # 不处理自己的消息
            if from_user == "wxid_3cgi458isvk322":
                return JsonResponse({"code": 200, "msg": "success"})

            # 确定任务类型
            if "@chatroom" in from_user:
                # 群聊
                content_type = 2
                resp_target = from_user
            elif from_user == "guoyingqi0":
                # 管理员
                content_type = 0
                resp_target = from_user
            else:
                # 个人
                content_type = 1
                resp_target = from_user

            if content_type == 2:
                content = msg_data.get('Content', {}).get('string').split(':\n', 1)[1]
                logger.info(f"[GeWechatWebhook] 收到群聊消息: room={from_user}, content={content}")
            else:
                content = msg_data.get('Content', {}).get('string').strip()
                logger.info(f"[GeWechatWebhook] 收到私聊消息: from={to_user}, content={content}")


            # 获取所有活跃的任务
            active_tasks = GeWechatTask.objects.filter(msg_type=1, is_active=True)

            # 遍历所有活跃任务，尝试匹配消息内容
            for task in active_tasks:
                if task.active_type == 0 and content_type == 0:
                    self._send_back_messge(resp_target, content, task.content_regex, task.response)
                elif task.active_type == 1:
                    self._send_back_messge(resp_target, content, task.content_regex, task.response)
                elif task.active_type == 2 and (content_type == 1 or content_type == 0):
                    self._send_back_messge(resp_target, content, task.content_regex, task.response)
                elif task.active_type == 3 and content_type == 2:
                    self._send_back_messge(resp_target, content, task.content_regex, task.response)

            return JsonResponse({"code": 200, "msg": "success"})
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 处理文本消息失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"code": 500, "msg": "处理文本消息失败"})

    def _send_back_messge(self, target, content, regex, resp):
        try:
            if regex and re.search(regex, content):
                logger.info(f"[GeWechatWebhook] 匹配到任务: regex={regex}")
                
                # 随机延迟1秒内再回复
                time.sleep(random.random())

                retext = resp
                # 发送回复消息
                if retext in self.special_task:
                    if retext == "#hexagram":
                        retext = self.hexagram.get_hexagram_mess()
                    elif retext == "#addgroup":
                        room_id = "52012712485@chatroom"
                        self.gewechat.invite_member_to_chatroom(room_id,target)
                        retext = "拉群成功，如果失败，你遇到bug啦，请等待处理。"

                response = self.gewechat.send_text_message(target, retext)
                if response:
                    logger.info(f"[GeWechatWebhook] 发送回复成功: to={target}, response={response}")
                else:
                    logger.error(f"[GeWechatWebhook] 发送回复失败: to={target}, response={response}")
        except re.error as e:
            logger.error(f"[GeWechatWebhook] 正则表达式错误: regex={regex}")
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 处理失败: error={traceback.format_exc()}")

    def _handle_friend_request(self, msg_data):
        """
        处理好友请求消息
        :param data: 消息数据
        :return: HTTP响应
        """
        try:
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
                time.sleep(random.uniform(1,3))
                response = self.gewechat.add_contact(v3, v4, scene=scene)
                if response:
                    logger.info(f"[GeWechatWebhook] 自动通过好友请求成功: nickname={fromnickname}")
                    
                    time.sleep(random.uniform(50,60))
                    # 发送欢迎消息
                    response = self.gewechat.send_text_message(fromusername, active_task.response)
                else:
                    logger.error(f"[GeWechatWebhook] 自动通过好友请求失败: nickname={fromnickname}")
            else:
                logger.warning(f"[GeWechatWebhook] 缺少必要的好友请求参数: v3={v3}, v4={v4}")

            return JsonResponse({"code": 200, "msg": "success"})
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 处理好友请求失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"code": 500, "msg": "处理好友请求失败"})
            
    def _join_group_request(self, msg_data):
        """
        进群消息
        :param data: 消息数据
        :return: HTTP响应
        """
        try:
            room_id = msg_data.get('FromUserName', {}).get('string')
            content = msg_data.get('Content', {}).get('string')
            response = None

            try:

                # 去掉消息头部的群号
                content = content.split(':\n', 1)[1]
                # 解析XML
                root = ET.fromstring(content)
                # 获取template标签内容
                template = root.find('.//template').text
                # 获取成员信息
                inviter = root.find('.//link[@name="username"]//nickname').text
                invitee_id = root.find('.//link[@name="names"]//username').text
                invitee = root.find('.//link[@name="names"]//nickname').text
                # 替换模板中的变量
                message = template.replace('$username$', inviter).replace('$names$', invitee)
                logger.info(f"[GeWechatWebhook] 收到群消息: room_id={room_id}, message={message}")

                # 处理进群消息
                if "加入了群聊" in message:
                    at = GeWechatTask.objects.filter(msg_type=10002, content_regex="#joingroup", is_active=True).first()
                    if at:
                        time.sleep(random.uniform(1,3))
                        resp = f"@{invitee}，{at.response}"
                        response = self.gewechat.send_text_message(room_id, resp, ats=invitee_id)
                
            except Exception as e:
                logger.error(f"解析邀请消息失败: {str(e)}")
                return JsonResponse({"code": 500, "msg": "处理进群消息失败"})

            return JsonResponse({"code": 200, "msg": "success"})
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 处理进群消息失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"code": 500, "msg": "处理进群消息失败"})
            
    def get(self, request):
        """
        处理GET请求，检查登录状态或获取登录二维码
        :param request: HTTP请求对象
        :return: HTTP响应
        """
        try:
            # 检查登录状态
            self.gewechat.init()
            if self.gewechat.check_login():
                html_content = """
                <!DOCTYPE html>
                <html>
                <head>
                    <title>GeWechat 登录状态</title>
                    <meta charset="utf-8">
                    <style>
                        body { font-family: Arial, sans-serif; text-align: center; margin-top: 50px; }
                        .status { font-size: 24px; color: #4CAF50; margin: 20px; }
                    </style>
                </head>
                <body>
                    <div class="status">✅ 已成功登录</div>
                </body>
                </html>
                """
                return HttpResponse(html_content)
            
            # 未登录，获取登录二维码
            qr_data = self.gewechat.get_login_qrcode()
            if qr_data:
                html_content = f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <title>GeWechat 登录</title>
                    <meta charset="utf-8">
                    <style>
                        body {{ font-family: Arial, sans-serif; text-align: center; margin-top: 50px; }}
                        .qr-container {{ margin: 20px auto; }}
                        .title {{ font-size: 24px; color: #333; margin-bottom: 20px; }}
                        .subtitle {{ font-size: 16px; color: #666; margin-bottom: 30px; }}
                    </style>
                </head>
                <body>
                    <div class="title">GeWechat 登录</div>
                    <div class="subtitle">请使用微信扫描下方二维码登录</div>
                    <div class="qr-container">
                        <img src="{qr_data.get('qrImgBase64', '')}" alt="登录二维码">
                    </div>
                </body>
                </html>
                """
                return HttpResponse(html_content)
            
            html_content = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>GeWechat 登录失败</title>
                <meta charset="utf-8">
                <style>
                    body { font-family: Arial, sans-serif; text-align: center; margin-top: 50px; }
                    .error { font-size: 24px; color: #f44336; margin: 20px; }
                </style>
            </head>
            <body>
                <div class="error">获取登录二维码失败</div>
            </body>
            </html>
            """
            return HttpResponse(html_content)
            
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 处理GET请求失败: {str(e)}\n{traceback.format_exc()}")
            html_content = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>GeWechat 系统错误</title>
                <meta charset="utf-8">
                <style>
                    body { font-family: Arial, sans-serif; text-align: center; margin-top: 50px; }
                    .error { font-size: 24px; color: #f44336; margin: 20px; }
                </style>
            </head>
            <body>
                <div class="error">系统内部错误</div>
            </body>
            </html>
            """
            return HttpResponse(html_content)

