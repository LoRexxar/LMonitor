"""LMonitor URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/3.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import redirect

from botend.webhook.hexagram import GetHexagramView
from botend.webhook.gewechat import GeWechatWebhookView
from botend.dashboard.dashboard import DashboardView
from botend.dashboard.api import ConvertTextAPIView, KeywordManagerAPIView
from botend.dashboard.auth_views import LoginView, RegisterView, LogoutView
from django.http import HttpResponse, JsonResponse

urlpatterns = [
    # path('admin/', admin.site.urls),
    path('', lambda request: redirect('/dashboard/'), name='home'),  # 根路径重定向到dashboard
    
    # 认证相关路由
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    
    # Webhook路由
    path('webhook/gethexagram', csrf_exempt(GetHexagramView.as_view()), name="gethexagram"),
    path('webhook/gewechat', csrf_exempt(GeWechatWebhookView.as_view()), name="gewechat"),
    
    # Dashboard路由
    path('dashboard/', DashboardView.as_view(), name="dashboard"),
    
    # API路由
    path('api/convert-text/', csrf_exempt(ConvertTextAPIView.as_view()), name="convert_text"),
    path('api/keyword-manager/', csrf_exempt(KeywordManagerAPIView.as_view()), name="keyword_manager"),
]
