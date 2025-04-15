#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: gewechat.py
@time: 2024/03/19
@desc: GeWechat Webhook Implementation
'''

import requests
import json
import traceback

from LMonitor.settings import GEWECHAT_CONFIG
from utils.log import logger
from botend.models import GeWechatAuth, GeWechatRoomList


class GeWechatInterface:
    """
    GeWechat的推送实现
    """
    def __init__(self):
        self.config = GEWECHAT_CONFIG
        self.callback_url = self.config['callback_url']
        self.access_token = None
        self.appId = ""
        self.is_login = False

        self.s = requests.Session()
        self.get_access_token()

        self.auth = GeWechatAuth.objects.filter(is_active=True).first()
        if self.auth:
            self.appId = self.auth.appId

        self.room_list = []
        gwrs = GeWechatRoomList.objects.all()
        for gwr in gwrs:
            self.room_list.append(gwr.room_id)

    def init(self):
        """
        初始化GeWechat接口
        """
        self.get_access_token()
        if self.check_login():
            self.set_callback_url(self.callback_url)
            self.update_chatrooms_list()
        else:
            self.get_login_qrcode()

    def get_access_token(self):
        """
        获取GeWechat的access token
        """
        url = f"{self.config['base_url']}/tools/getTokenId"
        data = {
        }
        try:
            result = self.s.post(url, json=data)
            response = result.json()
            if response.get('ret') == 200:
                self.access_token = response['data']
                return True
            logger.error(f"[GeWechatWebhook] 获取access token失败: {response.get('msg', '未知错误')}")
            return False
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 获取access token失败: {str(e)}\n{traceback.format_exc()}")
            return False

    def get_login_qrcode(self):
        """
        获取登录二维码
        :return: 成功返回二维码数据字典，失败返回None
        """
        if not self.access_token:
            if not self.get_access_token():
                return None

        url = f"{self.config['base_url']}/login/getLoginQrCode"
        headers = {"X-GEWE-TOKEN": self.access_token}
        data = {
            "appId": self.appId
        }

        try:
            result = self.s.post(url, headers=headers, json=data)
            response = result.json()
            if response.get('ret') == 200:
                qr_data = response['data']
                # 更新二维码数据到数据库
                auth = GeWechatAuth.objects.filter(is_active=True).first()
                if not auth:
                    auth = GeWechatAuth(appId=qr_data.get('appId'))
                auth.uuid = qr_data.get('uuid')
                auth.qrImgBase64 = qr_data.get('qrImgBase64')
                auth.login_status = 1
                auth.is_active = True
                auth.appId = qr_data.get('appId')
                self.appId = qr_data.get('appId')
                auth.save()
                return qr_data
            logger.error(f"[GeWechatWebhook] 获取登录二维码失败: {response.get('msg', '未知错误')}")
            return None
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 获取登录二维码失败: {str(e)}\n{traceback.format_exc()}")
            return None
            
    def check_login(self):
        """
        检查登录状态
        :return: 成功返回True，失败返回False
        """
        if not self.access_token:
            if not self.get_access_token():
                return False

        auth = GeWechatAuth.objects.filter(is_active=True).first()
        if not auth:
            logger.error("[GeWechatWebhook] 没有找到活跃的GeWechat认证信息，初始化登录.")
            self.get_login_qrcode()
            return False

        url = f"{self.config['base_url']}/login/checkOnline"
        headers = {"X-GEWE-TOKEN": self.access_token}
        data = {
            "appId": auth.appId,
        }

        try:
            # 先检查扫码状态
            if auth.login_status == 1:
                # 先检查是否扫码成功
                url2 = f"{self.config['base_url']}/login/checkLogin"
                data2 = {
                    "appId": auth.appId,
                    "uuid": auth.uuid,
                }
                result = self.s.post(url2, headers=headers, json=data2)
                response = result.json()
                if response.get('ret') == 200:
                    logger.info(f"[GeWechatWebhook] 二维码扫描成功，检查登录状态.")

            result = self.s.post(url, headers=headers, json=data)
            response = result.json()
            if response.get('ret') == 200:
                print(response)
                is_login = response['data']
                if is_login:
                    auth.login_status = 2
                    self.is_login = True
                    auth.save()
                    logger.info(f"[GeWechatWebhook] 已登录")
                    return True
                else:
                    auth.login_status = 0
                    self.is_login = False
                    auth.save()
                    self.get_login_qrcode()
                    logger.warning(f"[GeWechatWebhook] 未登录")
                    return False
            logger.error(f"[GeWechatWebhook] 检查登录状态失败: {response.get('msg', '未知错误')}")
            return False
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 检查登录状态失败: {str(e)}\n{traceback.format_exc()}")
            return False

    def set_callback_url(self, callback_url):
        """
        设置消息回调地址
        :param callback_url: 回调地址
        :return: 成功返回True，失败返回False
        """
        if not self.access_token:
            if not self.get_access_token():
                return False
    
        url = f"{self.config['base_url']}/tools/setCallback"
        headers = {"X-GEWE-TOKEN": self.access_token}
        data = {
            "token": self.access_token,
            "callbackUrl": self.callback_url
        }
    
        try:
            result = self.s.post(url, headers=headers, json=data)
            response = result.json()
            if response.get('ret') == 200:
                logger.info(f"[GeWechatWebhook] 回调地址{self.callback_url}设置成功")
                return True
            logger.error(f"[GeWechatWebhook] 设置回调地址失败: {response.get('msg', '未知错误')}")
            return False
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 设置回调地址失败: {str(e)}\n{traceback.format_exc()}")
            return False
    
    def update_chatrooms_list(self):
        """
        获取群列表
        :return: 成功返回群列表，失败返回None
        """
        if not self.access_token:
            if not self.get_access_token():
                return None

        url = f"{self.config['base_url']}/contacts/fetchContactsList"
        headers = {
            "X-GEWE-TOKEN": self.access_token,
            "Content-Type": "application/json"
        }
        data = {
            "appId": self.appId
        }

        try:
            result = self.s.post(url, headers=headers, json=data)
            response = result.json()
            if response.get('ret') == 200:
                data = response['data']
                roomlist = data.get('chatrooms')
                if roomlist:
                    for room_id in roomlist:
                        if room_id not in self.room_list:
                            wr = GeWechatRoomList(room_id=room_id)
                            logger.info(f"[GeWechatWebhook] New 群ID: {room_id}")

                            # 获取群聊的基本信息
                            url2 = f"{self.config['base_url']}/group/getChatroomInfo"
                            data2 = {
                                "appId": self.appId,
                                "chatroomId": room_id
                            }
                            result2 = self.s.post(url2, headers=headers, json=data2)
                            response2 = result2.json()
                            if response2.get('ret') == 200:
                                data2 = response2['data']
                                wr.room_name = data2.get('nickName')
                                wr.room_member_count = len(data2.get('memberList'))

                            wr.save()

                return True
            logger.error(f"[GeWechatWebhook] 获取联系人列表失败: {response.get('msg', '未知错误')}")
            return None
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 获取联系人列表失败: {str(e)}\n{traceback.format_exc()}")
            return None

    def invite_member_to_chatroom(self, chatroom_id, wxid):
        """
        邀请成员加入群聊
        :param chatroom_id: 群聊ID
        :param wxid: 待邀请成员的wxid
        :return: 成功返回True，失败返回False    
        """
        if not self.access_token:
            if not self.get_access_token():
                return False

        url = f"{self.config['base_url']}/group/inviteMember"
        headers = {
            "X-GEWE-TOKEN": self.access_token,
            "Content-Type": "application/json"
        }
        data = {
            "appId": self.appId,
            "wxids": wxid,
            "chatroomId": chatroom_id,
            "reason": "",
        }

        try:
            result = self.s.post(url, headers=headers, json=data)
            response = result.json()
            if response.get('ret') == 200:
                logger.info(f"[GeWechatWebhook] 邀请成员加入群聊成功，群ID: {chatroom_id}, wxid: {wxid}")
                return True
            logger.error(f"[GeWechatWebhook] 邀请成员加入群聊失败: {response.get('msg', '未知错误')}")
            return False
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 邀请成员加入群聊失败: {str(e)}\n{traceback.format_exc()}")
            return False

        

    def send_text_message(self, to_wxid, content, ats=None):
        """
        发送文本消息
        :param to_wxid: 接收者的wxid或群聊ID
        :param content: 消息内容
        :param ats: 需要@的用户wxid列表，仅在群聊中有效
        :return: 成功返回True，失败返回False
        """
        if not self.access_token:
            if not self.get_access_token():
                return False

        url = f"{self.config['base_url']}/message/postText"
        headers = {
            "X-GEWE-TOKEN": self.access_token,
            "Content-Type": "application/json"
        }
        data = {
            "appId": self.appId,
            "toWxid": to_wxid,
            "content": content
        }

        # 如果有@用户，添加到请求数据中
        if ats:
            data["ats"] = ats

        try:
            result = self.s.post(url, headers=headers, json=data)
            response = result.json()
            if response.get('ret') == 200:
                logger.info(f"[GeWechatWebhook] 发送消息成功，接收者: {to_wxid}")
                return True
            logger.error(f"[GeWechatWebhook] 发送消息失败: {response.get('msg', '未知错误')}")
            return False
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 发送消息失败: {str(e)}\n{traceback.format_exc()}")
            return False

    def send_link_message(self, to_wxid, title, desc, link_url, thumb_url):
        """
        发送链接消息
        :param to_wxid: 接收者的wxid或群聊ID
        :param title: 链接标题
        :param desc: 链接描述
        :param link_url: 链接URL
        :param thumb_url: 缩略图URL
        :return: 成功返回True，失败返回False
        """
        if not self.access_token:
            if not self.get_access_token():
                return False

        url = f"{self.config['base_url']}/message/postLink"
        headers = {
            "X-GEWE-TOKEN": self.access_token,
            "Content-Type": "application/json"
        }
        data = {
            "appId": self.appId,
            "toWxid": to_wxid,
            "title": title,
            "desc": desc,
            "linkUrl": link_url,
            "thumbUrl": thumb_url
        }

        try:
            result = self.s.post(url, headers=headers, json=data)
            response = result.json()
            if response.get('ret') == 200:
                logger.info(f"[GeWechatWebhook] 发送链接消息成功，接收者: {to_wxid}")
                return True
            logger.error(f"[GeWechatWebhook] 发送链接消息失败: {response.get('msg', '未知错误')}")
            return False
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 发送链接消息失败: {str(e)}\n{traceback.format_exc()}")
            return False

    def add_contact(self, v3, v4, scene=3, content=""):
        """
        添加联系人
        :param v3: v3参数
        :param v4: v4参数
        :param scene: 场景值，默认为3
        :param content: 验证消息内容
        :return: 成功返回True，失败返回False
        """
        if not self.access_token:
            if not self.get_access_token():
                return False

        url = f"{self.config['base_url']}/contacts/addContacts"
        headers = {
            "X-GEWE-TOKEN": self.access_token,
            "Content-Type": "application/json"
        }
        data = {
            "appId": self.appId,
            "scene": scene,
            "content": content,
            "v4": v4,
            "v3": v3,
            "option": 2
        }

        try:
            result = self.s.post(url, headers=headers, json=data)
            response = result.json()
            if response.get('ret') == 200:
                logger.info(f"[GeWechatWebhook] 添加联系人成功")
                return True
            logger.error(f"[GeWechatWebhook] 添加联系人失败: {response.get('msg', '未知错误')}")
            return False
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 添加联系人失败: {str(e)}\n{traceback.format_exc()}")
            return False

    def publish_text(self, content):
        """
        发布群消息
        :param content: 消息内容
        :return: 成功返回True，失败返回False
        """
        if not self.access_token:
            if not self.get_access_token():
                return False

        if not self.check_login():
            logger.error("[GeWechatWebhook] 未登录")
            return False

        url = f"{self.config['base_url']}/message/postText"
        headers = {
            "X-GEWE-TOKEN": self.access_token,
            "Content-Type": "application/json"
        }

        wx_list = GeWechatRoomList.objects.filter(is_active=True)

        for wx in wx_list:
            try:
                data = {
                    "appId": self.appId,
                    "toWxid": wx.room_id,
                    "content": content,
                    "ats": "",
                }

                result = self.s.post(url, headers=headers, json=data)
                response = result.json()
                if response.get('ret') == 200:
                    logger.info(f"[GeWechatWebhook] 发布消息到{wx.room_name}成功")
                logger.error(f"[GeWechatWebhook] 发布消息失败: {response.get('msg', '未知错误')}")
            except Exception as e:
                logger.error(f"[GeWechatWebhook] 发布消息失败: {str(e)}\n{traceback.format_exc()}")
        
    def publish_admin(self, content):
        """
        发布群消息
        :param content: 消息内容
        :return: 成功返回True，失败返回False
        """
        if not self.access_token:
            if not self.get_access_token():
                return False

        if not self.check_login():
            logger.error("[GeWechatWebhook] 未登录")
            return False

        url = f"{self.config['base_url']}/message/postText"
        headers = {
            "X-GEWE-TOKEN": self.access_token,
            "Content-Type": "application/json"
        }

        try:
            data = {
                "appId": self.appId,
                "toWxid": "guoyingqi0",
                "content": content,
                "ats": "",
            }

            result = self.s.post(url, headers=headers, json=data)
            response = result.json()
            if response.get('ret') == 200:
                return True
            logger.error(f"[GeWechatWebhook] 发布消息失败: {response.get('msg', '未知错误')}")
            return False
        except Exception as e:
            logger.error(f"[GeWechatWebhook] 发布消息失败: {str(e)}\n{traceback.format_exc()}")
            return False
        


if __name__ == "__main__":
    # 测试代码
    gw = GeWechatInterface()
    gw.init()