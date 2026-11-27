"""Dashboard API submodule: simc_resources."""
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


from ._shared import SIMC_SPEC_DB_IDENTITIES, SIMC_SPEC_OPTIONS, SIMC_SPEC_VALUES, SimcCatalogPermissionMixin, SimcTalentHeroTreeValidationError, WOW_SIMC_CLASS_NAMES, _canonical_simc_spec, _detect_simc_talent_spec, _is_simc_admin, _simc_class_label, _simc_spec_label, _simc_spec_visual, _validate_simc_talent_hero_tree
from .simc_benchmarks import _benchmark_resource_spec_key


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
    for item in qs.only('id', 'name', 'source', 'spec', 'class_name').annotate(
        display_content_length=Length('content'),
    ).order_by('source', 'name', 'id')[:50]:
        rows.append({
            'id': item.id,
            'name': item.name or item.spec,
            'source': item.source,
            'spec': item.spec,
            'class_name': item.class_name,
            'content_length': item.display_content_length,
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
    default_apl = default_apls.only('id').get()

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
    return default_apl, candidates.only('id').get()


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
class SimcRawInspectAPIView(DashboardPermissionRequiredMixin, View):
    """Inspect pasted raw SimulationCraft code and return safe task plans."""
    dashboard_permission = 'simc.workflow'

    def post(self, request):
        return JsonResponse({'success': False, 'error': '该接口已停用，请使用引用型 Profile、模板和 APL'}, status=410)


@method_decorator(login_required, name='dispatch')
class AplStorageAPIView(DashboardPermissionRequiredMixin, View):
    """
    APL存储API
    """
    dashboard_permission = 'simc.workflow'

    def get(self, request):
        """获取用户的APL列表"""
        try:
            user = request.user
            apl_list = SimcApl.objects.filter(is_active=True)
            if not _is_simc_admin(user):
                apl_list = apl_list.filter(owner_user_id=user.id)
            apl_list = apl_list.order_by('-id')

            result = []
            for apl in apl_list.only('id', 'name', 'spec'):
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
class AplDetailAPIView(DashboardPermissionRequiredMixin, View):
    """
    APL详情API
    """
    dashboard_permission = 'simc.workflow'

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
class SimcSpecOptionsAPIView(SimcCatalogPermissionMixin, View):
    def get(self, request):
        return JsonResponse({'success': True, 'data': SIMC_SPEC_OPTIONS})


@method_decorator(login_required, name='dispatch')
class SimcConsumableOptionsAPIView(SimcCatalogPermissionMixin, View):
    """Expose consumable tokens present in active upstream SimC Profiles."""

    OPTION_KEYS = ('flask', 'potion', 'food', 'augmentation')

    def get(self, request):
        values = {key: set() for key in self.OPTION_KEYS}
        values.update(temporary_enchant_main_hand=set(), temporary_enchant_off_hand=set())
        profiles = SimcProfile.objects.filter(
            user_id__isnull=True,
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            is_active=True,
        ).only('player_equipment')
        for profile in profiles:
            for raw_line in str(profile.player_equipment or '').splitlines():
                key, separator, raw_value = raw_line.partition('=')
                key = key.strip().lower()
                value = raw_value.strip()
                if not separator or not value:
                    continue
                if key in self.OPTION_KEYS:
                    values[key].add(value)
                elif key == 'temporary_enchant':
                    for entry in value.split('/'):
                        hand, hand_separator, enchant = entry.partition(':')
                        if not hand_separator or not enchant.strip():
                            continue
                        target = f'temporary_enchant_{hand.strip().lower()}'
                        if target in values:
                            values[target].add(enchant.strip())
        return JsonResponse({'success': True, 'data': {
            key: [simc_consumable_option(value) for value in sorted(items)]
            for key, items in values.items()
        }})


@method_decorator(login_required, name='dispatch')
class SimcBattlenetPreflightAPIView(DashboardPermissionRequiredMixin, View):
    """Fetch and validate Battle.net character data before it is saved or simulated."""
    dashboard_permission = 'simc.workflow'

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
class SimcBattlenetTopPlayersAPIView(DashboardPermissionRequiredMixin, View):
    """Return current-season specialization leaders as reusable Battle.net identities."""
    dashboard_permission = 'simc.workflow'

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
class SimcProfileAPIView(DashboardPermissionRequiredMixin, View):
    """
    SimC配置管理API
    """
    dashboard_permission = 'simc.workflow'

    @staticmethod
    def _profile_mode(profile, *, has_equipment=None):
        """Infer the legal legacy attribute-only form without rewriting stored data."""
        mode = (getattr(profile, 'player_config_mode', '') or '').strip()
        if has_equipment is None:
            has_equipment = bool(profile.player_equipment)
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
                is_admin = _is_simc_admin(request.user)
                if is_admin:
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
                
                spec = str(request.GET.get('spec') or '').strip().lower()
                if spec:
                    if spec not in SIMC_SPEC_VALUES:
                        return JsonResponse({'success': False, 'error': '必须选择有效的专精'}, status=400)
                    # Resolve only distinct identity metadata, never full player
                    # payloads. The central resolver owns legacy aliases and
                    # conflicting class/spec handling; fetch matching rows in SQL.
                    identities = profiles.order_by().values_list('spec', 'class_name').distinct()
                    matching = models.Q(pk__in=[])
                    for stored_spec, stored_class in identities:
                        if canonical_simc_profile_key(stored_spec, stored_class) == spec:
                            matching |= models.Q(spec=stored_spec, class_name=stored_class)
                    profiles = profiles.filter(matching)

                summary = request.GET.get('view') == 'summary'
                if summary:
                    profiles = profiles.only(
                        'user_id', 'name', 'spec', 'class_name', 'version', 'use_ptr',
                        'player_config_mode', 'battlenet_region', 'battlenet_realm',
                        'battlenet_character', 'is_active', 'source', 'sync_version',
                    ).annotate(equipment_length=Length('player_equipment'))

                profile_list = []
                for profile in profiles:
                    is_system = (
                        profile.user_id is None
                        and profile.source == SimcProfile.SOURCE_SIMC_UPSTREAM
                    )
                    has_equipment = bool(profile.equipment_length) if summary else bool(profile.player_equipment)
                    row = {
                        'id': profile.id,
                        'name': profile.name,
                        'spec': profile.spec,
                        'canonical_spec': canonical_simc_profile_key(profile.spec, profile.class_name),
                        'spec_label': _simc_spec_label(profile.spec, profile.class_name),
                        **_simc_spec_visual(profile.spec, profile.class_name),
                        'version': profile.version,
                        'use_ptr': bool(profile.use_ptr),
                        'class_name': getattr(profile, 'class_name', '') or '',
                        'player_config_mode': self._profile_mode(profile, has_equipment=has_equipment),
                        'battlenet_region': getattr(profile, 'battlenet_region', '') or '',
                        'battlenet_realm': getattr(profile, 'battlenet_realm', '') or '',
                        'battlenet_character': getattr(profile, 'battlenet_character', '') or '',
                        'is_active': profile.is_active,
                        'is_system': is_system,
                        'can_edit': is_admin or not is_system,
                        'can_delete': not is_system,
                        'source': profile.source,
                        'sync_version': getattr(profile, 'sync_version', '') or '',
                    }
                    if summary:
                        row['has_player_equipment'] = has_equipment
                    else:
                        player_equipment = profile.player_equipment or ''
                        row.update({
                            'player_equipment': player_equipment,
                            'talent': profile.talent,
                            'gear_strength': profile.gear_strength,
                            'gear_crit': profile.gear_crit,
                            'gear_haste': profile.gear_haste,
                            'gear_mastery': profile.gear_mastery,
                            'gear_versatility': profile.gear_versatility,
                            'equipment_line_count': sum(bool(line.strip()) for line in player_equipment.splitlines()),
                        })
                    profile_list.append(row)
                
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
                        talent_string_id=data.get('talent_string_id'),
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
                        talent_string_id=data.get('talent_string_id'),
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
                            talent_string_id=data.get('talent_string_id'),
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
                        talent_string_id=data.get('talent_string_id'),
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
                                base_template_id=None, selected_apl_id=None,
                                talent_string_id=None, is_admin=False):
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
                talent_string_id=talent_string_id,
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
                request.user.id,
                profile,
                regular_time=data.get('regular_time'),
                regular_target_count=data.get('regular_target_count'),
                base_template_id=data.get('base_template_id'),
                selected_apl_id=data.get('selected_apl_id'),
                talent_string_id=data.get('talent_string_id'),
                is_admin=_is_simc_admin(request.user),
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
class SimcAplCandidatesAPIView(DashboardPermissionRequiredMixin, View):
    """
    APL候选方案：GET获取指定专精的APL列表，POST基于GLM生成对比任务
    """
    dashboard_permission = 'simc.workflow'

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
                talent_string_id=data.get('talent_string_id'),
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
            template_id, apl_id, backend_id=None, talent_string_id=None):
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
            talent_string_id=talent_string_id,
        )
        for item in created:
            item['task_id'] = task.id
        return task, created


