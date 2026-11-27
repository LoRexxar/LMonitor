"""Dashboard API submodule: simc_workbench."""
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


from ._shared import _canonical_simc_spec, _fmt_dt, _is_simc_admin, _simc_class_for_spec, _simc_class_label, _simc_spec_label
from .simc_apl import ConvertTextAPIView, _latest_catalog_identity
from .simc_benchmarks import _benchmark_safe_string, _benchmark_spec_display_name
from .simc_resources import SimcTemplateAPIView
from .simc_tasks import SimcComparisonTaskAPIView
from .simc_analysis import SimcRegularCompareAPIView


class SimcWorkbenchAPIView(View):
    """安全的 SimC 工作台资源总览、详情与白名单生命周期操作。"""

    def dispatch(self, request, resource=None, object_id=None, *args, **kwargs):
        if not request.user.is_authenticated:
            return login_required(super().dispatch)(
                request, resource, object_id, *args, **kwargs,
            )
        if resource in {'tasks', 'history', 'task-favorites', 'artifacts', 'benchmark-executions'}:
            code = 'simc.history'
        elif resource in {'backends', 'secondary-rules', 'mastery-rules'}:
            code = 'simc.advanced'
        else:
            code = 'simc.workflow'
        if not has_dashboard_permission(request.user, code):
            return JsonResponse({'success': False, 'error': '缺少对应的 SimC 后台权限'}, status=403)
        return super().dispatch(request, resource, object_id, *args, **kwargs)

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
            match = re.search(r'^\s*Error\s*:\s*([^\r\n]+)', text, flags=re.IGNORECASE | re.MULTILINE)
        if not match:
            # These messages are generated by our scheduler, not by a subprocess.
            # They carry no command, path, profile, or raw SimC output, so showing
            # this finite allowlist gives operators the actionable failure cause.
            scheduler_message = text.strip()
            allowed_scheduler_messages = (
                'Agent 租约过期',
                'Agent 租约过期，执行已中断；已复制 Task 重试',
                'Agent 同级租约已失效',
                'Worker 心跳超时',
                'Worker 心跳超时，执行已中断；已复制 Task 重试',
                'SimC后端不可用: production',
                '属性寻优推进失败',
                '属性寻优推进失败：当前属性搜索轮次没有足够完成结果',
            )
            if scheduler_message in allowed_scheduler_messages or re.fullmatch(
                r'Worker 重试次数上限（\d+）', scheduler_message,
            ):
                return scheduler_message
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
    def _benchmark_history_summary(execution, task_counts=None, run_counts=None):
        """Serialize one list row without materializing child Case/Task details."""
        status_labels = {
            'pending': '待运行', 'running': '运行中', 'success': '成功',
            'partial': '部分完成', 'failed': '失败', 'cancelled': '已取消',
        }
        task_counts = task_counts or {
            key: 0 for key in ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
        }
        run_counts = run_counts or {
            key: 0 for key in ('pending', 'running', 'success', 'failed', 'cancelled')
        }
        case_count = sum(task_counts.values())
        run_count = sum(run_counts.values())
        if run_count:
            completed = run_counts['success'] + run_counts['failed'] + run_counts['cancelled']
            total = run_count
        else:
            completed = sum(task_counts[key] for key in ('success', 'partial', 'failed', 'cancelled'))
            total = case_count
        return {
            'row_type': 'benchmark_execution', 'id': execution.pk,
            'execution_id': execution.pk, 'panel_id': execution.panel_id,
            'name': f'{execution.panel.name} · 执行 #{execution.pk}',
            'status': execution.status,
            'status_label': status_labels.get(execution.status, '未知'),
            'is_active': execution.status in {
                SimcBenchmarkExecution.STATUS_PENDING,
                SimcBenchmarkExecution.STATUS_RUNNING,
            },
            'progress': int(completed * 100 / total) if total else 0,
            'case_count': case_count,
            'task_counts': task_counts,
            'run_count': run_count,
            'run_counts': run_counts,
            'baseline_counts': None,
            'created_at': execution.created_at,
            'detail_resource': 'benchmark_executions',
        }

    @staticmethod
    def _task_row(task, *, runs=None, latest_run=None, report_artifact=None):
        if runs is None:
            runs = task.simulation_runs.order_by('sequence')[:100]
            latest_run = task.simulation_runs.filter(status='completed').order_by('-sequence').first()
            report_artifact = task.artifacts.filter(artifact_type='html_report').order_by('-id').first()
        summary = {}
        if latest_run and isinstance(latest_run.result_summary, dict):
            summary = SimcWorkbenchAPIView._safe_summary(latest_run.result_summary)
        else:
            try:
                parsed = json.loads(task.result_summary or '{}')
                summary = SimcWorkbenchAPIView._safe_summary(parsed) if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                pass
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
            'task_error_summary': (
                SimcWorkbenchAPIView._safe_run_error_summary(task.error_detail)
                if task.current_status == 3 else ''
            ),
            'candidate_label': task.candidate_label, 'result_summary': summary,
            'runs': [
                SimcWorkbenchAPIView._run_row(run)
                for run in runs
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
            scope = str(request.GET.get('scope') or 'all').strip().lower()
            if scope not in ('all', 'favorites'):
                return JsonResponse({'success': False, 'error': '历史任务范围无效'}, status=400)
            simulation_type = str(request.GET.get('simulation_type') or 'all').strip().lower()
            if simulation_type not in {
                'all', 'non_benchmark', 'normal', 'comparison', 'attribute_sweep', 'benchmark',
            }:
                return JsonResponse({'success': False, 'error': '模拟类型筛选无效'}, status=400)
            requested_spec = str(request.GET.get('spec') or '').strip().lower()
            spec = _canonical_simc_spec(requested_spec) if requested_spec else ''
            if requested_spec and not spec:
                return JsonResponse({'success': False, 'error': '专精筛选无效'}, status=400)

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

            ordinary_queryset = SimcTask.objects.filter(
                user_id=request.user.id,
                is_active=True,
                benchmark_case__isnull=True,
            ).exclude(pk__in=benchmark_ancestor_ids)
            if spec:
                ordinary_queryset = ordinary_queryset.filter(
                    models.Q(talent_version__payload__spec=spec)
                    | models.Q(talent_version__isnull=True, profile_version__payload__spec=spec)
                    | models.Q(
                        talent_version__isnull=True,
                        profile_version__isnull=True,
                        profile__spec=spec,
                    )
                )
            if simulation_type == 'benchmark':
                ordinary_queryset = ordinary_queryset.none()
            elif simulation_type == 'normal':
                ordinary_queryset = ordinary_queryset.filter(mode__in=('', 'normal'))
            elif simulation_type in ('comparison', 'attribute_sweep'):
                ordinary_queryset = ordinary_queryset.filter(mode=simulation_type)

            if scope == 'favorites':
                refs = SimcTaskFavorite.objects.filter(
                    user_id=request.user.id, task_id__in=ordinary_queryset.values('id'),
                ).order_by('-created_at', '-task_id').annotate(
                    history_type=models.Value('task'),
                ).values_list('history_type', 'task_id', 'created_at')
            else:
                refs = ordinary_queryset.order_by().annotate(
                    history_type=models.Value('task'), history_time=models.F('modified_time'),
                ).values_list('history_type', 'id', 'history_time')
                include_benchmark = not spec and simulation_type in ('all', 'benchmark')
                if include_benchmark:
                    own_case = SimcBenchmarkCase.objects.filter(
                        execution_id=models.OuterRef('pk'), task__user_id=request.user.id,
                    )
                    execution_refs = SimcBenchmarkExecution.objects.filter(
                        models.Exists(own_case),
                    ).order_by().annotate(
                        history_type=models.Value('benchmark_execution'), history_time=models.F('created_at'),
                    ).values_list('history_type', 'id', 'history_time')
                    refs = refs.union(execution_refs, all=True)
                # 同一时刻先排 Benchmark，同类型再按主键倒序。
                refs = refs.order_by('-history_time', 'history_type', '-id')
            total = refs.count()
            total_pages = (total + page_size - 1) // page_size
            offset = (page - 1) * page_size
            page_refs = list(refs[offset:offset + page_size]) if offset < total else []

            task_ids = [object_id for row_type, object_id, _created_at in page_refs if row_type == 'task']
            execution_ids = [
                object_id for row_type, object_id, _created_at in page_refs
                if row_type == 'benchmark_execution'
            ]
            favorite_task_ids = set(SimcTaskFavorite.objects.filter(
                user_id=request.user.id,
                task_id__in=task_ids,
            ).values_list('task_id', flat=True))
            tasks = {
                task.pk: task for task in SimcTask.objects.filter(pk__in=task_ids).only(
                    'id', 'name', 'current_status', 'ext', 'modified_time', 'mode',
                    'apl_id', 'talent_string_id', 'apl_version_id', 'talent_version_id', 'simulation_params',
                )
            }
            version_ids = {
                version_id for task in tasks.values()
                for version_id in (task.apl_version_id, task.talent_version_id)
                if version_id
            }
            title_fields = ('name', 'spec', 'talent', 'hero_talent_names')
            versions = {
                version_id: dict(zip(title_fields, values))
                for version_id, *values in SimcResourceVersion.objects.filter(id__in=version_ids).values_list(
                    'id', *[f'payload__{field}' for field in title_fields],
                )
            }
            apl_ids = {task.apl_id for task in tasks.values() if task.apl_id}
            talent_ids = {task.talent_string_id for task in tasks.values() if task.talent_string_id}
            apls = {
                apl.id: apl for apl in SimcApl.objects.filter(id__in=apl_ids).only('id', 'name')
            }
            talents = {
                talent.id: talent for talent in apply_talent_display_names(list(
                    SimcTalentString.objects.filter(id__in=talent_ids)
                ))
            }
            completed_task_ids = set(SimulationRun.objects.filter(
                task_id__in=task_ids, status='completed',
            ).values_list('task_id', flat=True))
            active_execution_statuses = (
                SimcBenchmarkExecution.STATUS_PENDING,
                SimcBenchmarkExecution.STATUS_RUNNING,
            )
            effective_case_filters = {
                'pending': models.Q(task__current_status=0),
                'running': models.Q(task__current_status__in=(1, 4)),
                'success': models.Q(task__current_status=2),
                'partial': models.Q(pk__isnull=True),
                'failed': models.Q(task__current_status=3),
                'cancelled': models.Q(task__current_status=5),
            }
            case_annotations = {}
            for status_name, active_task_filter in effective_case_filters.items():
                case_annotations[status_name] = models.Count('id', filter=(
                    models.Q(execution__status__in=active_execution_statuses, task__isnull=False)
                    & active_task_filter
                ) | (
                    models.Q(execution__status__in=active_execution_statuses, task__isnull=True)
                    & models.Q(status=status_name)
                ) | (
                    ~models.Q(execution__status__in=active_execution_statuses)
                    & models.Q(status=status_name)
                ))
            task_count_keys = ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
            task_counts_by_execution = {
                row['execution_id']: {key: row[key] for key in task_count_keys}
                for row in SimcBenchmarkCase.objects.filter(
                    execution_id__in=execution_ids,
                ).values('execution_id').annotate(**case_annotations)
            }
            run_count_keys = ('pending', 'running', 'success', 'failed', 'cancelled')
            run_annotations = {
                key: models.Count('id', filter=models.Q(
                    status__in=('completed', 'success') if key == 'success' else (key,),
                ))
                for key in run_count_keys
            }
            run_counts_by_execution = {
                row['task__benchmark_case__execution_id']: {key: row[key] for key in run_count_keys}
                for row in SimulationRun.objects.filter(
                    task__benchmark_case__execution_id__in=execution_ids,
                ).values('task__benchmark_case__execution_id').annotate(**run_annotations)
            }
            executions = {
                execution.pk: execution
                for execution in SimcBenchmarkExecution.objects.filter(
                    pk__in=execution_ids,
                ).select_related('panel').only(
                    'id', 'panel_id', 'panel__name', 'status', 'created_at',
                )
            }

            frozen_title_rows = [
                (task.id, versions[task.talent_version_id], task.talent_string_id)
                for task in tasks.values()
                if versions.get(task.talent_version_id, {}).get('name')
            ]
            frozen_titles = dict(zip(
                [row[0] for row in frozen_title_rows],
                frozen_talent_display_names(
                    [row[1] for row in frozen_title_rows],
                    [row[2] for row in frozen_title_rows],
                ),
            ))
            page_rows = []
            for row_type, object_id, _created_at in page_refs:
                if row_type == 'benchmark_execution':
                    execution = executions.get(object_id)
                    if execution is not None:
                        page_rows.append(self._benchmark_history_summary(
                            execution,
                            task_counts_by_execution.get(object_id),
                            run_counts_by_execution.get(object_id),
                        ))
                    continue
                task = tasks.get(object_id)
                if task is not None:
                    apl_payload = versions.get(task.apl_version_id, {})
                    talent_payload = versions.get(task.talent_version_id, {})
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
                        'is_favorite': task.id in favorite_task_ids,
                        'can_compare': task.current_status == 2 and task.id in completed_task_ids,
                        'apl_name': apl_payload.get('name') or getattr(apls.get(task.apl_id), 'name', '') or '—',
                        'talent_name': (
                            talent_payload.get('name')
                            or getattr(talents.get(task.talent_string_id), 'name', '')
                            or '—'
                        ),
                        'talent_display_name': (
                            frozen_titles.get(task.id, '')
                            if talent_payload.get('name') else
                            getattr(talents.get(task.talent_string_id), 'display_name', '')
                        ) or '—',
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

        if resource == 'benchmark-executions' and object_id:
            execution = SimcBenchmarkExecution.objects.filter(
                id=object_id,
                cases__task__user_id=request.user.id,
            ).select_related('panel').distinct().first()
            if execution is None:
                return JsonResponse({'success': False, 'error': 'Benchmark 执行不存在'}, status=404)
            return JsonResponse({'success': True, 'data': self._benchmark_history_row(execution)})

        if resource == 'tasks':
            if object_id:
                task = SimcTask.objects.filter(id=object_id).first()
                if not task:
                    return JsonResponse({'success': False, 'error': '任务不存在'}, status=404)
                if request.GET.get('rerun_form') == '1':
                    if task.user_id != request.user.id:
                        return JsonResponse({'success': False, 'error': '任务不存在或无权限访问'}, status=404)
                    task = SimcTask.objects.select_related(
                        'profile', 'profile_version', 'talent_version',
                    ).get(id=task.id)
                    profile_payload = (
                        task.profile_version.payload
                        if task.profile_version and isinstance(task.profile_version.payload, dict)
                        else {}
                    )
                    return JsonResponse({'success': True, 'data': {'rerun_form': {
                        'source_task_id': task.id,
                        'name': task.name,
                        'mode': task.mode,
                        'mode_params': task.mode_params if isinstance(task.mode_params, dict) else {},
                        'profile_id': task.profile_id,
                        'profile_name': str(
                            profile_payload.get('name') or (task.profile.name if task.profile else '') or ''
                        ),
                        'backend_id': task.backend_id,
                        'apl_id': task.apl_id,
                        'talent_string_id': task.talent_string_id or (
                            task.talent_version.resource_id if task.talent_version else None
                        ),
                        'spec': (
                            canonical_simc_profile_key(
                                profile_payload.get('spec') or (task.profile.spec if task.profile else ''),
                                task.profile.class_name if task.profile else '',
                            )
                            or str(
                                profile_payload.get('spec')
                                or (task.profile.spec if task.profile else '')
                                or ''
                            ).strip()
                        ),
                        'simulation_params': (
                            task.simulation_params if isinstance(task.simulation_params, dict) else {}
                        ),
                    }}})
                row = self._task_row(task)
                talent_payload = task.talent_version.payload if task.talent_version and isinstance(task.talent_version.payload, dict) else {}
                profile_payload = task.profile_version.payload if task.profile_version and isinstance(task.profile_version.payload, dict) else {}
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
                    'talent_build_code': str(
                        talent_payload.get('talent') or talent_payload.get('build_code')
                        or profile_payload.get('talent_build_code') or profile_payload.get('talents') or ''
                    ).strip(),
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
                    attribute_report = SimcRegularCompareAPIView()._safe_attribute_report(raw_report)
                    recommendation = (
                        attribute_report.get('recommendation')
                        if isinstance(attribute_report, dict) else None
                    )
                    search_converged = (
                        attribute_report.get('converged') is True
                        or task.current_status == 2
                    )
                    final_result = (
                        dict(recommendation)
                        if search_converged and isinstance(recommendation, dict)
                        else None
                    )
                    final_artifact = None
                    if final_result and final_result.get('id') is not None:
                        final_artifact = next((
                            artifact for artifact in artifacts
                            if artifact.run_id == final_result['id']
                            and artifact.artifact_type == 'html_report'
                        ), None)
                        final_result['artifact_id'] = final_artifact.id if final_artifact else None
                        final_result['report_url'] = ''
                        if final_artifact:
                            final_artifact_row = self._artifact_row(final_artifact)
                            if final_artifact_row.get('can_preview'):
                                final_result['report_url'] = final_artifact_row.get('preview_url', '')
                            from botend.services.simc_result_analysis import (
                                analyze_run_artifact,
                                extract_unbuffed_secondary_stats,
                            )
                            final_report_summary = analyze_run_artifact(task, final_artifact)
                            final_result['unbuffed_stats'] = (
                                extract_unbuffed_secondary_stats(final_report_summary)
                            )
                        attribute_report['final_result'] = final_result
                    row['attribute_report'] = attribute_report
                    row['report_artifact_id'] = final_artifact.id if final_artifact else None
                    row['report_preview_url'] = (final_result or {}).get('report_url', '')
                    row['has_report'] = bool(row['report_preview_url'])
                row['report_url'] = (
                    (row.get('attribute_report') or {}).get('final_result', {}).get('report_url', '')
                    if task.mode == 'attribute_sweep'
                    else f'/simc-compare/?task_id={task.id}' if task.mode == 'comparison' and runs else ''
                )
                return JsonResponse({'success': True, 'data': row})

            qs = SimcTask.objects.filter(
                user_id=request.user.id,
            ).order_by('-modified_time')
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

            run_summaries = SimulationRun.objects.only(
                'id', 'task_id', 'sequence', 'round_number', 'candidate_key', 'candidate_label',
                'candidate_params', 'status', 'input_hash', 'result_summary', 'error_detail',
                'started_at', 'completed_at',
            ).order_by('sequence')
            latest_success = SimulationRun.objects.filter(status='completed').only(
                'id', 'task_id', 'result_summary',
            ).order_by('-sequence')[:1]
            report_links = SimcTaskArtifact.objects.filter(artifact_type='html_report').only(
                'id', 'task_id', 'file_path',
            ).order_by('-id')[:1]
            tasks = qs.only(
                'id', 'name', 'current_status', 'ext', 'mode', 'error_detail', 'candidate_label',
                'result_summary', 'is_active', 'create_time', 'modified_time',
            ).prefetch_related(
                models.Prefetch('simulation_runs', queryset=run_summaries[:100], to_attr='dashboard_runs'),
                models.Prefetch('simulation_runs', queryset=latest_success, to_attr='dashboard_latest_success'),
                models.Prefetch('artifacts', queryset=report_links, to_attr='dashboard_report'),
            )[offset:offset + page_size]
            return JsonResponse({
                'success': True,
                'data': [self._task_row(
                    row, runs=row.dashboard_runs,
                    latest_run=next(iter(row.dashboard_latest_success), None),
                    report_artifact=next(iter(row.dashboard_report), None),
                ) for row in tasks],
                'pagination': {
                    'page': page,
                    'page_size': page_size,
                    'total': total,
                    'total_pages': total_pages,
                }
            })

        if resource == 'artifacts':
            if object_id:
                row = SimcTaskArtifact.objects.filter(
                    id=object_id,
                ).select_related('task').first()
                if not row:
                    return JsonResponse({'success': False, 'error': '产物不存在'}, status=404)
                return JsonResponse({
                    'success': True,
                    'data': self._artifact_row(row, include_task=True),
                })
            qs = SimcTaskArtifact.objects.filter(
                task__user_id=request.user.id,
            ).select_related('task').only(
                'id', 'task_id', 'task__name', 'artifact_type', 'file_path', 'file_size', 'created_at',
            ).order_by('-created_at')
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
            rows = list(qs.only(
                'id', 'name', 'spec', 'class_name', 'source', 'is_system', 'is_active',
                'is_selectable', 'validation_status', 'validation_stale_reason',
                'validated_content_hash', 'validation_revision', 'validation_game_build',
            ).annotate(current_content_hash=models.Case(
                models.When(validation_status=SimcApl.VALIDATION_VALID, then=SHA256('content')),
                default=models.Value(''), output_field=models.CharField(),
            )))
            identity = current_validation_identity() if rows else None
            return JsonResponse({'success': True, 'data': [{
                'id': apl.id, 'name': apl.name, 'spec': apl.spec,
                    'spec_label': _simc_spec_label(apl.spec, apl.class_name),
                'class_label': _simc_class_label(apl.spec, apl.class_name),
                'class_name': apl.class_name, 'source': apl.source,
                'is_system': apl.is_system, 'is_active': apl.is_active,
                'is_selectable': apl.is_selectable,
                'validation_status': apl.validation_status,
                'validation_stale_reason': apl.validation_staleness(identity, content_hash=apl.current_content_hash),
                'can_use_for_task': apl.is_active and (not apl.is_system or apl.is_selectable),
                'read_only': apl.is_system and not _is_simc_admin(request.user),
                'can_copy': apl.is_system and apl.is_active and apl.is_selectable,
            } for apl in rows], 'can_write': True})

        if resource == 'templates':
            # 工作台只展示和维护 SimC 输入框架；默认玩家配置是内部导入资源，
            # APL 则由独立的 APL 资源库负责，不能混入内容模板列表。
            qs = SimcContentTemplate.objects.filter(
                owner_user_id__isnull=True,
            ).order_by('spec', 'name')
            if object_id:
                qs = qs.filter(id=object_id)
            else:
                qs = qs.defer('content')
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
            summary_only = request.GET.get('summary') == '1'
            if summary_only:
                qs = qs.only('id', 'name', 'spec', 'class_name', 'is_active')
            rows = []
            for row in qs:
                rows.append({
                    'id': row.id,
                    'title': row.name,
                    'spec': row.spec,
                    'spec_label': _simc_spec_label(row.spec, row.class_name),
                    'class_label': _simc_class_label(row.spec, row.class_name),
                    **({} if summary_only else {'apl_code': row.content}),
                    'is_active': row.is_active,
                })
            return JsonResponse({'success': True, 'data': rows})

        if resource == 'backend-options':
            backends = [{'id': row.pk, 'identifier': row.identifier, 'name': row.name,
                         'game_version': SimcBackendBinaryAPIView()._get_game_version(row),
                         'is_default': row.identifier == 'production'}
                        for row in SimcBackendBinary.objects.filter(is_active=True).order_by('id')]
            return JsonResponse({'success': True, 'data': {'backends': backends}})
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
        if resource == 'task-favorites' and object_id:
            task = SimcTask.objects.filter(
                id=object_id,
                user_id=request.user.id,
                benchmark_case__isnull=True,
            ).first()
            if not task:
                return JsonResponse({'success': False, 'error': '任务不存在'}, status=404)
            if action == 'favorite':
                SimcTaskFavorite.objects.get_or_create(user=request.user, task=task)
                is_favorite = True
            elif action == 'unfavorite':
                SimcTaskFavorite.objects.filter(user=request.user, task=task).delete()
                is_favorite = False
            else:
                return JsonResponse({'success': False, 'error': '收藏操作无效'}, status=400)
            return JsonResponse({
                'success': True,
                'data': {'task_id': task.id, 'is_favorite': is_favorite},
            })
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
                        spec = _canonical_simc_spec(data.get('spec'))
                        if not spec:
                            return JsonResponse({'success': False, 'error': '专精标识无效'}, status=400)
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
        if resource == 'tasks':
            if not object_id:
                return JsonResponse({'success': False, 'error': '缺少任务 ID'}, status=400)
            task = SimcTask.objects.filter(
                id=object_id,
                user_id=request.user.id,
                is_active=True,
            ).select_related('benchmark_case__execution__panel').first()
            if not task:
                return JsonResponse({'success': False, 'error': '任务不存在'}, status=404)
            benchmark_case = getattr(task, 'benchmark_case', None)
            if benchmark_case and benchmark_case.execution.panel_id:
                return JsonResponse(
                    {'success': False, 'error': '基准任务仍绑定面板，不能删除'}, status=409,
                )
            if task.current_status not in (2, 3, 5):
                return JsonResponse({'success': False, 'error': '待执行或执行中的任务不能删除'}, status=409)
            task.is_active = False
            task.save(update_fields=['is_active', 'modified_time'])
            return JsonResponse({'success': True})
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


class SimcRunInputPreviewAPIView(DashboardPermissionRequiredMixin, View):
    """Compose readable SimC input for one Run from its task configuration."""
    dashboard_permission = 'simc.history'

    def get(self, request, task_id, run_id):
        run = SimulationRun.objects.filter(
            id=run_id, task_id=task_id,
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


class SimcTaskReportPreviewAPIView(DashboardPermissionRequiredMixin, View):
    """兼容没有 Artifact 记录的旧任务，并隐藏报告文件名与存储路径。"""
    dashboard_permission = 'simc.history'

    def get(self, request, object_id):
        task = SimcTask.objects.filter(id=object_id).first()
        if not task or not task.result_file:
            return JsonResponse({'success': False, 'error': '任务报告不存在'}, status=404)
        if all((task.profile_id, task.template_id, task.apl_id,
                task.profile_version_id, task.template_version_id, task.apl_version_id)):
            return JsonResponse({'success': False, 'error': '引用型任务报告请通过 Artifact 预览'}, status=404)
        from botend.services.simc_artifacts import (
            _validated_result, _validated_result_location,
        )
        result_name = os.path.basename(str(task.result_file))
        artifact = SimcTaskArtifact.objects.filter(
            task=task, artifact_type='html_report',
            file_path=f'simc_results/{result_name}',
        ).select_related('run').order_by('-created_at', '-id').first()
        validated = _validated_result(task, result_name, run=artifact.run if artifact else None)
        if not validated:
            location = _validated_result_location(
                task, result_name, run=artifact.run if artifact else None,
            )
            expected_path = f'simc_results/{result_name}'
            if location and location[1] == expected_path:
                from botend.services.simc_agent_oss import ReportStorageError, public_legacy_report_url
                try:
                    return HttpResponseRedirect(public_legacy_report_url(location[1]))
                except ReportStorageError:
                    pass
            return JsonResponse({'success': False, 'error': '任务报告不可用'}, status=404)
        response = FileResponse(open(str(validated[0]), 'rb'), content_type='text/html; charset=utf-8')
        response['Content-Security-Policy'] = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; sandbox allow-scripts; frame-ancestors 'self'"
        return response


class SimcArtifactPreviewAPIView(DashboardPermissionRequiredMixin, View):
    def get_dashboard_permissions(self, request):
        return ('simc.history', 'simc.benchmarks')

    def get(self, request, object_id):
        artifact = SimcTaskArtifact.objects.filter(
            id=object_id,
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
        from botend.services.simc_artifacts import (
            _validated_result, _validated_result_location,
        )
        validated = _validated_result(
            artifact.task, os.path.basename(artifact_path), run=artifact.run,
        )
        if not validated:
            location = _validated_result_location(
                artifact.task, os.path.basename(artifact_path), run=artifact.run,
            )
            if location and location[1] == artifact_path:
                from botend.services.simc_agent_oss import ReportStorageError, public_legacy_report_url
                try:
                    return HttpResponseRedirect(public_legacy_report_url(location[1]))
                except ReportStorageError:
                    pass
            return JsonResponse({'success': False, 'error': '产物文件不可用'}, status=404)
        if validated[1] != artifact_path:
            return JsonResponse({'success': False, 'error': '产物文件不可用'}, status=404)
        full_path = str(validated[0])
        content_type = 'text/html; charset=utf-8'
        response = FileResponse(open(full_path, 'rb'), content_type=content_type)
        response['Content-Security-Policy'] = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; sandbox allow-scripts; frame-ancestors 'self'"
        return response


class _SkillDamageSnapshotSpool:
    """Own one temporary response file and release its process-local slot once."""

    def __init__(self, raw_file, release_slot):
        self._raw_file = raw_file
        self._release_slot = release_slot
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._raw_file, name)

    def read(self, *args, **kwargs):
        chunk = self._raw_file.read(*args, **kwargs)
        if not chunk:
            self.close()
        return chunk

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._raw_file.close()
        finally:
            self._release_slot()


@method_decorator(login_required, name='dispatch')
class SimcSkillDamageAplAPIView(DashboardPermissionRequiredMixin, View):
    """用页面选定的专精和快照生成个人 APL 草稿。"""
    dashboard_permission = 'simc.skill-damage'

    def post(self, request):
        from botend.services.simc_apl.from_skill_damage import SkillAplGenerationError, build_apl
        from botend.services.simc_skill_actor_page import ActorDocumentParser, ActorPageError

        try:
            data = json.loads(request.body)
            if not isinstance(data, dict):
                raise ValueError
            snapshot_id = data.get('snapshot_id')
            actor_id = data.get('actor_id')
            title = data.get('title', 'wowdaily自动生成').strip()
            if (type(snapshot_id) is not int or type(actor_id) is not int
                    or min(snapshot_id, actor_id) <= 0 or not 0 < len(title) <= 200):
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            return JsonResponse({'success': False, 'error': '请选择快照专精，并填写 1 到 200 字的标题'}, status=400)

        snapshot = SimcSkillDamageSnapshotService.latest_display_snapshot()
        if not snapshot or snapshot.pk != snapshot_id:
            return JsonResponse({'success': False, 'error': '快照已更新或不可用，请刷新页面'}, status=409)
        row = snapshot.actor_rows.filter(pk=actor_id).values(
            'id', 'class_name', 'specialization', 'actor_payload', 'payload_storage',
        ).first()
        if not row:
            return JsonResponse({'success': False, 'error': '该快照中没有选定的专精'}, status=404)
        spec = f"{row['class_name']}_{row['specialization']}"
        template = SimcApl.objects.filter(
            spec=spec, source=SimcApl.SOURCE_SIMC_UPSTREAM, is_active=True,
            is_system=True, owner_user_id__isnull=True,
        ).only('content', 'sync_version').first()
        if not template:
            return JsonResponse({'success': False, 'error': '该专精缺少可用的默认 APL'}, status=409)

        apl = SimcApl(owner_user_id=request.user.pk, spec=spec, class_name=row['class_name'],
                      name=title, source=SimcApl.SOURCE_USER, sync_version=template.sync_version)
        existing = SimcApl.objects.filter(active_unique_key=apl._compute_active_unique_key())
        conflict = {'success': False, 'error': '当前专精已有同名 APL，请修改标题后再生成'}
        if existing.exists():
            return JsonResponse(conflict, status=409)

        try:
            talent_guards = {int(entry): guard for entry, guard in
                             (row['actor_payload'].get('apl_talent_guards') or {}).items()}
            needs_current_catalog = not talent_guards
            if needs_current_catalog:
                from botend.services.simc_skill_damage import current_apl_guards
                talent_guards = current_apl_guards(row['class_name'], row['specialization'])
            parser = ActorDocumentParser(SkillSnapshotActorStorage.iterate_json_document(row, 'actor_payload'))
            apl.content = build_apl(template.content, parser.actions(),
                                    row['actor_payload'].get('hero_talent_trees') or [], spec,
                                    talent_guards=talent_guards, check_trait_names=needs_current_catalog)
            apl.content = f'# 技能归一化快照 {snapshot.pk} · {spec} · {snapshot.game_build}\n' + apl.content
        except ActorPageError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=exc.status)
        except SkillAplGenerationError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=422)
        except (ValueError, TypeError, KeyError):
            logger.exception('生成 APL 时无法读取技能快照 %s 的专精 %s', snapshot.pk, spec)
            return JsonResponse({'success': False, 'error': '专精快照数据不完整，无法生成 APL'}, status=503)

        try:
            with transaction.atomic():
                apl.save(force_insert=True)
        except IntegrityError:
            if existing.exists():
                return JsonResponse(conflict, status=409)
            raise
        return JsonResponse({'success': True, 'data': {'id': apl.pk, 'title': apl.name, 'spec': spec}}, status=201)


