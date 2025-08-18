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
from botend.dashboard.dashboard import DashboardView, SimcResultView, SimcAttributeAnalysisView
from botend.dashboard.api import ConvertTextAPIView, KeywordManagerAPIView, AplStorageAPIView, AplDetailAPIView, SimcTaskAPIView, SimcProfileAPIView, SimcTemplateAPIView, KeywordTranslationAPIView, OssConfigAPIView, SimcResultProxyAPIView, SimcAttributeAnalysisAPIView
from botend.dashboard.auth_views import LoginView, RegisterView, LogoutView, ChangePasswordView
from django.http import HttpResponse, JsonResponse

urlpatterns = [
    # path('admin/', admin.site.urls),
    path('', lambda request: redirect('/dashboard/'), name='home'),  # 根路径重定向到dashboard
    
    # 认证相关路由
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    path('auth/change-password/', ChangePasswordView.as_view(), name='change_password'),
    
    # Webhook路由
    path('webhook/gethexagram', csrf_exempt(GetHexagramView.as_view()), name="gethexagram"),
    path('webhook/gewechat', csrf_exempt(GeWechatWebhookView.as_view()), name="gewechat"),
    
    # Dashboard路由
    path('dashboard/', DashboardView.as_view(), name="dashboard"),
    
    # API路由
    path('api/convert-text/', csrf_exempt(ConvertTextAPIView.as_view()), name="convert_text"),
    path('api/keyword-manager/', csrf_exempt(KeywordManagerAPIView.as_view()), name="keyword_manager"),
    path('api/apl-storage/', csrf_exempt(AplStorageAPIView.as_view()), name="apl_storage"),
    path('api/apl-storage/<int:apl_id>/', csrf_exempt(AplDetailAPIView.as_view()), name="apl_detail"),
    path('api/simc-task/', csrf_exempt(SimcTaskAPIView.as_view()), name="simc_task"),
    path('api/simc-profile/', csrf_exempt(SimcProfileAPIView.as_view()), name="simc_profile"),
    path('api/simc-template/', csrf_exempt(SimcTemplateAPIView.as_view()), name="simc_template"),
    path('api/keyword-translation/', csrf_exempt(KeywordTranslationAPIView.as_view()), name="keyword_translation"),
    path('api/oss-config/', csrf_exempt(OssConfigAPIView.as_view()), name="oss_config"),
    path('api/simc-result-proxy/', csrf_exempt(SimcResultProxyAPIView.as_view()), name="simc_result_proxy"),
    path('api/simc-attribute-analysis/', csrf_exempt(SimcAttributeAnalysisAPIView.as_view()), name="simc_attribute_analysis"),
    
    # SimC结果查看页面
    path('simc-result/', SimcResultView.as_view(), name="simc_result"),
    path('simc-attribute-analysis/', SimcAttributeAnalysisView.as_view(), name="simc_attribute_analysis"),
]
