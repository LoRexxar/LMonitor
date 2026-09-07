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
from botend.dashboard.dashboard import DashboardView, SimcWorkbenchDetailPageView, SimcBenchmarkPanelEditPageView, SimcBenchmarkConfigPageView, SimcBenchmarkExecutionPageView, SimcResultView, SimcAttributeAnalysisView, SimcRegularCompareView, SimcAttributeAnalysisSSRView, WclAnalysisPageView, WclAnalysisReportView
from botend.dashboard.api import (
    ConvertTextAPIView, AplStorageAPIView, AplDetailAPIView,
    SimcTaskAPIView, SimcComparisonTaskAPIView, SimcProfileAPIView, SimcPlayerConfigDetailAPIView,
    SimcTemplateAPIView, SimcAplCandidatesAPIView, SimcTalentStringAPIView, SimcTalentStringCandidatesAPIView, SimcSpecOptionsAPIView,
    OssConfigAPIView, SimcResultProxyAPIView, SimcTaskPreviewAPIView, SimcAttributeAnalysisAPIView, SimcRegularCompareAPIView,
    SimcBattlenetPreflightAPIView, SimcBattlenetTopPlayersAPIView,
    SimcBackendBinaryAPIView, SimcSkillDamageSnapshotAPIView, SimcWorkbenchAPIView, SimcArtifactPreviewAPIView, SimcRunInputPreviewAPIView, SimcTaskReportPreviewAPIView, WclAnalysisTaskAPIView, SystemAlertAPIView, PortalPeakSpecRankRefreshAPIView,
    WowDailyReportListAPIView, WowDailyReportContentAPIView, WowDailyReportDownloadAPIView,
    WowDailyReportGenerateAPIView, WagoHotfixReportListAPIView, WagoSkillDiffRerunAPIView,
    SimcAplValidationAPIView, SimcAplSymbolsAPIView, SimcAplSpellsAPIView, SimcAplCompletionsAPIView,
    SimcBenchmarkPanelListAPIView, SimcBenchmarkPanelCoverageAPIView,
    SimcBenchmarkPanelDetailAPIView,
    SimcBenchmarkPanelPurgeAPIView, SimcBenchmarkPurgeTaskDetailAPIView,
    SimcBenchmarkPanelDuplicateAPIView,
    SimcBenchmarkPanelRunAPIView, SimcBenchmarkPanelExecutionListAPIView,
    SimcBenchmarkExecutionDetailAPIView, SimcBenchmarkExecutionRerunFailedAPIView,
    SimcBenchmarkCaseRerunAPIView,
    SimcBenchmarkExecutionCancelAPIView, SimcBenchmarkExecutionReconcileAPIView,
    SimcBenchmarkOptionsAPIView, SimcBenchmarkPanelOptionsAPIView, SimcBenchmarkItemLookupAPIView,
    SimcFightStyleOptionsAPIView, SimcRaidBuffOptionsAPIView, SimcExtraOptionsAPIView, SimcConsumableOptionsAPIView,
)
from botend.dashboard.auth_views import LoginView, RegisterView, LogoutView, ChangePasswordView
from botend.dashboard.user_management import (
    DashboardUserDetailAPIView,
    DashboardUserGroupDetailAPIView,
    DashboardUserGroupListAPIView,
    DashboardUserListAPIView,
)
from botend.portal.views import PortalHomeView, PortalSimcBenchmarkResultsView
from botend.portal.views import PortalArticleView, PortalMplusDpsRankingsView, PortalNewsView, PortalSpecsView
from botend.portal.views import PortalReportFileView, PortalWowHotfixReportView, PortalWowSkillDiffReportView
from botend.portal.spec_detail_views import SpecDetailPlayerView, SpecDetailPlayerDetailView, SpecDetailDungeonView, SpecDetailRaidView, SpecOverviewAPIView, SimcProfileDetailView
from botend.portal.talent_simulator import PortalTalentSimulatorAPIView, PortalTalentSimulatorEncodeAPIView, PortalTalentSimulatorView
from botend.portal.gear_builder import (
    PortalGearBuilderBootstrapAPIView,
    PortalGearBuilderCatalogAPIView,
    PortalGearBuilderCraftedResolveAPIView,
    PortalGearBuilderEnhancementsAPIView,
    PortalGearBuilderShareResolveAPIView,
    PortalGearBuilderShortLinkAPIView,
    PortalGearBuilderShortLinkDetailAPIView,
    PortalGearBuilderSimcImportAPIView,
    PortalGearBuilderOnlineLoadoutAPIView,
    PortalGearBuilderView,
    PortalGearBuilderOwnedItemsAPIView,
    PortalGearAssistantBootstrapAPIView,
    PortalGearAssistantOptimizeAPIView,
    PortalGearAssistantView,
)
from botend.dashboard.gear_builder_management import DashboardGearBuilderManagementAPIView
from botend.dashboard.wow_today_management import DashboardWowTodaySectionAPIView
from botend.dashboard.portal_navigation_management import DashboardPortalNavigationAPIView
from botend.portal.api import (
    PortalBluepostsAPIView,
    PortalNgaHotAPIView,
    PortalExwindLatestAPIView,
    PortalWowheadLatestAPIView,
    PortalNewsIndexAPIView,
    PortalEventsAPIView,
    PortalVideosAPIView,
    PortalToolsAPIView,
    PortalNavigationAPIView,
    PortalMplusAffixesAPIView,
    PortalMplusCutoffAPIView,
    PortalMplusRankingsAPIView,
    PortalPeakSpecRankingsAPIView,
    PortalRaidRankingsAPIView,
    PortalCharacterAPIView,
    PortalMythicstatsDpsAPIView,
    PortalMplusDpsRankingsAPIView,
    PortalWowSkillDiffListAPIView,
    PortalWowSkillDiffStatesAPIView,
    PortalHotfixReportsAPIView,
    PortalDailyReportLatestAPIView,
    PortalWowTodayAPIView,
    PortalArticleDetailAPIView,
)
from botend.mythic_planner.api import (
    DashboardMythicPlannerAPIView,
    MythicPlannerCatalogAPIView,
    MythicPlannerDungeonAPIView,
    MythicPlannerRouteShareAPIView,
    MythicPlannerShareCodeAPIView,
    MythicPlannerSharedRouteAPIView,
)
from botend.mythic_planner.views import (
    DashboardMythicPlannerPositionsView,
    DashboardMythicPlannerRoutesView,
    DashboardMythicPlannerView,
    PortalMythicPlannerView,
)
from botend.portal.simc_benchmark_api import (
    PortalSimcAplRankingAPIView,
    PortalSimcBaselineResultsAPIView,
    PortalSimcBenchmarkPanelListAPIView,
    PortalSimcBenchmarkPanelDetailAPIView,
    PortalSimcSpecRankingAPIView,
)
from botend.simc_agent_api import (
    SimcAgentHeartbeatAPIView, SimcAgentRegisterAPIView,
    SimcAgentJobClaimAPIView, SimcAgentJobHeartbeatAPIView,
    SimcAgentJobReportUploadAPIView, SimcAgentJobCompleteAPIView,
    SimcAgentMaintenanceTaskAPIView,
    SimcAgentManagementListAPIView, SimcAgentManagementActiveAPIView, SimcAgentManagementTaskScopeAPIView,
    SimcAgentEnrollmentCodeListAPIView, SimcAgentEnrollmentCodeRevokeAPIView,
)
from django.http import HttpResponse, JsonResponse