@method_decorator(login_required, name='dispatch')
class SimcTalentStringAPIView(DashboardPermissionRequiredMixin, View):
    """SimC 独立天赋字符串资源管理。"""
    dashboard_permission = 'simc.workflow'

    @staticmethod
    def _row(row, request):
        is_system = bool(row.is_system)
        return {
            'id': row.id, 'name': row.name,
            'display_name': getattr(row, 'display_name', None) or talent_display_name(row),
            'spec': row.spec, 'canonical_spec': row.spec,
            'spec_label': _simc_spec_label(row.spec, ''), 'class_label': _simc_class_label(row.spec, ''),
            'label': f"{_simc_spec_label(row.spec, '')} · {_simc_class_label(row.spec, '')}", 'talent': row.talent,
            'hero_talent_names': normalize_hero_subtree_names_zh(row.hero_talent_names),
            'default_apl_id': row.default_apl_id,
            'talent_simulator_url': '/portal/talents/?' + urlencode({'class': SIMC_SPEC_DB_IDENTITIES.get(row.spec, ('', ''))[0], 'spec': SIMC_SPEC_DB_IDENTITIES.get(row.spec, ('', ''))[1], 'code': row.talent}),
            'is_system': is_system, 'is_active': bool(row.is_active),
            'is_selectable': bool(row.is_selectable), 'owner_user_id': row.owner_user_id,
            'created_at': row.created_at.isoformat() if row.created_at else '',
            'modified_at': row.modified_at.isoformat() if row.modified_at else '',
            'can_edit': _is_simc_admin(request.user) or (not is_system and row.owner_user_id == request.user.id),
            'can_delete': not is_system and row.owner_user_id == request.user.id,
        }

    @staticmethod
    def _default_apl(raw_id, spec):
        if raw_id in (None, ''):
            return None
        try:
            apl = SimcApl.objects.filter(
                pk=int(raw_id), is_active=True, is_selectable=True,
            ).first()
        except (TypeError, ValueError):
            apl = None
        if apl is None or _benchmark_resource_spec_key(apl) != spec:
            raise ValueError('默认 APL 不存在、不可选，或与天赋专精不一致')
        return apl

    @staticmethod
    def _visible(request, include_inactive=False):
        rows = SimcTalentString.objects.filter(models.Q(is_system=True) | models.Q(owner_user_id=request.user.id))
        if not include_inactive:
            rows = rows.filter(is_active=True)
        return rows

    def get(self, request, talent_string_id=None):
        rows = self._visible(request, include_inactive=_is_simc_admin(request.user))
        if talent_string_id:
            row = rows.filter(id=talent_string_id).first()
            if row is None:
                return JsonResponse({'success': False, 'error': '天赋字符串不存在或无权限访问'}, status=404)
            return JsonResponse({'success': True, 'data': self._row(row, request)})
        spec = str(request.GET.get('spec') or '').strip().lower()
        if spec:
            rows = rows.filter(spec=spec)
        try:
            page = int(request.GET.get('page', 1))
            page_size = int(request.GET.get('page_size', 25))
            if page < 1 or page_size < 1:
                raise ValueError
        except (TypeError, ValueError):
            return JsonResponse({'success': False, 'error': '分页参数必须为正整数'}, status=400)
        page_size = min(page_size, 100)
        total = rows.count()
        offset = (page - 1) * page_size
        # Slice in SQL before projecting guide-backed titles; even huge empty
        # page numbers must not become an overflowing database OFFSET.
        rows = list(rows.order_by('spec', 'name', 'id')[offset:offset + page_size]) if offset < total else []
        rows = apply_talent_display_names(rows)
        return JsonResponse({
            'success': True,
            'data': [self._row(row, request) for row in rows],
            'pagination': {
                'page': page, 'page_size': page_size, 'total': total,
                'total_pages': (total + page_size - 1) // page_size,
            },
        })

    def post(self, request):
        try:
            data = json.loads(request.body or '{}')
            if data.get('copy_from_id'):
                return JsonResponse({'success': False, 'error': '天赋字符串不支持复制资源，请使用复制字符串'}, status=400)
            name = str(data.get('name') or '').strip()
            spec = str(data.get('spec') or '').strip().lower()
            talent = str(data.get('talent') or '').strip()
            if not name or not talent:
                return JsonResponse({'success': False, 'error': '名称和天赋字符串不能为空'}, status=400)
            if not spec:
                spec = _detect_simc_talent_spec(talent)
                if not spec:
                    return JsonResponse({'success': False, 'error': '无法根据天赋字符串识别专精，请手动选择专精'}, status=400)
            if spec not in SIMC_SPEC_VALUES:
                return JsonResponse({'success': False, 'error': '必须选择有效的专精'}, status=400)
            default_apl = self._default_apl(data.get('default_apl_id'), spec)
            hero_talent_names = _validate_simc_talent_hero_tree(spec, talent)
            row = SimcTalentString.objects.create(
                name=name[:200], spec=spec, talent=talent[:2000],
                default_apl=default_apl,
                hero_talent_names=hero_talent_names,
                owner_user_id=request.user.id, is_active=True, is_selectable=True,
            )
            return JsonResponse({'success': True, 'message': '天赋字符串已创建', 'data': self._row(row, request)})
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': '无效的JSON数据'}, status=400)
        except SimcTalentHeroTreeValidationError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=400)

    def put(self, request):
        try:
            data = json.loads(request.body or '{}')
            row = SimcTalentString.objects.filter(id=data.get('id')).first()
            if row is None or not (_is_simc_admin(request.user) or (not row.is_system and row.owner_user_id == request.user.id)):
                return JsonResponse({'success': False, 'error': '天赋字符串不存在或无权编辑'}, status=404)
            name = str(data.get('name', row.name) or '').strip()
            spec = str(data.get('spec', row.spec) or '').strip().lower()
            talent = str(data.get('talent', row.talent) or '').strip()
            if not name or not spec or not talent or spec not in SIMC_SPEC_VALUES:
                return JsonResponse({'success': False, 'error': '名称、有效专精和天赋字符串不能为空'}, status=400)
            default_apl = self._default_apl(
                data.get('default_apl_id') if 'default_apl_id' in data else row.default_apl_id,
                spec,
            )
            hero_talent_names = _validate_simc_talent_hero_tree(spec, talent)
            row.name, row.spec, row.talent, row.default_apl = name[:200], spec, talent[:2000], default_apl
            row.hero_talent_names = hero_talent_names
            if 'is_active' in data: row.is_active = bool(data['is_active'])
            if 'is_selectable' in data: row.is_selectable = bool(data['is_selectable'])
            row.save()
            return JsonResponse({'success': True, 'message': '天赋字符串已更新', 'data': self._row(row, request)})
        except SimcTalentHeroTreeValidationError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=400)
        except (TypeError, ValueError, json.JSONDecodeError):
            return JsonResponse({'success': False, 'error': '请求数据无效'}, status=400)

    def delete(self, request):
        try:
            data = json.loads(request.body or '{}')
            row = SimcTalentString.objects.filter(id=data.get('id'), owner_user_id=request.user.id, is_system=False).first()
            if row is None:
                return JsonResponse({'success': False, 'error': '天赋字符串不存在或无权删除'}, status=404)
            row.delete()
            return JsonResponse({'success': True, 'message': '天赋字符串已删除'})
        except (TypeError, ValueError, json.JSONDecodeError):
            return JsonResponse({'success': False, 'error': '请求数据无效'}, status=400)


class SimcTalentStringCandidatesAPIView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'simc.workflow'

    def get(self, request):
        spec = str(request.GET.get('spec') or '').strip()
        queryset = SimcTalentString.objects.filter(
            models.Q(is_system=True) | models.Q(owner_user_id=request.user.id),
            is_active=True, is_selectable=True,
        )
        if spec:
            queryset = queryset.filter(spec=spec)
        rows = apply_talent_display_names(list(queryset))
        return JsonResponse({'success': True, 'data': [{
            'id': row.id, 'name': row.name, 'display_name': row.display_name,
            'spec': row.spec, 'talent': row.talent,
            'hero_talent_names': row.hero_talent_names, 'is_system': row.is_system,
        } for row in rows]})


@method_decorator(login_required, name='dispatch')
class SimcTemplateAPIView(DashboardPermissionRequiredMixin, View):
    """Legacy compatibility API for the single global base template."""
    dashboard_permission = 'simc.workflow'

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
            for template in templates.only(
                'id', 'spec', 'class_name', 'name', 'source', 'is_active', 'is_selectable',
            ).annotate(preview_text=Substr('content', 1, 100), preview_length=Length('content')):
                preview = template.preview_text + ('...' if template.preview_length > 100 else '')
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


