#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: auth_views.py
@time: 2024/12/19
@desc: Authentication Views for Dashboard
'''

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.views import View
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.conf import settings
import json


class LoginView(View):
    """
    用户登录视图
    """
    
    def get(self, request):
        """显示登录页面"""
        if request.user.is_authenticated:
            return redirect('/dashboard/')
        return render(request, 'dashboard/login.html')
    
    @method_decorator(csrf_exempt)
    def post(self, request):
        """处理登录请求"""
        try:
            data = json.loads(request.body)
            username = data.get('username')
            password = data.get('password')
            
            if not username or not password:
                return JsonResponse({
                    'status': 'error',
                    'message': '用户名和密码不能为空'
                })
            
            user = authenticate(request, username=username, password=password)
            if user is not None:
                login(request, user)
                return JsonResponse({
                    'status': 'success',
                    'message': '登录成功',
                    'redirect_url': '/dashboard/'
                })
            else:
                return JsonResponse({
                    'status': 'error',
                    'message': '用户名或密码错误'
                })
                
        except json.JSONDecodeError:
            return JsonResponse({
                'status': 'error',
                'message': '请求数据格式错误'
            })
        except Exception as e:
            return JsonResponse({
                'status': 'error',
                'message': f'登录失败: {str(e)}'
            })


class RegisterView(View):
    """
    用户注册视图
    """
    
    def get(self, request):
        """显示注册页面"""
        if request.user.is_authenticated:
            return redirect('/dashboard/')
        
        # 检查是否允许注册
        if not getattr(settings, 'ALLOW_REGISTRATION', True):
            return render(request, 'dashboard/login.html', {
                'error_message': '注册功能已关闭，请联系管理员'
            })
        
        return render(request, 'dashboard/register.html')
    
    @method_decorator(csrf_exempt)
    def post(self, request):
        """处理注册请求"""
        # 检查是否允许注册
        if not getattr(settings, 'ALLOW_REGISTRATION', True):
            return JsonResponse({
                'status': 'error',
                'message': '注册功能已关闭，请联系管理员'
            })
        
        try:
            data = json.loads(request.body)
            username = data.get('username')
            email = data.get('email')
            password = data.get('password')
            confirm_password = data.get('confirm_password')
            
            # 验证必填字段
            if not all([username, email, password, confirm_password]):
                return JsonResponse({
                    'status': 'error',
                    'message': '所有字段都是必填的'
                })
            
            # 验证密码确认
            if password != confirm_password:
                return JsonResponse({
                    'status': 'error',
                    'message': '两次输入的密码不一致'
                })
            
            # 验证密码长度
            if len(password) < 6:
                return JsonResponse({
                    'status': 'error',
                    'message': '密码长度至少6位'
                })
            
            # 验证邮箱格式
            try:
                validate_email(email)
            except ValidationError:
                return JsonResponse({
                    'status': 'error',
                    'message': '邮箱格式不正确'
                })
            
            # 检查用户名是否已存在
            if User.objects.filter(username=username).exists():
                return JsonResponse({
                    'status': 'error',
                    'message': '用户名已存在'
                })
            
            # 检查邮箱是否已存在
            if User.objects.filter(email=email).exists():
                return JsonResponse({
                    'status': 'error',
                    'message': '邮箱已被注册'
                })
            
            # 创建用户
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password
            )
            
            # 自动登录
            login(request, user)
            
            return JsonResponse({
                'status': 'success',
                'message': '注册成功',
                'redirect_url': '/dashboard/'
            })
            
        except json.JSONDecodeError:
            return JsonResponse({
                'status': 'error',
                'message': '请求数据格式错误'
            })
        except Exception as e:
            return JsonResponse({
                'status': 'error',
                'message': f'注册失败: {str(e)}'
            })


class LogoutView(View):
    """
    用户登出视图
    """
    
    @method_decorator(login_required)
    def post(self, request):
        """处理登出请求"""
        logout(request)
        return JsonResponse({
            'status': 'success',
            'message': '已成功登出',
            'redirect_url': '/auth/login/'
        })


class ChangePasswordView(View):
    """
    修改密码视图
    """
    
    @method_decorator(login_required)
    def get(self, request):
        """显示修改密码页面"""
        return render(request, 'dashboard/change_password.html')
    
    @method_decorator([csrf_exempt, login_required])
    def post(self, request):
        """处理修改密码请求"""
        try:
            data = json.loads(request.body)
            current_password = data.get('current_password')
            new_password = data.get('new_password')
            confirm_password = data.get('confirm_password')
            
            # 验证必填字段
            if not all([current_password, new_password, confirm_password]):
                return JsonResponse({
                    'status': 'error',
                    'message': '所有字段都是必填的'
                })
            
            # 验证当前密码
            if not request.user.check_password(current_password):
                return JsonResponse({
                    'status': 'error',
                    'message': '当前密码不正确'
                })
            
            # 验证新密码确认
            if new_password != confirm_password:
                return JsonResponse({
                    'status': 'error',
                    'message': '两次输入的新密码不一致'
                })
            
            # 验证新密码长度
            if len(new_password) < 6:
                return JsonResponse({
                    'status': 'error',
                    'message': '新密码长度至少6位'
                })
            
            # 检查新密码是否与当前密码相同
            if request.user.check_password(new_password):
                return JsonResponse({
                    'status': 'error',
                    'message': '新密码不能与当前密码相同'
                })
            
            # 修改密码
            request.user.set_password(new_password)
            request.user.save()
            
            # 重新登录用户（因为密码改变会使session失效）
            login(request, request.user)
            
            return JsonResponse({
                'status': 'success',
                'message': '密码修改成功'
            })
            
        except json.JSONDecodeError:
            return JsonResponse({
                'status': 'error',
                'message': '请求数据格式错误'
            })
        except Exception as e:
            return JsonResponse({
                'status': 'error',
                'message': f'密码修改失败: {str(e)}'
            })