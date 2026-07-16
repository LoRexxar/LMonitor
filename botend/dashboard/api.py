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
from django.http import JsonResponse, HttpResponse, FileResponse
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
from botend.models import MonitorTask, PortalPeakSpecRankRow, SimcAplKeywordPair, UserAplStorage, SimcTask, SimcTaskBatch, SimcTaskArtifact, SimcProfile, SimcSecondaryStatRule, SimcMasteryCoefficient, SimcContentTemplate, SimcBackendBinary, WclAnalysisTask, SystemAlert, WowDailyReport, WowHotfixReport, WowWagoHotfixEvent, WowWagoMonitorState
from botend.alerting import upsert_system_alert
from django.db import models, transaction
from core.glm import GLMClient
from botend.monitor_env import is_task_runnable, env_limit_hint
from botend.wow_daily_report.generator import generate_wow_daily_report
from botend.services.simc_attribute_results import parse_attribute_result_filename
from botend.services.simc_player_config import EQUIPMENT_SLOT_ALIASES, resolve_attribute_player_baseline, validate_player_baseline
from botend.services.simc_composer import SimcComposer
from botend.services.battlenet_preflight import fetch_battlenet_character_preflight
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor


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
class SystemAlertAPIView(View):
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


@method_decorator([csrf_exempt, login_required], name='dispatch')
class ConvertTextAPIView(View):
    """
    SimC APL文本转换API
    """
    
    def post(self, request):
        try:
            # 解析请求数据
            data = json.loads(request.body)
            text = data.get('text', '').strip()
            conversion_type = data.get('conversion_type', '')
            
            if not text:
                return JsonResponse({
                    'success': False,
                    'error': '输入文本不能为空'
                })
            
            if conversion_type not in ['apl_to_cn', 'cn_to_apl']:
                return JsonResponse({
                    'success': False,
                    'error': '无效的转换类型'
                })
            
            # 执行转换
            if conversion_type == 'apl_to_cn':
                result = self.convert_apl_to_cn(text)
            else:
                result = self.convert_cn_to_apl(text)
            
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
    
    def convert_apl_to_cn(self, text):
        """
        将APL关键字转换为中文
        """
        try:
            # 获取所有关键字对
            keyword_pairs = SimcAplKeywordPair.objects.filter(is_active=True)
            
            # 按APL关键字长度降序排列，优先替换更长的关键字
            keyword_pairs = sorted(keyword_pairs, key=lambda x: len(x.apl_keyword), reverse=True)
            
            result = text
            for pair in keyword_pairs:
                # 从APL转换到中文
                result = result.replace(pair.apl_keyword, pair.cn_keyword)
            
            return result
            
        except Exception as e:
            logger.error(f"APL2CN错误: {str(e)}")
            raise e
    
    def convert_cn_to_apl(self, text):
        """
        将中文关键字转换为APL
        """
        try:
            # 获取所有关键字对
            keyword_pairs = SimcAplKeywordPair.objects.filter(is_active=True)
            
            # 按中文关键字长度降序排列，优先替换更长的关键字
            keyword_pairs = sorted(keyword_pairs, key=lambda x: len(x.cn_keyword), reverse=True)
            
            result = text
            for pair in keyword_pairs:
                # 从中文转换到APL
                result = result.replace(pair.cn_keyword, pair.apl_keyword)
            
            return result
            
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
                for p in SimcProfile.objects.filter(id__in=profile_ids, user_id=request.user.id, is_active=True)
                .values('id', 'name', 'spec')
            }
            
            tasks_data = []
            for task in tasks:
                ext_detail = self._task_ext_summary(task.task_type, task.ext)
                profile_info = profile_map.get(task.simc_profile_id) or {}
                tasks_data.append({
                    'id': task.id,
                    'name': task.name,
                    'simc_profile_id': task.simc_profile_id,
                    'simc_profile_name': profile_info.get('name', ''),
                    # New tasks keep their execution spec in ext; only old manifests fall back to the Profile.
                    'simc_profile_spec': ext_detail.get('spec') or profile_info.get('spec', ''),
                    'current_status': task.current_status,
                    'result_file': self._task_result_file_summary(task),
                    'task_type': task.task_type,
                    # 任务列表只需安全的结构化摘要；原始 SimC 文本只能留在执行快照中，
                    # 不得通过列表或前端内嵌 JSON 回显给浏览器。
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
        """创建新的SimC任务"""
        try:
            data = json.loads(request.body)
            name = data.get('name', '').strip()
            simc_profile_id = data.get('simc_profile_id')
            # 原始 SimC 输入是任务执行快照的一部分：保留首尾空白和末尾换行，不能静默改写。
            raw_simc_code = str(data.get('raw_simc_code') or '')
            current_status = data.get('current_status', 0)
            task_type = data.get('task_type', 1)
            ext = data.get('ext', '')
            regular_time = data.get('regular_time')
            regular_target_count = data.get('regular_target_count')
            selected_attributes = data.get('selected_attributes')
            attribute_step = data.get('attribute_step')
            selected_apl_id = data.get('selected_apl_id') or data.get('apl_template_id')
            base_template_id = data.get('base_template_id')
            base_template_content = data.get('base_template_content') if 'base_template_content' in data else None
            override_action_list = data.get('override_action_list') if 'override_action_list' in data else None
            override_action_list_provided = 'override_action_list' in data

            # 新版字段：SimC 工作台只接收"玩家信息块"，完整 simc 由后端模板拼装
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
            
            # 属性型 Profile 只保存天赋与副属性，不要求角色标识或装备行。
            if player_config_mode and player_config_mode not in ('battlenet', 'manual_equipment', 'attribute_only', 'addon_full_export'):
                return JsonResponse({
                    'success': False,
                    'error': '玩家信息导入方式必须是 battlenet、manual_equipment、addon_full_export 或 attribute_only'
                })
            
            if player_config_mode == 'manual_equipment' and not player_equipment:
                return JsonResponse({
                    'success': False,
                    'error': '手动装备模式下玩家装备配置不能为空'
                })
            
            if player_config_mode == 'battlenet':
                if battlenet_region not in ('us', 'eu', 'kr', 'tw', 'cn') or not battlenet_realm or not battlenet_character:
                    return JsonResponse({
                        'success': False,
                        'error': 'Battle.net 导入需要提供 region、realm 和 character'
                    })
            
            # 如果提供了 simc_profile_id，用 profile 填充缺失字段
            if simc_profile_id:
                try:
                    profile = SimcProfile.objects.get(
                        id=simc_profile_id,
                        user_id=request.user.id,
                        is_active=True
                    )
                    if not spec:
                        spec = profile.spec
                    if not talent:
                        talent = profile.talent
                    if not player_equipment:
                        player_equipment = str(getattr(profile, 'player_equipment', '') or '').strip()
                    if gear_strength is None:
                        gear_strength = profile.gear_strength
                    if gear_crit is None:
                        gear_crit = profile.gear_crit
                    if gear_haste is None:
                        gear_haste = profile.gear_haste
                    if gear_mastery is None:
                        gear_mastery = profile.gear_mastery
                    if gear_versatility is None:
                        gear_versatility = profile.gear_versatility
                except SimcProfile.DoesNotExist:
                    return JsonResponse({
                        'success': False,
                        'error': '指定的SimC配置不存在'
                    })

            if player_config_mode == 'attribute_only':
                try:
                    player_equipment = resolve_attribute_player_baseline(spec, player_equipment)
                except ValueError as e:
                    return JsonResponse({'success': False, 'error': str(e)})
            
            # 直接 SimC 代码模式：仅常规模拟允许不选 profile；属性模拟仍必须基于 profile。
            if raw_simc_code and int(task_type or 1) == 2:
                return JsonResponse({
                    'success': False,
                    'error': '直接 SimC 代码不支持属性模拟，请选择 SimC 配置后再运行属性模拟'
                })
            if raw_simc_code and int(task_type or 1) == 1:
                simc_profile_id = 0  # 占位
            elif not simc_profile_id:
                # 新版模式：如果提供了 player_config_mode，不需要 profile
                if not player_config_mode:
                    return JsonResponse({
                        'success': False,
                        'error': 'SimC配置不能为空'
                    })

            if int(task_type or 1) == 2:
                valid_combinations = {
                    'crit_mastery', 'crit_haste', 'crit_versatility',
                    'mastery_haste', 'mastery_versatility', 'haste_versatility',
                    'haste_mastery',
                }
                if str(selected_attributes or '').strip() not in valid_combinations:
                    return JsonResponse({'success': False, 'error': '属性模拟需要选择两项有效副属性'})
                try:
                    attribute_step = int(attribute_step) if attribute_step not in (None, '') else 50
                except (TypeError, ValueError):
                    return JsonResponse({'success': False, 'error': '属性模拟步长必须是整数'})
                if attribute_step != 50:
                    return JsonResponse({'success': False, 'error': '四属性自动寻优固定使用 50 绿字步长'})

            # 生成result_file：player_config_mode 新流程由 SimC 执行后自动检测，
            # 不预生成，避免预生成的文件名与 SimC 实际输出不一致。
            if player_config_mode:
                result_file = ''
            else:
                timestamp = str(int(time.time()))
                content_to_hash = timestamp + name + str(request.user.id)
                result_file = hashlib.md5(content_to_hash.encode('utf-8')).hexdigest() + '.html'

            normalized_ext = self._build_task_ext(
                task_type=task_type,
                ext=ext,
                owner_user_id=request.user.id,
                regular_time=regular_time,
                regular_target_count=regular_target_count,
                selected_attributes=selected_attributes,
                attribute_step=attribute_step,
                raw_simc_code=raw_simc_code,
                selected_apl_id=selected_apl_id,
                base_template_id=base_template_id,
                base_template_content=base_template_content,
                override_action_list=override_action_list,
                override_action_list_provided=override_action_list_provided,
                # 新版字段
                fight_style=fight_style,
                time=fight_time,
                target_count=target_count,
                player_config_mode=player_config_mode,
                player_equipment=player_equipment,
                gear_strength=gear_strength,
                gear_crit=gear_crit,
                gear_haste=gear_haste,
                gear_mastery=gear_mastery,
                gear_versatility=gear_versatility,
                talent=talent,
                spec=spec,
                battlenet_region=battlenet_region,
                battlenet_realm=battlenet_realm,
                battlenet_character=battlenet_character
            )

            # Phase 1: Use composer to generate frozen final_simc_content for new tasks
            final_simc_content = None
            input_hash = ''
            fragment_manifest = None

            if player_config_mode and int(task_type or 1) == 1:
                timestamp = str(int(time.time()))
                content_to_hash = timestamp + name + str(request.user.id)
                result_file_name = hashlib.md5(content_to_hash.encode('utf-8')).hexdigest() + '.html'
                # Keep the frozen html= target identical to SimcTask.result_file.
                # The worker resolves this relative name under simc_results/.
                result_file_path = result_file_name

                # For battlenet mode, fetch server-side preflight
                server_preflight = None
                if player_config_mode == 'battlenet' and battlenet_region and battlenet_realm and battlenet_character:
                    try:
                        preflight_result = fetch_battlenet_character_preflight(
                            region=battlenet_region,
                            realm=battlenet_realm,
                            character=battlenet_character,
                            requested_spec=spec
                        )
                        # Convert preflight to server_preflight structure
                        server_preflight = {
                            'character': {
                                'class': preflight_result.get('identity', {}).get('class_name', ''),
                                'spec': preflight_result.get('spec', {}).get('key', ''),
                                'level': preflight_result.get('identity', {}).get('level', 80),
                            }
                        }
                        # If preflight has warnings, reject early
                        if not preflight_result.get('simc_ready'):
                            return JsonResponse({
                                'success': False,
                                'error': '角色信息不完整：' + '；'.join(preflight_result.get('warnings', []))
                            })
                    except Exception as e:
                        return JsonResponse({
                            'success': False,
                            'error': f'Battle.net 预检失败: {str(e)}'
                        })

                # Build request data for composer
                composer_request = {
                    'spec': spec,
                    'player_import_mode': player_config_mode,
                    'player_equipment': player_equipment,
                    'talent': talent,
                    'fight_style': fight_style or 'Patchwerk',
                    'time': fight_time or 300,
                    'target_count': target_count or 1,
                    'gear_crit': gear_crit,
                    'gear_haste': gear_haste,
                    'gear_mastery': gear_mastery,
                    'gear_versatility': gear_versatility,
                    'selected_apl_id': selected_apl_id,
                    'override_action_list': override_action_list,
                    'base_template_id': base_template_id,
                    'base_template_content': base_template_content,
                    'battlenet_region': battlenet_region,
                    'battlenet_realm': battlenet_realm,
                    'battlenet_character': battlenet_character,
                    '_result_file_path': result_file_path,
                    '_server_preflight': server_preflight,
                }

                # Compose final content
                composer = SimcComposer(user_id=request.user.id)
                final_content, manifest, error = composer.compose(composer_request)

                if error:
                    return JsonResponse({'success': False, 'error': error})

                final_simc_content = final_content
                fragment_manifest = manifest.to_json() if manifest else None
                input_hash = SimcComposer.compute_input_hash(final_content)
                result_file = result_file_name

            # 创建新任务
            task = SimcTask.objects.create(
                user_id=request.user.id,
                name=name,
                simc_profile_id=simc_profile_id or 0,
                current_status=current_status,
                result_file=result_file,
                task_type=task_type,
                ext=normalized_ext,
                final_simc_content=final_simc_content,
                input_hash=input_hash,
                fragment_manifest=fragment_manifest
            )
            
            return JsonResponse({
                'success': True,
                'message': 'SimC任务创建成功',
                'data': {
                    'id': task.id,
                    'name': task.name,
                    'simc_profile_id': task.simc_profile_id,
                    'current_status': task.current_status,
                    'result_file': self._task_result_file_summary(task),
                    'task_type': task.task_type,
                    'ext_detail': self._task_ext_summary(task.task_type, task.ext),
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
        """更新SimC任务"""
        try:
            data = json.loads(request.body)
            task_id = data.get('id')
            name = data.get('name', '').strip()
            simc_profile_id = data.get('simc_profile_id')
            raw_simc_code = data.get('raw_simc_code', '').strip()
            current_status = data.get('current_status', 0)
            task_type = data.get('task_type', 1)
            ext = data.get('ext', '')
            regular_time = data.get('regular_time')
            regular_target_count = data.get('regular_target_count')
            selected_attributes = data.get('selected_attributes')
            attribute_step = data.get('attribute_step')
            selected_apl_id = data.get('selected_apl_id') or data.get('apl_template_id')
            
            if not task_id:
                return JsonResponse({
                    'success': False,
                    'error': '任务ID不能为空'
                })
            
            if not name:
                return JsonResponse({
                    'success': False,
                    'error': '任务名称不能为空'
                })
            
            # 先按当前用户取得任务；之后所有编辑和 manifest 判定均以它为准。
            try:
                task = SimcTask.objects.get(id=task_id, user_id=request.user.id, is_active=True)
            except SimcTask.DoesNotExist:
                return JsonResponse({'success': False, 'error': '任务不存在或无权限访问'})

            # 新版运行 manifest 是 Worker 的可信执行参数：普通编辑仅允许改显示名称。
            existing_ext = self._normalize_task_ext(task.task_type, task.ext)
            if existing_ext.get('player_config_mode'):
                task.name = name
                task.save(update_fields=['name', 'modified_time'])
                return JsonResponse({
                    'success': True,
                    'message': '任务名称更新成功；运行配置由创建时快照保护，请使用重跑操作重新执行。',
                    'data': {
                        'id': task.id,
                        'name': task.name,
                        'simc_profile_id': task.simc_profile_id,
                        'current_status': task.current_status,
                        'result_file': self._task_result_file_summary(task),
                        'task_type': task.task_type,
                        'ext_detail': self._task_ext_summary(task.task_type, task.ext),
                        'create_time': _fmt_dt(task.create_time),
                        'modified_time': _fmt_dt(task.modified_time),
                    }
                })

            # 旧版任务才允许更新其运行字段。
            if raw_simc_code and int(task_type or 1) == 2:
                return JsonResponse({'success': False, 'error': '直接 SimC 代码不支持属性模拟，请选择 SimC 配置后再运行属性模拟'})
            if raw_simc_code and int(task_type or 1) == 1:
                simc_profile_id = 0
            elif not simc_profile_id:
                return JsonResponse({'success': False, 'error': 'SimC配置不能为空'})
            if not (raw_simc_code and int(task_type or 1) == 1):
                try:
                    SimcProfile.objects.get(id=simc_profile_id, user_id=request.user.id, is_active=True)
                except SimcProfile.DoesNotExist:
                    return JsonResponse({'success': False, 'error': '指定的SimC配置不存在'})

            normalized_ext = self._build_task_ext(
                task_type=task_type, ext=ext, regular_time=regular_time,
                owner_user_id=request.user.id,
                regular_target_count=regular_target_count, selected_attributes=selected_attributes,
                attribute_step=attribute_step, raw_simc_code=raw_simc_code,
                selected_apl_id=selected_apl_id,
                fight_style=fight_style,
                time=fight_time,
                target_count=target_count,
                player_config_mode=player_config_mode,
                player_equipment=player_equipment,
                gear_strength=gear_strength,
                gear_crit=gear_crit,
                gear_haste=gear_haste,
                gear_mastery=gear_mastery,
                gear_versatility=gear_versatility,
                talent=talent,
                spec=spec,
                battlenet_region=battlenet_region,
                battlenet_realm=battlenet_realm,
                battlenet_character=battlenet_character,
            )
            task.name = name
            task.simc_profile_id = simc_profile_id
            task.current_status = current_status
            task.task_type = task_type
            task.ext = normalized_ext
            task.save()
            
            return JsonResponse({
                'success': True,
                'message': 'SimC任务更新成功',
                'data': {
                    'id': task.id,
                    'name': task.name,
                    'simc_profile_id': task.simc_profile_id,
                    'current_status': task.current_status,
                    'result_file': self._task_result_file_summary(task),
                    'task_type': task.task_type,
                    'ext_detail': self._task_ext_summary(task.task_type, task.ext),
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
            logger.error(f"更新SimC任务错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'更新任务失败: {str(e)}'
            })
    
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
            batch_id = task.batch_id
            task.is_active = False
            task.save()
            SimcMonitor(None, None).sync_batch_lifecycle(batch_id)
            
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
                return JsonResponse({
                    'success': False,
                    'error': '任务不存在或无权限访问'
                })
            
            # 检查任务是否可以重跑（只有已完成或失败的任务才能重跑）
            if task.current_status not in [2, 3]:  # 2=已完成, 3=失败
                return JsonResponse({
                    'success': False,
                    'error': '只有已完成或失败的任务才能重跑'
                })

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
                    'result_file': self._task_result_file_summary(rerun_task),
                    'task_type': rerun_task.task_type,
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
        """Create a clean queue row and retain only immutable execution inputs."""
        rerun_task = SimcTask.objects.create(
            user_id=task.user_id, name=task.name, simc_profile_id=task.simc_profile_id,
            current_status=0, result_file='', task_type=task.task_type, ext=task.ext,
            # A member rerun is independent. Reusing the old batch would mutate that
            # comparison's totals and ranking after it had already completed.
            batch_id=None, candidate_label=task.candidate_label,
            final_simc_content=task.final_simc_content, input_hash=task.input_hash,
            fragment_manifest=task.fragment_manifest, error_detail=None, result_summary=None,
            started_at=None, completed_at=None, is_active=True,
        )
        return rerun_task

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
                'batch_id', 'candidate_index', 'is_base', 'preprocess_stage',
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
                    allowed_types=[SimcContentTemplate.TYPE_BASE_TEMPLATE],
                    owner_user_id=owner_user_id,
                )
                if not template_obj:
                    raise Exception('选择的基础模板不存在或已禁用')
                base['base_template_id'] = template_obj.id
            base['base_template_content'] = frozen_template
        elif base_template_id not in (None, ''):
            template_obj = _get_simc_content_by_id(
                base_template_id,
                allowed_types=[SimcContentTemplate.TYPE_BASE_TEMPLATE],
                owner_user_id=owner_user_id,
            )
            if not template_obj:
                raise Exception('选择的基础模板不存在或已禁用')
            base['base_template_id'] = template_obj.id
            base['base_template_content'] = template_obj.content
        elif not base.get('base_template_content') and spec:
            # 先匹配专精模板；没有时再使用唯一全局默认模板。每一层都 fail closed。
            candidates = SimcContentTemplate.objects.filter(
                template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
                is_active=True,
                spec=spec,
            ).filter(models.Q(owner_user_id__isnull=True) | models.Q(owner_user_id=owner_user_id))
            if candidates.count() > 1:
                raise Exception(f'专精 {spec} 有多个启用的基础模板，请明确选择一个')
            if candidates.count() == 0:
                candidates = SimcContentTemplate.objects.filter(
                    template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
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
                apl_obj = _get_simc_content_by_id(
                    selected_apl_id,
                    allowed_types=[SimcContentTemplate.TYPE_DEFAULT_APL, SimcContentTemplate.TYPE_CUSTOM_APL],
                    owner_user_id=owner_user_id,
                )
                if not apl_obj:
                    raise Exception('选择的 APL 不存在或已禁用')
                base['selected_apl_id'] = apl_obj.id
                base['override_action_list_name'] = apl_obj.name or apl_obj.spec
                base['override_action_list_type'] = apl_obj.template_type
        elif selected_apl_id not in (None, ''):
            apl_obj = _get_simc_content_by_id(
                selected_apl_id,
                allowed_types=[SimcContentTemplate.TYPE_DEFAULT_APL, SimcContentTemplate.TYPE_CUSTOM_APL],
                owner_user_id=owner_user_id,
            )
            if not apl_obj:
                raise Exception('选择的 APL 不存在或已禁用')
            base['selected_apl_id'] = apl_obj.id
            base['override_action_list'] = apl_obj.content
            base['override_action_list_name'] = apl_obj.name or apl_obj.spec
            base['override_action_list_type'] = apl_obj.template_type
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
class SimcBatchTaskAPIView(View):
    """Create a small, self-describing regular-task comparison batch."""
    MAX_TASKS = 8
    MAX_ATTRIBUTE_TASKS = 13
    ATTRIBUTE_STATS = ('crit', 'haste', 'mastery', 'versatility')
    MAX_ATTRIBUTE_SEARCH_ROUNDS = 100
    ATTRIBUTE_SEARCH_STEP = 50
    DEFAULT_MIN_ATTRIBUTE_STEP = ATTRIBUTE_SEARCH_STEP
    ATTRIBUTE_DPS_TOLERANCE = 1.0

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
        """Measure the complete legal 50-rating directed pairwise neighborhood.

        The four ratings keep their exact total. A 50-rating transfer from every legal
        source stat to every other target stat is evaluated with real SimC. Therefore
        a winning centre is a local optimum under the declared 50-rating neighborhood,
        rather than merely under a versatility-anchored coordinate subset.
        """
        try:
            step = int(step if step is not None else cls.ATTRIBUTE_SEARCH_STEP)
        except (TypeError, ValueError):
            raise ValueError('属性寻优步长无效')
        if step != cls.ATTRIBUTE_SEARCH_STEP:
            raise ValueError(f'四属性自动寻优固定使用 {cls.ATTRIBUTE_SEARCH_STEP} 绿字步长')
        base = {stat: int(values[stat]) for stat in cls.ATTRIBUTE_STATS}
        rows = [('基准属性', base, mark_base, {
            'type': 'attribute', 'algorithm': 'four_stat_pairwise_hill_climb',
            'algorithm_version': 2, 'round': round_number, 'step': step,
            'total_rating': sum(base.values()), 'move': {'type': 'baseline'},
        })]
        for source in cls.ATTRIBUTE_STATS:
            if base[source] < step:
                continue
            for target in cls.ATTRIBUTE_STATS:
                if source == target:
                    continue
                variant = dict(base)
                variant[source] -= step
                variant[target] += step
                rows.append((f'{source} -{step} / {target} +{step}', variant, False, {
                    'type': 'attribute', 'algorithm': 'four_stat_pairwise_hill_climb',
                    'algorithm_version': 2, 'round': round_number, 'step': step,
                    'total_rating': sum(base.values()),
                    'move': {'from': source, 'to': target, 'transfer': step},
                }))
        return rows

    @classmethod
    def _next_attribute_search_center(cls, results, step, min_step=50):
        """Choose the next centre from one completed 50-rating local neighborhood."""
        if not results:
            raise ValueError('属性寻优需要至少一个完成结果')
        try:
            current_step = max(1, int(step))
            minimum_step = max(1, int(min_step))
        except (TypeError, ValueError):
            raise ValueError('属性寻优步长无效')
        if current_step != cls.ATTRIBUTE_SEARCH_STEP or minimum_step != cls.ATTRIBUTE_SEARCH_STEP:
            raise ValueError(f'四属性自动寻优固定使用 {cls.ATTRIBUTE_SEARCH_STEP} 绿字步长')
        if any(int(row.get('ratings', {}).get(stat, -1)) < 0 for row in results if isinstance(row, dict) for stat in cls.ATTRIBUTE_STATS):
            raise ValueError('属性寻优绿字不能为负数')
        valid = []
        for row in results:
            ratings = row.get('ratings') if isinstance(row, dict) else None
            dps = row.get('dps') if isinstance(row, dict) else None
            if not isinstance(ratings, dict) or any(stat not in ratings for stat in cls.ATTRIBUTE_STATS):
                continue
            try:
                normalized = {stat: int(ratings[stat]) for stat in cls.ATTRIBUTE_STATS}
                score = float(dps)
            except (TypeError, ValueError):
                continue
            if min(normalized.values()) < 0:
                continue
            valid.append({'ratings': normalized, 'dps': score, 'is_center': bool(row.get('is_center'))})
        if not valid:
            raise ValueError('属性寻优缺少有效 DPS 结果')
        center = next((row for row in valid if row['is_center']), None)
        if center is None:
            raise ValueError('属性寻优当前轮缺少基准点')
        tolerance = float(cls.ATTRIBUTE_DPS_TOLERANCE)
        best_neighbor = max((row for row in valid if not row['is_center']), key=lambda row: row['dps'], default=None)
        improved = bool(best_neighbor and best_neighbor['dps'] > center['dps'] + tolerance)
        winner = best_neighbor if improved else center
        return {
            'ratings': winner['ratings'],
            'step': cls.ATTRIBUTE_SEARCH_STEP,
            'round': 2,
            'dps': winner['dps'],
            'converged': not improved,
            'stop_reason': '' if improved else 'local_optimum_50_pairwise',
        }

    @classmethod
    def _attribute_center_signature(cls, ratings, step):
        return tuple(int(ratings[stat]) for stat in cls.ATTRIBUTE_STATS), int(step)

    @classmethod
    def _attribute_search_stop_reason(cls, round_number, ratings, step, visited_centers, max_rounds=None):
        limit = cls.MAX_ATTRIBUTE_SEARCH_ROUNDS if max_rounds is None else max(1, int(max_rounds))
        if int(round_number) >= limit:
            return 'max_rounds_reached'
        if cls._attribute_center_signature(ratings, step) in (visited_centers or set()):
            return 'cycle_detected'
        return ''

    @classmethod
    def _attribute_search_history(cls, tasks):
        """Read historical centres from task manifests; no extra model fields required."""
        history = set()
        for task in tasks:
            ext = cls._parse_task_ext(task.ext)
            manifest = ext.get('batch_compare') or {}
            candidate = manifest.get('candidate') or {}
            if not manifest.get('is_base'):
                continue
            step = candidate.get('step')
            ratings = {stat: ext.get(f'gear_{stat}') for stat in cls.ATTRIBUTE_STATS}
            if step in (None, '') or any(value is None for value in ratings.values()):
                continue
            try:
                history.add(cls._attribute_center_signature(ratings, step))
            except (KeyError, TypeError, ValueError):
                continue
        return history

    @staticmethod
    def _parse_task_ext(ext_data):
        if isinstance(ext_data, dict):
            return ext_data
        try:
            parsed = json.loads(ext_data or '{}')
        except (TypeError, ValueError):
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _create_frozen_atom(*, user_id, batch, name, candidate_label,
                            composer_request, batch_compare):
        """Compose and persist one immutable v2 atom in an existing batch."""
        result_file = f'{uuid.uuid4().hex}.html'
        request_data = dict(composer_request)
        request_data['_result_file_path'] = result_file
        composer = SimcComposer(user_id=user_id)
        final_content, manifest, error = composer.compose(request_data)
        if error:
            raise ValueError(f'{candidate_label}: {error}')
        if not final_content or manifest is None:
            raise ValueError(f'{candidate_label}: Composer 未生成冻结任务正文')

        # ext remains compatibility/audit metadata for existing result pages.
        # It is not an execution source for manifest-v2 tasks.
        ext_payload = {
            key: value for key, value in composer_request.items()
            if not key.startswith('_') and value is not None
        }
        ext_payload['player_config_mode'] = composer_request.get('player_import_mode', '')
        resolved_base_template = getattr(composer, '_base_template_content', None)
        if resolved_base_template is not None:
            ext_payload['base_template_content'] = resolved_base_template
        action_slot = composer.slots.get('action_list')
        if action_slot and action_slot.value is not None:
            # Preserve even an explicitly empty action list. The presence of this
            # key, not its truthiness, blocks mutable APL fallback in later rounds.
            ext_payload['override_action_list'] = action_slot.value.content
        ext_payload['batch_compare'] = batch_compare
        return SimcTask.objects.create(
            user_id=user_id,
            name=name,
            simc_profile_id=0,
            current_status=0,
            result_file=result_file,
            task_type=1,
            ext=json.dumps(ext_payload, ensure_ascii=False),
            batch=batch,
            candidate_label=candidate_label,
            final_simc_content=final_content,
            input_hash=SimcComposer.compute_input_hash(final_content),
            fragment_manifest=manifest.to_json(),
        )

    @staticmethod
    def _parse_manifest_round(manifest):
        try:
            return int((manifest.get('candidate') or {}).get('round') or 1)
        except (AttributeError, TypeError, ValueError):
            return 1

    @transaction.atomic
    def _create_attribute_round(self, request, payload, parent_batch_id):
        """Create the next isolated four-stat search round from completed batch data."""
        source_rows = []
        source_manifest = None
        source_ext = None
        batch = None
        try:
            batch = SimcTaskBatch.objects.select_for_update().get(
                id=int(parent_batch_id), user_id=request.user.id, is_active=True,
            )
        except (TypeError, ValueError, SimcTaskBatch.DoesNotExist):
            # Historical UUID batches have no FK. They remain readable and may be
            # continued once, but every newly created task is still Composer-frozen.
            batch = None
        if batch is not None:
            batch_tasks = list(SimcTask.objects.select_for_update().filter(
                user_id=request.user.id, is_active=True, task_type=1, batch=batch,
            ).order_by('id'))
        else:
            batch_tasks = list(SimcTask.objects.select_for_update().filter(
                user_id=request.user.id, is_active=True, task_type=1,
                ext__contains=f'"batch_id": "{parent_batch_id}"',
            ).order_by('id'))
            batch_tasks = [
                task for task in batch_tasks
                if (self._parse_task_ext(task.ext).get('batch_compare') or {}).get('batch_id') == parent_batch_id
                and (self._parse_task_ext(task.ext).get('batch_compare') or {}).get('kind') == 'attribute_variants'
            ]
        source_task_name = ''
        for task in batch_tasks:
            ext = self._parse_task_ext(task.ext)
            manifest = ext.get('batch_compare') or {}
            source_manifest = source_manifest or manifest
            source_ext = source_ext or ext
            source_task_name = source_task_name or task.name
        if not source_manifest:
            raise ValueError('当前属性搜索批次不存在或无权限访问')
        current_round = max(
            self._parse_manifest_round((self._parse_task_ext(task.ext).get('batch_compare') or {}))
            for task in batch_tasks
        )
        round_manifest = None
        for task in batch_tasks:
            ext = self._parse_task_ext(task.ext)
            manifest = ext.get('batch_compare') or {}
            if self._parse_manifest_round(manifest) != current_round:
                continue
            round_manifest = round_manifest or manifest
            if task.current_status != 2 or not task.result_file:
                raise ValueError('当前属性搜索轮次尚未全部完成')
            html_content = SimcRegularCompareAPIView()._get_result_file_content(task.result_file)
            parsed = SimcRegularCompareAPIView()._parse_regular_result(html_content) if html_content else {}
            dps = parsed.get('dps')
            ratings = {stat: ext.get(f'gear_{stat}') for stat in self.ATTRIBUTE_STATS}
            if dps is None or any(value is None for value in ratings.values()):
                raise ValueError('当前属性搜索轮次存在无法解析 DPS 或绿字的任务')
            source_rows.append({'ratings': ratings, 'dps': dps, 'is_center': bool(manifest.get('is_base'))})
        if len(source_rows) < 2:
            raise ValueError('当前属性搜索轮次没有足够完成结果')
        current_step = (round_manifest or {}).get('candidate', {}).get('step')
        candidate = self._next_attribute_search_center(
            source_rows, current_step, payload.get('min_attribute_step', self.DEFAULT_MIN_ATTRIBUTE_STEP)
        )
        if candidate['converged']:
            return None, candidate
        next_round = current_round + 1
        stop_reason = self._attribute_search_stop_reason(
            next_round, candidate['ratings'], candidate['step'],
            self._attribute_search_history(batch_tasks), self.MAX_ATTRIBUTE_SEARCH_ROUNDS,
        )
        if stop_reason:
            candidate['converged'] = True
            candidate['stop_reason'] = stop_reason
            return None, candidate
        specs = self._attribute_variants(candidate['ratings'], candidate['step'], round_number=next_round, mark_base=True)
        return (specs, candidate, source_ext, source_task_name, batch), None

    @transaction.atomic
    def _continue_attribute_search(self, request, data, continue_batch_id):
        """Advance one attribute-search batch while its current task rows stay locked."""
        continuation, converged = self._create_attribute_round(request, data, continue_batch_id)
        if converged:
            try:
                batch = SimcTaskBatch.objects.select_for_update().get(
                    id=int(continue_batch_id), user_id=request.user.id, is_active=True,
                )
            except (TypeError, ValueError, SimcTaskBatch.DoesNotExist):
                batch = None
            if batch is not None:
                batch.status = 2
                batch.completed_at = timezone.now()
                batch.save(update_fields=['status', 'completed_at', 'updated_at'])
            return {'batch_id': continue_batch_id, 'accepted': 0, 'converged': True, 'recommendation': converged}
        specs, recommendation, source_ext, source_task_name, batch = continuation
        if batch is None:
            batch = SimcTaskBatch.objects.create(
                user_id=request.user.id,
                name=f'{source_task_name.rsplit(" · ", 1)[0]} · 续跑',
                batch_type='attribute_sweep',
                request_manifest=json.dumps({
                    'version': 2, 'kind': 'attribute_variants',
                    'legacy_parent_batch_id': continue_batch_id,
                    'frozen_source': source_ext,
                }, ensure_ascii=False),
                status=1,
            )
        effective_batch_id = str(batch.id)
        created = []
        for index, (label, gear, is_base, candidate_data) in enumerate(specs):
            candidate = dict(candidate_data)
            candidate['search_center'] = recommendation['ratings']
            candidate['parent_batch_id'] = continue_batch_id
            batch_compare = {
                'version': 2, 'batch_id': effective_batch_id,
                'parent_batch_id': continue_batch_id,
                'kind': 'attribute_variants', 'index': index,
                'label': label, 'is_base': is_base, 'candidate': candidate,
            }
            composer_request = {
                'fight_style': source_ext.get('fight_style', 'Patchwerk'),
                'time': source_ext.get('time', 300),
                'target_count': source_ext.get('target_count', 1),
                'player_import_mode': 'attribute_only',
                'spec': source_ext.get('spec', ''),
                'player_equipment': source_ext.get('player_equipment', ''),
                'talent': source_ext.get('talent', ''),
                'gear_strength': source_ext.get('gear_strength'),
                'base_template_id': source_ext.get('base_template_id'),
                'base_template_content': source_ext.get('base_template_content'),
                'selected_apl_id': source_ext.get('selected_apl_id'),
            }
            if 'override_action_list' in source_ext:
                composer_request['override_action_list'] = source_ext.get('override_action_list')
                composer_request['override_action_list_provided'] = True
            composer_request.update({f'gear_{stat}': gear[stat] for stat in self.ATTRIBUTE_STATS})
            task = self._create_frozen_atom(
                user_id=request.user.id,
                batch=batch,
                name=f'{source_task_name.rsplit(" · ", 1)[0]} · 第{candidate["round"]}轮 {label}',
                candidate_label=label,
                composer_request=composer_request,
                batch_compare=batch_compare,
            )
            created.append(task)
        batch.status = 1
        batch.completed_at = None
        batch.save(update_fields=['status', 'completed_at', 'updated_at'])
        return {
            'batch_id': effective_batch_id,
            'parent_batch_id': continue_batch_id,
            'task_ids': [task.id for task in created],
            'accepted': len(created),
            'recommendation': recommendation,
        }

    def _safe_error_summary(self, task):
        """安全错误摘要：从ext的simc_error_summary提取，或返回固定文案"""
        try:
            ext = json.loads(task.ext) if isinstance(task.ext, str) else (task.ext or {})
        except (json.JSONDecodeError, TypeError):
            ext = {}

        summary = ext.get('simc_error_summary', '')
        if summary and isinstance(summary, str):
            # 从现有安全摘要中提取，禁止路径、命令、stderr、冻结输入
            safe = summary.strip()[:200]
            # 移除可能的敏感模式
            if any(x in safe.lower() for x in ['traceback', 'file "/', 'command', 'stderr', 'gear_', 'player=']):
                return '任务执行失败'
            return safe if safe else '任务执行失败'
        return '任务执行失败'

    def _has_valid_html_results(self, tasks):
        """检查是否所有任务都成功且有通过安全验证的HTML结果"""
        if not tasks:
            return False
        task_api = SimcTaskAPIView()
        for task in tasks:
            if task.current_status != 2:
                return False
            # 复用任务列表既有的文件名白名单；属性任务也必须逐个通过解析器。
            if not task_api._task_result_file_summary(task):
                return False
        return True

    def get(self, request):
        """返回最近20条Batch列表或单个Batch详情（严格用户隔离，仅安全摘要）"""
        try:
            user_id = request.user.id
            batch_id = str(request.GET.get('batch_id') or '').strip()

            if batch_id:
                # 返回单个Batch详情
                try:
                    batch = SimcTaskBatch.objects.get(
                        id=int(batch_id),
                        user_id=user_id,
                        is_active=True
                    )
                except (ValueError, SimcTaskBatch.DoesNotExist):
                    return JsonResponse({'success': False, 'error': 'Batch不存在或无权限访问'}, status=404)

                # 聚合该Batch下的所有活跃任务
                tasks = SimcTask.objects.filter(
                    batch=batch,
                    user_id=user_id,
                    is_active=True
                ).order_by('id')

                status_counts = {'pending': 0, 'running': 0, 'completed': 0, 'failed': 0}
                task_details = []

                for task in tasks:
                    if task.current_status == 0:
                        status_counts['pending'] += 1
                    elif task.current_status in (1, 4):
                        status_counts['running'] += 1
                    elif task.current_status == 2:
                        status_counts['completed'] += 1
                    elif task.current_status == 3:
                        status_counts['failed'] += 1

                    # 安全的错误摘要：从ext.simc_error_summary提取或固定文案
                    error_summary = ''
                    if task.current_status == 3:
                        error_summary = self._safe_error_summary(task)

                    task_details.append({
                        'task_id': task.id,
                        'candidate_label': task.candidate_label or '',
                        'status': task.current_status,
                        'error_summary': error_summary,
                        'started_at': _fmt_dt(task.started_at),
                        'completed_at': _fmt_dt(task.completed_at),
                    })

                # 报告URL：仅当FK成员非空、全部成功且有安全HTML结果时给出
                report_url = ''
                if tasks and self._has_valid_html_results(tasks):
                    report_url = f'/simc-compare/?batch_id={batch.id}'

                return JsonResponse({
                    'success': True,
                    'data': {
                        'batch_id': batch.id,
                        'name': batch.name,
                        'batch_type': batch.batch_type,
                        'status': batch.status,
                        'status_counts': status_counts,
                        'created_at': _fmt_dt(batch.created_at),
                        'completed_at': _fmt_dt(batch.completed_at),
                        'report_url': report_url,
                        'tasks': task_details,
                    }
                })
            else:
                # 返回最近20条Batch列表
                batches = SimcTaskBatch.objects.filter(
                    user_id=user_id,
                    is_active=True
                ).order_by('-created_at')[:20]

                batch_list = []
                for batch in batches:
                    # 聚合该Batch的任务状态计数
                    tasks = SimcTask.objects.filter(
                        batch=batch,
                        user_id=user_id,
                        is_active=True
                    )

                    status_counts = {'pending': 0, 'running': 0, 'completed': 0, 'failed': 0}
                    for task in tasks:
                        if task.current_status == 0:
                            status_counts['pending'] += 1
                        elif task.current_status in (1, 4):
                            status_counts['running'] += 1
                        elif task.current_status == 2:
                            status_counts['completed'] += 1
                        elif task.current_status == 3:
                            status_counts['failed'] += 1

                    # 报告URL：仅当FK成员非空、全部成功且有安全HTML结果时给出
                    report_url = ''
                    if tasks and self._has_valid_html_results(tasks):
                        report_url = f'/simc-compare/?batch_id={batch.id}'

                    batch_list.append({
                        'batch_id': batch.id,
                        'name': batch.name,
                        'batch_type': batch.batch_type,
                        'status': batch.status,
                        'status_counts': status_counts,
                        'created_at': _fmt_dt(batch.created_at),
                        'completed_at': _fmt_dt(batch.completed_at),
                        'report_url': report_url,
                    })

                return JsonResponse({'success': True, 'data': batch_list})

        except Exception as e:
            logger.error(f'获取 SimC Batch 数据失败: {e}\n{traceback.format_exc()}')
            return JsonResponse({'success': False, 'error': '服务器内部错误'}, status=500)

    def post(self, request):
        try:
            data = json.loads(request.body or '{}')
            continue_batch_id = str(data.get('continue_batch_id') or '').strip()
            if continue_batch_id:
                return JsonResponse({'success': True, 'data': self._continue_attribute_search(request, data, continue_batch_id)})

            kind = str(data.get('kind') or '').strip()
            category = str(data.get('category') or '').strip()
            spec = str(data.get('spec') or '').strip().lower()
            mode = str(data.get('player_config_mode') or data.get('player_import_mode') or '').strip()
            name = str(data.get('name') or '').strip()
            if kind not in ('attribute_variants', 'gear_candidates', 'talent_candidates'):
                raise ValueError('不支持的批次类型')
            if category and category not in ('trinket_candidates', 'gear_candidates', 'talent_candidates'):
                raise ValueError('不支持的候选类别')
            if category == 'trinket_candidates' and kind != 'gear_candidates':
                raise ValueError('饰品候选必须使用装备候选批次')
            if category in ('gear_candidates', 'talent_candidates') and category != kind:
                raise ValueError('候选类别与批次类型不匹配')
            if not name or not spec:
                raise ValueError('任务名称和专精不能为空')
            if mode not in ('attribute_only', 'manual_equipment'):
                raise ValueError('批次仅支持 attribute_only 或 manual_equipment 配置')
            fight_style = str(data.get('fight_style') or 'Patchwerk').strip()
            fight_time = max(1, self._int(data.get('time', 300), '战斗时长'))
            target_count = max(1, self._int(data.get('target_count', 1), '目标数量'))
            selected_apl_id = data.get('selected_apl_id')
            base_template_id = data.get('base_template_id')
            base_template_content = data.get('base_template_content') if 'base_template_content' in data else None
            override_action_list = data.get('override_action_list') if 'override_action_list' in data else None
            override_action_list_provided = 'override_action_list' in data
            specs = []

            if kind == 'attribute_variants':
                if mode != 'attribute_only':
                    raise ValueError('自动属性比较仅支持 attribute_only 配置')
                talent = str(data.get('talent') or '').strip()
                if not talent:
                    raise ValueError('自动属性比较需要天赋构筑码')
                player_equipment = str(data.get('player_equipment') or '').strip()
                try:
                    player_equipment = resolve_attribute_player_baseline(spec, player_equipment)
                except ValueError as e:
                    return JsonResponse({'success': False, 'error': str(e)}, status=400)
                step = self._int(data.get('attribute_step'), '属性步长')
                if step != self.ATTRIBUTE_SEARCH_STEP:
                    raise ValueError(f'四属性自动寻优固定使用 {self.ATTRIBUTE_SEARCH_STEP} 绿字步长')
                values = {stat: self._int(data.get(f'gear_{stat}'), f'{stat}绿字') for stat in self.ATTRIBUTE_STATS}
                for label, ratings, is_base, candidate in self._attribute_variants(values, step):
                    specs.append({'label': label, 'is_base': is_base, 'gear': ratings, 'candidate': candidate, 'player_equipment': player_equipment})
            else:
                if mode != 'manual_equipment':
                    raise ValueError('装备和天赋候选比较需要手动 SimC 玩家块')
                player_equipment = str(data.get('player_equipment') or '').strip()
                if not player_equipment:
                    raise ValueError('手动装备模式下玩家装备配置不能为空')
                from botend.services.simc_player_config import parse_manual_simc_candidates
                parsed = parse_manual_simc_candidates(player_equipment)
                base_talent = parsed.get('base_talent') or ''
                specs.append({'label': '基准配置', 'is_base': True, 'player_equipment': player_equipment, 'talent': base_talent, 'candidate': {'type': 'base'}})
                submitted = data.get('candidates') or []
                if not isinstance(submitted, list) or not submitted:
                    raise ValueError('请至少选择一个可信候选')
                if len(submitted) + 1 > self.MAX_TASKS:
                    raise ValueError(f'每批最多{self.MAX_TASKS}个任务（含基准）')
                if kind == 'gear_candidates':
                    trusted = {(row['slot'], row['item_id'], row['source']): row for row in parsed['gear_candidates']}
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
                        specs.append({'label': row['name'] or f"{row['slot']} #{row['item_id']}", 'is_base': False, 'player_equipment': '\n'.join(lines), 'talent': base_talent, 'candidate': {'type': 'gear_swap', 'slot': row['slot'], 'item_id': row['item_id'], 'source': row['source']}})
                else:
                    trusted = {row['talent']: row for row in parsed['talent_candidates']}
                    submitted_talents = [str(candidate.get('talent') or '') for candidate in submitted]
                    if len(set(submitted_talents)) != len(submitted_talents):
                        raise ValueError('候选天赋不可重复选择')
                    for talent in submitted_talents:
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
                            raise ValueError('基准玩家块未包含 talents 行，无法创建天赋对比')
                        specs.append({'label': row['name'] or '候选天赋', 'is_base': False, 'player_equipment': '\n'.join(lines), 'talent': talent, 'candidate': {'type': 'talent', 'talent': talent, 'source': row['source']}})

            if len(specs) < 2:
                raise ValueError('可生成的比较任务不足2个；请提高可转移绿字或选择候选')
            created = []
            with transaction.atomic():
                batch = SimcTaskBatch.objects.create(
                    user_id=request.user.id,
                    name=name,
                    batch_type=kind,
                    request_manifest=json.dumps(data, ensure_ascii=False),
                    status=1,
                )
                batch_id = str(batch.id)
                for index, item in enumerate(specs):
                    batch_compare = {
                        'version': 2 if kind == 'attribute_variants' else 1,
                        'batch_id': batch_id,
                        'kind': kind,
                        'category': category or kind,
                        'index': index,
                        'label': item['label'],
                        'is_base': item['is_base'],
                        'candidate': item['candidate'],
                    }
                    composer_request = {
                        'fight_style': fight_style,
                        'time': fight_time,
                        'target_count': target_count,
                        'player_import_mode': mode,
                        'spec': spec,
                        'talent': item.get('talent', str(data.get('talent') or '')),
                        'base_template_id': base_template_id,
                        'base_template_content': base_template_content,
                        'selected_apl_id': selected_apl_id,
                        'player_equipment': item['player_equipment'],
                    }
                    if override_action_list_provided:
                        composer_request['override_action_list'] = override_action_list
                    if mode == 'attribute_only':
                        composer_request['gear_strength'] = self._int(data.get('gear_strength', 0), '主属性')
                        composer_request.update({
                            f'gear_{stat}': item['gear'][stat]
                            for stat in self.ATTRIBUTE_STATS
                        })
                    task = self._create_frozen_atom(
                        user_id=request.user.id,
                        batch=batch,
                        name=f'{name} · {item["label"]}',
                        candidate_label=item['label'],
                        composer_request=composer_request,
                        batch_compare=batch_compare,
                    )
                    created.append(task)
            return JsonResponse({'success': True, 'data': {'batch_id': batch_id, 'task_ids': [task.id for task in created], 'accepted': len(created)}})
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': '无效的JSON数据'})
        except ValueError as e:
            return JsonResponse({'success': False, 'error': str(e)})
        except Exception as e:
            logger.error(f'创建 SimC 比较批次失败: {e}\n{traceback.format_exc()}')
            return JsonResponse({'success': False, 'error': f'创建比较批次失败: {e}'})


@method_decorator(login_required, name='dispatch')
class SimcPlayerConfigDetailAPIView(View):
    """只解析工作台当前玩家输入，返回结构化配置详情；不渲染完整 SimC 执行文本。"""

    def get(self, request):
        """返回指定专精当前唯一启用的默认玩家基线，供工作台编辑后冻结。"""
        spec = str(request.GET.get('spec') or '').strip()
        if not spec:
            return JsonResponse({'success': False, 'error': '请先选择专精'}, status=400)
        try:
            baseline = resolve_attribute_player_baseline(spec, '')
            return JsonResponse({'success': True, 'data': {'spec': spec, 'player_equipment': baseline}})
        except ValueError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=400)

    def post(self, request):
        try:
            data = json.loads(request.body)
            spec = str(data.get('spec') or '').strip()
            if not spec:
                return JsonResponse({'success': False, 'error': '请先选择专精'})
            mode = data.get('player_import_mode') or data.get('player_config_mode')
            if mode == 'equipment':
                mode = 'manual_equipment'
            if mode not in ('battlenet', 'manual_equipment', 'attribute_only'):
                return JsonResponse({'success': False, 'error': '玩家信息导入方式必须是 battlenet、manual_equipment 或 attribute_only'})
            player_equipment = str(data.get('player_equipment') or '').strip()
            battlenet_region = str(data.get('battlenet_region') or '').strip().lower()
            battlenet_realm = str(data.get('battlenet_realm') or '').strip()
            battlenet_character = str(data.get('battlenet_character') or '').strip()
            if mode == 'manual_equipment' and not player_equipment:
                return JsonResponse({'success': False, 'error': '手动装备模式下玩家装备配置不能为空'})
            if mode == 'battlenet' and (
                battlenet_region not in ('us', 'eu', 'kr', 'tw', 'cn')
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
            from botend.services.simc_player_config import build_player_config_detail, parse_manual_simc_candidates
            detail = build_player_config_detail(
                mode=mode, spec=spec, player_equipment=player_equipment,
                battlenet_region=battlenet_region, battlenet_realm=battlenet_realm,
                battlenet_character=battlenet_character,
                talent=str(data.get('talent') or '').strip(), gear_strength=data.get('gear_strength'),
                gear_crit=data.get('gear_crit'), gear_haste=data.get('gear_haste'),
                gear_mastery=data.get('gear_mastery'), gear_versatility=data.get('gear_versatility'),
            )
            if mode == 'manual_equipment':
                candidates = parse_manual_simc_candidates(player_equipment)
                detail['comparison_candidates'] = {
                    'gear': candidates['gear_candidates'],
                    'talents': candidates['talent_candidates'],
                    'max_selectable': SimcBatchTaskAPIView.MAX_TASKS - 1,
                }
            return JsonResponse({'success': True, 'data': detail})
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
    'deathknight': 'death_knight',
    'demonhunter': 'demon_hunter',
}


def _normalize_simc_token(value):
    return re.sub(r'[^a-z0-9_]+', '_', str(value or '').strip().lower()).strip('_')


def _get_active_simc_content(template_type, spec=None, source=None, class_name=None, selectable=None):
    qs = SimcContentTemplate.objects.filter(template_type=template_type, is_active=True)
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


def _list_selectable_apl_for_spec(spec_key='', class_name='', spec=''):
    qs = SimcContentTemplate.objects.filter(
        template_type__in=[SimcContentTemplate.TYPE_DEFAULT_APL, SimcContentTemplate.TYPE_CUSTOM_APL],
        is_active=True,
        is_selectable=True,
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
    for item in qs.order_by('template_type', 'source', 'name', 'id')[:50]:
        rows.append({
            'id': item.id,
            'name': item.name or item.spec,
            'template_type': item.template_type,
            'source': item.source,
            'spec': item.spec,
            'class_name': item.class_name,
            'content_length': len(item.content or ''),
            'is_default': item.template_type == SimcContentTemplate.TYPE_DEFAULT_APL,
        })
    return rows


def _get_simc_content_by_id(content_id, allowed_types=None, owner_user_id=None):
    if not content_id:
        return None
    try:
        qs = SimcContentTemplate.objects.filter(id=int(content_id), is_active=True)
    except (TypeError, ValueError):
        return None
    if allowed_types:
        qs = qs.filter(template_type__in=allowed_types)
    qs = qs.filter(models.Q(owner_user_id__isnull=True) | models.Q(owner_user_id=owner_user_id))
    return qs.first()


def _get_unique_default_apl_for_spec(spec, owner_user_id=None):
    """返回当前用户可见的默认 APL；用户模板优先于全局上游模板。"""
    spec_value = str(spec or '').strip().lower()
    spec_key = f'warrior_{spec_value}' if spec_value in ('fury', 'arms', 'protection') else spec_value
    candidates = SimcContentTemplate.objects.filter(
        template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
        is_active=True,
        spec=spec_key,
    ).filter(models.Q(owner_user_id__isnull=True) | models.Q(owner_user_id=owner_user_id))
    if owner_user_id is not None:
        owned = candidates.filter(owner_user_id=owner_user_id).first()
        if owned:
            return owned
    return candidates.filter(owner_user_id__isnull=True).first()


def inspect_raw_simc_code(raw_simc_code):
    """Parse a pasted SimulationCraft profile enough for dashboard task creation."""
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
        apl = _get_active_simc_content(
            SimcContentTemplate.TYPE_DEFAULT_APL,
            spec=result['spec_key'],
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            class_name=class_name,
            selectable=True,
        )
        if apl:
            result['default_apl_id'] = apl.id
            result['default_apl_available'] = True
            result['default_apl_length'] = len(apl.content or '')
        result['available_apls'] = _list_selectable_apl_for_spec(
            spec_key=result['spec_key'],
            class_name=class_name,
            spec=spec,
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
            'task_type': 1,
            'default_time': 300,
            'default_target_count': 1,
            'task_name': ' '.join(plan_name_parts) + ' 常规模拟',
            'reason': '',
        },
        {
            'id': 'attribute',
            'label': '属性模拟',
            'enabled': False,
            'checked': False,
            'task_type': 2,
            'reason': '属性模拟需要先保存为 SimC 配置，再基于配置生成属性变体',
        },
        {
            'id': 'apl_compare',
            'label': 'APL候选对比',
            'enabled': False,
            'checked': False,
            'task_type': 1,
            'reason': 'APL候选对比需要配置化 Profile 和可替换 action_list，raw 代码首版仅开放常规模拟',
        },
    ]
    return result


@method_decorator(login_required, name='dispatch')
class SimcRawInspectAPIView(View):
    """Inspect pasted raw SimulationCraft code and return safe task plans."""

    def post(self, request):
        try:
            data = json.loads(request.body or '{}')
            raw_simc_code = data.get('raw_simc_code', '')
            payload = inspect_raw_simc_code(raw_simc_code)
            return JsonResponse({'success': True, 'data': payload})
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': '无效的JSON数据'})
        except ValueError as e:
            return JsonResponse({'success': False, 'error': str(e)})
        except Exception as e:
            logger.error(f"解析SimC原始代码失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({'success': False, 'error': f'解析SimC代码失败: {str(e)}'})


@method_decorator(login_required, name='dispatch')
class KeywordManagerAPIView(View):
    """
    关键字管理API
    """
    
    def get(self, request):
        """获取关键字列表"""
        try:
            # 获取查询参数
            page = int(request.GET.get('page', 1))
            page_size = int(request.GET.get('page_size', 10))
            search = request.GET.get('search', '').strip()
            
            # 构建查询
            queryset = SimcAplKeywordPair.objects.all()
            
            if search:
                queryset = queryset.filter(
                    models.Q(apl_keyword__icontains=search) |
                    models.Q(cn_keyword__icontains=search) |
                    models.Q(description__icontains=search)
                )
            
            # 计算分页
            total = queryset.count()
            start = (page - 1) * page_size
            end = start + page_size
            keywords = queryset.order_by('-create_time')[start:end]
            
            # 序列化数据
            data = []
            for keyword in keywords:
                data.append({
                    'id': keyword.id,
                    'apl_keyword': keyword.apl_keyword,
                    'cn_keyword': keyword.cn_keyword,
                    'description': keyword.description or '',
                    'is_active': keyword.is_active,
                    'create_time': _fmt_dt(keyword.create_time) or ''
                })
            
            return JsonResponse({
                'success': True,
                'data': data,
                'pagination': {
                    'page': page,
                    'page_size': page_size,
                    'total': total,
                    'total_pages': (total + page_size - 1) // page_size
                }
            })
            
        except Exception as e:
            logger.error(f"获取关键字列表失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '获取数据失败'
            })
    
    def post(self, request):
        """创建新关键字"""
        if not request.user.is_staff:
            return JsonResponse({'success': False, 'error': '仅管理员可修改全局关键词'}, status=403)
        try:
            data = json.loads(request.body)
            
            # 验证必填字段
            apl_keyword = data.get('apl_keyword', '').strip()
            cn_keyword = data.get('cn_keyword', '').strip()
            
            if not apl_keyword or not cn_keyword:
                return JsonResponse({
                    'success': False,
                    'error': 'APL关键字和中文关键字不能为空'
                })
            
            # 检查是否已存在相同的关键字对
            if SimcAplKeywordPair.objects.filter(
                apl_keyword=apl_keyword, 
                cn_keyword=cn_keyword
            ).exists():
                return JsonResponse({
                    'success': False,
                    'error': '该关键字对已存在'
                })
            
            # 创建新记录
            keyword = SimcAplKeywordPair.objects.create(
                apl_keyword=apl_keyword,
                cn_keyword=cn_keyword,
                description=data.get('description', ''),
                is_active=data.get('is_active', True)
            )
            
            return JsonResponse({
                'success': True,
                'data': {
                    'id': keyword.id,
                    'apl_keyword': keyword.apl_keyword,
                    'cn_keyword': keyword.cn_keyword,
                    'description': keyword.description,
                    'is_active': keyword.is_active,
                    'create_time': _fmt_dt(keyword.create_time)
                }
            })
            
        except Exception as e:
            logger.error(f"创建关键字失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '创建失败'
            })
    
    def put(self, request):
        """更新关键字"""
        if not request.user.is_staff:
            return JsonResponse({'success': False, 'error': '仅管理员可修改全局关键词'}, status=403)
        try:
            data = json.loads(request.body)
            keyword_id = data.get('id')
            
            if not keyword_id:
                return JsonResponse({
                    'success': False,
                    'error': '缺少关键字ID'
                })
            
            try:
                keyword = SimcAplKeywordPair.objects.get(id=keyword_id)
            except SimcAplKeywordPair.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': '关键字不存在'
                })
            
            # 验证必填字段
            apl_keyword = data.get('apl_keyword', '').strip()
            cn_keyword = data.get('cn_keyword', '').strip()
            
            if not apl_keyword or not cn_keyword:
                return JsonResponse({
                    'success': False,
                    'error': 'APL关键字和中文关键字不能为空'
                })
            
            # 检查APL关键字是否与其他记录冲突
            if SimcAplKeywordPair.objects.filter(apl_keyword=apl_keyword).exclude(id=keyword_id).exists():
                return JsonResponse({
                    'success': False,
                    'error': 'APL关键字已存在'
                })
            
            # 更新记录
            keyword.apl_keyword = apl_keyword
            keyword.cn_keyword = cn_keyword
            keyword.description = data.get('description', '')
            keyword.is_active = data.get('is_active', True)
            keyword.save()
            
            return JsonResponse({
                'success': True,
                'data': {
                    'id': keyword.id,
                    'apl_keyword': keyword.apl_keyword,
                    'cn_keyword': keyword.cn_keyword,
                    'description': keyword.description,
                    'is_active': keyword.is_active
                }
            })
            
        except Exception as e:
            logger.error(f"更新关键字失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '更新失败'
            })
    
    def delete(self, request):
        """删除关键字"""
        if not request.user.is_staff:
            return JsonResponse({'success': False, 'error': '仅管理员可修改全局关键词'}, status=403)
        try:
            data = json.loads(request.body)
            keyword_id = data.get('id')
            
            if not keyword_id:
                return JsonResponse({
                    'success': False,
                    'error': '缺少关键字ID'
                })
            
            try:
                keyword = SimcAplKeywordPair.objects.get(id=keyword_id)
                keyword.delete()
                
                return JsonResponse({
                    'success': True,
                    'message': '删除成功'
                })
                
            except SimcAplKeywordPair.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': '关键字不存在'
                })
                
        except Exception as e:
            logger.error(f"删除关键字失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '删除失败'
            })


