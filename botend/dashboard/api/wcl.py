"""Dashboard API submodule: wcl."""
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


from ._shared import _fmt_dt


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
            logger.warning(f"WCL OAuth请求失败: {type(e).__name__}")
            return None

    def _wcl_graphql(self, token, query, variables):
        from botend.services.wcl_api_budget import WclTransportError, graphql_request

        self._wcl_retry_at = None
        self._wcl_last_error = ''
        client_id, _ = self._get_wcl_api_credentials()
        result = graphql_request(client_id, token, query, variables)
        self._wcl_last_error, self._wcl_retry_at = result.error, result.retry_at
        if result.error:
            raise WclTransportError(result)
        return result.payload

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


