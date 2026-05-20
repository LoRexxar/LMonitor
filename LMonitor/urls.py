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
from django.views.generic.base import RedirectView

from botend.webhook.hexagram import GetHexagramView
from botend.webhook.gewechat import GeWechatWebhookView
from botend.dashboard.dashboard import DashboardView, SimcResultView, SimcAttributeAnalysisView, SimcRegularCompareView, SimcAttributeAnalysisSSRView, WclAnalysisPageView, WclAnalysisReportView
from botend.dashboard.api import ConvertTextAPIView, KeywordManagerAPIView, AplStorageAPIView, AplDetailAPIView, SimcTaskAPIView, SimcProfileAPIView, SimcTemplateAPIView, SimcAplCandidatesAPIView, KeywordTranslationAPIView, OssConfigAPIView, SimcResultProxyAPIView, SimcAttributeAnalysisAPIView, SimcRegularCompareAPIView, SimcBackendBinaryAPIView, WclAnalysisTaskAPIView, SystemAlertAPIView, PortalPeakSpecRankRefreshAPIView, WowDailyReportListAPIView, WowDailyReportContentAPIView, WowDailyReportDownloadAPIView
from botend.dashboard.auth_views import LoginView, RegisterView, LogoutView, ChangePasswordView
from botend.portal.views import PortalHomeView
from botend.portal.views import PortalWowSkillDiffReportView
from botend.portal.api import (
    PortalBluepostsAPIView,
    PortalNgaHotAPIView,
    PortalExwindLatestAPIView,
    PortalEventsAPIView,
    PortalVideosAPIView,
    PortalToolsAPIView,
    PortalMplusAffixesAPIView,
    PortalMplusCutoffAPIView,
    PortalMplusRankingsAPIView,
    PortalPeakSpecRankingsAPIView,
    PortalRaidRankingsAPIView,
    PortalCharacterAPIView,
    PortalMythicstatsDpsAPIView,
    PortalWowSkillDiffListAPIView,
    PortalWowSkillDiffStatesAPIView,
)
from django.http import HttpResponse, JsonResponse

