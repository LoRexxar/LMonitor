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
from django.http import JsonResponse, HttpResponse, FileResponse, HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required

import json
import traceback
import hashlib
import time
import re
import requests
import os
import subprocess
import threading
import uuid
import platform as py_platform
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from django.utils import timezone
from django.template.loader import render_to_string

from django.conf import settings
from utils.log import logger
from botend.models import MonitorTask, PlayerSpecTopPlayer, PortalPeakSpecRankRow, SimcApl, SimcAplSymbol, SimcTask, SimulationRun, SimcTaskArtifact, SimcProfile, SimcSecondaryStatRule, SimcMasteryCoefficient, SimcContentTemplate, SimcBackendBinary, SimcAgent, SimcAgentMaintenanceTask, WclAnalysisTask, SystemAlert, WowDailyReport, WowHotfixReport, WowWagoHotfixEvent, WowWagoMonitorState, WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion, WowItemSnapshot, SimcResourceVersion
from botend.alerting import upsert_system_alert
from botend.dashboard.permissions import DashboardPermissionRequiredMixin, has_dashboard_permission
from django.db import IntegrityError, models, transaction
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
from botend.services.simc_benchmark_config import SIMC_RAID_BUFFS
from botend.services.spec_stats_service import SpecStatsService
from botend.services.simc_task_service import create_task, create_task_from_request, TaskCreationError
from botend.services.simc_attribute_search import (
    ATTRIBUTE_DPS_TOLERANCE as SIMC_ATTRIBUTE_DPS_TOLERANCE,
    ATTRIBUTE_SEARCH_STEP as SIMC_ATTRIBUTE_SEARCH_STEP,
    ATTRIBUTE_STATS as SIMC_ATTRIBUTE_STATS,
    attribute_variants,
)
from botend.services.task_rerun import create_rerun, TaskRerunError
from botend.services.battlenet_preflight import fetch_battlenet_character_preflight
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.constants.wow import CLASS_SPEC_MAP, CLASS_CN, CLASS_COLOR, SPEC_CN, SPEC_ICON, SPEC_ROLE
from botend.services.simc_apl.catalog import query_symbol_catalog
from botend.services.simc_apl.validation import validate_payload
from botend.services.simc_apl.authoritative_validator import RestrictedSimcValidator
from botend.services.simc_apl.publish import validate_apl_for_profile, content_hash, current_validation_identity
from botend.services.simc_composer import SimcComposer
from botend.services.simc_apl.completion import complete_document
from botend.services.simc_apl.translation import (
    extract_translation_demands, resolve_demand_mappings, translate_apl_ranges,
    TranslationDemand, CONTROL_ACTIONS, disambiguate_chinese_labels,
)
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.db.models.deletion import ProtectedError
from collections import defaultdict, deque

from botend.models import SimcBenchmarkCase, SimcBenchmarkExecution, SimcBenchmarkPanel
from botend.constants.wow import SPEC_CN
from botend.services.simc_benchmark_config import (
    MAX_PROFILES_PER_SPEC, MAX_SCENARIOS, MAX_SPECS, SIMC_FIGHT_STYLES,
    SIMC_RAID_BUFFS, benchmark_resource_querysets,
    replace_panel_config, serialize_panel_config,
)
from botend.services.simc_benchmark_execution import (
    BenchmarkExecutionConflict, cancel_execution, create_execution, reconcile_execution,
    rerun_failed_cases, serialize_incremental_panel_results,
    summarize_execution, summarize_incremental_panel_coverage,
    summarize_panel_coverage_counts, task_progress, _canonical_hash,
)
from botend.services.simc_task_service import TaskValidationUnavailable


def _accessible_simc_profile_q(user):
    """Profiles usable in read/execute flows under the product-admin policy."""
    if _is_simc_admin(user):
        return models.Q()
    return (
        models.Q(user_id=user.id)
        | models.Q(
            user_id__isnull=True,
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
        )
    )


def _simc_spec_options():
    """统一返回所有 SimC 资源使用的 class_spec 专精标识。"""
    rows = []
    for class_name, specs in CLASS_SPEC_MAP.items():
        class_key = re.sub(r'(?<!^)(?=[A-Z])', '_', class_name).lower()
        class_key = {'death_knight': 'deathknight', 'demon_hunter': 'demonhunter'}.get(class_key, class_key)
        for spec_name in specs:
            spec_key = re.sub(r'(?<!^)(?=[A-Z])', '_', spec_name).lower()
            rows.append({
                'value': f'{class_key}_{spec_key}',
                'class_name': class_key,
                'class_label': CLASS_CN.get(class_name, class_name),
                'spec_label': SPEC_CN.get(spec_name, spec_name),
                'label': f'{CLASS_CN.get(class_name, class_name)} · {SPEC_CN.get(spec_name, spec_name)}',
            })
    return rows


SIMC_SPEC_OPTIONS = _simc_spec_options()
SIMC_SPEC_VALUES = frozenset(row['value'] for row in SIMC_SPEC_OPTIONS)
SIMC_SPEC_LABELS = {row['value']: row['spec_label'] for row in SIMC_SPEC_OPTIONS}
SIMC_SPEC_CLASS_NAMES = {row['value']: row['class_name'] for row in SIMC_SPEC_OPTIONS}
SIMC_SPEC_CLASS_LABELS = {row['value']: row['class_label'] for row in SIMC_SPEC_OPTIONS}
SIMC_CLASS_DB_NAMES = {
    re.sub(r'(?<!^)(?=[A-Z])', '_', class_name).lower()
    .replace('death_knight', 'deathknight')
    .replace('demon_hunter', 'demonhunter'): class_name
    for class_name in CLASS_SPEC_MAP
}
SIMC_SPEC_DB_IDENTITIES = {
    f'{class_key}_{re.sub(r"(?<!^)(?=[A-Z])", "_", spec_name).lower()}': (db_class_name, spec_name)
    for class_key, db_class_name in SIMC_CLASS_DB_NAMES.items()
    for spec_name in CLASS_SPEC_MAP[db_class_name]
}


def _canonical_simc_spec(value):
    normalized = str(value or '').strip().lower()
    return normalized if normalized in SIMC_SPEC_VALUES else None


def _simc_spec_label(spec, class_name=''):
    """Return a Chinese display label without changing the stable SimC key."""
    normalized = canonical_simc_profile_key(spec, class_name) or str(spec or '').strip().lower()
    if normalized in ('', 'default', 'all', '*'):
        return '通用' if normalized else '未标记'
    if normalized in SIMC_SPEC_LABELS:
        return SIMC_SPEC_LABELS[normalized]
    suffix_labels = {
        label
        for key, label in SIMC_SPEC_LABELS.items()
        if key.endswith(f'_{normalized}')
    }
    if len(suffix_labels) == 1:
        return suffix_labels.pop()
    return str(spec or '').strip() or '未标记'


def _simc_class_label(spec, class_name=''):
    """Return the canonical Chinese class label for a SimC resource identity."""
    normalized = canonical_simc_profile_key(spec, class_name) or str(spec or '').strip().lower()
    if normalized in ('', 'default', 'all', '*'):
        return '通用职业'
    if normalized in SIMC_SPEC_CLASS_LABELS:
        return SIMC_SPEC_CLASS_LABELS[normalized]
    canonical_class = str(class_name or '').strip().lower().replace('_', '')
    for key, db_name in SIMC_CLASS_DB_NAMES.items():
        if canonical_class in (key.replace('_', ''), db_name.lower()):
            return CLASS_CN.get(db_name, db_name)
    return str(class_name or '').strip() or '通用职业'


def _simc_spec_visual(spec, class_name=''):
    """Resolve the authoritative specialization icon and Blizzard class color."""
    canonical = canonical_simc_profile_key(spec, class_name)
    identity = SIMC_SPEC_DB_IDENTITIES.get(canonical or '')
    if identity:
        db_class_name, db_spec_name = identity
    else:
        db_class_name = db_spec_name = None
    icon_url = SPEC_ICON.get((db_class_name, db_spec_name), '') if db_class_name and db_spec_name else ''
    return {
        'spec_icon_url': icon_url,
        'class_color': CLASS_COLOR.get(db_class_name, '#94A3B8') if db_class_name else '#94A3B8',
    }


def _simc_class_for_spec(spec):
    return SIMC_SPEC_CLASS_NAMES.get(spec)


def _is_simc_admin(user):
    return bool(user.is_staff or user.is_superuser)


def _fmt_dt(dt):
    if not dt:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_default_timezone())
    return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M:%S')


def _static_root():
    base_dir = str(getattr(settings, "BASE_DIR", "") or "")
    if base_dir:
        return os.path.join(base_dir, "static")
    return os.path.join(os.getcwd(), "static")


def _safe_join_static(rel_path):
    raw = str(rel_path or '').replace('\\', '/')
    if not raw or raw.startswith('/'):
        return None
    try:
        root = Path(_static_root()).resolve(strict=True)
        full = (root / raw).resolve(strict=False)
        if os.path.commonpath((str(root), str(full))) != str(root):
            return None
        return str(full)
    except (OSError, RuntimeError, ValueError):
        return None


def _portal_report_url_from_path(content_html_path, fallback_url=''):
    rel_path = str(content_html_path or '').strip().lstrip('/')
    if rel_path.startswith('static/'):
        rel_path = rel_path[len('static/'):]
    if rel_path.startswith('portal/reports/'):
        rel_path = rel_path[len('portal/reports/'):]
    if rel_path:
        return f'/portal/reports/{rel_path}'

    url = str(fallback_url or '').strip()
    if url.startswith('/static/portal/reports/'):
        return '/portal/reports/' + url[len('/static/portal/reports/'):]
    return url


@method_decorator([csrf_exempt, login_required], name='dispatch')
class SystemAlertAPIView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'system.alerts'

    def get(self, request):
        try:
            limit = request.GET.get('limit', '20')
            try:
                limit = max(1, min(100, int(limit)))
            except ValueError:
                limit = 20

            category = (request.GET.get('category') or '').strip()
            show_read = request.GET.get('show_read', '').strip().lower() in ('1', 'true', 'yes')
            page = max(1, int(request.GET.get('page', '1')))
            page_size = max(1, min(100, int(request.GET.get('page_size', '20'))))

            qs = SystemAlert.objects.all()
            if category:
                qs = qs.filter(category=category)
            if not show_read:
                qs = qs.filter(is_read=False)

            total_count = qs.count()
            total_pages = (total_count + page_size - 1) // page_size
            offset = (page - 1) * page_size
            alerts = list(qs.order_by('-last_seen_at')[offset:offset + page_size])

            unread_qs = SystemAlert.objects.filter(is_read=False)
            total_unread = unread_qs.count()

            return JsonResponse({
                'success': True,
                'data': [
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
                'total': total_count,
                'total_unread': total_unread,
                'page': page,
                'page_size': page_size,
                'total_pages': total_pages,
            })
        except Exception as e:
            logger.error(f"获取系统报警失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({'success': False, 'error': f'获取系统报警失败: {str(e)}'})

    def post(self, request):
        try:
            payload = json.loads(request.body or '{}')
            action = (payload.get('action') or '').strip()
            now = timezone.now()

            if action == 'mark_read':
                alert_id = payload.get('id')
                try:
                    alert_id = int(alert_id)
                except Exception:
                    return JsonResponse({'success': False, 'error': 'id参数错误'})
                SystemAlert.objects.filter(id=alert_id).update(is_read=True, read_at=now)
                return JsonResponse({'success': True})

            if action == 'mark_all_read':
                category = (payload.get('category') or '').strip()
                qs = SystemAlert.objects.filter(is_read=False)
                if category:
                    qs = qs.filter(category=category)
                qs.update(is_read=True, read_at=now)
                return JsonResponse({'success': True})

            if action == 'delete':
                alert_id = payload.get('id')
                try:
                    alert_id = int(alert_id)
                except Exception:
                    return JsonResponse({'success': False, 'error': 'id参数错误'})
                SystemAlert.objects.filter(id=alert_id).delete()
                return JsonResponse({'success': True})

            if action == 'delete_all_read':
                category = (payload.get('category') or '').strip()
                qs = SystemAlert.objects.filter(is_read=True)
                if category:
                    qs = qs.filter(category=category)
                qs.delete()
                return JsonResponse({'success': True})

            return JsonResponse({'success': False, 'error': '未知操作'})
        except Exception as e:
            logger.error(f"更新系统报警状态失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({'success': False, 'error': f'更新系统报警状态失败: {str(e)}'})


@method_decorator([csrf_exempt, login_required], name='dispatch')
class PortalPeakSpecRankRefreshAPIView(View):
    def post(self, request):
        try:
            task = MonitorTask.objects.filter(name="PortalPeakSpecRankMonitor").first()
            if not task:
                return JsonResponse({'success': False, 'error': '未找到 PortalPeakSpecRankMonitor 任务，请先执行 SyncMonitorTasksFromPlugins'})
            if not is_task_runnable(task):
                return JsonResponse(
                    {
                        'success': False,
                        'error': env_limit_hint(getattr(task, "env_limit", 0)),
                        'code': 'env_limit_blocked',
                        'env_limit': int(getattr(task, "env_limit", 0) or 0),
                    }
                )
            from LMonitor.config import Monitor_Type_BaseObject_List

            task_type = int(getattr(task, "type", 0) or 0)
            if task_type < 0 or task_type >= len(Monitor_Type_BaseObject_List):
                return JsonResponse({'success': False, 'error': '任务 type 无效'})
            plugin_cls = Monitor_Type_BaseObject_List[task_type]
            plugin = plugin_cls(None, task)
            ok = bool(plugin.scan(getattr(task, "target", "") or ""))
            total = PortalPeakSpecRankRow.objects.filter(is_active=True).count()
            return JsonResponse({'success': True, 'ok': ok, 'total': total})
        except Exception as e:
            logger.error(f"刷新巅峰榜失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({'success': False, 'error': f'刷新巅峰榜失败: {str(e)}'})



@method_decorator([csrf_exempt], name='dispatch')
class WagoHotfixReportListAPIView(View):
    def get(self, request):
        try:
            if not getattr(request, 'user', None) or not request.user.is_authenticated:
                return JsonResponse({'success': False, 'error': '请先登录 Dashboard 后查看 Hotfix 报告'}, status=401)
            if not has_dashboard_permission(request.user, 'reports.hotfix'):
                return JsonResponse({'status': 'error', 'message': '无权访问该 Dashboard 页面'}, status=403)

            limit_raw = request.GET.get('limit', '20')
            try:
                limit = max(1, min(100, int(limit_raw)))
            except Exception:
                limit = 20

            state_rows = WowWagoMonitorState.objects.filter(branch='wow').order_by('locale', 'id')
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
                for r in WowHotfixReport.objects.filter(branch='wow').order_by('-created_at')[:limit]
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
                for e in WowWagoHotfixEvent.objects.filter(branch='wow').select_related('report').order_by('-created_at')[:limit]
            ]

            return JsonResponse({'success': True, 'states': states, 'reports': reports, 'events': events})
        except Exception as e:
            logger.error(f"获取 Wago Hotfix 报告列表失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({'success': False, 'error': f'获取 Wago Hotfix 报告列表失败: {str(e)}'})


@method_decorator([csrf_exempt], name='dispatch')
class WagoSkillDiffRerunAPIView(View):
    def post(self, request):
        try:
            if not getattr(request, 'user', None) or not request.user.is_authenticated:
                return JsonResponse({'success': False, 'error': '请先登录 Dashboard 后再执行 Wago 指定版本重跑'}, status=401)
            if not has_dashboard_permission(request.user, 'tools.wago-rerun'):
                return JsonResponse({'status': 'error', 'message': '无权访问该 Dashboard 页面'}, status=403)

            payload = json.loads(request.body or '{}')
            event_id = payload.get('event_id')
            branch = (payload.get('branch') or 'wow').strip()
            from_build = (payload.get('from_build') or '').strip()
            to_build = (payload.get('to_build') or '').strip()
            locale = (payload.get('locale') or 'enUS').strip()

            if not event_id and (not from_build or not to_build):
                return JsonResponse({'success': False, 'error': '请填写 event_id 或 from_build/to_build'})

            from LMonitor.config import Monitor_Type_BaseObject_List
            from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor

            task = MonitorTask.objects.filter(name='WagoSkillDiffMonitor').first()
            if not task:
                try:
                    idx = Monitor_Type_BaseObject_List.index(WagoSkillDiffMonitor)
                    task = MonitorTask.objects.filter(type=idx).order_by('id').first()
                except ValueError:
                    task = None
            if not task:
                return JsonResponse({'success': False, 'error': '未找到 WagoSkillDiffMonitor 任务，请先同步 MonitorTask'})

            monitor = WagoSkillDiffMonitor(None, task)
            if event_id:
                result = monitor.rerun_build_event(event_id=event_id)
            else:
                result = monitor.rerun_build_diff(branch=branch, from_build=from_build, to_build=to_build, locale=locale)
            return JsonResponse(result)
        except Exception as e:
            logger.error(f"Wago指定版本重跑失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({'success': False, 'error': f'Wago指定版本重跑失败: {str(e)}'})


_APL_EDITOR_RATE_BUCKETS = defaultdict(deque)
_APL_EDITOR_RATE_LOCK = threading.Lock()
_APL_EDITOR_RATE_BUCKET_SOFT_LIMIT = 1024


class _EditorSemaphore:
    def __init__(self, value):
        self._semaphore = threading.BoundedSemaphore(value)

    def acquire(self):
        return self._semaphore.acquire(blocking=False)

    def release(self):
        self._semaphore.release()


_APL_EDITOR_SEMAPHORE = _EditorSemaphore(4)


def _editor_error(code, message, status=400):
    return JsonResponse({'success': False, 'error': {'code': code, 'message': message}}, status=status)


def _editor_spec(raw):
    canonical = _canonical_simc_spec(raw)
    if not canonical:
        return None
    class_name = _simc_class_for_spec(canonical)
    return canonical, class_name, canonical[len(class_name) + 1:]


def _latest_catalog_identity():
    configured = getattr(settings, 'SIMC_APL_CURRENT_IDENTITY', None)
    if configured and len(configured) == 2:
        revision, build = (str(value or '').strip() for value in configured)
        if re.fullmatch(r'[0-9a-f]{40}', revision) and build:
            return revision, build
        return None
    platform = 'linuxarm64' if 'aarch64' in py_platform.machine().lower() else 'linux64'
    # A seeded/placeholder backend has an empty version and is not a catalog
    # identity.  There can be more than one backend per platform, so selecting
    # the first row would hide a valid running backend behind that placeholder.
    versions = list(SimcBackendBinary.objects.filter(
        platform=platform,
        is_active=True,
    ).exclude(current_version='').values_list('current_version', flat=True))
    # Several backend rows may report the same runtime revision.  This is one
    # catalog identity, not an ambiguity.  Distinct revisions do remain
    # fail-closed because choosing one could expose the wrong symbol catalog.
    versions = sorted(set(versions))
    if len(versions) != 1:
        return None
    current = versions[0]

    revision = current if re.fullmatch(r'[0-9a-f]{40}', current) else None
    catalog = SimcAplSymbol.objects.filter(is_active=True)
    if revision:
        catalog = catalog.filter(simc_revision=revision)
    else:
        suffix = re.search(r'(?:^|-)([0-9a-f]{7,39})$', current)
        if not suffix:
            return None
        catalog = catalog.filter(simc_revision__startswith=suffix.group(1))

    identities = list(catalog.order_by().values_list(
        'simc_revision', 'wow_build',
    ).distinct()[:2])
    if len(identities) != 1:
        return None
    revision, build = identities[0]
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        return None
    return revision, build


def _authoritative_action_bindings(parsed_spec, identity):
    """Resolve each visible action token to one authoritative SpellID.

    The visible token set remains global + current class + current spec.  A
    token that has no SpellID there may borrow a binding only from the exact
    same token in another spec of the same class.  Conflicting bindings are
    left unresolved rather than guessed.
    """
    if not parsed_spec or not identity:
        return {}
    _, class_name, spec_name = parsed_spec
    base = SimcAplSymbol.objects.filter(
        simc_revision=identity[0], wow_build=identity[1], is_active=True,
        symbol_kind=SimcAplSymbol.KIND_ACTION, hero_tree__isnull=True,
    ).exclude(token='').exclude(token__in=CONTROL_ACTIONS)
    visible_scope = (
        models.Q(class_name__isnull=True, spec__isnull=True) |
        models.Q(class_name=class_name, spec__isnull=True) |
        models.Q(class_name=class_name, spec=spec_name)
    )
    visible_rows = list(base.filter(visible_scope).values_list(
        'token', 'spell_id', 'class_name', 'spec',
    ))
    visible_tokens = {token for token, _spell_id, _class, _spec in visible_rows}
    visible_spell_ids = {}
    candidates = {}
    for token, spell_id, row_class, row_spec in visible_rows:
        if not spell_id:
            continue
        visible_spell_ids.setdefault(token, set()).add(spell_id)
        rank = 2 if row_class == class_name and row_spec == spec_name else (
            1 if row_class == class_name and row_spec is None else 0
        )
        current_rank, current_ids = candidates.get(token, (-1, set()))
        if rank > current_rank:
            candidates[token] = (rank, {spell_id})
        elif rank == current_rank:
            current_ids.add(spell_id)

    bindings = {}
    for token in visible_tokens:
        # Different SpellIDs for one visible token are an authoritative
        # conflict even when they occur at different scope ranks.
        if len(visible_spell_ids.get(token, ())) > 1:
            continue
        rank, spell_ids = candidates.get(token, (-1, set()))
        if len(spell_ids) == 1:
            bindings[token] = (next(iter(spell_ids)), 'visible_scope')

    # A visible token with conflicting SpellIDs must remain unresolved; it is
    # not eligible for same-class fallback.
    conflicts = {token for token, spell_ids in visible_spell_ids.items() if len(spell_ids) > 1}
    missing = {token for token in visible_tokens if token not in bindings and token not in conflicts}
    fallback_rows = base.filter(
        class_name=class_name, token__in=missing, spell_id__isnull=False,
    ).values_list('token', 'spell_id')
    fallback_ids = {}
    for token, spell_id in fallback_rows:
        if token not in missing:
            continue
        fallback_ids.setdefault(token, set()).add(spell_id)
    for token, spell_ids in fallback_ids.items():
        if len(spell_ids) == 1:
            bindings[token] = (next(iter(spell_ids)), 'same_class_exact_token')
    return bindings


def _authoritative_action_tokens(spell_ids, parsed_spec, identity):
    """Return the visible authoritative tokens grouped by requested SpellID."""
    requested = set(spell_ids or ())
    selected = {}
    for token, (spell_id, _source) in _authoritative_action_bindings(
            parsed_spec, identity).items():
        if spell_id in requested:
            selected.setdefault(spell_id, set()).add(token)
    return {spell_id: tuple(sorted(tokens)) for spell_id, tokens in selected.items()}


def _catalog_item(item, simc_revision='', game_build=''):
    scope = 'hero_tree' if item.hero_tree else ('spec' if item.spec else ('class' if item.class_name else 'global'))
    return {
        'token': item.token, 'kind': item.kind, 'scope': scope, 'spell_id': item.spell_id,
        'name_zh': item.name, 'name_en': item.name_en, 'description_zh': item.description,
        'icon': item.icon, 'insertable': item.insertable, 'reason': item.reason,
        'source': item.source, 'simc_revision': simc_revision, 'game_build': game_build,
    }


class SimcAplEditorAPIView(View):
    """Authenticated, bounded JSON protocol shared by editor endpoints."""

    def dispatch(self, request, *args, **kwargs):
        if not getattr(request, 'user', None) or not request.user.is_authenticated:
            return _editor_error('authentication_required', 'Authentication required.', 401)
        return super().dispatch(request, *args, **kwargs)

    def http_method_not_allowed(self, request, *args, **kwargs):
        return _editor_error('method_not_allowed', 'HTTP method is not allowed.', 405)

    @staticmethod
    def payload(request):
        try:
            value = json.loads(request.body or b'{}')
        except (TypeError, ValueError, UnicodeDecodeError):
            raise SuspiciousOperation('invalid_json')
        if not isinstance(value, dict):
            raise SuspiciousOperation('invalid_json')
        return value

    @staticmethod
    def content(payload):
        value = payload.get('content', '')
        if not isinstance(value, str):
            return None, _editor_error('invalid_content', 'content must be a string.')
        maximum = int(getattr(settings, 'SIMC_APL_EDITOR_MAX_CONTENT_LENGTH', 200000))
        if len(value) > maximum:
            return None, _editor_error('content_too_large', 'Document exceeds the size limit.', 413)
        return value, None

    def rate_limit(self, request):
        limit = int(getattr(settings, 'SIMC_APL_EDITOR_RATE_LIMIT', 60))
        window = float(getattr(settings, 'SIMC_APL_EDITOR_RATE_WINDOW', 60))
        now = time.monotonic()
        key = (request.user.id, self.__class__.__name__)
        with _APL_EDITOR_RATE_LOCK:
            for stale_key, stale_bucket in list(_APL_EDITOR_RATE_BUCKETS.items()):
                while stale_bucket and stale_bucket[0] <= now - window:
                    stale_bucket.popleft()
                if not stale_bucket:
                    del _APL_EDITOR_RATE_BUCKETS[stale_key]
            if len(_APL_EDITOR_RATE_BUCKETS) >= _APL_EDITOR_RATE_BUCKET_SOFT_LIMIT:
                return _editor_error('rate_limited', 'Request rate limit exceeded.', 429)
            bucket = _APL_EDITOR_RATE_BUCKETS[key]
            if len(bucket) >= limit:
                return _editor_error('rate_limited', 'Request rate limit exceeded.', 429)
            bucket.append(now)
        return None


class SimcAplValidationAPIView(SimcAplEditorAPIView):
    def post(self, request):
        try:
            payload = self.payload(request)
        except SuspiciousOperation:
            return _editor_error('invalid_json', 'Request body must be a JSON object.')
        content, error = self.content(payload)
        if error:
            return error
        limited = self.rate_limit(request)
        if limited:
            return limited
        spec = _editor_spec(payload.get('spec'))
        if not spec:
            return _editor_error('invalid_spec', 'Unknown specialization.')
        profile_id = payload.get('profile_id')
        profile = None
        if profile_id is not None:
            profile = SimcProfile.objects.filter(id=profile_id, user_id=request.user.id, is_active=True).first()
            if not profile:
                return _editor_error('profile_not_found', 'Profile not found.', 404)
            profile_spec = _editor_spec(profile.spec)
            if not profile_spec or profile_spec[0] != spec[0]:
                return _editor_error('profile_spec_mismatch', 'Profile specialization does not match the validation context.')
        mode = payload.get('mode', 'structural')
        if mode not in ('structural', 'authoritative', 'both'):
            return _editor_error('invalid_mode', 'Unknown validation mode.')
        try:
            diagnostic_page = max(1, int(payload.get('diagnostic_page', 1)))
            page_size = max(1, min(100, int(payload.get('page_size', 50))))
        except (TypeError, ValueError):
            return _editor_error('invalid_pagination', 'diagnostic_page and page_size must be integers.')
        if not _APL_EDITOR_SEMAPHORE.acquire():
            return _editor_error('concurrency_limited', 'Validation capacity is busy.', 429)
        started = time.monotonic()
        try:
            validator = None
            validation_context = None
            if mode in ('authoritative', 'both'):
                if profile is None:
                    profiles = list(SimcProfile.objects.filter(
                        user_id=request.user.id, spec=spec[1], is_active=True)[:2])
                    if len(profiles) == 1:
                        profile = profiles[0]
                identity = _latest_catalog_identity()
                platform = 'linuxarm64' if 'aarch64' in py_platform.machine().lower() else 'linux64'
                backend = SimcBackendBinary.objects.filter(platform=platform).first()
                if profile is not None and identity and backend:
                    composer = SimcComposer(request.user.id)
                    try:
                        validation_input = composer.compose_validation_input(profile, content)
                    except (ValueError, TypeError, AttributeError):
                        validation_input = None
                    if validation_input is not None:
                        validation_context = SimcComposer.validation_context(
                            profile, catalog_revision=identity[0],
                            binary_revision=backend.current_version,
                            validation_input=validation_input)
                        validator = RestrictedSimcValidator(
                            backend.simc_path, catalog_revision=identity[0],
                            binary_revision=backend.current_version,
                            temp_root=getattr(settings, 'SIMC_APL_VALIDATION_TEMP_ROOT', None))
            data = validate_payload(content, mode=mode,
                                    authoritative_validator=validator,
                                    validation_context=validation_context)
        finally:
            _APL_EDITOR_SEMAPHORE.release()
        total = len(data['diagnostics'])
        start = (diagnostic_page - 1) * page_size
        data['diagnostics'] = data['diagnostics'][start:start + page_size]
        data['pagination'] = {'page': diagnostic_page, 'page_size': page_size, 'total': total,
                              'total_pages': (total + page_size - 1) // page_size}
        data['document_version'] = payload.get('document_version')
        data['context'] = {'spec': spec[0], 'profile_id': profile.id if profile else None}
        data['elapsed_ms'] = int((time.monotonic() - started) * 1000)
        return JsonResponse({'success': True, 'data': data})


class SimcAplSymbolsAPIView(SimcAplEditorAPIView):
    def get(self, request):
        limited = self.rate_limit(request)
        if limited:
            return limited
        spec = _editor_spec(request.GET.get('spec'))
        if not spec:
            return _editor_error('invalid_spec', 'Unknown specialization.')
        try:
            page = int(request.GET.get('page', 1))
            page_size = int(request.GET.get('page_size', 50))
        except (TypeError, ValueError):
            return _editor_error('invalid_pagination', 'Invalid pagination parameters.')
        if page < 1 or page_size < 1:
            return _editor_error('invalid_pagination', 'Invalid pagination parameters.')
        page_size = min(100, page_size)
        identity = _latest_catalog_identity()
        if not identity:
            return _editor_error('catalog_unavailable', 'The current symbol catalog is unavailable.', 503)
        revision, build = identity
        if not _APL_EDITOR_SEMAPHORE.acquire():
            return _editor_error('concurrency_limited', 'Symbol query capacity is busy.', 429)
        kind = (request.GET.get('kind') or '').strip()
        query = (request.GET.get('query') or '').strip()[:200]
        try:
            items = query_symbol_catalog(revision, build, spec[1], spec[2], search=query or None)
        finally:
            _APL_EDITOR_SEMAPHORE.release()
        if kind:
            items = [item for item in items if item.kind == kind]
        total = len(items)
        total_pages = (total + page_size - 1) // page_size
        start = (page - 1) * page_size
        return JsonResponse({'success': True, 'data': {
            'items': [_catalog_item(item, revision, build) for item in items[start:start + page_size]],
            'pagination': {'page': page, 'page_size': page_size, 'total': total, 'total_pages': total_pages},
        }})


class SimcAplSpellsAPIView(SimcAplEditorAPIView):
    """Bilingual actions for the current authoritative spec catalog."""

    def get(self, request):
        spec = _editor_spec(request.GET.get('spec'))
        if not spec:
            return _editor_error('invalid_spec', 'Unknown specialization.')
        try:
            page = int(request.GET.get('page', 1))
            page_size = int(request.GET.get('page_size', 50))
        except (TypeError, ValueError):
            return _editor_error('invalid_pagination', 'Invalid pagination parameters.')
        if page < 1 or page_size < 1:
            return _editor_error('invalid_pagination', 'Invalid pagination parameters.')
        page_size = min(100, page_size)
        identity = _latest_catalog_identity()
        if not identity:
            return _editor_error('catalog_unavailable', 'The current symbol catalog is unavailable.', 503)

        revision, build = identity
        bindings = _authoritative_action_bindings(spec, identity)
        spell_ids = {spell_id for spell_id, _source in bindings.values()}
        localized = {
            row['spell_id']: row
            for row in WowSpellSnapshot.objects.filter(
                branch='wow', locale='zhCN', snapshot_build=build,
                spell_id__in=spell_ids,
            ).exclude(name='').exclude(name_zh='').values(
                'spell_id', 'name', 'name_zh',
            ).order_by('spell_id', '-updated_at', '-id')
        }
        items = []
        for token, (spell_id, binding_source) in bindings.items():
            names = localized.get(spell_id)
            if not names:
                continue
            items.append({
                'english': names['name'], 'chinese': names['name_zh'],
                'token': token, 'spell_id': spell_id,
                'token_source': 'simc_symbol',
                'binding_source': binding_source, 'authoritative': True,
            })

        query = (request.GET.get('query') or '').strip()[:200].casefold()
        if query:
            items = [item for item in items if query in item['token'].casefold()
                     or query in item['english'].casefold()
                     or query in item['chinese'].casefold()]
        items.sort(key=lambda item: (item['english'].casefold(), item['token']))
        total = len(items)
        if request.GET.get('all') == '1':
            page = 1
            page_size = max(total, 1)
            total_pages = 1
            selected_items = items
        else:
            total_pages = (total + page_size - 1) // page_size
            start = (page - 1) * page_size
            selected_items = items[start:start + page_size]
        return JsonResponse({'success': True, 'data': {
            'items': selected_items,
            'pagination': {'page': page, 'page_size': page_size, 'total': total, 'total_pages': total_pages},
        }})


class SimcAplCompletionsAPIView(SimcAplEditorAPIView):
    def post(self, request):
        try:
            payload = self.payload(request)
        except SuspiciousOperation:
            return _editor_error('invalid_json', 'Request body must be a JSON object.')
        content, error = self.content(payload)
        if error:
            return error
        limited = self.rate_limit(request)
        if limited:
            return limited
        spec = _editor_spec(payload.get('spec'))
        position = payload.get('position')
        if not spec:
            return _editor_error('invalid_spec', 'Unknown specialization.')
        if not isinstance(position, dict):
            return _editor_error('invalid_position', 'position must be an object.')
        try:
            line, column = int(position['line']), int(position['column'])
        except (KeyError, TypeError, ValueError):
            return _editor_error('invalid_position', 'position requires integer line and column.')
        lines = content.splitlines() or ['']
        if line < 1 or line > len(lines) or column < 1 or column > len(lines[line - 1]) + 1:
            return _editor_error('invalid_position', 'position is outside the document.')
        if not _APL_EDITOR_SEMAPHORE.acquire():
            return _editor_error('concurrency_limited', 'Completion capacity is busy.', 429)
        try:
            items = complete_document(content, line, column)[:100]
        finally:
            _APL_EDITOR_SEMAPHORE.release()
        return JsonResponse({'success': True, 'data': {
            'document_version': payload.get('document_version'),
            'items': items,
        }})


@method_decorator([csrf_exempt, login_required], name='dispatch')
class ConvertTextAPIView(View):
    """
    SimC APL文本转换API
    """
    
    def post(self, request):
        try:
            # 解析请求数据
            data = json.loads(request.body)
            text = data.get('text', '')
            conversion_type = data.get('conversion_type', '')
            spec = data.get('spec', '')

            if not isinstance(text, str) or not text.strip():
                return JsonResponse({
                    'success': False,
                    'error': '输入文本不能为空'
                })
            maximum = int(getattr(settings, 'SIMC_APL_EDITOR_MAX_CONTENT_LENGTH', 200000))
            if len(text) > maximum:
                return JsonResponse({
                    'success': False,
                    'error': f'输入文本不能超过 {maximum} 个字符'
                }, status=413)
            
            if conversion_type not in ['apl_to_cn', 'cn_to_apl']:
                return JsonResponse({
                    'success': False,
                    'error': '无效的转换类型'
                })
            
            # 执行转换
            if conversion_type == 'apl_to_cn':
                result = self.convert_apl_to_cn(text, spec)
            else:
                result = self.convert_cn_to_apl(text, spec)
            
            return JsonResponse({
                'success': True,
                'result': result
            })
            
        except Exception as e:
            logger.error(f"一键模拟SimC配置失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '一键模拟SimC配置失败'
            })
        except Exception as e:
            logger.error(f"文本转换API错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'获取APL详情失败: {str(e)}'
            })
    
    def bilingual_pairs(self, spec='', text=''):
        """Build a typed catalog, then scope visible names to the submitted document."""
        identity = _latest_catalog_identity()
        parsed_spec = _editor_spec(spec) if spec else None
        if not identity or not parsed_spec:
            return [], []
        class_key, class_name, spec_name = parsed_spec
        demands = list(extract_translation_demands(text))
        # The legacy editor accepts action-list lines that the strict parser
        # rejects (notably a standalone ``actions+=/...``).  Extract only the
        # action slot on those lines; do not broaden into a catalog scan.
        parsed_action_tokens = {demand.token.casefold() for demand in demands if demand.kind == 'action'}
        action_slot = re.compile(
            r'(?m)^\s*actions(?:\.[A-Za-z0-9_]+)?\s*\+?=\s*/\s*'
            r'([A-Za-z][A-Za-z0-9_]*(?:[ _][A-Za-z0-9_]+)*)'
        )
        for match in action_slot.finditer(text):
            source_token = match.group(1)
            token_key = source_token.replace(' ', '_').casefold()
            if token_key not in parsed_action_tokens:
                demands.append(TranslationDemand('action', token_key, token_key in CONTROL_ACTIONS))
                parsed_action_tokens.add(token_key)
        symbol_rows = SimcAplSymbol.objects.filter(
            simc_revision=identity[0], wow_build=identity[1], is_active=True,
        ).filter(
            models.Q(class_name__isnull=True, spec__isnull=True) |
            models.Q(class_name=class_name, spec__isnull=True) |
            models.Q(class_name=class_name, spec=spec_name)
        ).filter(models.Q(hero_tree__isnull=True) | models.Q(hero_tree='')).values('symbol_kind', 'token', 'spell_id', 'trait_id', 'hero_tree')
        facts = list(symbol_rows)
        action_bindings = _authoritative_action_bindings(parsed_spec, identity)
        # The action resolver is the stricter source of truth (including the
        # same-class exact-token fallback). Replace visible action rows with
        # its single resolved fact so a visible NULL row cannot conflict with
        # the fallback fact we just proved authoritative.
        non_action_facts = [row for row in facts if row['symbol_kind'] != 'action']
        action_facts = [
            {
                'symbol_kind': 'action', 'token': token, 'spell_id': spell_id,
                'trait_id': None, 'hero_tree': None,
            }
            for token, (spell_id, _source) in action_bindings.items()
        ]
        facts = non_action_facts + action_facts
        spell_ids = {row['spell_id'] for row in facts if row['spell_id']}
        trait_ids = {row['trait_id'] for row in facts if row['trait_id']}
        localized = {}
        for row in WowSpellSnapshot.objects.filter(
            branch='wow', locale='zhCN', snapshot_build=identity[1],
            spell_id__in=spell_ids,
        ).exclude(name_zh='').values('spell_id', 'name_zh').order_by('spell_id', '-updated_at', '-id'):
            localized.setdefault(('spell', row['spell_id']), row['name_zh'])
        for row in WowTalentNodeMetadata.objects.filter(
            talent_version__branch='retail', talent_version__is_active=True,
            talent_version__current_build__in=(identity[1], ''),
            name_zh__gt='', talent_id__in=trait_ids,
        ).values('talent_id', 'name_zh').order_by('talent_id', '-last_updated', '-id'):
            localized.setdefault(('trait', row['talent_id']), row['name_zh'])
        mapping, _failures = resolve_demand_mappings(demands, facts, localized)
        scoped_facts = {}
        for fact in facts:
            kind = str(fact.get('symbol_kind') or '')
            token = str(fact.get('token') or '')
            if not token or kind not in {'action', 'buff', 'debuff', 'dot', 'cooldown', 'talent'}:
                continue
            scoped_facts.setdefault((kind, token.casefold()), []).append(fact)
        for (kind, token_key), token_facts in scoped_facts.items():
            if len(token_facts) != 1:
                continue
            fact = token_facts[0]
            identity_type = 'trait' if kind == 'talent' else 'spell'
            identity_id = fact.get('trait_id') if identity_type == 'trait' else fact.get('spell_id')
            chinese = localized.get((identity_type, identity_id))
            if isinstance(identity_id, int) and not isinstance(identity_id, bool) and identity_id > 0 and chinese:
                mapping.setdefault((kind, token_key), chinese)
        # A token that resolves to multiple localized names is not reversible.
        token_names = {}
        for key, chinese in mapping.items():
            token_names.setdefault(key, set()).add(chinese.casefold())
        mapping = {key: value for key, value in mapping.items() if len(token_names[key]) == 1}
        mapping = {
            key: value for key, value in mapping.items()
            if key[0] != 'action' or key[1] not in CONTROL_ACTIONS
        }
        active_keys = {(demand.kind, demand.token.casefold()) for demand in demands}
        # Scope disambiguation to the submitted document: unrelated runtime
        # aliases stay plain and cannot make a normal single-token edit verbose.
        pairs = disambiguate_chinese_labels(
            [(kind, token, chinese) for (kind, token), chinese in mapping.items()],
            active_keys=active_keys if text else None,
            include_plain_alias=not bool(text),
        )
        forward_pairs = list(pairs)
        reverse_pairs = list(pairs)
        return forward_pairs, reverse_pairs


    def convert_apl_to_cn(self, text, spec=''):
        """
        将APL关键字转换为中文
        """
        try:
            keyword_pairs = sorted(
                self.bilingual_pairs(spec, text=text)[0], key=lambda pair: len(pair[1]), reverse=True,
            )
            pair_by_token = {
                (kind, apl_keyword.casefold()): cn_keyword
                for kind, apl_keyword, cn_keyword in keyword_pairs
            }
            document_demands = list(extract_translation_demands(text))
            parsed_action_tokens = {demand.token.casefold() for demand in document_demands if demand.kind == 'action'}
            for token in re.findall(r'(?:^|\n)\s*actions(?:\.[A-Za-z0-9_]+)?\s*[+]?=/\s*([A-Za-z][A-Za-z0-9_]*)', text):
                if token.casefold() not in parsed_action_tokens:
                    document_demands.append(TranslationDemand('action', token, token.casefold() in CONTROL_ACTIONS))
                    parsed_action_tokens.add(token.casefold())
            mapping = {
                (demand.kind, demand.token.casefold()): pair_by_token[(demand.kind, demand.token.casefold())]
                for demand in document_demands
                if (demand.kind, demand.token.casefold()) in pair_by_token
            }
            translated = translate_apl_ranges(text, mapping)
            # Legacy editor input also accepts spaces in an action token. Such
            # input is outside canonical SimC token grammar, so apply a narrow
            # fallback only at the parser-defined action slot, never to
            # comments, expressions, option values, or arbitrary text.
            for kind, apl_keyword, cn_keyword in keyword_pairs:
                if kind != 'action':
                    continue
                token_pattern = r'[ _]+'.join(re.escape(part) for part in apl_keyword.split('_'))
                pattern = (
                    r'(?m)^(?P<prefix>\s*actions(?:\.[A-Za-z0-9_]+)?\+?=\/?)'
                    + token_pattern + r'(?=[,\s#]|$)'
                )
                translated = re.sub(
                    pattern,
                    lambda match, value=cn_keyword: match.group('prefix') + value,
                    translated,
                )
            return translated
        except Exception as e:
            logger.error(f"APL2CN错误: {str(e)}")
            raise e
    
    def convert_cn_to_apl(self, text, spec=''):
        """
        将中文关键字转换为APL
        """
        try:
            keyword_pairs = sorted(
                self.bilingual_pairs(spec)[1], key=lambda pair: len(pair[2]), reverse=True,
            )
            lines = text.splitlines(keepends=True)
            # Reverse only known SimC slots. Chinese input cannot be parsed by
            # the English parser, so do not run a document-wide substitution:
            # action names occur after ``actions...=/`` and expression names
            # occur after one of the typed prefixes.
            for kind, apl_keyword, cn_keyword in keyword_pairs:
                value_pattern = r'\s*'.join(
                    re.escape(char) for char in cn_keyword if not char.isspace()
                )
                action_pattern = (
                    r'(?m)^(?P<prefix>\s*actions(?:\.[A-Za-z0-9_]+)?\+?=\/?)'
                    + value_pattern + r'(?=[,\s#]|$)'
                ) if kind == 'action' else r'(?!x)x'
                expression_pattern = (
                    rf'(?P<prefix>\b{re.escape(kind)}\.)'
                    + value_pattern + r'(?=\.|[\s<>=!,)&|+*/%@^~?-]|$)'
                ) if kind in {'buff', 'debuff', 'dot', 'cooldown', 'talent'} else r'(?!x)x'
                lines = [
                    re.sub(
                        expression_pattern,
                        lambda match, value=apl_keyword: match.group('prefix') + value,
                        re.sub(
                            action_pattern,
                            lambda match, value=apl_keyword: match.group('prefix') + value,
                            line,
                        ),
                    ) if not line.lstrip().startswith('#') else line
                    for line in lines
                ]
            return ''.join(lines)
        except Exception as e:
            logger.error(f"CN2APL错误: {str(e)}")
            raise e


@method_decorator([csrf_exempt, login_required], name='dispatch')
class WowDailyReportListAPIView(View):
    def get(self, request):
        try:
            limit = request.GET.get("limit", "30")
            try:
                limit = max(1, min(200, int(limit)))
            except ValueError:
                limit = 30
            rows = list(WowDailyReport.objects.all().order_by("-report_date", "-updated_at", "-id")[:limit])
            data = []
            for r in rows:
                data.append(
                    {
                        "id": r.id,
                        "report_date": getattr(r, "report_date", None).isoformat() if getattr(r, "report_date", None) else "",
                        "md_path": getattr(r, "md_path", "") or "",
                        "portal_url": _portal_report_url_from_path(getattr(r, "md_path", "") or ""),
                        "updated_at": _fmt_dt(getattr(r, "updated_at", None)),
                    }
                )
            return JsonResponse({"success": True, "data": data, "total": len(data)})
        except Exception as e:
            logger.error(f"获取WoW日报列表失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"success": False, "error": f"获取WoW日报列表失败: {str(e)}"})


@method_decorator([csrf_exempt, login_required], name='dispatch')
class WowDailyReportContentAPIView(View):
    def get(self, request):
        try:
            rid = (request.GET.get("id") or "").strip()
            date_s = (request.GET.get("date") or "").strip()
            row = None
            if rid:
                try:
                    row = WowDailyReport.objects.filter(id=int(rid)).first()
                except Exception:
                    row = None
            if not row and date_s:
                try:
                    row = WowDailyReport.objects.filter(report_date=date_s).first()
                except Exception:
                    row = None
            if not row:
                return JsonResponse({"success": False, "error": "未找到日报记录"})
            md_path = (getattr(row, "md_path", "") or "").strip()
            full = _safe_join_static(md_path)
            if not full or (not os.path.exists(full)):
                return JsonResponse({"success": False, "error": "日报文件不存在"})
            with open(full, "r", encoding="utf-8") as f:
                content = f.read()
            report_format = "html" if md_path.lower().endswith(".html") else "markdown"
            return JsonResponse(
                {
                    "success": True,
                    "data": {
                        "id": row.id,
                        "report_date": getattr(row, "report_date", None).isoformat() if getattr(row, "report_date", None) else "",
                        "md_path": md_path,
                        "format": report_format,
                        "updated_at": _fmt_dt(getattr(row, "updated_at", None)),
                        "content": content,
                    },
                }
            )
        except Exception as e:
            logger.error(f"获取WoW日报内容失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"success": False, "error": f"获取WoW日报内容失败: {str(e)}"})


@method_decorator([csrf_exempt, login_required], name='dispatch')
class WowDailyReportDownloadAPIView(View):
    def get(self, request):
        try:
            date_s = (request.GET.get("date") or "").strip()
            rid = (request.GET.get("id") or "").strip()
            row = None
            if rid:
                try:
                    row = WowDailyReport.objects.filter(id=int(rid)).first()
                except Exception:
                    row = None
            if not row and date_s:
                row = WowDailyReport.objects.filter(report_date=date_s).first()
            if not row:
                return JsonResponse({"success": False, "error": "未找到日报记录"})
            md_path = (getattr(row, "md_path", "") or "").strip()
            full = _safe_join_static(md_path)
            if not full or (not os.path.exists(full)):
                return JsonResponse({"success": False, "error": "日报文件不存在"})
            with open(full, "rb") as f:
                content = f.read()
            filename = os.path.basename(md_path) or "wow_daily_report.md"
            resp = HttpResponse(content, content_type="application/octet-stream")
            resp["Content-Disposition"] = f'attachment; filename="{filename}"'
            return resp
        except Exception as e:
            logger.error(f"下载WoW日报失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"success": False, "error": f"下载WoW日报失败: {str(e)}"})


@method_decorator([csrf_exempt, login_required], name='dispatch')
class WowDailyReportGenerateAPIView(View):
    def post(self, request):
        try:
            meta = generate_wow_daily_report(report_date=timezone.localdate(), use_llm=True)
            ext = meta.get("ext") if isinstance(meta, dict) else {}
            llm_errors = []
            if isinstance(ext, dict):
                llm_errors = ext.get("llm_errors") or []
            first_err = ""
            if isinstance(llm_errors, list) and llm_errors:
                try:
                    first_err = str((llm_errors[0] or {}).get("error") or "")
                except Exception:
                    first_err = ""
            return JsonResponse(
                {
                    "success": True,
                    "data": {
                        "md_path": meta.get("md_path"),
                        "llm_ok": not bool(llm_errors),
                        "llm_error": first_err,
                    },
                }
            )
        except Exception as e:
            logger.error(f"生成WoW日报失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"success": False, "error": f"生成WoW日报失败: {str(e)}"})


@method_decorator(login_required, name='dispatch')
class SimcTaskAPIView(View):
    """
    SimC任务管理API
    """
    
    def get(self, request):
        """获取当前用户的SimC任务列表"""
        try:
            # 获取当前用户的所有SimC任务
            tasks = SimcTask.objects.filter(user_id=request.user.id, is_active=True).order_by('-modified_time')
            profile_ids = [t.simc_profile_id for t in tasks if t.simc_profile_id]
            profile_map = {
                p['id']: p
                for p in SimcProfile.objects.filter(
                    _accessible_simc_profile_q(request.user),
                    id__in=profile_ids,
                    is_active=True,
                ).values('id', 'name', 'spec')
            }
            
            tasks_data = []
            for task in tasks:
                ext_detail = self._task_ext_summary(task.task_type, task.ext)
                profile_info = profile_map.get(task.simc_profile_id) or {}
                reference_detail = {
                    'profile_id': task.profile_id, 'template_id': task.template_id, 'apl_id': task.apl_id,
                    'profile_version_id': task.profile_version_id, 'template_version_id': task.template_version_id,
                    'apl_version_id': task.apl_version_id, 'mode': task.mode,
                    'simulation_params': task.simulation_params or {}, 'mode_params': task.mode_params or {},
                } if task.profile_id and task.profile_version_id else {}
                tasks_data.append({
                    'id': task.id,
                    'name': task.name,
                    'simc_profile_id': task.simc_profile_id,
                    'simc_profile_name': profile_info.get('name', ''),
                    # New tasks keep their execution spec in ext; only old manifests fall back to the Profile.
                    'simc_profile_spec': ext_detail.get('spec') or profile_info.get('spec', ''),
                    'current_status': task.current_status,
                    'mode': task.mode,
                    # 任务列表只需安全的结构化摘要；原始 SimC 文本只能留在执行快照中，
                    # 不得通过列表或前端内嵌 JSON 回显给浏览器。
                    'reference': reference_detail,
                    'simulation_runs': [{
                        'id': run.id, 'sequence': run.sequence,
                        'candidate_key': run.candidate_key,
                        'candidate_label': run.candidate_label,
                        'round_number': run.round_number,
                        'status': run.status,
                        'result_summary': {
                            key: value for key, value in (run.result_summary or {}).items()
                            if key in {'dps', 'hps', 'dtps', 'score', 'rank', 'delta', 'percent'}
                            and isinstance(value, (int, float, bool, str))
                        } if isinstance(run.result_summary, dict) else {},
                        'error_summary': '任务执行失败' if run.status == 'failed' else '',
                        'started_at': _fmt_dt(run.started_at),
                        'completed_at': _fmt_dt(run.completed_at),
                    } for run in task.simulation_runs.order_by('sequence')],
                    'ext_detail': ext_detail,
                    'create_time': _fmt_dt(task.create_time),
                    'modified_time': _fmt_dt(task.modified_time),
                })
            
            return JsonResponse({
                'success': True,
                'data': tasks_data,
                'total': len(tasks_data)
            })
            
        except Exception as e:
            logger.error(f"获取SimC任务列表错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'获取任务列表失败: {str(e)}'
            })
    
    def post(self, request):
        """创建新的SimC任务，或通过 action=rerun 创建不可变任务的新执行。"""
        try:
            data = json.loads(request.body)
            if 'task_type' in data:
                return JsonResponse({'success': False, 'error': '不再支持 task_type 参数；请使用 SimC 工作台的任务模式入口。'}, status=400)
            if data.get('action') == 'rerun':
                task_id = data.get('id')
                if not task_id:
                    return JsonResponse({'success': False, 'error': '任务ID不能为空'}, status=400)
                try:
                    source = SimcTask.objects.get(
                        id=task_id, user_id=request.user.id, is_active=True,
                    )
                except SimcTask.DoesNotExist:
                    return JsonResponse({'success': False, 'error': '任务不存在或无权限访问'}, status=404)
                overrides = {
                    key: data[key]
                    for key in ('name', 'simulation_params', 'mode_params',
                                'profile_id', 'template_id', 'apl_id')
                    if key in data
                }
                try:
                    rerun = create_rerun(source.id, request.user.id, overrides)
                except TaskRerunError as exc:
                    return JsonResponse({'success': False, 'error': str(exc)}, status=400)
                return JsonResponse({
                    'success': True,
                    'message': '已创建新的引用型任务',
                    'data': {
                        'id': rerun.id,
                        'source_task_id': source.id,
                        'current_status': rerun.current_status,
                        'mode': rerun.mode,
                        'profile_version_id': rerun.profile_version_id,
                        'template_version_id': rerun.template_version_id,
                        'apl_version_id': rerun.apl_version_id,
                    },
                })
            name = data.get('name', '').strip()
            simc_profile_id = data.get('simc_profile_id')
            raw_simc_code = str(data.get('raw_simc_code') or '')
            selected_apl_id = data.get('selected_apl_id') or data.get('apl_template_id')
            base_template_id = data.get('base_template_id')
            base_template_content = data.get('base_template_content') if 'base_template_content' in data else None
            override_action_list = data.get('override_action_list') if 'override_action_list' in data else None

            # 新版字段
            fight_style = data.get('fight_style')
            fight_time = data.get('time')
            target_count = data.get('target_count')
            player_import_mode = data.get('player_import_mode') or data.get('player_config_mode')
            if player_import_mode == 'equipment':
                player_import_mode = 'manual_equipment'
            player_config_mode = player_import_mode
            player_equipment = data.get('player_equipment', '').strip()
            battlenet_region = data.get('battlenet_region', '').strip().lower()
            battlenet_realm = data.get('battlenet_realm', '').strip()
            battlenet_character = data.get('battlenet_character', '').strip()
            gear_strength = data.get('gear_strength')
            gear_crit = data.get('gear_crit')
            gear_haste = data.get('gear_haste')
            gear_mastery = data.get('gear_mastery')
            gear_versatility = data.get('gear_versatility')
            talent = data.get('talent', '').strip()
            spec = data.get('spec', '').strip()

            if not name:
                return JsonResponse({
                    'success': False,
                    'error': '任务名称不能为空'
                })

            # ========== 引用型任务切片：拒绝旧模式 ==========

            # 1. 拒绝 raw_simc_code
            if raw_simc_code.strip():
                return JsonResponse({
                    'success': False,
                    'error': '不再支持直接 SimC 代码模式。请使用基础模板 + APL 引用方式创建任务。'
                })

            # 2. 拒绝临时正文字段
            if base_template_content is not None:
                return JsonResponse({
                    'success': False,
                    'error': '不再支持 base_template_content 临时正文。请先保存为模板资源，然后传递 base_template_id。'
                })

            if override_action_list is not None:
                return JsonResponse({
                    'success': False,
                    'error': '不再支持 override_action_list 临时正文。请先保存为 APL 资源，然后传递 selected_apl_id。'
                })

            # 4. 要求必填字段
            if not base_template_id:
                return JsonResponse({
                    'success': False,
                    'error': '必须提供 base_template_id（基础模板 ID）'
                })

            if not selected_apl_id:
                return JsonResponse({
                    'success': False,
                    'error': '必须提供 selected_apl_id（APL ID）'
                })

            player_source = data.get('player_source') or {}
            if not isinstance(player_source, dict):
                return JsonResponse({'success': False, 'error': 'player_source 格式无效'}, status=400)
            source_type = str(player_source.get('type') or ('saved_profile' if simc_profile_id else '')).strip()
            if not source_type:
                return JsonResponse({'success': False, 'error': '必须提供 simc_profile_id 或 player_source'}, status=400)
            target_class, target_spec = canonical_simc_spec_identity(spec)
            if source_type == 'saved_profile' and simc_profile_id and not target_class:
                existing_profile = SimcProfile.objects.filter(
                    id=simc_profile_id, is_active=True,
                ).first()
                if existing_profile:
                    target_class, target_spec = canonical_simc_spec_identity(existing_profile.spec)
            if not target_class or f'{target_class}_{target_spec}' not in SIMC_SPEC_VALUES:
                return JsonResponse({'success': False, 'error': '必须选择有效的目标专精'}, status=400)

            profile = None
            profile_fields = None
            if source_type == 'saved_profile':
                profile_id = player_source.get('profile_id') or simc_profile_id
                profile = SimcProfile.objects.filter(
                    id=profile_id, is_active=True,
                ).first()
                if not profile:
                    return JsonResponse({'success': False, 'error': '玩家配置不存在'}, status=400)
                profile_class, profile_spec = canonical_simc_profile_identity(profile.spec, profile.class_name)
                if profile_spec != target_spec or (target_class and profile_class and profile_class != target_class):
                    return JsonResponse({'success': False, 'error': '已保存玩家配置专精与目标专精不一致'}, status=400)
            elif source_type == 'default':
                default_key = f'{target_class}_{target_spec}'
                default_rows = SimcProfile.objects.filter(
                    user_id__isnull=True,
                    source=SimcProfile.SOURCE_SIMC_UPSTREAM,
                    spec=default_key, is_active=True,
                )
                count = default_rows.count()
                if count != 1:
                    return JsonResponse({
                        'success': False,
                        'error': f'专精 {default_key} 默认玩家配置{("缺少" if count == 0 else f"存在多个（{count} 个）")}，需要且只能解析到一个',
                    }, status=409)
                default_profile = default_rows.get()
                try:
                    baseline = validate_default_player_baseline(default_key, default_profile.player_equipment)
                except ValueError as exc:
                    return JsonResponse({'success': False, 'error': str(exc)}, status=400)
                profile_fields = {
                    'name': f'本次默认配置 · {target_spec}', 'spec': target_spec,
                    'use_ptr': default_profile.use_ptr,
                    'player_config_mode': 'manual_equipment', 'player_equipment': baseline,
                }
            elif source_type == 'simc_addon':
                baseline = authoritative_player_baseline(player_source.get('simc_code'))
                baseline = '\n'.join(
                    line for line in baseline.splitlines()
                    if not re.match(r'^\s*actions(?:[.+]?=|\.)', line, re.IGNORECASE)
                ).strip()
                try:
                    baseline = validate_player_baseline(baseline)
                except ValueError as exc:
                    return JsonResponse({'success': False, 'error': str(exc)}, status=400)
                parsed = parse_manual_player_config(baseline, target_spec)
                source_spec = str(parsed.get('identity', {}).get('spec') or '').strip().lower()
                source_class = str(parsed.get('identity', {}).get('class_name') or '').strip().lower()
                if source_spec != target_spec or (source_class and source_class != target_class):
                    return JsonResponse({'success': False, 'error': 'SimC 代码专精与目标专精不一致'}, status=400)
                profile_fields = {
                    'name': f'本次 SimC 导入 · {target_spec}', 'spec': target_spec,
                    'player_config_mode': 'manual_equipment', 'player_equipment': baseline,
                }
            elif source_type == 'battlenet':
                try:
                    preflight = fetch_battlenet_character_preflight(
                        region=player_source.get('region'), realm=player_source.get('realm'),
                        character=player_source.get('character'), requested_spec=target_spec,
                    )
                except ValueError as exc:
                    return JsonResponse({'success': False, 'error': str(exc)}, status=400)
                if not preflight.get('simc_ready'):
                    return JsonResponse({'success': False, 'error': '；'.join(preflight.get('warnings') or ['Battle.net 角色不可用于模拟'])}, status=400)
                values = preflight['simc_config']
                source_class, source_spec = canonical_simc_spec_identity(values.get('spec'))
                if source_spec != target_spec or (source_class and source_class != target_class):
                    return JsonResponse({'success': False, 'error': 'Battle.net 角色专精与目标专精不一致'}, status=400)
                profile_fields = {
                    'name': f"本次 Battle.net · {values['battlenet_character']}",
                    'spec': target_spec,
                    'player_config_mode': 'battlenet',
                    'battlenet_region': values.get('battlenet_region', ''),
                    'battlenet_realm': values.get('battlenet_realm', ''),
                    'battlenet_character': values.get('battlenet_character', ''),
                    'player_equipment': values.get('player_equipment', ''),
                }
            else:
                return JsonResponse({'success': False, 'error': '请选择玩家配置来源'}, status=400)

            # Transient profiles inherit their equipment/player snapshot by default.
            # Only request-authored fields become final SimC overrides.
            if profile_fields is not None:
                if talent:
                    profile_fields['talent'] = talent
                for field, value in (
                    ('gear_strength', gear_strength),
                    ('gear_crit', gear_crit),
                    ('gear_haste', gear_haste),
                    ('gear_mastery', gear_mastery),
                    ('gear_versatility', gear_versatility),
                ):
                    if value is not None:
                        profile_fields[field] = value

            # 构建 simulation_params
            simulation_params = {}
            if fight_style:
                simulation_params['fight_style'] = fight_style
            if fight_time is not None:
                simulation_params['max_time'] = fight_time
            if target_count is not None:
                simulation_params['desired_targets'] = target_count
            if 'raid_buffs' in data:
                simulation_params['raid_buffs'] = data['raid_buffs']
            if 'use_class_raid_buff' in data:
                simulation_params['use_class_raid_buff'] = data['use_class_raid_buff']
            option_error = validate_simulation_options(simulation_params)
            if option_error:
                return JsonResponse({'success': False, 'error': option_error}, status=400)

            try:
                if profile_fields is not None:
                    from botend.services.simc_task_service import create_task_from_request
                    task = create_task_from_request(
                        user_id=request.user.id,
                        profile_fields=profile_fields,
                        base_template_id=base_template_id,
                        selected_apl_id=selected_apl_id,
                        simulation_params=simulation_params if simulation_params else None,
                        name=name,
                        backend_id=data.get('backend_id'),
                        is_admin=_is_simc_admin(request.user),
                    )
                else:
                    task = create_task(
                        user_id=request.user.id,
                        profile_id=profile.id,
                        template_id=base_template_id,
                        apl_id=selected_apl_id,
                        simulation_params=simulation_params if simulation_params else None,
                        name=name,
                        backend_id=data.get('backend_id'),
                        is_admin=_is_simc_admin(request.user),
                    )
            except TaskCreationError as e:
                return JsonResponse({
                    'success': False,
                    'error': str(e)
                })

            return JsonResponse({
                'success': True,
                'message': 'SimC任务创建成功',
                'data': {
                    'id': task.id,
                    'name': task.name,
                    'simc_profile_id': task.simc_profile_id,
                    'current_status': task.current_status,
                    'mode': task.mode,
                    'create_time': _fmt_dt(task.create_time),
                    'modified_time': _fmt_dt(task.modified_time),
                }
            })

        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': '无效的JSON数据'
            })
        except Exception as e:
            logger.error(f"创建SimC任务错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'创建任务失败: {str(e)}'
            })
    
    def put(self, request):
        """更新显示名称，或将仍为 pending 的任务原子终止。"""
        try:
            data = json.loads(request.body)
            task_id = data.get('id')
            name = str(data.get('name') or '').strip()
            requested_status = data.get('current_status')
            if not task_id:
                return JsonResponse({'success': False, 'error': '任务ID不能为空'}, status=400)
            if not name:
                return JsonResponse({'success': False, 'error': '任务名称不能为空'}, status=400)
            try:
                task = SimcTask.objects.get(id=task_id, user_id=request.user.id, is_active=True)
            except SimcTask.DoesNotExist:
                return JsonResponse({'success': False, 'error': '任务不存在或无权限访问'}, status=404)
            complete_reference = all((task.profile_id, task.template_id, task.apl_id,
                                      task.profile_version_id, task.template_version_id, task.apl_version_id))
            if not complete_reference:
                return JsonResponse({'success': False, 'error': '旧版冻结任务不支持更新；请使用新的引用型任务流程'}, status=400)
            if requested_status is not None:
                try:
                    requested_status = int(requested_status)
                except (TypeError, ValueError):
                    return JsonResponse({'success': False, 'error': '任务状态无效'}, status=400)
                if requested_status not in (3, 5):
                    return JsonResponse({'success': False, 'error': 'pending 任务只能标记为失败或取消'}, status=400)
                now = timezone.now()
                updated = SimcTask.objects.filter(
                    id=task.id, user_id=request.user.id, is_active=True, current_status=0,
                ).update(
                    name=name, current_status=requested_status,
                    completed_at=now, modified_time=now,
                )
                if not updated:
                    return JsonResponse({
                        'success': False,
                        'error': '任务已被领取或状态已变化，不能再修改执行状态',
                    }, status=409)
                task.refresh_from_db()
                return JsonResponse({
                    'success': True,
                    'message': '任务已取消' if requested_status == 5 else '任务已标记为失败',
                    'data': {
                        'id': task.id, 'name': task.name, 'current_status': task.current_status,
                        'mode': task.mode, 'create_time': _fmt_dt(task.create_time),
                        'modified_time': _fmt_dt(task.modified_time),
                    },
                })
            task.name = name
            task.save(update_fields=['name', 'modified_time'])
            return JsonResponse({
                'success': True,
                'message': '任务名称更新成功；执行输入和状态不可原地修改，请使用重跑创建新任务。',
                'data': {
                    'id': task.id, 'name': task.name, 'current_status': task.current_status,
                    'mode': task.mode, 'create_time': _fmt_dt(task.create_time),
                    'modified_time': _fmt_dt(task.modified_time),
                },
            })
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': '无效的JSON数据'}, status=400)
        except Exception as e:
            logger.error(f"更新SimC任务错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({'success': False, 'error': f'更新任务失败: {str(e)}'})

    def delete(self, request):
        """删除SimC任务（软删除）"""
        try:
            data = json.loads(request.body)
            task_id = data.get('id')
            
            if not task_id:
                return JsonResponse({
                    'success': False,
                    'error': '任务ID不能为空'
                })
            
            # 获取任务并检查权限
            try:
                task = SimcTask.objects.get(id=task_id, user_id=request.user.id, is_active=True)
            except SimcTask.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': '任务不存在或无权限访问'
                })
            
            # 软删除
            task.is_active = False
            task.save()
            
            return JsonResponse({
                'success': True,
                'message': 'SimC任务删除成功'
            })
            
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': '无效的JSON数据'
            })
        except Exception as e:
            logger.error(f"删除SimC任务错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'删除任务失败: {str(e)}'
            })
    
    def patch(self, request):
        """重跑SimC任务"""
        try:
            data = json.loads(request.body)
            if 'task_type' in data:
                return JsonResponse({'success': False, 'error': '不再支持 task_type 参数；请使用 SimC 工作台的任务模式入口。'}, status=400)
            task_id = data.get('id')
            action = data.get('action')
            
            if not task_id:
                return JsonResponse({
                    'success': False,
                    'error': '任务ID不能为空'
                })
            
            if action != 'rerun':
                return JsonResponse({
                    'success': False,
                    'error': '不支持的操作类型'
                })
            
            # 获取任务并检查权限
            try:
                task = SimcTask.objects.get(id=task_id, user_id=request.user.id, is_active=True)
            except SimcTask.DoesNotExist:
                return JsonResponse({'success': False, 'error': '任务不存在或无权限访问'})

            if action == 'rerun' and task.profile_id and task.profile_version_id:
                from botend.services.task_rerun import (
                    TaskRerunError,
                    create_rerun as service_create_rerun,
                )
                overrides = {
                    key: data[key]
                    for key in ('name', 'simulation_params', 'mode_params',
                                'profile_id', 'template_id', 'apl_id')
                    if key in data
                }
                try:
                    rerun_task = service_create_rerun(task.id, request.user.id, overrides)
                except TaskRerunError as exc:
                    return JsonResponse({'success': False, 'error': str(exc)}, status=400)
                return JsonResponse({'success': True, 'message': '已创建新的引用型任务', 'data': {
                    'id': rerun_task.id, 'source_task_id': task.id, 'current_status': rerun_task.current_status,
                    'mode': rerun_task.mode,
                    'profile_version_id': rerun_task.profile_version_id,
                    'template_version_id': rerun_task.template_version_id,
                    'apl_version_id': rerun_task.apl_version_id,
                }})

            ext_payload = {}
            try:
                ext_payload = json.loads(task.ext or '{}')
                if not isinstance(ext_payload, dict):
                    ext_payload = {}
            except Exception:
                ext_payload = {}
            compare_payload = ext_payload.get('apl_compare') if isinstance(ext_payload.get('apl_compare'), dict) else {}
            if compare_payload and not ext_payload.get('override_action_list'):
                return JsonResponse({
                    'success': False,
                    'error': '该任务在预处理阶段失败，无法直接重跑，请重新发起"APL候选对比模拟"'
                })
            
            rerun_task = self.create_rerun(task)
            
            return JsonResponse({
                'success': True,
                'message': 'SimC任务重跑成功，新任务已加入队列',
                'data': {
                    'id': rerun_task.id,
                    'name': rerun_task.name,
                    'simc_profile_id': rerun_task.simc_profile_id,
                    'current_status': rerun_task.current_status,
                    'mode': rerun_task.mode,
                    'ext_detail': self._task_ext_summary(rerun_task.task_type, rerun_task.ext),
                    'create_time': _fmt_dt(rerun_task.create_time),
                    'modified_time': _fmt_dt(rerun_task.modified_time),
                }
            })
            
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': '无效的JSON数据'
            })
        except Exception as e:
            logger.error(f"重跑SimC任务错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'重跑任务失败: {str(e)}'
            })

    @staticmethod
    def create_rerun(task):
        """
        Create a rerun from an existing task.

        For reference-based tasks: delegates to unified rerun service.
        For old frozen tasks: prevented - legacy task creation is frozen.
        """
        # Check if this is a reference task or legacy frozen task
        if task.profile_id and task.template_id and task.apl_id and \
           task.profile_version_id and task.template_version_id and task.apl_version_id:
            # Reference-based task: use unified service
            from botend.services.task_rerun import create_rerun as service_create_rerun, TaskRerunError
            try:
                return service_create_rerun(
                    source_task_id=task.id,
                    user_id=task.user_id,
                    overrides={},
                )
            except TaskRerunError as e:
                raise ValueError(f'重跑失败: {e}')
        else:
            # Legacy frozen task: prevent rerun
            raise ValueError(
                '旧版冻结任务不支持重跑。只有完整引用型任务（profile/template/apl + versions）才能重跑。'
            )

    def _task_ext_summary(self, task_type, ext):
        """Return only the browser fields needed to render a task context.

        The persisted manifest deliberately retains executable SimC text, APL and
        equipment snapshots for the Worker.  Browser responses must instead be
        an allowlist, so newly-added manifest fields cannot leak raw input.
        """
        payload = self._normalize_task_ext(task_type, ext)
        if not isinstance(payload, dict):
            return {}
        browser_fields = (
            'player_config_mode', 'player_import_mode',
            'battlenet_region', 'battlenet_realm', 'battlenet_character',
            'spec', 'talent', 'fight_style', 'time', 'target_count',
            'regular_time', 'regular_target_count',
            'selected_attributes', 'attribute_step',
            'gear_strength', 'gear_crit', 'gear_haste',
            'gear_mastery', 'gear_versatility',
            'selected_apl_id', 'profile_name', 'override_action_list_name',
            'override_action_list_type',
            'simc_error_code', 'simc_error_summary',
        )
        summary = {field: payload[field] for field in browser_fields if field in payload}
        apl_compare = payload.get('apl_compare')
        if isinstance(apl_compare, dict):
            apl_compare_fields = (
                'task_id', 'candidate_index', 'is_base', 'preprocess_stage',
            )
            summary['apl_compare'] = {
                field: apl_compare[field]
                for field in apl_compare_fields
                if field in apl_compare
            }
        return summary

    def _task_result_file_summary(self, task):
        """Expose result filenames only; native SimC output remains server-side."""
        if int(task.current_status or 0) != 2:
            return ''
        result_file = str(task.result_file or '').strip()
        if not result_file:
            return ''
        if int(task.task_type or 1) == 1:
            valid_regular_name = re.fullmatch(r'(?:simc_task_\d+|[a-f0-9]{32})\.html', result_file)
            return result_file if valid_regular_name else ''
        filenames = [name.strip() for name in result_file.split(',') if name.strip()]
        if not filenames:
            return ''
        if all(parse_attribute_result_filename(name) for name in filenames):
            return ','.join(filenames)
        return ''

    def _normalize_task_ext(self, task_type, ext):
        if not ext:
            return {}
        if isinstance(ext, dict):
            payload = ext
        else:
            text = str(ext).strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
                payload = parsed if isinstance(parsed, dict) else {}
            except Exception:
                payload = {}
                if int(task_type or 1) == 2:
                    payload['selected_attributes'] = text
        return payload

    def _build_task_ext(self, task_type, ext, regular_time=None, regular_target_count=None, selected_attributes=None, attribute_step=None, raw_simc_code=None, selected_apl_id=None, base_template_id=None, base_template_content=None, override_action_list=None, override_action_list_provided=False, owner_user_id=None,
                        fight_style=None, time=None, target_count=None, player_config_mode=None, player_equipment=None,
                        gear_strength=None, gear_crit=None, gear_haste=None, gear_mastery=None, gear_versatility=None, talent=None, spec=None,
                        battlenet_region=None, battlenet_realm=None, battlenet_character=None):
        ttype = int(task_type or 1)
        base = self._normalize_task_ext(ttype, ext)

        # 用户编辑后的正文是任务的权威快照；ID 仅保留来源元数据。
        if base_template_content is not None:
            frozen_template = str(base_template_content)
            if not frozen_template.strip():
                raise Exception('基础模板内容不能为空')
            if base_template_id not in (None, ''):
                template_obj = _get_simc_content_by_id(
                    base_template_id,
                    owner_user_id=owner_user_id,
                )
                if not template_obj:
                    raise Exception('选择的基础模板不存在或已禁用')
                base['base_template_id'] = template_obj.id
            base['base_template_content'] = frozen_template
        elif base_template_id not in (None, ''):
            template_obj = _get_simc_content_by_id(
                base_template_id,
                owner_user_id=owner_user_id,
            )
            if not template_obj:
                raise Exception('选择的基础模板不存在或已禁用')
            base['base_template_id'] = template_obj.id
            base['base_template_content'] = template_obj.content
        elif not base.get('base_template_content') and spec:
            # 先匹配专精模板；没有时再使用唯一全局默认模板。每一层都 fail closed。
            candidates = SimcContentTemplate.objects.filter(
                is_active=True,
                spec=spec,
            ).filter(models.Q(owner_user_id__isnull=True) | models.Q(owner_user_id=owner_user_id))
            if candidates.count() > 1:
                raise Exception(f'专精 {spec} 有多个启用的基础模板，请明确选择一个')
            if candidates.count() == 0:
                candidates = SimcContentTemplate.objects.filter(
                    is_active=True,
                    spec__in=['default', 'all', '*'],
                ).filter(models.Q(owner_user_id__isnull=True) | models.Q(owner_user_id=owner_user_id))
                if candidates.count() > 1:
                    raise Exception('存在多个启用的默认基础模板，请明确选择一个')
            candidate_count = candidates.count()
            if candidate_count == 1:
                template_obj = candidates.first()
                base['base_template_id'] = template_obj.id
                base['base_template_content'] = template_obj.content
            elif candidate_count > 1:
                raise Exception(f'专精 {spec} 存在重复启用的基础模板，请明确选择一个')
            else:
                # 首次同步前兼容已有任务入口：读取部署配置中的基础模板并立即冻结，
                # 执行阶段仍只消费任务快照，不会再次读取该文件。
                template_path = str((getattr(settings, 'SIMC_CONFIG', {}) or {}).get('simc_template') or 'LMonitor/simc_template.txt')
                if not os.path.isabs(template_path):
                    template_path = os.path.join(settings.BASE_DIR, template_path)
                if not os.path.isfile(template_path):
                    raise Exception(f'专精 {spec} 没有可用的基础模板')
                with open(template_path, encoding='utf-8') as template_file:
                    frozen_template = template_file.read()
                if not frozen_template.strip():
                    raise Exception(f'专精 {spec} 没有可用的基础模板')
                base['base_template_content'] = frozen_template

        # 快照冻结：APL - 用户编辑内容优先
        if override_action_list_provided:
            base['override_action_list'] = str(override_action_list or '')
            if selected_apl_id not in (None, ''):
                apl_obj = SimcApl.objects.filter(id=selected_apl_id, is_active=True).filter(
                    models.Q(is_system=True, owner_user_id__isnull=True)
                    | models.Q(is_system=False, owner_user_id=owner_user_id)
                ).first()
                if not apl_obj:
                    raise Exception('选择的 APL 不存在或已禁用')
                base['selected_apl_id'] = apl_obj.id
                base['override_action_list_name'] = apl_obj.name or apl_obj.spec
                base['override_action_list_type'] = 'default_apl' if apl_obj.is_system else 'custom_apl'
        elif selected_apl_id not in (None, ''):
            apl_obj = SimcApl.objects.filter(id=selected_apl_id, is_active=True).filter(
                models.Q(is_system=True, owner_user_id__isnull=True)
                | models.Q(is_system=False, owner_user_id=owner_user_id)
            ).first()
            if not apl_obj:
                raise Exception('选择的 APL 不存在或已禁用')
            base['selected_apl_id'] = apl_obj.id
            base['override_action_list'] = apl_obj.content
            base['override_action_list_name'] = apl_obj.name or apl_obj.spec
            base['override_action_list_type'] = 'default_apl' if apl_obj.is_system else 'custom_apl'
        elif not base.get('selected_apl_id') and not base.get('override_action_list') and spec:
            # 没有 APL ID/override 时，当前用户默认 APL 优先于全局上游默认。
            apl_obj = _get_unique_default_apl_for_spec(spec, owner_user_id=owner_user_id)
            if apl_obj:
                base['selected_apl_id'] = apl_obj.id
                base['override_action_list'] = apl_obj.content

        if ttype == 1:
            payload = {}
            if isinstance(base, dict):
                payload.update(base)
            if regular_time not in (None, ''):
                payload['regular_time'] = max(1, int(regular_time))
            if regular_target_count not in (None, ''):
                payload['regular_target_count'] = max(1, int(regular_target_count))
            raw_code_value = raw_simc_code if raw_simc_code is not None else payload.get('raw_simc_code', '')
            # 任务 manifest 必须保真保存原始 SimC 文本；仅将非字符串值转成字符串。
            raw_code = raw_code_value if isinstance(raw_code_value, str) else str(raw_code_value or '')
            if raw_code:
                payload['raw_simc_code'] = raw_code
            else:
                payload.pop('raw_simc_code', None)
            
            # 新版字段：只保存玩家信息导入方式和由表单选择的战斗/APL 配置
            # 快照冻结：player_equipment
            if player_config_mode:
                payload['player_config_mode'] = player_config_mode
                payload['player_import_mode'] = player_config_mode
                if player_config_mode in ('manual_equipment', 'attribute_only'):
                    # 冻结 player_equipment 到 ext
                    if player_equipment:
                        payload['player_equipment'] = player_equipment
                    elif player_config_mode == 'attribute_only' and spec:
                        # attribute_only 模式下，从 default_player 获取并冻结
                        from botend.services.simc_player_config import authoritative_player_baseline
                        baseline = authoritative_player_baseline(spec)
                        if baseline:
                            payload['player_equipment'] = baseline
                elif player_config_mode == 'battlenet':
                    payload['battlenet_region'] = str(battlenet_region or '').lower()
                    payload['battlenet_realm'] = str(battlenet_realm or '').strip()
                    payload['battlenet_character'] = str(battlenet_character or '').strip()
                if gear_strength not in (None, ''):
                    payload['gear_strength'] = gear_strength
                if gear_crit not in (None, ''):
                    payload['gear_crit'] = gear_crit
                if gear_haste not in (None, ''):
                    payload['gear_haste'] = gear_haste
                if gear_mastery not in (None, ''):
                    payload['gear_mastery'] = gear_mastery
                if gear_versatility not in (None, ''):
                    payload['gear_versatility'] = gear_versatility
                if fight_style:
                    payload['fight_style'] = fight_style
                if time not in (None, ''):
                    payload['time'] = max(1, int(time))
                if target_count not in (None, ''):
                    payload['target_count'] = max(1, int(target_count))
                if spec:
                    payload['spec'] = spec
                if talent:
                    payload['talent'] = talent
            
            return json.dumps(payload, ensure_ascii=False) if payload else ''

        # New and legacy attribute scans share one manifest shape.  The runner
        # needs the entire frozen player snapshot, not just the selected pair.
        payload = {}
        if isinstance(base, dict):
            payload.update(base)
        if selected_attributes:
            payload['selected_attributes'] = str(selected_attributes).strip()
        selected = str(payload.get('selected_attributes') or '').strip()
        if not selected:
            raise Exception('属性模拟任务缺少属性组合')
        if attribute_step not in (None, ''):
            payload['attribute_step'] = max(1, int(attribute_step))
        if player_config_mode:
            payload['player_config_mode'] = player_config_mode
            payload['player_import_mode'] = player_config_mode
            payload['player_equipment'] = str(player_equipment or '')
            payload['battlenet_region'] = str(battlenet_region or '').lower()
            payload['battlenet_realm'] = str(battlenet_realm or '').strip()
            payload['battlenet_character'] = str(battlenet_character or '').strip()
            for field, value in (
                ('gear_strength', gear_strength), ('gear_crit', gear_crit),
                ('gear_haste', gear_haste), ('gear_mastery', gear_mastery),
                ('gear_versatility', gear_versatility),
            ):
                if value not in (None, ''):
                    payload[field] = value
            if fight_style:
                payload['fight_style'] = fight_style
            if time not in (None, ''):
                payload['time'] = max(1, int(time))
            if target_count not in (None, ''):
                payload['target_count'] = max(1, int(target_count))
            if spec:
                payload['spec'] = spec
            if talent is not None:
                payload['talent'] = str(talent).strip()
        return json.dumps(payload, ensure_ascii=False)


@method_decorator(login_required, name='dispatch')
class SimcComparisonTaskAPIView(View):
    """Create one self-describing comparison Task with multiple candidate Runs."""
    MAX_TASKS = 8
    MAX_ATTRIBUTE_TASKS = 13
    ATTRIBUTE_STATS = SIMC_ATTRIBUTE_STATS
    ATTRIBUTE_SEARCH_STEP = SIMC_ATTRIBUTE_SEARCH_STEP
    ATTRIBUTE_DPS_TOLERANCE = SIMC_ATTRIBUTE_DPS_TOLERANCE

    @staticmethod
    def _int(value, field):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError(f'{field}必须是整数')
        if value < 0:
            raise ValueError(f'{field}不能小于0')
        return value

    @classmethod
    def _attribute_variants(cls, values, step=None, round_number=1, mark_base=True):
        """Return the server-owned fixed 50-rating pairwise neighborhood."""
        try:
            step = int(step if step is not None else cls.ATTRIBUTE_SEARCH_STEP)
        except (TypeError, ValueError):
            raise ValueError('属性寻优步长无效')
        if step != cls.ATTRIBUTE_SEARCH_STEP:
            raise ValueError(f'四属性自动寻优固定使用 {cls.ATTRIBUTE_SEARCH_STEP} 绿字步长')
        return attribute_variants(values, round_number=round_number, mark_base=mark_base)

    @staticmethod
    def _run_result_file(run):
        artifact = run.artifacts.filter(artifact_type='html_report').order_by('-created_at').first()
        if artifact:
            return os.path.basename(artifact.file_path)
        params = run.candidate_params if isinstance(run.candidate_params, dict) else {}
        value = str(params.get('legacy_result_file') or '').strip()
        return value if value and '/' not in value and '\\' not in value and '\n' not in value else ''

    def get(self, request):
        """Return one owned comparison/attribute task with safe run summaries."""
        try:
            task_id = str(request.GET.get('task_id') or '').strip()
            if not task_id:
                return JsonResponse({'success': False, 'error': 'task_id不能为空'}, status=400)
            try:
                task = SimcTask.objects.get(
                    id=int(task_id), user_id=request.user.id, is_active=True,
                    mode__in=('comparison', 'attribute_sweep'),
                )
            except (TypeError, ValueError, SimcTask.DoesNotExist):
                return JsonResponse({'success': False, 'error': '任务不存在或无权限访问'}, status=404)
            runs = list(task.simulation_runs.order_by('sequence'))
            status_counts = {'pending': 0, 'running': 0, 'completed': 0, 'failed': 0}
            run_details = []
            for run in runs:
                key = run.status if run.status in status_counts else 'failed'
                status_counts[key] += 1
                run_details.append({
                    'run_id': run.id, 'sequence': run.sequence,
                    'candidate_label': run.candidate_label or '',
                    'round_number': run.round_number, 'status': run.status,
                    'error_summary': '任务执行失败' if run.status == 'failed' else '',
                    'result_summary': SimcWorkbenchAPIView._safe_summary(run.result_summary or {}),
                    'started_at': _fmt_dt(run.started_at), 'completed_at': _fmt_dt(run.completed_at),
                })
            return JsonResponse({'success': True, 'data': {
                'task_id': task.id, 'name': task.name, 'mode': task.mode,
                'status': task.current_status, 'status_counts': status_counts,
                'created_at': _fmt_dt(task.create_time), 'completed_at': _fmt_dt(task.completed_at),
                'report_url': f'/simc-compare/?task_id={task.id}' if runs else '',
                'runs': run_details,
            }})
        except Exception as e:
            logger.error(f'获取 SimC comparison 数据失败: {e}\n{traceback.format_exc()}')
            return JsonResponse({'success': False, 'error': '服务器内部错误'}, status=500)

    def post(self, request):
        try:
            data = json.loads(request.body or '{}')
            if 'task_type' in data:
                return JsonResponse({'success': False, 'error': '不再支持 task_type 参数；请使用 SimC 工作台的任务模式入口。'}, status=400)
            continue_task_id = str(data.get('continue_task_id') or data.get('task_id') or '').strip()
            if continue_task_id:
                return JsonResponse({
                    'success': False,
                    'error': '属性寻优续轮已由 SimC Worker 自动管理，无需客户端触发',
                }, status=409)

            kind = str(data.get('kind') or '').strip()
            category = str(data.get('category') or '').strip()
            name = str(data.get('name') or '').strip()
            profile_id = data.get('simc_profile_id')
            base_template_id = data.get('base_template_id')
            selected_apl_id = data.get('selected_apl_id')
            if not base_template_id or not selected_apl_id:
                raise ValueError('base_template_id 和 selected_apl_id 均不能为空')

            player_source = data.get('player_source') or {}
            if not isinstance(player_source, dict):
                raise ValueError('player_source 格式无效')
            source_type = str(player_source.get('type') or ('saved_profile' if profile_id else '')).strip()
            target_class, target_spec = canonical_simc_spec_identity(data.get('spec'))
            transient_profile = False
            source_talent_candidates = []
            attribute_baseline_values = None
            if source_type == 'saved_profile':
                profile_id = player_source.get('profile_id') or profile_id
                try:
                    profile = SimcProfile.objects.get(
                        _accessible_simc_profile_q(request.user),
                        id=int(profile_id),
                        is_active=True,
                    )
                except (TypeError, ValueError, SimcProfile.DoesNotExist):
                    raise ValueError('玩家配置不存在或无权使用')
                profile_class, profile_spec = canonical_simc_profile_identity(profile.spec, profile.class_name)
                if target_spec and (profile_spec != target_spec or (target_class and profile_class and profile_class != target_class)):
                    raise ValueError('已保存玩家配置专精与目标专精不一致')
            else:
                if not target_class or f'{target_class}_{target_spec}' not in SIMC_SPEC_VALUES:
                    raise ValueError('必须选择有效的目标专精')
                if source_type == 'default':
                    default_key = f'{target_class}_{target_spec}'
                    default_rows = SimcProfile.objects.filter(
                        user_id__isnull=True,
                        source=SimcProfile.SOURCE_SIMC_UPSTREAM,
                        spec=default_key, is_active=True,
                    )
                    count = default_rows.count()
                    if count != 1:
                        raise ValueError(
                            f'专精 {default_key} 默认玩家配置{("缺少" if count == 0 else f"存在多个（{count} 个）")}，需要且只能解析到一个'
                        )
                    default_profile = default_rows.get()
                    baseline = validate_default_player_baseline(default_key, default_profile.player_equipment)
                    profile_fields = {
                        'name': f'本次默认配置 · {target_spec}', 'spec': target_spec,
                        'use_ptr': default_profile.use_ptr,
                        'player_config_mode': 'manual_equipment', 'player_equipment': baseline,
                    }
                elif source_type == 'simc_addon':
                    baseline = authoritative_player_baseline(player_source.get('simc_code'))
                    baseline = '\n'.join(
                        line for line in baseline.splitlines()
                        if not re.match(r'^\s*actions(?:[.+]?=|\.)', line, re.IGNORECASE)
                    ).strip()
                    baseline = validate_player_baseline(baseline)
                    parsed = parse_manual_player_config(baseline, target_spec)
                    source_spec = str(parsed.get('identity', {}).get('spec') or '').strip().lower()
                    source_class = str(parsed.get('identity', {}).get('class_name') or '').strip().lower()
                    if source_spec != target_spec or (source_class and source_class != target_class):
                        raise ValueError('SimC 代码专精与目标专精不一致')
                    raw_fields = parsed.get('raw_fields') or {}
                    attribute_baseline_values = {
                        stat: self._int(
                            raw_fields.get(f'gear_{stat}', raw_fields.get(f'gear_{stat}_rating', 0)),
                            stat,
                        )
                        for stat in self.ATTRIBUTE_STATS
                    }
                    profile_fields = {
                        'name': f'本次 SimC 导入 · {target_spec}', 'spec': target_spec,
                        'player_config_mode': 'attribute_only', 'player_equipment': baseline,
                    }
                elif source_type == 'battlenet':
                    preflight = fetch_battlenet_character_preflight(
                        region=player_source.get('region'), realm=player_source.get('realm'),
                        character=player_source.get('character'), requested_spec=target_spec,
                    )
                    if not preflight.get('simc_ready'):
                        raise ValueError('；'.join(preflight.get('warnings') or ['Battle.net 角色不可用于模拟']))
                    values = preflight['simc_config']
                    source_talent_candidates = list(
                        (preflight.get('comparison_candidates') or {}).get('talents') or []
                    )
                    source_class, source_spec = canonical_simc_spec_identity(values.get('spec'))
                    if source_spec != target_spec or (source_class and source_class != target_class):
                        raise ValueError('Battle.net 角色专精与目标专精不一致')
                    if kind == 'attribute_variants':
                        # Attribute variants use the stable upstream baseline so rating
                        # overrides are rendered into the final SimC input.
                        default_key = f'{target_class}_{target_spec}'
                        default_rows = SimcProfile.objects.filter(
                            user_id__isnull=True,
                            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
                            spec=default_key, is_active=True,
                        )
                        if default_rows.count() != 1:
                            raise ValueError(f'专精 {default_key} 默认玩家配置需要且只能解析到一个')
                        frozen_baseline = validate_default_player_baseline(default_key, default_rows.get().player_equipment)
                        attribute_baseline_values = {
                            stat: self._int(values.get(f'gear_{stat}', 0), stat)
                            for stat in self.ATTRIBUTE_STATS
                        }
                        profile_fields = {
                            'name': f"本次 Battle.net 属性快照 · {values['battlenet_character']}",
                            'spec': target_spec,
                            'player_config_mode': 'attribute_only',
                            'player_equipment': frozen_baseline,
                        }
                    else:
                        frozen_baseline = validate_player_baseline(values.get('player_equipment'))
                        profile_fields = {
                            'name': f"本次 Battle.net 玩家快照 · {values['battlenet_character']}",
                            'spec': target_spec,
                            'player_config_mode': 'manual_equipment',
                            'battlenet_region': values.get('battlenet_region', ''),
                            'battlenet_realm': values.get('battlenet_realm', ''),
                            'battlenet_character': values.get('battlenet_character', ''),
                            'player_equipment': frozen_baseline,
                        }
                else:
                    raise ValueError('请选择玩家配置来源')
                profile = SimcProfile(user_id=request.user.id, is_active=True, **profile_fields)
                transient_profile = True

            spec = str(profile.spec or '').strip().lower()
            mode = SimcProfileAPIView._profile_mode(profile)
            if kind not in ('attribute_variants', 'gear_candidates', 'talent_candidates'):
                raise ValueError('不支持的候选类型')
            if category and category not in ('trinket_candidates', 'gear_candidates', 'talent_candidates'):
                raise ValueError('不支持的候选类别')
            if category == 'trinket_candidates' and kind != 'gear_candidates':
                raise ValueError('饰品候选必须使用装备候选类型')
            if category in ('gear_candidates', 'talent_candidates') and category != kind:
                raise ValueError('候选类别与任务类型不匹配')
            if not name or not spec:
                raise ValueError('任务名称和 Profile 专精不能为空')
            if mode not in ('attribute_only', 'manual_equipment', 'battlenet'):
                raise ValueError('比较任务仅支持 attribute_only、manual_equipment 或 battlenet Profile')
            fight_style = str(data.get('fight_style') or 'Patchwerk').strip()
            fight_time = max(1, self._int(data.get('time', 300), '战斗时长'))
            target_count = max(1, self._int(data.get('target_count', 1), '目标数量'))
            specs = []

            if kind == 'attribute_variants':
                if mode not in ('attribute_only', 'manual_equipment', 'battlenet'):
                    raise ValueError('自动属性比较仅支持属性配置、装备列表或 Battle.net 配置')
                if mode in ('attribute_only', 'manual_equipment') and not str(profile.player_equipment or '').strip():
                    raise ValueError('自动属性比较需要 Profile 包含玩家装备基线')
                step = self._int(data.get('attribute_step'), '属性步长')
                if step != self.ATTRIBUTE_SEARCH_STEP:
                    raise ValueError(f'四属性自动寻优固定使用 {self.ATTRIBUTE_SEARCH_STEP} 绿字步长')
                if mode == 'manual_equipment':
                    specs.append({
                        'label': '基准属性探测',
                        'is_base': True,
                        'candidate': {
                            'type': 'attribute_baseline_probe',
                            'algorithm': 'four_stat_pairwise_hill_climb',
                            'algorithm_version': 2,
                            'round': 1,
                            'step': self.ATTRIBUTE_SEARCH_STEP,
                            'baseline_source': 'simc_report_gear_amount',
                            'move': {'type': 'baseline'},
                        },
                    })
                else:
                    values = attribute_baseline_values or {
                        stat: int(getattr(profile, f'gear_{stat}', 0) or 0)
                        for stat in self.ATTRIBUTE_STATS
                    }
                    for label, ratings, is_base, candidate in self._attribute_variants(values, step):
                        specs.append({'label': label, 'is_base': is_base, 'gear': ratings, 'candidate': candidate})
            else:
                if mode != 'manual_equipment':
                    raise ValueError('装备和天赋候选比较需要手动 SimC 玩家块')
                player_equipment = str(profile.player_equipment or '').strip()
                if not player_equipment:
                    raise ValueError('所选 Profile 缺少手动装备配置')
                from botend.services.simc_player_config import parse_manual_simc_candidates
                parsed = parse_manual_simc_candidates(player_equipment)
                base_talent = parsed.get('base_talent') or ''
                include_base = data.get('include_base', True) is not False
                if include_base:
                    specs.append({'label': '基准配置', 'is_base': True, 'player_equipment': player_equipment, 'talent': base_talent, 'candidate': {'type': 'base'}})
                submitted = data.get('candidates') or []
                if not isinstance(submitted, list) or not submitted:
                    raise ValueError('请至少选择一个可信候选')
                if len(submitted) + (1 if include_base else 0) > self.MAX_TASKS:
                    raise ValueError(f'每批最多{self.MAX_TASKS}个任务')
                if kind == 'gear_candidates':
                    trusted = {(row['slot'], row['item_id'], row['source']): row for row in parsed['gear_candidates']}
                    for candidate in submitted:
                        source = str(candidate.get('source') or '')
                        if source == 'manual':
                            slot = EQUIPMENT_SLOT_ALIASES.get(str(candidate.get('slot') or '').strip().lower(), str(candidate.get('slot') or '').strip().lower())
                            if slot not in EQUIPMENT_SLOTS:
                                raise ValueError('手工候选的槽位或 SimC 装备配置无效')
                            raw_value = normalize_gear_candidate_value(slot, candidate.get('raw_value'))
                            item_match = re.search(r'(?:^|,)\s*id=(\d+)', raw_value, re.IGNORECASE)
                            candidate['slot'] = slot
                            candidate['item_id'] = int(item_match.group(1))
                            candidate['raw_value'] = raw_value
                            candidate['source'] = 'manual'
                            trusted[(slot, candidate['item_id'], 'manual')] = {
                                'slot': slot, 'item_id': candidate['item_id'], 'source': 'manual',
                                'raw_value': candidate['raw_value'], 'name': str(candidate.get('name') or '').strip(),
                            }
                    submitted_keys = [(str(candidate.get('slot') or ''), candidate.get('item_id'), str(candidate.get('source') or '')) for candidate in submitted]
                    if len(set(submitted_keys)) != len(submitted_keys):
                        raise ValueError('候选装备不可重复选择')
                    for candidate, key in zip(submitted, submitted_keys):
                        if key not in trusted:
                            raise ValueError('候选装备的来源、槽位或物品不可信')
                        row = trusted[key]
                        lines = []
                        replaced = False
                        in_candidate_section = False
                        for line in player_equipment.splitlines():
                            stripped = line.strip()
                            # Candidate blocks in an exported SimC profile are not part of
                            # the equipped baseline and must never satisfy replacement.
                            if stripped.startswith('###'):
                                in_candidate_section = True
                            current_key = line.partition('=')[0].strip().lower()
                            canonical_current_key = EQUIPMENT_SLOT_ALIASES.get(current_key, current_key)
                            if canonical_current_key == row['slot'] and not replaced and not in_candidate_section:
                                lines.append(f"{current_key}={row['raw_value']}")
                                replaced = True
                            else:
                                lines.append(line)
                        if not replaced:
                            raise ValueError(f'基准玩家块未包含可替换的装备槽位: {row["slot"]}')
                        specs.append({
                            'label': row['name'] or f"{row['slot']} #{row['item_id']}",
                            'is_base': False,
                            'player_equipment': '\n'.join(lines),
                            'talent': base_talent,
                            'candidate': {
                                'type': 'gear_swap', 'slot': row['slot'],
                                'raw_value': row['raw_value'], 'item_id': row['item_id'],
                                'source': row['source'],
                            },
                        })
                else:
                    trusted = {
                        row['talent']: row
                        for row in [*parsed['talent_candidates'], *source_talent_candidates]
                    }
                    submitted_talents = [str(candidate.get('talent') or '').strip() for candidate in submitted]
                    if len(set(submitted_talents)) != len(submitted_talents):
                        raise ValueError('候选天赋不可重复选择')
                    for submitted_candidate, talent in zip(submitted, submitted_talents):
                        source = str(submitted_candidate.get('source') or '').strip()
                        if source == 'manual':
                            candidate_name = str(submitted_candidate.get('name') or '').strip()
                            if not candidate_name:
                                raise ValueError('手工候选必须填写方案名称')
                            if not talent:
                                raise ValueError('手工候选必须填写完整天赋字符串')
                            row = {'name': candidate_name, 'talent': talent, 'source': 'manual'}
                        else:
                            if talent not in trusted:
                                raise ValueError('候选天赋来源不可信')
                            row = trusted[talent]
                        lines = []
                        replaced = False
                        for line in player_equipment.splitlines():
                            if line.partition('=')[0].strip().lower() in ('talent', 'talents') and not replaced:
                                lines.append(f'talents={talent}')
                                replaced = True
                            else:
                                lines.append(line)
                        if not replaced:
                            # Canonical saved Profiles deliberately keep talents in the
                            # dedicated field instead of duplicating it in the actor export.
                            # The candidate override is authoritative for this comparison run.
                            if str(profile.talent or '').strip():
                                lines.append(f'talents={talent}')
                            else:
                                raise ValueError('基准玩家配置未提供可替换的 talents，无法创建天赋对比')
                        specs.append({
                            'label': row['name'] or '候选天赋',
                            'is_base': False,
                            'player_equipment': '\n'.join(lines),
                            'talent': talent,
                            'candidate': {
                                'type': 'talent', 'name': row['name'] or '候选天赋',
                                'talent': talent, 'source': row['source'],
                            },
                        })

            if not specs:
                raise ValueError('请至少选择一个可模拟方案')
            candidates = []
            for index, item in enumerate(specs):
                candidate = item['candidate']
                candidate_type = candidate.get('type') or 'base'
                candidate_params = {
                    'candidate_type': candidate_type,
                    'is_base': item['is_base'],
                    'search': {'candidate_index': index},
                }
                if candidate_type == 'gear_swap':
                    candidate_params['gear_swap'] = {
                        key: candidate.get(key)
                        for key in ('slot', 'raw_value', 'item_id', 'source')
                    }
                elif candidate_type == 'talent':
                    candidate_params['candidate_type'] = 'talent_override'
                    candidate_params['talent_override'] = candidate.get('talent')
                    candidate_params['talent_candidate'] = {
                        key: candidate.get(key) for key in ('name', 'talent', 'source')
                    }
                elif kind == 'attribute_variants' and candidate_type == 'attribute_baseline_probe':
                    candidate_params['search'] = {**candidate, 'candidate_index': index}
                elif kind == 'attribute_variants':
                    candidate_params['candidate_type'] = 'attribute_ratings'
                    candidate_params['attribute_ratings'] = item['gear']
                    candidate_params['search'] = {**candidate, 'candidate_index': index}
                candidates.append({
                    'candidate_key': (
                        'round-1-baseline-probe'
                        if candidate_type == 'attribute_baseline_probe'
                        else f'candidate-{index}'
                    ),
                    'candidate_label': item['label'],
                    'round_number': int((candidate_params.get('search') or {}).get('round') or 1),
                    'candidate_params': candidate_params,
                })

            profile_fields = {
                **({} if transient_profile else {'simc_profile_id': profile.id}),
                'name': profile.name, 'spec': profile.spec,
                'player_config_mode': profile.player_config_mode,
                'battlenet_region': profile.battlenet_region or '',
                'battlenet_realm': profile.battlenet_realm or '',
                'battlenet_character': profile.battlenet_character or '',
                'player_equipment': profile.player_equipment or '', 'talent': profile.talent or '',
                'gear_strength': profile.gear_strength, 'gear_crit': profile.gear_crit,
                'gear_haste': profile.gear_haste, 'gear_mastery': profile.gear_mastery,
                'gear_versatility': profile.gear_versatility,
            }
            task_mode = 'attribute_sweep' if kind == 'attribute_variants' else 'comparison'
            simulation_params = {
                'fight_style': fight_style,
                'max_time': fight_time,
                'desired_targets': target_count,
            }
            if 'raid_buffs' in data:
                simulation_params['raid_buffs'] = data['raid_buffs']
            if 'use_class_raid_buff' in data:
                simulation_params['use_class_raid_buff'] = data['use_class_raid_buff']
            option_error = validate_simulation_options(simulation_params)
            if option_error:
                raise ValueError(option_error)
            task = create_task_from_request(
                user_id=request.user.id, profile_fields=profile_fields,
                base_template_id=base_template_id, selected_apl_id=selected_apl_id,
                simulation_params=simulation_params,
                name=name, mode=task_mode,
                mode_params={'request_manifest': {
                    'kind': kind, 'category': category or kind, 'candidate_count': len(specs),
                }},
                candidates=candidates,
                backend_id=data.get('backend_id'),
                is_admin=_is_simc_admin(request.user),
            )
            return JsonResponse({'success': True, 'data': {
                'task_id': task.id, 'run_ids': [],
                'mode': task.mode,
                'accepted': len(candidates),
            }})
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': '无效的JSON数据'})
        except ValueError as e:
            return JsonResponse({'success': False, 'error': str(e)})
        except Exception as e:
            logger.error(f'创建 SimC 比较任务失败: {e}\n{traceback.format_exc()}')
            return JsonResponse({'success': False, 'error': f'创建比较任务失败: {e}'})


@method_decorator(login_required, name='dispatch')
class SimcPlayerConfigDetailAPIView(View):
    """只解析工作台当前玩家输入，返回结构化配置详情；不渲染完整 SimC 执行文本。"""

    def get(self, request):
        """Return structured detail for an owner or read-only upstream Profile."""
        profile_id = request.GET.get('profile_id')
        if not profile_id:
            return JsonResponse({'success': False, 'error': '请先选择已有 Profile'}, status=400)
        profile = SimcProfile.objects.filter(
            _accessible_simc_profile_q(request.user),
            id=profile_id,
            **({} if _is_simc_admin(request.user) else {'is_active': True}),
        ).first()
        if not profile:
            return JsonResponse({'success': False, 'error': 'Profile 不存在或无权使用'}, status=404)
        from botend.services.simc_player_config import build_player_config_detail
        detail = build_player_config_detail(
            mode=SimcProfileAPIView._profile_mode(profile), spec=profile.spec,
            player_equipment=profile.player_equipment or '',
            battlenet_region=profile.battlenet_region or '',
            battlenet_realm=profile.battlenet_realm or '',
            battlenet_character=profile.battlenet_character or '',
            talent=profile.talent or '', gear_strength=profile.gear_strength,
            gear_crit=profile.gear_crit, gear_haste=profile.gear_haste,
            gear_mastery=profile.gear_mastery, gear_versatility=profile.gear_versatility,
        )
        if profile.player_equipment and 'comparison_candidates' in detail:
            detail['comparison_candidates']['max_selectable'] = SimcComparisonTaskAPIView.MAX_TASKS - 1
        detail['profile'] = {
            'id': profile.id,
            'name': profile.name,
            'spec': profile.spec,
            'canonical_spec': canonical_simc_profile_key(profile.spec, profile.class_name),
            'spec_label': _simc_spec_label(profile.spec, profile.class_name),
            'talent': profile.talent or '',
            'use_ptr': bool(profile.use_ptr),
            'version': profile.version,
            'is_active': profile.is_active,
            'is_system': profile.user_id is None and profile.source == SimcProfile.SOURCE_SIMC_UPSTREAM,
            'can_edit': (
                _is_simc_admin(request.user)
                or (profile.user_id == request.user.id and profile.is_active)
            ),
            'raw_player_equipment': profile.player_equipment or '',
        }
        detail['talent_versions'] = SimcProfileAPIView._talent_simulator_versions()
        return JsonResponse({'success': True, 'data': detail})

    def post(self, request):
        try:
            data = json.loads(request.body)
            spec = str(data.get('spec') or '').strip()
            mode = data.get('player_import_mode') or data.get('player_config_mode')
            if mode == 'equipment':
                mode = 'manual_equipment'
            if mode not in ('battlenet', 'manual_equipment', 'attribute_only', 'simc_addon'):
                return JsonResponse({'success': False, 'error': '玩家信息导入方式无效'}, status=400)
            player_equipment = str(data.get('player_equipment') or data.get('simc_code') or '').strip()
            canonical_spec = ''
            if mode == 'simc_addon':
                if not player_equipment:
                    return JsonResponse({'success': False, 'error': 'SimC Addon 代码不能为空'}, status=400)
                parsed_identity = parse_manual_player_config(player_equipment, '').get('identity') or {}
                actor = str(parsed_identity.get('class_name') or '').strip().lower()
                parsed_spec = str(parsed_identity.get('spec') or '').strip().lower()
                spec_class, canonical_key = canonical_simc_spec_identity(f'{actor}_{parsed_spec}')
                canonical_spec = f'{spec_class}_{canonical_key}' if spec_class and canonical_key else ''
                if not actor or actor != spec_class or canonical_spec not in SIMC_SPEC_VALUES:
                    return JsonResponse({'success': False, 'error': '无法识别或不支持的职业专精'}, status=400)
                spec = canonical_spec
                mode = 'manual_equipment'
            elif not spec:
                return JsonResponse({'success': False, 'error': '请先选择专精'}, status=400)
            battlenet_region = str(data.get('battlenet_region') or '').strip().lower()
            battlenet_realm = str(data.get('battlenet_realm') or '').strip()
            battlenet_character = str(data.get('battlenet_character') or '').strip()
            if mode == 'manual_equipment' and not player_equipment:
                return JsonResponse({'success': False, 'error': '手动装备模式下玩家装备配置不能为空'})
            if mode == 'battlenet' and battlenet_region == 'cn':
                return JsonResponse({'success': False, 'error': '国服角色无法通过 Battle.net 加载，请改用 SimC Addon 导入'}, status=400)
            if mode == 'battlenet' and (
                battlenet_region not in ('us', 'eu', 'kr', 'tw')
                or not battlenet_realm or not battlenet_character
            ):
                return JsonResponse({'success': False, 'error': 'Battle.net 导入需要提供 region、realm 和 character'})
            if mode == 'attribute_only' and not player_equipment:
                try:
                    player_equipment = resolve_attribute_player_baseline(spec, player_equipment)
                except ValueError:
                    # Detail remains backward-compatible for legacy profiles; creation paths
                    # still require a valid explicit or default frozen baseline.
                    player_equipment = ''
            from botend.services.simc_player_config import build_player_config_detail
            detail = build_player_config_detail(
                mode=mode, spec=spec, player_equipment=player_equipment,
                battlenet_region=battlenet_region, battlenet_realm=battlenet_realm,
                battlenet_character=battlenet_character,
                talent=str(data.get('talent') or '').strip(), gear_strength=data.get('gear_strength'),
                gear_crit=data.get('gear_crit'), gear_haste=data.get('gear_haste'),
                gear_mastery=data.get('gear_mastery'), gear_versatility=data.get('gear_versatility'),
            )
            if mode == 'manual_equipment' and 'comparison_candidates' in detail:
                detail['comparison_candidates']['max_selectable'] = SimcComparisonTaskAPIView.MAX_TASKS - 1
            response = {'success': True, 'data': detail}
            if canonical_spec:
                response['canonical_spec'] = canonical_spec
            return JsonResponse(response)
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': '无效的JSON数据'})
        except Exception as e:
            logger.error(f"生成 SimC 玩家配置详情失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({'success': False, 'error': f'刷新详情失败: {str(e)}'})


WOW_SIMC_CLASS_NAMES = {
    'deathknight', 'death_knight', 'demonhunter', 'demon_hunter', 'druid', 'evoker',
    'hunter', 'mage', 'monk', 'paladin', 'priest', 'rogue', 'shaman', 'warlock', 'warrior'
}

WOW_SIMC_CLASS_ALIASES = {
    'death_knight': 'deathknight',
    'demon_hunter': 'demonhunter',
}


def _normalize_simc_token(value):
    return re.sub(r'[^a-z0-9_]+', '_', str(value or '').strip().lower()).strip('_')


def _get_active_simc_content(spec=None, source=None, class_name=None, selectable=None):
    qs = SimcContentTemplate.objects.filter(is_active=True)
    if source:
        qs = qs.filter(source=source)
    if selectable is not None:
        qs = qs.filter(is_selectable=selectable)
    if class_name:
        qs = qs.filter(class_name=class_name)
    if spec:
        spec_value = str(spec or '').strip().lower()
        exact = qs.filter(spec=spec_value).order_by('id').first()
        if exact:
            return exact
        if '_' not in spec_value:
            suffix = qs.filter(spec__endswith=f'_{spec_value}').order_by('id').first()
            if suffix:
                return suffix
    return qs.order_by('id').first()


def _list_selectable_apl_for_spec(spec_key='', class_name='', spec='', owner_user_id=None):
    qs = SimcApl.objects.filter(is_active=True).filter(
        models.Q(is_system=True, is_selectable=True, owner_user_id__isnull=True)
        | models.Q(is_system=False, owner_user_id=owner_user_id)
    )
    specs = [v for v in [spec_key, spec] if v]
    if specs:
        filters = models.Q(spec__in=specs)
        if spec and '_' not in spec:
            filters |= models.Q(spec__endswith=f'_{spec}')
        qs = qs.filter(filters)
    if class_name:
        qs = qs.filter(models.Q(class_name='') | models.Q(class_name=class_name))
    rows = []
    for item in qs.order_by('source', 'name', 'id')[:50]:
        rows.append({
            'id': item.id,
            'name': item.name or item.spec,
            'source': item.source,
            'spec': item.spec,
            'class_name': item.class_name,
            'content_length': len(item.content or ''),
            'is_default': False,
        })
    return rows


def _resolve_home_creation_defaults(spec_key, class_name='', owner_user_id=None):
    """Resolve the one explicit system APL and base template for the home flow."""
    default_apls = SimcApl.objects.filter(
        is_active=True, is_selectable=True, is_system=True,
        owner_user_id__isnull=True, spec=spec_key,
    )
    if class_name:
        default_apls = default_apls.filter(models.Q(class_name='') | models.Q(class_name=class_name))
    apl_count = default_apls.count()
    if apl_count != 1:
        raise ValueError(f'专精 {spec_key} 需要且只能有一个系统默认 APL，当前为 {apl_count} 个')
    default_apl = default_apls.get()

    templates = SimcContentTemplate.objects.filter(
        is_active=True, is_selectable=True,
    ).filter(models.Q(owner_user_id__isnull=True) | models.Q(owner_user_id=owner_user_id))
    if class_name:
        templates = templates.filter(models.Q(class_name='') | models.Q(class_name=class_name))
    exact = templates.filter(spec=spec_key)
    candidates = exact if exact.exists() else templates.filter(spec__in=('default', 'all', '*'))
    template_count = candidates.count()
    if template_count != 1:
        detail = '缺少' if template_count == 0 else f'存在多个（{template_count} 个）'
        raise ValueError(f'专精 {spec_key} 基础模板{detail}，需要且只能解析到一个')
    return default_apl, candidates.get()


def _get_simc_content_by_id(content_id, owner_user_id=None):
    if not content_id:
        return None
    try:
        qs = SimcContentTemplate.objects.filter(id=int(content_id), is_active=True)
    except (TypeError, ValueError):
        return None
    qs = qs.filter(models.Q(owner_user_id__isnull=True) | models.Q(owner_user_id=owner_user_id))
    return qs.first()


def _get_unique_default_apl_for_spec(spec, owner_user_id=None):
    """返回当前用户可见的默认 APL；用户模板优先于全局上游模板。"""
    spec_value = str(spec or '').strip().lower()
    spec_key = f'warrior_{spec_value}' if spec_value in ('fury', 'arms', 'protection') else spec_value
    candidates = SimcApl.objects.filter(is_active=True, spec=spec_key)
    if owner_user_id is not None:
        owned = candidates.filter(owner_user_id=owner_user_id, is_system=False)
        if owned.count() > 1:
            raise Exception(f'专精 {spec_key} 存在多个可用的个人 APL，请明确选择一个')
        if owned.count() == 1:
            return owned.first()
    global_defaults = candidates.filter(owner_user_id__isnull=True, is_system=True)
    if global_defaults.count() > 1:
        raise Exception(f'专精 {spec_key} 存在多个系统默认 APL，请明确选择一个')
    return global_defaults.first()


def inspect_raw_simc_code(raw_simc_code):
    """Removed obsolete raw-code inspection entry point."""
    raise ValueError('不再支持直接 SimC 代码模式；请使用引用型 Profile、模板和 APL')
    text = str(raw_simc_code or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        raise ValueError('SimC代码不能为空')

    result = {
        'character_name': '',
        'class': '',
        'spec': '',
        'spec_key': '',
        'role': '',
        'level': '',
        'race': '',
        'default_apl_id': None,
        'default_apl_available': False,
        'default_apl_length': 0,
        'available_apls': [],
        'warnings': [],
        'plans': [],
    }

    profile_line_re = re.compile(r'^\s*([a-zA-Z_]+)\s*=\s*(?:"([^"]+)"|([^\s#]+))')
    kv_re = re.compile(r'^\s*([a-zA-Z_]+)\s*=\s*([^#\s]+)')

    for raw_line in text.split('\n'):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        profile_match = profile_line_re.match(line)
        if profile_match:
            class_token = _normalize_simc_token(profile_match.group(1))
            normalized_class = WOW_SIMC_CLASS_ALIASES.get(class_token, class_token)
            if normalized_class in WOW_SIMC_CLASS_NAMES:
                result['class'] = normalized_class
                result['character_name'] = (profile_match.group(2) or profile_match.group(3) or '').strip()
                continue
        kv_match = kv_re.match(line)
        if not kv_match:
            continue
        key = kv_match.group(1).strip().lower()
        value = kv_match.group(2).strip().strip('"')
        if key == 'spec' and not result['spec']:
            result['spec'] = _normalize_simc_token(value)
        elif key == 'role' and not result['role']:
            result['role'] = value
        elif key == 'level' and not result['level']:
            result['level'] = value
        elif key == 'race' and not result['race']:
            result['race'] = _normalize_simc_token(value)

    class_name = result['class']
    spec = result['spec']
    if class_name and spec:
        result['spec_key'] = f'{class_name}_{spec}'
        apl = None
        apl_candidates = SimcApl.objects.filter(
            spec=result['spec_key'], source='simc_upstream', is_active=True, is_selectable=True,
            is_system=True, owner_user_id__isnull=True,
        )
        if apl_candidates.count() == 1:
            apl = apl_candidates.first()
        if apl:
            result['default_apl_id'] = apl.id
            result['default_apl_available'] = True
            result['default_apl_length'] = len(apl.content or '')
        result['available_apls'] = _list_selectable_apl_for_spec(
            spec_key=result['spec_key'],
            class_name=class_name,
            spec=spec,
            owner_user_id=None,
        )
    else:
        if not class_name:
            result['warnings'].append('未识别到职业行，例如 hunter="角色名"')
        if not spec:
            result['warnings'].append('未识别到 spec= 专精字段')

    if class_name and spec and not result['default_apl_available']:
        result['warnings'].append(f'未找到 {result["spec_key"]} 的默认APL记录；直接代码仍可运行常规模拟')

    plan_name_parts = [result['character_name'] or 'Raw SimC']
    if spec:
        plan_name_parts.append(spec)
    result['plans'] = [
        {
            'id': 'regular',
            'label': '常规模拟',
            'enabled': True,
            'checked': True,
            'mode': 'normal',
            'default_time': 300,
            'default_target_count': 1,
            'task_name': ' '.join(plan_name_parts) + ' 常规模拟',
            'reason': '',
        },
        {
            'id': 'attribute',
            'label': '属性寻优',
            'enabled': False,
            'checked': False,
            'mode': 'attribute_sweep',
            'reason': '属性寻优需要先保存为 SimC 配置，再基于配置生成候选 Run',
        },
        {
            'id': 'apl_compare',
            'label': 'APL候选对比',
            'enabled': False,
            'checked': False,
            'mode': 'comparison',
            'reason': 'APL候选对比需要配置化 Profile 和可替换 action_list，raw 代码首版仅开放常规模拟',
        },
    ]
    return result


@method_decorator(login_required, name='dispatch')
class SimcRawInspectAPIView(View):
    """Inspect pasted raw SimulationCraft code and return safe task plans."""

    def post(self, request):
        return JsonResponse({'success': False, 'error': '该接口已停用，请使用引用型 Profile、模板和 APL'}, status=410)


@method_decorator(login_required, name='dispatch')
class AplStorageAPIView(View):
    """
    APL存储API
    """
    
    def get(self, request):
        """获取用户的APL列表"""
        try:
            user = request.user
            apl_list = SimcApl.objects.filter(is_active=True)
            if not _is_simc_admin(user):
                apl_list = apl_list.filter(owner_user_id=user.id)
            apl_list = apl_list.order_by('-id')

            result = []
            for apl in apl_list:
                result.append({
                    'id': apl.id,
                    'title': apl.name,
                    'spec': apl.spec,
                })

            return JsonResponse({
                'success': True,
                'data': result
            })

        except Exception as e:
            logger.error(f"获取APL列表失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '获取APL列表失败'
            })
    
    def post(self, request):
        """保存新的APL或从默认模板复制"""
        try:
            data = json.loads(request.body)
            copy_template_id = data.get('copy_template_id')

            if copy_template_id:
                template = SimcApl.objects.filter(
                    models.Q(owner_user_id=request.user.id)
                    | models.Q(is_system=True, owner_user_id__isnull=True),
                    id=copy_template_id,
                    is_system=True,
                    is_active=True,
                    is_selectable=True,
                ).first()
                if not template:
                    return JsonResponse({
                        'success': False,
                        'error': '模板不存在或不可复制'
                    }, status=404)

                base_title = template.name or 'APL'
                title = base_title
                counter = 1
                while SimcApl.objects.filter(
                    owner_user_id=request.user.id,
                    name=title,
                    is_active=True
                ).exists():
                    title = f"{base_title} 副本 {counter}"
                    counter += 1

                apl_storage = SimcApl.objects.create(
                    owner_user_id=request.user.id,
                    name=title,
                    spec=template.spec or '',
                    class_name=template.class_name or '',
                    content=template.content,
                    source='user',
                    is_system=False,
                    is_selectable=False,
                    validation_status=SimcApl.VALIDATION_DRAFT,
                )

                return JsonResponse({
                    'success': True,
                    'message': 'APL 复制成功',
                    'data': {
                        'id': apl_storage.id,
                        'title': apl_storage.name,
                        'spec': apl_storage.spec,
                    }
                })

            title = data.get('title', '').strip()
            spec = data.get('spec', '').strip()[:100]
            apl_code = data.get('apl_code', '').strip()

            if not title:
                return JsonResponse({
                    'success': False,
                    'error': 'APL标题不能为空'
                })

            if not apl_code:
                return JsonResponse({
                    'success': False,
                    'error': 'APL代码不能为空'
                })

            # 检查标题是否重复
            if SimcApl.objects.filter(
                owner_user_id=request.user.id,
                name=title,
                is_active=True
            ).exists():
                return JsonResponse({
                    'success': False,
                    'error': '该标题已存在，请使用其他标题'
                })

            # 创建新的APL存储记录
            apl_storage = SimcApl.objects.create(
                owner_user_id=request.user.id,
                name=title,
                spec=spec,
                content=apl_code,
                source='user',
                is_system=False,
                is_selectable=False,
                validation_status=SimcApl.VALIDATION_DRAFT,
            )

            return JsonResponse({
                'success': True,
                'message': 'APL保存成功',
                'data': {
                    'id': apl_storage.id,
                    'title': apl_storage.name,
                    'spec': apl_storage.spec,
                }
            })

        except Exception as e:
            logger.error(f"保存APL失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '保存APL失败'
            })
    
    def put(self, request):
        """更新APL"""
        try:
            data = json.loads(request.body)
            apl_id = data.get('id')
            title = data.get('title', '').strip()
            spec = data.get('spec', '').strip()[:100]
            apl_code = data.get('apl_code', '').strip()

            if not apl_id:
                return JsonResponse({
                    'success': False,
                    'error': 'APL ID不能为空'
                })

            if not title:
                return JsonResponse({
                    'success': False,
                    'error': 'APL标题不能为空'
                })

            if not apl_code:
                return JsonResponse({
                    'success': False,
                    'error': 'APL代码不能为空'
                })

            try:
                apl_storage = SimcApl.objects.filter(id=apl_id, is_active=True)
                if not _is_simc_admin(request.user):
                    apl_storage = apl_storage.filter(owner_user_id=request.user.id)
                apl_storage = apl_storage.get()

                # 检查标题是否与其他记录重复
                if SimcApl.objects.filter(
                    owner_user_id=apl_storage.owner_user_id,
                    name=title,
                    is_active=True
                ).exclude(id=apl_id).exists():
                    return JsonResponse({
                        'success': False,
                        'error': '该标题已存在，请使用其他标题'
                    })

                # 更新记录
                apl_storage.name = title
                apl_storage.spec = spec
                apl_storage.content = apl_code
                apl_storage.save(update_fields=['name', 'spec', 'content', 'updated_at'])

                return JsonResponse({
                    'success': True,
                    'message': 'APL更新成功'
                })

            except SimcApl.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': 'APL记录不存在'
                })

        except Exception as e:
            logger.error(f"更新APL失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '更新APL失败'
            })
    
    def delete(self, request):
        """删除APL"""
        try:
            data = json.loads(request.body)
            apl_id = data.get('id')

            if not apl_id:
                return JsonResponse({
                    'success': False,
                    'error': 'APL ID不能为空'
                })

            try:
                apl_storage = SimcApl.objects.filter(id=apl_id, is_active=True)
                if not _is_simc_admin(request.user):
                    apl_storage = apl_storage.filter(owner_user_id=request.user.id)
                apl_storage = apl_storage.get()

                # 软删除
                apl_storage.is_active = False
                apl_storage.save()

                return JsonResponse({
                    'success': True,
                    'message': 'APL删除成功'
                })

            except SimcApl.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': 'APL记录不存在'
                })

        except Exception as e:
            logger.error(f"删除APL失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '删除APL失败'
            })


@method_decorator(login_required, name='dispatch')
class AplDetailAPIView(View):
    """
    APL详情API
    """
    
    def get(self, request, apl_id):
        """获取APL详情"""
        try:
            apl_storage = SimcApl.objects.filter(id=apl_id, is_active=True)
            if not _is_simc_admin(request.user):
                apl_storage = apl_storage.filter(owner_user_id=request.user.id)
            apl_storage = apl_storage.get()

            return JsonResponse({
                'success': True,
                'data': {
                    'id': apl_storage.id,
                    'title': apl_storage.name,
                    'spec': apl_storage.spec,
                    'spec_label': _simc_spec_label(apl_storage.spec, apl_storage.class_name),
                    'apl_code': apl_storage.content
                }
            })

        except SimcApl.DoesNotExist:
            return JsonResponse({
                'success': False,
                'error': 'APL记录不存在'
            })
        except Exception as e:
            logger.error(f"获取APL详情失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '获取APL详情失败'
            })


@method_decorator(login_required, name='dispatch')
class SimcSpecOptionsAPIView(View):
    def get(self, request):
        return JsonResponse({'success': True, 'data': SIMC_SPEC_OPTIONS})


@method_decorator(login_required, name='dispatch')
class SimcBattlenetPreflightAPIView(View):
    """Fetch and validate Battle.net character data before it is saved or simulated."""

    def post(self, request):
        try:
            data = json.loads(request.body or '{}')
            region = str(data.get('region') or data.get('battlenet_region') or '').strip().lower()
            if region == 'cn':
                raise ValueError('国服角色无法通过 Battle.net 加载，请改用 SimC Addon 导入')
            from botend.services.battlenet_preflight import fetch_battlenet_character_preflight
            result = fetch_battlenet_character_preflight(
                region=region,
                realm=str(data.get('realm') or data.get('battlenet_realm') or '').strip(),
                character=str(data.get('character') or data.get('battlenet_character') or '').strip(),
                requested_spec=str(data.get('spec') or '').strip().lower(),
            )
            class_name = str((result.get('identity') or {}).get('class_name') or '').strip().lower()
            spec_key = str((result.get('spec') or {}).get('key') or '').strip().lower()
            canonical_class, canonical_key = canonical_simc_spec_identity(f'{class_name}_{spec_key}')
            canonical_spec = f'{canonical_class}_{canonical_key}' if canonical_class and canonical_key else ''
            if not class_name or class_name != canonical_class or canonical_spec not in SIMC_SPEC_VALUES:
                raise ValueError('Battle.net 返回了无法识别或不支持的职业专精')
            result['canonical_spec'] = canonical_spec
            return JsonResponse({'success': True, 'data': result})
        except ValueError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=400)
        except Exception:
            logger.exception('Battle.net SimC preflight failed')
            return JsonResponse({'success': False, 'error': '获取 Battle.net 角色配置失败，请稍后重试'}, status=502)


@method_decorator(login_required, name='dispatch')
class SimcBattlenetTopPlayersAPIView(View):
    """Return current-season specialization leaders as reusable Battle.net identities."""

    def get(self, request):
        spec = _canonical_simc_spec(request.GET.get('spec'))
        db_identity = SIMC_SPEC_DB_IDENTITIES.get(spec)
        if not db_identity:
            return JsonResponse({'success': False, 'error': '请选择有效专精'}, status=400)
        db_class_name, db_spec_name = db_identity

        season = SpecStatsService.get_active_season()
        if not season:
            return JsonResponse({
                'success': True, 'spec': spec, 'season': None, 'data': [],
            })

        players = PlayerSpecTopPlayer.objects.filter(
            season_id=season.id,
            class_name=db_class_name,
            spec_name=db_spec_name,
            rank__isnull=False,
            score__isnull=False,
        ).exclude(region='cn').order_by('-score', 'rank', 'id').values(
            'id', 'rank', 'score', 'spec_name', 'region', 'realm', 'character_name',
        )
        rows = []
        seen_characters = set()
        for player in players:
            region = str(player['region'] or '').strip().lower()
            identity = (
                region,
                str(player['realm'] or '').strip().casefold(),
                str(player['character_name'] or '').strip().casefold(),
            )
            if identity in seen_characters:
                continue
            seen_characters.add(identity)
            row_spec = re.sub(r'(?<!^)(?=[A-Z])', '_', str(player['spec_name'] or '')).lower()
            rows.append({
                'id': player['id'],
                'rank': player['rank'],
                'score': player['score'],
                'spec': row_spec,
                'region': region,
                'realm': player['realm'],
                'character': player['character_name'],
                'label': f"{player['character_name']} · {player['realm']} · {region.upper()} · {_simc_spec_label(row_spec, db_class_name)}",
            })
            if len(rows) == 10:
                break
        return JsonResponse({
            'success': True,
            'spec': spec,
            'season': {'id': season.id, 'key': season.season_key, 'name': season.season_name},
            'data': rows,
        })


@method_decorator(login_required, name='dispatch')
class SimcProfileAPIView(View):
    """
    SimC配置管理API
    """
    
    @staticmethod
    def _profile_mode(profile):
        """Infer the legal legacy attribute-only form without rewriting stored data."""
        mode = (getattr(profile, 'player_config_mode', '') or '').strip()
        has_equipment = bool(getattr(profile, 'player_equipment', ''))
        has_battlenet_identity = any(
            getattr(profile, field, '') for field in ('battlenet_region', 'battlenet_realm', 'battlenet_character')
        )
        # 历史属性配置在新增 mode 字段时会被数据库默认值标记为 battlenet，
        # 但并没有 Battle.net 三元组或装备块。以实际持久化数据为准，不能让
        # 这个默认值遮蔽原有的 talent + ratings。
        if mode in ('battlenet', 'manual_equipment', 'attribute_only'):
            # Explicit modern mode is authoritative. Data-based inference is only for
            # legacy rows whose mode is empty/invalid; stale cross-mode fields must not
            # silently change execution semantics.
            if mode == 'battlenet' and not has_battlenet_identity and not has_equipment:
                return 'attribute_only'
            return mode
        if has_equipment:
            return 'manual_equipment'
        if has_battlenet_identity:
            return 'battlenet'
        return 'attribute_only'

    @staticmethod
    def _talent_simulator_versions():
        versions = {}
        rows = WowTalentVersion.objects.filter(is_active=True).order_by(
            'branch', '-is_default_simulator', '-updated_at', '-id',
        )
        for row in rows:
            branch = str(row.branch or '').strip().lower()
            if branch in ('retail', 'ptr') and branch not in versions:
                versions[branch] = row.key
        return versions

    def get(self, request, profile_id=None):
        """获取SimC配置列表或单个配置"""
        try:
            if profile_id:
                # 管理资源列表可见的上游/未生效 Profile 必须可被同一管理员
                # 读取进编辑表单；普通用户仍只能读取自己拥有的 Profile。
                profile = SimcProfile.objects.filter(
                    id=profile_id,
                    **({} if _is_simc_admin(request.user) else {'user_id': request.user.id}),
                ).first()
                if profile is None:
                    return JsonResponse({
                        'success': False,
                        'error': '配置不存在或无权限访问',
                    })
                return JsonResponse({
                    'success': True,
                    'id': profile.id,
                    'name': profile.name,
                    'spec': profile.spec,
                    'canonical_spec': canonical_simc_profile_key(profile.spec, profile.class_name),
                    'spec_label': _simc_spec_label(profile.spec, profile.class_name),
                    **_simc_spec_visual(profile.spec, profile.class_name),
                    'use_ptr': bool(profile.use_ptr),
                    'player_config_mode': self._profile_mode(profile),
                    'battlenet_region': getattr(profile, 'battlenet_region', '') or '',
                    'battlenet_realm': getattr(profile, 'battlenet_realm', '') or '',
                    'battlenet_character': getattr(profile, 'battlenet_character', '') or '',
                    'player_equipment': getattr(profile, 'player_equipment', '') or '',
                    'talent': profile.talent,
                    'talent_versions': self._talent_simulator_versions(),
                    'gear_strength': profile.gear_strength,
                    'gear_crit': profile.gear_crit,
                    'gear_haste': profile.gear_haste,
                    'gear_mastery': profile.gear_mastery,
                    'gear_versatility': profile.gear_versatility,
                    'is_active': profile.is_active,
                })
            else:
                # 用户配置与迁移后的 SimC 上游默认玩家属于同一 Profile 资源库。
                # 系统记录只在列表中展示，写接口仍严格按 user_id 校验所有权。
                if _is_simc_admin(request.user):
                    # 管理后台是资源审计入口，停用记录也必须可见；状态由列表表达。
                    profiles = SimcProfile.objects.all().order_by('user_id', 'class_name', 'spec', '-id')
                else:
                    profiles = SimcProfile.objects.filter(
                        models.Q(user_id=request.user.id)
                        | models.Q(
                            user_id__isnull=True,
                            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
                        ),
                        is_active=True,
                    ).order_by('user_id', 'class_name', 'spec', '-id')
                
                profile_list = []
                for profile in profiles:
                    is_system = (
                        profile.user_id is None
                        and profile.source == SimcProfile.SOURCE_SIMC_UPSTREAM
                    )
                    player_equipment = getattr(profile, 'player_equipment', '') or ''
                    profile_list.append({
                        'id': profile.id,
                        'name': profile.name,
                        'spec': profile.spec,
                        'canonical_spec': canonical_simc_profile_key(profile.spec, profile.class_name),
                        'spec_label': _simc_spec_label(profile.spec, profile.class_name),
                        **_simc_spec_visual(profile.spec, profile.class_name),
                        'version': profile.version,
                        'use_ptr': bool(profile.use_ptr),
                        'class_name': getattr(profile, 'class_name', '') or '',
                        'player_config_mode': self._profile_mode(profile),
                        'battlenet_region': getattr(profile, 'battlenet_region', '') or '',
                        'battlenet_realm': getattr(profile, 'battlenet_realm', '') or '',
                        'battlenet_character': getattr(profile, 'battlenet_character', '') or '',
                        'player_equipment': player_equipment,
                        'talent': profile.talent,
                        'gear_strength': profile.gear_strength,
                        'gear_crit': profile.gear_crit,
                        'gear_haste': profile.gear_haste,
                        'gear_mastery': profile.gear_mastery,
                        'gear_versatility': profile.gear_versatility,
                        'is_active': profile.is_active,
                        'is_system': is_system,
                        'can_edit': _is_simc_admin(request.user) or not is_system,
                        'can_delete': not is_system,
                        'source': profile.source,
                        'sync_version': getattr(profile, 'sync_version', '') or '',
                        'equipment_line_count': len([
                            line for line in player_equipment.splitlines() if line.strip()
                        ]),
                    })
                
                return JsonResponse({
                    'success': True,
                    'data': profile_list,
                    'talent_versions': self._talent_simulator_versions(),
                })
            
        except Exception as e:
            logger.error(f"获取SimC配置失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '获取SimC配置失败'
            })
    
    @staticmethod
    def _validate_profile_payload(data, fallback=None):
        """Validate all supported saved-player configuration modes consistently."""
        fallback = fallback or {}
        mode = str(data.get('player_config_mode') or data.get('player_import_mode') or fallback.get('mode') or '').strip().lower()
        if mode == 'equipment':
            mode = 'manual_equipment'
        if mode not in ('battlenet', 'manual_equipment', 'attribute_only'):
            raise ValueError('玩家信息导入方式必须是 battlenet、manual_equipment 或 attribute_only')

        raw_use_ptr = data['use_ptr'] if 'use_ptr' in data else fallback.get('use_ptr', False)
        if type(raw_use_ptr) is not bool:
            raise ValueError('use_ptr 必须是布尔值')
        values = {
            'mode': mode,
            'spec': str(data.get('spec', fallback.get('spec', 'fury')) or 'fury').strip().lower() or 'fury',
            'class_name': str(data.get('class_name', fallback.get('class_name', '')) or '').strip().lower(),
            'use_ptr': raw_use_ptr,
            'battlenet_region': str(data.get('battlenet_region', fallback.get('battlenet_region', '')) or '').strip().lower(),
            'battlenet_realm': str(data.get('battlenet_realm', fallback.get('battlenet_realm', '')) or '').strip(),
            'battlenet_character': str(data.get('battlenet_character', fallback.get('battlenet_character', '')) or '').strip(),
            'player_equipment': str(data.get('player_equipment', fallback.get('player_equipment', '')) or '').strip(),
            'talent': str(data.get('talent', fallback.get('talent', '')) or '').strip(),
        }
        profile_class, profile_spec = canonical_simc_profile_identity(values['spec'], values['class_name'])
        canonical_spec = f'{profile_class}_{profile_spec}' if profile_class and profile_spec else ''
        if canonical_spec not in SIMC_SPEC_VALUES:
            raise ValueError('必须选择有效且职业唯一的专精')
        values['class_name'], values['spec'] = profile_class, canonical_spec
        if mode == 'battlenet':
            if values['battlenet_region'] == 'cn':
                raise ValueError('国服角色无法通过 Battle.net 加载，请改用 SimC Addon 导入')
            if values['battlenet_region'] not in ('us', 'eu', 'kr', 'tw'):
                raise ValueError('Battle.net region 必须是 us、eu、kr 或 tw')
            if not values['battlenet_realm']:
                raise ValueError('Battle.net realm 不能为空')
            if not values['battlenet_character']:
                raise ValueError('Battle.net character 不能为空')
            preflight = fetch_battlenet_character_preflight(
                region=values['battlenet_region'], realm=values['battlenet_realm'],
                character=values['battlenet_character'], requested_spec=values['spec'],
            )
            if not preflight.get('simc_ready'):
                raise ValueError('；'.join(preflight.get('warnings') or ['Battle.net 角色不可用于模拟']))
            frozen = preflight.get('simc_config') or {}
            if not str(frozen.get('player_equipment') or '').strip():
                raise ValueError('Battle.net 角色未生成完整玩家快照')
            values.update(frozen)
            values['mode'] = 'battlenet'
            profile_class, profile_spec = canonical_simc_profile_identity(
                values.get('spec'), values.get('class_name'),
            )
            canonical_spec = f'{profile_class}_{profile_spec}' if profile_class and profile_spec else ''
            if canonical_spec not in SIMC_SPEC_VALUES:
                raise ValueError('Battle.net 返回了无效或无法唯一识别的专精')
            values['class_name'], values['spec'] = profile_class, canonical_spec
        elif mode == 'manual_equipment':
            values['battlenet_region'] = values['battlenet_realm'] = values['battlenet_character'] = ''
            if not values['player_equipment']:
                raise ValueError('manual_equipment 模式下 player_equipment 不能为空')
        elif mode == 'attribute_only':
            values['battlenet_region'] = values['battlenet_realm'] = values['battlenet_character'] = ''
            if not values['talent']:
                raise ValueError('attribute_only 模式下 talent 不能为空')
            values['player_equipment'] = resolve_attribute_player_baseline(values['spec'], values['player_equipment'])
        return values

    @staticmethod
    def _coerce_profile_number(data, field, fallback=None):
        """Keep omitted attribute overrides absent instead of coercing them to zero."""
        raw_value = data.get(field, fallback)
        if raw_value in (None, ''):
            return None
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            raise ValueError(f'{field} 必须是整数')

    @classmethod
    def _profile_numeric_values(cls, data, fallback=None, mode=None):
        # Explicit stat totals are an optional final override layer shared by all
        # Profile source modes. Omitted fields retain their stored value; null/empty
        # values explicitly clear that single override.
        fallback = fallback or {}
        return {
            field: cls._coerce_profile_number(data, field, fallback.get(field))
            for field in ('gear_strength', 'gear_crit', 'gear_haste', 'gear_mastery', 'gear_versatility')
        }

    def post(self, request):
        """创建新的SimC配置或复制现有配置，或者为现有配置创建模拟任务"""
        try:
            data = json.loads(request.body)
            if 'use_ptr' in data and type(data['use_ptr']) is not bool:
                return JsonResponse({'success': False, 'error': 'use_ptr 必须是布尔值'}, status=400)
            if 'task_type' in data:
                return JsonResponse({
                    'success': False,
                    'error': '不再支持 task_type 参数；请使用 SimC 工作台的任务模式入口。',
                }, status=400)
            
            # 检查是否为一键模拟操作
            simulate_now = data.get('simulate_now', False)
            profile_id = data.get('profile_id') or data.get('simc_profile_id')

            # 如果是一键模拟操作且提供了profile_id，直接创建任务
            if simulate_now and profile_id:
                try:
                    # Simulation is not an ownership boundary. Resource
                    # selectors remain filtered, but an explicit Profile ID
                    # may be simulated; task service validates executable state.
                    profile_qs = SimcProfile.objects.filter(id=profile_id)
                    profile = profile_qs.get()

                    regular_time = data.get('regular_time')
                    regular_target_count = data.get('regular_target_count')
                    base_template_id = data.get('base_template_id')
                    selected_apl_id = data.get('selected_apl_id')

                    task_result = self._create_simulation_task(
                        request.user.id,
                        profile,
                        regular_time=regular_time,
                        regular_target_count=regular_target_count,
                        base_template_id=base_template_id,
                        selected_apl_id=selected_apl_id,
                        is_admin=_is_simc_admin(request.user),
                    )

                    if task_result['success']:
                        return JsonResponse({
                            'success': True,
                            'message': '模拟任务创建成功',
                            'task_data': task_result['data']
                        })
                    else:
                        return JsonResponse({
                            'success': False,
                            'error': task_result['error']
                        })

                except SimcProfile.DoesNotExist:
                    return JsonResponse({
                        'success': False,
                        'error': 'SimC配置不存在'
                    })
            
            # 创建配置或复制现有配置。复制请求无需客户端填写名称，后端统一生成副本名。
            copy_from_id = data.get('copy_from_id')
            name = str(data.get('name') or '').strip()
            if copy_from_id and not name:
                source_profiles = SimcProfile.objects.all()
                if not _is_simc_admin(request.user):
                    source_profiles = source_profiles.filter(
                        models.Q(user_id=request.user.id)
                        | models.Q(
                            user_id__isnull=True,
                            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
                            is_active=True,
                        )
                    )
                try:
                    copy_name_source = source_profiles.get(id=copy_from_id)
                except SimcProfile.DoesNotExist:
                    return JsonResponse(
                        {'success': False, 'error': '源配置不存在或无权复制'},
                        status=404,
                    )
                base_name = str(copy_name_source.name or '未命名配置').strip() or '未命名配置'
                copy_number = 1
                while True:
                    suffix = ' 副本' if copy_number == 1 else f' 副本 {copy_number}'
                    name = f'{base_name[:200 - len(suffix)]}{suffix}'
                    if not SimcProfile.objects.filter(user_id=request.user.id, name=name).exists():
                        break
                    copy_number += 1

            if not name:
                return JsonResponse({
                    'success': False,
                    'error': '配置名称不能为空'
                })
            
            # 检查名称是否重复
            if SimcProfile.objects.filter(
                user_id=request.user.id,
                name=name,
                is_active=True
            ).exists():
                return JsonResponse({
                    'success': False,
                    'error': '配置名称已存在'
                })
            
            # 新建 Profile 并立即模拟必须走统一原子服务；资源校验失败时不保留 Profile。
            if simulate_now and not copy_from_id:
                try:
                    values = self._validate_profile_payload(data)
                    # Battle.net preflight returns observed character stats as display
                    # metadata. They are part of the frozen equipment snapshot, not
                    # user-authored overrides. Only request fields may populate gear_*.
                    numeric_values = self._profile_numeric_values(data, mode=values['mode'])
                    from botend.services.simc_task_service import create_task_from_request, TaskCreationError

                    profile_fields = {
                        'name': name,
                        'spec': values['spec'],
                        'class_name': values['class_name'],
                        'use_ptr': values['use_ptr'],
                        'player_config_mode': values['mode'],
                        'battlenet_region': values['battlenet_region'],
                        'battlenet_realm': values['battlenet_realm'],
                        'battlenet_character': values['battlenet_character'],
                        'player_equipment': values['player_equipment'],
                        'talent': values['talent'],
                        **numeric_values,
                    }
                    simulation_params = {}
                    if data.get('regular_time') is not None:
                        simulation_params['max_time'] = int(data['regular_time'])
                    if data.get('regular_target_count') is not None:
                        simulation_params['desired_targets'] = int(data['regular_target_count'])

                    task = create_task_from_request(
                        user_id=request.user.id,
                        profile_fields=profile_fields,
                        base_template_id=data.get('base_template_id'),
                        selected_apl_id=data.get('selected_apl_id'),
                        simulation_params=simulation_params or None,
                        name=f'{name}_常规模拟',
                        is_admin=_is_simc_admin(request.user),
                    )
                except (ValueError, TaskCreationError) as e:
                    return JsonResponse({'success': False, 'error': str(e)})

                return JsonResponse({
                    'success': True,
                    'message': 'SimC配置创建成功，模拟任务已创建',
                    'data': {'id': task.profile_id, 'name': task.profile.name},
                    'task_data': {
                        'id': task.id,
                        'name': task.name,
                        'current_status': task.current_status,
                        'mode': task.mode,
                    },
                })
            
            if copy_from_id:
                try:
                    # 可见的配置均可复制；副本归当前用户且不会继承 system_key。
                    source_profiles = SimcProfile.objects.all()
                    if not _is_simc_admin(request.user):
                        source_profiles = source_profiles.filter(
                            models.Q(user_id=request.user.id)
                            | models.Q(
                                user_id__isnull=True,
                                source=SimcProfile.SOURCE_SIMC_UPSTREAM,
                                is_active=True,
                            )
                        )
                    source_profile = source_profiles.get(id=copy_from_id)
                    if self._profile_mode(source_profile) == 'attribute_only':
                        validate_player_baseline(source_profile.player_equipment)
                    
                    # 复制完整执行语义；仅数据库身份、所有者、名称和 system_key 不继承。
                    profile = SimcProfile.objects.create(
                        user_id=request.user.id,
                        name=name,
                        source=source_profile.source,
                        class_name=source_profile.class_name,
                        version=source_profile.version,
                        use_ptr=source_profile.use_ptr,
                        sync_version=source_profile.sync_version,
                        spec=source_profile.spec,
                        player_config_mode=source_profile.player_config_mode,
                        battlenet_region=getattr(source_profile, 'battlenet_region', '') or '',
                        battlenet_realm=getattr(source_profile, 'battlenet_realm', '') or '',
                        battlenet_character=getattr(source_profile, 'battlenet_character', '') or '',
                        player_equipment=getattr(source_profile, 'player_equipment', '') or '',
                        talent=source_profile.talent,
                        gear_strength=source_profile.gear_strength,
                        gear_crit=source_profile.gear_crit,
                        gear_haste=source_profile.gear_haste,
                        gear_mastery=source_profile.gear_mastery,
                        gear_versatility=source_profile.gear_versatility,
                        is_active=source_profile.is_active,
                    )
                    
                    response_data = {
                        'success': True,
                        'message': 'SimC配置复制成功',
                        'data': {
                            'id': profile.id,
                            'name': profile.name
                        }
                    }
                    
                    # 如果需要立即模拟，创建SimcTask
                    if simulate_now:
                        regular_time = data.get('regular_time')
                        regular_target_count = data.get('regular_target_count')
                        base_template_id = data.get('base_template_id')
                        selected_apl_id = data.get('selected_apl_id')
                        task_result = self._create_simulation_task(
                            request.user.id,
                            profile,
                            regular_time=regular_time,
                            regular_target_count=regular_target_count,
                            base_template_id=base_template_id,
                            selected_apl_id=selected_apl_id,
                            is_admin=_is_simc_admin(request.user),
                        )
                        if task_result['success']:
                            response_data['message'] += '，模拟任务已创建'
                            response_data['task_data'] = task_result['data']
                        else:
                            response_data['message'] += '，但模拟任务创建失败: ' + task_result['error']
                    
                    return JsonResponse(response_data)
                    
                except SimcProfile.DoesNotExist:
                    return JsonResponse({
                        'success': False,
                        'error': '要复制的配置不存在'
                    })
            else:
                # 创建新配置：与更新操作使用同一模式校验，避免保存不可运行的预设。
                try:
                    values = self._validate_profile_payload(data)
                    # Battle.net preflight returns observed character stats as display
                    # metadata. They are part of the frozen equipment snapshot, not
                    # user-authored overrides. Only request fields may populate gear_*.
                    numeric_values = self._profile_numeric_values(data, mode=values['mode'])
                except ValueError as e:
                    return JsonResponse({'success': False, 'error': str(e)})

                profile = SimcProfile.objects.create(
                    user_id=request.user.id,
                    name=name,
                    spec=values['spec'],
                    class_name=values['class_name'],
                    use_ptr=values['use_ptr'],
                    player_config_mode=values['mode'],
                    battlenet_region=values['battlenet_region'],
                    battlenet_realm=values['battlenet_realm'],
                    battlenet_character=values['battlenet_character'],
                    player_equipment=values['player_equipment'],
                    talent=values['talent'],
                    gear_strength=numeric_values['gear_strength'],
                    gear_crit=numeric_values['gear_crit'],
                    gear_haste=numeric_values['gear_haste'],
                    gear_mastery=numeric_values['gear_mastery'],
                    gear_versatility=numeric_values['gear_versatility'],
                    is_active=data.get('is_active', True)
                )
                
                response_data = {
                    'success': True,
                    'message': 'SimC配置创建成功',
                    'data': {
                        'id': profile.id,
                        'name': profile.name
                    }
                }
                
                # 如果需要立即模拟，创建SimcTask
                if simulate_now:
                    regular_time = data.get('regular_time')
                    regular_target_count = data.get('regular_target_count')
                    base_template_id = data.get('base_template_id')
                    selected_apl_id = data.get('selected_apl_id')
                    task_result = self._create_simulation_task(
                        request.user.id,
                        profile,
                        regular_time=regular_time,
                        regular_target_count=regular_target_count,
                        base_template_id=base_template_id,
                        selected_apl_id=selected_apl_id,
                        is_admin=_is_simc_admin(request.user),
                    )
                    if task_result['success']:
                        response_data['message'] += '，模拟任务已创建'
                        response_data['task_data'] = task_result['data']
                    else:
                        response_data['message'] += '，但模拟任务创建失败: ' + task_result['error']
                
                return JsonResponse(response_data)
            
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': '无效的JSON数据'
            })
        except ValueError as e:
            return JsonResponse({
                'success': False,
                'error': str(e)
            })
        except Exception as e:
            logger.error(f"创建SimC配置失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'创建SimC配置失败: {str(e)}'
            })
    
    def _create_simulation_task(self, user_id, profile, regular_time=None, regular_target_count=None,
                                base_template_id=None, selected_apl_id=None, is_admin=False):
        """创建模拟任务的辅助方法"""
        from botend.services.simc_task_service import create_task_from_request, TaskCreationError

        try:
            # 普通立即模拟要求显式提供 template 和 APL。
            if not base_template_id or not selected_apl_id:
                raise ValueError(
                    'simulate_now 必须提供显式的 base_template_id 和 selected_apl_id。'
                    '当前版本不支持自动选择默认模板/APL。'
                )

            # ========== 使用引用型服务创建任务 ==========

            # 构建 profile_fields（使用现有 profile 的 ID）
            profile_fields = {
                'simc_profile_id': profile.id,
                'spec': profile.spec,
                'use_ptr': bool(profile.use_ptr),
                'player_config_mode': profile.player_config_mode,
                'battlenet_region': profile.battlenet_region or '',
                'battlenet_realm': profile.battlenet_realm or '',
                'battlenet_character': profile.battlenet_character or '',
                'player_equipment': profile.player_equipment or '',
                'talent': profile.talent or '',
                'gear_strength': profile.gear_strength,
                'gear_crit': profile.gear_crit,
                'gear_haste': profile.gear_haste,
                'gear_mastery': profile.gear_mastery,
                'gear_versatility': profile.gear_versatility,
            }

            # 构建 simulation_params
            simulation_params = {}
            if regular_time is not None:
                simulation_params['max_time'] = int(regular_time)
            if regular_target_count is not None:
                simulation_params['desired_targets'] = int(regular_target_count)

            # 生成任务名称
            task_name = f"{profile.name}_常规模拟"

            task = create_task_from_request(
                user_id=user_id,
                profile_fields=profile_fields,
                base_template_id=base_template_id,
                selected_apl_id=selected_apl_id,
                simulation_params=simulation_params if simulation_params else None,
                name=task_name,
                is_admin=is_admin,
            )

            return {
                'success': True,
                'data': {
                    'id': task.id,
                    'name': task.name,
                    'current_status': task.current_status,
                    'mode': task.mode,
                }
            }

        except TaskCreationError as e:
            return {'success': False, 'error': str(e)}
        except ValueError as e:
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logger.error(f"创建模拟任务失败: {str(e)}\n{traceback.format_exc()}")
            return {'success': False, 'error': f'创建模拟任务失败: {str(e)}'}
    
    def put(self, request):
        """更新SimC配置"""
        try:
            data = json.loads(request.body)
            if 'use_ptr' in data and type(data['use_ptr']) is not bool:
                return JsonResponse({'success': False, 'error': 'use_ptr 必须是布尔值'}, status=400)
            profile_id = data.get('id')
            
            if not profile_id:
                return JsonResponse({
                    'success': False,
                    'error': '配置ID不能为空'
                })
            
            # 获取配置记录
            profile_query = models.Q(id=profile_id)
            if not _is_simc_admin(request.user):
                profile_query &= models.Q(user_id=request.user.id, is_active=True)
            profile = SimcProfile.objects.get(profile_query)

            # Detail view uses a narrow equipment editor: only item IDs and item
            # levels are client supplied; the rest of the exported player block
            # remains authoritative and is preserved byte-for-byte where possible.
            if 'equipment' in data:
                equipment = data.get('equipment')
                if not isinstance(equipment, list):
                    return JsonResponse({'success': False, 'error': '装备列表格式无效'}, status=400)
                updates = {}
                for row in equipment:
                    if not isinstance(row, dict):
                        return JsonResponse({'success': False, 'error': '装备项格式无效'}, status=400)
                    slot = str(row.get('slot') or '').strip().lower()
                    if not re.match(r'^[a-z_]+$', slot):
                        return JsonResponse({'success': False, 'error': '装备槽位无效'}, status=400)
                    try:
                        item_id = int(row.get('item_id'))
                        item_level = int(row.get('item_level'))
                    except (TypeError, ValueError):
                        return JsonResponse({'success': False, 'error': '装备 ID 和装等必须是整数'}, status=400)
                    if item_id <= 0 or item_level <= 0:
                        return JsonResponse({'success': False, 'error': '装备 ID 和装等必须大于 0'}, status=400)
                    updates[slot] = (item_id, item_level)
                lines = (profile.player_equipment or '').splitlines()
                seen = set()
                output = []
                for line in lines:
                    match = re.match(r'^(\s*)([a-z_]+)(\s*=)(.*)$', line, re.IGNORECASE)
                    slot = match.group(2).lower() if match else ''
                    if match and slot in updates:
                        item_id, item_level = updates[slot]
                        value = match.group(4)
                        if re.search(r'(^|,)\s*id\s*=', value, re.IGNORECASE):
                            value = re.sub(r'(^|,)\s*id\s*=\s*[^,]*', rf'\1id={item_id}', value, count=1, flags=re.IGNORECASE)
                        else:
                            value += f',id={item_id}'
                        if re.search(r'(^|,)\s*ilevel\s*=', value, re.IGNORECASE):
                            value = re.sub(r'(^|,)\s*ilevel\s*=\s*[^,]*', rf'\1ilevel={item_level}', value, count=1, flags=re.IGNORECASE)
                        else:
                            value += f',ilevel={item_level}'
                        line = f'{match.group(1)}{match.group(2)}{match.group(3)}{value}'
                        seen.add(slot)
                    output.append(line)
                missing = [slot for slot in updates if slot not in seen]
                if missing:
                    output.extend(f'{slot}=,id={updates[slot][0]},ilevel={updates[slot][1]}' for slot in missing)
                profile.player_equipment = '\n'.join(output)
                update_fields = ['player_equipment']
                if hasattr(profile, 'update_time'):
                    update_fields.append('update_time')
                profile.save(update_fields=update_fields)
                return JsonResponse({'success': True, 'message': '装备配置更新成功'})
            
            # 验证名称；partial update 未提交名称时保留现值。
            name = str(data.get('name', profile.name) or '').strip()
            if not name:
                return JsonResponse({
                    'success': False,
                    'error': '配置名称不能为空'
                })
            
            # 检查名称是否重复（排除当前记录）
            if SimcProfile.objects.filter(
                user_id=profile.user_id,
                name=name,
                is_active=True
            ).exclude(id=profile_id).exists():
                return JsonResponse({
                    'success': False,
                    'error': '配置名称已存在'
                })
            
            # 更新配置：与创建使用同一套模式校验，并允许 partial update 保留未提交字段。
            try:
                values = self._validate_profile_payload(data, {
                    'mode': self._profile_mode(profile),
                    'spec': profile.spec,
                    'use_ptr': bool(profile.use_ptr),
                    'player_config_mode': self._profile_mode(profile),
                    'battlenet_region': profile.battlenet_region,
                    'battlenet_realm': profile.battlenet_realm,
                    'battlenet_character': profile.battlenet_character,
                    'player_equipment': profile.player_equipment,
                    'talent': profile.talent,
                })
                numeric_values = self._profile_numeric_values(data, {
                    field: getattr(profile, field, None)
                    for field in ('gear_strength', 'gear_crit', 'gear_haste', 'gear_mastery', 'gear_versatility')
                }, mode=values['mode'])
            except ValueError as e:
                return JsonResponse({'success': False, 'error': str(e)})
            profile.name = name
            profile.spec = values['spec']
            profile.class_name = values['class_name']
            profile.use_ptr = values['use_ptr']
            profile.player_config_mode = values['mode']
            profile.battlenet_region = values['battlenet_region']
            profile.battlenet_realm = values['battlenet_realm']
            profile.battlenet_character = values['battlenet_character']
            profile.player_equipment = values['player_equipment']
            profile.talent = values['talent']
            profile.gear_strength = numeric_values['gear_strength']
            profile.gear_crit = numeric_values['gear_crit']
            profile.gear_haste = numeric_values['gear_haste']
            profile.gear_mastery = numeric_values['gear_mastery']
            profile.gear_versatility = numeric_values['gear_versatility']
            profile.is_active = True
            profile.save()
            
            return JsonResponse({
                'success': True,
                'message': 'SimC配置更新成功'
            })
            
        except SimcProfile.DoesNotExist:
            return JsonResponse({
                'success': False,
                'error': 'SimC配置不存在'
            })
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': '无效的JSON数据'
            })
        except Exception as e:
            logger.error(f"更新SimC配置失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '更新SimC配置失败'
            })
    
    def delete(self, request):
        """删除SimC配置"""
        try:
            data = json.loads(request.body)
            profile_id = data.get('id')
            
            if not profile_id:
                return JsonResponse({
                    'success': False,
                    'error': '配置ID不能为空'
                })
            
            # 真实删除配置；历史任务使用冻结版本，不依赖此配置记录。
            profile = SimcProfile.objects.filter(id=profile_id)
            if not _is_simc_admin(request.user):
                profile = profile.filter(user_id=request.user.id)
            profile = profile.get()
            profile.delete()
            
            return JsonResponse({
                'success': True,
                'message': 'SimC配置删除成功'
            })
            
        except SimcProfile.DoesNotExist:
            return JsonResponse({
                'success': False,
                'error': 'SimC配置不存在'
            })
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': '无效的JSON数据'
            })
        except Exception as e:
            logger.error(f"删除SimC配置失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '删除SimC配置失败'
            })
    
    def patch(self, request, profile_id=None):
        """一键模拟SimC配置"""
        try:
            data = json.loads(request.body or '{}')
            if 'task_type' in data:
                return JsonResponse({'success': False, 'error': '不再支持 task_type 参数；请使用 SimC 工作台的任务模式入口。'}, status=400)
            # 从URL参数获取profile_id
            if not profile_id:
                return JsonResponse({
                    'success': False,
                    'error': '配置ID不能为空'
                })
            
            # 获取配置并检查权限
            try:
                profile = SimcProfile.objects.get(id=profile_id, is_active=True)
            except SimcProfile.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': 'SimC配置不存在或无权限访问'
                })
            
            # 创建模拟任务
            task_result = self._create_simulation_task(
                request.user.id, profile, is_admin=_is_simc_admin(request.user),
            )
            
            if task_result['success']:
                return JsonResponse({
                    'success': True,
                    'message': '模拟任务创建成功，正在执行模拟',
                    'data': task_result['data']
                })
            else:
                return JsonResponse({
                    'success': False,
                    'error': f'创建模拟任务失败: {task_result["error"]}'
                })
            
        except Exception as e:
            logger.error(f"一键模拟失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '一键模拟失败'
            })


@method_decorator(login_required, name='dispatch')
class SimcAplCandidatesAPIView(View):
    """
    APL候选方案：GET获取指定专精的APL列表，POST基于GLM生成对比任务
    """

    def get(self, request):
        """获取指定专精的APL候选列表"""
        try:
            raw_spec = (request.GET.get('spec') or '').strip().lower()
            raw_class = (request.GET.get('class_name') or request.GET.get('class') or '').strip().lower()
            if not raw_spec:
                return JsonResponse({'success': False, 'error': 'spec参数不能为空'})

            spec_token = _normalize_simc_token(raw_spec)
            class_token = WOW_SIMC_CLASS_ALIASES.get(_normalize_simc_token(raw_class), _normalize_simc_token(raw_class))
            spec_key = spec_token
            if class_token and '_' not in spec_token:
                spec_key = f'{class_token}_{spec_token}'

            data = _list_selectable_apl_for_spec(
                spec_key=spec_key,
                class_name=class_token,
                spec=spec_token,
                owner_user_id=request.user.id,
            )
            default_apl, default_template = _resolve_home_creation_defaults(
                spec_key, class_name=class_token, owner_user_id=request.user.id,
            )
            for row in data:
                row['is_default'] = row['id'] == default_apl.id
                row['spec_label'] = _simc_spec_label(row.get('spec'), row.get('class_name'))
            return JsonResponse({
                'success': True,
                'data': data,
                'default_apl_id': default_apl.id,
                'default_template_id': default_template.id,
            })
        except ValueError as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=409)
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})

    def post(self, request):
        try:
            data = json.loads(request.body or '{}')
            if 'task_type' in data:
                return JsonResponse({'success': False, 'error': '不再支持 task_type 参数；请使用 SimC 工作台的任务模式入口。'}, status=400)
            profile_id = data.get('profile_id')
            include_base = bool(data.get('include_base', True))
            candidate_count = int(data.get('candidate_count', 5) or 5)
            candidate_count = max(1, min(candidate_count, 5))
            base_template_id = data.get('base_template_id')
            selected_apl_id = data.get('selected_apl_id')

            if not profile_id:
                return JsonResponse({'success': False, 'error': 'profile_id不能为空'})
            if not base_template_id or not selected_apl_id:
                return JsonResponse({'success': False, 'error': 'base_template_id 和 selected_apl_id 不能为空'})

            profile = SimcProfile.objects.filter(
                id=profile_id,
                user_id=request.user.id,
                is_active=True
            ).first()
            if not profile:
                return JsonResponse({'success': False, 'error': 'SimC配置不存在或无权限访问'})

            apl_template = SimcApl.objects.filter(
                id=selected_apl_id, is_active=True, is_selectable=True,
            ).filter(
                models.Q(owner_user_id=request.user.id)
                | models.Q(owner_user_id__isnull=True, is_system=True)
            ).first()
            base_apl = str(apl_template.content if apl_template else '').strip()
            if not base_apl:
                return JsonResponse({'success': False, 'error': '当前配置缺少基础APL，无法生成候选方案'})

            task, created = self._create_compare_preprocessing_task(
                user_id=request.user.id,
                profile=profile,
                include_base=include_base,
                candidate_count=candidate_count,
                template_id=base_template_id,
                apl_id=apl_template.id,
                backend_id=data.get('backend_id'),
            )
            run_ids = []
            return JsonResponse({
                'success': True,
                'message': f'已创建包含 {len(created)} 个冻结候选的对比任务',
                'data': {
                    'profile_id': profile.id,
                    'profile_name': profile.name,
                    'candidate_count': candidate_count,
                    'include_base': include_base,
                    'simulation_started': False,
                    'preprocessing_started': False,
                    'task_id': task.id,
                    'mode': task.mode,
                    'run_ids': run_ids,
                    'runs': created
                }
            })
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': '无效的JSON数据'})
        except Exception as e:
            logger.error(f"生成APL候选方案失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({'success': False, 'error': f'生成候选方案失败: {str(e)}'})

    def _generate_glm_candidates(self, profile, base_apl, total_count):
        glm = GLMClient()
        generated = []
        total_batches = int(total_count)
        for idx in range(total_batches):
            batch_size = 1
            chunk = self._request_candidate_batch_with_fallback(
                glm=glm,
                profile=profile,
                base_apl=base_apl,
                batch_size=batch_size,
                batch_index=idx + 1,
                total_batches=total_batches,
                base_limits=[7000, 3600, 1800]
            )
            if len(chunk) < 1:
                raise Exception(f'第{idx + 1}个候选方案生成失败')
            generated.append(chunk[0])
        return generated[:total_count]

    def _request_candidate_batch_with_fallback(self, glm, profile, base_apl, batch_size, batch_index, total_batches, base_limits=None):
        limits = [int(x) for x in (base_limits or [7000, 3600, 1800]) if int(x) > 0]
        last_error = ''
        best_chunk = []
        for limit in limits:
            try:
                chunk = self._request_candidate_batch(
                    glm=glm,
                    profile=profile,
                    base_apl=base_apl,
                    batch_size=batch_size,
                    batch_index=batch_index,
                    total_batches=total_batches,
                    base_limit=limit
                )
                if len(chunk) >= batch_size:
                    return chunk
                if len(chunk) > len(best_chunk):
                    best_chunk = chunk
            except Exception as e:
                last_error = str(e)
                logger.warning(f"APL候选批次重试: batch={batch_index}, limit={limit}, error={last_error}")
                continue
        if best_chunk:
            return best_chunk
        if last_error:
            raise Exception(f'GLM候选生成失败（批次{batch_index}）: {last_error}')
        return []

    def _request_candidate_batch(self, glm, profile, base_apl, batch_size, batch_index, total_batches, base_limit=7000):
        base_text = str(base_apl or '').strip()
        if len(base_text) > base_limit:
            base_text = base_text[:base_limit] + "\n# ... 省略过长内容 ..."
        prompt = (
            "你是SimulationCraft APL优化专家。请基于给定基础APL，生成不同思路的候选APL。\n"
            "要求:\n"
            "1) 必须输出严格JSON数组，不要Markdown、不要解释文字。\n"
            "2) 数组长度必须等于请求数量。\n"
            "3) 每个元素结构: {\"name\":\"方案名\",\"reason\":\"一句话思路\",\"apl_list\":\"完整APL列表\"}\n"
            "4) apl_list必须符合APL语法，行格式仅允许注释行(#...)或 actions 开头行（如 actions+=/... 或 actions.xxx+=/...）。\n"
            "5) 强制约束：你只能调整基础APL中各行的先后顺序，绝对禁止新增、删除、改写任何一行文本。\n"
            "6) 与基础方案保持同职业同专精，不要改角色基础属性、天赋字段。\n\n"
            f"批次: {batch_index}/{total_batches}\n"
            f"本批数量: {batch_size}\n"
            "注意：本次只生成1个候选方案，不要返回多个。\n"
            f"配置专精: {profile.spec}\\n"
            f"天赋: {profile.talent}\\n\\n"
            "基础APL如下:\n"
            f"{base_text}\n"
        )
        raw = glm.send_message(prompt, max_tokens=8192, thinking_type='disabled')
        if (not raw) and ('finish_reason=length' in str(getattr(glm, 'last_error', '') or '')):
            raw = glm.send_message(prompt, max_tokens=12288, thinking_type='disabled')
        if not raw:
            reasoning = str(getattr(glm, 'last_reasoning', '') or '').strip()
            if reasoning:
                reasoning = reasoning[:3000]
                raise Exception(f"GLM未返回内容: {glm.last_error or 'empty response'} | reasoning_preview={reasoning}")
            raise Exception(f"GLM未返回内容: {glm.last_error or 'empty response'}")
        rows = self._extract_json_array(raw)
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            apl_list = self._normalize_apl_text(row.get('apl_list', ''))
            if not apl_list:
                continue
            if not self._is_valid_apl_format(apl_list):
                continue
            if not self._is_reorder_only(base_apl, apl_list):
                continue
            result.append({
                'name': str(row.get('name') or '').strip() or f'候选方案{len(result) + 1}',
                'reason': str(row.get('reason') or '').strip(),
                'apl_list': apl_list
            })
        return result

    def _extract_json_array(self, raw_text):
        text = str(raw_text or '').strip()
        if not text:
            return []
        if text.startswith('```'):
            text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        m = re.search(r'\[[\s\S]*\]', text)
        if not m:
            return []
        try:
            parsed = json.loads(m.group(0))
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

    def _normalize_apl_text(self, apl_text):
        text = str(apl_text or '').replace('\r', '')
        if text.startswith('```'):
            text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        lines = [ln.rstrip() for ln in text.split('\n')]
        cleaned = [ln for ln in lines if ln.strip()]
        return '\n'.join(cleaned).strip()

    def _is_valid_apl_format(self, apl_text):
        text = str(apl_text or '').strip()
        if not text:
            return False
        lines = text.replace('\r', '').split('\n')
        valid_count = 0
        for raw in lines:
            line = str(raw or '').strip()
            if not line:
                continue
            if line.startswith('#'):
                continue
            if line.startswith('actions'):
                valid_count += 1
                continue
            return False
        return valid_count > 0

    def _canonical_apl_lines(self, apl_text):
        lines = []
        for raw in str(apl_text or '').replace('\r', '').split('\n'):
            line = str(raw or '').strip()
            if not line:
                continue
            lines.append(line)
        return lines

    def _is_reorder_only(self, base_apl, candidate_apl):
        from collections import Counter
        base_lines = self._canonical_apl_lines(base_apl)
        candidate_lines = self._canonical_apl_lines(candidate_apl)
        if not base_lines or not candidate_lines:
            return False
        # 只允许调整顺序：逐行内容的多重集合必须完全一致
        if Counter(base_lines) != Counter(candidate_lines):
            return False
        # 至少存在顺序变化，避免返回与基础完全相同
        if base_lines == candidate_lines:
            return False
        return True

    def _create_compare_preprocessing_task(
            self, user_id, profile, include_base, candidate_count,
            template_id, apl_id, backend_id=None):
        total_count = int(candidate_count) + (1 if include_base else 0)
        if total_count <= 0:
            raise Exception('候选数量无效')

        apl = SimcApl.objects.filter(id=apl_id, is_active=True).first()
        base_apl = str(apl.content if apl else '').strip()
        if not base_apl:
            raise Exception('当前配置缺少基础APL')

        plans = []
        if include_base:
            plans.append({
                'name': '基础方案',
                'apl_list': base_apl,
                'reason': '当前配置中的原始APL',
            })
        plans.extend(self._generate_glm_candidates(profile, base_apl, int(candidate_count)))
        if len(plans) != total_count:
            raise Exception(f'候选方案数量不匹配（预期{total_count}，实际{len(plans)}）')

        candidates = []
        created = []
        for idx, plan in enumerate(plans):
            is_base = bool(include_base and idx == 0)
            plan_name = str(plan.get('name') or '').strip() or (
                '基础方案' if is_base else f'候选方案{idx}'
            )
            plan_reason = str(plan.get('reason') or '').strip()
            apl_list = str(plan.get('apl_list') or '').strip()
            if not apl_list:
                raise Exception(f'{plan_name} 的 APL 为空')
            if (not self._is_valid_apl_format(apl_list)
                    or (not is_base and not self._is_reorder_only(base_apl, apl_list))):
                raise Exception(f'{plan_name} 未通过 APL 重排约束校验')
            candidates.append({
                'candidate_key': f'apl-candidate-{idx}',
                'candidate_label': plan_name,
                'candidate_params': {
                    'candidate_type': 'apl_override',
                    'apl_override': apl_list,
                    'is_base': is_base,
                    'search': {
                        'candidate_index': idx,
                        'preprocess_stage': 'ready',
                        'candidate_reason': plan_reason,
                    },
                },
            })
            created.append({
                'task_id': None,
                'run_id': None,
                'candidate_name': plan_name,
                'candidate_reason': plan_reason,
                'is_base': is_base,
                'preprocess_stage': 'ready',
                'status': 'pending',
            })
        task = create_task(
            user_id=user_id,
            name=f'{profile.name} APL候选对比',
            profile_id=profile.id,
            template_id=template_id,
            apl_id=apl_id,
            mode='comparison',
            simulation_params={'fight_style': 'Patchwerk', 'max_time': 300, 'desired_targets': 1},
            mode_params={'candidate_type': 'apl_override'},
            candidates=candidates,
            backend_id=backend_id,
        )
        for item in created:
            item['task_id'] = task.id
        return task, created


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


@method_decorator([csrf_exempt, login_required], name='dispatch')
class SimcTaskPreviewAPIView(View):
    """Return a user-authorized, structured snapshot of a task manifest only."""

    def get(self, request):
        task_id = request.GET.get('task_id')
        try:
            task = SimcTask.objects.get(id=task_id, user_id=request.user.id, is_active=True)
        except (SimcTask.DoesNotExist, TypeError, ValueError):
            return JsonResponse({'success': False, 'error': '任务不存在或无权限访问'})
        manifest = SimcTaskAPIView()._normalize_task_ext(task.task_type, task.ext)
        if task.profile_id and task.template_id and task.apl_id and task.profile_version_id and task.template_version_id and task.apl_version_id:
            apl_payload = task.apl_version.payload if isinstance(task.apl_version.payload, dict) else {}
            profile_payload = task.profile_version.payload if isinstance(task.profile_version.payload, dict) else {}
            params = task.simulation_params if isinstance(task.simulation_params, dict) else {}
            return JsonResponse({'success': True, 'data': {
                'id': task.id, 'name': task.name,
                'mode': task.mode, 'status': task.current_status,
                'profile_id': task.profile_id, 'template_id': task.template_id, 'apl_id': task.apl_id,
                'profile_version_id': task.profile_version_id,
                'template_version_id': task.template_version_id,
                'apl_version_id': task.apl_version_id,
                'apl_name': apl_payload.get('name') or task.apl.name or params.get('override_action_list_name') or '',
                'profile_name': profile_payload.get('name') or task.profile.name or '',
                'simulation_params': params,
                'mode_params': task.mode_params or {},
                'candidate_label': task.candidate_label,
            }})
        profile = None
        if not manifest and task.simc_profile_id:
            profile = SimcProfile.objects.filter(id=task.simc_profile_id, user_id=request.user.id).first()
        context = {
            'id': task.id,
            'name': task.name,
            'mode': task.mode,
            'status': task.current_status,
            'result_file': SimcTaskAPIView()._task_result_file_summary(task),
            'spec': manifest.get('spec') or (profile.spec if profile else ''),
            'fight_style': manifest.get('fight_style') or '',
            'time': manifest.get('time') or manifest.get('regular_time') or '',
            'target_count': manifest.get('target_count') or manifest.get('regular_target_count') or '',
            'player_config_mode': manifest.get('player_config_mode') or '',
            'talent': manifest.get('talent') or '',
            'gear': {
                # Preserve a valid explicit zero from the task snapshot.
                'strength': manifest.get('gear_strength', 0),
                'crit': manifest.get('gear_crit', 0),
                'haste': manifest.get('gear_haste', 0),
                'mastery': manifest.get('gear_mastery', 0),
                'versatility': manifest.get('gear_versatility', 0),
            },
            'selected_attributes': manifest.get('selected_attributes') or '',
            'attribute_step': manifest.get('attribute_step') or '',
            'selected_apl_id': manifest.get('selected_apl_id'),
            'final_config_validation': manifest.get('final_config_validation') or {},
        }
        return JsonResponse({'success': True, 'data': context})


@method_decorator([csrf_exempt, login_required], name='dispatch')
class SimcResultProxyAPIView(View):
    """
    SimC结果文件代理API - 用于从OSS获取文件内容
    """
    
    def get(self, request):
        """代理获取OSS文件内容"""
        try:
            import requests
            import os
            from django.conf import settings
            
            result_file = request.GET.get('file')
            if not result_file:
                return JsonResponse({
                    'success': False,
                    'error': '文件名不能为空'
                })
            
            # 只允许当前用户自己任务中精确登记的结果文件，禁止借代理读取任意 OSS/local 文件。
            requested_files = [part.strip() for part in str(result_file).split(',') if part.strip()]
            if len(requested_files) != 1 or requested_files[0] != result_file.strip() or '/' in result_file or '\\' in result_file:
                return JsonResponse({'success': False, 'error': '结果文件名无效'})
            legacy_tasks = SimcTask.objects.filter(
                user_id=request.user.id,
                is_active=True,
            ).exclude(
                profile_id__isnull=False,
                template_id__isnull=False,
                apl_id__isnull=False,
                profile_version_id__isnull=False,
                template_version_id__isnull=False,
                apl_version_id__isnull=False,
            )
            if not legacy_tasks.filter(
                models.Q(result_file=result_file) | models.Q(result_file__startswith=result_file + ',') |
                models.Q(result_file__endswith=',' + result_file) | models.Q(result_file__contains=',' + result_file + ',')
            ).exists():
                return JsonResponse({'success': False, 'error': '结果文件不存在或无权限访问'})

            # 首先尝试从OSS获取文件
            oss_config = getattr(settings, 'OSS_CONFIG', {})
            base_url = oss_config.get('base_url', '')
            
            if base_url:
                try:
                    # 构建完整的OSS文件URL
                    file_url = base_url + result_file
                    
                    # 从OSS获取文件内容
                    response = requests.get(file_url, timeout=30)
                    
                    if response.status_code == 200:
                        return JsonResponse({
                            'success': True,
                            'content': response.text
                        })
                    else:
                        logger.warning(f"OSS文件获取失败，状态码: {response.status_code}，尝试本地文件")
                        
                except requests.RequestException as e:
                    logger.warning(f"OSS请求失败: {str(e)}，尝试本地文件")
            
            # OSS获取失败，尝试从本地static目录获取
            local_file_path = os.path.join(settings.BASE_DIR, 'static', 'simc_results', result_file)
            
            if os.path.exists(local_file_path):
                try:
                    with open(local_file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    return JsonResponse({
                        'success': True,
                        'content': content
                    })
                    
                except Exception as e:
                    logger.error(f"读取本地文件失败: {str(e)}")
                    return JsonResponse({
                        'success': False,
                        'error': f'读取本地文件失败: {str(e)}'
                    })
            else:
                return JsonResponse({
                    'success': False,
                    'error': f'文件未找到: {result_file}'
                })
            
        except Exception as e:
            logger.error(f"SimC结果代理错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'获取文件失败: {str(e)}'
            })


@method_decorator([csrf_exempt, login_required], name='dispatch')
class SimcAttributeAnalysisAPIView(View):
    """
    属性模拟分析API - 解析所有结果文件并提取DPS数据
    """
    
    def get(self, request):
        """获取属性模拟任务的分析数据"""
        try:
            import requests
            import re
            from bs4 import BeautifulSoup
            from django.conf import settings
            
            task_id = request.GET.get('task_id')
            if not task_id:
                return JsonResponse({
                    'success': False,
                    'error': '任务ID不能为空'
                })
            
            # 仅允许当前用户读取自己的任务分析，避免以 task_id 枚举他人 DPS/装备结果。
            try:
                task = SimcTask.objects.get(id=task_id, user_id=request.user.id, is_active=True)
            except SimcTask.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': '任务不存在或无权限访问'
                })
            
            is_attribute_sweep = task.mode == 'attribute_sweep'
            has_complete_references = all((
                task.profile_id, task.template_id, task.apl_id,
                task.profile_version_id, task.template_version_id, task.apl_version_id,
            ))
            is_legacy_attribute = task.task_type == 2 and not has_complete_references
            if not is_legacy_attribute and not is_attribute_sweep:
                return JsonResponse({
                    'success': False,
                    'error': '该任务不是属性模拟或四属性寻优任务'
                })
            if not task.result_file and is_legacy_attribute:
                return JsonResponse({
                    'success': False,
                    'error': '任务尚未完成或无结果文件'
                })

            # 旧式且结构不完整的属性任务由一个任务持有多个受控属性报告；
            # 新式四属性寻优只聚合同一请求级任务下的候选 Runs。
            result_files = task.result_file.split(',') if is_legacy_attribute else []
            analysis_data = []
            
            # OSS配置
            oss_config = getattr(settings, 'OSS_CONFIG', {})
            base_url = oss_config.get('base_url', '')
            
            for result_file in result_files:
                result_file = result_file.strip()
                if not result_file:
                    continue
                
                try:
                    # 只接受 Worker 受控生成的属性结果文件，并确保它属于当前任务。
                    parsed = parse_attribute_result_filename(result_file)
                    if not parsed or parsed['task_id'] != task.id:
                        logger.warning(f"无法解析或无权读取属性结果文件: {result_file}")
                        continue
                    attr1_name = parsed['attr1_name']
                    attr1_value = parsed['attr1_value']
                    attr2_name = parsed['attr2_name']
                    attr2_value = parsed['attr2_value']
                    
                    # 获取文件内容
                    file_content = None
                    
                    # 首先尝试从OSS获取
                    if base_url:
                        try:
                            file_url = base_url + result_file
                            response = requests.get(file_url, timeout=30)
                            if response.status_code == 200:
                                file_content = response.text
                        except requests.RequestException as e:
                            logger.warning(f"OSS获取失败: {str(e)}，尝试本地文件")
                    
                    # OSS失败，尝试本地文件
                    if not file_content:
                        import os
                        local_file_path = os.path.join(settings.BASE_DIR, 'static', 'simc_results', result_file)
                        if os.path.exists(local_file_path):
                            with open(local_file_path, 'r', encoding='utf-8') as f:
                                file_content = f.read()
                    
                    if not file_content:
                        logger.warning(f"无法获取文件内容: {result_file}")
                        continue
                    
                    # 解析DPS数据
                    dps_value = self.extract_dps_from_html(file_content)
                    
                    if dps_value is not None:
                        analysis_data.append({
                            'file_name': result_file,
                            'attr1_name': attr1_name,
                            'attr1_value': attr1_value,
                            'attr2_name': attr2_name,
                            'attr2_value': attr2_value,
                            'dps': dps_value
                        })
                    
                except Exception as e:
                    logger.error(f"解析文件 {result_file} 失败: {str(e)}")
                    continue
            
            # 按属性1值排序（处理混合类型）
            def sort_key(x):
                value = x['attr1_value']
                if isinstance(value, int):
                    return (0, value)  # 数字优先，按数值排序
                else:
                    return (1, str(value))  # 字符串其次，按字母排序
            
            analysis_data.sort(key=sort_key)
            attribute_report = None
            if is_attribute_sweep:
                runs = task.simulation_runs.order_by('sequence')
                attribute_report = SimcRegularCompareAPIView()._build_reference_attribute_report(
                    runs, task.analysis_result,
                )
            
            return JsonResponse({
                'success': True,
                'data': {
                    'task_name': task.name,
                    'task_id': task.id,
                    'results': analysis_data,
                    'total_count': len(analysis_data),
                    'attribute_report': attribute_report
                }
            })
            
        except Exception as e:
            logger.error(f"属性模拟分析失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'分析失败: {str(e)}'
            })
    
    def extract_dps_from_html(self, html_content):
        """
        从HTML内容中提取DPS值
        """
        try:
            # 使用正则表达式查找DPS值
            # 查找类似 "角色名: 123,456 dps" 的模式
            dps_pattern = r':\s*([\d,]+)\s*dps'
            match = re.search(dps_pattern, html_content, re.IGNORECASE)
            
            if match:
                dps_str = match.group(1).replace(',', '')
                return int(dps_str)
            
            # 备用方法：使用BeautifulSoup解析
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html_content, 'html.parser')
                
                # 查找包含DPS的元素
                player_section = soup.find(class_='player')
                if player_section:
                    h2_tag = player_section.find('h2')
                    if h2_tag:
                        text = h2_tag.get_text()
                        match = re.search(r':\s*([\d,]+)\s*dps', text, re.IGNORECASE)
                        if match:
                            dps_str = match.group(1).replace(',', '')
                            return int(dps_str)
            except ImportError:
                pass  # BeautifulSoup不可用，继续使用正则表达式
            
            return None
            
        except Exception as e:
            logger.error(f"提取DPS失败: {str(e)}")
            return None


@method_decorator([csrf_exempt, login_required], name='dispatch')
class SimcRegularCompareAPIView(View):
    """
    常规模拟对比API - 解析多个任务的结果文件并返回可对比数据
    """

    @staticmethod
    def _safe_attribute_report(attribute_report):
        if not isinstance(attribute_report, dict):
            return None
        safe_candidate_fields = ('id', 'label', 'round', 'is_center', 'ratings', 'dps')

        def safe_candidate(value):
            if not isinstance(value, dict):
                return None
            return {key: value.get(key) for key in safe_candidate_fields if key in value}

        safe = {
            key: attribute_report.get(key)
            for key in (
                'algorithm', 'algorithm_version', 'step', 'tolerance',
                'rounds_completed', 'current_round', 'total_rating',
                'initial_ratings', 'stop_reason', 'local_optimum',
            )
        }
        safe['recommendation'] = safe_candidate(attribute_report.get('recommendation'))
        safe['search_path'] = [{
            key: point.get(key) for key in ('round', 'ratings', 'dps') if key in point
        } for point in attribute_report.get('search_path', []) if isinstance(point, dict)]
        safe['candidates'] = [
            safe_candidate(value) for value in attribute_report.get('candidates', [])
            if isinstance(value, dict)
        ]
        return safe

    def _build_reference_attribute_report(self, runs, analysis_result=None):
        """Build an attribute report from frozen run candidate parameters."""
        run_rows = []
        for run in runs:
            params = run.candidate_params if isinstance(run.candidate_params, dict) else {}
            run_rows.append((run, params))
        report = self._build_attribute_report(run_rows)
        analysis = analysis_result if isinstance(analysis_result, dict) else {}
        persisted = analysis.get('attribute_search')
        if not isinstance(persisted, dict) or not isinstance(persisted.get('converged'), bool):
            return report

        report['converged'] = persisted['converged']
        report['stop_reason'] = str(persisted.get('stop_reason') or '')
        report['local_optimum'] = (
            persisted['converged']
            and report['stop_reason'] == 'local_optimum_50_pairwise'
        )
        persisted_ratings = persisted.get('ratings')
        if isinstance(persisted_ratings, dict):
            recommendation = next(
                (row for row in report['all_candidates'] if row.get('ratings') == persisted_ratings),
                None,
            )
            if recommendation is None:
                recommendation = {
                    'round': persisted.get('round'),
                    'ratings': persisted_ratings,
                    'dps': persisted.get('dps'),
                }
            else:
                recommendation = {**recommendation, 'dps': persisted.get('dps')}
            report['recommendation'] = recommendation
        return report

    def _build_attribute_report(self, run_candidates):
        """Return a truthful report for the measured 50-rating local search only."""
        stats = SimcComparisonTaskAPIView.ATTRIBUTE_STATS
        tolerance = SimcComparisonTaskAPIView.ATTRIBUTE_DPS_TOLERANCE
        candidates = []
        centers = []
        invalid = []
        for run, candidate_params in run_candidates:
            params = candidate_params if isinstance(candidate_params, dict) else {}
            candidate = params.get('search') or {}
            ratings = params.get('attribute_ratings') or {}
            round_number = run.round_number
            result_file = SimcComparisonTaskAPIView._run_result_file(run)
            summary = run.result_summary if isinstance(run.result_summary, dict) else {}
            if params.get('candidate_type') == 'attribute_baseline_probe':
                ratings = summary.get('gear_ratings') or {}
            row = {
                'id': run.id, 'label': run.candidate_label or run.candidate_key,
                'round': round_number, 'is_center': bool(params.get('is_base')),
                'move': candidate.get('move') or {}, 'ratings': ratings,
                'result_file': result_file, 'status': run.status,
                'dps': summary.get('dps'),
            }
            if any(value is None for value in ratings.values()) or any(stat not in ratings for stat in stats):
                invalid.append({'id': run.id, 'error': '候选缺少四项绿字'})
                candidates.append(row)
                continue
            try:
                row['ratings'] = {stat: int(ratings[stat]) for stat in stats}
            except (TypeError, ValueError):
                invalid.append({'id': run.id, 'error': '候选绿字无效'})
                candidates.append(row)
                continue
            if run.status == 'completed' and row['dps'] is None and result_file:
                html_content = self._get_result_file_content(result_file)
                parsed = self._parse_regular_result(html_content) if html_content else {}
                if parsed.get('dps') is None:
                    invalid.append({'id': run.id, 'error': '无法解析该候选的独立 DPS 结果'})
                else:
                    row['dps'] = parsed['dps']
            candidates.append(row)
            if row['is_center']:
                centers.append(row)

        completed = [row for row in candidates if row['dps'] is not None]
        ranked = sorted(completed, key=lambda row: row['dps'], reverse=True)
        current_round = max([row['round'] for row in candidates] or [1])
        current = [row for row in candidates if row['round'] == current_round]
        center = next((row for row in current if row['is_center']), None)
        current_complete = bool(current) and all(row['dps'] is not None for row in current)
        recommendation = ranked[0] if ranked else None
        stop_reason = 'awaiting_current_round'
        if current_complete and center:
            best_neighbor = max((row for row in current if not row['is_center']), key=lambda row: row['dps'], default=None)
            recommendation = best_neighbor if best_neighbor and best_neighbor['dps'] > center['dps'] + tolerance else center
            stop_reason = '' if recommendation is not center else 'local_optimum_50_pairwise'
        path = [
            {'round': row['round'], 'ratings': row['ratings'], 'dps': row['dps'], 'result_file': row['result_file']}
            for row in sorted(centers, key=lambda item: item['round'])
        ]
        first_center = next((row for row in sorted(centers, key=lambda item: item['round']) if row['round'] == 1), None)
        completed_rounds = 0
        for round_number in sorted({row['round'] for row in candidates}):
            round_rows = [row for row in candidates if row['round'] == round_number]
            round_centers = [row for row in round_rows if row['is_center']]
            if len(round_centers) != 1 or any(row['dps'] is None for row in round_rows):
                continue
            try:
                expected = SimcComparisonTaskAPIView._attribute_variants(
                    round_centers[0]['ratings'], SimcComparisonTaskAPIView.ATTRIBUTE_SEARCH_STEP,
                    round_number=round_number, mark_base=True,
                )
                expected_ratings = {
                    tuple(int(ratings[stat]) for stat in stats)
                    for _, ratings, _, _ in expected
                }
                actual_ratings = [
                    tuple(int(row['ratings'][stat]) for stat in stats)
                    for row in round_rows
                ]
            except (KeyError, TypeError, ValueError):
                continue
            if len(actual_ratings) == len(set(actual_ratings)) and set(actual_ratings) == expected_ratings:
                completed_rounds += 1
        return {
            'algorithm': 'four_stat_pairwise_hill_climb', 'algorithm_version': 2,
            'step': SimcComparisonTaskAPIView.ATTRIBUTE_SEARCH_STEP,
            'tolerance': tolerance, 'rounds_completed': completed_rounds,
            'current_round': current_round, 'total_rating': sum(first_center['ratings'].values()) if first_center else None,
            'initial_ratings': first_center['ratings'] if first_center else {},
            'recommendation': recommendation, 'stop_reason': stop_reason,
            'converged': stop_reason not in ('', 'awaiting_current_round'),
            'local_optimum': stop_reason == 'local_optimum_50_pairwise',
            'search_path': path, 'candidates': ranked, 'all_candidates': candidates, 'invalid': invalid,
        }

    @staticmethod
    def _safe_candidate_summary(params):
        """Expose display metadata without leaking frozen candidate internals or paths."""
        def safe_text(value, max_length=4096):
            if not isinstance(value, str):
                return None
            value = value.strip()
            if (not value or len(value) > max_length or '\n' in value or '\r' in value
                    or value.startswith(('/', '\\'))
                    or (len(value) >= 3 and value[1] == ':' and value[2] in ('/', '\\'))
                    or '../' in value or '..\\' in value):
                return None
            return value

        talent_candidate = params.get('talent_candidate')
        if isinstance(talent_candidate, dict):
            candidate = {'type': 'talent'}
            for key, max_length in (('name', 200), ('talent', 4096), ('source', 200)):
                value = safe_text(talent_candidate.get(key), max_length)
                if value is not None:
                    candidate[key] = value
            return candidate

        gear_candidate = params.get('gear_candidate')
        if isinstance(gear_candidate, dict):
            candidate = {'type': 'gear'}
            for key in ('slot', 'item_id'):
                value = gear_candidate.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    candidate[key] = value
                else:
                    value = safe_text(value, 100)
                    if value is not None:
                        candidate[key] = value
            for key in ('name', 'source'):
                value = safe_text(gear_candidate.get(key), 200)
                if value is not None:
                    candidate[key] = value
            return candidate

        search = params.get('search')
        if not isinstance(search, dict):
            return {}
        candidate = {}
        for key in ('round', 'candidate_index'):
            value = search.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                candidate[key] = value
        move = search.get('move')
        if isinstance(move, dict):
            candidate['move'] = {
                key: value for key, value in move.items()
                if key in SimcComparisonTaskAPIView.ATTRIBUTE_STATS
                and isinstance(value, (int, float)) and not isinstance(value, bool)
            }
        return candidate

    @classmethod
    def _frozen_input_facts(cls, task, baseline=None):
        """Return display-safe facts from the immutable inputs selected by a task."""
        baseline = baseline or SimcWorkbenchAPIView._comparison_baseline_summary(task)
        facts = {}

        def add(key, label, value, display=None, detail=''):
            facts[key] = {
                'label': label,
                'value': value,
                'display': cls._format_input_value(value) if display is None else str(display or '—'),
                'detail': str(detail or ''),
            }

        def resource_fact(key, label, version, fallback, summary):
            name = str((summary or {}).get('name') or fallback or '—')
            identity = (
                ('snapshot', str(version.content_hash or f'id:{version.id}'))
                if version else ('live', getattr(fallback, 'id', None), name)
            )
            add(key, label, identity, name, f'快照 #{version.id}' if version else '')

        resource_fact('profile', 'Profile', task.profile_version, task.profile, baseline.get('profile'))
        resource_fact('template', '基础模板', task.template_version, task.template, baseline.get('template'))
        resource_fact('apl', 'APL', task.apl_version, task.apl, baseline.get('apl'))

        backend = baseline.get('backend') or {}
        backend_name = str(backend.get('name') or '—')
        backend_version = str(backend.get('version') or '')
        add(
            'backend', '执行后端',
            (task.backend_id, backend_version),
            backend_name,
            f'版本 {backend_version}' if backend_version else '',
        )

        simulation_labels = (
            ('fight_style', '战斗类型'), ('desired_targets', '目标数'),
            ('max_time', '模拟时长'), ('iterations', '迭代次数'),
            ('target_error', '目标误差'), ('vary_combat_length', '战斗时长浮动'),
            ('threads', '线程数'), ('enemy_type', '敌人类型'),
            ('raid_buffs', '团队增益'), ('use_class_raid_buff', '使用职业团队增益'),
        )
        simulation_params = task.simulation_params if isinstance(task.simulation_params, dict) else {}
        for key, label in simulation_labels:
            if key in simulation_params:
                value = simulation_params[key]
                normalized = sorted(value) if key == 'raid_buffs' and isinstance(value, list) else value
                add(f'simulation.{key}', label, normalized)

        character_labels = (
            ('name', '玩家'), ('class', '职业'), ('spec', '专精'),
            ('race', '种族'), ('level', '等级'),
        )
        character = baseline.get('character') if isinstance(baseline.get('character'), dict) else {}
        for key, label in character_labels:
            value = character.get(key)
            if value not in (None, ''):
                add(f'character.{key}', label, value)

        talent = str(baseline.get('_talent') or '')
        if talent:
            add('talent', '天赋', talent)

        stat_labels = {
            'strength': '力量', 'agility': '敏捷', 'intellect': '智力',
            'crit': '暴击', 'haste': '急速', 'mastery': '精通',
            'versatility': '全能',
        }
        stats = baseline.get('stats') if isinstance(baseline.get('stats'), dict) else {}
        for key, value in stats.items():
            add(f'stat.{key}', stat_labels.get(key, key), value)

        equipped = baseline.get('equipped') if isinstance(baseline.get('equipped'), dict) else {}
        for slot, item in equipped.items():
            signature = SimcWorkbenchAPIView._simc_item_signature(item)
            add(
                f'equipment.{slot}', f'装备 · {slot}', signature,
                cls._format_input_item(item),
            )
        return facts

    @staticmethod
    def _format_input_value(value):
        if value is None or value == '':
            return '—'
        if isinstance(value, bool):
            return '是' if value else '否'
        if isinstance(value, (list, tuple)):
            return '、'.join(str(item) for item in value) if value else '无'
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value)

    @staticmethod
    def _format_input_item(item):
        item = item if isinstance(item, dict) else {}
        title = str(item.get('name') or '物品')
        details = []
        if item.get('item_id') is not None:
            details.append(f"ID {item['item_id']}")
        if item.get('item_level') is not None:
            details.append(f"装等 {item['item_level']}")
        modifiers = item.get('modifiers') if isinstance(item.get('modifiers'), dict) else {}
        if modifiers:
            details.append(json.dumps(modifiers, ensure_ascii=False, sort_keys=True))
        return f"{title}（{'，'.join(details)}）" if details else title

    @classmethod
    def _frozen_input_differences(cls, baseline, current):
        differences = []
        ordered_keys = list(baseline) + [key for key in current if key not in baseline]
        for key in ordered_keys:
            before = baseline.get(key)
            after = current.get(key)
            before_value = before.get('value') if before else None
            after_value = after.get('value') if after else None
            if before_value == after_value:
                continue
            before_display = before.get('display', '—') if before else '—'
            after_display = after.get('display', '—') if after else '—'
            if before_display == after_display:
                if before and before.get('detail'):
                    before_display = f"{before_display}（{before['detail']}）"
                if after and after.get('detail'):
                    after_display = f"{after_display}（{after['detail']}）"
            differences.append({
                'key': key,
                'label': (after or before or {}).get('label') or key,
                'before': before_display,
                'after': after_display,
            })
        return differences

    def _get_multi_task_payload(self, request, task_ids):
        """Build a safe comparison report from selected ordinary simulation tasks."""
        tasks = list(SimcTask.objects.filter(
            id__in=task_ids, user_id=request.user.id, is_active=True,
        ).select_related(
            'apl', 'apl_version', 'profile', 'profile_version',
            'template', 'template_version', 'backend',
        ).order_by('id'))
        if len(tasks) != len(task_ids):
            raise PermissionError('所选任务不存在或无权限访问')
        rows = []
        invalid = []
        for task in tasks:
            run = task.simulation_runs.filter(status='completed').prefetch_related('artifacts').order_by('-sequence').first()
            if run is None:
                invalid.append({'id': task.id, 'name': task.name, 'error': '没有已完成的结果'})
                continue
            summary = run.result_summary if isinstance(run.result_summary, dict) else {}
            params = task.simulation_params if isinstance(task.simulation_params, dict) else {}
            apl_payload = task.apl_version.payload if task.apl_version_id and isinstance(task.apl_version.payload, dict) else {}
            profile_payload = task.profile_version.payload if task.profile_version_id and isinstance(task.profile_version.payload, dict) else {}
            frozen_baseline = SimcWorkbenchAPIView._comparison_baseline_summary(task)
            frozen_input_facts = self._frozen_input_facts(task, frozen_baseline)
            apl_name = str(apl_payload.get('name') or getattr(task.apl, 'name', '') or params.get('override_action_list_name') or '—')
            profile_name = str(profile_payload.get('name') or getattr(task.profile, 'name', '') or '—')
            fight_style = str(params.get('fight_style') or params.get('fight_style_label') or 'Patchwerk')
            target_count = params.get('target_count', params.get('desired_targets'))
            battle_scenario = f'{fight_style} · {target_count}目标' if target_count not in (None, '') else fight_style
            result_file = SimcComparisonTaskAPIView._run_result_file(run)
            parsed = {}
            if not summary.get('dps') and result_file:
                content = self._get_result_file_content(result_file)
                parsed = self._parse_regular_result(content) if content else {}
            dps = summary.get('dps') or parsed.get('dps')
            if not isinstance(dps, (int, float)):
                invalid.append({'id': task.id, 'name': task.name, 'error': '无法解析已完成结果的 DPS'})
                continue
            rows.append({
                'id': task.id, 'name': task.name, 'label': task.name,
                'is_base': not rows, 'is_base_candidate': not rows,
                'dps': dps, 'candidate_name': '',
                'character': parsed.get('character') or frozen_baseline.get('character') or {},
                'simulation': parsed.get('simulation') or frozen_baseline.get('simulation_params') or {},
                'talents': parsed.get('talents') or {'string': frozen_baseline.get('_talent') or ''},
                'abilities': parsed.get('abilities', parsed.get('top_abilities', [])),
                'top_abilities': parsed.get('top_abilities', []),
                'apl_name': apl_name, 'profile_name': profile_name,
                'battle_scenario': battle_scenario,
                'apl_list': str(apl_payload.get('content') or getattr(task.apl, 'content', '') or params.get('override_action_list') or ''),
                'run_id': run.id,
                '_input_facts': frozen_input_facts,
            })
        if len(rows) < 2:
            raise ValueError('至少需要两个拥有已完成结果的任务')
        baseline = rows[0]
        baseline_dps = baseline['dps']
        baseline_input_facts = baseline.get('_input_facts') or {}
        for row in rows:
            current_input_facts = row.pop('_input_facts', {})
            if row is baseline:
                row['input_differences'] = []
                row['input_difference_summary'] = '对比基准'
            else:
                differences = self._frozen_input_differences(
                    baseline_input_facts, current_input_facts,
                )
                row['input_differences'] = differences
                row['input_difference_summary'] = (
                    f'{len(differences)} 项输入不同' if differences else '与基准输入一致'
                )
        ranked = sorted(rows, key=lambda row: (-row['dps'], row['id']))
        for rank, row in enumerate(ranked, start=1):
            row['rank'] = rank
            row['delta_dps'] = row['dps'] - baseline_dps
            row['delta_percent'] = round(row['delta_dps'] / baseline_dps * 100, 2) if baseline_dps else None
        winner = ranked[0]
        return {
            'task': {'task_id': None, 'name': '选定模拟结果对比', 'mode': 'multi_task_comparison', 'total': len(rows)},
            'runs': rows,
            'comparison': {
                'baseline': {'id': baseline['id'], 'label': baseline['label'], 'dps': baseline_dps},
                'winner': {'id': winner['id'], 'label': winner['label'], 'dps': winner['dps'],
                           'delta_dps': winner['delta_dps'], 'delta_percent': winner['delta_percent']},
            },
            'invalid': invalid,
            'selected_task_ids': [task.id for task in tasks],
        }
    
    def get(self, request):
        try:
            raw_task_ids = request.GET.get('task_ids')
            if raw_task_ids:
                try:
                    task_ids = list(dict.fromkeys(
                        int(value) for value in raw_task_ids.split(',') if str(value).strip()
                    ))
                except (TypeError, ValueError):
                    return JsonResponse({'success': False, 'error': 'task_ids必须是逗号分隔的整数'}, status=400)
                if len(task_ids) < 2 or len(task_ids) > 20:
                    return JsonResponse({'success': False, 'error': '请选择2至20个模拟结果'}, status=400)
                try:
                    payload = self._get_multi_task_payload(request, task_ids)
                except PermissionError as error:
                    return JsonResponse({'success': False, 'error': str(error)}, status=404)
                except ValueError as error:
                    return JsonResponse({'success': False, 'error': str(error)}, status=400)
                return JsonResponse({'success': True, 'data': payload})
            task_id = str(request.GET.get('task_id') or '').strip()
            if not task_id:
                return JsonResponse({'success': False, 'error': 'task_id不能为空'}, status=400)
            try:
                task = SimcTask.objects.get(
                    id=int(task_id), user_id=request.user.id, is_active=True,
                    mode__in=('comparison', 'attribute_sweep'),
                )
            except (TypeError, ValueError, SimcTask.DoesNotExist):
                return JsonResponse({'success': False, 'error': '比较任务不存在或无权限访问'}, status=404)
            runs = list(task.simulation_runs.prefetch_related('artifacts').order_by('sequence'))
            if not runs:
                return JsonResponse({'success': False, 'error': '比较任务没有执行记录'}, status=404)
            status_counts = {'pending': 0, 'running': 0, 'succeeded': 0, 'failed': 0}
            invalid = []
            rows = []
            run_candidates = []
            for run in runs:
                params = run.candidate_params if isinstance(run.candidate_params, dict) else {}
                search = params.get('search') if isinstance(params.get('search'), dict) else {}
                label = run.candidate_label or run.candidate_key
                candidate_index = search.get('candidate_index', run.sequence - 1)
                is_base = bool(params.get('is_base'))
                run_candidates.append((run, params))
                status_key = {'pending': 'pending', 'running': 'running', 'completed': 'succeeded',
                              'failed': 'failed'}.get(run.status, 'failed')
                status_counts[status_key] += 1
                summary = run.result_summary if isinstance(run.result_summary, dict) else {}
                dps = summary.get('dps')
                result_file = SimcComparisonTaskAPIView._run_result_file(run)
                if dps is None and run.status == 'completed' and result_file:
                    html_content = self._get_result_file_content(result_file)
                    dps = (self._parse_regular_result(html_content) if html_content else {}).get('dps')
                if run.status == 'completed' and dps is None:
                    invalid.append({'id': run.id, 'error': '无法解析该候选的独立 DPS 结果'})
                candidate = self._safe_candidate_summary(params)
                rows.append({
                    'id': run.id, 'name': label, 'label': label, 'index': candidate_index,
                    'is_base': is_base, 'candidate': candidate,
                    'current_status': run.status, 'dps': dps,
                })
            rows.sort(key=lambda row: (row['index'] is None,
                                       row['index'] if row['index'] is not None else row['id']))
            completed_rows = [row for row in rows if isinstance(row.get('dps'), (int, float))]
            ranked_rows = sorted(completed_rows, key=lambda row: (-row['dps'], row['id']))
            rank_by_id = {row['id']: rank for rank, row in enumerate(ranked_rows, start=1)}
            baseline_row = next((row for row in rows if row.get('is_base')), None)
            baseline_dps = baseline_row.get('dps') if baseline_row else None
            for row in rows:
                row['rank'] = rank_by_id.get(row['id'])
                if isinstance(row.get('dps'), (int, float)) and isinstance(baseline_dps, (int, float)):
                    row['delta_dps'] = row['dps'] - baseline_dps
                    row['delta_percent'] = round((row['delta_dps'] / baseline_dps) * 100, 2) if baseline_dps else None
                else:
                    row['delta_dps'] = row['delta_percent'] = None
            winner_row = next((row for row in ranked_rows if not row['is_base']), None)
            comparison = {
                'baseline': ({'id': baseline_row['id'], 'label': baseline_row['label'],
                              'dps': baseline_row['dps']} if baseline_row else None),
                'winner': ({'id': winner_row['id'], 'label': winner_row['label'],
                            'dps': winner_row['dps'], 'delta_dps': winner_row.get('delta_dps'),
                            'delta_percent': winner_row.get('delta_percent')} if winner_row else None),
            }
            current_round = max([run.round_number for run in runs] or [1])
            attribute_report = (
                self._build_reference_attribute_report(runs, task.analysis_result)
                if task.mode == 'attribute_sweep' else None
            )
            task_payload = {
                'task_id': task.id, 'name': task.name, 'status': task.current_status,
                'mode': task.mode, 'total': len(rows), 'current_round': current_round,
                **status_counts,
            }
            summary_rows = [{
                'id': row['id'], 'name': row['name'], 'label': row['label'],
                'rank': row['rank'], 'dps': row['dps'],
                'delta_dps': row['delta_dps'], 'delta_percent': row['delta_percent'],
                'candidate': row['candidate'],
            } for row in rows]
            return JsonResponse({'success': True, 'data': {
                'task': task_payload, 'runs': summary_rows, 'comparison': comparison,
                'attribute_report': self._safe_attribute_report(attribute_report),
                'invalid': [{'id': item.get('id'), 'error': item.get('error', '')} for item in invalid],
            }})
        except Exception as e:
            logger.error(f"常规模拟对比失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': '生成对比摘要失败，请稍后重试'
            }, status=500)

    def _parse_task_ext(self, ext_data):
        if not ext_data:
            return {}
        if isinstance(ext_data, dict):
            return ext_data
        text = str(ext_data).strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    
    def _get_result_file_content(self, result_file):
        try:
            import os
            import requests
            from django.conf import settings
            
            oss_config = getattr(settings, 'OSS_CONFIG', {})
            base_url = oss_config.get('base_url', '')
            
            if base_url:
                try:
                    file_url = base_url + result_file
                    response = requests.get(file_url, timeout=30)
                    if response.status_code == 200:
                        return response.text
                except requests.RequestException:
                    pass
            
            local_file_path = os.path.join(settings.BASE_DIR, 'static', 'simc_results', result_file)
            if os.path.exists(local_file_path):
                with open(local_file_path, 'r', encoding='utf-8') as f:
                    return f.read()
            
            return None
        except Exception:
            return None
    
    def _parse_regular_result(self, html_content):
        result = {
            'dps': None,
            'character': {},
            'simulation': {},
            'talents': {},
            'abilities': [],
            'top_abilities': []
        }
        
        try:
            dps_pattern = r':\s*([\d,]+)\s*dps'
            match = re.search(dps_pattern, html_content, re.IGNORECASE)
            if match:
                dps_str = match.group(1).replace(',', '')
                try:
                    result['dps'] = int(dps_str)
                except ValueError:
                    result['dps'] = None
        except Exception:
            result['dps'] = None
        
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, 'html.parser')
            
            player = soup.find(class_='player')
            if player:
                h2_tag = player.find('h2')
                if h2_tag and not result['dps']:
                    text = h2_tag.get_text(' ', strip=True)
                    match = re.search(r':\s*([\d,]+)\s*dps', text, re.IGNORECASE)
                    if match:
                        try:
                            result['dps'] = int(match.group(1).replace(',', ''))
                        except ValueError:
                            pass
                
                params = player.select('.params li')
                for li in params:
                    text = li.get_text(' ', strip=True)
                    if 'Race:' in text:
                        result['character']['race'] = text.split(':', 1)[1].strip()
                    elif 'Class:' in text:
                        result['character']['class'] = text.split(':', 1)[1].strip()
                    elif 'Spec:' in text:
                        result['character']['spec'] = text.split(':', 1)[1].strip()
                    elif 'Level:' in text:
                        result['character']['level'] = text.split(':', 1)[1].strip()
                
                talent_row = player.select_one('tr.left td')
                if talent_row:
                    talent_string = talent_row.get_text(' ', strip=True)
                    if talent_string:
                        result['talents']['string'] = talent_string
                
                set_bonus_items = player.select('tr.left.nowrap td li')
                if set_bonus_items:
                    result['talents']['set_bonuses'] = [li.get_text(' ', strip=True) for li in set_bonus_items if li.get_text(strip=True)]
                
                abilities_table = soup.select_one('.player table.sc.sort') or soup.select_one('table.sc.sort')
                if abilities_table:
                    abilities = []
                    rows = abilities_table.select('tbody tr.toprow:not(.childrow)')
                    for row in rows:
                        cells = row.find_all('td', recursive=False)
                        if len(cells) < 3:
                            cells = row.find_all('td')
                        if len(cells) < 3:
                            continue
                        
                        name = cells[0].get_text(' ', strip=True)
                        dps_text = cells[1].get_text(' ', strip=True)
                        dps_match = re.search(r'\(([\d,]+)\)', dps_text)
                        dps_value_text = (dps_match.group(1) if dps_match else dps_text).replace(',', '').strip()
                        
                        dps_percent_text = cells[2].get_text(' ', strip=True)
                        dps_percent_match = re.search(r'\(([\d.]+%)\)', dps_percent_text)
                        dps_percent_value_text = (dps_percent_match.group(1) if dps_percent_match else dps_percent_text).strip()
                        
                        dps_percent_number = None
                        percent_match = re.search(r'([\d.]+)', dps_percent_value_text)
                        if percent_match:
                            try:
                                dps_percent_number = float(percent_match.group(1))
                            except ValueError:
                                dps_percent_number = None
                        
                        if name:
                            abilities.append({
                                'name': name,
                                'dps': dps_value_text,
                                'dps_percent': dps_percent_value_text,
                                'dps_percent_number': dps_percent_number
                            })
                    
                    abilities.sort(key=lambda x: x.get('dps_percent_number') if x.get('dps_percent_number') is not None else -1, reverse=True)
                    result['abilities'] = [{
                        'name': a.get('name', ''),
                        'dps': a.get('dps', ''),
                        'dps_percent': a.get('dps_percent', '')
                    } for a in abilities]
                    result['top_abilities'] = result['abilities'][:12]
            
            masthead = soup.find(id='masthead')
            if masthead:
                params = masthead.select('.params li')
                for li in params:
                    text = li.get_text(' ', strip=True)
                    if 'Timestamp:' in text:
                        result['simulation']['timestamp'] = text.split(':', 1)[1].strip()
                    elif 'Iterations:' in text:
                        result['simulation']['iterations'] = text.split(':', 1)[1].strip()
                    elif 'Fight Length:' in text:
                        result['simulation']['fight_length'] = text.split(':', 1)[1].strip()
                    elif 'Fight Style:' in text:
                        result['simulation']['fight_style'] = text.split(':', 1)[1].strip()
        
        except Exception:
            pass
        
        return result


@method_decorator(login_required, name='dispatch')
class SimcTemplateAPIView(View):
    """Legacy compatibility API for the single global base template."""

    @staticmethod
    def _get_writable_template(request, template_id):
        template = SimcContentTemplate.objects.filter(
            id=template_id,
            owner_user_id__isnull=True,
        ).first()
        if not template:
            return None, JsonResponse({'success': False, 'error': '模板不存在'}, status=404)
        if template.source == SimcContentTemplate.SOURCE_SIMC_UPSTREAM:
            return None, JsonResponse({'success': False, 'error': '上游模板为只读资源'}, status=403)
        if not (request.user.is_staff or request.user.is_superuser):
            return None, JsonResponse({'success': False, 'error': '系统模板仅管理员可修改'}, status=403)
        return template, None

    @staticmethod
    def _validate_base_template(content):
        """验证 base_template 必须恰好一个 {player_config} 占位符，不允许 actor= 行。"""
        import re
        player_config_count = content.count('{player_config}')
        if player_config_count != 1:
            return f'基础模板必须恰好包含一个 {{player_config}} 占位符（当前有 {player_config_count} 个）'

        # 检查是否包含 actor= 行（player-scoped 或 actor-scoped 行）
        for line in content.split('\n'):
            stripped = line.strip()
            if stripped.startswith('actor=') or stripped.startswith('warrior=') or stripped.startswith('mage=') or stripped.startswith('priest=') or re.match(r'^(warrior|mage|priest|rogue|hunter|shaman|druid|paladin|warlock|monk|demon_hunter|death_knight|evoker)=', stripped):
                return f'基础模板不允许包含 actor 或玩家定义行（发现: {stripped[:50]}）'

        return None

    def get(self, request):
        """Return only the global base template; internal resources stay hidden."""
        try:
            template_id = request.GET.get('id')
            requested_type = request.GET.get('template_type')
            if requested_type and requested_type != 'base_template':
                return JsonResponse({'success': False, 'error': '无效的模板类型'}, status=400)

            templates = SimcContentTemplate.objects.filter(
                owner_user_id__isnull=True,
            ).order_by('spec', '-id')

            if template_id:
                template = templates.filter(id=template_id).first()
                if not template:
                    return JsonResponse({'success': False, 'error': '模板不存在'}, status=404)
                return JsonResponse({
                    'success': True,
                    'id': template.id,
                    'template_content': template.content,
                    'content': template.content,
                    'spec': template.spec,
                    'spec_label': _simc_spec_label(template.spec, template.class_name),
                    'class_name': template.class_name,
                    'name': template.name,
                    'template_type': 'base_template',
                    'source': template.source,
                    'is_active': template.is_active,
                    'is_selectable': template.is_selectable,
                })

            template_list = []
            for template in templates:
                preview = template.content[:100] + '...' if len(template.content) > 100 else template.content
                template_list.append({
                    'id': template.id,
                    'preview': preview,
                    'spec': template.spec,
                    'spec_label': _simc_spec_label(template.spec, template.class_name),
                    'class_name': template.class_name,
                    'name': template.name,
                    'template_type': 'base_template',
                    'source': template.source,
                    'is_active': template.is_active,
                    'is_selectable': template.is_selectable,
                })
            return JsonResponse({'success': True, 'templates': template_list})

        except Exception as e:
            logger.error(f"获取SimC模板失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '获取SimC模板失败'
            })
    
    def put(self, request):
        """Allow administrators to update only base-template content."""
        try:
            data = json.loads(request.body)
            template_id = request.GET.get('id') or data.get('id')
            template_content = data.get('template_content', '') or data.get('content', '') or data.get('template', '')

            if not template_id:
                return JsonResponse({'success': False, 'error': '模板ID不能为空'}, status=400)

            if not str(template_content).strip():
                return JsonResponse({'success': False, 'error': '模板内容不能为空'}, status=400)

            template, error_response = self._get_writable_template(request, template_id)
            if error_response:
                return error_response
            immutable_fields = ('name', 'source', 'spec', 'class_name')
            identity_changed = any(
                field in data and data[field] != getattr(template, field)
                for field in immutable_fields
            )
            identity_changed = identity_changed or any(
                field in data and data[field] != 'base_template'
                for field in ('template_type', 'type')
            )
            if identity_changed:
                return JsonResponse({'success': False, 'error': '系统模板身份字段不可修改'}, status=400)
            validation_error = self._validate_base_template(template_content)
            if validation_error:
                return JsonResponse({'success': False, 'error': validation_error}, status=400)
            template.content = template_content
            template.save(update_fields=['content', 'updated_at'])
            logger.info(f"SimC基础模板已更新: ID {template.id}")
            return JsonResponse({'success': True, 'message': '模板更新成功'})

        except Exception as e:
            logger.error(f"更新SimC模板失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '更新SimC模板失败'
            })
    
    def patch(self, request):
        return JsonResponse({'success': False, 'error': '系统基础模板不能停用'}, status=405)
    
    def post(self, request):
        """基础模板是单一系统资源，不通过 API 新增。"""
        return JsonResponse({'success': False, 'error': '基础模板不支持新增'}, status=405)

    def delete(self, request):
        return JsonResponse({'success': False, 'error': '系统基础模板不能删除'}, status=405)


@method_decorator(login_required, name='dispatch')
class SimcWorkbenchAPIView(View):
    """安全的 SimC 工作台资源总览、详情与白名单生命周期操作。"""

    SUMMARY_KEYS = {
        'dps', 'hps', 'dtps', 'mean', 'min', 'max', 'median', 'iterations', 'samples',
        'score', 'value', 'amount', 'percent', 'percentage', 'delta', 'rank', 'duration',
        'report', 'summary', 'metrics', 'statistics', 'players', 'name', 'label', 'unit',
    }

    @staticmethod
    def _template_is_protected(template):
        return template.source == SimcContentTemplate.SOURCE_SIMC_UPSTREAM

    @classmethod
    def _template_is_writable(cls, request, template):
        if cls._template_is_protected(template):
            return False
        return (
            template.owner_user_id is None
            and (request.user.is_staff or request.user.is_superuser)
        )

    @classmethod
    def _get_writable_template(cls, request, object_id):
        template = SimcContentTemplate.objects.filter(id=object_id).first()
        if not template:
            return None, JsonResponse({'success': False, 'error': '模板不存在'}, status=404)
        if template.owner_user_id is not None:
            return None, JsonResponse({'success': False, 'error': '模板不存在'}, status=404)
        if cls._template_is_protected(template):
            return None, JsonResponse({'success': False, 'error': '受保护模板为只读资源'}, status=403)
        if cls._template_is_writable(request, template):
            return template, None
        return None, JsonResponse({'success': False, 'error': '系统模板仅管理员可修改'}, status=403)

    @staticmethod
    def _validate_template_content(content):
        return SimcTemplateAPIView._validate_base_template(content)

    @staticmethod
    def _is_unique_integrity_error(exc):
        message = str(exc).lower()
        return 'unique' in message or 'duplicate entry' in message

    @staticmethod
    def _json_body(request):
        try:
            value = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            raise ValueError('无效的 JSON 数据')
        if not isinstance(value, dict):
            raise ValueError('请求正文必须是对象')
        return value

    @staticmethod
    def _safe_summary(value, key=None):
        if isinstance(value, dict):
            return {str(k): SimcWorkbenchAPIView._safe_summary(v, str(k).lower())
                    for k, v in value.items()
                    if str(k).lower() in SimcWorkbenchAPIView.SUMMARY_KEYS}
        if isinstance(value, list):
            return [SimcWorkbenchAPIView._safe_summary(v, key) for v in value[:100]]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, str) and key in {'report', 'summary', 'name', 'label', 'unit'}:
            return value[:500]
        return None

    @staticmethod
    def _safe_mode_summary(value):
        """Expose candidate metadata without frozen player/APL/gear bodies."""
        if not isinstance(value, dict):
            return {}
        safe = {}
        for key in ('candidate_type', 'is_base'):
            item = value.get(key)
            if isinstance(item, (str, int, float, bool)) or item is None:
                safe[key] = item
        ratings = value.get('attribute_ratings')
        if isinstance(ratings, dict):
            safe['attribute_ratings'] = {
                key: item for key, item in ratings.items()
                if key in SimcComparisonTaskAPIView.ATTRIBUTE_STATS and isinstance(item, (int, float))
            }
        search = value.get('search')
        if isinstance(search, dict):
            safe['search'] = {
                key: item for key, item in search.items()
                if key in {'round', 'step', 'converged', 'stop_reason',
                           'candidate_index', 'preprocess_stage'}
                and isinstance(item, (str, int, float, bool))
            }
        talent_candidate = value.get('talent_candidate')
        if isinstance(talent_candidate, dict):
            safe['talent_candidate'] = {
                key: str(talent_candidate.get(key) or '')
                for key in ('name', 'talent', 'source')
            }
        gear_swap = value.get('gear_swap')
        if isinstance(gear_swap, dict):
            safe['gear_swap'] = SimcWorkbenchAPIView._safe_simc_item(
                gear_swap.get('raw_value'), gear_swap.get('slot'), gear_swap,
            )
        return safe

    @staticmethod
    def _safe_simc_item(raw_value, slot='', metadata=None):
        """Return display-only item facts without exposing a complete player block."""
        metadata = metadata if isinstance(metadata, dict) else {}
        raw = str(raw_value or '').strip()
        if '=' in raw and raw.partition('=')[0].strip().lower() == str(slot or '').strip().lower():
            raw = raw.partition('=')[2].strip()
        parts = [part.strip() for part in raw.split(',') if part.strip()]
        properties = {}
        name = ''
        for index, part in enumerate(parts):
            if '=' in part:
                key, item = part.split('=', 1)
                properties[key.strip().lower()] = item.strip()
            elif index == 0:
                name = part
        def integer(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        modifiers = {}
        for key in ('bonus_id', 'gem_id', 'crafted_stats'):
            if properties.get(key):
                modifiers[key] = [item for item in properties[key].split('/') if item]
        if properties.get('enchant_id'):
            modifiers['enchant_id'] = properties['enchant_id']
        return {
            'slot': str(slot or metadata.get('slot') or ''),
            'name': name,
            'item_id': integer(metadata.get('item_id')) or integer(properties.get('id')),
            'item_level': integer(properties.get('ilevel')),
            'source': str(metadata.get('source') or ''),
            'modifiers': modifiers,
        }

    @staticmethod
    def _simc_item_signature(item):
        item = item if isinstance(item, dict) else {}
        return {
            'item_id': item.get('item_id'),
            'item_level': item.get('item_level'),
            'modifiers': item.get('modifiers') or {},
        }

    @classmethod
    def _comparison_baseline_summary(cls, task):
        """Describe the frozen comparison baseline without returning executable bodies."""
        def resource(version, fallback_name=''):
            payload = version.payload if version and isinstance(version.payload, dict) else {}
            return {
                'name': str(payload.get('name') or fallback_name or ''),
                'version_id': version.id if version else None,
            }

        profile_version = task.profile_version
        profile_payload = (
            profile_version.payload
            if profile_version and isinstance(profile_version.payload, dict) else {}
        )
        player = str(profile_payload.get('player_equipment') or '')
        equipped = {}
        talent = str(profile_payload.get('talent') or '')
        character = {'name': '', 'class': '', 'spec': '', 'race': '', 'level': None}
        actor_classes = {
            'deathknight', 'demonhunter', 'druid', 'evoker', 'hunter', 'mage', 'monk',
            'paladin', 'priest', 'rogue', 'shaman', 'warlock', 'warrior',
        }
        in_candidate_section = False
        for line in player.splitlines():
            stripped = line.strip()
            if stripped.startswith('###'):
                in_candidate_section = True
            if in_candidate_section or not stripped or stripped.startswith('#') or '=' not in stripped:
                continue
            key, raw = stripped.split('=', 1)
            key = key.strip().lower()
            raw = raw.strip()
            if key in actor_classes:
                character['class'] = key
                character['name'] = raw.strip('"')
                continue
            if key in ('spec', 'race'):
                character[key] = raw
                continue
            if key == 'level':
                try:
                    character['level'] = int(raw)
                except (TypeError, ValueError):
                    character['level'] = None
                continue
            if key in ('talent', 'talents'):
                talent = raw
                continue
            parsed = cls._safe_simc_item(raw, key)
            if parsed.get('item_id'):
                equipped[key] = parsed
        character['spec'] = character['spec'] or str(
            profile_payload.get('spec') or getattr(task.profile, 'spec', '') or ''
        )
        stats = {}
        for payload_key, label in (
            ('gear_strength', 'strength'), ('gear_agility', 'agility'),
            ('gear_intellect', 'intellect'), ('gear_crit', 'crit'),
            ('gear_haste', 'haste'), ('gear_mastery', 'mastery'),
            ('gear_versatility', 'versatility'),
        ):
            value = profile_payload.get(payload_key)
            if isinstance(value, (int, float)):
                stats[label] = value
        return {
            'profile': {
                **resource(profile_version, getattr(task.profile, 'name', '')),
                'spec': str(profile_payload.get('spec') or getattr(task.profile, 'spec', '') or ''),
            },
            'template': resource(task.template_version, getattr(task.template, 'name', '')),
            'apl': resource(task.apl_version, getattr(task.apl, 'name', '')),
            'backend': {
                'name': str(getattr(task.backend, 'name', '') or ''),
                'version': str(getattr(task.backend, 'current_version', '') or ''),
            },
            'simulation_params': {
                key: item for key, item in (task.simulation_params or {}).items()
                if key in {'iterations', 'fight_style', 'max_time', 'vary_combat_length', 'threads'}
                and isinstance(item, (str, int, float, bool))
            },
            'character': character,
            'stats': stats,
            'talent': {'value': talent},
            'equipment': list(equipped.values()),
            '_talent': talent,
            'equipped': equipped,
        }

    @staticmethod
    def _comparison_candidate_params(params):
        """Normalize candidates created before mode fields moved to the Run root."""
        params = params if isinstance(params, dict) else {}
        nested = {}
        if isinstance(params.get('mode_params'), dict):
            nested.update(params['mode_params'])
        nested.update(params)
        return nested

    @classmethod
    def _comparison_change_summary(cls, params, baseline):
        params = params if isinstance(params, dict) else {}
        candidate_type = str(params.get('candidate_type') or 'base')
        if candidate_type == 'gear_swap':
            swap = params.get('gear_swap') if isinstance(params.get('gear_swap'), dict) else {}
            slot = str(swap.get('slot') or '')
            before = (baseline.get('equipped') or {}).get(slot)
            after = cls._safe_simc_item(swap.get('raw_value'), slot, swap)
            is_equivalent = bool(
                before and after and before.get('item_id') and after.get('item_id')
                and cls._simc_item_signature(before) == cls._simc_item_signature(after)
            )
            return {
                'change': {
                    'kind': 'gear', 'field': slot,
                    'before': before,
                    'after': after,
                    'is_equivalent': is_equivalent,
                },
                'unchanged': ['玩家身份', '其他装备槽位', '天赋', '基础模板', 'APL', '模拟参数', '执行后端'],
            }
        if candidate_type in ('talent', 'talent_override'):
            candidate = params.get('talent_candidate') if isinstance(params.get('talent_candidate'), dict) else {}
            return {
                'change': {
                    'kind': 'talent', 'field': 'talents',
                    'before': {'name': '基准天赋', 'value': baseline.get('_talent') or ''},
                    'after': {
                        'name': str(candidate.get('name') or '候选天赋'),
                        'value': str(candidate.get('talent') or params.get('talent_override') or ''),
                    },
                },
                'unchanged': ['玩家身份', '全部装备', '基础模板', 'APL', '模拟参数', '执行后端'],
            }
        return {'change': None, 'unchanged': ['全部基准配置']}

    @staticmethod
    def _task_status_label(status):
        """返回中文状态标签"""
        labels = {0: '待运行', 1: '运行中', 2: '成功', 3: '失败', 4: '运行中', 5: '已取消'}
        return labels.get(status, '未知')

    @staticmethod
    def _task_progress(task):
        """返回任务可信进度；运行中仅采用 Worker 已持久化的 progress。"""
        status = task.current_status
        if status == 0:  # pending
            return 0
        if status in (2, 3, 5):  # terminal
            return 100
        if status in (1, 4):  # running
            try:
                ext = json.loads(task.ext) if isinstance(task.ext, str) else (task.ext or {})
                progress = ext.get('progress')
                if isinstance(progress, (int, float)) and 0 <= progress <= 100:
                    return int(progress)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            return None
        return 0

    @staticmethod
    def _safe_run_error_summary(error_detail):
        """Expose the first native SimC diagnostic without leaking execution context."""
        text = str(error_detail or '')
        match = re.search(r'错误输出\s*:\s*([^\r\n]+)', text, flags=re.IGNORECASE)
        if not match:
            return '任务执行失败'
        diagnostic = match.group(1).strip()
        diagnostic = re.sub(r'^Error\s*:\s*', '', diagnostic, flags=re.IGNORECASE)
        diagnostic = re.split(r'\s+(?:command|stderr|stdout)\s*=', diagnostic, maxsplit=1)[0]
        diagnostic = re.sub(r'https?://\S+', '[已隐藏链接]', diagnostic)
        diagnostic = re.sub(r'(?<![\w.])/(?:[^\s/:]+/)+[^\s:]*', '[已隐藏路径]', diagnostic)
        diagnostic = re.sub(r'\b[A-Za-z]:\\(?:[^\s\\]+\\)*[^\s]*', '[已隐藏路径]', diagnostic)
        return diagnostic.strip()[:800] or '任务执行失败'

    @staticmethod
    def _run_row(run):
        params = run.candidate_params if isinstance(run.candidate_params, dict) else {}
        return {
            'id': run.id,
            'sequence': run.sequence,
            'round_number': run.round_number,
            'candidate_key': run.candidate_key,
            'candidate_label': run.candidate_label,
            'candidate_summary': SimcWorkbenchAPIView._safe_mode_summary(params),
            'status': run.status,
            'input_hash': run.input_hash,
            'result_summary': SimcWorkbenchAPIView._safe_summary(run.result_summary or {}),
            'error_summary': (
                SimcWorkbenchAPIView._safe_run_error_summary(run.error_detail)
                if run.status == 'failed' else ''
            ),
            'started_at': _fmt_dt(run.started_at),
            'completed_at': _fmt_dt(run.completed_at),
        }

    @staticmethod
    def _benchmark_history_row(execution):
        status_labels = {
            'pending': '待运行', 'running': '运行中', 'success': '成功',
            'partial': '部分完成', 'failed': '失败', 'cancelled': '已取消',
        }
        task_status_names = {0: 'pending', 1: 'running', 2: 'success', 3: 'failed', 4: 'running', 5: 'cancelled'}
        active = execution.status in {
            SimcBenchmarkExecution.STATUS_PENDING,
            SimcBenchmarkExecution.STATUS_RUNNING,
        }
        # History is a lightweight lifecycle list: terminal rows trust the frozen
        # Execution/Case state. Full result/seal validation belongs to the detail API
        # and must not prefetch every SimcBenchmarkResult on each polling request.
        case_rows = getattr(execution, '_history_cases', None)
        if case_rows is None:
            case_rows = list(SimcBenchmarkCase.objects.filter(execution_id=execution.pk).select_related(
                'task',
            ).only(
                'id', 'execution_id', 'task_id', 'status', 'error_detail',
                'spec_key', 'scenario_key', 'profile_key',
                'spec_label', 'scenario_label', 'profile_label',
                'task__id', 'task__current_status', 'task__ext', 'task__source_task_id',
                'task__error_detail',
            ).order_by('id'))

        task_ids = [case.task_id for case in case_rows if case.task_id]
        runs_by_task = defaultdict(list)
        for task_id, run_status, count in SimulationRun.objects.filter(task_id__in=task_ids).values_list(
            'task_id', 'status',
        ).annotate(count=models.Count('id')):
            runs_by_task[task_id].append((run_status, count))

        cases = []
        progress_values = []
        task_counts = {key: 0 for key in ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')}
        run_counts = {key: 0 for key in ('pending', 'running', 'success', 'failed', 'cancelled')}
        current_run_count = 0
        for case in case_rows:
            task = case.task
            task_status = task_status_names.get(task.current_status, 'failed') if active and task else case.status
            progress = task_progress(task) if active and task else (None if active else 100)
            effective_progress = progress
            if effective_progress is None:
                effective_progress = 100 if task_status in {'success', 'failed', 'cancelled'} else 0
            progress_values.append(effective_progress)
            if task_status in task_counts:
                task_counts[task_status] += 1
            if task is not None:
                for run_status, count in runs_by_task[task.id]:
                    normalized_status = 'success' if run_status == 'completed' else run_status
                    if normalized_status in run_counts:
                        run_counts[normalized_status] += count
                    current_run_count += count
            cases.append({
                'case_id': case.pk, 'task_id': case.task_id,
                'source_task_id': task.source_task_id if task is not None else None,
                'coordinate': {
                    'spec_key': case.spec_key,
                    'scenario_key': case.scenario_key,
                    'profile_key': case.profile_key,
                },
                'labels': {
                    'spec': _benchmark_spec_display_name(case.spec_label, case.spec_key),
                    'scenario': case.scenario_label,
                    'profile': case.profile_label,
                },
                'status': task_status,
                'status_label': status_labels.get(task_status, '未知'),
                'progress': progress,
                'case_status': case.status,
                'error': _benchmark_safe_string(
                    case.error_detail or (task.error_detail if task is not None else ''),
                ),
            })
        progress = int(sum(progress_values) / len(progress_values)) if progress_values else None
        snapshot = execution.config_snapshot if isinstance(execution.config_snapshot, dict) else {}
        baseline_case_count = snapshot.get('case_count')
        baseline_run_count = snapshot.get('run_count')
        if type(baseline_case_count) is not int or baseline_case_count < 0:
            baseline_case_count = len(cases)
        if type(baseline_run_count) is not int or baseline_run_count < 0:
            baseline_run_count = current_run_count
        is_retry = bool(cases) and all(case['source_task_id'] is not None for case in cases)
        baseline_counts = None
        if is_retry:
            source_execution_ids = set()
            source_task_ids = [case['source_task_id'] for case in cases]
            source_cases = SimcBenchmarkCase.objects.filter(task_id__in=source_task_ids)
            source_execution_ids.update(source_cases.values_list('execution_id', flat=True))
            if len(source_execution_ids) == 1:
                source_execution_id = source_execution_ids.pop()
                source_execution = SimcBenchmarkExecution.objects.get(pk=source_execution_id)
                source_status_counts = dict(SimcBenchmarkCase.objects.filter(
                    execution_id=source_execution_id,
                ).values_list('status').annotate(count=models.Count('id')))
                source_run_counts = {key: 0 for key in ('pending', 'running', 'success', 'failed', 'cancelled')}
                for run_status, count in SimulationRun.objects.filter(
                    task__benchmark_case__execution_id=source_execution_id,
                ).values_list('status').annotate(count=models.Count('id')):
                    normalized_status = 'success' if run_status == 'completed' else run_status
                    if normalized_status in source_run_counts:
                        source_run_counts[normalized_status] += count
                baseline_counts = {
                    'execution_id': source_execution_id,
                    'cases': source_execution.config_snapshot.get('case_count', source_cases.count()),
                    'runs': source_execution.config_snapshot.get('run_count', 0),
                    'case_counts': {key: source_status_counts.get(key, 0) for key in (
                        'pending', 'running', 'success', 'partial', 'failed', 'cancelled',
                    )},
                    'run_counts': source_run_counts,
                }
            else:
                baseline_counts = {'cases': baseline_case_count, 'runs': baseline_run_count}
        return {
            'row_type': 'benchmark_execution', 'id': execution.pk,
            'execution_id': execution.pk, 'panel_id': execution.panel_id,
            'name': f'{execution.panel.name} · 执行 #{execution.pk}',
            'status': execution.status,
            'status_label': status_labels.get(execution.status, '未知'),
            'is_active': active,
            'progress': progress, 'case_count': len(cases),
            'task_counts': task_counts,
            'run_count': current_run_count, 'run_counts': run_counts,
            'baseline_counts': baseline_counts,
            'created_at': execution.created_at,
            'detail_resource': 'benchmark_executions', 'cases': cases,
        }

    @staticmethod
    def _task_row(task):
        summary = {}
        latest_run = task.simulation_runs.filter(status='completed').order_by('-sequence').first()
        if latest_run and isinstance(latest_run.result_summary, dict):
            summary = SimcWorkbenchAPIView._safe_summary(latest_run.result_summary)
        else:
            try:
                parsed = json.loads(task.result_summary or '{}')
                summary = SimcWorkbenchAPIView._safe_summary(parsed) if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                pass
        report_artifact = task.artifacts.filter(artifact_type='html_report').order_by('-id').first()
        report_preview_url = ''
        if report_artifact is not None:
            if report_artifact.file_path.startswith('simc_agent_results/'):
                from botend.services.simc_agent_oss import ReportStorageError, public_report_url
                try:
                    report_preview_url = public_report_url(report_artifact.file_path)
                except ReportStorageError:
                    report_preview_url = ''
            else:
                report_preview_url = f'/api/simc-workbench/tasks/{task.id}/report-preview/'
        has_report = bool(report_preview_url)
        return {
            'id': task.id, 'name': task.name, 'status': task.current_status,
            'status_label': SimcWorkbenchAPIView._task_status_label(task.current_status),
            'progress': SimcWorkbenchAPIView._task_progress(task),
            'mode': task.mode,
            'candidate_label': task.candidate_label, 'result_summary': summary,
            'runs': [
                SimcWorkbenchAPIView._run_row(run)
                for run in task.simulation_runs.order_by('sequence')[:100]
            ],
            'has_report': has_report,
            'report_preview_url': report_preview_url,
            'is_active': task.is_active,
            'created_at': _fmt_dt(task.create_time), 'updated_at': _fmt_dt(task.modified_time),
        }

    @staticmethod
    def _artifact_row(artifact, include_task=False):
        can_preview = artifact.artifact_type == 'html_report'
        row = {
            'id': artifact.id,
            'task_id': artifact.task_id,
            'artifact_type': artifact.artifact_type,
            'file_name': os.path.basename(artifact.file_path),
            'file_size': artifact.file_size,
            'can_preview': can_preview,
            'created_at': _fmt_dt(artifact.created_at),
        }
        if include_task:
            row['task_name'] = artifact.task.name
        if can_preview:
            if artifact.file_path.startswith('simc_agent_results/'):
                from botend.services.simc_agent_oss import ReportStorageError, public_report_url
                try:
                    row['preview_url'] = public_report_url(artifact.file_path)
                except ReportStorageError:
                    row['can_preview'] = False
                else:
                    row['is_external'] = True
            else:
                row['preview_url'] = f'/api/simc-workbench/artifacts/{artifact.id}/preview/'
                row['is_external'] = False
        return row

    def get(self, request, resource, object_id=None):
        if resource == 'history':
            try:
                page = int(request.GET.get('page', 1))
                page_size = int(request.GET.get('page_size', 20))
            except (ValueError, TypeError):
                return JsonResponse({'success': False, 'error': '分页参数必须为整数'}, status=400)
            page = max(1, page)
            page_size = max(1, min(50, page_size))

            benchmark_ancestor_ids = set()
            ancestor_frontier = set(
                SimcBenchmarkCase.objects.filter(
                    task__user_id=request.user.id,
                    task__source_task_id__isnull=False,
                ).values_list('task__source_task_id', flat=True)
            )
            while ancestor_frontier:
                benchmark_ancestor_ids.update(ancestor_frontier)
                ancestor_frontier = set(
                    SimcTask.objects.filter(
                        pk__in=ancestor_frontier,
                        source_task_id__isnull=False,
                    ).values_list('source_task_id', flat=True)
                ) - benchmark_ancestor_ids

            ordinary_refs = [
                ('task', task_id, modified_time)
                for task_id, modified_time in SimcTask.objects.filter(
                    user_id=request.user.id,
                    benchmark_case__isnull=True,
                ).exclude(
                    pk__in=benchmark_ancestor_ids,
                ).values_list('id', 'modified_time')
            ]
            execution_refs = [
                ('benchmark_execution', execution_id, created_at)
                for execution_id, created_at in SimcBenchmarkExecution.objects.filter(
                    cases__task__user_id=request.user.id,
                ).distinct().values_list('id', 'created_at')
            ]
            refs = ordinary_refs + execution_refs
            # Stable page boundaries even when MySQL timestamps have identical precision.
            refs.sort(
                key=lambda item: (item[2], item[0] == 'benchmark_execution', item[1]),
                reverse=True,
            )
            total = len(refs)
            total_pages = (total + page_size - 1) // page_size
            offset = (page - 1) * page_size
            page_refs = refs[offset:offset + page_size]

            task_ids = [object_id for row_type, object_id, _created_at in page_refs if row_type == 'task']
            execution_ids = [
                object_id for row_type, object_id, _created_at in page_refs
                if row_type == 'benchmark_execution'
            ]
            tasks = {
                task.pk: task for task in SimcTask.objects.filter(pk__in=task_ids).only(
                    'id', 'name', 'current_status', 'ext', 'modified_time', 'mode',
                    'profile_id', 'apl_id', 'profile_version_id', 'apl_version_id', 'simulation_params',
                )
            }
            version_ids = {
                version_id for task in tasks.values()
                for version_id in (task.profile_version_id, task.apl_version_id)
                if version_id
            }
            versions = {
                version.id: version for version in SimcResourceVersion.objects.filter(
                    id__in=version_ids,
                ).only('id', 'resource_type', 'payload')
            }
            profile_ids = {task.profile_id for task in tasks.values() if task.profile_id}
            apl_ids = {task.apl_id for task in tasks.values() if task.apl_id}
            profiles = {
                profile.id: profile for profile in SimcProfile.objects.filter(id__in=profile_ids).only('id', 'name')
            }
            apls = {
                apl.id: apl for apl in SimcApl.objects.filter(id__in=apl_ids).only('id', 'name')
            }
            completed_task_ids = set(SimulationRun.objects.filter(
                task_id__in=task_ids, status='completed',
            ).values_list('task_id', flat=True))
            history_cases = SimcBenchmarkCase.objects.select_related('task').only(
                'id', 'execution_id', 'task_id', 'status', 'error_detail',
                'spec_key', 'scenario_key', 'profile_key',
                'spec_label', 'scenario_label', 'profile_label',
                'task__id', 'task__current_status', 'task__ext', 'task__error_detail',
            ).order_by('id')
            executions = {
                execution.pk: execution
                for execution in SimcBenchmarkExecution.objects.filter(
                    pk__in=execution_ids,
                ).select_related('panel').only(
                    'id', 'panel_id', 'panel__name', 'status', 'created_at', 'config_snapshot',
                ).prefetch_related(
                    models.Prefetch('cases', queryset=history_cases, to_attr='_history_cases'),
                )
            }

            page_rows = []
            for row_type, object_id, _created_at in page_refs:
                if row_type == 'benchmark_execution':
                    execution = executions.get(object_id)
                    if execution is not None:
                        page_rows.append(self._benchmark_history_row(execution))
                    continue
                task = tasks.get(object_id)
                if task is not None:
                    profile_version = versions.get(task.profile_version_id)
                    apl_version = versions.get(task.apl_version_id)
                    profile_payload = profile_version.payload if profile_version and isinstance(profile_version.payload, dict) else {}
                    apl_payload = apl_version.payload if apl_version and isinstance(apl_version.payload, dict) else {}
                    params = task.simulation_params if isinstance(task.simulation_params, dict) else {}
                    fight_style = str(params.get('fight_style') or 'Patchwerk').strip()
                    target_count = params.get('desired_targets', 1)
                    try:
                        target_count = int(target_count)
                    except (TypeError, ValueError):
                        target_count = None
                    scenario = fight_style or 'Patchwerk'
                    if target_count is not None and target_count > 0:
                        scenario = f'{scenario} · {target_count}目标'
                    page_rows.append({
                        'id': task.id,
                        'name': task.name,
                        'status': task.current_status,
                        'status_label': self._task_status_label(task.current_status),
                        'progress': self._task_progress(task),
                        'created_at': task.modified_time,
                        'detail_resource': 'tasks',
                        'mode': task.mode,
                        'can_compare': task.current_status == 2 and task.id in completed_task_ids,
                        'apl_name': apl_payload.get('name') or getattr(apls.get(task.apl_id), 'name', '') or '—',
                        'profile_name': profile_payload.get('name') or getattr(profiles.get(task.profile_id), 'name', '') or '—',
                        'battle_scenario': scenario,
                    })
            return JsonResponse({
                'success': True,
                'data': [{**row, 'created_at': _fmt_dt(row['created_at'])} for row in page_rows],
                'pagination': {
                    'page': page,
                    'page_size': page_size,
                    'total': total,
                    'total_pages': total_pages,
                },
            })

        if resource == 'tasks':
            qs = SimcTask.objects.filter(user_id=request.user.id).order_by('-modified_time')
            if object_id:
                task = qs.filter(id=object_id).first()
                if not task:
                    return JsonResponse({'success': False, 'error': '任务不存在'}, status=404)
                row = self._task_row(task)
                row.update({
                    'profile_id': task.profile_id, 'template_id': task.template_id, 'apl_id': task.apl_id,
                    'profile_version_id': task.profile_version_id,
                    'template_version_id': task.template_version_id,
                    'apl_version_id': task.apl_version_id,
                    'apl_name': (
                        ((task.apl_version.payload or {}).get('name')
                         if task.apl_version and isinstance(task.apl_version.payload, dict) else '')
                        or (task.apl.name if task.apl else '')
                        or ''
                    ),
                    'profile_name': (
                        ((task.profile_version.payload or {}).get('name')
                         if task.profile_version and isinstance(task.profile_version.payload, dict) else '')
                        or (task.profile.name if task.profile else '')
                        or ''
                    ),
                    'simulation_params': task.simulation_params or {},
                    'mode_summary': self._safe_mode_summary(task.mode_params),
                    'source_task_id': task.source_task_id,
                })
                artifacts = list(task.artifacts.all().order_by('-created_at'))
                runs = list(task.simulation_runs.all().order_by('-sequence'))
                latest_run = runs[0] if runs else None
                report_artifact = next((
                    artifact for artifact in artifacts
                    if latest_run and artifact.run_id == latest_run.id
                    and artifact.artifact_type == 'html_report'
                ), None)
                report_summary = None
                if report_artifact:
                    from botend.services.simc_result_analysis import (
                        analyze_run_artifact,
                        localize_report_summary,
                    )
                    report_summary = analyze_run_artifact(task, report_artifact)
                    if report_summary:
                        profile = task.profile
                        class_name = (profile.class_name if profile else '') or report_summary.get('character', {}).get('class', '')
                        spec_name = (profile.spec if profile else '') or report_summary.get('character', {}).get('spec', '')
                        class_token = re.sub(r'[^a-z0-9]+', '_', str(class_name).casefold()).strip('_')
                        spec_token = re.sub(r'[^a-z0-9]+', '_', str(spec_name).casefold()).strip('_')
                        spec_key = spec_token if spec_token.startswith(class_token + '_') else '_'.join(
                            part for part in (class_token, spec_token) if part
                        )
                        bilingual_pairs = []
                        spell_names = {}
                        try:
                            if spec_key:
                                bilingual_pairs = ConvertTextAPIView().bilingual_pairs(spec_key)[0]
                            identity = _latest_catalog_identity()
                            spell_ids = {
                                int(row['spell_id'])
                                for rows in (
                                    report_summary.get('abilities') or [],
                                    (report_summary.get('buffs') or {}).get('dynamic') or [],
                                    (report_summary.get('buffs') or {}).get('constant') or [],
                                )
                                for row in rows
                                if str(row.get('spell_id') or '').isdigit()
                            }
                            if identity and spell_ids:
                                for localized_spell in WowSpellSnapshot.objects.filter(
                                    branch='wow', locale='zhCN', snapshot_build=identity[1],
                                    spell_id__in=spell_ids,
                                ).exclude(name_zh='').values('spell_id', 'name_zh').order_by(
                                    'spell_id', '-updated_at', '-id',
                                ):
                                    spell_names.setdefault(
                                        localized_spell['spell_id'], localized_spell['name_zh'],
                                    )
                        except Exception:
                            logger.warning('SimC report APL localization catalog unavailable', exc_info=True)
                        report_summary = localize_report_summary(
                            report_summary, bilingual_pairs, spell_names,
                        )
                row['report_summary'] = report_summary
                row['report_artifact_id'] = report_artifact.id if report_artifact else None
                row['artifacts'] = [self._artifact_row(item) for item in artifacts]
                ordered_runs = list(reversed(runs))
                row['runs'] = [self._run_row(run) for run in ordered_runs]
                baseline_context = self._comparison_baseline_summary(task)
                row['comparison_baseline'] = {
                    key: item for key, item in baseline_context.items()
                    if key not in {'equipped', '_talent'}
                }
                ranking = []
                for run in ordered_runs:
                    params = self._comparison_candidate_params(run.candidate_params)
                    summary = run.result_summary if isinstance(run.result_summary, dict) else {}
                    dps = summary.get('dps')
                    comparison = self._comparison_change_summary(params, baseline_context)
                    ranking.append({
                        'id': run.id,
                        'label': run.candidate_label or run.candidate_key,
                        'candidate_icon_url': str(
                            (run.display_metadata or {}).get('icon_url') or params.get('icon_url') or '',
                        ),
                        'dps': dps if isinstance(dps, (int, float)) else None,
                        'is_base': bool(params.get('is_base')),
                        'is_complete': run.status == 'completed' and isinstance(dps, (int, float)),
                        'candidate': self._safe_mode_summary(params),
                        **comparison,
                    })
                ranked = sorted(
                    (item for item in ranking if item['is_complete'] and not item['is_base']),
                    key=lambda item: (-item['dps'], item['id']),
                )
                rank_by_id = {item['id']: index for index, item in enumerate(ranked, 1)}
                for item in ranking:
                    item['rank'] = rank_by_id.get(item['id'])
                row['ranking'] = ranking
                row['attribute_report'] = None
                if task.mode == 'attribute_sweep':
                    raw_report = SimcRegularCompareAPIView()._build_reference_attribute_report(
                        ordered_runs, task.analysis_result,
                    )
                    row['attribute_report'] = SimcRegularCompareAPIView()._safe_attribute_report(raw_report)
                row['report_url'] = (
                    f'/simc-compare/?task_id={task.id}'
                    if task.mode in ('comparison', 'attribute_sweep') and runs else ''
                )
                return JsonResponse({'success': True, 'data': row})

            # 分页参数白名单校验
            try:
                page = int(request.GET.get('page', 1))
                page_size = int(request.GET.get('page_size', 20))
            except (ValueError, TypeError):
                return JsonResponse({'success': False, 'error': '分页参数必须为整数'}, status=400)

            page = max(1, page)
            page_size = max(1, min(50, page_size))  # 默认20，最大50

            total = qs.count()
            total_pages = (total + page_size - 1) // page_size
            offset = (page - 1) * page_size

            tasks = qs[offset:offset + page_size]
            return JsonResponse({
                'success': True,
                'data': [self._task_row(row) for row in tasks],
                'pagination': {
                    'page': page,
                    'page_size': page_size,
                    'total': total,
                    'total_pages': total_pages,
                }
            })

        if resource == 'artifacts':
            qs = SimcTaskArtifact.objects.filter(task__user_id=request.user.id).select_related('task').order_by('-created_at')
            if object_id:
                row = qs.filter(id=object_id).first()
                if not row:
                    return JsonResponse({'success': False, 'error': '产物不存在'}, status=404)
                return JsonResponse({
                    'success': True,
                    'data': self._artifact_row(row, include_task=True),
                })
            try:
                page = int(request.GET.get('page', 1))
                page_size = int(request.GET.get('page_size', 20))
            except (ValueError, TypeError):
                return JsonResponse({'success': False, 'error': '分页参数必须为整数'}, status=400)
            page = max(1, page)
            page_size = max(1, min(50, page_size))
            task_id = request.GET.get('task_id')
            if task_id not in (None, ''):
                try:
                    qs = qs.filter(task_id=int(task_id))
                except (ValueError, TypeError):
                    return JsonResponse({'success': False, 'error': 'task_id 必须为整数'}, status=400)
            artifact_type = str(request.GET.get('artifact_type') or '').strip()
            if artifact_type:
                qs = qs.filter(artifact_type=artifact_type)
            total = qs.count()
            total_pages = (total + page_size - 1) // page_size
            offset = (page - 1) * page_size
            rows = [
                self._artifact_row(row, include_task=True)
                for row in qs[offset:offset + page_size]
            ]
            return JsonResponse({'success': True, 'data': rows, 'pagination': {
                'page': page, 'page_size': page_size, 'total': total, 'total_pages': total_pages,
            }})

        if resource == 'profiles':
            qs = SimcProfile.objects.filter(user_id=request.user.id).order_by('-id')
            if object_id:
                qs = qs.filter(id=object_id)
            rows = list(qs.values('id', 'name', 'spec', 'player_config_mode', 'battlenet_region', 'battlenet_realm', 'battlenet_character', 'talent', 'gear_strength', 'gear_crit', 'gear_haste', 'gear_mastery', 'gear_versatility', 'is_active'))
            if object_id:
                return JsonResponse({'success': True, 'data': rows[0]} if rows else {'success': False, 'error': '配置不存在'}, status=200 if rows else 404)
            return JsonResponse({'success': True, 'data': rows})

        if resource == 'apls':
            if _is_simc_admin(request.user):
                qs = SimcApl.objects.all()
            else:
                qs = SimcApl.objects.filter(
                    models.Q(is_system=True, owner_user_id__isnull=True)
                    | models.Q(is_system=False, owner_user_id=request.user.id)
                )
            qs = qs.order_by('is_system', 'spec', 'name')
            if object_id:
                apl = qs.filter(id=object_id).first()
                if not apl:
                    return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
                return JsonResponse({'success': True, 'data': {
                    'id': apl.id, 'name': apl.name, 'spec': apl.spec,
                    'spec_label': _simc_spec_label(apl.spec, apl.class_name),
                    'class_label': _simc_class_label(apl.spec, apl.class_name),
                    'class_name': apl.class_name, 'source': apl.source,
                    'is_system': apl.is_system, 'is_active': apl.is_active,
                    'is_selectable': apl.is_selectable, 'content': apl.content,
                    'read_only': apl.is_system and not _is_simc_admin(request.user),
                    'can_copy': apl.is_system and apl.is_active and apl.is_selectable,
                    'content_hash': content_hash(apl.content),
                    'validation_status': apl.validation_status,
                    'validation_revision': apl.validation_revision,
                    'validation_game_build': apl.validation_game_build,
                    'validation_stale_reason': apl.validation_staleness(current_validation_identity()),
                    'can_use_for_task': apl.is_active and (not apl.is_system or apl.is_selectable),
                }, 'can_write': _is_simc_admin(request.user) or not apl.is_system})
            return JsonResponse({'success': True, 'data': [{
                'id': apl.id, 'name': apl.name, 'spec': apl.spec,
                    'spec_label': _simc_spec_label(apl.spec, apl.class_name),
                'class_label': _simc_class_label(apl.spec, apl.class_name),
                'class_name': apl.class_name, 'source': apl.source,
                'is_system': apl.is_system, 'is_active': apl.is_active,
                'is_selectable': apl.is_selectable,
                'validation_status': apl.validation_status,
                'validation_stale_reason': apl.validation_staleness(current_validation_identity()),
                'can_use_for_task': apl.is_active and (not apl.is_system or apl.is_selectable),
                'read_only': apl.is_system and not _is_simc_admin(request.user),
                'can_copy': apl.is_system and apl.is_active and apl.is_selectable,
            } for apl in qs], 'can_write': True})

        if resource == 'templates':
            # 工作台只展示和维护 SimC 输入框架；默认玩家配置是内部导入资源，
            # APL 则由独立的 APL 资源库负责，不能混入内容模板列表。
            qs = SimcContentTemplate.objects.filter(
                owner_user_id__isnull=True,
            ).order_by('spec', 'name')
            if object_id:
                qs = qs.filter(id=object_id)
            rows = []
            for row in qs:
                item = {
                    'id': row.id, 'name': row.name, 'template_type': 'base_template',
                    'type_label': '基础模板', 'source': row.source, 'spec': row.spec,
                    'spec_label': _simc_spec_label(row.spec, row.class_name),
                    'class_name': row.class_name, 'is_active': row.is_active,
                    'is_selectable': row.is_selectable, 'is_system': row.owner_user_id is None,
                    'read_only': not self._template_is_writable(request, row),
                }
                if object_id:
                    item['content'] = row.content
                rows.append(item)
            if object_id:
                return JsonResponse(
                    {'success': True, 'data': rows[0], 'can_write': not rows[0]['read_only']}
                    if rows else {'success': False, 'error': '模板不存在'},
                    status=200 if rows else 404,
                )
            return JsonResponse({
                'success': True,
                'data': rows,
                'can_write': request.user.is_staff or request.user.is_superuser,
            })

        if resource in ('secondary-rules', 'mastery-rules'):
            model = SimcSecondaryStatRule if resource == 'secondary-rules' else SimcMasteryCoefficient
            fields = ('id', 'class_name', 'crit_per_percent', 'haste_per_percent', 'mastery_per_percent', 'versatility_per_percent') if resource == 'secondary-rules' else ('id', 'spec', 'mastery_coefficient')
            if object_id:
                row = model.objects.filter(id=object_id).values(*fields).first()
                if not row:
                    return JsonResponse({'success': False, 'error': '规则不存在'}, status=404)
                return JsonResponse({'success': True, 'data': row})
            return JsonResponse({'success': True, 'data': list(model.objects.order_by(fields[1]).values(*fields)), 'can_write': request.user.is_staff})


        if resource == 'apl-storage':
            qs = SimcApl.objects.filter(owner_user_id=request.user.id).order_by('-id')
            if object_id:
                row = qs.filter(id=object_id).first()
                if not row:
                    return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
                return JsonResponse({'success': True, 'data': {
                    'id': row.id,
                    'title': row.name,
                    'spec': row.spec,
                    'spec_label': _simc_spec_label(row.spec, row.class_name),
                    'class_label': _simc_class_label(row.spec, row.class_name),
                    'apl_code': row.content,
                    'is_active': row.is_active,
                }})
            rows = []
            for row in qs:
                rows.append({
                    'id': row.id,
                    'title': row.name,
                    'spec': row.spec,
                    'spec_label': _simc_spec_label(row.spec, row.class_name),
                    'class_label': _simc_class_label(row.spec, row.class_name),
                    'apl_code': row.content,
                    'is_active': row.is_active,
                })
            return JsonResponse({'success': True, 'data': rows})

        if resource == 'backends':
            return SimcBackendBinaryAPIView().get(request)

        return JsonResponse({'success': False, 'error': '未知工作台资源'}, status=404)

    def post(self, request, resource, object_id=None):
        try:
            data = self._json_body(request)
        except ValueError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=400)
        if 'task_type' in data:
            return JsonResponse({'success': False, 'error': '不再支持 task_type 参数；请使用 SimC 工作台的任务模式入口。'}, status=400)
        action = str(data.get('action') or '').strip()
        if resource == 'tasks' and object_id:
            task = SimcTask.objects.filter(id=object_id, user_id=request.user.id).first()
            if not task:
                return JsonResponse({'success': False, 'error': '任务不存在'}, status=404)
            if action == 'archive' and task.current_status not in (1, 4):
                task.is_active = False
                task.save(update_fields=['is_active', 'modified_time'])
            elif action == 'restore' and task.current_status not in (1, 4):
                task.is_active = True
                task.save(update_fields=['is_active', 'modified_time'])
            elif action == 'rerun':
                from botend.services.task_rerun import create_rerun as service_create_rerun, TaskRerunError
                overrides = {}
                for key in ('name', 'simulation_params', 'profile_id', 'template_id', 'apl_id'):
                    if key in data:
                        overrides[key] = data[key]
                try:
                    task = service_create_rerun(task.id, request.user.id, overrides)
                except TaskRerunError as exc:
                    return JsonResponse({'success': False, 'error': str(exc)}, status=400)
                object_id = task.id
            else:
                return JsonResponse({'success': False, 'error': '当前状态不允许该操作'}, status=409)
            return JsonResponse({'success': True, 'data': {'id': object_id, 'mode': task.mode}})
        if resource == 'apls' and object_id and action == 'publish':
            try:
                profile_id = int(data.get('profile_id'))
            except (TypeError, ValueError):
                return JsonResponse({'success': False, 'error': '需要实际玩家配置进行校验'}, status=400)
            try:
                from botend.services.simc_apl.publish import publish_apl
                result = publish_apl(object_id, request.user.id, profile_id)
                apl = SimcApl.objects.get(pk=object_id)
            except SimcApl.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
            except SimcProfile.DoesNotExist:
                return JsonResponse({'success': False, 'error': '玩家配置不存在'}, status=404)
            except PermissionError:
                return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
            except ValueError as exc:
                return JsonResponse({'success': False, 'error': str(exc)}, status=400)
            except RuntimeError:
                return JsonResponse({'success': False, 'error': 'APL 内容已变化，请重新校验'}, status=409)
            status = 200 if result.get('valid') else 422
            return JsonResponse({'success': bool(result.get('valid')), 'data': {
                'id': apl.id, 'content_hash': content_hash(apl.content),
                'validation_status': apl.validation_status,
                'is_selectable': apl.is_selectable,
            }, 'error': None if result.get('valid') else 'APL 权威校验失败'}, status=status)
        if resource == 'apls' and object_id and action in ('archive', 'restore'):
            apl = SimcApl.objects.filter(id=object_id).first()
            if not apl:
                return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
            if apl.is_system and not _is_simc_admin(request.user):
                return JsonResponse({'success': False, 'error': '系统默认 APL 为只读资源'}, status=403)
            if not apl.is_system and apl.owner_user_id != request.user.id and not _is_simc_admin(request.user):
                return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
            try:
                with transaction.atomic():
                    apl.is_active = action == 'restore'
                    apl.save(update_fields=['is_active'])
            except IntegrityError as exc:
                if action == 'restore' and self._is_unique_integrity_error(exc):
                    return JsonResponse({
                        'success': False,
                        'error': '同一专精下已存在同名的活跃 APL',
                    }, status=409)
                raise
            return JsonResponse({'success': True})
        if resource == 'apl-storage' and object_id and action in ('archive', 'restore'):
            apl = SimcApl.objects.filter(id=object_id, owner_user_id=request.user.id).first()
            if not apl:
                return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
            apl.is_active = action == 'restore'
            apl.save(update_fields=['is_active'])
            return JsonResponse({'success': True})
        if resource == 'templates':
            if not object_id:
                return JsonResponse({'success': False, 'error': '基础模板不支持新增'}, status=405)
            if action in ('archive', 'restore'):
                return JsonResponse({'success': False, 'error': '系统基础模板不能停用'}, status=405)
        if resource == 'apls' and not object_id:
            copy_source_id = data.get('copy_source_id')
            if copy_source_id is not None:
                try:
                    source_id = int(copy_source_id)
                except (TypeError, ValueError):
                    return JsonResponse({'success': False, 'error': 'APL ID 无效'}, status=400)
                source = SimcApl.objects.filter(id=source_id).first()
                if not source or (not source.is_system and source.owner_user_id != request.user.id):
                    return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
                if source.is_system and (not source.is_active or not source.is_selectable):
                    return JsonResponse({'success': False, 'error': '该 APL 不可复制'}, status=400)
                spec = _canonical_simc_spec(source.spec)
                if not spec:
                    return JsonResponse({'success': False, 'error': '专精标识无效'}, status=400)
                for suffix in range(1, 9):
                    name = f'{source.name} 副本 {suffix}'
                    if SimcApl.objects.filter(
                        owner_user_id=request.user.id, spec=spec, name=name, is_active=True,
                    ).exists():
                        continue
                    try:
                        with transaction.atomic():
                            apl = SimcApl.objects.create(
                                name=name, spec=spec, class_name=_simc_class_for_spec(spec),
                                content=source.content, source=SimcApl.SOURCE_USER,
                                is_system=False, owner_user_id=request.user.id,
                                is_active=True, is_selectable=False,
                                validation_status=SimcApl.VALIDATION_DRAFT,
                            )
                    except IntegrityError as exc:
                        if self._is_unique_integrity_error(exc):
                            continue
                        raise
                    return JsonResponse({'success': True, 'data': {'id': apl.id}})
                return JsonResponse({
                    'success': False,
                    'error': '无法分配可用的 APL 副本名称，请稍后重试',
                }, status=409)
            copy_template_id = data.get('copy_template_id')
            if copy_template_id is not None:
                try:
                    template_id = int(copy_template_id)
                except (TypeError, ValueError):
                    return JsonResponse({'success': False, 'error': '模板 ID 无效'}, status=400)
                template = SimcApl.objects.filter(id=template_id).first()
                if not template:
                    return JsonResponse({'success': False, 'error': 'APL 模板不存在'}, status=404)
                if template.owner_user_id is not None and template.owner_user_id != request.user.id and not _is_simc_admin(request.user):
                    return JsonResponse({'success': False, 'error': 'APL 模板不存在'}, status=404)
                if not template.is_system or not template.is_active or not template.is_selectable:
                    return JsonResponse({'success': False, 'error': '该 APL 模板不可复制'}, status=400)
                spec = _canonical_simc_spec(template.spec)
                if not spec:
                    return JsonResponse({'success': False, 'error': '专精标识无效'}, status=400)
                for suffix in range(8):
                    name = template.name if suffix == 0 else f'{template.name} 副本 {suffix}'
                    if SimcApl.objects.filter(
                        owner_user_id=request.user.id, spec=spec, name=name, is_active=True,
                    ).exists():
                        continue
                    try:
                        with transaction.atomic():
                            apl = SimcApl.objects.create(
                                name=name, spec=spec, class_name=_simc_class_for_spec(spec),
                                content=template.content, source=SimcApl.SOURCE_USER,
                                is_system=False, owner_user_id=request.user.id,
                                is_active=True, is_selectable=False,
                                validation_status=SimcApl.VALIDATION_DRAFT,
                            )
                    except IntegrityError as exc:
                        if self._is_unique_integrity_error(exc):
                            continue
                        raise
                    return JsonResponse({'success': True, 'data': {'id': apl.id}})
                return JsonResponse({
                    'success': False,
                    'error': '无法分配可用的 APL 副本名称，请稍后重试',
                }, status=409)
            name = str(data.get('name') or '').strip()
            spec = _canonical_simc_spec(data.get('spec'))
            content = str(data.get('content') or '')
            if not name:
                return JsonResponse({'success': False, 'error': 'APL 名称不能为空'}, status=400)
            if not spec:
                return JsonResponse({'success': False, 'error': '专精标识无效'}, status=400)
            if not content.strip():
                return JsonResponse({'success': False, 'error': 'APL 内容不能为空'}, status=400)
            try:
                apl = SimcApl.objects.create(
                    name=name, spec=spec, class_name=_simc_class_for_spec(spec), content=content,
                    source=SimcApl.SOURCE_USER, is_system=False,
                    owner_user_id=request.user.id, is_active=True, is_selectable=False,
                    validation_status=SimcApl.VALIDATION_DRAFT,
                )
            except Exception as exc:
                if 'active_unique_key' in str(exc) or 'UNIQUE' in str(exc):
                    return JsonResponse({'success': False, 'error': '同一专精下已存在同名 APL'}, status=409)
                raise
            return JsonResponse({'success': True, 'data': {'id': apl.id}})

        if resource in ('secondary-rules', 'mastery-rules'):
            if not request.user.is_staff:
                return JsonResponse({'success': False, 'error': '仅管理员可修改规则'}, status=403)
            model = SimcSecondaryStatRule if resource == 'secondary-rules' else SimcMasteryCoefficient
            if not object_id:
                try:
                    if resource == 'secondary-rules':
                        class_name = str(data.get('class_name') or '').strip()
                        if not class_name:
                            return JsonResponse({'success': False, 'error': '职业标识不能为空'}, status=400)
                        if model.objects.filter(class_name=class_name).exists():
                            return JsonResponse({'success': False, 'error': f'职业 {class_name} 的规则已存在'}, status=409)
                        try:
                            crit = float(data.get('crit_per_percent', 46))
                            haste = float(data.get('haste_per_percent', 44))
                            mastery = float(data.get('mastery_per_percent', 46))
                            versa = float(data.get('versatility_per_percent', 54))
                        except (TypeError, ValueError):
                            return JsonResponse({'success': False, 'error': '属性值必须是有效数字'}, status=400)
                        rule = model.objects.create(
                            class_name=class_name,
                            crit_per_percent=crit,
                            haste_per_percent=haste,
                            mastery_per_percent=mastery,
                            versatility_per_percent=versa,
                        )
                    else:
                        spec = str(data.get('spec') or '').strip()
                        if not spec:
                            return JsonResponse({'success': False, 'error': '专精标识不能为空'}, status=400)
                        if model.objects.filter(spec=spec).exists():
                            return JsonResponse({'success': False, 'error': f'专精 {spec} 的规则已存在'}, status=409)
                        try:
                            coef = float(data.get('mastery_coefficient', 1.4))
                        except (TypeError, ValueError):
                            return JsonResponse({'success': False, 'error': '精通系数必须是有效数字'}, status=400)
                        rule = model.objects.create(
                            spec=spec,
                            mastery_coefficient=coef,
                        )
                    return JsonResponse({'success': True, 'data': {'id': rule.id}})
                except Exception as e:
                    logger.error(f"创建规则失败: {str(e)}")
                    return JsonResponse({'success': False, 'error': '创建规则失败'}, status=500)
        return JsonResponse({'success': False, 'error': '不支持的资源操作'}, status=400)

    def put(self, request, resource, object_id=None):
        try:
            data = self._json_body(request)
        except ValueError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=400)
        if resource == 'apls':
            if not object_id:
                return JsonResponse({'success': False, 'error': '缺少 APL ID'}, status=400)
            apl = SimcApl.objects.filter(id=object_id).first()
            if not apl:
                return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
            if apl.is_system and not _is_simc_admin(request.user):
                return JsonResponse({'success': False, 'error': '系统默认 APL 为只读资源'}, status=403)
            if not apl.is_system and apl.owner_user_id != request.user.id and not _is_simc_admin(request.user):
                return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
            target_spec = _canonical_simc_spec(data.get('spec') if 'spec' in data else apl.spec)
            if not target_spec:
                return JsonResponse({'success': False, 'error': '专精标识无效'}, status=400)
            target_name = str(data.get('name') or '').strip() if 'name' in data else apl.name
            target_content = str(data.get('content') or '') if 'content' in data else apl.content
            if not target_name:
                return JsonResponse({'success': False, 'error': 'APL 名称不能为空'}, status=400)
            if not target_content.strip():
                return JsonResponse({'success': False, 'error': 'APL 内容不能为空'}, status=400)
            apl.name = target_name
            apl.spec = target_spec
            apl.class_name = _simc_class_for_spec(target_spec)
            apl.content = target_content
            try:
                apl.save()
            except Exception as exc:
                if 'active_unique_key' in str(exc) or 'UNIQUE' in str(exc):
                    return JsonResponse({'success': False, 'error': '同一专精下已存在同名 APL'}, status=409)
                raise
            return JsonResponse({'success': True})
        if resource == 'templates':
            if not object_id:
                return JsonResponse({'success': False, 'error': '缺少模板ID'}, status=400)
            tpl, error_response = self._get_writable_template(request, object_id)
            if error_response:
                return error_response
            try:
                immutable_fields = ('name', 'source', 'spec', 'class_name')
                identity_changed = any(
                    field in data and data[field] != getattr(tpl, field)
                    for field in immutable_fields
                )
                identity_changed = identity_changed or (
                    'template_type' in data and data['template_type'] != 'base_template'
                )
                if identity_changed:
                    return JsonResponse({'success': False, 'error': '系统模板身份字段不可修改'}, status=400)
                target_content = str(data['content'] or '') if 'content' in data else tpl.content
                if not target_content.strip():
                    return JsonResponse({'success': False, 'error': '模板内容不能为空'}, status=400)
                validation_error = self._validate_template_content(target_content)
                if validation_error:
                    return JsonResponse({'success': False, 'error': validation_error}, status=400)
                if 'content' in data:
                    tpl.content = target_content
                tpl.save(update_fields=['content', 'updated_at'])
                return JsonResponse({'success': True})
            except Exception as e:
                if 'active_unique_key' in str(e) or 'UNIQUE' in str(e):
                    return JsonResponse({'success': False, 'error': '修改后的模板与已有活跃模板冲突'}, status=409)
                logger.error(f"更新模板失败: {str(e)}")
                return JsonResponse({'success': False, 'error': '更新模板失败'}, status=500)

        if resource in ('secondary-rules', 'mastery-rules'):
            if not request.user.is_staff:
                return JsonResponse({'success': False, 'error': '仅管理员可修改规则'}, status=403)
            if not object_id:
                return JsonResponse({'success': False, 'error': '缺少规则ID'}, status=400)
            model = SimcSecondaryStatRule if resource == 'secondary-rules' else SimcMasteryCoefficient
            rule = model.objects.filter(id=object_id).first()
            if not rule:
                return JsonResponse({'success': False, 'error': '规则不存在'}, status=404)
            try:
                if resource == 'secondary-rules':
                    if 'crit_per_percent' in data:
                        rule.crit_per_percent = float(data['crit_per_percent'])
                    if 'haste_per_percent' in data:
                        rule.haste_per_percent = float(data['haste_per_percent'])
                    if 'mastery_per_percent' in data:
                        rule.mastery_per_percent = float(data['mastery_per_percent'])
                    if 'versatility_per_percent' in data:
                        rule.versatility_per_percent = float(data['versatility_per_percent'])
                else:
                    if 'mastery_coefficient' in data:
                        rule.mastery_coefficient = float(data['mastery_coefficient'])
                rule.save()
                return JsonResponse({'success': True})
            except (TypeError, ValueError):
                return JsonResponse({'success': False, 'error': '属性值必须是有效数字'}, status=400)
            except Exception as e:
                logger.error(f"更新规则失败: {str(e)}")
                return JsonResponse({'success': False, 'error': '更新规则失败'}, status=500)
        return JsonResponse({'success': False, 'error': '不支持的资源操作'}, status=400)

    def delete(self, request, resource, object_id=None):
        if resource == 'apls':
            if not object_id:
                return JsonResponse({'success': False, 'error': '缺少 APL ID'}, status=400)
            apl = SimcApl.objects.filter(id=object_id).first()
            if not apl:
                return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
            if apl.is_system and not _is_simc_admin(request.user):
                return JsonResponse({'success': False, 'error': '系统默认 APL 为只读资源'}, status=403)
            if not apl.is_system and apl.owner_user_id != request.user.id and not _is_simc_admin(request.user):
                return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
            apl.delete()
            return JsonResponse({'success': True})
        if resource == 'templates':
            return JsonResponse({'success': False, 'error': '系统基础模板不能删除'}, status=405)

        if resource in ('secondary-rules', 'mastery-rules'):
            if not request.user.is_staff:
                return JsonResponse({'success': False, 'error': '仅管理员可修改规则'}, status=403)
            if not object_id:
                return JsonResponse({'success': False, 'error': '缺少规则ID'}, status=400)
            model = SimcSecondaryStatRule if resource == 'secondary-rules' else SimcMasteryCoefficient
            rule = model.objects.filter(id=object_id).first()
            if not rule:
                return JsonResponse({'success': False, 'error': '规则不存在'}, status=404)
            rule.delete()
            return JsonResponse({'success': True})
        return JsonResponse({'success': False, 'error': '不支持的资源操作'}, status=400)


@method_decorator(login_required, name='dispatch')
class SimcRunInputPreviewAPIView(View):
    """Compose readable SimC input for one owned Run from its task configuration."""

    def get(self, request, task_id, run_id):
        run = SimulationRun.objects.filter(
            id=run_id, task_id=task_id, task__user_id=request.user.id,
        ).select_related('task').first()
        if run is None:
            return JsonResponse({'success': False, 'error': '执行输入不存在'}, status=404)

        from botend.services.simc_run_control import build_frozen_run_input
        try:
            content, _manifest = build_frozen_run_input(run.task, run)
        except (TypeError, ValueError):
            return JsonResponse({'success': False, 'error': '当前任务配置无法生成 SimC 输入'}, status=422)
        return JsonResponse({'success': True, 'data': {
            'task_id': run.task_id,
            'run_id': run.id,
            'sequence': run.sequence,
            'content': content,
        }})


@method_decorator(login_required, name='dispatch')
class SimcTaskReportPreviewAPIView(View):
    """兼容没有 Artifact 记录的旧任务，并隐藏报告文件名与存储路径。"""

    def get(self, request, object_id):
        task = SimcTask.objects.filter(id=object_id, user_id=request.user.id).first()
        if not task or not task.result_file:
            return JsonResponse({'success': False, 'error': '任务报告不存在'}, status=404)
        if all((task.profile_id, task.template_id, task.apl_id,
                task.profile_version_id, task.template_version_id, task.apl_version_id)):
            return JsonResponse({'success': False, 'error': '引用型任务报告请通过 Artifact 预览'}, status=404)
        from botend.services.simc_artifacts import _validated_result
        result_name = os.path.basename(str(task.result_file))
        artifact = SimcTaskArtifact.objects.filter(
            task=task, artifact_type='html_report',
            file_path=f'simc_results/{result_name}',
        ).select_related('run').order_by('-created_at', '-id').first()
        validated = _validated_result(task, result_name, run=artifact.run if artifact else None)
        if not validated:
            return JsonResponse({'success': False, 'error': '任务报告不可用'}, status=404)
        response = FileResponse(open(str(validated[0]), 'rb'), content_type='text/html; charset=utf-8')
        response['Content-Security-Policy'] = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; sandbox allow-scripts; frame-ancestors 'self'"
        return response


@method_decorator(login_required, name='dispatch')
class SimcArtifactPreviewAPIView(View):
    def get(self, request, object_id):
        artifact = SimcTaskArtifact.objects.filter(
            id=object_id, task__user_id=request.user.id,
        ).select_related('task', 'run').first()
        artifact_path = str(artifact.file_path or '').replace('\\', '/') if artifact else ''
        allowed_prefixes = ('simc_results/', 'simc_agent_results/')
        if (not artifact or artifact.artifact_type != 'html_report'
                or not artifact_path.startswith(allowed_prefixes)):
            return JsonResponse({'success': False, 'error': '产物不存在'}, status=404)
        if artifact_path.startswith('simc_agent_results/'):
            from botend.services.simc_agent_oss import ReportStorageError, public_report_url
            try:
                return HttpResponseRedirect(public_report_url(artifact_path))
            except ReportStorageError:
                return JsonResponse({'success': False, 'error': '产物链接不可用'}, status=503)
        from botend.services.simc_artifacts import _validated_result
        validated = _validated_result(
            artifact.task, os.path.basename(artifact_path), run=artifact.run,
        )
        if not validated or validated[1] != artifact_path:
            return JsonResponse({'success': False, 'error': '产物文件不可用'}, status=404)
        full_path = str(validated[0])
        content_type = 'text/html; charset=utf-8'
        response = FileResponse(open(full_path, 'rb'), content_type=content_type)
        response['Content-Security-Policy'] = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; sandbox allow-scripts; frame-ancestors 'self'"
        return response


@method_decorator(login_required, name='dispatch')
class SimcBackendBinaryAPIView(View):
    """SimC后端更新状态API"""

    def _get_runtime_platform(self):
        sys_name = str(py_platform.system() or '').lower()
        if 'linux' in sys_name:
            machine = str(py_platform.machine() or '').lower()
            return 'linuxarm64' if machine in ('aarch64', 'arm64') else 'linux64'
        return 'unsupported'

    def _resolve_local_build_paths(self):
        cfg = getattr(settings, 'SIMC_CONFIG', {}) or {}
        source_dir = str(cfg.get('simc_source_dir') or '/home/lighthouse/simc').rstrip('/')
        build_dir = str(cfg.get('simc_build_dir') or os.path.join(source_dir, 'build-cli')).rstrip('/')
        binary_path = str(cfg.get('simc_path') or os.path.join(build_dir, 'simc'))
        return source_dir, build_dir, binary_path

    def _json_bool(self, value, default=False):
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in ('1', 'true', 'yes', 'y', 'on'):
            return True
        if text in ('0', 'false', 'no', 'n', 'off'):
            return False
        return default

    def _get_source_versions(self, source_dir):
        """Read canonical full SHA values for checkout and origin/midnight.

        BackendBinary versions are persisted as full git SHAs. Returning a
        short SHA here makes an up-to-date backend look stale forever.
        """
        def git_full(ref):
            result = subprocess.run(
                ['git', 'rev-parse', ref],
                cwd=source_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            value = result.stdout.strip().lower() if result.returncode == 0 else ''
            return value if re.fullmatch(r'[0-9a-f]{40}', value) else ''

        try:
            if not os.path.isdir(source_dir):
                return '', ''
            return git_full('HEAD'), git_full('refs/remotes/origin/midnight')
        except Exception:
            return '', ''

    def _get_game_version(self, backend=None):
        """Return the WoW build paired with one execution backend."""
        try:
            identity = current_validation_identity(backend=backend)
        except Exception:
            return ''
        return identity[1] if identity else ''

    def _serialize_maintenance_task(self, task):
        return {
            'id': task.pk, 'agent_id': task.agent_id, 'status': task.status,
            'requested_at': _fmt_dt(task.requested_at),
            'completed_at': _fmt_dt(task.completed_at),
            'has_error': bool(task.error),
        }

    def _serialize_backend_row(self, row, source_dir, build_dir, binary_path):
        current_hash, upstream_hash = self._get_source_versions(source_dir)
        current_version = current_hash or str(row.current_version or '').strip()
        latest_version = upstream_hash or str(row.latest_version or '').strip()
        backend_id = getattr(row, 'pk', None)
        return {
            'id': backend_id if isinstance(backend_id, int) else None,
            'platform': row.platform,
            'binary_name': os.path.basename(binary_path),
            'available': bool(binary_path and os.path.isfile(binary_path) and os.access(binary_path, os.X_OK)),
            'current_version': current_version,
            'latest_version': latest_version,
            'game_version': self._get_game_version(row),
            'need_update': bool(latest_version) and (latest_version != current_version),
            'auto_update': row.auto_update,
            'maintenance_policy': {
                'enabled': (getattr(row, 'maintenance_enabled', True)
                            if type(getattr(row, 'maintenance_enabled', True)) is bool else True),
                'policy_revision': (getattr(row, 'maintenance_policy_revision', 1)
                                    if type(getattr(row, 'maintenance_policy_revision', 1)) is int else 1),
                'timezone': 'Asia/Shanghai',
                'daily_time': (getattr(row, 'maintenance_daily_time', '03:00')
                               if isinstance(getattr(row, 'maintenance_daily_time', '03:00'), str) else '03:00'),
                'window_minutes': (getattr(row, 'maintenance_window_minutes', 60)
                                   if type(getattr(row, 'maintenance_window_minutes', 60)) is int else 60),
            },
            'is_updating': row.is_updating,
            'update_progress': row.update_progress,
            'update_status': row.update_status,
            'has_error': bool(row.last_error),
            'last_checked_at': _fmt_dt(row.last_checked_at),
            'last_updated_at': _fmt_dt(row.last_updated_at)
        }

    def get(self, request):
        try:
            can_write = request.user.is_staff
            row = SimcBackendBinary.objects.filter(identifier='production').first()
            source_dir, build_dir, binary_path = self._resolve_local_build_paths()

            if not row:
                return JsonResponse({
                    'success': True,
                    'data': {
                        'platform': self._get_runtime_platform(),
                        'binary_name': os.path.basename(binary_path),
                        'available': bool(binary_path and os.path.isfile(binary_path) and os.access(binary_path, os.X_OK)),
                        'current_version': '',
                        'latest_version': '',
                        'game_version': '',
                        'need_update': False,
                        'auto_update': True,
                        'maintenance_policy': {
                            'enabled': True, 'policy_revision': 1, 'timezone': 'Asia/Shanghai',
                            'daily_time': '03:00', 'window_minutes': 60,
                        },
                        'is_updating': False,
                        'update_progress': 0,
                        'update_status': '未初始化',
                        'has_error': False,
                        'last_checked_at': None,
                        'last_updated_at': None,
                        'can_write': can_write
                    }
                })

            data = self._serialize_backend_row(row, source_dir, build_dir, row.simc_path)
            data['can_write'] = can_write
            data['backends'] = [
                {
                    'id': backend.id,
                    'identifier': backend.identifier,
                    'name': backend.name,
                    'platform': backend.platform,
                    'version': backend.current_version,
                    'game_version': self._get_game_version(backend),
                    'available': bool(backend.simc_path and os.path.isfile(backend.simc_path) and os.access(backend.simc_path, os.X_OK)),
                    'is_default': backend.identifier == 'production',
                }
                for backend in SimcBackendBinary.objects.filter(is_active=True).order_by('id')
            ]
            return JsonResponse({
                'success': True,
                'data': data
            })
        except Exception as e:
            logger.error(f"获取SimC后端更新状态失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': '获取 SimC 后端状态失败，请稍后重试'
            }, status=500)

    def post(self, request):
        if not request.user.is_staff:
            return JsonResponse({'success': False, 'error': '仅管理员可管理 SimC 后端'}, status=403)
        try:
            from django.core.management import call_command

            data = json.loads(request.body or '{}')
            action = (data.get('action') or '').strip()
            if action not in ('set_auto_update', 'set_maintenance_schedule', 'check', 'update', 'dispatch_agent_maintenance'):
                return JsonResponse({'success': False, 'error': '不支持的后端操作'}, status=400)

            if action == 'dispatch_agent_maintenance':
                backend_id = data.get('backend_id')
                if type(backend_id) is not int or backend_id <= 0:
                    return JsonResponse({'success': False, 'error': 'backend_id 必须是正整数'}, status=400)
                agent_ids = data.get('agent_ids')
                if agent_ids is not None and (not isinstance(agent_ids, list) or any(type(value) is not int or value <= 0 for value in agent_ids)):
                    return JsonResponse({'success': False, 'error': 'agent_ids 必须是正整数数组'}, status=400)
                agents = SimcAgent.objects.filter(backend_id=backend_id, is_active=True)
                if agent_ids is not None:
                    agents = agents.filter(id__in=set(agent_ids))
                tasks = [SimcAgentMaintenanceTask(agent=agent) for agent in agents.order_by('id')]
                SimcAgentMaintenanceTask.objects.bulk_create(tasks)
                return JsonResponse({'success': True, 'data': [self._serialize_maintenance_task(task) for task in tasks]})

            runtime_platform = self._get_runtime_platform()
            row = SimcBackendBinary.objects.filter(identifier='production').first()
            if not row:
                row = SimcBackendBinary(identifier='production', name='正式服', platform=runtime_platform)
                row.simc_path = ''
                row.current_version = ''
                row.latest_version = ''
                row.auto_update = True
                row.maintenance_enabled = True
                row.maintenance_daily_time = '03:00'
                row.maintenance_window_minutes = 60
                row.maintenance_policy_revision = 1
                row.last_checked_at = None
                row.last_updated_at = None
                row.update_progress = 0
                row.update_status = '未初始化'
                row.last_error = ''
                row.is_updating = False
                row.save()

            if action == 'set_maintenance_schedule':
                enabled = self._json_bool(data.get('enabled'), True)
                daily_time = str(data.get('daily_time') or '').strip()
                if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', daily_time):
                    return JsonResponse({'success': False, 'error': 'daily_time 必须是 HH:MM（Asia/Shanghai）'}, status=400)
                try:
                    window_minutes = int(data.get('window_minutes'))
                except (TypeError, ValueError):
                    return JsonResponse({'success': False, 'error': 'window_minutes 必须是 1 到 180 的整数'}, status=400)
                if not 1 <= window_minutes <= 180:
                    return JsonResponse({'success': False, 'error': 'window_minutes 必须是 1 到 180 的整数'}, status=400)
                changed = (row.maintenance_enabled != enabled
                           or row.maintenance_daily_time != daily_time
                           or row.maintenance_window_minutes != window_minutes)
                row.maintenance_enabled = enabled
                row.maintenance_daily_time = daily_time
                row.maintenance_window_minutes = window_minutes
                if changed:
                    row.maintenance_policy_revision = int(row.maintenance_policy_revision or 0) + 1
                row.save(update_fields=[
                    'maintenance_enabled', 'maintenance_daily_time', 'maintenance_window_minutes',
                    'maintenance_policy_revision',
                ])
                source_dir, build_dir, binary_path = self._resolve_local_build_paths()
                return JsonResponse({
                    'success': True,
                    'message': '每日 SimC 维护窗口已保存（Asia/Shanghai）',
                    'data': self._serialize_backend_row(row, source_dir, build_dir, binary_path),
                })

            # 处理自动更新开关设置
            if action == 'set_auto_update':
                auto_update = self._json_bool(data.get('auto_update'), True)
                row.auto_update = auto_update
                row.save(update_fields=['auto_update'])
                source_dir, build_dir, binary_path = self._resolve_local_build_paths()
                return JsonResponse({
                    'success': True,
                    'message': f'自动更新已{"开启" if auto_update else "关闭"}',
                    'data': self._serialize_backend_row(row, source_dir, build_dir, binary_path)
                })

            try:
                threads = int(data.get('threads', 2) or 2)
            except (TypeError, ValueError):
                return JsonResponse({'success': False, 'error': 'threads 必须是 1 到 8 的整数'}, status=400)
            if not 1 <= threads <= 8:
                return JsonResponse({'success': False, 'error': 'threads 必须是 1 到 8 的整数'}, status=400)
            no_pull = self._json_bool(data.get('no_pull'), False)
            check_only = action == 'check'

            # Atomic claim prevents two requests from launching concurrent commands.
            claimed = SimcBackendBinary.objects.filter(
                pk=row.pk, is_updating=False,
            ).update(is_updating=True, update_progress=1,
                     update_status='已提交后端操作', last_error='')
            if claimed != 1:
                return JsonResponse({'success': False, 'error': '当前正在更新中，请稍后重试'}, status=409)
            row.refresh_from_db()

            if not check_only:
                row.update_status = '已提交本地编译更新'
                row.save(update_fields=['update_status'])

            def _run_update():
                from django.db import close_old_connections
                try:
                    call_command('update_simc_binary', threads=threads, no_pull=no_pull, check=check_only)
                except Exception:
                    close_old_connections()
                    err_msg = 'SimC 本地编译命令执行失败'
                    try:
                        row_inner = SimcBackendBinary.objects.filter(identifier='production').first()
                        if row_inner:
                            row_inner.is_updating = False
                            row_inner.update_status = 'SimC 本地编译失败'
                            row_inner.last_error = err_msg
                            row_inner.save(update_fields=['is_updating', 'update_status', 'last_error'])
                            upsert_system_alert('SIMC_UPDATE_FAILED', runtime_platform, 3, 'SimC 更新失败', f'本地编译失败: {err_msg}')
                    except Exception:
                        pass
                    logger.error(f"SimC本地编译失败: {err_msg}\n{traceback.format_exc()}")
                finally:
                    close_old_connections()

            if not check_only:
                t = threading.Thread(target=_run_update, daemon=True)
                t.start()
                message = '已开始本地编译更新，请稍后刷新查看进度'
            else:
                threading.Thread(target=_run_update, daemon=True).start()
                message = '已开始检查当前版本'

            return JsonResponse({'success': True, 'message': message})
        except Exception as e:
            logger.error(f"触发SimC本地编译失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({'success': False, 'error': '触发 SimC 本地编译失败'}, status=500)


@method_decorator([csrf_exempt, login_required], name='dispatch')
class WclAnalysisTaskAPIView(View):
    def dispatch(self, request, *args, **kwargs):
        if not has_dashboard_permission(request.user, 'tools.wcl-analysis'):
            return JsonResponse({'success': False, 'error': '无权访问该 Dashboard 页面。'}, status=403)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, task_id=None):
        try:
            if task_id:
                task = WclAnalysisTask.objects.filter(id=task_id, is_active=True).first()
                if not task:
                    return JsonResponse({'success': False, 'error': '任务不存在'})
                return JsonResponse({
                    'success': True,
                    'data': self._serialize_task(task, with_token=True)
                })

            limit = request.GET.get('limit', '30')
            try:
                limit = max(1, min(100, int(limit)))
            except ValueError:
                limit = 30

            tasks = WclAnalysisTask.objects.filter(is_active=True).order_by('-created_at')[:limit]
            return JsonResponse({
                'success': True,
                'data': [self._serialize_task(t, with_token=True) for t in tasks]
            })
        except Exception as e:
            logger.error(f"WCL任务查询失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({'success': False, 'error': f'查询失败: {str(e)}'})

    def post(self, request):
        try:
            data = json.loads(request.body or '{}')
            wcl_url = (data.get('wcl_url') or '').strip()
            ok, parsed = self._validate_wcl_url(wcl_url)
            if not ok:
                return JsonResponse({'success': False, 'error': parsed})

            task = WclAnalysisTask.objects.create(
                wcl_url=wcl_url,
                report_code=parsed.get('report_code'),
                fight_id=parsed.get('fight_id'),
                access_token=uuid.uuid4().hex + uuid.uuid4().hex[:8],
                status=0,
                is_active=True
            )
            threading.Thread(target=self._run_task, args=(task.id,), daemon=True).start()
            report_url = f"/wcl-analysis/report/{task.id}/?token={task.access_token}"
            return JsonResponse({
                'success': True,
                'data': {
                    'task_id': task.id,
                    'status': task.status,
                    'report_url': report_url
                }
            })
        except Exception as e:
            logger.error(f"WCL任务创建失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({'success': False, 'error': f'创建失败: {str(e)}'})

    def _serialize_task(self, task, with_token=False):
        item = {
            'id': task.id,
            'wcl_url': task.wcl_url,
            'report_code': task.report_code,
            'fight_id': task.fight_id,
            'status': task.status,
            'error_message': task.error_message,
            'summary': task.summary,
            'benchmark_unavailable': task.benchmark_unavailable,
            'report_html_file': task.report_html_file,
            'created_at': _fmt_dt(task.created_at),
            'updated_at': _fmt_dt(task.updated_at),
        }
        if with_token:
            item['report_url'] = f"/wcl-analysis/report/{task.id}/?token={task.access_token}"
        return item

    def _validate_wcl_url(self, wcl_url):
        if not wcl_url:
            return False, 'WCL链接不能为空'
        try:
            parsed = urlparse(wcl_url)
        except Exception:
            return False, 'WCL链接格式错误'
        if parsed.scheme not in ('http', 'https'):
            return False, '仅支持http/https链接'
        host = (parsed.netloc or '').lower()
        if host not in ('warcraftlogs.com', 'cn.warcraftlogs.com'):
            return False, '仅支持 warcraftlogs.com 链接'
        if '/reports/' not in (parsed.path or ''):
            return False, '链接必须包含 /reports/'
        query = parse_qs(parsed.query or '')
        fight_list = query.get('fight', [])
        if not fight_list or not str(fight_list[0]).strip():
            return False, '链接必须包含 fight 参数'
        report_code = (parsed.path.split('/reports/', 1)[1] if '/reports/' in parsed.path else '').split('/')[0].strip()
        if not report_code:
            return False, '无法解析 report_code'
        return True, {
            'report_code': report_code,
            'fight_id': str(fight_list[0]).strip()
        }

    def _run_task(self, task_id):
        task = WclAnalysisTask.objects.filter(id=task_id).first()
        if not task:
            return
        try:
            task.status = 1
            task.error_message = None
            task.save(update_fields=['status', 'error_message', 'updated_at'])
            logger.info(f"WCL任务开始[{task_id}]")

            logger.info(f"WCL任务抓取阶段开始[{task_id}]")
            battle_data = self._fetch_wcl_battle_data(task.wcl_url, task.report_code, task.fight_id)
            self._validate_battle_data_or_raise(battle_data)
            WclAnalysisTask.objects.filter(id=task_id).update(updated_at=timezone.now())
            logger.info(f"WCL任务抓取阶段完成[{task_id}]")
            logger.info(f"WCL任务横向对比阶段开始[{task_id}]")
            benchmark_summary, benchmark_unavailable = self._fetch_benchmark_summary(battle_data)
            WclAnalysisTask.objects.filter(id=task_id).update(updated_at=timezone.now())
            logger.info(f"WCL任务横向对比阶段完成[{task_id}]")
            logger.info(f"WCL任务模型分析阶段开始[{task_id}]")
            prompt_content = self._build_prompt_content(task.wcl_url, battle_data, benchmark_summary)
            html_content, summary = self._call_glm_report_html(prompt_content, task.wcl_url, battle_data, task)
            if not html_content:
                extra = f"：{summary}" if summary else ""
                raise Exception(f'GLM未返回可用HTML，任务按严格GLM直出模式失败{extra}')
            WclAnalysisTask.objects.filter(id=task_id).update(updated_at=timezone.now())
            logger.info(f"WCL任务模型分析阶段完成[{task_id}]")
            logger.info(f"WCL任务渲染阶段开始[{task_id}]")

            report_dir = os.path.join(settings.BASE_DIR, 'static', 'wcl_reports')
            snap_dir = os.path.join(settings.BASE_DIR, 'static', 'wcl_snapshots')
            os.makedirs(report_dir, exist_ok=True)
            os.makedirs(snap_dir, exist_ok=True)

            report_file = f"wcl_report_{task.id}_{int(time.time())}.html"
            snap_file = f"wcl_snapshot_{task.id}_{int(time.time())}.json"
            with open(os.path.join(report_dir, report_file), 'w', encoding='utf-8') as f:
                f.write(html_content)
            with open(os.path.join(snap_dir, snap_file), 'w', encoding='utf-8') as f:
                f.write(json.dumps({
                    'wcl_url': task.wcl_url,
                    'battle_data': battle_data,
                    'benchmark_summary': benchmark_summary
                }, ensure_ascii=False, indent=2))

            task.status = 2
            task.report_html_file = report_file
            task.source_snapshot_file = snap_file
            task.summary = summary[:1000] if summary else ''
            task.benchmark_unavailable = benchmark_unavailable
            task.error_message = None
            task.save(update_fields=[
                'status', 'report_html_file', 'source_snapshot_file', 'summary',
                'benchmark_unavailable', 'error_message', 'updated_at'
            ])
            logger.info(f"WCL任务完成[{task_id}]")
        except Exception as e:
            logger.error(f"WCL任务执行失败[{task_id}]: {str(e)}\n{traceback.format_exc()}")
            WclAnalysisTask.objects.filter(id=task_id).update(
                status=3,
                error_message=str(e)[:1000],
                updated_at=timezone.now()
            )

    def _validate_battle_data_or_raise(self, battle_data):
        players = battle_data.get('players') or []
        fights = battle_data.get('fights') or []
        selected_fight = battle_data.get('selected_fight') or {}
        if not selected_fight:
            raise Exception('WCL v2 API未返回目标fight数据，请确认report_code与fight参数')
        if not players:
            raise Exception('WCL v2 API未返回玩家列表，请检查报告访问权限或API授权范围')
        if not fights:
            raise Exception('WCL v2 API未返回fights列表，请检查报告是否可访问')

    def _fetch_wcl_battle_data(self, wcl_url, report_code, fight_id):
        api_data = self._fetch_wcl_battle_data_via_api(report_code, fight_id)
        if not api_data:
            raise Exception('WCL v2 API调用失败，请检查WCL_V2_CONFIG(client_id/client_secret)与报告访问权限')
        api_data['wcl_url'] = wcl_url
        return api_data

    def _fetch_wcl_battle_data_via_api(self, report_code, fight_id):
        token = self._get_wcl_access_token()
        if not token:
            return None
        report = self._wcl_query_report_overview(token, report_code)
        if not report:
            return None

        fights = report.get('fights') or []
        selected_fight = None
        for f in fights:
            if str(f.get('id')) == str(fight_id):
                selected_fight = f
                break
        if selected_fight is None and fights:
            selected_fight = fights[0]
        selected_fight_id = int(selected_fight.get('id')) if selected_fight and selected_fight.get('id') is not None else int(fight_id)

        damage_table = self._wcl_query_table(token, report_code, selected_fight_id, 'DamageDone')
        healing_table = self._wcl_query_table(token, report_code, selected_fight_id, 'Healing')
        damage_taken_table = self._wcl_query_table(token, report_code, selected_fight_id, 'DamageTaken')
        casts_table = self._wcl_query_table(token, report_code, selected_fight_id, 'Casts')
        interrupts_table = self._wcl_query_table(token, report_code, selected_fight_id, 'Interrupts')
        dispels_table = self._wcl_query_table(token, report_code, selected_fight_id, 'Dispels')
        deaths_events = self._wcl_query_events(token, report_code, selected_fight_id, 'Deaths')
        rankings_data = self._wcl_query_rankings(token, report_code, selected_fight_id)

        players = self._build_players_from_tables(
            damage_table=damage_table,
            healing_table=healing_table,
            damage_taken_table=damage_taken_table,
            casts_table=casts_table,
            interrupts_table=interrupts_table,
            dispels_table=dispels_table
        )
        interrupt_actor_entries = self._extract_actor_totals_from_spell_detail_table(interrupts_table)
        control_actor_entries = self._extract_actor_totals_from_spell_detail_table(dispels_table)

        title = report.get('title') or ''
        dungeon_name = ''
        keystone_level = None
        m = re.search(r'Mythic\+\s*([A-Za-z\'\-\s]+)\s*[-|,]', title, re.IGNORECASE)
        if m:
            dungeon_name = m.group(1).strip()
        for text_source in [str((selected_fight or {}).get('name') or ''), title]:
            km = re.search(r'(?:(?:\+|Level\s*)(\d{1,2})|(\d{1,2})\s*层)', text_source, re.IGNORECASE)
            if km:
                try:
                    keystone_level = int(km.group(1) or km.group(2))
                    break
                except Exception:
                    pass
        if isinstance(rankings_data, list) and rankings_data:
            r0 = rankings_data[0] or {}
            encounter = r0.get('encounter') or {}
            if not dungeon_name:
                dungeon_name = encounter.get('name') or dungeon_name
            bd = r0.get('bracketData')
            try:
                bd_int = int(bd)
                if bd_int > 0:
                    keystone_level = bd_int
            except Exception:
                pass

        api_functions_status = {
            'query_report_overview': bool(report),
            'query_table_damage_done': bool(damage_table),
            'query_table_healing': bool(healing_table),
            'query_table_damage_taken': bool(damage_taken_table),
            'query_table_casts': bool(casts_table),
            'query_table_interrupts': bool(interrupts_table),
            'query_table_dispels': bool(dispels_table),
            'query_events_deaths': bool(deaths_events),
            'query_rankings': bool(rankings_data)
        }

        return {
            'source': 'wcl_api',
            'wcl_url': f"https://www.warcraftlogs.com/reports/{report_code}?fight={fight_id}",
            'report_code': report_code,
            'fight_id': str(fight_id),
            'title': title,
            'dungeon_name': dungeon_name,
            'keystone_level': keystone_level,
            'players': players,
            'fights': fights[:80],
            'selected_fight': selected_fight or {},
            'events_text': json.dumps({
                'selected_fight': selected_fight or {},
                'deaths_events': deaths_events[:200] if isinstance(deaths_events, list) else deaths_events,
                'rankings_sample': rankings_data[:3] if isinstance(rankings_data, list) else rankings_data
            }, ensure_ascii=False),
            'tables': {
                'damage_done': damage_table,
                'healing': healing_table,
                'damage_taken': damage_taken_table,
                'casts': casts_table,
                'interrupts': {'entries': interrupt_actor_entries},
                'controls': {'entries': control_actor_entries}
            },
            'script_data_snippets': [],
            'api_functions_status': api_functions_status,
            'raw_excerpt': json.dumps({
                'report': report,
                'damage_table': damage_table,
                'healing_table': healing_table,
                'damage_taken_table': damage_taken_table,
                'casts_table': casts_table,
                'interrupts_table': interrupts_table,
                'dispels_table': dispels_table,
                'deaths_events': deaths_events,
                'rankings': rankings_data
            }, ensure_ascii=False)[:180000]
        }

    def _wcl_query_report_overview(self, token, report_code):
        query = """
        query($code: String!) {
          reportData {
            report(code: $code) {
              title
              startTime
              endTime
              fights {
                id
                name
                startTime
                endTime
                kill
              }
            }
          }
        }
        """
        payload = self._wcl_graphql(token, query, {"code": report_code})
        return (((payload or {}).get('data') or {}).get('reportData') or {}).get('report')

    def _wcl_query_table(self, token, report_code, fight_id, data_type):
        query = f"""
        query($code: String!, $fid: Int!) {{
          reportData {{
            report(code: $code) {{
              table(dataType: {data_type}, fightIDs: [$fid])
            }}
          }}
        }}
        """
        try:
            payload = self._wcl_graphql(token, query, {"code": report_code, "fid": int(fight_id)})
            report = (((payload or {}).get('data') or {}).get('reportData') or {}).get('report') or {}
            return (report.get('table') or {}).get('data') or {}
        except Exception as e:
            logger.warning(f"WCL table {data_type} 查询失败: {str(e)}")
            return {}

    def _wcl_query_events(self, token, report_code, fight_id, data_type):
        query = f"""
        query($code: String!, $fid: Int!) {{
          reportData {{
            report(code: $code) {{
              events(dataType: {data_type}, fightIDs: [$fid]) {{
                data
                nextPageTimestamp
              }}
            }}
          }}
        }}
        """
        try:
            payload = self._wcl_graphql(token, query, {"code": report_code, "fid": int(fight_id)})
            report = (((payload or {}).get('data') or {}).get('reportData') or {}).get('report') or {}
            return (report.get('events') or {}).get('data') or []
        except Exception as e:
            logger.warning(f"WCL events {data_type} 查询失败: {str(e)}")
            return []

    def _wcl_query_rankings(self, token, report_code, fight_id):
        query = """
        query($code: String!, $fid: Int!) {
          reportData {
            report(code: $code) {
              rankings(fightIDs: [$fid])
            }
          }
        }
        """
        try:
            payload = self._wcl_graphql(token, query, {"code": report_code, "fid": int(fight_id)})
            report = (((payload or {}).get('data') or {}).get('reportData') or {}).get('report') or {}
            return (report.get('rankings') or {}).get('data') or []
        except Exception as e:
            logger.warning(f"WCL rankings 查询失败: {str(e)}")
            return []

    def _extract_actor_totals_from_spell_detail_table(self, table_data):
        actor_map = {}
        entries = (table_data or {}).get('entries') or []
        normalized_entries = []
        for row in entries:
            if isinstance(row, dict) and isinstance(row.get('entries'), list) and not row.get('name'):
                normalized_entries.extend(row.get('entries') or [])
            else:
                normalized_entries.append(row)
        entries = normalized_entries
        for spell_row in entries:
            if not isinstance(spell_row, dict):
                continue
            for d in (spell_row.get('details') or []):
                if not isinstance(d, dict):
                    continue
                name = d.get('name') or ''
                if not name:
                    continue
                pid = d.get('id')
                key = f"{pid}:{name}"
                if key not in actor_map:
                    actor_map[key] = {
                        'id': pid,
                        'name': name,
                        'type': d.get('type') or '',
                        'icon': d.get('icon') or '',
                        'total': 0
                    }
                actor_map[key]['total'] += int(d.get('total') or 0)
        rows = list(actor_map.values())
        rows.sort(key=lambda x: x.get('total', 0), reverse=True)
        return rows

    def _build_players_from_tables(self, damage_table, healing_table, damage_taken_table, casts_table, interrupts_table, dispels_table):
        players_map = {}
        table_pairs = [
            ('damage', damage_table),
            ('healing', healing_table),
            ('damage_taken', damage_taken_table),
            ('casts', casts_table),
        ]
        for metric, table_data in table_pairs:
            entries = (table_data or {}).get('entries') or []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                name = e.get('name') or ''
                if not name:
                    continue
                pid = e.get('id')
                key = f"{pid}:{name}"
                if key not in players_map:
                    players_map[key] = {
                        'id': pid,
                        'name': name,
                        'class': e.get('type') or '',
                        'spec': e.get('icon') or '',
                        'damage': 0,
                        'healing': 0,
                        'damage_taken': 0,
                        'casts': 0,
                        'interrupts': 0,
                        'controls': 0
                    }
                if metric == 'damage':
                    players_map[key]['damage'] = e.get('total', 0) or 0
                elif metric == 'healing':
                    players_map[key]['healing'] = e.get('total', 0) or 0
                elif metric == 'damage_taken':
                    players_map[key]['damage_taken'] = e.get('total', 0) or 0
                elif metric == 'casts':
                    players_map[key]['casts'] = e.get('total', 0) or 0

        interrupt_rows = self._extract_actor_totals_from_spell_detail_table(interrupts_table)
        for row in interrupt_rows:
            key = f"{row.get('id')}:{row.get('name')}"
            if key not in players_map:
                players_map[key] = {
                    'id': row.get('id'),
                    'name': row.get('name'),
                    'class': row.get('type') or '',
                    'spec': row.get('icon') or '',
                    'damage': 0,
                    'healing': 0,
                    'damage_taken': 0,
                    'casts': 0,
                    'interrupts': 0,
                    'controls': 0
                }
            players_map[key]['interrupts'] = row.get('total', 0) or 0

        control_rows = self._extract_actor_totals_from_spell_detail_table(dispels_table)
        for row in control_rows:
            key = f"{row.get('id')}:{row.get('name')}"
            if key not in players_map:
                players_map[key] = {
                    'id': row.get('id'),
                    'name': row.get('name'),
                    'class': row.get('type') or '',
                    'spec': row.get('icon') or '',
                    'damage': 0,
                    'healing': 0,
                    'damage_taken': 0,
                    'casts': 0,
                    'interrupts': 0,
                    'controls': 0
                }
            players_map[key]['controls'] = row.get('total', 0) or 0
        players = list(players_map.values())
        players.sort(key=lambda x: x.get('damage', 0), reverse=True)
        return players[:20]

    def _get_wcl_api_credentials(self):
        cfg = getattr(settings, 'WCL_V2_CONFIG', {}) or getattr(settings, 'WCL_API_CONFIG', {}) or {}
        client_id = cfg.get('client_id') or os.getenv('WCL_CLIENT_ID')
        client_secret = cfg.get('client_secret') or os.getenv('WCL_CLIENT_SECRET')
        if not client_id or not client_secret:
            return None, None
        return client_id, client_secret

    def _get_wcl_access_token(self):
        client_id, client_secret = self._get_wcl_api_credentials()
        if not client_id or not client_secret:
            return None
        token_url = "https://www.warcraftlogs.com/oauth/token"
        try:
            resp = requests.post(
                token_url,
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                timeout=20
            )
            if resp.status_code != 200:
                logger.warning(f"WCL OAuth失败: HTTP {resp.status_code}")
                return None
            data = resp.json()
            return data.get('access_token')
        except Exception as e:
            logger.warning(f"WCL OAuth请求失败: {str(e)}")
            return None

    def _wcl_graphql(self, token, query, variables):
        url = "https://www.warcraftlogs.com/api/v2/client"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        resp = requests.post(url, json={"query": query, "variables": variables}, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise Exception(f"WCL GraphQL HTTP {resp.status_code}")
        payload = resp.json()
        if payload.get('errors'):
            raise Exception(f"WCL GraphQL错误: {payload.get('errors')}")
        return payload

    def _fetch_benchmark_summary(self, battle_data):
        dungeon_name = (battle_data.get('dungeon_name') or '').strip()
        level = battle_data.get('keystone_level')
        selected_fight = battle_data.get('selected_fight') or {}
        players = battle_data.get('players') or []
        summary = {
            'sample_size': 0,
            'scope': 'WCL v2 API基线（当前报告与fight维度）',
            'top_times': [],
            'deaths': [],
            'interrupts': [],
            'records_raw': [],
            'search_url': '',
            'benchmark_source': 'wcl_v2_api'
        }
        if selected_fight:
            fight_seconds = max(1, int((selected_fight.get('endTime', 0) - selected_fight.get('startTime', 0)) / 1000))
            mm, ss = divmod(fight_seconds, 60)
            summary['top_times'] = [f"{mm}:{ss:02d}"]
            summary['records_raw'].append({
                'title': selected_fight.get('name') or dungeon_name or 'Fight',
                'time': f"{mm}:{ss:02d}",
                'kill': bool(selected_fight.get('kill'))
            })

        summary['sample_size'] = 1 if selected_fight else 0
        if dungeon_name or level:
            summary['note'] = f'已通过WCL v2 API读取当前fight基线：副本={dungeon_name or "未知"}，层数={level if level is not None else "未知"}，玩家数={len(players)}。'
            return summary, False

        summary['note'] = 'WCL v2 API已读取fight数据，但副本名或层数缺失，无法构建同层横向基线。'
        return summary, True

    def _build_prompt_content(self, wcl_url, battle_data, benchmark_summary):
        prompt_file = os.path.join(settings.BASE_DIR, 'core', 'prompts', 'wcl_report_prompt.txt')
        if os.path.exists(prompt_file):
            with open(prompt_file, 'r', encoding='utf-8') as f:
                template = f.read()
        else:
            template = (
                "你是一名魔兽世界大秘境复盘分析师，请输出分析文本。\n"
                "输入URL: {{WCL_URL}}\n"
                "战斗数据JSON:\n{{BATTLE_DATA_JSON}}\n"
                "榜单基准JSON:\n{{BENCHMARK_JSON}}\n"
                "请按标题输出：战斗总览、横向差距、关键失败点、玩家复盘、责任排序、优先修复项、最终结论。"
            )

        compact_battle = self._build_prompt_battle_data(battle_data, tight=False)
        compact_benchmark = self._build_prompt_benchmark_data(benchmark_summary)
        return (template
                .replace('{{WCL_URL}}', wcl_url)
                .replace('{{BATTLE_DATA_JSON}}', json.dumps(compact_battle, ensure_ascii=False))
                .replace('{{BENCHMARK_JSON}}', json.dumps(compact_benchmark, ensure_ascii=False)))

    def _build_html_prompt_content(self, wcl_url, battle_data, tight=False):
        prompt_file = os.path.join(settings.BASE_DIR, 'core', 'prompts', 'wcl_report_html_prompt.txt')
        if os.path.exists(prompt_file):
            with open(prompt_file, 'r', encoding='utf-8') as f:
                template = f.read()
        else:
            template = (
                "你是资深前端设计师与魔兽大秘境分析师。\n"
                "请输出一个完整、可直接打开的HTML文档（<!DOCTYPE html> 开始）。\n"
                "要求：\n"
                "1) 只输出HTML，不要markdown代码块，不要解释。\n"
                "2) 页面风格专业、现代、可读性强。\n"
                "3) 页面必须包含：战斗总览、横向差距、关键失败点、玩家复盘、责任排序、优先修复项、最终结论。\n"
                "4) 支持markdown内容渲染（可用marked+DOMPurify CDN）。\n"
                "5) 基于输入数据生成可视化图表（可用Chart.js CDN）。\n"
                "输入URL:\n{{WCL_URL}}\n"
                "战斗数据(JSON):\n{{BATTLE_DATA_JSON}}\n"
            )
        compact_battle = self._build_prompt_battle_data(battle_data, tight=tight)
        return (template
                .replace('{{WCL_URL}}', wcl_url)
                .replace('{{BATTLE_DATA_JSON}}', json.dumps(compact_battle, ensure_ascii=False)))

    def _build_prompt_benchmark_data(self, benchmark_summary):
        summary = benchmark_summary or {}
        return {
            'sample_size': summary.get('sample_size', 0),
            'scope': summary.get('scope', ''),
            'top_times': (summary.get('top_times') or [])[:8],
            'deaths': (summary.get('deaths') or [])[:20],
            'interrupts': (summary.get('interrupts') or [])[:20],
            'records_raw': (summary.get('records_raw') or [])[:6],
            'benchmark_source': summary.get('benchmark_source', ''),
            'note': summary.get('note', '')
        }

    def _build_prompt_battle_data(self, battle_data, tight=False):
        data = battle_data or {}
        top_n = 8 if tight else 12
        players = []
        for p in (data.get('players') or [])[:top_n]:
            if not isinstance(p, dict):
                continue
            players.append({
                'id': p.get('id'),
                'name': p.get('name'),
                'class': p.get('class'),
                'spec': p.get('spec'),
                'damage': p.get('damage'),
                'healing': p.get('healing'),
                'damage_taken': p.get('damage_taken'),
                'interrupts': p.get('interrupts'),
                'casts': p.get('casts'),
                'controls': p.get('controls')
            })

        fights = []
        for f in (data.get('fights') or [])[:20]:
            if not isinstance(f, dict):
                continue
            fights.append({
                'id': f.get('id'),
                'name': f.get('name'),
                'kill': f.get('kill'),
                'startTime': f.get('startTime'),
                'endTime': f.get('endTime')
            })

        selected = data.get('selected_fight') or {}
        selected_fight = {
            'id': selected.get('id'),
            'name': selected.get('name'),
            'kill': selected.get('kill'),
            'startTime': selected.get('startTime'),
            'endTime': selected.get('endTime')
        }

        tables = {}
        for key in ['damage_done', 'healing', 'damage_taken', 'interrupts', 'casts', 'controls']:
            table = ((data.get('tables') or {}).get(key) or {})
            entries = []
            for e in (table.get('entries') or [])[:top_n]:
                if not isinstance(e, dict):
                    continue
                entries.append({
                    'id': e.get('id'),
                    'name': e.get('name'),
                    'type': e.get('type'),
                    'icon': e.get('icon'),
                    'total': e.get('total')
                })
            tables[key] = {'entries': entries}

        payload = {
            'source': data.get('source'),
            'report_code': data.get('report_code'),
            'fight_id': data.get('fight_id'),
            'title': data.get('title'),
            'dungeon_name': data.get('dungeon_name'),
            'keystone_level': data.get('keystone_level'),
            'api_functions_status': data.get('api_functions_status') or {},
            'players': players,
            'fights': fights,
            'selected_fight': selected_fight,
            'tables': tables,
        }
        if not tight:
            payload['events_text'] = str(data.get('events_text') or '')[:8000]
            payload['raw_excerpt'] = str(data.get('raw_excerpt') or '')[:6000]
        return payload

    def _call_glm_report_html(self, prompt_content, wcl_url, battle_data, task):
        glm = GLMClient()
        html_prompt = self._build_html_prompt_content(wcl_url, battle_data, tight=False)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_local_battle_context",
                    "description": "获取已抓取的WCL v2 API战斗上下文，不新增网络请求",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "可选字段名数组，如 players,tables,events_text,raw_excerpt,fights,selected_fight,api_functions_status"
                            }
                        }
                    }
                }
            }
        ]

        def tool_handler(name, args):
            if name == 'get_local_battle_context':
                return self._tool_get_local_battle_context(battle_data, args.get('fields'))
            return {"error": f"unknown tool: {name}"}

        raw = None
        try:
            raw = glm.send_message_with_tools(html_prompt, tools, tool_handler)
        except Exception as e:
            if not self._is_prompt_too_long_error(e):
                raise
        if not raw:
            try:
                raw = glm.send_message(html_prompt)
            except Exception as e:
                if not self._is_prompt_too_long_error(e):
                    raise
        if not raw:
            compact_prompt = self._build_html_prompt_content(wcl_url, battle_data, tight=True)
            raw = glm.send_message(compact_prompt)
        html_doc = None
        if raw:
            html_doc = self._extract_html_document(raw, task)
            if not html_doc:
                html_doc = self._retry_convert_to_html(glm, raw, wcl_url, battle_data, task)
            if html_doc and self._is_html_incomplete(html_doc):
                continue_prompt = self._build_html_prompt_content(wcl_url, battle_data, tight=True)
                html_doc = self._continue_generate_html(glm, continue_prompt, html_doc, task)
            if html_doc:
                html_doc = self._normalize_html_document(html_doc)
                if self._is_html_incomplete(html_doc):
                    html_doc = self._force_regenerate_html(glm, wcl_url, battle_data, task)
        if not html_doc:
            sections = self._call_glm_analysis_text(prompt_content, wcl_url, battle_data)
            plain_text = self._sections_to_plain_text(sections)
            html_doc = self._retry_convert_to_html(glm, plain_text, wcl_url, battle_data, task)
        if not html_doc or self._is_html_incomplete(html_doc):
            err = str(getattr(glm, 'last_error', '') or '')[:220]
            return None, err
        summary = self._extract_summary_from_html(html_doc)[:180]
        return html_doc, summary

    def _retry_convert_to_html(self, glm, raw_text, wcl_url, battle_data, task):
        if not raw_text:
            return None
        repair_prompt = (
            self._build_html_prompt_content(wcl_url, battle_data, tight=True) +
            "\n\n你刚才返回了非HTML内容。请把下面内容重构为完整HTML页面，必须从<!DOCTYPE html>开始并闭合到</html>，只输出HTML：\n" +
            str(raw_text)[:12000]
        )
        retry = glm.send_message(repair_prompt)
        if not retry:
            return None
        html_doc = self._extract_html_document(retry, task)
        if html_doc:
            html_doc = self._normalize_html_document(html_doc)
        return html_doc

    def _force_regenerate_html(self, glm, wcl_url, battle_data, task):
        prompt = self._build_html_prompt_content(wcl_url, battle_data, tight=True) + "\n\n重新从头生成完整可用HTML，不要续写。"
        for _ in range(2):
            raw = glm.send_message(prompt)
            if not raw:
                continue
            html_doc = self._extract_html_document(raw, task)
            if not html_doc:
                continue
            html_doc = self._normalize_html_document(html_doc)
            if not self._is_html_incomplete(html_doc):
                return html_doc
        return None

    def _sections_to_plain_text(self, sections):
        s = sections or {}
        ordered = [
            ('战斗总览', s.get('overview')),
            ('横向差距', s.get('benchmark_gap')),
            ('关键失败点', s.get('key_failures')),
            ('玩家复盘', s.get('player_analysis')),
            ('责任排序', s.get('blame_ranking')),
            ('优先修复项', s.get('priority_fixes')),
            ('最终结论', s.get('final_verdict')),
        ]
        chunks = []
        for title, content in ordered:
            c = str(content or '').strip()
            if not c:
                continue
            chunks.append(f"{title}\n{c}")
        return "\n\n".join(chunks)

    def _extract_html_document(self, text, task):
        raw = (text or '').strip()
        if not raw:
            return None
        fence = re.search(r'```(?:html)?\s*([\s\S]*?)```', raw, re.IGNORECASE)
        if fence:
            raw = fence.group(1).strip()
        raw = self._strip_markdown_fences(raw)
        if '<html' in raw.lower():
            return raw
        if '<body' in raw.lower() or '<div' in raw.lower():
            return (
                "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
                f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"><title>WCL战斗分析报告 #{task.id}</title>"
                "</head><body>" + raw + "</body></html>"
            )
        return None

    def _is_html_incomplete(self, html_doc):
        text = (html_doc or '').lower()
        if not text:
            return True
        if '```' in text:
            return True
        if '</html>' not in text:
            return True
        if '</body>' not in text:
            return True
        if text.count('<style') != text.count('</style>'):
            return True
        if text.count('<script') != text.count('</script>'):
            return True
        if self._has_broken_css_block(html_doc):
            return True
        if ('margin-bottom' in text and 'margin-bottom:' in text and ';' not in text[text.rfind('margin-bottom'):text.rfind('margin-bottom') + 40]):
            return True
        required_blocks = ['战斗总览', '关键失败点', '玩家复盘', '最终结论']
        if sum(1 for b in required_blocks if b in text) < 2:
            return True
        return False

    def _continue_generate_html(self, glm, base_prompt, current_html, task, rounds=3):
        merged = current_html or ''
        for _ in range(rounds):
            if not self._is_html_incomplete(merged):
                break
            tail = merged[-1800:]
            continue_prompt = (
                base_prompt +
                "\n\n你上一次输出被截断。下面是已输出HTML末尾片段，请从该位置继续输出剩余HTML，直到完整闭合到</html>。"
                "只输出续写部分，不要重复，不要解释，不要markdown代码块：\n" + tail
            )
            chunk = glm.send_message(continue_prompt)
            if not chunk:
                break
            chunk = (chunk or '').strip()
            fence = re.search(r'```(?:html)?\s*([\s\S]*?)```', chunk, re.IGNORECASE)
            if fence:
                chunk = fence.group(1).strip()
            if '<html' in chunk.lower():
                merged = chunk
            else:
                merged += "\n" + chunk
            merged_doc = self._extract_html_document(merged, task)
            if merged_doc:
                merged = merged_doc
        return merged

    def _normalize_html_document(self, html_doc):
        text = self._strip_markdown_fences((html_doc or '').strip())
        if not text:
            return text
        if '<html' in text.lower() and '</body>' not in text.lower():
            text += '\n</body>'
        if '<html' in text.lower() and '</html>' not in text.lower():
            text += '\n</html>'
        if '<!doctype' not in text.lower():
            text = '<!DOCTYPE html>\n' + text
        return text

    def _strip_markdown_fences(self, text):
        if not text:
            return text
        t = str(text)
        t = re.sub(r'^\s*```(?:html)?\s*$', '', t, flags=re.IGNORECASE | re.MULTILINE)
        t = re.sub(r'^\s*```\s*$', '', t, flags=re.MULTILINE)
        return t

    def _has_broken_css_block(self, html_doc):
        try:
            styles = re.findall(r'<style[^>]*>([\s\S]*?)</style>', html_doc or '', flags=re.IGNORECASE)
            if not styles:
                return False
            for css in styles:
                for ln in css.splitlines():
                    s = ln.strip()
                    if not s:
                        continue
                    if s.startswith('/*') or s.endswith('*/'):
                        continue
                    if s in ('{', '}'):
                        continue
                    if s.endswith('{') or s.endswith('}'):
                        continue
                    if ':' not in s and not s.startswith('@') and not s.startswith('--'):
                        return True
            return False
        except Exception:
            return False

    def _extract_summary_from_html(self, html_doc):
        text = re.sub(r'<script[\s\S]*?</script>', ' ', html_doc, flags=re.IGNORECASE)
        text = re.sub(r'<style[\s\S]*?</style>', ' ', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:400]

    def _is_prompt_too_long_error(self, e):
        msg = str(e or '')
        return ('Prompt exceeds max length' in msg) or ('context' in msg.lower() and 'exceed' in msg.lower())

    def _call_glm_analysis_text(self, prompt_content, wcl_url, battle_data):
        glm = GLMClient()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_local_battle_context",
                    "description": "获取已抓取的WCL v2 API战斗上下文，不新增网络请求",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "可选字段名数组，如 players,tables,events_text,raw_excerpt,fights,selected_fight,api_functions_status"
                            }
                        }
                    }
                }
            }
        ]

        def tool_handler(name, args):
            if name == 'get_local_battle_context':
                return self._tool_get_local_battle_context(battle_data, args.get('fields'))
            return {"error": f"unknown tool: {name}"}

        raw = None
        try:
            raw = glm.send_message_with_tools(prompt_content, tools, tool_handler)
        except Exception as e:
            if not self._is_prompt_too_long_error(e):
                raise
        if not raw:
            try:
                raw = glm.send_message(prompt_content)
            except Exception as e:
                if not self._is_prompt_too_long_error(e):
                    raise
        if not raw:
            compact_prompt = self._build_html_prompt_content(wcl_url, battle_data, tight=True) + "\n请只输出分析文本，不要输出HTML。"
            raw = glm.send_message(compact_prompt)
        if not raw:
            raise Exception('GLM未返回内容')
        sections = self._split_llm_sections(raw)
        if self._is_analysis_incomplete(sections):
            continuation_prompt = (
                prompt_content +
                "\n\n你上一次输出被截断。请只补全缺失章节，不要重复已输出内容。"
                "至少补全：责任排序、优先修复项、最终结论。"
            )
            more = glm.send_message(continuation_prompt)
            if more:
                sections = self._merge_sections(sections, self._split_llm_sections(more))
        return sections

    def _tool_get_local_battle_context(self, battle_data, fields):
        allowed = ['players', 'tables', 'events_text', 'raw_excerpt', 'title', 'dungeon_name', 'keystone_level', 'fights', 'selected_fight', 'source']
        if not fields or not isinstance(fields, list):
            fields = allowed
        compact = self._build_prompt_battle_data(battle_data, tight=True)
        result = {}
        for f in fields:
            if f in allowed:
                if f == 'raw_excerpt':
                    result[f] = str((battle_data or {}).get('raw_excerpt') or '')[:4000]
                elif f == 'events_text':
                    result[f] = str((battle_data or {}).get('events_text') or '')[:6000]
                elif f in compact:
                    result[f] = compact.get(f)
                else:
                    result[f] = (battle_data or {}).get(f)
        return result

    def _split_llm_sections(self, text):
        raw = (text or '').strip()
        sections = {
            'overview': '',
            'benchmark_gap': '',
            'key_failures': '',
            'player_analysis': '',
            'blame_ranking': '',
            'priority_fixes': '',
            'final_verdict': '',
            'raw_text': raw
        }
        if not raw:
            return sections
        title_map = {
            '战斗总览': 'overview',
            '总体复盘': 'overview',
            '总览': 'overview',
            '横向差距': 'benchmark_gap',
            '对比差距': 'benchmark_gap',
            '基线差距': 'benchmark_gap',
            '关键失败点': 'key_failures',
            '核心问题': 'key_failures',
            '主要问题': 'key_failures',
            '玩家复盘': 'player_analysis',
            '逐人复盘': 'player_analysis',
            '逐个分析': 'player_analysis',
            '责任排序': 'blame_ranking',
            '责任归因': 'blame_ranking',
            '优先修复项': 'priority_fixes',
            '优先改进': 'priority_fixes',
            '最终结论': 'final_verdict',
            '最终总结': 'final_verdict',
            '结论': 'final_verdict'
        }
        line_starts = [0]
        for m in re.finditer(r'\n', raw):
            line_starts.append(m.end())
        hits = []
        for start in line_starts:
            end = raw.find('\n', start)
            if end == -1:
                end = len(raw)
            line = raw[start:end].strip()
            normalized = re.sub(r'^[#\-\*\d\.\s]+', '', line)
            normalized = re.sub(r'[:：\s]+$', '', normalized)
            for k, key in title_map.items():
                if normalized.startswith(k):
                    hits.append((start, end, key))
                    break
        if not hits:
            sections['overview'] = raw
            sections['final_verdict'] = raw[:600]
            return sections
        dedup = []
        seen_start = set()
        for h in sorted(hits, key=lambda x: x[0]):
            if h[0] in seen_start:
                continue
            seen_start.add(h[0])
            dedup.append(h)
        hits = dedup
        for i, (start, end, key) in enumerate(hits):
            next_start = hits[i + 1][0] if i + 1 < len(hits) else len(raw)
            content = raw[end:next_start].strip()
            if not sections[key]:
                sections[key] = content
        if not sections['final_verdict']:
            sections['final_verdict'] = (sections.get('overview') or raw)[:600]
        if not sections['overview']:
            sections['overview'] = raw[:1000]
        return sections

    def _is_analysis_incomplete(self, sections):
        if not sections:
            return True
        required = ['overview', 'key_failures', 'player_analysis', 'final_verdict']
        for k in required:
            if not str(sections.get(k) or '').strip():
                return True
        return False

    def _merge_sections(self, base_sections, extra_sections):
        merged = dict(base_sections or {})
        extra = extra_sections or {}
        for k in ['overview', 'benchmark_gap', 'key_failures', 'player_analysis', 'blame_ranking', 'priority_fixes', 'final_verdict']:
            if not str(merged.get(k) or '').strip() and str(extra.get(k) or '').strip():
                merged[k] = extra.get(k)
            elif str(extra.get(k) or '').strip() and len(str(merged.get(k) or '')) < 120:
                merged[k] = (str(merged.get(k) or '').strip() + "\n" + str(extra.get(k) or '').strip()).strip()
        merged['raw_text'] = (str(base_sections.get('raw_text') or '') + "\n" + str(extra.get('raw_text') or '')).strip()
        return merged

    def _render_report_html(self, task, llm_sections, battle_data, benchmark_summary, benchmark_unavailable):
        summary = str(llm_sections.get('final_verdict') or llm_sections.get('overview') or '')[:180]
        html_content = render_to_string('wcl_report_content.html', {
            'task': task,
            'llm': llm_sections,
            'battle_data': battle_data,
            'benchmark_summary': benchmark_summary,
            'benchmark_unavailable': benchmark_unavailable,
            'benchmark_pretty': json.dumps(benchmark_summary, ensure_ascii=False, indent=2)
        })
        return html_content, summary


# Benchmark Dashboard APIs intentionally use a small, consistent and safe JSON
# contract instead of inheriting legacy Dashboard exception/CSRF behaviour.
def _benchmark_iso(value):
    return value.isoformat() if value is not None else None


def _benchmark_error(code, status, *, fields=None):
    payload = {'success': False, 'error': code}
    if fields:
        payload['fields'] = fields
    return JsonResponse(payload, status=status)


def _benchmark_validation_fields(exc):
    if hasattr(exc, 'message_dict'):
        return {key: [str(item) for item in values]
                for key, values in exc.message_dict.items()}
    return {'non_field_errors': [str(item) for item in exc.messages]}


class _BenchmarkAPIError(Exception):
    def __init__(self, code, status):
        self.code = code
        self.status = status
        super().__init__(code)


def _benchmark_json_object(request, *, empty=False, allowed_fields=None):
    content_type = (request.META.get('CONTENT_TYPE') or '').split(';', 1)[0].strip().lower()
    if content_type != 'application/json':
        raise _BenchmarkAPIError('unsupported_media_type', 415)

    def reject_non_json_constant(value):
        raise ValueError(value)

    try:
        payload = json.loads(
            request.body.decode('utf-8'), parse_constant=reject_non_json_constant,
        )
    except (UnicodeDecodeError, ValueError):
        raise ValidationError({'body': ['请求体必须是有效 JSON 对象']})
    if not isinstance(payload, dict):
        raise ValidationError({'body': ['请求体必须是 JSON 对象']})
    if empty and payload:
        raise _BenchmarkAPIError('unknown_fields', 400)
    if allowed_fields is not None and set(payload).difference(allowed_fields):
        raise _BenchmarkAPIError('unknown_fields', 400)
    return payload


class _BenchmarkReadAPIView(View):
    """Authenticated Benchmark result access for users granted the page capability."""

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        if not has_dashboard_permission(request.user, 'simc.benchmarks'):
            return _benchmark_error('forbidden', 403)
        return super().dispatch(request, *args, **kwargs)


class _BenchmarkAdminAPIView(View):
    """Benchmark Dashboard API access controlled by the page capability."""

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        if not has_dashboard_permission(request.user, 'simc.benchmarks'):
            return _benchmark_error('forbidden', 403)
        try:
            return super().dispatch(request, *args, **kwargs)
        except _BenchmarkAPIError as exc:
            return _benchmark_error(exc.code, exc.status)
        except ValidationError as exc:
            return _benchmark_error('validation_error', 400,
                                    fields=_benchmark_validation_fields(exc))
        except BenchmarkExecutionConflict:
            return _benchmark_error('execution_conflict', 409)
        except TaskValidationUnavailable:
            return _benchmark_error('service_unavailable', 503)
        except PermissionDenied:
            return _benchmark_error('forbidden', 403)
        except ProtectedError:
            return _benchmark_error(
                'protected_resource', 400,
                fields={'panel': ['Panel 被受保护资源引用，无法删除']},
            )
        except Exception:
            logger.exception('Benchmark Dashboard API unexpected failure')
            return _benchmark_error('internal_error', 500)

    def http_method_not_allowed(self, request, *args, **kwargs):
        methods = self._allowed_methods()
        response = _benchmark_error('method_not_allowed', 405)
        response['Allow'] = ', '.join(methods)
        return response

    @staticmethod
    def panel_or_404(panel_id):
        panel = SimcBenchmarkPanel.objects.filter(pk=panel_id).first()
        if panel is None:
            return None, _benchmark_error('not_found', 404)
        return panel, None


class _SimcOptionsAPIView(View):
    """Resource catalogs use the SimC product-admin identity, not page grants."""

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        if not _is_simc_admin(request.user):
            return _benchmark_error('forbidden', 403)
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def panel_or_404(panel_id):
        panel = SimcBenchmarkPanel.objects.filter(pk=panel_id).first()
        if panel is None:
            return None, _benchmark_error('not_found', 404)
        return panel, None

    def http_method_not_allowed(self, request, *args, **kwargs):
        methods = self._allowed_methods()
        response = _benchmark_error('method_not_allowed', 405)
        response['Allow'] = ', '.join(methods)
        return response


def _benchmark_panel_summary(panel, execution=None, panel_coverage=None):
    data = {
        'id': panel.pk, 'slug': panel.slug, 'name': panel.name,
        'is_active': panel.is_active, 'is_public': panel.is_public,
        'schedule_enabled': panel.schedule_enabled,
        'interval_seconds': panel.interval_seconds,
        'next_run_at': _benchmark_iso(panel.next_run_at),
        'last_scheduled_at': _benchmark_iso(panel.last_scheduled_at),
        'published_execution_id': panel.published_execution_id,
        'aggregate_baseline_execution_id': panel.aggregate_baseline_execution_id,
        'counts': {
            'specs': panel.spec_count, 'scenarios': panel.scenario_count,
            'profiles': panel.profile_count, 'candidates': panel.candidate_count,
        },
    }
    data['execution'] = (
        _benchmark_execution_progress(
            execution,
            getattr(execution, '_dashboard_cases', []),
            is_active=panel.active_execution_id == execution.pk,
        ) if execution is not None else None
    )
    data['panel_coverage'] = panel_coverage or {
        'aggregate_baseline_execution_id': panel.aggregate_baseline_execution_id,
        'coordinates': 0,
        'candidate_runs': 0,
        'available_results': 0,
        'missing_results': 0,
        'source_executions': [],
    }
    return data


def _benchmark_config_frozen(execution):
    snapshot = execution.config_snapshot
    try:
        return (
            isinstance(snapshot, dict)
            and isinstance(execution.config_hash, str)
            and bool(execution.config_hash)
            and execution.config_hash == _canonical_hash(snapshot)
        )
    except (TypeError, ValueError):
        return False


def _benchmark_execution_metadata(execution, cases, total_cases=None):
    """Expose aggregate lifecycle only; never rebuild results from Task/Run here."""
    expected_tasks = len(cases) if total_cases is None else total_cases
    return {
        'config_frozen': _benchmark_config_frozen(execution),
        'task_bindings': sum(1 for case in cases if case.task_id is not None),
        'task_total': expected_tasks,
        'results_available': (
            len(cases) == expected_tasks
            and execution.status == SimcBenchmarkExecution.STATUS_SUCCESS
            and execution.completed_at is not None
            and execution.results_finalized_at is not None
            and isinstance(execution.result_hash, str)
            and len(execution.result_hash) == 64
        ),
    }


def _benchmark_progress_case_queryset():
    """Load only lifecycle fields used by the live Dashboard projection."""
    return SimcBenchmarkCase.objects.select_related('task').only(
        'id', 'execution_id', 'task_id', 'status', 'error_detail',
        'spec_key', 'scenario_key', 'profile_key',
        'spec_label', 'scenario_label', 'profile_label',
        'task__id', 'task__current_status', 'task__ext', 'task__error_detail',
        'task__simulation_runs__id', 'task__simulation_runs__status',
        'task__simulation_runs__error_detail',
        'task__artifacts__id', 'task__artifacts__artifact_type',
    ).prefetch_related('task__simulation_runs', 'task__artifacts').order_by('id')


def _benchmark_failure_rows(execution, cases):
    """Project authoritative Case/Task failures for list and history surfaces."""
    failures = []
    for case in cases:
        task = case.task if case.task_id else None
        run_error = ''
        if task is not None:
            failed_run = next((
                run for run in task.simulation_runs.all()
                if run.status == 'failed' and run.error_detail
            ), None)
            run_error = failed_run.error_detail if failed_run is not None else ''
        error = _benchmark_safe_string(
            case.error_detail or (task.error_detail if task is not None else '') or run_error,
        )
        if not error:
            continue
        report_artifact = None
        if task is not None:
            report_artifact = next((
                artifact for artifact in task.artifacts.all()
                if artifact.artifact_type == 'html_report'
            ), None)
        failures.append({
            'case_id': case.pk,
            'task_id': case.task_id,
            'labels': {
                'spec': _benchmark_safe_key(case.spec_label),
                'scenario': _benchmark_safe_key(case.scenario_label),
                'profile': _benchmark_safe_key(case.profile_label),
            },
            'error': error,
            'report_url': (
                f'/api/simc-workbench/artifacts/{report_artifact.id}/preview/'
                if report_artifact is not None else ''
            ),
            'detail_url': f'/dashboard/simc/benchmarks/executions/{execution.pk}/',
        })
    return failures


def _benchmark_execution_progress(execution, cases, *, is_active=False, case_count=None):
    cases = list(cases)
    if case_count is None:
        snapshot = execution.config_snapshot
        declared_cases = snapshot.get('case_count') if isinstance(snapshot, dict) else None
        total_cases = (declared_cases if type(declared_cases) is int and declared_cases >= 0
                       else len(cases))
    else:
        total_cases = _benchmark_safe_count(case_count)
    total_cases = max(total_cases, len(cases))
    statuses = ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
    counts = {status: 0 for status in statuses}
    progress_values = []
    current_cases = []
    run_counts = {status: 0 for status in ('pending', 'running', 'success', 'failed', 'cancelled')}
    total_runs = 0
    for case in cases:
        status = case.status if case.status in counts else 'failed'
        counts[status] += 1
        for run in case.task.simulation_runs.all() if case.task_id else ():
            run_status = {'completed': 'success'}.get(run.status, run.status)
            if run_status not in run_counts:
                run_status = 'failed'
            run_counts[run_status] += 1
            total_runs += 1
        if status == 'pending':
            progress = 0
        elif status == 'running':
            progress = task_progress(case.task)
            progress = progress if progress is not None else 0
            current_cases.append({
                'task_id': case.task_id,
                'spec': case.spec_label,
                'scenario': case.scenario_label,
                'profile': case.profile_label,
                'progress': progress,
            })
        else:
            progress = 100
        progress_values.append(progress)
    counts['pending'] += total_cases - len(cases)
    progress = round(sum(progress_values) / total_cases) if total_cases else 0
    return {
        'id': execution.pk,
        'status': execution.status,
        'is_active': is_active,
        'progress': progress,
        'case_count': total_cases,
        # Snapshot run_count is the frozen, scheduled workload.  Runs are
        # materialized lazily by comparison Tasks, so a live supplement can have
        # fewer rows than this until every Task has been consumed.
        'total_runs': _benchmark_safe_count(
            (execution.config_snapshot or {}).get('run_count')
            if isinstance(execution.config_snapshot, dict) else total_runs
        ),
        'materialized_runs': total_runs,
        'run_counts': run_counts,
        'counts': counts,
        'current_cases': current_cases[:3],
        'failures': _benchmark_failure_rows(execution, cases),
        'metadata': _benchmark_execution_metadata(execution, cases, total_cases),
        'created_at': _benchmark_iso(execution.created_at),
        'completed_at': _benchmark_iso(execution.completed_at),
    }


def _benchmark_execution_summary(execution, *, published_id=None, case_count=None, cases=None):
    snapshot = execution.config_snapshot if isinstance(execution.config_snapshot, dict) else {}
    snapshot_cases, snapshot_runs = snapshot.get('case_count'), snapshot.get('run_count')
    data = {
        'id': execution.pk, 'panel_id': execution.panel_id, 'trigger': execution.trigger,
        'status': execution.status,
        'scheduled_slot': _benchmark_iso(execution.scheduled_slot),
        'created_at': _benchmark_iso(execution.created_at),
        'completed_at': _benchmark_iso(execution.completed_at),
        'case_count': (snapshot_cases if type(snapshot_cases) is int
                       else (case_count if case_count is not None else 0)),
        'run_count': snapshot_runs if type(snapshot_runs) is int else 0,
        'is_published': execution.pk == published_id,
    }
    if cases is not None:
        cases = list(cases)
        data.update(_benchmark_execution_progress(
            execution, cases, case_count=data['case_count'],
        ))
        data['preflight_failures'] = [{
            'coordinate': {
                'spec_key': _benchmark_safe_key(case.spec_key),
                'scenario_key': _benchmark_safe_key(case.scenario_key),
                'profile_key': _benchmark_safe_key(case.profile_key),
            },
            'labels': {
                'spec': _benchmark_spec_display_name(case.spec_label, case.spec_key),
                'scenario': _benchmark_safe_key(case.scenario_label),
                'profile': _benchmark_safe_key(case.profile_label),
            },
            'error': _benchmark_safe_string(case.error_detail),
        } for case in cases if case.task_id is None and case.status == 'failed']
        data['panel_id'] = execution.panel_id
        data['trigger'] = execution.trigger
        data['scheduled_slot'] = _benchmark_iso(execution.scheduled_slot)
        data['run_count'] = snapshot_runs if type(snapshot_runs) is int else 0
        data['is_published'] = execution.pk == published_id
    return data


def _benchmark_safe_string(value, *, limit=240):
    """Return a bounded scalar with path/traceback details conservatively redacted."""
    if not isinstance(value, str):
        return None
    text = ' '.join(value.split())
    if not text:
        return None
    if re.search(r'(?i)traceback', text):
        return '[redacted]'
    text = re.sub(
        r'(?:[A-Za-z]:[\\/]|/)(?:[^\s;:,]+[\\/])*[^\s;:,]*',
        '[redacted]', text,
    )
    return text[:limit]


def _benchmark_safe_key(value):
    return _benchmark_safe_string(value, limit=200) or ''


def _benchmark_spec_display_name(value, spec_key=None):
    text = _benchmark_safe_key(value)
    normalized = re.sub(r'[^a-z0-9]', '', text.lower())
    names = {
        re.sub(r'[^a-z0-9]', '', name.lower()): label
        for name, label in SPEC_CN.items()
    }
    direct = names.get(normalized)
    if not direct:
        for name, label in sorted(names.items(), key=lambda item: -len(item[0])):
            if normalized.endswith(name):
                direct = label
                break
    if not direct:
        normalized_key = re.sub(r'[^a-z0-9]', '', _benchmark_safe_key(spec_key).lower())
        for name, label in sorted(names.items(), key=lambda item: -len(item[0])):
            if normalized_key.endswith(name):
                direct = label
                break
    if not direct:
        return text

    normalized_key = re.sub(r'[^a-z0-9]', '', _benchmark_safe_key(spec_key).lower())
    for name, label in CLASS_CN.items():
        if normalized_key.startswith(re.sub(r'[^a-z0-9]', '', name.lower())):
            return f'{direct}-{label}'
    return direct


def _benchmark_safe_count(value):
    return value if type(value) is int and value >= 0 else 0


def _benchmark_safe_dps(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float('inf'), float('-inf')):
        return None
    return value


def _benchmark_safe_detail(summary, execution):
    """Recursively project service output; no nested object is passed through."""
    summary = summary if isinstance(summary, dict) else {}
    cases = []
    source_cases = summary.get('cases')
    if not isinstance(source_cases, list):
        source_cases = []
    case_statuses = frozenset(('pending', 'running', 'success', 'partial', 'failed', 'cancelled'))
    run_statuses = frozenset(('pending', 'running', 'success', 'failed', 'cancelled'))
    for row in source_cases:
        if not isinstance(row, dict):
            continue
        labels = row.get('labels') if isinstance(row.get('labels'), dict) else {}
        source_runs = row.get('runs') if isinstance(row.get('runs'), list) else []
        runs = []
        for run in source_runs:
            if not isinstance(run, dict):
                continue
            status = run.get('status')
            runs.append({
                'key': _benchmark_safe_key(run.get('key')),
                'label': _benchmark_safe_key(run.get('label')),
                'status': status if status in run_statuses else 'failed',
                'dps': _benchmark_safe_dps(run.get('dps')),
                'error': _benchmark_safe_string(run.get('error')),
            })
        status = row.get('status')
        task_id = row.get('task_id')
        cases.append({
            'coordinate': {
                'spec_key': _benchmark_safe_key(row.get('spec_key')),
                'scenario_key': _benchmark_safe_key(row.get('scenario_key')),
                'profile_key': _benchmark_safe_key(row.get('profile_key')),
            },
            'labels': {
                'spec': _benchmark_spec_display_name(
                    labels.get('spec'), row.get('spec_key'),
                ),
                'scenario': _benchmark_safe_key(labels.get('scenario')),
                'profile': _benchmark_safe_key(labels.get('profile')),
            },
            'status': status if status in case_statuses else 'failed',
            'task_id': task_id if type(task_id) is int and task_id > 0 else None,
            'task_status': row.get('task_status') if row.get('task_status') in run_statuses else None,
            'task_status_label': _benchmark_safe_key(row.get('task_status_label')) or None,
            'task_progress': row.get('task_progress') if type(row.get('task_progress')) is int and 0 <= row.get('task_progress') <= 100 else None,
            'error': _benchmark_safe_string(row.get('error')),
            'runs': runs,
        })
    count_keys = ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
    run_count_keys = ('pending', 'running', 'success', 'failed', 'cancelled')
    source_run_counts = summary.get('run_counts')
    if not isinstance(source_run_counts, dict):
        source_run_counts = {}
    materialized_runs = _benchmark_safe_count(summary.get('total_runs'))
    snapshot = execution.config_snapshot
    planned_runs = (snapshot.get('run_count') if isinstance(snapshot, dict) else None)
    total_runs = planned_runs if type(planned_runs) is int and planned_runs >= 0 else materialized_runs
    status = summary.get('status')
    declared_cases = snapshot.get('case_count') if isinstance(snapshot, dict) else None
    total_cases = (declared_cases if type(declared_cases) is int and declared_cases >= 0
                   else _benchmark_safe_count(summary.get('total_cases')))
    total_cases = max(total_cases, len(cases))
    progress_values = []
    current_cases = []
    for case in cases:
        case_status = case['status']
        if case_status == 'pending':
            progress = 0
        elif case_status == 'running':
            progress = case['task_progress'] if case['task_progress'] is not None else 0
            current_cases.append({
                'task_id': case['task_id'],
                'spec': case['labels']['spec'],
                'scenario': case['labels']['scenario'],
                'profile': case['labels']['profile'],
                'progress': progress,
            })
        else:
            progress = 100
        progress_values.append(progress)
    progress = round(sum(progress_values) / total_cases) if total_cases else 0
    safe_counts = {
        key: _benchmark_safe_count(summary.get(key)) for key in count_keys
    }
    counted_cases = sum(safe_counts.values())
    if counted_cases < total_cases:
        safe_counts['pending'] += total_cases - counted_cases
    return {
        'id': execution.pk, 'panel_id': execution.panel_id,
        'trigger': execution.trigger,
        'status': status if status in case_statuses else 'failed',
        'scheduled_slot': _benchmark_iso(execution.scheduled_slot),
        'created_at': _benchmark_iso(execution.created_at),
        'completed_at': _benchmark_iso(execution.completed_at),
        'total_cases': total_cases,
        'total_runs': total_runs,
        'materialized_runs': materialized_runs,
        'progress': progress,
        'counts': safe_counts,
        'run_counts': {
            key: _benchmark_safe_count(source_run_counts.get(key))
            for key in run_count_keys
        },
        'current_cases': current_cases[:3],
        'metadata': {
            'config_frozen': _benchmark_config_frozen(execution),
            'task_bindings': sum(1 for case in cases if case['task_id'] is not None),
            'task_total': total_cases,
            'results_available': (
                len(cases) == total_cases
                and status == SimcBenchmarkExecution.STATUS_SUCCESS
                and execution.completed_at is not None
                and execution.results_finalized_at is not None
                and isinstance(execution.result_hash, str)
                and len(execution.result_hash) == 64
            ),
        },
        'is_active': execution.panel.active_execution_id == execution.pk,
        'is_published': execution.panel.published_execution_id == execution.pk,
        'cases': cases,
    }


class SimcBenchmarkPanelListAPIView(_BenchmarkAdminAPIView):
    def get(self, request):
        latest_execution = SimcBenchmarkExecution.objects.filter(
            panel_id=models.OuterRef('pk'),
        ).order_by('-created_at', '-id').values('pk')[:1]
        rows = list(SimcBenchmarkPanel.objects.annotate(
            spec_count=models.Count('specs', distinct=True),
            scenario_count=models.Count('scenarios', distinct=True),
            profile_count=models.Count('specs__profiles', distinct=True),
            candidate_count=models.Count('candidates', distinct=True),
            dashboard_latest_execution_id=models.Subquery(latest_execution),
        ).order_by('name', 'id'))
        execution_ids = {
            panel.active_execution_id or panel.dashboard_latest_execution_id
            for panel in rows
            if panel.active_execution_id or panel.dashboard_latest_execution_id
        }
        case_queryset = _benchmark_progress_case_queryset()
        executions = SimcBenchmarkExecution.objects.filter(pk__in=execution_ids).prefetch_related(
            models.Prefetch('cases', queryset=case_queryset, to_attr='_dashboard_cases'),
        )
        execution_by_id = {execution.pk: execution for execution in executions}
        coverage_by_panel = summarize_panel_coverage_counts(rows)
        return JsonResponse({'success': True, 'data': [
            _benchmark_panel_summary(
                panel,
                execution_by_id.get(
                    panel.active_execution_id or panel.dashboard_latest_execution_id,
                ),
                coverage_by_panel[panel.pk],
            )
            for panel in rows
        ]})

    def post(self, request):
        panel, _plan = replace_panel_config(_benchmark_json_object(request), request.user.id)
        data = serialize_panel_config(panel)
        data['next_run_at'] = _benchmark_iso(data['next_run_at'])
        return JsonResponse({'success': True, 'data': data}, status=201)


def _benchmark_spec_options():
    """Project the maintained WoW catalog, constrained by the executable spec set."""
    rows = []
    for class_display, spec_displays in CLASS_SPEC_MAP.items():
        for spec_display in spec_displays:
            spec_input = re.sub(r'(?<!^)(?=[A-Z])', '_', spec_display).lower()
            class_name, spec_name = canonical_simc_spec_identity(
                f'{class_display}_{spec_input}',
            )
            value = f'{class_name}_{spec_name}'
            if (class_name, spec_name) not in SUPPORTED_SIMC_SPEC_IDENTITIES:
                continue
            class_label = CLASS_CN.get(class_display, class_display)
            spec_label = SPEC_CN.get(spec_display, spec_display)
            rows.append({
                'value': value, 'spec_key': value, 'class_name': class_name,
                'class_label': class_label, 'spec_label': spec_label,
                'label': f'{class_label} · {spec_label}',
                'role': SPEC_ROLE[(class_display, spec_display)],
            })
    return rows


def _benchmark_backend_game_versions(backends):
    """Resolve all backend WoW builds with one catalog query (never one per row)."""
    selectors = {}
    catalog_q = models.Q()
    for backend in backends:
        current = str(backend.current_version or '').strip()
        if re.fullmatch(r'[0-9a-f]{40}', current):
            selectors[backend.pk] = ('exact', current)
            catalog_q |= models.Q(simc_revision=current)
            continue
        suffix = re.search(r'(?:^|-)([0-9a-f]{7,39})$', current)
        if suffix:
            selectors[backend.pk] = ('prefix', suffix.group(1))
            catalog_q |= models.Q(simc_revision__startswith=suffix.group(1))
    if not selectors:
        return {}
    identities = list(SimcAplSymbol.objects.filter(
        catalog_q, is_active=True,
    ).order_by().values_list('simc_revision', 'wow_build').distinct())
    result = {}
    for backend_id, (mode, revision) in selectors.items():
        matches = [row for row in identities if (
            row[0] == revision if mode == 'exact' else row[0].startswith(revision)
        )]
        if len(matches) == 1 and re.fullmatch(r'[0-9a-f]{40}', matches[0][0]):
            result[backend_id] = matches[0][1]
    return result


def _benchmark_resource_spec_key(row, *, allow_generic=False):
    """Return the same authoritative class/spec tag used by Profile APIs."""
    raw_spec = str(row.spec or '').strip().lower()
    if allow_generic and raw_spec in {'', 'generic', 'default', 'all', '*'}:
        return ''
    resolved = canonical_simc_profile_identity(raw_spec, row.class_name)
    if resolved not in SUPPORTED_SIMC_SPEC_IDENTITIES:
        return ''
    return f'{resolved[0]}_{resolved[1]}'


def _benchmark_create_defaults(resources, specs):
    """Resolve one authoritative default bundle per spec without browser guesswork."""
    production_backends = [row for row in resources['backends'] if row.identifier == 'production']
    result = {}
    for spec in specs:
        spec_key, class_name = spec['value'], spec['class_name']
        apls = [row for row in resources['apls'] if (
            row.is_system and row.owner_user_id is None
            and _benchmark_resource_spec_key(row) == spec_key
            and (not row.class_name or row.class_name == class_name)
        )]
        exact_templates = [row for row in resources['templates'] if (
            _benchmark_resource_spec_key(row, allow_generic=True) == spec_key
            and (not row.class_name or row.class_name == class_name)
        )]
        generic_templates = [row for row in resources['templates'] if (
            _benchmark_resource_spec_key(row, allow_generic=True) == ''
            and (not row.class_name or row.class_name == class_name)
        )]
        templates = exact_templates if exact_templates else generic_templates
        profiles = [row for row in resources['profiles'] if (
            row.user_id is None and row.source == SimcProfile.SOURCE_SIMC_UPSTREAM
            and bool(row.system_key) and _benchmark_resource_spec_key(row) == spec_key
        )]
        problems = []
        for label, rows in (('正式 Backend', production_backends), ('系统默认 APL', apls),
                            ('基础 Template', templates)):
            if len(rows) != 1:
                problems.append(f'{label}{"缺少" if not rows else "不唯一"}')
        if not profiles:
            problems.append('系统默认 Profile缺少')
        if problems:
            result[spec_key] = {'available': False, 'reason': '、'.join(problems)}
        else:
            default_profile = profiles[0]
            result[spec_key] = {
                'available': True, 'backend_id': production_backends[0].pk,
                'apl_id': apls[0].pk,
                'template_id': templates[0].pk, 'profile_id': default_profile.pk,
                'profile_label': default_profile.name,
            }
    return result


def _benchmark_options_payload(owner_id=None, ownership_context=None):
    # Benchmark resource selection is global. Keep the parameters temporarily
    # for call-site/API compatibility, but never use them to scope content.
    querysets = benchmark_resource_querysets()
    resources = {name: list(queryset) for name, queryset in querysets.items()}
    specs = _benchmark_spec_options()
    backend_game_versions = _benchmark_backend_game_versions(resources['backends'])
    return {
        'specs': specs,
        'fight_styles': [
            {'value': value, 'label': label} for value, label in SIMC_FIGHT_STYLES
        ],
        'raid_buffs': [
            {
                'value': value,
                'label': label,
                'simc_option': f'override.{value}',
            }
            for value, label in SIMC_RAID_BUFFS
        ],
        'create_defaults': _benchmark_create_defaults(resources, specs),
        'resources': {
            'backends': [{
                'id': row.pk, 'identifier': row.identifier, 'name': row.name,
                'platform': row.platform, 'version': row.current_version,
                'game_version': backend_game_versions.get(row.pk, ''),
                'is_default': row.identifier == 'production',
            } for row in resources['backends']],
            'templates': [{
                'id': row.pk, 'name': row.name, 'spec': row.spec,
                'spec_key': _benchmark_resource_spec_key(row, allow_generic=True),
                'canonical_spec': _benchmark_resource_spec_key(row, allow_generic=True),
                'class_name': row.class_name, 'source': row.source,
                'is_system': row.owner_user_id is None,
            } for row in resources['templates']],
            'apls': [{
                'id': row.pk, 'name': row.name, 'spec': row.spec,
                'spec_key': _benchmark_resource_spec_key(row),
                'canonical_spec': _benchmark_resource_spec_key(row),
                'class_name': row.class_name, 'source': row.source,
                'is_system': bool(row.is_system or row.owner_user_id is None),
                'validation_status': row.validation_status,
            } for row in resources['apls']],
            'profiles': [{
                'id': row.pk, 'name': row.name, 'spec': row.spec,
                'spec_key': _benchmark_resource_spec_key(row),
                'canonical_spec': _benchmark_resource_spec_key(row),
                'class_name': row.class_name, 'source': row.source,
                'is_system': row.user_id is None,
                'is_default': row.user_id is None,
            } for row in resources['profiles']],
        },
        'limits': {
            'max_specs': MAX_SPECS,
            'max_profiles_per_spec': MAX_PROFILES_PER_SPEC,
            'max_scenarios': MAX_SCENARIOS,
        },
        'ownership_context': 'benchmark_global',
    }


class SimcBenchmarkOptionsAPIView(_SimcOptionsAPIView):
    def get(self, request):
        return JsonResponse({
            'success': True,
            'data': _benchmark_options_payload(request.user.id, 'current_user'),
        })


@method_decorator(login_required, name='dispatch')
class SimcRaidBuffOptionsAPIView(View):
    """Small shared catalog for regular simulations and benchmark configuration."""

    def get(self, request):
        from botend.services.simc_composer import SIMC_CLASS_RAID_BUFFS
        return JsonResponse({'success': True, 'data': [
            {
                'value': value,
                'label': label,
                'simc_option': f'override.{value}',
                'default_classes': [
                    class_name for class_name, buffs in SIMC_CLASS_RAID_BUFFS.items()
                    if value in buffs
                ],
            }
            for value, label in SIMC_RAID_BUFFS
        ]})


class SimcBenchmarkPanelOptionsAPIView(_SimcOptionsAPIView):
    def get(self, request, panel_id):
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        return JsonResponse({
            'success': True,
            'data': _benchmark_options_payload(panel.created_by_id, 'panel_creator'),
        })


class SimcBenchmarkItemLookupAPIView(_BenchmarkAdminAPIView):
    def get(self, request):
        raw = (request.GET.get('item_id') or '').strip()
        if not raw:
            return JsonResponse({'success': True, 'data': None})
        if not raw.isdigit() or int(raw) <= 0:
            return _benchmark_error('item_id 必须是正整数', 400)
        item = WowItemSnapshot.objects.filter(item_id=int(raw)).first()
        if item is None:
            return JsonResponse({'success': True, 'data': None})
        return JsonResponse({'success': True, 'data': {
            'item_id': item.item_id,
            'name': item.name_zh or item.name or f'物品 {item.item_id}',
            'icon': item.icon or '',
        }})


class SimcBenchmarkPanelDetailAPIView(_BenchmarkAdminAPIView):
    def get(self, request, panel_id):
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        data = serialize_panel_config(panel)
        data['next_run_at'] = _benchmark_iso(data['next_run_at'])
        return JsonResponse({'success': True, 'data': data})

    def put(self, request, panel_id):
        payload = _benchmark_json_object(request)
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        # The creator is immutable and remains the task owner even when another
        # administrator maintains the globally sourced resource configuration.
        panel, _plan = replace_panel_config(payload, panel.created_by_id, panel=panel)
        data = serialize_panel_config(panel)
        data['next_run_at'] = _benchmark_iso(data['next_run_at'])
        return JsonResponse({'success': True, 'data': data})

    def patch(self, request, panel_id):
        payload = _benchmark_json_object(request, allowed_fields={
            'name', 'description', 'is_active', 'is_public',
            'schedule_enabled', 'interval_seconds', 'next_run_at',
        })
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        for field in ('name', 'description', 'is_active', 'is_public',
                      'schedule_enabled', 'interval_seconds', 'next_run_at'):
            if field in payload:
                setattr(panel, field, payload[field])
        panel.full_clean()
        panel.save()
        data = serialize_panel_config(panel)
        data['next_run_at'] = _benchmark_iso(data['next_run_at'])
        return JsonResponse({'success': True, 'data': data})

    def delete(self, request, panel_id):
        with transaction.atomic():
            panel = SimcBenchmarkPanel.objects.select_for_update().filter(pk=panel_id).first()
            if panel is None:
                return _benchmark_error('not_found', 404)
            panel.delete()
        return HttpResponse(status=204)


class SimcBenchmarkPanelRunAPIView(_BenchmarkAdminAPIView):
    def post(self, request, panel_id):
        payload = _benchmark_json_object(request, allowed_fields={'mode'})
        mode = payload.get('mode', 'supplement')
        if mode not in {'full', 'supplement'}:
            raise ValidationError({'mode': ['必须是 full 或 supplement']})
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        if not panel.is_active:
            raise ValidationError({'panel': ['Panel 未启用，无法执行']})
        execution = create_execution(
            panel, requested_by=request.user, execution_mode=mode,
        )
        return JsonResponse({
            'success': True,
            'data': _benchmark_execution_summary(
                execution, published_id=panel.published_execution_id,
                case_count=execution.cases.count(),
                cases=list(_benchmark_progress_case_queryset().filter(execution=execution)),
            ),
        }, status=202)


class SimcBenchmarkPanelExecutionListAPIView(_BenchmarkAdminAPIView):
    def get(self, request, panel_id):
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        try:
            page, size = int(request.GET.get('page', '1')), int(request.GET.get('size', '20'))
        except (TypeError, ValueError):
            raise ValidationError({'pagination': ['page 和 size 必须是整数']})
        if page < 1 or size < 1:
            raise ValidationError({'pagination': ['page 和 size 必须是正整数']})
        size = min(size, 50)
        case_queryset = _benchmark_progress_case_queryset()
        queryset = panel.executions.annotate(
            dashboard_case_count=models.Count('cases'),
        ).prefetch_related(models.Prefetch(
            'cases',
            queryset=case_queryset,
            to_attr='_dashboard_cases',
        )).order_by('-created_at', '-id')
        total = queryset.count()
        offset = (page - 1) * size
        rows = list(queryset[offset:offset + size])
        return JsonResponse({'success': True, 'data': {
            'items': [_benchmark_execution_summary(
                row, published_id=panel.published_execution_id,
                case_count=row.dashboard_case_count,
                cases=row._dashboard_cases,
            ) for row in rows],
            'pagination': {'page': page, 'size': size, 'total': total,
                           'has_next': offset + len(rows) < total},
        }})


class SimcBenchmarkExecutionDetailAPIView(_BenchmarkReadAPIView):
    def get(self, request, execution_id):
        execution = SimcBenchmarkExecution.objects.select_related('panel').filter(
            pk=execution_id,
        ).first()
        if execution is None:
            return _benchmark_error('not_found', 404)
        data = _benchmark_safe_detail(summarize_execution(execution), execution)
        # A retry's frozen snapshot is intentionally smaller than the Panel's full
        # current plan. Surface the independent reusable-result coverage so a
        # historical 65-case execution cannot look like it replaced a 96-case panel.
        try:
            data['panel_coverage'] = summarize_incremental_panel_coverage(execution.panel)
        except ValidationError:
            # Frozen Case detail remains readable if the current Panel configuration
            # is incomplete and cannot form a fresh logical surface.
            data['panel_coverage'] = {
                'aggregate_baseline_execution_id': execution.panel.aggregate_baseline_execution_id,
                'coordinates': 0, 'candidate_runs': 0,
                'available_results': 0, 'missing_results': 0,
                'source_executions': [],
            }
        return JsonResponse({'success': True, 'data': data})


class SimcBenchmarkExecutionRerunFailedAPIView(_BenchmarkAdminAPIView):
    def post(self, request, execution_id):
        _benchmark_json_object(request, empty=True)
        execution = SimcBenchmarkExecution.objects.select_related('panel').filter(
            pk=execution_id,
        ).first()
        if execution is None:
            return _benchmark_error('not_found', 404)
        rerun = rerun_failed_cases(execution, requested_by=request.user)
        cases = list(_benchmark_progress_case_queryset().filter(execution=rerun))
        return JsonResponse({
            'success': True,
            'data': _benchmark_execution_summary(
                rerun, published_id=rerun.panel.published_execution_id,
                case_count=rerun.cases.count(), cases=cases,
            ),
        }, status=202)


class SimcBenchmarkExecutionCancelAPIView(_BenchmarkAdminAPIView):
    def post(self, request, execution_id):
        _benchmark_json_object(request, empty=True)
        execution = SimcBenchmarkExecution.objects.select_related('panel').filter(
            pk=execution_id,
        ).first()
        if execution is None:
            return _benchmark_error('not_found', 404)
        cancelled = cancel_execution(execution, requested_by=request.user)
        cancelled = SimcBenchmarkExecution.objects.select_related('panel').get(pk=cancelled.pk)
        return JsonResponse({'success': True,
                             'data': _benchmark_safe_detail(
                                 summarize_execution(cancelled), cancelled)})


class SimcBenchmarkExecutionReconcileAPIView(_BenchmarkAdminAPIView):
    def post(self, request, execution_id):
        _benchmark_json_object(request, empty=True)
        execution = SimcBenchmarkExecution.objects.select_related('panel').filter(
            pk=execution_id,
        ).first()
        if execution is None:
            return _benchmark_error('not_found', 404)
        reconciled = reconcile_execution(execution)
        reconciled = SimcBenchmarkExecution.objects.select_related('panel').get(pk=reconciled.pk)
        return JsonResponse({'success': True,
                             'data': _benchmark_safe_detail(
                                 summarize_execution(reconciled), reconciled)})
