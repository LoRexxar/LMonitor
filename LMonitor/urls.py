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
from botend.dashboard.api import (
    ConvertTextAPIView, KeywordManagerAPIView, AplStorageAPIView, AplDetailAPIView,
    SimcTaskAPIView, SimcBatchTaskAPIView, SimcProfileAPIView, SimcPlayerConfigDetailAPIView,
    SimcRawInspectAPIView, SimcTemplateAPIView, SimcAplCandidatesAPIView, KeywordTranslationAPIView,
    OssConfigAPIView, SimcResultProxyAPIView, SimcTaskPreviewAPIView, SimcAttributeAnalysisAPIView, SimcRegularCompareAPIView,
    SimcBattlenetPreflightAPIView,
    SimcBackendBinaryAPIView, SimcWorkbenchAPIView, SimcArtifactPreviewAPIView, SimcTaskReportPreviewAPIView, WclAnalysisTaskAPIView, SystemAlertAPIView, PortalPeakSpecRankRefreshAPIView,
    WowDailyReportListAPIView, WowDailyReportContentAPIView, WowDailyReportDownloadAPIView,
    WowDailyReportGenerateAPIView, WagoHotfixReportListAPIView, WagoSkillDiffRerunAPIView,
)
from botend.dashboard.auth_views import LoginView, RegisterView, LogoutView, ChangePasswordView
from botend.portal.views import PortalHomeView
from botend.portal.views import PortalArticleView, PortalNewsView, PortalSpecsView
from botend.portal.views import PortalReportFileView, PortalWowHotfixReportView, PortalWowSkillDiffReportView
from botend.portal.spec_detail_views import SpecDetailPlayerView, SpecDetailPlayerDetailView, SpecDetailDungeonView, SpecDetailRaidView
from botend.portal.talent_simulator import PortalTalentSimulatorAPIView, PortalTalentSimulatorEncodeAPIView, PortalTalentSimulatorView
from botend.portal.api import (
    PortalBluepostsAPIView,
    PortalNgaHotAPIView,
    PortalExwindLatestAPIView,
    PortalWowheadLatestAPIView,
    PortalNewsIndexAPIView,
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
    PortalHotfixReportsAPIView,
    PortalDailyReportLatestAPIView,
    PortalArticleDetailAPIView,
)
from django.http import HttpResponse, JsonResponse

