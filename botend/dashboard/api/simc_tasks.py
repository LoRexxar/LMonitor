"""Dashboard API submodule: simc_tasks."""
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


from ._shared import SIMC_SPEC_VALUES, _accessible_simc_profile_q, _fmt_dt, _is_simc_admin, _simc_spec_label
from .simc_resources import SimcProfileAPIView, _get_simc_content_by_id, _get_unique_default_apl_for_spec


@method_decorator(login_required, name='dispatch')
class SimcTaskAPIView(DashboardPermissionRequiredMixin, View):
    """
    SimC任务管理API
    """
    dashboard_permission = 'simc.workflow'

    def get_dashboard_permissions(self, request):
        if request.method == 'GET':
            return ('simc.workflow', 'simc.history')
        if request.method in ('PUT', 'DELETE', 'PATCH'):
            return ('simc.history',)
        return super().get_dashboard_permissions(request)

    
    def get(self, request):
        """获取当前用户的SimC任务列表"""
        try:
            runs = SimulationRun.objects.only(
                'id', 'task_id', 'sequence', 'candidate_key', 'candidate_label', 'round_number',
                'status', 'result_summary', 'started_at', 'completed_at',
            ).order_by('sequence')
            tasks = SimcTask.objects.filter(user_id=request.user.id, is_active=True).only(
                'id', 'name', 'simc_profile_id', 'task_type', 'ext', 'current_status', 'mode',
                'profile_id', 'template_id', 'apl_id', 'profile_version_id', 'template_version_id',
                'apl_version_id', 'simulation_params', 'mode_params', 'create_time', 'modified_time',
            ).prefetch_related(models.Prefetch(
                'simulation_runs', queryset=runs, to_attr='dashboard_runs',
            )).order_by('-modified_time')
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
                    } for run in task.dashboard_runs],
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
            talent_string_id = data.get('talent_string_id')
            base_template_id = data.get('base_template_id')
            base_template_content = data.get('base_template_content') if 'base_template_content' in data else None
            override_action_list = data.get('override_action_list') if 'override_action_list' in data else None

            # 新版字段
            fight_style = data.get('fight_style')
            fight_time = data.get('time')
            target_count = data.get('target_count')
            enemy_initial_health_percentage = data.get('enemy_initial_health_percentage')
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
            use_ptr = data.get('use_ptr', False)
            if type(use_ptr) is not bool:
                return JsonResponse({'success': False, 'error': 'use_ptr 必须是布尔值'}, status=400)

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
                profile_fields['use_ptr'] = use_ptr
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
            simulation_params['enemy_initial_health_percentage'] = (
                enemy_initial_health_percentage if enemy_initial_health_percentage is not None else 100
            )
            if 'raid_buffs' in data:
                simulation_params['raid_buffs'] = data['raid_buffs']
            if 'use_class_raid_buff' in data:
                simulation_params['use_class_raid_buff'] = data['use_class_raid_buff']
            if 'extra_options' in data:
                simulation_params['extra_options'] = data['extra_options']
            if 'additional_simc_input' in data:
                simulation_params['additional_simc_input'] = str(data['additional_simc_input'] or '').strip()
            if 'profile_overrides' in data:
                raw_overrides = data['profile_overrides']
                if not isinstance(raw_overrides, dict):
                    return JsonResponse({'success': False, 'error': 'profile_overrides 必须是对象'}, status=400)
                allowed_overrides = {'flask', 'potion', 'food', 'augmentation', 'temporary_enchant', 'talents', 'class_talents', 'spec_talents', 'hero_talents'}
                profile_overrides = {}
                for key, value in raw_overrides.items():
                    if key in allowed_overrides and str(value or '').strip():
                        value = str(value).strip()
                        if '\n' in value or '\r' in value or '=' in value:
                            return JsonResponse({'success': False, 'error': f'{key} 覆盖值格式无效'}, status=400)
                        profile_overrides[key] = value
                if profile_overrides:
                    simulation_params['profile_overrides'] = profile_overrides
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
                        talent_string_id=talent_string_id,
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
                        talent_string_id=talent_string_id,
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


