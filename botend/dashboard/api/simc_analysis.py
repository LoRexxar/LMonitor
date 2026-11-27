"""Dashboard API submodule: simc_analysis."""
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


from .simc_tasks import SimcComparisonTaskAPIView, SimcTaskAPIView


@method_decorator(csrf_exempt, name='dispatch')
class SimcTaskPreviewAPIView(DashboardPermissionRequiredMixin, View):
    """Return a structured, display-safe snapshot of a task manifest only."""
    dashboard_permission = 'simc.workflow'

    def get(self, request):
        task_id = request.GET.get('task_id')
        try:
            task = SimcTask.objects.get(id=task_id, is_active=True)
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
            profile = SimcProfile.objects.filter(id=task.simc_profile_id).first()
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


@method_decorator(csrf_exempt, name='dispatch')
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
            
            # 只允许任务中精确登记的结果文件，禁止借代理读取任意 OSS/local 文件。
            requested_files = [part.strip() for part in str(result_file).split(',') if part.strip()]
            if len(requested_files) != 1 or requested_files[0] != result_file.strip() or '/' in result_file or '\\' in result_file:
                return JsonResponse({'success': False, 'error': '结果文件名无效'})
            legacy_tasks = SimcTask.objects.filter(
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


@method_decorator(csrf_exempt, name='dispatch')
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
            
            try:
                task = SimcTask.objects.get(id=task_id, is_active=True)
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


@method_decorator(csrf_exempt, name='dispatch')
class SimcRegularCompareAPIView(View):
    """
    常规模拟对比API - 解析多个任务的结果文件并返回可对比数据
    """

    @staticmethod
    def _safe_attribute_report(attribute_report):
        if not isinstance(attribute_report, dict):
            return None
        safe_candidate_fields = ('id', 'label', 'round', 'step', 'is_center', 'ratings', 'dps')
        safe_marginal_fields = (
            'run_id', 'stat', 'amount', 'ratings', 'baseline_dps', 'dps',
            'dps_error', 'dps_gain', 'gain_percent',
        )

        def safe_candidate(value):
            if not isinstance(value, dict):
                return None
            return {key: value.get(key) for key in safe_candidate_fields if key in value}

        safe = {
            key: attribute_report.get(key)
            for key in (
                'algorithm', 'algorithm_version', 'step', 'steps', 'tolerance',
                'rounds_completed', 'current_round', 'total_rating',
                'initial_ratings', 'stop_reason', 'converged', 'local_optimum',
                'marginal_gain_status', 'marginal_gain_round',
                'marginal_gain_amounts', 'marginal_gain_baseline_dps',
            )
        }
        safe['recommendation'] = safe_candidate(attribute_report.get('recommendation'))
        safe['search_path'] = [{
            key: point.get(key) for key in ('round', 'step', 'ratings', 'dps') if key in point
        } for point in attribute_report.get('search_path', []) if isinstance(point, dict)]
        safe['candidates'] = [
            safe_candidate(value) for value in attribute_report.get('candidates', [])
            if isinstance(value, dict)
        ]
        safe['marginal_gains'] = [
            {key: value.get(key) for key in safe_marginal_fields if key in value}
            for value in attribute_report.get('marginal_gains', [])
            if isinstance(value, dict)
        ]
        return safe

    def _build_reference_attribute_report(self, runs, analysis_result=None):
        """Build an attribute report from frozen run candidate parameters."""
        run_rows = []
        for run in runs:
            params = run.candidate_params if isinstance(run.candidate_params, dict) else {}
            search = params.get('search') if isinstance(params.get('search'), dict) else {}
            if search.get('type') == 'attribute_marginal_gain':
                continue
            run_rows.append((run, params))
        report = self._build_attribute_report(run_rows)
        analysis = analysis_result if isinstance(analysis_result, dict) else {}
        persisted = analysis.get('attribute_search')
        if not isinstance(persisted, dict) or not isinstance(persisted.get('converged'), bool):
            return report

        for key in (
            'marginal_gain_status', 'marginal_gain_round',
            'marginal_gain_amounts', 'marginal_gain_baseline_dps',
        ):
            if key in persisted:
                report[key] = persisted[key]
        report['marginal_gains'] = [
            row for row in persisted.get('marginal_gains', [])
            if isinstance(row, dict)
        ]
        report['converged'] = persisted['converged']
        report['stop_reason'] = str(persisted.get('stop_reason') or '')
        report['local_optimum'] = (
            persisted['converged']
            and report['stop_reason'].startswith('local_optimum_')
            and report['stop_reason'].endswith('_pairwise')
        )
        persisted_ratings = persisted.get('ratings')
        if isinstance(persisted_ratings, dict):
            persisted_round = persisted.get('round')
            recommendation = next(
                (
                    row for row in report['all_candidates']
                    if row.get('ratings') == persisted_ratings
                    and (
                        persisted_round in (None, '')
                        or row.get('round') == persisted_round
                    )
                ),
                None,
            )
            if recommendation is None:
                recommendation = {
                    'round': persisted_round,
                    'step': persisted.get('step'),
                    'ratings': persisted_ratings,
                    'dps': persisted.get('dps'),
                }
            else:
                recommendation = {**recommendation, 'dps': persisted.get('dps')}
            report['recommendation'] = recommendation
        return report

    def _build_attribute_report(self, run_candidates):
        """Return a truthful report for the measured progressive pairwise search."""
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
                'round': round_number, 'step': candidate.get('step'),
                'algorithm_version': candidate.get('algorithm_version'),
                'is_center': bool(params.get('is_base')),
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
        current_steps = {
            int(row['step']) for row in current
            if row.get('step') in SimcComparisonTaskAPIView.ATTRIBUTE_SEARCH_STEPS
        }
        current_step = (
            next(iter(current_steps)) if len(current_steps) == 1
            else SimcComparisonTaskAPIView.ATTRIBUTE_SEARCH_STEP
        )
        center = next((row for row in current if row['is_center']), None)
        current_complete = bool(current) and all(row['dps'] is not None for row in current)
        recommendation = ranked[0] if ranked else None
        stop_reason = 'awaiting_current_round'
        if current_complete and center:
            best_neighbor = max((row for row in current if not row['is_center']), key=lambda row: row['dps'], default=None)
            recommendation = best_neighbor if best_neighbor and best_neighbor['dps'] > center['dps'] + tolerance else center
            stop_reason = '' if recommendation is not center else (
                f'local_optimum_{current_step}_pairwise'
                if current_step == SimcComparisonTaskAPIView.ATTRIBUTE_SEARCH_STEPS[-1]
                else 'refining_step'
            )
        path = [
            {
                'round': row['round'], 'step': row.get('step'),
                'ratings': row['ratings'], 'dps': row['dps'],
                'result_file': row['result_file'],
            }
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
                round_steps = {
                    int(row['step']) for row in round_rows
                    if int(row['step']) in SimcComparisonTaskAPIView.ATTRIBUTE_SEARCH_STEPS
                }
                if len(round_steps) != 1:
                    continue
                round_step = next(iter(round_steps))
                expected = SimcComparisonTaskAPIView._attribute_variants(
                    round_centers[0]['ratings'], round_step,
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
        algorithm_versions = [
            int(row['algorithm_version']) for row in candidates
            if isinstance(row.get('algorithm_version'), int)
        ]
        return {
            'algorithm': 'four_stat_pairwise_hill_climb',
            'algorithm_version': max(algorithm_versions or [3]),
            'step': current_step,
            'steps': list(SimcComparisonTaskAPIView.ATTRIBUTE_SEARCH_STEPS),
            'tolerance': tolerance, 'rounds_completed': completed_rounds,
            'current_round': current_round, 'total_rating': sum(first_center['ratings'].values()) if first_center else None,
            'initial_ratings': first_center['ratings'] if first_center else {},
            'recommendation': recommendation, 'stop_reason': stop_reason,
            'converged': stop_reason not in (
                '', 'awaiting_current_round', 'refining_step',
            ),
            'local_optimum': (
                stop_reason.startswith('local_optimum_')
                and stop_reason.endswith('_pairwise')
            ),
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
        """Return the compact, reproducible equipment identity used by multi-task comparison."""
        item = item if isinstance(item, dict) else {}
        details = []
        if item.get('item_id') is not None:
            details.append(f"ID {item['item_id']}")
        if item.get('item_level') is not None:
            details.append(f"装等 {item['item_level']}")
        return ' · '.join(details) if details else '—'

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

    @staticmethod
    def _apl_action_localizations(baseline):
        """Return only authoritative APL action token → Chinese labels for a frozen spec."""
        character = baseline.get('character') if isinstance(baseline, dict) else {}
        character = character if isinstance(character, dict) else {}
        raw_class = str(character.get('class') or '').strip().lower()
        raw_spec = str(character.get('spec') or '').strip().lower()
        if '_' in raw_spec:
            class_name, spec = raw_spec.split('_', 1)
        elif raw_class and raw_spec:
            # Frozen SimC reports expose canonical class and spec as separate fields.
            class_name, spec = raw_class, raw_spec
        else:
            return {}
        localizations = {}
        for symbol in query_visible_symbols(class_name, spec):
            if symbol.symbol_kind not in (SimcAplSymbol.KIND_ACTION, SimcAplSymbol.KIND_PSEUDO_ACTION):
                continue
            chinese = str(symbol.name_zh or '').strip()
            if chinese:
                localizations[str(symbol.token or '').strip().lower()] = chinese
        return localizations

    @staticmethod
    def _cast_timeline_action_key(value):
        """Normalize only presentation separators; collisions remain unresolved."""
        return re.sub(r'[\s_-]+', '', str(value or '').strip().casefold())

    @classmethod
    def _cast_timeline_spell_metadata(cls, frozen_baseline, sequence):
        """Resolve displayed Sample Sequence actions to audited APL SpellIDs.

        Report actions have no spell ID of their own.  We therefore accept only a
        unique match in the frozen actor's visible APL action catalog; aliases,
        fuzzy names and conflicting IDs deliberately remain unresolved.
        """
        character = frozen_baseline.get('character') if isinstance(frozen_baseline, dict) else {}
        character = character if isinstance(character, dict) else {}
        raw_class = str(character.get('class') or '').strip().lower()
        raw_spec = str(character.get('spec') or '').strip().lower()
        if '_' in raw_spec:
            class_name, spec = raw_spec.split('_', 1)
        elif raw_class and raw_spec:
            class_name, spec = raw_class, raw_spec
        else:
            return {}

        wanted = {
            cls._cast_timeline_action_key(row.get('action_en') or row.get('action'))
            for row in sequence if isinstance(row, dict)
        }
        wanted.discard('')
        if not wanted:
            return {}

        candidates = defaultdict(set)
        for symbol in query_visible_symbols(class_name, spec):
            if symbol.symbol_kind != SimcAplSymbol.KIND_ACTION or not symbol.spell_id:
                continue
            key = cls._cast_timeline_action_key(symbol.token)
            if key in wanted:
                candidates[key].add(symbol.spell_id)
        bindings = {
            key: next(iter(spell_ids)) for key, spell_ids in candidates.items()
            if len(spell_ids) == 1
        }
        if not bindings:
            return {}

        rows = WowSpellSnapshot.objects.filter(
            branch='wow', spell_id__in=set(bindings.values()), locale__in=('zhCN', 'enUS'),
        ).order_by('-updated_at')
        snapshots = defaultdict(dict)
        for row in rows:
            snapshots[row.spell_id].setdefault(row.locale, row)

        metadata = {}
        for key, spell_id in bindings.items():
            zh = snapshots[spell_id].get('zhCN')
            en = snapshots[spell_id].get('enUS')
            primary = zh or en
            if primary is None:
                continue
            icon = str((zh and zh.icon) or (en and en.icon) or '').strip()
            metadata[key] = {
                'spell_id': spell_id,
                'name': str((zh and (zh.name_zh or zh.name)) or (en and en.name) or '').strip(),
                'name_en': str((en and en.name) or (zh and zh.name) or '').strip(),
                'description': str((zh and (zh.description or zh.aura_description)) or
                    (en and (en.description or en.aura_description)) or '').strip(),
                'icon_url': wow_icon_oss_url(icon) if icon else '',
            }
        return metadata

    @classmethod
    def _safe_sample_sequence(cls, sequence, spell_metadata=None):
        """Return a bounded, display-only action sequence with numeric time coordinates."""
        if not isinstance(sequence, list):
            return []

        def safe_text(value, maximum):
            return ' '.join(str(value or '').split())[:maximum]

        safe_rows = []
        for row in sequence[:1200]:
            if not isinstance(row, dict):
                continue
            raw_time = row.get('time')
            if raw_time is None or str(raw_time).strip() == '':
                continue
            time_label = safe_text(raw_time, 32)
            if time_label.lower() == 'pre':
                time_seconds = 0.0
            else:
                clock_match = re.fullmatch(r'(\d+):(\d+(?:\.\d+)?)', time_label)
                try:
                    time_seconds = (
                        int(clock_match.group(1)) * 60 + float(clock_match.group(2))
                        if clock_match else float(time_label)
                    )
                except (TypeError, ValueError):
                    continue
            if not math.isfinite(time_seconds):
                continue
            action = safe_text(row.get('action'), 300)
            if not action:
                continue
            action_key = cls._cast_timeline_action_key(row.get('action_en') or action)
            spell = (spell_metadata or {}).get(action_key) or {}
            safe_rows.append({
                'time_seconds': time_seconds,
                'time_label': safe_text(raw_time, 32),
                'action': action,
                'action_en': safe_text(row.get('action_en'), 300),
                'spell': {
                    'spell_id': spell.get('spell_id'),
                    'name': safe_text(spell.get('name'), 300),
                    'name_en': safe_text(spell.get('name_en'), 300),
                    'description': safe_text(spell.get('description'), 1200),
                    'icon_url': safe_text(spell.get('icon_url'), 1000),
                } if spell else {},
                'action_list': safe_text(row.get('action_list'), 100),
                'target': safe_text(row.get('target'), 300),
                'resources': safe_text(row.get('resources'), 300),
                'buffs': safe_text(row.get('buffs'), 1000),
            })
        return safe_rows

    def _get_multi_task_payload(self, request, task_ids):
        """Build a safe comparison report from selected ordinary simulation tasks."""
        tasks = list(SimcTask.objects.filter(
            id__in=task_ids, is_active=True,
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
            report_artifact = next(
                (artifact for artifact in run.artifacts.all() if artifact.artifact_type == 'html_report'),
                None,
            )
            if report_artifact:
                from botend.services.simc_result_analysis import analyze_run_artifact
                parsed = analyze_run_artifact(task, report_artifact) or {}
            if not parsed and result_file:
                content = self._get_result_file_content(result_file)
                parsed = self._parse_regular_result(content) if content else {}
            dps = summary.get('dps') or parsed.get('dps')
            if not isinstance(dps, (int, float)):
                invalid.append({'id': task.id, 'name': task.name, 'error': '无法解析已完成结果的 DPS'})
                continue
            raw_sequence = parsed.get('sample_sequence')
            spell_metadata = self._cast_timeline_spell_metadata(frozen_baseline, raw_sequence)
            rows.append({
                'id': task.id, 'name': task.name, 'label': task.name,
                'is_base': not rows, 'is_base_candidate': not rows,
                'dps': dps, 'candidate_name': '',
                'character': parsed.get('character') or frozen_baseline.get('character') or {},
                'simulation': parsed.get('simulation') or frozen_baseline.get('simulation_params') or {},
                'talents': parsed.get('talents') or {'string': frozen_baseline.get('_talent') or ''},
                'abilities': [
                    {
                        'name': ability.get('name', ''),
                        'dps': ability.get('dps', ''),
                        'dps_percent': ability.get('dps_percent', ''),
                    }
                    for ability in parsed.get('abilities', parsed.get('top_abilities', []))
                ],
                'top_abilities': parsed.get('top_abilities', []),
                'sample_sequence': self._safe_sample_sequence(raw_sequence, spell_metadata),
                'action_localizations': self._apl_action_localizations(frozen_baseline),
                'apl_name': apl_name, 'profile_name': profile_name,
                'spec': str(profile_payload.get('spec') or getattr(task.profile, 'spec', '') or ''),
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
            differences = [] if row is baseline else self._frozen_input_differences(
                baseline_input_facts, current_input_facts,
            )
            row['input_differences'] = differences
            row['input_difference_summary'] = (
                '对比基准' if row is baseline
                else (f'{len(differences)} 项输入不同' if differences else '与基准输入一致')
            )
            row['config_facts'] = {
                key: {
                    'label': str((current_input_facts.get(key) or baseline_fact).get('label') or key),
                    'display': str((current_input_facts.get(key) or {}).get('display') or '—'),
                    'value': current_input_facts.get(key, {}).get('value'),
                    'is_different': False,
                }
                for key, baseline_fact in baseline_input_facts.items()
            }
            for key, current_fact in current_input_facts.items():
                if key not in row['config_facts']:
                    row['config_facts'][key] = {
                        'label': str(current_fact.get('label') or key),
                        'display': str(current_fact.get('display') or '—'),
                        'value': current_fact.get('value'),
                        'is_different': False,
                    }
        # A highlighted cell means its configuration row contains multiple frozen
        # values, not merely that it differs from the first selected task.  This
        # includes the baseline itself when every selected APL is different.
        fact_keys = {key for row in rows for key in row['config_facts']}
        for key in fact_keys:
            values = {
                json.dumps(row['config_facts'][key].get('value'), sort_keys=True, default=str)
                for row in rows if key in row['config_facts']
            }
            changed = len(values) > 1
            for row in rows:
                fact = row['config_facts'].get(key)
                if fact:
                    fact['is_different'] = changed
                    fact.pop('value', None)
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
                    id=int(task_id), is_active=True,
                    mode__in=('comparison', 'attribute_sweep'),
                )
            except (TypeError, ValueError, SimcTask.DoesNotExist):
                return JsonResponse({'success': False, 'error': '比较任务不存在'}, status=404)
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
        from botend.services.simc_result_analysis import parse_simc_html_report

        return parse_simc_html_report(html_content)