@method_decorator(login_required, name='dispatch')
class SimcSkillDamageSnapshotAPIView(DashboardPermissionRequiredMixin, View):
    """Read the latest successful DBC snapshot and explicitly trigger generation."""
    dashboard_permission = 'simc.skill-damage'

    _RESPONSE_SPOOL_SLOTS = threading.BoundedSemaphore(value=1)
    _RESPONSE_SPOOL_MAX_BYTES = 512 * 1024 * 1024
    _RESPONSE_SPOOL_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024

    @staticmethod
    def _job_data(row):
        if not row:
            return None
        payload = row.payload or {}
        return {
            'id': row.pk,
            'identity': {
                'simc_revision': row.simc_revision,
                'game_build': row.game_build,
                'schema_revision': row.schema_revision,
            },
            'status': row.status,
            'spec_count': row.generated_spec_count,
            'total_spec_count': payload.get('total_spec_count'),
            'current_specialization': payload.get('current_specialization') or '',
            'action_count': row.generated_action_count,
            'has_error': bool(row.error_text),
            'created_at': _fmt_dt(row.created_at),
            'completed_at': _fmt_dt(row.completed_at),
        }

    @staticmethod
    def _summary_job_data(row):
        if not row:
            return None
        try:
            total_spec_count = int(row.get('total_spec_count_value') or 0)
        except (TypeError, ValueError):
            total_spec_count = 0
        return {
            'id': row['id'],
            'identity': {
                'simc_revision': row['simc_revision'],
                'game_build': row['game_build'],
                'schema_revision': row['schema_revision'],
            },
            'status': row['status'],
            'spec_count': row['generated_spec_count'],
            'total_spec_count': total_spec_count,
            'current_specialization': row.get('current_specialization_value') or '',
            'action_count': row['generated_action_count'],
            'has_error': bool(row['error_text']),
            'created_at': _fmt_dt(row['created_at']),
            'completed_at': _fmt_dt(row['completed_at']),
        }

    def _summary_job(self):
        fields = (
            'id', 'simc_revision', 'game_build', 'schema_revision', 'status',
            'generated_spec_count', 'generated_action_count', 'error_text',
            'created_at', 'completed_at', 'total_spec_count_value',
            'current_specialization_value',
        )
        jobs = SimcSkillDamageSnapshot.objects.annotate(
            total_spec_count_value=KeyTextTransform('total_spec_count', 'payload'),
            current_specialization_value=KeyTextTransform('current_specialization', 'payload'),
        ).filter(
            schema_revision=SimcSkillDamageSnapshotService.DATASET_SCHEMA_REVISION,
        )
        backend = SimcBackendBinary.objects.filter(
            identifier='production', is_active=True,
        ).only('current_version', 'game_build').first()
        backend_revision = str(backend.current_version or '').strip().lower() if backend else ''
        backend_game_build = str(backend.game_build or '').strip() if backend else ''
        if (
            len(backend_revision) == 40
            and all(ch in '0123456789abcdef' for ch in backend_revision)
            and backend_game_build
        ):
            jobs = jobs.filter(
                simc_revision=backend_revision,
                game_build=backend_game_build,
            )
        active = jobs.filter(status__in=(
            SimcSkillDamageSnapshot.STATUS_PENDING,
            SimcSkillDamageSnapshot.STATUS_RUNNING,
        )).order_by('-created_at', '-id').values(*fields).first()
        return active or jobs.order_by('-created_at', '-id').values(*fields).first()

    @classmethod
    def _sharded_snapshot_response(
        cls, display_snapshot, *, job, can_generate, actor_ids=None,
        total_actor_count=None, total_action_count=None,
    ):
        """Spool one actor at a time, then stream bytes without retaining DB rows."""
        actor_queryset = display_snapshot.actor_rows.order_by('ordinal', 'id')
        if actor_ids is not None:
            actor_queryset = actor_queryset.filter(id__in=actor_ids)
        # Inspect manifests/SQL lengths BEFORE selecting either JSON document.
        # Legacy complete materialization is intentionally limited even below
        # the unchanged 512MiB transport cap; clients must select actor_page.
        from django.db.models.functions import Cast, Length
        estimates = actor_queryset.annotate(
            actor_chars=Length(Cast('actor_payload', models.TextField())),
            unresolved_chars=Length(Cast('unresolved_payload', models.TextField())),
        ).values('id', 'payload_storage', 'actor_chars', 'unresolved_chars')
        estimated_bytes = 0
        try:
            for item in estimates:
                documents = SkillSnapshotActorStorage._manifest(item)
                for document, length_key in (('actor_payload', 'actor_chars'),
                                              ('unresolved_payload', 'unresolved_chars')):
                    estimated_bytes += (documents[document]['bytes'] if document in documents
                                        else int(item[length_key] or 0) * 6)
                if estimated_bytes > min(cls._RESPONSE_SPOOL_MAX_BYTES, 16 * 1024 * 1024):
                    return JsonResponse({'success': False,
                        'error': '完整快照超过安全展示上限，请使用技能分页视图',
                        'code': 'snapshot_requires_paging',
                        'paged_view': {'view': 'actor_page', 'snapshot_id': display_snapshot.pk,
                                       'actor_ids': actor_ids}}, status=413)
        except (ValueError, TypeError):
            return JsonResponse({'success': False, 'error': '技能快照存储清单无效'}, status=503)
        actor_rows = list(actor_queryset.values(
            'id', 'class_name', 'specialization', 'unresolved_payload', 'payload_storage',
            'raw_action_count', 'display_action_count',
        ))
        unresolved = []
        unresolved_keys = set()
        for row in actor_rows:
            for unresolved_row in SkillSnapshotActorStorage.read_document(row, 'unresolved_payload') or []:
                key = json.dumps(unresolved_row, ensure_ascii=False, sort_keys=True)
                if key not in unresolved_keys:
                    unresolved_keys.add(key)
                    unresolved.append(unresolved_row)

        payload = dict(display_snapshot.payload or {})
        payload.pop('storage_format', None)
        payload.pop('wire_schema_revision', None)
        actor_placeholder = f'__lmonitor_actor_stream_{uuid.uuid4().hex}__'
        payload.update({
            'actors': actor_placeholder,
            'unresolved': unresolved,
            'display_action_count': sum(
                int(row['display_action_count'] or 0) for row in actor_rows
            ),
            'completed_profile_identities': sorted([
                [row['class_name'], row['specialization']] for row in actor_rows
            ]),
        })
        identity = {
            'simc_revision': display_snapshot.simc_revision,
            'game_build': display_snapshot.game_build,
            'schema_revision': display_snapshot.schema_revision,
        }
        snapshot = {**payload, 'identity': identity}
        snapshot['identity'] = identity
        snapshot['id'] = display_snapshot.pk
        snapshot['status'] = display_snapshot.status
        snapshot['completed_at'] = _fmt_dt(display_snapshot.completed_at)
        snapshot['spec_count'] = (
            total_actor_count if total_actor_count is not None else len(actor_rows)
        )
        snapshot['action_count'] = (
            total_action_count
            if total_action_count is not None
            else snapshot.get('display_action_count', 0)
        )
        snapshot['raw_action_count'] = sum(
            int(row['raw_action_count'] or 0) for row in actor_rows
        )
        envelope = {
            'success': True,
            'data': {
                'snapshot': snapshot,
                'snapshot_unavailable_reason': None,
                'job': job,
                'can_generate': can_generate,
            },
        }
        encoder = DjangoJSONEncoder()
        encoded_placeholder = encoder.encode(actor_placeholder)
        actor_ids = [row['id'] for row in actor_rows]
        if not cls._RESPONSE_SPOOL_SLOTS.acquire(blocking=False):
            return JsonResponse({
                'success': False,
                'message': '技能伤害快照正在传输，请稍后重试',
            }, status=503)

        managed_spool = None
        try:
            raw_spool = tempfile.TemporaryFile(mode='w+b')
            managed_spool = _SkillDamageSnapshotSpool(
                raw_spool, cls._RESPONSE_SPOOL_SLOTS.release,
            )
            written_bytes = 0
            next_disk_check = 0

            def write_chunk(chunk):
                nonlocal written_bytes, next_disk_check
                projected_bytes = written_bytes + len(chunk)
                if projected_bytes > cls._RESPONSE_SPOOL_MAX_BYTES:
                    raise RuntimeError('技能伤害快照响应超过安全大小上限')
                if projected_bytes >= next_disk_check:
                    if hasattr(os, 'statvfs'):
                        filesystem = os.statvfs(tempfile.gettempdir())
                        free_bytes = filesystem.f_bavail * filesystem.f_frsize
                    else:
                        from shutil import disk_usage
                        free_bytes = disk_usage(tempfile.gettempdir()).free
                    if free_bytes - max(len(chunk), 1024 * 1024) < cls._RESPONSE_SPOOL_MIN_FREE_BYTES:
                        raise RuntimeError('技能伤害快照响应临时磁盘余量不足')
                    next_disk_check = projected_bytes + 1024 * 1024
                managed_spool.write(chunk)
                written_bytes = projected_bytes

            placeholders = 0
            for token in encoder.iterencode(envelope):
                if token != encoded_placeholder:
                    write_chunk(token.encode('utf-8'))
                    continue
                placeholders += 1
                write_chunk(b'[')
                for index, actor_id in enumerate(actor_ids):
                    if index:
                        write_chunk(b', ')
                    actor_row = display_snapshot.actor_rows.filter(
                        pk=actor_id,
                    ).values('id', 'actor_payload', 'payload_storage').get()
                    actor = SkillSnapshotActorStorage.read_document(actor_row, 'actor_payload')
                    del actor_row
                    # Project only this bounded response-owned copy.
                    if isinstance(actor.get('reviewed_global_effects'), list):
                        actor['global_skill_effects'] = reviewed_global_display_effects(actor)
                    attach_skill_damage_source_context(actor)
                    attach_skill_damage_descriptions(actor, game_build=display_snapshot.game_build,
                        branch=(display_snapshot.payload.get('identity') or {}).get('data_branch', 'retail'))
                    for actor_token in encoder.iterencode(actor):
                        write_chunk(actor_token.encode('utf-8'))
                    del actor
                write_chunk(b']')
            if placeholders != 1:
                raise RuntimeError('技能伤害流式响应 actor 占位符无效')
            managed_spool.seek(0)
            return FileResponse(managed_spool, content_type='application/json')
        except Exception:
            if managed_spool is not None:
                managed_spool.close()
            else:
                cls._RESPONSE_SPOOL_SLOTS.release()
            logger.exception(
                '[Skill damage] failed to build bounded snapshot response id=%s',
                display_snapshot.pk,
            )
            return JsonResponse({
                'success': False,
                'message': '技能伤害快照暂时不可用，请稍后重试',
            }, status=503)

    def _actor_page_response(self, request, *, allow_stream=True, can_generate=None):
        """Pinned immutable membership, with no lock spanning the central scan."""
        if can_generate is None:
            # Resolve lazy auth on the request thread, not a worker DB connection.
            can_generate = bool(request.user.is_staff)
        if allow_stream and request.GET.get('stream') == '1':
            from botend.services.simc_skill_actor_stream import actor_json_stream
            return actor_json_stream(lambda: self._actor_page_response(
                request, allow_stream=False, can_generate=can_generate))
        try:
            try:
                snapshot_id = int(request.GET['snapshot_id'])
                actor_id = int(request.GET['actor_id'])
                if snapshot_id <= 0 or actor_id <= 0:
                    raise ValueError('Invalid identity')
            except (KeyError, TypeError, ValueError):
                raise ActorPageError('分页需要有效 snapshot_id 和 actor_id', code='invalid_query', status=400)
            snapshot = SimcSkillDamageSnapshot.objects.filter(pk=snapshot_id,
                status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
                schema_revision__gte=SimcSkillDamageSnapshotService.MIN_DISPLAY_SCHEMA_REVISION,
                schema_revision__lte=SimcSkillDamageSnapshotService.DATASET_SCHEMA_REVISION,
                payload__payload_format=SimcSkillDamageSnapshotService.PAYLOAD_FORMAT,
                payload__storage_format=SimcSkillDamageSnapshotService.STORAGE_FORMAT).first()
            if snapshot is None:
                raise ActorPageError('已发布技能快照不存在', code='snapshot_not_found', status=404)
            if not SimcSkillDamageSnapshotService._is_complete_wire_snapshot(snapshot):
                raise ActorPageError('已发布技能快照存储不完整', code='snapshot_incomplete')
            fields = ['id', 'actor_payload', 'payload_storage', 'raw_action_count',
                      'display_action_count', 'class_name', 'specialization']
            if request.GET.get('view') == 'unresolved_fragment':
                fields.append('unresolved_payload')
            row = snapshot.actor_rows.filter(pk=actor_id).values(*fields).first()
            if row is None:
                raise ActorPageError('技能专精不存在', code='actor_not_found', status=404)
            service = ActorPageService(row, snapshot)
            view = request.GET.get('view')
            if view in {'actor_fragment', 'actor_header_fragment', 'unresolved_fragment'}:
                try:
                    ordinal = int(request.GET.get('source_ordinal', 0))
                    offset = int(request.GET.get('offset', 0))
                    limit = int(request.GET.get('fragment_limit', 16384))
                    if ordinal < 0 or offset < 0 or not 1 <= limit <= 16384:
                        raise ValueError('Fragment bounds')
                except (TypeError, ValueError):
                    raise ActorPageError('原始片段范围无效', code='invalid_query', status=400)
                field = request.GET.get('field') if view == 'actor_header_fragment' else None
                if view == 'actor_header_fragment' and (
                        not isinstance(field, str) or not field or len(field) > 4096 or field == 'actions'):
                    raise ActorPageError('需要精确的原始页头字段', code='invalid_query', status=400)
                fragment = service.fragment(ordinal=ordinal, offset=offset, limit=limit,
                    document='unresolved_payload' if view == 'unresolved_fragment' else 'actor_payload',
                    field=field)
                return JsonResponse({'success': True, 'data': {'fragment': fragment}})
            actor, metadata = service.page(normalize_query(request.GET), request.GET.get('cursor', ''))
            if isinstance(actor.get('reviewed_global_effects'), list):
                actor['global_skill_effects'] = reviewed_global_display_effects(actor)
            attach_skill_damage_descriptions(actor, game_build=snapshot.game_build,
                branch=(snapshot.payload.get('identity') or {}).get('data_branch', 'retail'))
            payload = dict(snapshot.payload or {})
            payload.pop('storage_format', None)
            payload.pop('wire_schema_revision', None)
            payload.update({'actors': [actor], 'actor_page': metadata, 'unresolved': [],
                'identity': {'simc_revision': snapshot.simc_revision, 'game_build': snapshot.game_build,
                             'schema_revision': snapshot.schema_revision},
                'id': snapshot.pk, 'status': snapshot.status, 'completed_at': _fmt_dt(snapshot.completed_at),
                'spec_count': snapshot.generated_spec_count, 'action_count': snapshot.generated_action_count,
                'display_action_count': row['display_action_count'], 'raw_action_count': row['raw_action_count']})
            envelope = {'success': True, 'data': {'snapshot': payload,
                'snapshot_unavailable_reason': None, 'job': self._summary_job_data(self._summary_job()),
                'can_generate': can_generate}}
            # Never build an unbounded dumps string, including post-query labels.
            output = bytearray()
            for token in DjangoJSONEncoder(ensure_ascii=False, separators=(',', ':')).iterencode(envelope):
                encoded = token.encode('utf-8')
                if len(output) + len(encoded) > RESPONSE_BYTES:
                    raise ActorPageError('技能页展示资料超过安全字节上限', code='page_too_large', status=413)
                output.extend(encoded)
            return HttpResponse(bytes(output), content_type='application/json')
        except ActorPageError as exc:
            return JsonResponse({'success': False, 'error': str(exc), 'code': exc.code,
                **exc.details, 'paged_view': {'view': 'actor_page',
                    'snapshot_id': request.GET.get('snapshot_id'), 'actor_id': request.GET.get('actor_id')}},
                status=exc.status)
        except (ValueError, TypeError):
            logger.exception('[Skill damage] invalid bounded page data')
            return JsonResponse({'success': False, 'error': '技能快照数据暂时不可用'}, status=503)

    def get(self, request):
        if request.GET.get('view') in {'actor_page', 'actor_fragment', 'actor_header_fragment', 'unresolved_fragment'}:
            return self._actor_page_response(request)
        if request.GET.get('summary') == '1':
            return JsonResponse({
                'success': True,
                'data': {
                    'job': self._summary_job_data(self._summary_job()),
                    'can_generate': bool(request.user.is_staff),
                },
            })
        latest = SimcSkillDamageSnapshotService.latest_display_snapshot(lock=False)
        legacy_latest_exists = (
            latest is None
            and SimcSkillDamageSnapshot.objects.filter(
                status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            ).exists()
        )
        snapshot = None
        # A running snapshot is an unpublished partial dataset. Keep serving the
        # latest successful snapshot until all specializations are complete;
        # progress is exposed separately through the lightweight job summary.
        display_snapshot = latest
        if display_snapshot:
            actor_id = request.GET.get('actor_id')
            if actor_id:
                try:
                    selected_actor_id = int(actor_id)
                except (TypeError, ValueError):
                    return JsonResponse({'success': False, 'error': '技能专精索引无效'}, status=400)
                if not display_snapshot.actor_rows.filter(pk=selected_actor_id).exists():
                    return JsonResponse({'success': False, 'error': '技能专精不存在'}, status=404)
                return self._sharded_snapshot_response(
                    display_snapshot,
                    job=self._summary_job_data(self._summary_job()),
                    can_generate=bool(request.user.is_staff),
                    actor_ids=[selected_actor_id],
                    total_actor_count=display_snapshot.actor_rows.count(),
                    total_action_count=display_snapshot.generated_action_count,
                )
            if request.GET.get('index') == '1':
                actor_index = []
                for row in display_snapshot.actor_rows.order_by('ordinal', 'id').annotate(
                    hero_talent_trees_value=KeyTextTransform(
                        'hero_talent_trees', 'actor_payload',
                    ),
                ).values(
                    'id', 'class_name', 'specialization', 'hero_talent_trees_value',
                ):
                    raw_hero_talent_trees = row.pop('hero_talent_trees_value', None)
                    if isinstance(raw_hero_talent_trees, str):
                        try:
                            raw_hero_talent_trees = json.loads(raw_hero_talent_trees)
                        except (TypeError, ValueError):
                            raw_hero_talent_trees = []
                    hero_talent_trees = [
                        {
                            key: tree[key]
                            for key in ('id', 'name', 'name_zh')
                            if key in tree
                        }
                        for tree in (raw_hero_talent_trees or [])
                        if isinstance(tree, dict) and tree.get('id') is not None
                    ]
                    row['hero_talent_trees'] = hero_talent_trees
                    actor_index.append(row)
                payload = dict(display_snapshot.payload or {})
                payload.pop('storage_format', None)
                payload.pop('wire_schema_revision', None)
                payload.update({
                    'actors': [], 'actor_index': actor_index, 'unresolved': [],
                    'identity': {
                        'simc_revision': display_snapshot.simc_revision,
                        'game_build': display_snapshot.game_build,
                        'schema_revision': display_snapshot.schema_revision,
                    },
                    'id': display_snapshot.pk, 'status': display_snapshot.status,
                    'completed_at': _fmt_dt(display_snapshot.completed_at),
                    'spec_count': len(actor_index),
                    'action_count': display_snapshot.generated_action_count,
                })
                return JsonResponse({'success': True, 'data': {
                    'snapshot': payload, 'snapshot_unavailable_reason': None,
                    'job': self._summary_job_data(self._summary_job()),
                    'can_generate': bool(request.user.is_staff),
                }})
            return self._sharded_snapshot_response(
                display_snapshot,
                job=self._summary_job_data(self._summary_job()),
                can_generate=bool(request.user.is_staff),
            )
        return JsonResponse({
            'success': True,
            'data': {
                'snapshot': snapshot,
                'snapshot_unavailable_reason': (
                    f'最新成功快照不是 schema {SimcSkillDamageSnapshotService.DATASET_SCHEMA_REVISION} '
                    '英雄树划分的前置成对单项天赋运行时数据，请生成新快照。'
                    if legacy_latest_exists else None
                ),
                'job': self._summary_job_data(self._summary_job()),
                'can_generate': bool(request.user.is_staff),
            },
        })

    def post(self, request):
        if not request.user.is_staff:
            return JsonResponse({'success': False, 'error': '仅管理员可生成技能伤害快照'}, status=403)
        try:
            service = SimcSkillDamageSnapshotService.create_for_current_backend(
                request.user.id, claim=True,
            )
        except ValueError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=409)

        snapshot_id = service.snapshot.pk

        ready_path = Path(tempfile.gettempdir()) / f'lmonitor-skill-snapshot-ready-{uuid.uuid4().hex}'
        command = [
            sys.executable,
            str(Path(settings.BASE_DIR) / 'manage.py'),
            'generate_simc_skill_damage_snapshot',
            '--snapshot-id', str(snapshot_id),
            '--ready-file', str(ready_path),
        ]
        if service.backend and service.backend.pk:
            command.extend(['--backend-id', str(service.backend.pk)])
        process = None
        startup_error = ''
        try:
            process = subprocess.Popen(
                command,
                cwd=str(settings.BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            deadline = time.monotonic() + 10
            while not ready_path.exists():
                return_code = process.poll()
                if return_code is not None:
                    startup_error = f'生成命令在初始化阶段退出（code={return_code}）'
                    break
                if time.monotonic() >= deadline:
                    startup_error = '生成命令初始化超时'
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                    break
                time.sleep(0.05)
        except OSError as exc:
            startup_error = str(exc)
        finally:
            ready_path.unlink(missing_ok=True)
        if startup_error:
            SimcSkillDamageSnapshot.objects.filter(pk=snapshot_id).update(
                status=SimcSkillDamageSnapshot.STATUS_FAILED,
                error_text=startup_error[:4000],
                completed_at=timezone.now(),
            )
            return JsonResponse({'success': False, 'error': '启动技能伤害生成进程失败'}, status=500)
        return JsonResponse({
            'success': True,
            'message': '已开始生成当前 SimC/DBC 版本的技能伤害快照',
            'data': {'job': self._summary_job_data(self._summary_job())},
        }, status=202)


@method_decorator(login_required, name='dispatch')
class SimcBackendBinaryAPIView(DashboardPermissionRequiredMixin, View):
    """SimC后端更新状态API"""
    dashboard_permission = 'simc.advanced'

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
            'local_worker_enabled': row.local_worker_enabled,
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
                        'local_worker_enabled': True,
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
            if action not in ('set_auto_update', 'set_local_worker_enabled', 'set_maintenance_schedule', 'check', 'update', 'dispatch_agent_maintenance'):
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
                if not tasks:
                    return JsonResponse({
                        'success': False,
                        'error': '该 Backend 没有可下发更新任务的启用 Agent',
                    }, status=404)
                SimcAgentMaintenanceTask.objects.bulk_create(tasks)
                return JsonResponse({
                    'success': True,
                    'message': f'已向 {len(tasks)} 个 Agent 下发更新任务',
                    'data': [self._serialize_maintenance_task(task) for task in tasks],
                })

            runtime_platform = self._get_runtime_platform()
            row = SimcBackendBinary.objects.filter(identifier='production').first()
            if not row:
                row = SimcBackendBinary(identifier='production', name='正式服', platform=runtime_platform)
                row.simc_path = ''
                row.current_version = ''
                row.latest_version = ''
                row.auto_update = True
                row.local_worker_enabled = True
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

            if action == 'set_local_worker_enabled':
                enabled = self._json_bool(data.get('enabled'), True)
                row.local_worker_enabled = enabled
                row.save(update_fields=['local_worker_enabled'])
                source_dir, build_dir, binary_path = self._resolve_local_build_paths()
                return JsonResponse({
                    'success': True,
                    'message': f'本地 Worker 接收任务已{"开启" if enabled else "关闭"}',
                    'data': self._serialize_backend_row(row, source_dir, build_dir, binary_path),
                })

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
                threads = int(data.get('threads', 1) or 1)
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
                except Exception as exc:
                    close_old_connections()
                    err_msg = str(exc)[:500] or 'SimC 本地编译命令执行失败'
                    try:
                        row_inner = SimcBackendBinary.objects.filter(identifier='production').first()
                        if row_inner:
                            row_inner.is_updating = False
                            if not row_inner.last_error:
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