urlpatterns = [
    # path('admin/', admin.site.urls),
    path('favicon.ico', RedirectView.as_view(url='/static/portal/favicons/3accfdf0352f2189a3292605e1ad80f12bd5a15c605069102f42c03c3c4fceda.ico', permanent=True)),
    path('', PortalHomeView.as_view(), name='portal_home'),
    
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

    # Portal API
    path('portal/api/blueposts/', csrf_exempt(PortalBluepostsAPIView.as_view()), name="portal_blueposts"),
    path('portal/api/nga-hot/', csrf_exempt(PortalNgaHotAPIView.as_view()), name="portal_nga_hot"),
    path('portal/api/exwind/latest/', csrf_exempt(PortalExwindLatestAPIView.as_view()), name="portal_exwind_latest"),
    path('portal/api/events/', csrf_exempt(PortalEventsAPIView.as_view()), name="portal_events"),
    path('portal/api/videos/', csrf_exempt(PortalVideosAPIView.as_view()), name="portal_videos"),
    path('portal/api/tools/', csrf_exempt(PortalToolsAPIView.as_view()), name="portal_tools"),
    path('portal/api/mplus/affixes/', csrf_exempt(PortalMplusAffixesAPIView.as_view()), name="portal_mplus_affixes"),
    path('portal/api/mplus/cutoff/', csrf_exempt(PortalMplusCutoffAPIView.as_view()), name="portal_mplus_cutoff"),
    path('portal/api/mplus/rankings/', csrf_exempt(PortalMplusRankingsAPIView.as_view()), name="portal_mplus_rankings"),
    path('portal/api/peak/spec-rankings/', csrf_exempt(PortalPeakSpecRankingsAPIView.as_view()), name="portal_peak_spec_rankings"),
    path('portal/api/raid/rankings/', csrf_exempt(PortalRaidRankingsAPIView.as_view()), name="portal_raid_rankings"),
    path('portal/api/character/', csrf_exempt(PortalCharacterAPIView.as_view()), name="portal_character"),
    path('portal/api/mythicstats/dps/', csrf_exempt(PortalMythicstatsDpsAPIView.as_view()), name="portal_mythicstats_dps"),
    path('portal/api/wow-skill-diffs/', csrf_exempt(PortalWowSkillDiffListAPIView.as_view()), name="portal_wow_skill_diffs"),
    path('portal/api/wow-skill-diff/states/', csrf_exempt(PortalWowSkillDiffStatesAPIView.as_view()), name="portal_wow_skill_diff_states"),
    path('portal/wow-skill-diff/<int:report_id>/', PortalWowSkillDiffReportView.as_view(), name="portal_wow_skill_diff_report"),
    
    # API路由
    path('api/convert-text/', csrf_exempt(ConvertTextAPIView.as_view()), name="convert_text"),
    path('api/keyword-manager/', csrf_exempt(KeywordManagerAPIView.as_view()), name="keyword_manager"),
    path('api/apl-storage/', csrf_exempt(AplStorageAPIView.as_view()), name="apl_storage"),
    path('api/apl-storage/<int:apl_id>/', csrf_exempt(AplDetailAPIView.as_view()), name="apl_detail"),
    path('api/simc-task/', csrf_exempt(SimcTaskAPIView.as_view()), name="simc_task"),
    path('api/simc-profile/', csrf_exempt(SimcProfileAPIView.as_view()), name="simc_profile"),
    path('api/simc-profile/<int:profile_id>/', csrf_exempt(SimcProfileAPIView.as_view()), name="simc_profile_detail"),
    path('api/simc-apl-candidates/', csrf_exempt(SimcAplCandidatesAPIView.as_view()), name="simc_apl_candidates"),
    path('api/simc-template/', csrf_exempt(SimcTemplateAPIView.as_view()), name="simc_template"),
    path('api/simc-backend-binary/', csrf_exempt(SimcBackendBinaryAPIView.as_view()), name="simc_backend_binary"),
    path('api/system-alert/', csrf_exempt(SystemAlertAPIView.as_view()), name="system_alert"),
    path('api/portal/peak/refresh/', csrf_exempt(PortalPeakSpecRankRefreshAPIView.as_view()), name="portal_peak_refresh"),
    path('api/wow-daily-report/list/', csrf_exempt(WowDailyReportListAPIView.as_view()), name="wow_daily_report_list"),
    path('api/wow-daily-report/content/', csrf_exempt(WowDailyReportContentAPIView.as_view()), name="wow_daily_report_content"),
    path('api/wow-daily-report/download/', csrf_exempt(WowDailyReportDownloadAPIView.as_view()), name="wow_daily_report_download"),
    path('api/keyword-translation/', csrf_exempt(KeywordTranslationAPIView.as_view()), name="keyword_translation"),
    path('api/oss-config/', csrf_exempt(OssConfigAPIView.as_view()), name="oss_config"),
    path('api/simc-result-proxy/', csrf_exempt(SimcResultProxyAPIView.as_view()), name="simc_result_proxy"),
    path('api/simc-attribute-analysis/', csrf_exempt(SimcAttributeAnalysisAPIView.as_view()), name="simc_attribute_analysis"),
    path('api/simc-regular-compare/', csrf_exempt(SimcRegularCompareAPIView.as_view()), name="simc_regular_compare"),
    path('api/wcl-analysis-task/', csrf_exempt(WclAnalysisTaskAPIView.as_view()), name="wcl_analysis_task"),
    path('api/wcl-analysis-task/<int:task_id>/', csrf_exempt(WclAnalysisTaskAPIView.as_view()), name="wcl_analysis_task_detail"),
    
    # SimC结果查看页面
    path('simc-result/', SimcResultView.as_view(), name="simc_result"),
    path('simc-attribute-analysis/', SimcAttributeAnalysisView.as_view(), name="simc_attribute_analysis"),
    path('simc-attribute-analysis-ssr/', SimcAttributeAnalysisSSRView.as_view(), name="simc_attribute_analysis_ssr"),
    path('simc-compare/', SimcRegularCompareView.as_view(), name="simc_regular_compare_view"),
    path('wcl-analysis/', WclAnalysisPageView.as_view(), name="wcl_analysis"),
    path('wcl-analysis/report/<int:task_id>/', WclAnalysisReportView.as_view(), name="wcl_analysis_report"),
]