class SimcComparisonTaskAPIView(DashboardPermissionRequiredMixin, View):
    """Create one self-describing comparison Task with multiple candidate Runs."""
    dashboard_permission = 'simc.workflow'

    MAX_TASKS = 8
    MAX_ATTRIBUTE_TASKS = 13
    ATTRIBUTE_STATS = SIMC_ATTRIBUTE_STATS
    ATTRIBUTE_SEARCH_STEP = SIMC_ATTRIBUTE_SEARCH_STEP
    ATTRIBUTE_SEARCH_STEPS = SIMC_ATTRIBUTE_SEARCH_STEPS
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
        """Return one server-owned pairwise neighborhood for an allowed search step."""
        try:
            step = int(step if step is not None else cls.ATTRIBUTE_SEARCH_STEP)
        except (TypeError, ValueError):
            raise ValueError('属性寻优步长无效')
        return attribute_variants(
            values, round_number=round_number, mark_base=mark_base, step=step,
        )

    @staticmethod
    def _run_result_file(run):
        artifact = run.artifacts.filter(artifact_type='html_report').order_by('-created_at').first()
        if artifact:
            return str(artifact.file_path or '').strip()
        params = run.candidate_params if isinstance(run.candidate_params, dict) else {}
        value = str(params.get('legacy_result_file') or '').strip()
        return value if value and '/' not in value and '\\' not in value and '\n' not in value else ''

    def get(self, request):
        """Return one comparison/attribute task with safe run summaries."""
        try:
            task_id = str(request.GET.get('task_id') or '').strip()
            if not task_id:
                return JsonResponse({'success': False, 'error': 'task_id不能为空'}, status=400)
            try:
                task = SimcTask.objects.only(
                    'id', 'name', 'mode', 'current_status', 'create_time', 'completed_at',
                ).get(
                    id=int(task_id), is_active=True,
                    mode__in=('comparison', 'attribute_sweep'),
                )
            except (TypeError, ValueError, SimcTask.DoesNotExist):
                return JsonResponse({'success': False, 'error': '任务不存在或无权限访问'}, status=404)
            runs = list(task.simulation_runs.only(
                'id', 'sequence', 'candidate_label', 'round_number', 'status',
                'result_summary', 'started_at', 'completed_at',
            ).order_by('sequence'))
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

    @method_decorator(login_required)
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
                    explicit_ratings = {}
                    for stat in self.ATTRIBUTE_STATS:
                        value = raw_fields.get(f'gear_{stat}', raw_fields.get(f'gear_{stat}_rating'))
                        if value is not None:
                            explicit_ratings[stat] = self._int(value, stat)
                    # An Addon export normally supplies equipped items, not gear_*
                    # totals. Missing ratings are unknown, never zero: discover the
                    # full baseline through the existing equipment report probe.
                    if len(explicit_ratings) == len(self.ATTRIBUTE_STATS):
                        attribute_baseline_values = explicit_ratings
                    profile_fields = {
                        'name': f'本次 SimC 导入 · {target_spec}', 'spec': target_spec,
                        'player_config_mode': (
                            'attribute_only' if attribute_baseline_values is not None else 'manual_equipment'
                        ),
                        'player_equipment': baseline,
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
                    raise ValueError(
                        f'四属性自动寻优从 {self.ATTRIBUTE_SEARCH_STEP} 绿字步长开始并自动缩小精度'
                    )
                if mode == 'manual_equipment':
                    specs.append({
                        'label': '基准属性探测',
                        'is_base': True,
                        'candidate': {
                            'type': 'attribute_baseline_probe',
                            'algorithm': 'four_stat_pairwise_hill_climb',
                            'algorithm_version': 3,
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
            if 'extra_options' in data:
                simulation_params['extra_options'] = data['extra_options']
            if 'additional_simc_input' in data:
                simulation_params['additional_simc_input'] = str(data['additional_simc_input'] or '').strip()
            if 'profile_overrides' in data:
                raw_overrides = data['profile_overrides']
                if not isinstance(raw_overrides, dict):
                    raise ValueError('profile_overrides 必须是对象')
                allowed_overrides = {'flask', 'potion', 'food', 'augmentation', 'temporary_enchant', 'talents', 'class_talents', 'spec_talents', 'hero_talents'}
                profile_overrides = {}
                for key, value in raw_overrides.items():
                    if key in allowed_overrides and str(value or '').strip():
                        value = str(value).strip()
                        if '\n' in value or '\r' in value or '=' in value:
                            raise ValueError(f'{key} 覆盖值格式无效')
                        profile_overrides[key] = value
                if profile_overrides:
                    simulation_params['profile_overrides'] = profile_overrides
            option_error = validate_simulation_options(simulation_params)
            if option_error:
                raise ValueError(option_error)
            task = create_task_from_request(
                user_id=request.user.id, profile_fields=profile_fields,
                base_template_id=base_template_id, selected_apl_id=selected_apl_id,
                talent_string_id=data.get('talent_string_id'),
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
                'accepted': len((task.mode_params or {}).get('initial_candidates') or []),
                'excluded_candidates': (task.mode_params or {}).get('excluded_candidates') or [],
            }})
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': '无效的JSON数据'})
        except ValueError as e:
            return JsonResponse({'success': False, 'error': str(e)})
        except Exception as e:
            logger.error(f'创建 SimC 比较任务失败: {e}\n{traceback.format_exc()}')
            return JsonResponse({'success': False, 'error': f'创建比较任务失败: {e}'})


@method_decorator(login_required, name='dispatch')
class SimcPlayerConfigDetailAPIView(DashboardPermissionRequiredMixin, View):
    """只解析工作台当前玩家输入，返回结构化配置详情；不渲染完整 SimC 执行文本。"""
    dashboard_permission = 'simc.workflow'

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