urlpatterns = [
    # path('admin/', admin.site.urls),
    path('favicon.ico', RedirectView.as_view(url='/static/portal/favicons/3accfdf0352f2189a3292605e1ad80f12bd5a15c605069102f42c03c3c4fceda.ico', permanent=True)),
    path('', PortalHomeView.as_view(), name='portal_home'),
    path('portal/simc-benchmarks/', PortalSimcBenchmarkResultsView.as_view(), name='portal_simc_benchmark_results'),
    path('portal/simc-benchmarks/<int:panel_id>/', PortalSimcBenchmarkResultsView.as_view(), name='portal_simc_benchmark_panel_results'),
    path('portal/news/', PortalNewsView.as_view(), name='portal_news'),
    path('portal/specs/', PortalSpecsView.as_view(), name='portal_specs'),
    path('portal/mplus/dps-rankings/', PortalMplusDpsRankingsView.as_view(), name='portal_mplus_dps_rankings'),
    path('portal/article/<int:article_id>/', PortalArticleView.as_view(), name='portal_article'),
    path('portal/talents/', PortalTalentSimulatorView.as_view(), name='portal_talent_simulator'),
    path('portal/gear-builder/', PortalGearBuilderView.as_view(), name='portal_gear_builder'),
    path('portal/gear-assistant/', PortalGearAssistantView.as_view(), name='portal_gear_assistant'),
    path('g/<slug:share_token>/', PortalGearBuilderView.as_view(), name='portal_gear_builder_short_link'),
    path('portal/mythic-planner/', PortalMythicPlannerView.as_view(), name='portal_mythic_planner'),
    path('m/<slug:share_token>', PortalMythicPlannerView.as_view(), name='portal_mythic_planner_short_link'),

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
    path('api/dashboard/users/', DashboardUserListAPIView.as_view(), name='dashboard_user_list'),
    path('api/dashboard/users/<int:user_id>/', DashboardUserDetailAPIView.as_view(), name='dashboard_user_detail'),
    path('api/dashboard/user-groups/', DashboardUserGroupListAPIView.as_view(), name='dashboard_user_group_list'),
    path('api/dashboard/user-groups/<int:group_id>/', DashboardUserGroupDetailAPIView.as_view(), name='dashboard_user_group_detail'),
    path('api/dashboard/gear-builder/<str:resource>/', DashboardGearBuilderManagementAPIView.as_view(), name='dashboard_gear_builder_management'),
    path('api/dashboard/gear-builder/<str:resource>/<int:object_id>/', DashboardGearBuilderManagementAPIView.as_view(), name='dashboard_gear_builder_management_detail'),
    path('api/dashboard/wow-today-sections/', DashboardWowTodaySectionAPIView.as_view(), name='dashboard_wow_today_sections'),
    path('api/dashboard/portal-navigation/', DashboardPortalNavigationAPIView.as_view(), name='dashboard_portal_navigation'),
    path('dashboard/mythic-planner/', DashboardMythicPlannerView.as_view(), name='dashboard_mythic_planner'),
    path('dashboard/mythic-planner/positions/', DashboardMythicPlannerPositionsView.as_view(), name='dashboard_mythic_planner_positions'),
    path('dashboard/mythic-planner/routes/', DashboardMythicPlannerRoutesView.as_view(), name='dashboard_mythic_planner_routes'),
    path('dashboard/simc/tasks/<int:object_id>/', SimcWorkbenchDetailPageView.as_view(), {'kind': 'tasks'}, name='simc_task_detail_page'),
    path('dashboard/simc/benchmarks/<int:panel_id>/edit/', SimcBenchmarkPanelEditPageView.as_view(), name='simc_benchmark_panel_edit_page'),
    path('dashboard/simc/benchmarks/<int:panel_id>/config/', SimcBenchmarkConfigPageView.as_view(), name='simc_benchmark_config_page'),
    path('dashboard/simc/benchmarks/executions/<int:execution_id>/', SimcBenchmarkExecutionPageView.as_view(), name='simc_benchmark_execution_page'),

    # Portal API
    path('portal/api/blueposts/', csrf_exempt(PortalBluepostsAPIView.as_view()), name="portal_blueposts"),
    path('portal/api/nga-hot/', csrf_exempt(PortalNgaHotAPIView.as_view()), name="portal_nga_hot"),
    path('portal/api/exwind/latest/', csrf_exempt(PortalExwindLatestAPIView.as_view()), name="portal_exwind_latest"),
    path('portal/api/wowhead/latest/', csrf_exempt(PortalWowheadLatestAPIView.as_view()), name="portal_wowhead_latest"),
    path('portal/api/news/', csrf_exempt(PortalNewsIndexAPIView.as_view()), name="portal_news_index"),
    path('portal/api/events/', csrf_exempt(PortalEventsAPIView.as_view()), name="portal_events"),
    path('portal/api/videos/', csrf_exempt(PortalVideosAPIView.as_view()), name="portal_videos"),
    path('portal/api/tools/', csrf_exempt(PortalToolsAPIView.as_view()), name="portal_tools"),
    path('portal/api/navigation/', csrf_exempt(PortalNavigationAPIView.as_view()), name="portal_navigation"),
    path('portal/api/mplus/affixes/', csrf_exempt(PortalMplusAffixesAPIView.as_view()), name="portal_mplus_affixes"),
    path('portal/api/mplus/cutoff/', csrf_exempt(PortalMplusCutoffAPIView.as_view()), name="portal_mplus_cutoff"),
    path('portal/api/mplus/rankings/', csrf_exempt(PortalMplusRankingsAPIView.as_view()), name="portal_mplus_rankings"),
    path('portal/api/mplus/dps-rankings/', csrf_exempt(PortalMplusDpsRankingsAPIView.as_view()), name="portal_mplus_dps_rankings_api"),
    path('portal/api/peak/spec-rankings/', csrf_exempt(PortalPeakSpecRankingsAPIView.as_view()), name="portal_peak_spec_rankings"),
    path('portal/api/raid/rankings/', csrf_exempt(PortalRaidRankingsAPIView.as_view()), name="portal_raid_rankings"),
    path('portal/api/character/', csrf_exempt(PortalCharacterAPIView.as_view()), name="portal_character"),
    path('portal/api/mythicstats/dps/', csrf_exempt(PortalMythicstatsDpsAPIView.as_view()), name="portal_mythicstats_dps"),
    path('portal/api/wow-skill-diffs/', csrf_exempt(PortalWowSkillDiffListAPIView.as_view()), name="portal_wow_skill_diffs"),
    path('portal/api/wow-skill-diff/states/', csrf_exempt(PortalWowSkillDiffStatesAPIView.as_view()), name="portal_wow_skill_diff_states"),
    path('portal/api/hotfix-reports/', csrf_exempt(PortalHotfixReportsAPIView.as_view()), name="portal_hotfix_reports"),
    path('portal/api/daily-report/latest/', csrf_exempt(PortalDailyReportLatestAPIView.as_view()), name="portal_daily_report_latest"),
    path('portal/api/today-in-wow/latest/', csrf_exempt(PortalWowTodayAPIView.as_view()), name="portal_wow_today_latest"),
    path('portal/api/article/<int:article_id>/', csrf_exempt(PortalArticleDetailAPIView.as_view()), name="portal_article_detail"),
    path('portal/api/simc-benchmarks/panels/', PortalSimcBenchmarkPanelListAPIView.as_view(), name='portal_simc_benchmark_panels'),
    path('portal/api/simc-benchmarks/panels/<int:panel_id>/', PortalSimcBenchmarkPanelDetailAPIView.as_view(), name='portal_simc_benchmark_panel_detail_by_id'),
    path('portal/api/simc-benchmarks/panels/<slug:slug>/', PortalSimcBenchmarkPanelDetailAPIView.as_view(), name='portal_simc_benchmark_panel_detail'),
    path('portal/api/simc-benchmarks/apl-rankings/', PortalSimcAplRankingAPIView.as_view(), name='portal_simc_apl_rankings'),
    path('portal/api/simc-benchmarks/baseline-results/', PortalSimcBaselineResultsAPIView.as_view(), name='portal_simc_baseline_results'),
    path('portal/api/simc-benchmarks/spec-rankings/', PortalSimcSpecRankingAPIView.as_view(), name='portal_simc_spec_rankings'),
    path('portal/api/talents/simulator/', csrf_exempt(PortalTalentSimulatorAPIView.as_view()), name="portal_talent_simulator_api"),
    path('portal/api/talents/simulator/encode/', csrf_exempt(PortalTalentSimulatorEncodeAPIView.as_view()), name="portal_talent_simulator_encode"),
    path('portal/api/gear-builder/bootstrap/', PortalGearBuilderBootstrapAPIView.as_view(), name='portal_gear_builder_bootstrap'),
    path('portal/api/gear-builder/catalog/', PortalGearBuilderCatalogAPIView.as_view(), name='portal_gear_builder_catalog'),
    path('portal/api/gear-builder/enhancements/', PortalGearBuilderEnhancementsAPIView.as_view(), name='portal_gear_builder_enhancements'),
    path('portal/api/gear-builder/resolve-crafted/', csrf_exempt(PortalGearBuilderCraftedResolveAPIView.as_view()), name='portal_gear_builder_resolve_crafted'),
    path('portal/api/gear-builder/resolve-share/', csrf_exempt(PortalGearBuilderShareResolveAPIView.as_view()), name='portal_gear_builder_resolve_share'),
    path('portal/api/gear-builder/import-simc/', csrf_exempt(PortalGearBuilderSimcImportAPIView.as_view()), name='portal_gear_builder_import_simc'),
    path('portal/api/gear-builder/online-loadouts/', PortalGearBuilderOnlineLoadoutAPIView.as_view(), name='portal_gear_builder_online_loadouts'),
    path('portal/api/gear-builder/online-loadouts/<int:loadout_id>/', PortalGearBuilderOnlineLoadoutAPIView.as_view(), name='portal_gear_builder_online_loadout_detail'),
    path('portal/api/gear-builder/short-links/', PortalGearBuilderShortLinkAPIView.as_view(), name='portal_gear_builder_short_links'),
    path('portal/api/gear-builder/short-links/<slug:share_token>/', PortalGearBuilderShortLinkDetailAPIView.as_view(), name='portal_gear_builder_short_link_detail'),
    path('portal/api/gear-builder/owned-items/', PortalGearBuilderOwnedItemsAPIView.as_view(), name='portal_gear_builder_owned_items'),
    path('portal/api/gear-builder/owned-items/<int:owned_id>/', PortalGearBuilderOwnedItemsAPIView.as_view(), name='portal_gear_builder_owned_item_detail'),
    path('portal/api/gear-assistant/bootstrap/', PortalGearAssistantBootstrapAPIView.as_view(), name='portal_gear_assistant_bootstrap'),
    path('portal/api/gear-assistant/optimize/', PortalGearAssistantOptimizeAPIView.as_view(), name='portal_gear_assistant_optimize'),
    path('portal/api/mythic-planner/catalog/', MythicPlannerCatalogAPIView.as_view(), name='mythic_planner_catalog'),
    path('portal/api/mythic-planner/dungeons/<slug:dungeon_key>/', MythicPlannerDungeonAPIView.as_view(), name='mythic_planner_dungeon'),
    path('portal/api/mythic-planner/share-code/', MythicPlannerShareCodeAPIView.as_view(), name='mythic_planner_share_code'),
    path('portal/api/mythic-planner/share-links/', MythicPlannerRouteShareAPIView.as_view(), name='mythic_planner_route_share_create'),
    path('portal/api/mythic-planner/share-links/<slug:share_token>/', MythicPlannerRouteShareAPIView.as_view(), name='mythic_planner_route_share_detail'),
    path('portal/api/mythic-planner/shared/<uuid:share_id>/', MythicPlannerSharedRouteAPIView.as_view(), name='mythic_planner_shared_route'),
    path('portal/reports/<path:report_path>', PortalReportFileView.as_view(), name="portal_report_file"),
    path('portal/wow-hotfix-report/<int:report_id>/', PortalWowHotfixReportView.as_view(), name="portal_wow_hotfix_report"),
    path('portal/wow-skill-diff/<int:report_id>/', PortalWowSkillDiffReportView.as_view(), name="portal_wow_skill_diff_report"),

    # API路由
    path('api/simc-agent/v1/register/', SimcAgentRegisterAPIView.as_view(), name='simc_agent_register'),
    path('api/simc-agent/v1/heartbeat/', SimcAgentHeartbeatAPIView.as_view(), name='simc_agent_heartbeat'),
    path('api/simc-agent/v1/jobs/claim/', SimcAgentJobClaimAPIView.as_view(), name='simc_agent_job_claim'),
    path('api/simc-agent/v1/jobs/<int:run_id>/heartbeat/', SimcAgentJobHeartbeatAPIView.as_view(), name='simc_agent_job_heartbeat'),
    path('api/simc-agent/v1/jobs/<int:run_id>/report-upload/', SimcAgentJobReportUploadAPIView.as_view(), name='simc_agent_job_report_upload'),
    path('api/simc-agent/v1/jobs/<int:run_id>/complete/', SimcAgentJobCompleteAPIView.as_view(), name='simc_agent_job_complete'),
    path('api/simc-agent/v1/maintenance-tasks/<int:task_id>/', SimcAgentMaintenanceTaskAPIView.as_view(), name='simc_agent_maintenance_task'),
    path('api/simc-workbench/agents/', SimcAgentManagementListAPIView.as_view(), name='simc_agent_management_list'),
    path('api/simc-workbench/agents/<int:agent_id>/active/', SimcAgentManagementActiveAPIView.as_view(), name='simc_agent_management_active'),
    path('api/simc-workbench/agents/<int:agent_id>/task-scope/', SimcAgentManagementTaskScopeAPIView.as_view(), name='simc_agent_management_task_scope'),
    path('api/simc-workbench/agent-enrollment-codes/', SimcAgentEnrollmentCodeListAPIView.as_view(), name='simc_agent_enrollment_codes'),
    path('api/simc-workbench/agent-enrollment-codes/<int:code_id>/revoke/', SimcAgentEnrollmentCodeRevokeAPIView.as_view(), name='simc_agent_enrollment_code_revoke'),
    path('api/convert-text/', csrf_exempt(ConvertTextAPIView.as_view()), name="convert_text"),
    path('api/mythic-planner/manage/', DashboardMythicPlannerAPIView.as_view(), name='dashboard_mythic_planner_api'),
    path('api/mythic-planner/manage/<int:object_id>/', DashboardMythicPlannerAPIView.as_view(), name='dashboard_mythic_planner_detail_api'),

    path('api/apl-storage/', AplStorageAPIView.as_view(), name="apl_storage"),
    path('api/apl-storage/<int:apl_id>/', AplDetailAPIView.as_view(), name="apl_detail"),
    path('api/simc-task/', SimcTaskAPIView.as_view(), name="simc_task"),
    path('api/simc-fight-styles/options/', SimcFightStyleOptionsAPIView.as_view(), name='simc_fight_style_options'),
    path('api/simc-raid-buffs/options/', SimcRaidBuffOptionsAPIView.as_view(), name='simc_raid_buff_options'),
    path('api/simc-profile/consumable-options/', SimcConsumableOptionsAPIView.as_view(), name='simc_consumable_options'),
    path('api/simc-task/comparison/', SimcComparisonTaskAPIView.as_view(), name="simc_task_comparison"),
    path('api/simc-task/preview/', SimcTaskPreviewAPIView.as_view(), name="simc_task_preview"),
    path('api/simc-profile/', SimcProfileAPIView.as_view(), name="simc_profile"),

    path('api/simc-player-config-detail/', SimcPlayerConfigDetailAPIView.as_view(), name="simc_player_config_detail"),
    path('api/simc-battlenet-preflight/', SimcBattlenetPreflightAPIView.as_view(), name="simc_battlenet_preflight"),
    path('api/simc-battlenet-top-players/', SimcBattlenetTopPlayersAPIView.as_view(), name="simc_battlenet_top_players"),
    path('api/simc-profile/<int:profile_id>/', SimcProfileAPIView.as_view(), name="simc_profile_detail"),
    path('api/simc-apl-candidates/', SimcAplCandidatesAPIView.as_view(), name="simc_apl_candidates"),
    path('api/simc-talent-string/', SimcTalentStringAPIView.as_view(), name="simc_talent_string"),
    path('api/simc-talent-string/<int:talent_string_id>/', SimcTalentStringAPIView.as_view(), name="simc_talent_string_detail"),
    path('api/simc-talent-string-candidates/', SimcTalentStringCandidatesAPIView.as_view(), name="simc_talent_string_candidates"),
    path('api/simc-spec-options/', SimcSpecOptionsAPIView.as_view(), name="simc_spec_options"),
    path('api/simc-extra-options/options/', SimcExtraOptionsAPIView.as_view(), name="simc_extra_options"),
    path('api/simc-template/', SimcTemplateAPIView.as_view(), name="simc_template"),
    path('api/simc-backend-binary/', SimcBackendBinaryAPIView.as_view(), name="simc_backend_binary"),
    path('api/simc-skill-damage/', SimcSkillDamageSnapshotAPIView.as_view(), name="simc_skill_damage_snapshot"),
    path('api/simc-workbench/apl-validation/', SimcAplValidationAPIView.as_view(), name="simc_apl_validation"),
    path('api/simc-workbench/apl-symbols/', SimcAplSymbolsAPIView.as_view(), name="simc_apl_symbols"),
    path('api/simc-workbench/apl-spells/', SimcAplSpellsAPIView.as_view(), name="simc_apl_spells"),
    path('api/simc-workbench/apl-completions/', SimcAplCompletionsAPIView.as_view(), name="simc_apl_completions"),
    path('api/simc-workbench/<str:resource>/', SimcWorkbenchAPIView.as_view(), name="simc_workbench"),
    path('api/simc-workbench/<str:resource>/<int:object_id>/', SimcWorkbenchAPIView.as_view(), name="simc_workbench_detail"),
    path('api/simc-workbench/tasks/<int:task_id>/runs/<int:run_id>/input/', SimcRunInputPreviewAPIView.as_view(), name="simc_run_input_preview"),
    path('api/simc-workbench/tasks/<int:object_id>/report-preview/', SimcTaskReportPreviewAPIView.as_view(), name="simc_task_report_preview"),
    path('api/simc-workbench/artifacts/<int:object_id>/preview/', SimcArtifactPreviewAPIView.as_view(), name="simc_artifact_preview"),
    path('api/simc-benchmarks/panels/', SimcBenchmarkPanelListAPIView.as_view(), name='simc_benchmark_panels'),
    path('api/simc-benchmarks/panels/<int:panel_id>/coverage/', SimcBenchmarkPanelCoverageAPIView.as_view(), name='simc_benchmark_panel_coverage'),
    path('api/simc-benchmarks/options/', SimcBenchmarkOptionsAPIView.as_view(), name='simc_benchmark_options'),
    path('api/simc-benchmarks/item-lookup/', SimcBenchmarkItemLookupAPIView.as_view(), name='simc_benchmark_item_lookup'),
    path('api/simc-benchmarks/panels/<int:panel_id>/options/', SimcBenchmarkPanelOptionsAPIView.as_view(), name='simc_benchmark_panel_options'),
    path('api/simc-benchmarks/panels/<int:panel_id>/', SimcBenchmarkPanelDetailAPIView.as_view(), name='simc_benchmark_panel_detail'),
    path('api/simc-benchmarks/panels/<int:panel_id>/purge/', SimcBenchmarkPanelPurgeAPIView.as_view(), name='simc_benchmark_panel_purge'),
    path('api/simc-benchmarks/purges/<int:purge_id>/', SimcBenchmarkPurgeTaskDetailAPIView.as_view(), name='simc_benchmark_purge_detail'),
    path('api/simc-benchmarks/panels/<int:panel_id>/duplicate/', SimcBenchmarkPanelDuplicateAPIView.as_view(), name='simc_benchmark_panel_duplicate'),
    path('api/simc-benchmarks/panels/<int:panel_id>/run/', SimcBenchmarkPanelRunAPIView.as_view(), name='simc_benchmark_panel_run'),
    path('api/simc-benchmarks/panels/<int:panel_id>/executions/', SimcBenchmarkPanelExecutionListAPIView.as_view(), name='simc_benchmark_panel_executions'),
    path('api/simc-benchmarks/executions/<int:execution_id>/', SimcBenchmarkExecutionDetailAPIView.as_view(), name='simc_benchmark_execution_detail'),
    path('api/simc-benchmarks/executions/<int:execution_id>/rerun-failed/', SimcBenchmarkExecutionRerunFailedAPIView.as_view(), name='simc_benchmark_execution_rerun_failed'),
    path('api/simc-benchmarks/executions/<int:execution_id>/cases/<int:case_id>/rerun/', SimcBenchmarkCaseRerunAPIView.as_view(), name='simc_benchmark_case_rerun'),
    path('api/simc-benchmarks/executions/<int:execution_id>/cancel/', SimcBenchmarkExecutionCancelAPIView.as_view(), name='simc_benchmark_execution_cancel'),
    path('api/simc-benchmarks/executions/<int:execution_id>/reconcile/', SimcBenchmarkExecutionReconcileAPIView.as_view(), name='simc_benchmark_execution_reconcile'),
    path('api/system-alert/', csrf_exempt(SystemAlertAPIView.as_view()), name="system_alert"),
    path('api/portal/peak/refresh/', csrf_exempt(PortalPeakSpecRankRefreshAPIView.as_view()), name="portal_peak_refresh"),
    path('api/wow-daily-report/list/', csrf_exempt(WowDailyReportListAPIView.as_view()), name="wow_daily_report_list"),
    path('api/wow-daily-report/content/', csrf_exempt(WowDailyReportContentAPIView.as_view()), name="wow_daily_report_content"),
    path('api/wow-daily-report/download/', csrf_exempt(WowDailyReportDownloadAPIView.as_view()), name="wow_daily_report_download"),
    path('api/wow-daily-report/generate/', csrf_exempt(WowDailyReportGenerateAPIView.as_view()), name="wow_daily_report_generate"),
    path('api/wago-skill-diff/rerun/', csrf_exempt(WagoSkillDiffRerunAPIView.as_view()), name="wago_skill_diff_rerun"),
    path('api/wago-hotfix-reports/', csrf_exempt(WagoHotfixReportListAPIView.as_view()), name="wago_hotfix_reports"),

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
    path('portal/api/spec/<str:class_name>/<str:spec_name>/<str:module>/', SpecOverviewAPIView.as_view(), name="spec_overview_api"),
    path('portal/spec/<str:class_name>/<str:spec_name>/simc-profile/<int:profile_id>/', SimcProfileDetailView.as_view(), name="portal_simc_profile_detail"),
    path('portal/spec/<str:class_name>/<str:spec_name>/', SpecDetailPlayerView.as_view(), name="spec_detail_player"),
    path('portal/spec/<str:class_name>/<str:spec_name>/player/<int:player_id>/', SpecDetailPlayerDetailView.as_view(), name="spec_detail_player_detail"),
    path('portal/spec/<str:class_name>/<str:spec_name>/dungeons/', SpecDetailDungeonView.as_view(), name="spec_detail_dungeon"),
    path('portal/spec/<str:class_name>/<str:spec_name>/raid/', SpecDetailRaidView.as_view(), name="spec_detail_raid"),
]