@method_decorator(login_required, name='dispatch')
class AplStorageAPIView(View):
    """
    APL存储API
    """
    
    def get(self, request):
        """获取用户的APL列表"""
        try:
            user = request.user
            apl_list = UserAplStorage.objects.filter(
                user_id=user.id, 
                is_active=True
            ).order_by('-id')
            
            result = []
            for apl in apl_list:
                result.append({
                    'id': apl.id,
                    'title': apl.title
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
        """保存新的APL"""
        try:
            data = json.loads(request.body)
            title = data.get('title', '').strip()
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
            if UserAplStorage.objects.filter(
                user_id=request.user.id, 
                title=title, 
                is_active=True
            ).exists():
                return JsonResponse({
                    'success': False,
                    'error': '该标题已存在，请使用其他标题'
                })
            
            # 创建新的APL存储记录
            apl_storage = UserAplStorage.objects.create(
                user_id=request.user.id,
                title=title,
                apl_code=apl_code
            )
            
            return JsonResponse({
                'success': True,
                'message': 'APL保存成功',
                'data': {
                    'id': apl_storage.id,
                    'title': apl_storage.title
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
                apl_storage = UserAplStorage.objects.get(
                    id=apl_id, 
                    user_id=request.user.id, 
                    is_active=True
                )
                
                # 检查标题是否与其他记录重复
                if UserAplStorage.objects.filter(
                    user_id=request.user.id, 
                    title=title, 
                    is_active=True
                ).exclude(id=apl_id).exists():
                    return JsonResponse({
                        'success': False,
                        'error': '该标题已存在，请使用其他标题'
                    })
                
                # 更新记录
                apl_storage.title = title
                apl_storage.apl_code = apl_code
                apl_storage.save()
                
                return JsonResponse({
                    'success': True,
                    'message': 'APL更新成功'
                })
                
            except UserAplStorage.DoesNotExist:
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
                apl_storage = UserAplStorage.objects.get(
                    id=apl_id, 
                    user_id=request.user.id, 
                    is_active=True
                )
                
                # 软删除
                apl_storage.is_active = False
                apl_storage.save()
                
                return JsonResponse({
                    'success': True,
                    'message': 'APL删除成功'
                })
                
            except UserAplStorage.DoesNotExist:
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
            apl_storage = UserAplStorage.objects.get(
                id=apl_id, 
                user_id=request.user.id, 
                is_active=True
            )
            
            return JsonResponse({
                'success': True,
                'data': {
                    'id': apl_storage.id,
                    'title': apl_storage.title,
                    'apl_code': apl_storage.apl_code
                }
            })
            
        except UserAplStorage.DoesNotExist:
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
class SimcBattlenetPreflightAPIView(View):
    """Fetch and validate Battle.net character data before it is saved or simulated."""

    def post(self, request):
        try:
            data = json.loads(request.body or '{}')
            from botend.services.battlenet_preflight import fetch_battlenet_character_preflight
            result = fetch_battlenet_character_preflight(
                region=str(data.get('region') or data.get('battlenet_region') or '').strip().lower(),
                realm=str(data.get('realm') or data.get('battlenet_realm') or '').strip(),
                character=str(data.get('character') or data.get('battlenet_character') or '').strip(),
                requested_spec=str(data.get('spec') or '').strip().lower(),
            )
            return JsonResponse({'success': True, 'data': result})
        except ValueError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=400)
        except Exception:
            logger.exception('Battle.net SimC preflight failed')
            return JsonResponse({'success': False, 'error': '获取 Battle.net 角色配置失败，请稍后重试'}, status=502)


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

    def get(self, request, profile_id=None):
        """获取SimC配置列表或单个配置"""
        try:
            if profile_id:
                # 获取单个配置
                try:
                    profile = SimcProfile.objects.get(
                        id=profile_id,
                        user_id=request.user.id,
                        is_active=True
                    )
                    
                    return JsonResponse({
                        'success': True,
                        'id': profile.id,
                        'name': profile.name,
                        'spec': profile.spec,
                        'player_config_mode': self._profile_mode(profile),
                        'battlenet_region': getattr(profile, 'battlenet_region', '') or '',
                        'battlenet_realm': getattr(profile, 'battlenet_realm', '') or '',
                        'battlenet_character': getattr(profile, 'battlenet_character', '') or '',
                        'player_equipment': getattr(profile, 'player_equipment', '') or '',
                        'talent': profile.talent,
                        'gear_strength': profile.gear_strength,
                        'gear_crit': profile.gear_crit,
                        'gear_haste': profile.gear_haste,
                        'gear_mastery': profile.gear_mastery,
                        'gear_versatility': profile.gear_versatility,
                        'is_active': profile.is_active
                    })
                    
                except SimcProfile.DoesNotExist:
                    return JsonResponse({
                        'success': False,
                        'error': '配置不存在或无权限访问'
                    })
            else:
                # 获取配置列表
                profile_filters = {'user_id': request.user.id}
                if request.GET.get('include_inactive') not in ('1', 'true'):
                    profile_filters['is_active'] = True
                profiles = SimcProfile.objects.filter(**profile_filters).order_by('-id')
                
                profile_list = []
                for profile in profiles:
                    profile_list.append({
                        'id': profile.id,
                        'name': profile.name,
                        'spec': profile.spec,
                        'player_config_mode': self._profile_mode(profile),
                        'battlenet_region': getattr(profile, 'battlenet_region', '') or '',
                        'battlenet_realm': getattr(profile, 'battlenet_realm', '') or '',
                        'battlenet_character': getattr(profile, 'battlenet_character', '') or '',
                        'player_equipment': getattr(profile, 'player_equipment', '') or '',
                        'talent': profile.talent,
                        'gear_strength': profile.gear_strength,
                        'gear_crit': profile.gear_crit,
                        'gear_haste': profile.gear_haste,
                        'gear_mastery': profile.gear_mastery,
                        'gear_versatility': profile.gear_versatility,
                        'is_active': profile.is_active
                    })
                
                return JsonResponse({
                    'success': True,
                    'data': profile_list
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

        values = {
            'mode': mode,
            'spec': str(data.get('spec', fallback.get('spec', 'fury')) or 'fury').strip().lower() or 'fury',
            'battlenet_region': str(data.get('battlenet_region', fallback.get('battlenet_region', '')) or '').strip().lower(),
            'battlenet_realm': str(data.get('battlenet_realm', fallback.get('battlenet_realm', '')) or '').strip(),
            'battlenet_character': str(data.get('battlenet_character', fallback.get('battlenet_character', '')) or '').strip(),
            'player_equipment': str(data.get('player_equipment', fallback.get('player_equipment', '')) or '').strip(),
            'talent': str(data.get('talent', fallback.get('talent', '')) or '').strip(),
        }
        if mode == 'battlenet':
            values['player_equipment'] = ''
            if values['battlenet_region'] not in ('us', 'eu', 'kr', 'tw', 'cn'):
                raise ValueError('Battle.net region 必须是 us、eu、kr、tw 或 cn')
            if not values['battlenet_realm']:
                raise ValueError('Battle.net realm 不能为空')
            if not values['battlenet_character']:
                raise ValueError('Battle.net character 不能为空')
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
    def _coerce_profile_number(data, field, fallback=0):
        """Accept integer-form fields while rejecting malformed persisted configuration."""
        raw_value = data.get(field, fallback)
        if raw_value in (None, ''):
            return 0
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            raise ValueError(f'{field} 必须是整数')

    @classmethod
    def _profile_numeric_values(cls, data, fallback=None):
        fallback = fallback or {}
        return {
            field: cls._coerce_profile_number(data, field, fallback.get(field, 0))
            for field in ('gear_strength', 'gear_crit', 'gear_haste', 'gear_mastery', 'gear_versatility')
        }

    def post(self, request):
        """创建新的SimC配置或复制现有配置，或者为现有配置创建模拟任务"""
        try:
            data = json.loads(request.body)
            
            # 检查是否为一键模拟操作
            simulate_now = data.get('simulate_now', False)
            profile_id = data.get('profile_id')
            
            # 如果是一键模拟操作且提供了profile_id，直接创建任务
            if simulate_now and profile_id:
                try:
                    profile = SimcProfile.objects.get(
                        id=profile_id,
                        user_id=request.user.id,
                        is_active=True
                    )
                    
                    task_type = data.get('task_type', 1)
                    selected_attributes = data.get('selected_attributes')
                    regular_time = data.get('regular_time')
                    regular_target_count = data.get('regular_target_count')
                    attribute_step = data.get('attribute_step')
                    
                    task_result = self._create_simulation_task(
                        request.user.id, 
                        profile, 
                        task_type=task_type,
                        selected_attributes=selected_attributes,
                        regular_time=regular_time,
                        regular_target_count=regular_target_count,
                        attribute_step=attribute_step
                    )
                    
                    if task_result['success']:
                        return JsonResponse({
                            'success': True,
                            'message': '模拟任务创建成功',
                            'task_id': task_result['data']['id']
                        })
                    else:
                        return JsonResponse({
                            'success': False,
                            'message': '模拟任务创建失败: ' + task_result['error']
                        })
                        
                except SimcProfile.DoesNotExist:
                    return JsonResponse({
                        'success': False,
                        'message': 'SimC配置不存在'
                    })
            
            # 原有的创建配置逻辑
            # 验证必填字段
            name = data.get('name', '').strip()
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
            
            # 检查是否为复制操作
            copy_from_id = data.get('copy_from_id')
            
            if copy_from_id:
                try:
                    # 获取要复制的配置
                    source_profile = SimcProfile.objects.get(
                        id=copy_from_id,
                        user_id=request.user.id,
                        is_active=True
                    )
                    if self._profile_mode(source_profile) == 'attribute_only':
                        validate_player_baseline(source_profile.player_equipment)
                    
                    # 复制配置数据（只保留 spec + talent + gear stats）
                    profile = SimcProfile.objects.create(
                        user_id=request.user.id,
                        name=name,
                        spec=source_profile.spec,
                        player_config_mode=self._profile_mode(source_profile),
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
                        is_active=True
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
                        task_type = data.get('task_type', 1)
                        selected_attributes = data.get('selected_attributes')
                        regular_time = data.get('regular_time')
                        regular_target_count = data.get('regular_target_count')
                        attribute_step = data.get('attribute_step')
                        task_result = self._create_simulation_task(
                            request.user.id, 
                            profile, 
                            task_type=task_type,
                            selected_attributes=selected_attributes,
                            regular_time=regular_time,
                            regular_target_count=regular_target_count,
                            attribute_step=attribute_step
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
                    numeric_values = self._profile_numeric_values(data)
                except ValueError as e:
                    return JsonResponse({'success': False, 'error': str(e)})

                profile = SimcProfile.objects.create(
                    user_id=request.user.id,
                    name=name,
                    spec=values['spec'],
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
                    task_type = data.get('task_type', 1)
                    selected_attributes = data.get('selected_attributes')
                    regular_time = data.get('regular_time')
                    regular_target_count = data.get('regular_target_count')
                    attribute_step = data.get('attribute_step')
                    task_result = self._create_simulation_task(
                        request.user.id, 
                        profile, 
                        task_type=task_type,
                        selected_attributes=selected_attributes,
                        regular_time=regular_time,
                        regular_target_count=regular_target_count,
                        attribute_step=attribute_step
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
        except Exception as e:
            logger.error(f"创建SimC配置失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '创建SimC配置失败'
            })
    
    def _create_simulation_task(self, user_id, profile, task_type=1, selected_attributes=None, regular_time=None, regular_target_count=None, attribute_step=None):
        """创建模拟任务的辅助方法"""
        try:
            task_type = int(task_type or 1)
            frozen_player_equipment = profile.player_equipment or ''
            if self._profile_mode(profile) == 'attribute_only':
                frozen_player_equipment = resolve_attribute_player_baseline(profile.spec, frozen_player_equipment)
            if task_type not in (1, 2):
                raise ValueError('任务类型无效')
            if task_type == 2:
                selected_attributes = str(selected_attributes or '').strip()
                if not selected_attributes:
                    raise ValueError('属性模拟需要选择两项副属性')
                selected = SimcMonitor(None, None).parse_selected_attributes(selected_attributes)
                if len(selected) != 2:
                    raise ValueError('属性模拟需要选择两项有效副属性')
                try:
                    step = int(attribute_step) if attribute_step not in (None, '') else 50
                except (TypeError, ValueError):
                    raise ValueError('属性模拟步长必须是整数')
                if step != 50:
                    raise ValueError('四属性自动寻优固定使用 50 绿字步长')
                attribute_step = step

            # 新流程由执行器使用任务专属输出名并在实际生成后回写 result_file；不能预填 MD5 名阻断真实结果检测。
            result_file = ''
            
            # 根据任务类型生成任务名称
            if task_type == 2 and selected_attributes:
                task_name = f"{profile.name}_属性模拟_{selected_attributes}"
            else:
                try:
                    display_time = max(1, int(regular_time)) if regular_time not in (None, '') else 300
                except Exception:
                    display_time = 300
                try:
                    display_target_count = max(1, int(regular_target_count)) if regular_target_count not in (None, '') else 1
                except Exception:
                    display_target_count = 1
                task_name = f"{profile.name}_常规模拟_{display_time}s_{display_target_count}目标"
            
            ext_payload = {
                'spec': profile.spec,
                'talent': profile.talent or '',
                'player_config_mode': self._profile_mode(profile),
                'player_import_mode': self._profile_mode(profile),
                'player_equipment': frozen_player_equipment,
                'battlenet_region': profile.battlenet_region or '',
                'battlenet_realm': profile.battlenet_realm or '',
                'battlenet_character': profile.battlenet_character or '',
                'gear_strength': profile.gear_strength,
                'gear_crit': profile.gear_crit,
                'gear_haste': profile.gear_haste,
                'gear_mastery': profile.gear_mastery,
                'gear_versatility': profile.gear_versatility,
            }
            if task_type == 2:
                if selected_attributes:
                    ext_payload['selected_attributes'] = selected_attributes
                if attribute_step not in (None, ''):
                    ext_payload['attribute_step'] = max(1, int(attribute_step))
            else:
                # 常规模拟始终固化"最终生效"的时长和目标数，避免后续查看/执行链路歧义
                try:
                    effective_time = max(1, int(regular_time)) if regular_time not in (None, '') else 300
                except Exception:
                    effective_time = 300
                try:
                    effective_target_count = max(1, int(regular_target_count)) if regular_target_count not in (None, '') else 1
                except Exception:
                    effective_target_count = 1
                ext_payload['regular_time'] = effective_time
                ext_payload['regular_target_count'] = effective_target_count
                ext_payload['time'] = effective_time
                ext_payload['target_count'] = effective_target_count
                ext_payload['fight_style'] = 'Patchwerk'

            # 已保存配置的"立即模拟"也必须冻结三类执行输入，避免 Worker 执行时
            # 因基础模板或默认 APL 后续更新而改变已创建任务的语义。
            ext_payload = json.loads(SimcTaskAPIView()._build_task_ext(
                task_type=task_type,
                ext=ext_payload,
                owner_user_id=user_id,
                regular_time=regular_time,
                regular_target_count=regular_target_count,
                selected_attributes=selected_attributes,
                attribute_step=attribute_step,
                fight_style=ext_payload.get('fight_style'),
                time=ext_payload.get('time'),
                target_count=ext_payload.get('target_count'),
                player_config_mode=ext_payload.get('player_config_mode'),
                player_equipment=frozen_player_equipment,
                gear_strength=profile.gear_strength,
                gear_crit=profile.gear_crit,
                gear_haste=profile.gear_haste,
                gear_mastery=profile.gear_mastery,
                gear_versatility=profile.gear_versatility,
                talent=profile.talent or '',
                spec=profile.spec,
                battlenet_region=profile.battlenet_region or '',
                battlenet_realm=profile.battlenet_realm or '',
                battlenet_character=profile.battlenet_character or '',
            ))

            # 已保存配置的常规模拟必须在创建时生成不可变 v2 原子任务。
            # task_type=2 仍由属性扫描执行器负责展开多个点，不能误冻结成单次常规模拟。
            final_simc_content = None
            input_hash = ''
            fragment_manifest = None
            if task_type == 1 and ext_payload.get('player_config_mode') != 'battlenet':
                result_file = f'{uuid.uuid4().hex}.html'
                composer_request = {
                    'spec': profile.spec,
                    'player_import_mode': ext_payload.get('player_config_mode'),
                    'player_equipment': frozen_player_equipment,
                    'talent': profile.talent or '',
                    'fight_style': ext_payload.get('fight_style', 'Patchwerk'),
                    'time': ext_payload.get('time', 300),
                    'target_count': ext_payload.get('target_count', 1),
                    'gear_strength': profile.gear_strength,
                    'gear_crit': profile.gear_crit,
                    'gear_haste': profile.gear_haste,
                    'gear_mastery': profile.gear_mastery,
                    'gear_versatility': profile.gear_versatility,
                    'selected_apl_id': ext_payload.get('selected_apl_id'),
                    'override_action_list': ext_payload.get('override_action_list'),
                    'base_template_id': ext_payload.get('base_template_id'),
                    'base_template_content': ext_payload.get('base_template_content'),
                    'battlenet_region': profile.battlenet_region or '',
                    'battlenet_realm': profile.battlenet_realm or '',
                    'battlenet_character': profile.battlenet_character or '',
                    '_result_file_path': result_file,
                }
                composer = SimcComposer(user_id=user_id)
                final_simc_content, manifest, error = composer.compose(composer_request)
                if error:
                    raise ValueError(error)
                if not final_simc_content or manifest is None:
                    raise ValueError('Composer 未生成冻结任务正文')
                input_hash = SimcComposer.compute_input_hash(final_simc_content)
                fragment_manifest = manifest.to_json()

            # 创建SimcTask
            task = SimcTask.objects.create(
                user_id=user_id,
                name=task_name,
                simc_profile_id=profile.id,
                current_status=0,  # 待执行
                result_file=result_file,
                task_type=task_type,
                ext=json.dumps(ext_payload, ensure_ascii=False),
                final_simc_content=final_simc_content,
                input_hash=input_hash,
                fragment_manifest=fragment_manifest,
            )
            
            return {
                'success': True,
                'data': {
                    'id': task.id,
                    'name': task.name,
                    'current_status': task.current_status,
                    'result_file': task.result_file
                }
            }
            
        except Exception as e:
            logger.error(f"创建模拟任务失败: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def put(self, request):
        """更新SimC配置"""
        try:
            data = json.loads(request.body)
            profile_id = data.get('id')
            
            if not profile_id:
                return JsonResponse({
                    'success': False,
                    'error': '配置ID不能为空'
                })
            
            if data.get('status_only') is True:
                if not isinstance(data.get('is_active'), bool):
                    return JsonResponse({'success': False, 'error': 'is_active 必须是布尔值'})
                profile = SimcProfile.objects.get(
                    id=profile_id,
                    user_id=request.user.id,
                )
                profile.is_active = data['is_active']
                profile.save(update_fields=['is_active'])
                return JsonResponse({
                    'success': True,
                    'message': 'SimC配置已恢复' if profile.is_active else 'SimC配置已停用',
                })

            # 获取配置记录
            profile = SimcProfile.objects.get(
                id=profile_id,
                user_id=request.user.id,
                is_active=True
            )
            
            # 验证名称
            name = data.get('name', '').strip()
            if not name:
                return JsonResponse({
                    'success': False,
                    'error': '配置名称不能为空'
                })
            
            # 检查名称是否重复（排除当前记录）
            if SimcProfile.objects.filter(
                user_id=request.user.id,
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
                    'battlenet_region': profile.battlenet_region,
                    'battlenet_realm': profile.battlenet_realm,
                    'battlenet_character': profile.battlenet_character,
                    'player_equipment': profile.player_equipment,
                    'talent': profile.talent,
                })
                numeric_values = self._profile_numeric_values(data, {
                    field: getattr(profile, field, 0)
                    for field in ('gear_strength', 'gear_crit', 'gear_haste', 'gear_mastery', 'gear_versatility')
                })
            except ValueError as e:
                return JsonResponse({'success': False, 'error': str(e)})
            profile.name = name
            profile.spec = values['spec']
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
            profile.is_active = data.get('is_active', profile.is_active)
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
            
            # 软删除配置
            profile = SimcProfile.objects.get(
                id=profile_id,
                user_id=request.user.id,
                is_active=True
            )
            profile.is_active = False
            profile.save()
            
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
            # 从URL参数获取profile_id
            if not profile_id:
                return JsonResponse({
                    'success': False,
                    'error': '配置ID不能为空'
                })
            
            # 获取配置并检查权限
            try:
                profile = SimcProfile.objects.get(
                    id=profile_id,
                    user_id=request.user.id,
                    is_active=True
                )
            except SimcProfile.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': 'SimC配置不存在或无权限访问'
                })
            
            # 创建模拟任务
            task_result = self._create_simulation_task(request.user.id, profile)
            
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
            )
            return JsonResponse({'success': True, 'data': data})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})

    def post(self, request):
        try:
            data = json.loads(request.body or '{}')
            profile_id = data.get('profile_id')
            include_base = bool(data.get('include_base', True))
            candidate_count = int(data.get('candidate_count', 5) or 5)
            candidate_count = max(1, min(candidate_count, 5))

            if not profile_id:
                return JsonResponse({'success': False, 'error': 'profile_id不能为空'})

            profile = SimcProfile.objects.filter(
                id=profile_id,
                user_id=request.user.id,
                is_active=True
            ).first()
            if not profile:
                return JsonResponse({'success': False, 'error': 'SimC配置不存在或无权限访问'})

            apl_template = _get_unique_default_apl_for_spec(
                profile.spec,
                owner_user_id=request.user.id,
            )
            base_apl = str(apl_template.content if apl_template else '').strip()
            if not base_apl:
                return JsonResponse({'success': False, 'error': '当前配置缺少基础APL，无法生成候选方案'})

            created = self._create_compare_preprocessing_tasks(
                user_id=request.user.id,
                profile=profile,
                include_base=include_base,
                candidate_count=candidate_count
            )
            task_ids = [x['task_id'] for x in created]
            batch_id = created[0]['batch_id'] if created else ''
            self._start_compare_preprocess_async(
                user_id=request.user.id,
                profile_id=profile.id,
                batch_id=batch_id,
                task_ids=task_ids,
                include_base=include_base,
                candidate_count=candidate_count
            )
            return JsonResponse({
                'success': True,
                'message': f'已创建 {len(task_ids)} 个对比任务，进入预处理阶段',
                'data': {
                    'profile_id': profile.id,
                    'profile_name': profile.name,
                    'candidate_count': candidate_count,
                    'include_base': include_base,
                    'simulation_started': False,
                    'preprocessing_started': True,
                    'batch_id': batch_id,
                    'task_ids': task_ids,
                    'tasks': created
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

    def _create_compare_preprocessing_tasks(self, user_id, profile, include_base, candidate_count):
        total_count = int(candidate_count) + (1 if include_base else 0)
        if total_count <= 0:
            raise Exception('候选数量无效')
        frozen_inputs = json.loads(SimcTaskAPIView()._build_task_ext(
            task_type=1,
            ext={},
            owner_user_id=user_id,
            fight_style='Patchwerk',
            time=300,
            target_count=1,
            player_config_mode=SimcProfileAPIView._profile_mode(profile),
            player_equipment=profile.player_equipment or '',
            gear_strength=profile.gear_strength,
            gear_crit=profile.gear_crit,
            gear_haste=profile.gear_haste,
            gear_mastery=profile.gear_mastery,
            gear_versatility=profile.gear_versatility,
            talent=profile.talent or '',
            spec=profile.spec,
            battlenet_region=profile.battlenet_region or '',
            battlenet_realm=profile.battlenet_realm or '',
            battlenet_character=profile.battlenet_character or '',
        ))
        batch_id = uuid.uuid4().hex[:12]
        created = []
        for idx in range(total_count):
            is_base = bool(include_base and idx == 0)
            plan_name = '基础方案' if is_base else f'候选方案{idx}'
            display_name = f"{profile.name}_APL对比_{idx:02d}_预处理中"
            ext_payload = dict(frozen_inputs)
            ext_payload['apl_compare'] = {
                'batch_id': batch_id,
                'candidate_index': idx,
                'candidate_name': plan_name,
                'candidate_reason': '等待预处理生成',
                'is_base': is_base,
                'preprocess_stage': 'pending'
            }
            task = SimcTask.objects.create(
                user_id=user_id,
                name=display_name,
                simc_profile_id=profile.id,
                current_status=4,
                result_file='',
                task_type=1,
                ext=json.dumps(ext_payload, ensure_ascii=False)
            )
            created.append({
                'batch_id': batch_id,
                'task_id': task.id,
                'task_name': task.name,
                'candidate_name': plan_name,
                'candidate_reason': '等待预处理生成',
                'is_base': is_base,
                'status': 4
            })
        return created

    def _start_compare_preprocess_async(self, user_id, profile_id, batch_id, task_ids, include_base, candidate_count):
        ids = [int(x) for x in (task_ids or []) if str(x).isdigit()]
        if not ids:
            return

        def _runner():
            try:
                from django.db import close_old_connections
                close_old_connections()

                profile = SimcProfile.objects.filter(
                    id=profile_id,
                    user_id=user_id,
                    is_active=True
                ).first()
                if not profile:
                    self._mark_preprocess_failed(ids, '预处理失败: 配置不存在或已删除')
                    close_old_connections()
                    return

                first_task = SimcTask.objects.filter(id__in=ids, user_id=user_id, is_active=True).order_by('id').first()
                first_ext = {}
                if first_task:
                    try:
                        first_ext = json.loads(first_task.ext or '{}')
                    except Exception:
                        first_ext = {}
                base_apl = str(first_ext.get('override_action_list') or '').strip()
                if not base_apl:
                    self._mark_preprocess_failed(ids, '预处理失败: 当前配置缺少基础APL')
                    close_old_connections()
                    return

                generated_candidates = self._generate_glm_candidates(profile, base_apl, int(candidate_count))
                plans = []
                if include_base:
                    plans.append({
                        'name': '基础方案',
                        'apl_list': base_apl,
                        'reason': '当前配置中的原始APL'
                    })
                plans.extend(generated_candidates)

                if len(plans) != len(ids):
                    self._mark_preprocess_failed(ids, f'预处理失败: 方案数量不匹配（任务{len(ids)}，方案{len(plans)}）')
                    close_old_connections()
                    return

                for idx, task_id in enumerate(ids):
                    task = SimcTask.objects.filter(id=task_id, user_id=user_id, is_active=True).first()
                    if not task:
                        continue
                    if int(task.current_status or 0) != 4:
                        continue

                    plan = plans[idx] if idx < len(plans) else {}
                    plan_name = str(plan.get('name') or '').strip() or f'候选方案{idx}'
                    plan_reason = str(plan.get('reason') or '').strip()
                    apl_list = str(plan.get('apl_list') or '').strip()
                    if not apl_list:
                        task.current_status = 3
                        task.result_file = '预处理失败: APL为空'
                        task.save(update_fields=['current_status', 'result_file', 'modified_time'])
                        continue

                    timestamp = str(int(time.time()))
                    content_to_hash = f"{timestamp}:{user_id}:{profile.id}:{idx}:{plan_name}:{batch_id}"
                    result_file = hashlib.md5(content_to_hash.encode('utf-8')).hexdigest() + '.html'
                    display_name = f"{profile.name}_APL对比_{idx:02d}_{plan_name[:20]}"

                    ext_payload = {}
                    try:
                        ext_payload = json.loads(task.ext or '{}')
                        if not isinstance(ext_payload, dict):
                            ext_payload = {}
                    except Exception:
                        ext_payload = {}
                    compare_payload = ext_payload.get('apl_compare') if isinstance(ext_payload.get('apl_compare'), dict) else {}
                    compare_payload.update({
                        'batch_id': batch_id,
                        'candidate_index': idx,
                        'candidate_name': plan_name,
                        'candidate_reason': plan_reason,
                        'is_base': bool(include_base and idx == 0),
                        'preprocess_stage': 'done'
                    })
                    ext_payload['apl_compare'] = compare_payload
                    ext_payload['override_action_list'] = apl_list

                    task.name = display_name
                    task.result_file = result_file
                    task.ext = json.dumps(ext_payload, ensure_ascii=False)
                    task.current_status = 0
                    task.save(update_fields=['name', 'result_file', 'ext', 'current_status', 'modified_time'])
                # 预处理完成后仅置为待处理，由后端bot统一调度执行，避免本地重复触发模拟
                close_old_connections()
            except Exception as e:
                logger.error(f"APL候选对比预处理失败: {str(e)}\n{traceback.format_exc()}")
                self._mark_preprocess_failed(ids, f'预处理失败: {str(e)}')

        t = threading.Thread(target=_runner, daemon=True)
        t.start()

    def _mark_preprocess_failed(self, task_ids, error_message):
        ids = [int(x) for x in (task_ids or []) if str(x).isdigit()]
        if not ids:
            return
        message = str(error_message or '预处理失败').strip()
        reasoning_text = ''
        m = re.search(r'reasoning_preview=(.*)$', message, re.S)
        if m:
            reasoning_text = m.group(1).strip()
            message = message[:m.start()].strip().rstrip('|').strip()
        if len(message) > 1500:
            message = message[:1500] + ' ...'
        if len(reasoning_text) > 5000:
            reasoning_text = reasoning_text[:5000] + ' ...'
        tasks = SimcTask.objects.filter(id__in=ids, is_active=True)
        for task in tasks:
            if int(task.current_status or 0) != 4:
                continue
            ext_payload = {}
            try:
                ext_payload = json.loads(task.ext or '{}')
                if not isinstance(ext_payload, dict):
                    ext_payload = {}
            except Exception:
                ext_payload = {}
            compare_payload = ext_payload.get('apl_compare') if isinstance(ext_payload.get('apl_compare'), dict) else {}
            compare_payload.update({
                'preprocess_stage': 'failed',
                'preprocess_error': message
            })
            if reasoning_text:
                compare_payload['preprocess_reasoning'] = reasoning_text
            ext_payload['apl_compare'] = compare_payload
            task.current_status = 3
            task.result_file = message
            task.ext = json.dumps(ext_payload, ensure_ascii=False)
            task.save(update_fields=['current_status', 'result_file', 'ext', 'modified_time'])

@method_decorator([csrf_exempt], name='dispatch')
class KeywordTranslationAPIView(View):
    """
    关键字翻译API - 用于SimC结果分析页面
    """
    
    def get(self, request):
        """获取所有活跃的关键字映射"""
        try:
            import re
            # 获取所有活跃的关键字映射
            keywords = SimcAplKeywordPair.objects.filter(is_active=True)
            
            # 构建映射字典
            translation_map = {}
            
            def title_case(s):
                parts = re.split(r'[\s_\-]+', s.strip())
                return ' '.join(p.capitalize() for p in parts if p)
            
            for keyword in keywords:
                apl = (keyword.apl_keyword or '').strip()
                cn = (keyword.cn_keyword or '').strip()
                if not apl or not cn:
                    continue
                
                # 原始键
                translation_map[apl] = cn
                
                # 常见变体：下划线、空格、连字符、大小写、标题化
                variants = set()
                variants.add(apl.lower())
                variants.add(apl.replace('_', ' '))
                variants.add(apl.replace('_', '-'))
                
                v_space = apl.replace('_', ' ')
                variants.add(v_space.lower())
                variants.add(title_case(v_space))
                
                v_hyphen = apl.replace('_', '-')
                variants.add(v_hyphen.lower())
                variants.add(title_case(v_hyphen))
                
                # 下划线标题化（少见，但兼容）
                v_underscore_title = '_'.join(w.capitalize() for w in apl.split('_') if w)
                if v_underscore_title:
                    variants.add(v_underscore_title)
                
                for v in variants:
                    if not v:
                        continue
                    translation_map.setdefault(v, cn)
            
            return JsonResponse({
                'success': True,
                'translations': translation_map
            })
            
        except Exception as e:
            logger.error(f"获取关键字翻译映射失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '获取翻译映射失败'
            })


@method_decorator([csrf_exempt], name='dispatch')
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
        profile = None
        if not manifest and task.simc_profile_id:
            profile = SimcProfile.objects.filter(id=task.simc_profile_id, user_id=request.user.id).first()
        context = {
            'id': task.id,
            'name': task.name,
            'task_type': task.task_type,
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
            'batch_compare': manifest.get('batch_compare') or {},
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
            if not SimcTask.objects.filter(user_id=request.user.id, is_active=True).filter(
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
            
            task_ext = SimcRegularCompareAPIView()._parse_task_ext(task.ext) or {}
            raw_manifest = task_ext.get('batch_compare')
            batch_manifest = raw_manifest if isinstance(raw_manifest, dict) else {}
            is_four_stat_batch = batch_manifest.get('kind') == 'attribute_variants' and bool(batch_manifest.get('batch_id'))
            if task.task_type != 2 and not is_four_stat_batch:
                return JsonResponse({
                    'success': False,
                    'error': '该任务不是属性模拟或四属性寻优批次'
                })
            if not task.result_file and not is_four_stat_batch:
                return JsonResponse({
                    'success': False,
                    'error': '任务尚未完成或无结果文件'
                })
            
            # 旧式属性任务由一个任务持有多个受控属性报告；四属性批次的候选
            # 各自属于常规任务，后面统一由 batch manifest 聚合。
            result_files = task.result_file.split(',') if task.task_type == 2 else []
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
            task_ext = SimcRegularCompareAPIView()._parse_task_ext(task.ext) or {}
            raw_manifest = task_ext.get('batch_compare')
            batch_manifest = raw_manifest if isinstance(raw_manifest, dict) else {}
            batch_id = str(batch_manifest.get('batch_id') or '').strip()
            if batch_manifest.get('kind') == 'attribute_variants' and batch_id:
                batch_tasks = []
                for batch_task in SimcTask.objects.filter(user_id=request.user.id, is_active=True, task_type=1).order_by('id'):
                    ext_payload = SimcRegularCompareAPIView()._parse_task_ext(batch_task.ext) or {}
                    raw_candidate_manifest = ext_payload.get('batch_compare')
                    manifest = raw_candidate_manifest if isinstance(raw_candidate_manifest, dict) else {}
                    if manifest.get('batch_id') == batch_id and manifest.get('kind') == 'attribute_variants':
                        batch_tasks.append((batch_task, manifest))
                if batch_tasks:
                    attribute_report = SimcRegularCompareAPIView()._build_attribute_report(batch_tasks)
            
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
    def _batch_round(manifest_or_candidate):
        try:
            candidate = manifest_or_candidate.get('candidate', manifest_or_candidate)
            return int((candidate or {}).get('round') or 1)
        except (AttributeError, TypeError, ValueError):
            return 1

    def _build_attribute_report(self, batch_tasks):
        """Return a truthful report for the measured 50-rating local search only."""
        stats = SimcBatchTaskAPIView.ATTRIBUTE_STATS
        tolerance = SimcBatchTaskAPIView.ATTRIBUTE_DPS_TOLERANCE
        candidates = []
        centers = []
        invalid = []
        for task, manifest in batch_tasks:
            ext = self._parse_task_ext(task.ext)
            candidate = manifest.get('candidate') or {}
            ratings = {stat: ext.get(f'gear_{stat}') for stat in stats}
            round_number = self._batch_round(manifest)
            row = {
                'id': task.id, 'label': manifest.get('label') or task.name,
                'round': round_number, 'is_center': bool(manifest.get('is_base')),
                'move': candidate.get('move') or {}, 'ratings': ratings,
                'result_file': task.result_file or '', 'status': task.current_status,
                'dps': None,
            }
            if any(value is None for value in ratings.values()):
                invalid.append({'id': task.id, 'error': '候选缺少四项绿字'})
                candidates.append(row)
                continue
            try:
                row['ratings'] = {stat: int(ratings[stat]) for stat in stats}
            except (TypeError, ValueError):
                invalid.append({'id': task.id, 'error': '候选绿字无效'})
                candidates.append(row)
                continue
            if task.current_status == 2 and task.result_file:
                html_content = self._get_result_file_content(task.result_file)
                parsed = self._parse_regular_result(html_content) if html_content else {}
                if parsed.get('dps') is None:
                    invalid.append({'id': task.id, 'error': '无法解析该候选的独立 DPS 结果'})
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
        return {
            'algorithm': 'four_stat_pairwise_hill_climb', 'algorithm_version': 2,
            'step': SimcBatchTaskAPIView.ATTRIBUTE_SEARCH_STEP,
            'tolerance': tolerance, 'rounds_completed': len({row['round'] for row in candidates if row['dps'] is not None}),
            'current_round': current_round, 'total_rating': sum(first_center['ratings'].values()) if first_center else None,
            'initial_ratings': first_center['ratings'] if first_center else {},
            'recommendation': recommendation, 'stop_reason': stop_reason,
            'local_optimum': stop_reason == 'local_optimum_50_pairwise',
            'search_path': path, 'candidates': ranked, 'all_candidates': candidates, 'invalid': invalid,
        }
    
    def get(self, request):
        try:
            batch_id = str(request.GET.get('batch_id') or '').strip()
            if batch_id:
                database_batch = None
                try:
                    database_batch = SimcTaskBatch.objects.get(
                        id=int(batch_id), user_id=request.user.id, is_active=True,
                    )
                except (TypeError, ValueError, SimcTaskBatch.DoesNotExist):
                    database_batch = None

                batch_tasks = []
                if database_batch is not None:
                    task_queryset = SimcTask.objects.filter(
                        user_id=request.user.id, is_active=True, task_type=1,
                        batch=database_batch,
                    ).order_by('id')
                    for task in task_queryset:
                        ext_payload = self._parse_task_ext(task.ext)
                        manifest = ext_payload.get('batch_compare') if isinstance(ext_payload.get('batch_compare'), dict) else {}
                        batch_tasks.append((task, manifest))
                else:
                    # Read-only compatibility for historical UUID batches that predate
                    # SimcTask.batch. Numeric database batches never fall back to ext.
                    for task in SimcTask.objects.filter(user_id=request.user.id, is_active=True, task_type=1).order_by('id'):
                        ext_payload = self._parse_task_ext(task.ext)
                        manifest = ext_payload.get('batch_compare') if isinstance(ext_payload.get('batch_compare'), dict) else {}
                        if manifest.get('batch_id') != batch_id:
                            continue
                        batch_tasks.append((task, manifest))
                if not batch_tasks:
                    return JsonResponse({'success': False, 'error': '比较批次不存在或无权限访问'})
                status_counts = {'pending': 0, 'running': 0, 'succeeded': 0, 'failed': 0}
                invalid = []
                rows = []
                for task, manifest in batch_tasks:
                    status_key = {0: 'pending', 1: 'running', 2: 'succeeded', 3: 'failed'}.get(task.current_status, 'failed')
                    status_counts[status_key] += 1
                    candidate = manifest.get('candidate') or {}
                    row = {
                        'id': task.id, 'name': task.name, 'label': manifest.get('label') or task.name,
                        'index': manifest.get('index'), 'is_base': bool(manifest.get('is_base')),
                        'candidate': candidate, 'current_status': task.current_status,
                        'dps': None, 'result_file': '',
                    }
                    if task.current_status == 2 and task.result_file:
                        html_content = self._get_result_file_content(task.result_file)
                        parsed = self._parse_regular_result(html_content) if html_content else {}
                        row['dps'] = parsed.get('dps')
                        row['character'] = parsed.get('character', {})
                        row['simulation'] = parsed.get('simulation', {})
                        row['talents'] = parsed.get('talents', {})
                        row['abilities'] = parsed.get('abilities', [])
                        row['top_abilities'] = parsed.get('top_abilities', [])
                        row['apl_list'] = ''
                        row['candidate_name'] = manifest.get('label') or ''
                        row['is_base_candidate'] = bool(manifest.get('is_base'))
                        row['candidate_index'] = manifest.get('index')
                        if row['dps'] is None:
                            invalid.append({'id': task.id, 'error': '无法解析该候选的独立 DPS 结果'})
                        else:
                            result_summary = SimcTaskAPIView()._task_result_file_summary(task)
                            if result_summary:
                                row['result_file'] = result_summary
                    rows.append(row)
                rows.sort(key=lambda row: (row['index'] is None, row['index'] if row['index'] is not None else row['id']))
                completed_rows = [row for row in rows if row.get('dps') is not None]
                ranked_rows = sorted(completed_rows, key=lambda row: (-row['dps'], row['id']))
                rank_by_id = {row['id']: rank for rank, row in enumerate(ranked_rows, start=1)}
                baseline_row = next((row for row in rows if row.get('is_base')), None)
                baseline_dps = baseline_row.get('dps') if baseline_row else None
                for row in rows:
                    row['rank'] = rank_by_id.get(row['id'])
                    if row.get('dps') is not None and baseline_dps is not None:
                        row['delta_dps'] = row['dps'] - baseline_dps
                        row['delta_percent'] = round((row['delta_dps'] / baseline_dps) * 100, 2) if baseline_dps else None
                    else:
                        row['delta_dps'] = None
                        row['delta_percent'] = None
                winner_row = ranked_rows[0] if ranked_rows else None
                comparison = {
                    'baseline': ({'id': baseline_row['id'], 'label': baseline_row['label'], 'dps': baseline_row['dps']} if baseline_row else None),
                    'winner': ({'id': winner_row['id'], 'label': winner_row['label'], 'dps': winner_row['dps'],
                                'delta_dps': winner_row.get('delta_dps'), 'delta_percent': winner_row.get('delta_percent')} if winner_row else None),
                }
                first_manifest = batch_tasks[0][1]
                current_round = max([self._batch_round(manifest) for _, manifest in batch_tasks] or [1])
                active_rows = [row for row in rows if self._batch_round(row.get('candidate') or {}) == current_round]
                active_counts = {'pending': 0, 'running': 0, 'succeeded': 0, 'failed': 0}
                for row in active_rows:
                    active_counts[{0: 'pending', 1: 'running', 2: 'succeeded', 3: 'failed'}.get(row['current_status'], 'failed')] += 1
                attribute_report = self._build_attribute_report(batch_tasks) if first_manifest.get('kind') == 'attribute_variants' else None
                batch_payload = {
                    'batch_id': batch_id,
                    'name': database_batch.name if database_batch is not None else '',
                    'status': database_batch.status if database_batch is not None else None,
                    'kind': first_manifest.get('kind'), 'total': len(rows),
                    'current_round': current_round, 'current_round_total': len(active_rows),
                    **status_counts, 'current_round_status': active_counts,
                }
                # This browser endpoint is always summary-only. A query flag must never
                # expose candidate manifests, parsed report bodies, APL, result locations,
                # or filenames embedded in an attribute search path.
                summary_rows = [{
                    'id': row['id'], 'name': row['name'], 'label': row['label'],
                    'rank': row['rank'], 'dps': row['dps'],
                    'delta_dps': row['delta_dps'], 'delta_percent': row['delta_percent'],
                } for row in rows]
                safe_attribute_report = None
                if attribute_report:
                    safe_candidate_fields = ('id', 'label', 'round', 'is_center', 'ratings', 'dps')
                    def safe_candidate(value):
                        if not isinstance(value, dict):
                            return None
                        return {key: value.get(key) for key in safe_candidate_fields if key in value}
                    safe_attribute_report = {
                        key: attribute_report.get(key)
                        for key in (
                            'algorithm', 'algorithm_version', 'step', 'tolerance',
                            'rounds_completed', 'current_round', 'total_rating',
                            'initial_ratings', 'stop_reason', 'local_optimum',
                        )
                    }
                    safe_attribute_report['recommendation'] = safe_candidate(attribute_report.get('recommendation'))
                    safe_attribute_report['search_path'] = [{
                        key: point.get(key) for key in ('round', 'ratings', 'dps') if key in point
                    } for point in attribute_report.get('search_path', []) if isinstance(point, dict)]
                    safe_attribute_report['candidates'] = [
                        safe_candidate(value) for value in attribute_report.get('candidates', [])
                        if isinstance(value, dict)
                    ]
                return JsonResponse({'success': True, 'data': {
                    'batch': batch_payload,
                    'tasks': summary_rows,
                    'comparison': comparison,
                    'attribute_report': safe_attribute_report,
                    'invalid': [{'id': item.get('id'), 'error': item.get('error', '')} for item in invalid],
                }})

            task_ids_raw = request.GET.get('task_ids', '')
            task_ids = []
            for part in task_ids_raw.split(','):
                part = part.strip()
                if not part:
                    continue
                try:
                    task_id = int(part)
                except ValueError:
                    continue
                task_ids.append(task_id)
            
            unique_task_ids = []
            seen = set()
            for task_id in task_ids:
                if task_id in seen:
                    continue
                seen.add(task_id)
                unique_task_ids.append(task_id)
            
            if len(unique_task_ids) < 2:
                return JsonResponse({
                    'success': False,
                    'error': '请至少选择2个任务进行对比'
                })
            
            if len(unique_task_ids) > 8:
                unique_task_ids = unique_task_ids[:8]
            
            tasks_data = []
            invalid = []
            
            for task_id in unique_task_ids:
                try:
                    task = SimcTask.objects.get(id=task_id, user_id=request.user.id, is_active=True)
                except SimcTask.DoesNotExist:
                    invalid.append({'id': task_id, 'error': '任务不存在或无权限访问'})
                    continue
                
                if task.task_type != 1:
                    invalid.append({'id': task.id, 'name': task.name, 'error': '仅支持常规模拟任务对比'})
                    continue
                
                if task.current_status != 2:
                    invalid.append({'id': task.id, 'name': task.name, 'error': '任务未完成'})
                    continue
                
                if not task.result_file or not isinstance(task.result_file, str) or not task.result_file.endswith('.html') or '\n' in task.result_file:
                    invalid.append({'id': task.id, 'name': task.name, 'error': '任务结果文件无效'})
                    continue
                
                html_content = self._get_result_file_content(task.result_file)
                if not html_content:
                    invalid.append({'id': task.id, 'name': task.name, 'error': '无法获取结果文件内容'})
                    continue
                
                parsed = self._parse_regular_result(html_content)
                if not parsed.get('dps'):
                    invalid.append({'id': task.id, 'name': task.name, 'error': '无法从结果文件中解析DPS'})
                    continue

                tasks_data.append({
                    'id': task.id,
                    'name': task.name,
                    'label': task.name,
                    'dps': parsed.get('dps'),
                    'rank': None,
                    'delta_dps': None,
                    'delta_percent': None,
                })
            
            if len(tasks_data) < 2:
                return JsonResponse({
                    'success': False,
                    'error': '可用于对比的任务不足2个',
                    'invalid': [{'id': item.get('id'), 'error': item.get('error', '')} for item in invalid],
                    'data': {'tasks': tasks_data},
                })
            
            ranked = sorted(tasks_data, key=lambda row: (-row['dps'], row['id']))
            rank_by_id = {row['id']: index for index, row in enumerate(ranked, 1)}
            baseline_dps = tasks_data[0]['dps']
            for row in tasks_data:
                row['rank'] = rank_by_id[row['id']]
                row['delta_dps'] = row['dps'] - baseline_dps
                row['delta_percent'] = round((row['delta_dps'] / baseline_dps) * 100, 2) if baseline_dps else None

            return JsonResponse({
                'success': True,
                'data': {
                    'tasks': tasks_data,
                    'invalid': [{'id': item.get('id'), 'error': item.get('error', '')} for item in invalid]
                }
            })
            
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
    """
    SimC模板API
    """

    @staticmethod
    def _protected_response():
        return JsonResponse({'success': False, 'error': '默认玩家装备模板仅允许通过受控导入命令维护'}, status=403)

    @staticmethod
    def _is_protected(template):
        return template.template_type == SimcContentTemplate.TYPE_DEFAULT_PLAYER

    @staticmethod
    def _get_writable_template(request, template_id):
        """Return a writable template without exposing another user's private row."""
        template = SimcContentTemplate.objects.filter(id=template_id).first()
        if not template:
            return None, JsonResponse({'success': False, 'error': '模板不存在'}, status=404)
        if template.source == SimcContentTemplate.SOURCE_SIMC_UPSTREAM:
            return None, JsonResponse({'success': False, 'error': '上游模板为只读资源'}, status=403)
        if template.owner_user_id == request.user.id:
            return template, None
        if template.owner_user_id is None:
            if request.user.is_staff or request.user.is_superuser:
                return template, None
            return None, JsonResponse({'success': False, 'error': '系统模板仅管理员可修改'}, status=403)
        return None, JsonResponse({'success': False, 'error': '模板不存在'}, status=404)

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

    @staticmethod
    def _validate_default_player_baseline(content):
        """验证 default_player 内容必须是合法的玩家配置块。"""
        # default_player 应该包含玩家定义，不应该包含全局配置如 fight_style/max_time
        for line in content.split('\n'):
            stripped = line.strip()
            if stripped.startswith('fight_style=') or stripped.startswith('max_time=') or stripped.startswith('desired_targets='):
                return f'默认玩家配置不允许包含全局运行参数（发现: {stripped[:50]}）'
        return None
    
    def get(self, request):
        """获取SimC模板列表或单个模板内容"""
        try:
            template_id = request.GET.get('id')

            if template_id:
                # 获取单个模板的完整内容
                try:
                    template = SimcContentTemplate.objects.filter(
                        models.Q(owner_user_id=request.user.id) | models.Q(owner_user_id__isnull=True),
                        id=template_id,
                    ).first()
                    if not template:
                        raise SimcContentTemplate.DoesNotExist
                    return JsonResponse({
                        'success': True,
                        'id': template.id,
                        'template_content': template.content,
                        'content': template.content,
                        'spec': template.spec,
                        'class_name': template.class_name,
                        'name': template.name,
                        'template_type': template.template_type,
                        'source': template.source,
                        'is_active': template.is_active,
                        'is_selectable': template.is_selectable,
                    })
                except SimcContentTemplate.DoesNotExist:
                    return JsonResponse({
                        'success': False,
                        'error': '模板不存在'
                    })
            else:
                # 模板管理支持四类内容：base_template、default_apl、custom_apl、default_player
                template_type = request.GET.get('template_type') or SimcContentTemplate.TYPE_BASE_TEMPLATE
                if template_type not in dict(SimcContentTemplate.TEMPLATE_TYPE_CHOICES):
                    return JsonResponse({'success': False, 'error': '无效的模板类型'}, status=400)
                templates = SimcContentTemplate.objects.filter(
                    models.Q(owner_user_id=request.user.id) | models.Q(owner_user_id__isnull=True),
                    template_type=template_type,
                ).order_by('source', 'spec', '-id')
                template_list = []

                for template in templates:
                    preview = template.content[:100] + '...' if len(template.content) > 100 else template.content
                    template_list.append({
                        'id': template.id,
                        'preview': preview,
                        'spec': template.spec,
                        'class_name': template.class_name,
                        'name': template.name,
                        'template_type': template.template_type,
                        'source': template.source,
                        'is_active': template.is_active,
                        'is_selectable': template.is_selectable,
                    })

                return JsonResponse({
                    'success': True,
                    'templates': template_list
                })

        except Exception as e:
            logger.error(f"获取SimC模板失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '获取SimC模板失败'
            })
    
    def put(self, request):
        """更新SimC模板内容"""
        try:
            # 解析请求数据
            data = json.loads(request.body)
            template_id = request.GET.get('id') or data.get('id')
            template_content = data.get('template_content', '') or data.get('content', '') or data.get('template', '')
            template_spec = (str(data.get('spec') or '').strip().lower() or None)
            template_name = str(data.get('name') or '').strip()
            template_type = data.get('template_type') or data.get('type')
            source = data.get('source')
            is_selectable = data.get('is_selectable')
            is_active = data.get('is_active')

            if not template_id:
                return JsonResponse({
                    'success': False,
                    'error': '模板ID不能为空'
                })

            if not template_content:
                return JsonResponse({
                    'success': False,
                    'error': '模板内容不能为空'
                })

            # 获取并更新模板
            try:
                template, error_response = self._get_writable_template(request, template_id)
                if error_response:
                    return error_response
                if self._is_protected(template):
                    # 拒绝改 template_type、source、spec
                    if template_type and template_type != template.template_type:
                        return self._protected_response()
                    if source and source != template.source:
                        return self._protected_response()
                    if template_spec is not None and template_spec != template.spec:
                        return self._protected_response()

                    # default_player 必须通过 validate_default_player_baseline 验证
                    validation_error = self._validate_default_player_baseline(template_content)
                    if validation_error:
                        return JsonResponse({
                            'success': False,
                            'error': validation_error
                        }, status=400)

                    # 允许更新 content、name、is_selectable、is_active
                    template.content = template_content
                    if template_name:
                        template.name = template_name
                    if is_selectable is not None:
                        template.is_selectable = bool(is_selectable)
                    if is_active is not None:
                        template.is_active = bool(is_active)
                else:
                    # 非 default_player 可以自由更新所有字段
                    if template_type == SimcContentTemplate.TYPE_DEFAULT_PLAYER:
                        return self._protected_response()

                    # base_template 必须恰好一个 {player_config} 占位符，不允许 actor= 行
                    final_type = template_type if template_type in dict(SimcContentTemplate.TEMPLATE_TYPE_CHOICES) else template.template_type
                    if final_type == SimcContentTemplate.TYPE_BASE_TEMPLATE:
                        validation_error = self._validate_base_template(template_content)
                        if validation_error:
                            return JsonResponse({
                                'success': False,
                                'error': validation_error
                            }, status=400)

                    template.content = template_content
                    if template_spec is not None:
                        template.spec = template_spec
                    if template_name:
                        template.name = template_name
                    if template_type in dict(SimcContentTemplate.TEMPLATE_TYPE_CHOICES):
                        template.template_type = template_type
                    if source in dict(SimcContentTemplate.SOURCE_CHOICES):
                        template.source = source
                    if is_selectable is not None:
                        template.is_selectable = bool(is_selectable)
                    if is_active is not None:
                        template.is_active = bool(is_active)

                template.save()

                logger.info(f"SimC模板/APL已更新: ID {template.id}")

                return JsonResponse({
                    'success': True,
                    'message': '模板更新成功'
                })
            except SimcContentTemplate.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': '模板不存在'
                })

        except Exception as e:
            logger.error(f"更新SimC模板失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '更新SimC模板失败'
            })
    
    def patch(self, request):
        """更新模板状态（启用/禁用）"""
        try:
            # 解析请求数据
            data = json.loads(request.body)
            template_id = request.GET.get('id') or data.get('id')
            is_active = data.get('is_active')
            
            if not template_id:
                return JsonResponse({
                    'success': False,
                    'error': '模板ID不能为空'
                })
            
            if is_active is None:
                return JsonResponse({
                    'success': False,
                    'error': '状态参数不能为空'
                })
            
            # 获取并更新模板状态
            try:
                template, error_response = self._get_writable_template(request, template_id)
                if error_response:
                    return error_response
                if self._is_protected(template):
                    return self._protected_response()
                template.is_active = is_active
                if 'is_selectable' in data:
                    template.is_selectable = bool(data.get('is_selectable'))
                template.save()
                
                status_text = '启用' if is_active else '禁用'
                logger.info(f"SimC模板/APL已{status_text}: ID {template.id}")
                
                return JsonResponse({
                    'success': True,
                    'message': f'模板{status_text}成功'
                })
            except SimcContentTemplate.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': '模板不存在'
                })
                
        except Exception as e:
            logger.error(f"更新模板状态失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '更新模板状态失败'
            })
    
    def post(self, request):
        """新增SimC模板"""
        try:
            # 解析请求数据
            data = json.loads(request.body)
            template_content = data.get('template_content', '') or data.get('content', '') or data.get('template', '')
            template_spec = (str(data.get('spec') or '').strip().lower() or 'default')
            template_type = data.get('template_type') or data.get('type') or SimcContentTemplate.TYPE_BASE_TEMPLATE
            if template_type not in dict(SimcContentTemplate.TEMPLATE_TYPE_CHOICES):
                template_type = SimcContentTemplate.TYPE_BASE_TEMPLATE
            if template_type == SimcContentTemplate.TYPE_DEFAULT_PLAYER:
                return self._protected_response()
            source = SimcContentTemplate.SOURCE_USER
            template_name = str(data.get('name') or '').strip() or ('个人APL' if template_type == SimcContentTemplate.TYPE_CUSTOM_APL else '基础模板')
            class_name = str(data.get('class_name') or '').strip().lower()
            is_selectable = data.get('is_selectable')

            if not template_content:
                return JsonResponse({
                    'success': False,
                    'error': '模板内容不能为空'
                })

            # 创建新模板/APL
            template = SimcContentTemplate.objects.create(
                name=template_name,
                template_type=template_type,
                source=source,
                spec=template_spec,
                class_name=class_name,
                content=template_content,
                is_active=False,  # 新创建的模板默认为禁用状态
                is_selectable=True if is_selectable is None else bool(is_selectable),
                owner_user_id=request.user.id,
            )

            logger.info(f"SimC模板/APL已创建: ID {template.id}")

            return JsonResponse({
                'success': True,
                'message': '模板创建成功',
                'template_id': template.id
            })

        except Exception as e:
            logger.error(f"创建SimC模板失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '创建SimC模板失败'
            })

    def delete(self, request):
        """删除SimC模板"""
        try:
            template_id = request.GET.get('id')

            if not template_id:
                return JsonResponse({
                    'success': False,
                    'error': '模板ID不能为空'
                }, status=400)

            try:
                template, error_response = self._get_writable_template(request, template_id)
                if error_response:
                    return error_response
                if self._is_protected(template):
                    return self._protected_response()

                template.delete()
                logger.info(f"SimC模板/APL已删除: ID {template_id}")

                return JsonResponse({
                    'success': True,
                    'message': '模板删除成功'
                })
            except SimcContentTemplate.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': '模板不存在'
                }, status=404)

        except Exception as e:
            logger.error(f"删除SimC模板失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '删除SimC模板失败'
            })


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
        return (
            template.source == SimcContentTemplate.SOURCE_SIMC_UPSTREAM
            or template.template_type == SimcContentTemplate.TYPE_DEFAULT_PLAYER
        )

    @classmethod
    def _template_is_writable(cls, request, template):
        if cls._template_is_protected(template):
            return False
        if template.owner_user_id == request.user.id:
            return True
        return template.owner_user_id is None and (request.user.is_staff or request.user.is_superuser)

    @classmethod
    def _get_writable_template(cls, request, object_id):
        template = SimcContentTemplate.objects.filter(id=object_id).first()
        if not template:
            return None, JsonResponse({'success': False, 'error': '模板不存在'}, status=404)
        if template.owner_user_id is not None and template.owner_user_id != request.user.id:
            return None, JsonResponse({'success': False, 'error': '模板不存在'}, status=404)
        if cls._template_is_protected(template):
            return None, JsonResponse({'success': False, 'error': '受保护模板为只读资源'}, status=403)
        if cls._template_is_writable(request, template):
            return template, None
        return None, JsonResponse({'success': False, 'error': '系统模板仅管理员可修改'}, status=403)

    @staticmethod
    def _validate_template_content(template_type, content):
        if template_type == SimcContentTemplate.TYPE_BASE_TEMPLATE:
            return SimcTemplateAPIView._validate_base_template(content)
        return None

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
    def _task_status_label(status):
        """返回中文状态标签"""
        labels = {0: '待运行', 1: '运行中', 2: '成功', 3: '失败', 4: '运行中'}
        return labels.get(status, '未知')

    @staticmethod
    def _task_progress(task):
        """返回任务可信进度；运行中仅采用 Worker 已持久化的 progress。"""
        status = task.current_status
        if status == 0:  # pending
            return 0
        if status in (2, 3):  # success or failed
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
    def _task_row(task):
        summary = {}
        try:
            parsed = json.loads(task.result_summary or '{}')
            summary = SimcWorkbenchAPIView._safe_summary(parsed) if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            pass
        return {
            'id': task.id, 'name': task.name, 'status': task.current_status,
            'status_label': SimcWorkbenchAPIView._task_status_label(task.current_status),
            'progress': SimcWorkbenchAPIView._task_progress(task),
            'task_type': task.task_type, 'batch_id': task.batch_id,
            'candidate_label': task.candidate_label, 'result_summary': summary,
            'has_report': bool(task.result_file),
            'report_preview_url': f'/api/simc-workbench/tasks/{task.id}/report-preview/' if task.result_file else '',
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
            row['preview_url'] = f'/api/simc-workbench/artifacts/{artifact.id}/preview/'
        return row

    def get(self, request, resource, object_id=None):
        if resource == 'tasks':
            qs = SimcTask.objects.filter(user_id=request.user.id).order_by('-modified_time')
            if object_id:
                task = qs.filter(id=object_id).first()
                if not task:
                    return JsonResponse({'success': False, 'error': '任务不存在'}, status=404)
                row = self._task_row(task)
                row['artifacts'] = [
                    self._artifact_row(item)
                    for item in task.artifacts.all().order_by('-created_at')
                ]
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

        if resource == 'batches':
            member_filter = models.Q(simctask__user_id=request.user.id, simctask__is_active=True)
            qs = SimcTaskBatch.objects.filter(user_id=request.user.id).annotate(
                task_total=models.Count('simctask', filter=member_filter),
                task_pending=models.Count('simctask', filter=member_filter & models.Q(simctask__current_status=0)),
                task_running=models.Count('simctask', filter=member_filter & models.Q(simctask__current_status__in=(1, 4))),
                task_succeeded=models.Count('simctask', filter=member_filter & models.Q(simctask__current_status=2)),
                task_failed=models.Count('simctask', filter=member_filter & models.Q(simctask__current_status=3)),
                task_with_result=models.Count(
                    'simctask',
                    filter=member_filter & models.Q(simctask__current_status=2, simctask__result_file__isnull=False)
                           & ~models.Q(simctask__result_file=''),
                ),
            ).order_by('-created_at')
            if object_id:
                batch = qs.filter(id=object_id).first()
                if not batch:
                    return JsonResponse({'success': False, 'error': '批次不存在'}, status=404)

                total = batch.task_total
                pending = batch.task_pending
                running = batch.task_running
                succeeded = batch.task_succeeded
                failed = batch.task_failed
                percent = int(((succeeded + failed) / total * 100)) if total > 0 else 0

                report_url = ''
                if total > 0 and failed == 0 and succeeded == total and batch.task_with_result == total:
                    report_url = f'/simc-compare/?batch_id={batch.id}'

                return JsonResponse({'success': True, 'data': {
                    'id': batch.id, 'name': batch.name, 'batch_type': batch.batch_type,
                    'status': batch.status, 'is_active': batch.is_active,
                    'total': total, 'pending': pending, 'running': running,
                    'succeeded': succeeded, 'failed': failed, 'percent': percent,
                    'report_url': report_url,
                    'created_at': _fmt_dt(batch.created_at), 'updated_at': _fmt_dt(batch.updated_at),
                    'tasks': [{
                        'id': task.id,
                        'name': task.name,
                        'status': task.current_status,
                        'status_label': self._task_status_label(task.current_status),
                        'task_type': task.task_type,
                        'updated_at': _fmt_dt(task.modified_time),
                        'can_view': True,
                        'has_report': bool(task.result_file),
                    } for task in SimcTask.objects.filter(
                        batch_id=batch.id, user_id=request.user.id, is_active=True,
                    ).order_by('id')],
                }})

            # 分页参数白名单校验
            try:
                page = int(request.GET.get('page', 1))
                page_size = int(request.GET.get('page_size', 20))
            except (ValueError, TypeError):
                return JsonResponse({'success': False, 'error': '分页参数必须为整数'}, status=400)

            page = max(1, page)
            page_size = max(1, min(50, page_size))

            total = qs.count()
            total_pages = (total + page_size - 1) // page_size
            offset = (page - 1) * page_size

            batches = qs[offset:offset + page_size]
            rows = []
            for row in batches:
                task_total = row.task_total
                task_pending = row.task_pending
                task_running = row.task_running
                task_succeeded = row.task_succeeded
                task_failed = row.task_failed
                task_percent = int(((task_succeeded + task_failed) / task_total * 100)) if task_total > 0 else 0

                report_url = ''
                if (task_total > 0 and task_failed == 0 and task_succeeded == task_total
                        and row.task_with_result == task_total):
                    report_url = f'/simc-compare/?batch_id={row.id}'

                rows.append({
                    'id': row.id, 'name': row.name, 'batch_type': row.batch_type,
                    'status': row.status, 'is_active': row.is_active,
                    'total': task_total, 'pending': task_pending, 'running': task_running,
                    'succeeded': task_succeeded, 'failed': task_failed, 'percent': task_percent,
                    'report_url': report_url,
                    'created_at': _fmt_dt(row.created_at), 'updated_at': _fmt_dt(row.updated_at),
                })

            return JsonResponse({
                'success': True,
                'data': rows,
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

        if resource == 'templates':
            qs = SimcContentTemplate.objects.filter(models.Q(owner_user_id=request.user.id) | models.Q(owner_user_id__isnull=True)).order_by('template_type', 'spec', 'name')
            if object_id:
                qs = qs.filter(id=object_id)
            rows = [{
                'id': row.id, 'name': row.name, 'template_type': row.template_type,
                'type_label': row.get_template_type_display(), 'source': row.source, 'spec': row.spec,
                'class_name': row.class_name, 'content': row.content, 'is_active': row.is_active,
                'is_selectable': row.is_selectable, 'is_system': row.owner_user_id is None,
                'read_only': not self._template_is_writable(request, row),
            } for row in qs]
            if object_id:
                return JsonResponse(
                    {'success': True, 'data': rows[0], 'can_write': not rows[0]['read_only']}
                    if rows else {'success': False, 'error': '模板不存在'},
                    status=200 if rows else 404,
                )
            return JsonResponse({'success': True, 'data': rows, 'can_write': True})

        if resource in ('secondary-rules', 'mastery-rules'):
            model = SimcSecondaryStatRule if resource == 'secondary-rules' else SimcMasteryCoefficient
            fields = ('id', 'class_name', 'crit_per_percent', 'haste_per_percent', 'mastery_per_percent', 'versatility_per_percent') if resource == 'secondary-rules' else ('id', 'spec', 'mastery_coefficient')
            if object_id:
                row = model.objects.filter(id=object_id).values(*fields).first()
                if not row:
                    return JsonResponse({'success': False, 'error': '规则不存在'}, status=404)
                return JsonResponse({'success': True, 'data': row})
            return JsonResponse({'success': True, 'data': list(model.objects.order_by(fields[1]).values(*fields)), 'can_write': request.user.is_staff})

        if resource == 'apl-keywords':
            qs = SimcAplKeywordPair.objects.order_by('apl_keyword')
            fields = ('id', 'apl_keyword', 'cn_keyword', 'description', 'is_active')
            if object_id:
                row = qs.filter(id=object_id).values(*fields).first()
                if not row:
                    return JsonResponse({'success': False, 'error': 'APL 关键字不存在'}, status=404)
                return JsonResponse({'success': True, 'data': row, 'can_write': request.user.is_staff})
            return JsonResponse({'success': True, 'data': list(qs.values(*fields)), 'can_write': request.user.is_staff})

        if resource == 'apl-storage':
            qs = UserAplStorage.objects.filter(user_id=request.user.id).order_by('-id')
            fields = ('id', 'title', 'apl_code', 'is_active')
            if object_id:
                row = qs.filter(id=object_id).values(*fields).first()
                if not row:
                    return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
                return JsonResponse({'success': True, 'data': row})
            return JsonResponse({'success': True, 'data': list(qs.values(*fields))})

        if resource == 'backends':
            return SimcBackendBinaryAPIView().get(request)

        return JsonResponse({'success': False, 'error': '未知工作台资源'}, status=404)

    def post(self, request, resource, object_id=None):
        try:
            data = self._json_body(request)
        except ValueError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=400)
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
            elif action == 'rerun' and task.current_status in (2, 3):
                task = SimcTaskAPIView.create_rerun(task)
                object_id = task.id
            else:
                return JsonResponse({'success': False, 'error': '当前状态不允许该操作'}, status=409)
            SimcMonitor(None, None).sync_batch_lifecycle(task.batch_id)
            return JsonResponse({'success': True, 'data': {'id': object_id}})
        if resource == 'batches' and object_id and action in ('archive', 'restore'):
            batch = SimcTaskBatch.objects.filter(id=object_id, user_id=request.user.id).first()
            if not batch:
                return JsonResponse({'success': False, 'error': '批次不存在'}, status=404)
            if batch.status in (1, 4):
                return JsonResponse({'success': False, 'error': '运行中批次不能归档或恢复'}, status=409)
            batch.is_active = action == 'restore'
            batch.save(update_fields=['is_active', 'updated_at'])
            batch.simctask_set.exclude(current_status__in=(1, 4)).update(is_active=action == 'restore')
            SimcMonitor(None, None).sync_batch_lifecycle(batch.id)
            return JsonResponse({'success': True})
        if resource == 'profiles' and object_id and action in ('archive', 'restore'):
            profile = SimcProfile.objects.filter(id=object_id, user_id=request.user.id).first()
            if not profile:
                return JsonResponse({'success': False, 'error': '配置不存在'}, status=404)
            profile.is_active = action == 'restore'
            profile.save(update_fields=['is_active'])
            return JsonResponse({'success': True})
        if resource == 'apl-storage' and object_id and action in ('archive', 'restore'):
            apl = UserAplStorage.objects.filter(id=object_id, user_id=request.user.id).first()
            if not apl:
                return JsonResponse({'success': False, 'error': 'APL 不存在'}, status=404)
            apl.is_active = action == 'restore'
            apl.save(update_fields=['is_active'])
            return JsonResponse({'success': True})
        if resource == 'templates':
            if object_id and action in ('archive', 'restore'):
                tpl, error_response = self._get_writable_template(request, object_id)
                if error_response:
                    return error_response
                tpl.is_active = action == 'restore'
                tpl.save(update_fields=['is_active', 'updated_at'])
                return JsonResponse({'success': True})
            if not object_id:
                try:
                    name = str(data.get('name') or '').strip()
                    template_type = str(data.get('template_type') or '').strip()
                    spec = str(data.get('spec') or 'default').strip()
                    content = str(data.get('content') or '').strip()
                    if not template_type or template_type not in dict(SimcContentTemplate.TEMPLATE_TYPE_CHOICES):
                        return JsonResponse({'success': False, 'error': '模板类型无效'}, status=400)
                    if template_type == SimcContentTemplate.TYPE_DEFAULT_PLAYER:
                        return JsonResponse({'success': False, 'error': '默认玩家模板为只读资源'}, status=403)
                    if not content:
                        return JsonResponse({'success': False, 'error': '模板内容不能为空'}, status=400)
                    validation_error = self._validate_template_content(template_type, content)
                    if validation_error:
                        return JsonResponse({'success': False, 'error': validation_error}, status=400)
                    owner_user_id = data.get('owner_user_id') if (request.user.is_staff or request.user.is_superuser) else request.user.id
                    if owner_user_id is not None:
                        try:
                            owner_user_id = int(owner_user_id)
                        except (TypeError, ValueError):
                            return JsonResponse({'success': False, 'error': 'owner_user_id 必须是整数'}, status=400)
                        if owner_user_id != request.user.id:
                            return JsonResponse({'success': False, 'error': '不能为其他用户创建私有模板'}, status=403)
                    tpl = SimcContentTemplate(
                        name=name,
                        template_type=template_type,
                        spec=spec,
                        content=content,
                        owner_user_id=owner_user_id,
                        source=SimcContentTemplate.SOURCE_USER,
                        class_name=str(data.get('class_name') or '').strip(),
                        is_active=True,
                    )
                    tpl.save()
                    return JsonResponse({'success': True, 'data': {'id': tpl.id}})
                except Exception as e:
                    if 'active_unique_key' in str(e) or 'UNIQUE' in str(e):
                        return JsonResponse({'success': False, 'error': '相同 owner/spec/type 的活跃模板已存在'}, status=409)
                    logger.error(f"创建模板失败: {str(e)}")
                    return JsonResponse({'success': False, 'error': '创建模板失败'}, status=500)
        if resource == 'apl-keywords':
            if not request.user.is_staff:
                return JsonResponse({'success': False, 'error': '仅管理员可修改关键词'}, status=403)
            if object_id and action in ('archive', 'restore'):
                kw = SimcAplKeywordPair.objects.filter(id=object_id).first()
                if not kw:
                    return JsonResponse({'success': False, 'error': '关键词不存在'}, status=404)
                kw.is_active = action == 'restore'
                kw.save(update_fields=['is_active'])
                return JsonResponse({'success': True})
            if not object_id:
                try:
                    apl_keyword = str(data.get('apl_keyword') or '').strip()
                    cn_keyword = str(data.get('cn_keyword') or '').strip()
                    if not apl_keyword or not cn_keyword:
                        return JsonResponse({'success': False, 'error': 'apl_keyword 和 cn_keyword 不能为空'}, status=400)
                    if SimcAplKeywordPair.objects.filter(apl_keyword=apl_keyword, is_active=True).exists():
                        return JsonResponse({'success': False, 'error': f'活跃关键词 {apl_keyword} 已存在'}, status=409)
                    kw = SimcAplKeywordPair.objects.create(
                        apl_keyword=apl_keyword,
                        cn_keyword=cn_keyword,
                        description=str(data.get('description') or '').strip(),
                        is_active=True,
                    )
                    return JsonResponse({'success': True, 'data': {'id': kw.id}})
                except Exception as e:
                    logger.error(f"创建关键词失败: {str(e)}")
                    return JsonResponse({'success': False, 'error': '创建关键词失败'}, status=500)
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
        if resource == 'templates':
            if not object_id:
                return JsonResponse({'success': False, 'error': '缺少模板ID'}, status=400)
            tpl, error_response = self._get_writable_template(request, object_id)
            if error_response:
                return error_response
            try:
                target_type = tpl.template_type
                if 'template_type' in data:
                    target_type = str(data.get('template_type') or '').strip()
                    if target_type not in dict(SimcContentTemplate.TEMPLATE_TYPE_CHOICES):
                        return JsonResponse({'success': False, 'error': '模板类型无效'}, status=400)
                    if target_type == SimcContentTemplate.TYPE_DEFAULT_PLAYER:
                        return JsonResponse({'success': False, 'error': '默认玩家模板为只读资源'}, status=403)
                if 'source' in data and data.get('source') != tpl.source:
                    return JsonResponse({'success': False, 'error': '模板来源不可修改'}, status=400)
                target_content = str(data['content'] or '').strip() if 'content' in data else tpl.content
                if not target_content:
                    return JsonResponse({'success': False, 'error': '模板内容不能为空'}, status=400)
                validation_error = self._validate_template_content(target_type, target_content)
                if validation_error:
                    return JsonResponse({'success': False, 'error': validation_error}, status=400)
                if 'name' in data:
                    tpl.name = str(data['name'] or '').strip()
                if 'content' in data:
                    tpl.content = target_content
                if 'spec' in data:
                    tpl.spec = str(data['spec'] or 'default').strip()
                if 'class_name' in data:
                    tpl.class_name = str(data['class_name'] or '').strip()
                tpl.template_type = target_type
                tpl.save()
                return JsonResponse({'success': True})
            except Exception as e:
                if 'active_unique_key' in str(e) or 'UNIQUE' in str(e):
                    return JsonResponse({'success': False, 'error': '修改后的模板与已有活跃模板冲突'}, status=409)
                logger.error(f"更新模板失败: {str(e)}")
                return JsonResponse({'success': False, 'error': '更新模板失败'}, status=500)
        if resource == 'apl-keywords':
            if not request.user.is_staff:
                return JsonResponse({'success': False, 'error': '仅管理员可修改关键词'}, status=403)
            if not object_id:
                return JsonResponse({'success': False, 'error': '缺少关键词ID'}, status=400)
            kw = SimcAplKeywordPair.objects.filter(id=object_id).first()
            if not kw:
                return JsonResponse({'success': False, 'error': '关键词不存在'}, status=404)
            try:
                if 'apl_keyword' in data and str(data.get('apl_keyword') or '').strip() != kw.apl_keyword:
                    return JsonResponse({'success': False, 'error': 'apl_keyword 不可修改'}, status=400)
                if 'cn_keyword' in data:
                    kw.cn_keyword = str(data['cn_keyword'] or '').strip()
                if 'description' in data:
                    kw.description = str(data['description'] or '').strip()
                kw.save()
                return JsonResponse({'success': True})
            except Exception as e:
                logger.error(f"更新关键词失败: {str(e)}")
                return JsonResponse({'success': False, 'error': '更新关键词失败'}, status=500)
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
        if resource == 'templates':
            return JsonResponse({'success': False, 'error': '模板不支持真实删除，请使用停用操作'}, status=400)
        if resource == 'apl-keywords':
            return JsonResponse({'success': False, 'error': '关键词不支持真实删除，请使用停用操作'}, status=400)
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
class SimcTaskReportPreviewAPIView(View):
    """兼容没有 Artifact 记录的旧任务，并隐藏报告文件名与存储路径。"""

    def get(self, request, object_id):
        task = SimcTask.objects.filter(id=object_id, user_id=request.user.id).first()
        if not task or not task.result_file:
            return JsonResponse({'success': False, 'error': '任务报告不存在'}, status=404)
        from botend.services.simc_artifacts import _validated_result
        validated = _validated_result(task, os.path.basename(str(task.result_file)))
        if not validated:
            return JsonResponse({'success': False, 'error': '任务报告不可用'}, status=404)
        response = FileResponse(open(str(validated[0]), 'rb'), content_type='text/html; charset=utf-8')
        response['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"
        return response


@method_decorator(login_required, name='dispatch')
class SimcArtifactPreviewAPIView(View):
    def get(self, request, object_id):
        artifact = SimcTaskArtifact.objects.filter(id=object_id, task__user_id=request.user.id).select_related('task').first()
        artifact_path = str(artifact.file_path or '').replace('\\', '/') if artifact else ''
        if (not artifact or artifact.artifact_type != 'html_report'
                or not artifact_path.startswith('simc_results/')):
            return JsonResponse({'success': False, 'error': '产物不存在'}, status=404)
        from botend.services.simc_artifacts import _validated_result
        validated = _validated_result(artifact.task, os.path.basename(artifact_path))
        if not validated or validated[1] != artifact_path:
            return JsonResponse({'success': False, 'error': '产物文件不可用'}, status=404)
        full_path = str(validated[0])
        content_type = 'text/html; charset=utf-8'
        response = FileResponse(open(full_path, 'rb'), content_type=content_type)
        response['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"
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
        """Read the checked-out and tracked-upstream commits without modifying the source checkout."""
        def git_short(ref):
            result = subprocess.run(
                ['git', 'rev-parse', '--short', ref],
                cwd=source_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip() if result.returncode == 0 else ''

        try:
            if not os.path.isdir(source_dir):
                return '', ''
            return git_short('HEAD'), git_short('@{u}')
        except Exception:
            return '', ''

    def _serialize_backend_row(self, row, source_dir, build_dir, binary_path):
        current_hash, upstream_hash = self._get_source_versions(source_dir)
        current_version = current_hash or str(row.current_version or '').strip()
        latest_version = upstream_hash or str(row.latest_version or '').strip()
        return {
            'platform': row.platform,
            'binary_name': os.path.basename(binary_path),
            'available': bool(binary_path and os.path.isfile(binary_path) and os.access(binary_path, os.X_OK)),
            'current_version': current_version,
            'latest_version': latest_version,
            'need_update': bool(latest_version) and (latest_version != current_version),
            'auto_update': row.auto_update,
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
            runtime_platform = self._get_runtime_platform()
            row = SimcBackendBinary.objects.filter(platform=runtime_platform).first()
            source_dir, build_dir, binary_path = self._resolve_local_build_paths()

            if not row:
                return JsonResponse({
                    'success': True,
                    'data': {
                        'platform': runtime_platform,
                        'binary_name': os.path.basename(binary_path),
                        'available': bool(binary_path and os.path.isfile(binary_path) and os.access(binary_path, os.X_OK)),
                        'current_version': '',
                        'latest_version': '',
                        'need_update': False,
                        'auto_update': True,
                        'is_updating': False,
                        'update_progress': 0,
                        'update_status': '未初始化',
                        'has_error': False,
                        'last_checked_at': None,
                        'last_updated_at': None,
                        'can_write': can_write
                    }
                })

            # 以本地编译配置路径为准；历史记录里的旧路径不能继续覆盖运行路径。
            data = self._serialize_backend_row(row, source_dir, build_dir, binary_path)
            data['can_write'] = can_write
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
            if action not in ('set_auto_update', 'check', 'update'):
                return JsonResponse({'success': False, 'error': '不支持的后端操作'}, status=400)

            runtime_platform = self._get_runtime_platform()
            row = SimcBackendBinary.objects.filter(platform=runtime_platform).first()
            if not row:
                row = SimcBackendBinary(platform=runtime_platform)
                row.simc_path = ''
                row.current_version = ''
                row.latest_version = ''
                row.auto_update = True
                row.last_checked_at = None
                row.last_updated_at = None
                row.update_progress = 0
                row.update_status = '未初始化'
                row.last_error = ''
                row.is_updating = False
                row.save()

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
                        row_inner = SimcBackendBinary.objects.filter(platform=runtime_platform).first()
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
