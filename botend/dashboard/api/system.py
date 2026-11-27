"""Dashboard API submodule: system."""
#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: api.py
@time: 2024/01/15
@desc: Dashboard API Views
'''

from django.views import View
from django.http import (
    JsonResponse, HttpResponse, FileResponse, HttpResponseRedirect,
)
from django.core.serializers.json import DjangoJSONEncoder
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required

import json
import traceback
import hashlib
import math
import time
import re
import requests
import os
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
import platform as py_platform
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
from django.utils import timezone
from django.template.loader import render_to_string

from django.conf import settings
from utils.log import logger
from botend.common.api import (
    ApiError,
    BaseJsonAPIView,
    parse_bool,
    parse_bounded_int,
    parse_int,
    parse_json_body,
    parse_text,
    require_int,
)
from botend.models import MonitorTask, PlayerSpecTopPlayer, PortalPeakSpecRankRow, SimcApl, SimcAplSymbol, SimcAplSymbolScope, SimcTask, SimcTaskFavorite, SimulationRun, SimcTaskArtifact, SimcProfile, SimcTalentString, SimcSecondaryStatRule, SimcMasteryCoefficient, SimcContentTemplate, SimcBackendBinary, SimcSkillDamageSnapshot, SimcAgent, SimcAgentMaintenanceTask, WclAnalysisTask, SystemAlert, WowDailyReport, WowHotfixReport, WowWagoHotfixEvent, WowWagoMonitorState, WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion, WowItemSnapshot, SimcResourceVersion
from botend.alerting import upsert_system_alert
from botend.dashboard.permissions import DashboardPermissionRequiredMixin, has_dashboard_permission
from django.db import IntegrityError, models, transaction
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Length, SHA256, Substr
from core.glm import GLMClient
from botend.monitor_env import is_task_runnable, env_limit_hint
from botend.wow_daily_report.generator import generate_wow_daily_report
from botend.services.simc_attribute_results import parse_attribute_result_filename
from botend.services.simc_player_config import (
    EQUIPMENT_SLOT_ALIASES,
    EQUIPMENT_SLOTS,
    authoritative_player_baseline,
    canonical_simc_profile_identity,
    canonical_simc_profile_key,
    canonical_simc_spec_identity,
    normalize_gear_candidate_value,
    parse_manual_player_config,
    resolve_attribute_player_baseline,
    validate_default_player_baseline,
    validate_player_baseline,
    SUPPORTED_SIMC_SPEC_IDENTITIES,
)
from botend.services.simc_composer import SimcComposer, validate_simulation_options
from botend.services.simc_skill_damage import SimcSkillDamageSnapshotService, reviewed_global_display_effects
from botend.services.simc_skill_source_context import attach_skill_damage_source_context
from botend.services.simc_skill_descriptions import attach_skill_damage_descriptions
from botend.services.simc_skill_snapshot_storage import SkillSnapshotActorStorage
from botend.services.simc_skill_actor_page import (
    ActorPageService, ActorPageError, normalize_query, RESPONSE_BYTES,
)
from botend.wow.talents.service import TalentBuildCodeService
from botend.services.simc_hero_talents import resolve_hero_talent_names
from botend.services.simc_talent_titles import (
    apply_talent_display_names, talent_display_name, frozen_talent_display_names,
)
from botend.services.simc_consumables import simc_consumable_option
from botend.services.simc_benchmark_config import SIMC_RAID_BUFFS
from botend.services.spec_stats_service import SpecStatsService, _hero_subtree_name_from_table
from botend.services.simc_task_service import create_task, create_task_from_request, TaskCreationError
from botend.services.simc_attribute_search import (
    ATTRIBUTE_DPS_TOLERANCE as SIMC_ATTRIBUTE_DPS_TOLERANCE,
    ATTRIBUTE_SEARCH_STEP as SIMC_ATTRIBUTE_SEARCH_STEP,
    ATTRIBUTE_SEARCH_STEPS as SIMC_ATTRIBUTE_SEARCH_STEPS,
    ATTRIBUTE_STATS as SIMC_ATTRIBUTE_STATS,
    attribute_variants,
)
from botend.services.task_rerun import create_rerun, TaskRerunError
from botend.services.battlenet_preflight import fetch_battlenet_character_preflight
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.constants.hero_talents import (
    normalize_hero_subtree_names_zh, normalize_hero_subtree_title_zh,
)
from botend.constants.wow import CLASS_SPEC_MAP, CLASS_CN, CLASS_COLOR, SPEC_CN, SPEC_ICON, SPEC_ROLE
from botend.templatetags.wow_tags import wow_icon_oss_url
from botend.services.simc_apl.catalog import query_symbol_catalog, query_visible_symbols
from botend.services.simc_apl.validation import validate_payload
from botend.services.simc_apl.authoritative_validator import RestrictedSimcValidator
from botend.services.simc_apl.publish import validate_apl_for_profile, content_hash, current_validation_identity
from botend.services.simc_composer import SimcComposer, SIMC_EXTRA_OPTIONS
from botend.services.simc_apl.completion import complete_document
from botend.services.simc_apl.translation import (
    extract_translation_demands, resolve_demand_mappings, translate_apl_ranges,
    TranslationDemand, CONTROL_ACTIONS, disambiguate_chinese_labels,
)
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.db.models.deletion import ProtectedError
from collections import defaultdict, deque

from botend.models import SimcBenchmarkCase, SimcBenchmarkExecution, SimcBenchmarkPanel, SimcBenchmarkPurgeTask
from botend.constants.wow import SPEC_CN
from botend.services.simc_benchmark_purge import (
    PurgeBlocked,
    PurgeConflict,
    PurgeValidation,
    build_panel_purge_plan,
    panel_has_active_purge,
    queue_panel_purge,
)
from botend.services.simc_benchmark_config import (
    MAX_PROFILES_PER_SPEC, MAX_SCENARIOS, MAX_SPECS, SIMC_FIGHT_STYLES,
    SIMC_RAID_BUFFS, benchmark_resource_querysets,
    build_execution_plan, duplicate_panel_config, replace_panel_config, serialize_panel_config,
)
from botend.services.simc_benchmark_execution import (
    BenchmarkExecutionConflict, cancel_execution, create_execution, reconcile_execution,
    rerun_failed_cases, rerun_case, serialize_incremental_panel_results,
    summarize_execution, summarize_incremental_panel_coverage,
    summarize_panel_coverage_counts, task_progress, _canonical_hash,
)
from botend.services.simc_task_service import TaskValidationUnavailable


from ._shared import _admission_busy_response, _fmt_dt, _portal_report_url_from_path, _safe_join_static


@method_decorator([csrf_exempt, login_required], name='dispatch')
class SystemAlertAPIView(DashboardPermissionRequiredMixin, BaseJsonAPIView):
    dashboard_permission = 'system.alerts'
    error_prefix = '系统报警操作失败'

    def get(self, request):
        category = parse_text(request.GET.get('category'))
        show_read = parse_bool(request.GET.get('show_read'))
        page = parse_bounded_int(request.GET.get('page'), default=1, min_value=1)
        page_size = parse_bounded_int(request.GET.get('page_size'), default=20, min_value=1, max_value=100)

        qs = SystemAlert.objects.all()
        if category:
            qs = qs.filter(category=category)
        if not show_read:
            qs = qs.filter(is_read=False)

        total_count = qs.count()
        total_pages = (total_count + page_size - 1) // page_size
        offset = (page - 1) * page_size
        alerts = list(qs.order_by('-last_seen_at')[offset:offset + page_size])
        total_unread = SystemAlert.objects.filter(is_read=False).count()

        return self.ok(
            [
                {
                    'id': a.id,
                    'category': a.category,
                    'subject': a.subject,
                    'dedup_key': a.dedup_key,
                    'level': a.level,
                    'title': a.title,
                    'content': a.content,
                    'count': a.count,
                    'is_read': a.is_read,
                    'first_seen_at': _fmt_dt(a.first_seen_at),
                    'last_seen_at': _fmt_dt(a.last_seen_at),
                }
                for a in alerts
            ],
            total=total_count,
            total_unread=total_unread,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    def post(self, request):
        payload = parse_json_body(request)
        action = parse_text(payload.get('action'))
        now = timezone.now()

        if action == 'mark_read':
            alert_id = require_int(payload.get('id'), error='id参数错误', status=200)
            SystemAlert.objects.filter(id=alert_id).update(is_read=True, read_at=now)
            return self.ok()

        if action == 'mark_all_read':
            category = parse_text(payload.get('category'))
            qs = SystemAlert.objects.filter(is_read=False)
            if category:
                qs = qs.filter(category=category)
            qs.update(is_read=True, read_at=now)
            return self.ok()

        if action == 'delete':
            alert_id = require_int(payload.get('id'), error='id参数错误', status=200)
            SystemAlert.objects.filter(id=alert_id).delete()
            return self.ok()

        if action == 'delete_all_read':
            category = parse_text(payload.get('category'))
            qs = SystemAlert.objects.filter(is_read=True)
            if category:
                qs = qs.filter(category=category)
            qs.delete()
            return self.ok()

        return self.fail('未知操作', status=200)


@method_decorator([csrf_exempt, login_required], name='dispatch')
class PortalPeakSpecRankRefreshAPIView(DashboardPermissionRequiredMixin, BaseJsonAPIView):
    dashboard_permission = 'database.tables'
    error_prefix = '刷新巅峰榜失败'

    def post(self, request):
        from botend.services.monitor_task_execution import monitor_task_execution, MonitorTaskAdmissionBusy
        from LMonitor.config import Monitor_Type_BaseObject_List

        try:
            with monitor_task_execution('PortalPeakSpecRankMonitor') as task:
                plugin_cls = Monitor_Type_BaseObject_List[task.type]
                plugin = plugin_cls(None, task)
                ok = bool(plugin.scan(task.target or ""))
                total = PortalPeakSpecRankRow.objects.filter(is_active=True).count()
            return self.ok(ok=ok, total=total)
        except MonitorTaskAdmissionBusy as e:
            return _admission_busy_response(e)


@method_decorator([csrf_exempt], name='dispatch')
class WagoHotfixReportListAPIView(BaseJsonAPIView):
    error_prefix = '获取 Wago Hotfix 报告列表失败'

    def get(self, request):
        if not request.user.is_authenticated:
            return self.fail('请先登录 Dashboard 后查看 Hotfix 报告', status=401)
        if not has_dashboard_permission(request.user, 'reports.hotfix'):
            return JsonResponse({'status': 'error', 'message': '无权访问该 Dashboard 页面'}, status=403)

        limit = parse_bounded_int(request.GET.get('limit'), default=20, min_value=1, max_value=100)

        state_rows = WowWagoMonitorState.objects.filter(branch='wow').only(
            'id', 'branch', 'locale', 'build', 'hotfix_push_id',
            'hotfix_last_run_at', 'hotfix_last_run_status', 'hotfix_last_event_at',
            'hotfix_last_event_status', 'hotfix_report_url', 'hotfix_wago_url',
            'hotfix_summary_title',
        ).order_by('locale', 'id')
        latest_known_push = max(
            WowHotfixReport.objects.filter(branch='wow').aggregate(v=models.Max('to_push')).get('v') or 0,
            WowWagoHotfixEvent.objects.filter(branch='wow').aggregate(v=models.Max('to_push')).get('v') or 0,
        )
        states = [
            {
                'id': st.id,
                'branch': st.branch,
                'locale': st.locale,
                'build': st.build,
                'hotfix_push_id': st.hotfix_push_id,
                'hotfix_last_run_at': _fmt_dt(st.hotfix_last_run_at),
                'hotfix_last_run_status': st.hotfix_last_run_status,
                'hotfix_last_event_at': _fmt_dt(st.hotfix_last_event_at),
                'hotfix_last_event_status': st.hotfix_last_event_status,
                'hotfix_report_url': _portal_report_url_from_path('', st.hotfix_report_url),
                'hotfix_wago_url': st.hotfix_wago_url,
                'hotfix_summary_title': st.hotfix_summary_title,
                'latest_known_push': latest_known_push,
                'cursor_is_ahead_of_known': bool(latest_known_push and st.hotfix_push_id and st.hotfix_push_id > latest_known_push),
            }
            for st in state_rows
        ]

        reports = [
            {
                'id': r.id,
                'branch': r.branch,
                'locale': r.locale,
                'build_num': r.build_num,
                'build_str': r.build_str,
                'from_push': r.from_push,
                'to_push': r.to_push,
                'summary_title': r.summary_title,
                'report_url': _portal_report_url_from_path(r.content_html_path, r.report_url),
                'wago_url': r.wago_url,
                'table_count': r.table_count,
                'entry_count': r.entry_count,
                'created_at': _fmt_dt(r.created_at),
                'updated_at': _fmt_dt(r.updated_at),
            }
            for r in WowHotfixReport.objects.filter(branch='wow').only(
                'id', 'branch', 'locale', 'build_num', 'build_str', 'from_push', 'to_push',
                'summary_title', 'content_html_path', 'report_url', 'wago_url',
                'table_count', 'entry_count', 'created_at', 'updated_at',
            ).order_by('-created_at')[:limit]
        ]

        events = [
            {
                'id': e.id,
                'branch': e.branch,
                'locale': e.locale,
                'from_push': e.from_push,
                'to_push': e.to_push,
                'push_id': e.push_id,
                'build_num': e.build_num,
                'build_str': e.build_str,
                'status': e.status,
                'wago_url': e.wago_url,
                'report_id': e.report_id,
                'report_url': _portal_report_url_from_path(e.report.content_html_path, e.report.report_url) if e.report_id and e.report else '',
                'table_count': e.table_count,
                'entry_count': e.entry_count,
                'summary_title': e.summary_title,
                'error_message': e.error_message,
                'detected_at': _fmt_dt(e.detected_at),
                'last_attempt_at': _fmt_dt(e.last_attempt_at),
                'updated_at': _fmt_dt(e.updated_at),
            }
            for e in WowWagoHotfixEvent.objects.filter(branch='wow').select_related('report').only(
                'id', 'branch', 'locale', 'from_push', 'to_push', 'push_id', 'build_num',
                'build_str', 'status', 'wago_url', 'report_id', 'report__content_html_path',
                'report__report_url', 'table_count', 'entry_count', 'summary_title',
                'error_message', 'detected_at', 'last_attempt_at', 'updated_at',
            ).order_by('-created_at')[:limit]
        ]

        return self.ok(states=states, reports=reports, events=events)


@method_decorator([csrf_exempt], name='dispatch')
class WagoSkillDiffRerunAPIView(BaseJsonAPIView):
    error_prefix = 'Wago指定版本重跑失败'

    def post(self, request):
        from botend.services.monitor_task_execution import monitor_task_execution, MonitorTaskAdmissionBusy
        from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor

        if not request.user.is_authenticated:
            return self.fail('请先登录 Dashboard 后再执行 Wago 指定版本重跑', status=401)
        if not has_dashboard_permission(request.user, 'tools.wago-rerun'):
            return JsonResponse({'status': 'error', 'message': '无权访问该 Dashboard 页面'}, status=403)

        payload = parse_json_body(request)
        event_id = payload.get('event_id')
        branch = parse_text(payload.get('branch'), 'wow')
        from_build = parse_text(payload.get('from_build'))
        to_build = parse_text(payload.get('to_build'))
        locale = parse_text(payload.get('locale'), 'enUS')

        if not event_id and (not from_build or not to_build):
            return self.fail('请填写 event_id 或 from_build/to_build', status=200)

        try:
            with monitor_task_execution('WagoSkillDiffMonitor') as task:
                monitor = WagoSkillDiffMonitor(None, task)
                if event_id:
                    result = monitor.rerun_build_event(event_id=event_id)
                else:
                    result = monitor.rerun_build_diff(branch=branch, from_build=from_build, to_build=to_build, locale=locale)
            return JsonResponse(result)
        except MonitorTaskAdmissionBusy as e:
            return _admission_busy_response(e)


@method_decorator([csrf_exempt, login_required], name='dispatch')
def _lookup_wow_daily_report(request):
    rid = parse_int(request.GET.get("id"))
    if rid is not None:
        row = WowDailyReport.objects.filter(id=rid).first()
        if row:
            return row
    date_s = parse_text(request.GET.get("date"))
    if date_s:
        try:
            return WowDailyReport.objects.filter(report_date=date_s).first()
        except ValidationError:
            return None
    return None


class WowDailyReportListAPIView(DashboardPermissionRequiredMixin, BaseJsonAPIView):
    dashboard_permission = 'reports.wow-daily'
    error_prefix = '获取WoW日报列表失败'

    def get(self, request):
        limit = parse_bounded_int(request.GET.get("limit"), default=30, min_value=1, max_value=200)
        rows = WowDailyReport.objects.only(
            'id', 'report_date', 'md_path', 'updated_at',
        ).order_by('-report_date', '-updated_at', '-id')[:limit]
        data = [
            {
                "id": r.id,
                "report_date": r.report_date.isoformat() if r.report_date else "",
                "md_path": r.md_path or "",
                "portal_url": _portal_report_url_from_path(r.md_path or ""),
                "updated_at": _fmt_dt(r.updated_at),
            }
            for r in rows
        ]
        return self.ok(data, total=len(data))


@method_decorator([csrf_exempt, login_required], name='dispatch')
class WowDailyReportContentAPIView(DashboardPermissionRequiredMixin, BaseJsonAPIView):
    dashboard_permission = 'reports.wow-daily'
    error_prefix = '获取WoW日报内容失败'

    def get(self, request):
        row = _lookup_wow_daily_report(request)
        if not row:
            return self.fail("未找到日报记录", status=200)
        md_path = (row.md_path or "").strip()
        full = _safe_join_static(md_path)
        if not full or not os.path.exists(full):
            return self.fail("日报文件不存在", status=200)
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
        report_format = "html" if md_path.lower().endswith(".html") else "markdown"
        return self.ok(
            {
                "id": row.id,
                "report_date": row.report_date.isoformat() if row.report_date else "",
                "md_path": md_path,
                "format": report_format,
                "updated_at": _fmt_dt(row.updated_at),
                "content": content,
            }
        )


@method_decorator([csrf_exempt, login_required], name='dispatch')
class WowDailyReportDownloadAPIView(DashboardPermissionRequiredMixin, BaseJsonAPIView):
    dashboard_permission = 'reports.wow-daily'
    error_prefix = '下载WoW日报失败'

    def get(self, request):
        row = _lookup_wow_daily_report(request)
        if not row:
            return self.fail("未找到日报记录", status=200)
        md_path = (row.md_path or "").strip()
        full = _safe_join_static(md_path)
        if not full or not os.path.exists(full):
            return self.fail("日报文件不存在", status=200)
        with open(full, "rb") as f:
            content = f.read()
        filename = os.path.basename(md_path) or "wow_daily_report.md"
        resp = HttpResponse(content, content_type="application/octet-stream")
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp


@method_decorator(login_required, name='dispatch')
class WowDailyReportGenerateAPIView(DashboardPermissionRequiredMixin, BaseJsonAPIView):
    dashboard_permission = 'reports.wow-daily'
    error_prefix = '生成WoW日报失败'

    def post(self, request):
        meta = generate_wow_daily_report(report_date=timezone.localdate(), use_llm=True)
        ext = meta.get("ext") if isinstance(meta, dict) else {}
        llm_errors = ext.get("llm_errors") or [] if isinstance(ext, dict) else []
        first_err = ""
        if isinstance(llm_errors, list) and llm_errors and isinstance(llm_errors[0], dict):
            first_err = str(llm_errors[0].get("error") or "")
        return self.ok(
            {
                "md_path": meta.get("md_path"),
                "llm_ok": not bool(llm_errors),
                "llm_error": first_err,
            }
        )


class OssConfigAPIView(View):
    """
    OSS配置API
    """
    
    def get(self, request):
        """获取OSS配置信息"""
        try:
            from django.conf import settings
            oss_config = getattr(settings, 'OSS_CONFIG', {})
            
            # 只返回前端需要的配置信息，不暴露敏感信息
            return JsonResponse({
                'success': True,
                'data': {
                    'base_url': oss_config.get('base_url', '')
                }
            })
            
        except Exception as e:
            logger.error(f"获取OSS配置错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'获取OSS配置失败: {str(e)}'
            })