urlpatterns = [
    # path('admin/', admin.site.urls),
    path('favicon.ico', RedirectView.as_view(url='/static/portal/favicons/3accfdf0352f2189a3292605e1ad80f12bd5a15c605069102f42c03c3c4fceda.ico', permanent=True)),
    path('', PortalHomeView.as_view(), name='portal_home'),
    path('portal/news/', PortalNewsView.as_view(), name='portal_news'),
    path('portal/specs/', PortalSpecsView.as_view(), name='portal_specs'),
    path('portal/article/<int:article_id>/', PortalArticleView.as_view(), name='portal_article'),
    path('portal/talents/', PortalTalentSimulatorView.as_view(), name='portal_talent_simulator'),

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
    path('portal/api/wowhead/latest/', csrf_exempt(PortalWowheadLatestAPIView.as_view()), name="portal_wowhead_latest"),
    path('portal/api/news/', csrf_exempt(PortalNewsIndexAPIView.as_view()), name="portal_news_index"),
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
    path('portal/api/hotfix-reports/', csrf_exempt(PortalHotfixReportsAPIView.as_view()), name="portal_hotfix_reports"),
    path('portal/api/daily-report/latest/', csrf_exempt(PortalDailyReportLatestAPIView.as_view()), name="portal_daily_report_latest"),
    path('portal/api/article/<int:article_id>/', csrf_exempt(PortalArticleDetailAPIView.as_view()), name="portal_article_detail"),
    path('portal/api/talents/simulator/', csrf_exempt(PortalTalentSimulatorAPIView.as_view()), name="portal_talent_simulator_api"),
    path('portal/api/talents/simulator/encode/', csrf_exempt(PortalTalentSimulatorEncodeAPIView.as_view()), name="portal_talent_simulator_encode"),
    path('portal/reports/<path:report_path>', PortalReportFileView.as_view(), name="portal_report_file"),
    path('portal/wow-hotfix-report/<int:report_id>/', PortalWowHotfixReportView.as_view(), name="portal_wow_hotfix_report"),
    path('portal/wow-skill-diff/<int:report_id>/', PortalWowSkillDiffReportView.as_view(), name="portal_wow_skill_diff_report"),

    # API路由
    path('api/convert-text/', csrf_exempt(ConvertTextAPIView.as_view()), name="convert_text"),
    path('api/keyword-manager/', KeywordManagerAPIView.as_view(), name="keyword_manager"),
    path('api/apl-storage/', AplStorageAPIView.as_view(), name="apl_storage"),
    path('api/apl-storage/<int:apl_id>/', AplDetailAPIView.as_view(), name="apl_detail"),
    path('api/simc-task/', SimcTaskAPIView.as_view(), name="simc_task"),
    path('api/simc-task/batch/', SimcBatchTaskAPIView.as_view(), name="simc_task_batch"),
    path('api/simc-task/preview/', SimcTaskPreviewAPIView.as_view(), name="simc_task_preview"),
    path('api/simc-profile/', SimcProfileAPIView.as_view(), name="simc_profile"),
    path('api/simc-profile/inspect-raw/', SimcRawInspectAPIView.as_view(), name="simc_profile_inspect_raw"),
    path('api/simc-player-config-detail/', SimcPlayerConfigDetailAPIView.as_view(), name="simc_player_config_detail"),
    path('api/simc-battlenet-preflight/', SimcBattlenetPreflightAPIView.as_view(), name="simc_battlenet_preflight"),
    path('api/simc-profile/<int:profile_id>/', SimcProfileAPIView.as_view(), name="simc_profile_detail"),
    path('api/simc-apl-candidates/', SimcAplCandidatesAPIView.as_view(), name="simc_apl_candidates"),
    path('api/simc-template/', SimcTemplateAPIView.as_view(), name="simc_template"),
    path('api/simc-backend-binary/', SimcBackendBinaryAPIView.as_view(), name="simc_backend_binary"),
    path('api/simc-workbench/<str:resource>/', SimcWorkbenchAPIView.as_view(), name="simc_workbench"),
    path('api/simc-workbench/<str:resource>/<int:object_id>/', SimcWorkbenchAPIView.as_view(), name="simc_workbench_detail"),
    path('api/simc-workbench/tasks/<int:object_id>/report-preview/', SimcTaskReportPreviewAPIView.as_view(), name="simc_task_report_preview"),
    path('api/simc-workbench/artifacts/<int:object_id>/preview/', SimcArtifactPreviewAPIView.as_view(), name="simc_artifact_preview"),
    path('api/system-alert/', csrf_exempt(SystemAlertAPIView.as_view()), name="system_alert"),
    path('api/portal/peak/refresh/', csrf_exempt(PortalPeakSpecRankRefreshAPIView.as_view()), name="portal_peak_refresh"),
    path('api/wow-daily-report/list/', csrf_exempt(WowDailyReportListAPIView.as_view()), name="wow_daily_report_list"),
    path('api/wow-daily-report/content/', csrf_exempt(WowDailyReportContentAPIView.as_view()), name="wow_daily_report_content"),
    path('api/wow-daily-report/download/', csrf_exempt(WowDailyReportDownloadAPIView.as_view()), name="wow_daily_report_download"),
    path('api/wow-daily-report/generate/', csrf_exempt(WowDailyReportGenerateAPIView.as_view()), name="wow_daily_report_generate"),
    path('api/wago-skill-diff/rerun/', csrf_exempt(WagoSkillDiffRerunAPIView.as_view()), name="wago_skill_diff_rerun"),
    path('api/wago-hotfix-reports/', csrf_exempt(WagoHotfixReportListAPIView.as_view()), name="wago_hotfix_reports"),
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

    # 专精详情页
    path('portal/spec/<str:class_name>/<str:spec_name>/', SpecDetailPlayerView.as_view(), name="spec_detail_player"),
    path('portal/spec/<str:class_name>/<str:spec_name>/player/<int:player_id>/', SpecDetailPlayerDetailView.as_view(), name="spec_detail_player_detail"),
    path('portal/spec/<str:class_name>/<str:spec_name>/dungeons/', SpecDetailDungeonView.as_view(), name="spec_detail_dungeon"),
    path('portal/spec/<str:class_name>/<str:spec_name>/raid/', SpecDetailRaidView.as_view(), name="spec_detail_raid"),
]
