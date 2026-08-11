#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: dashboard.py
@time: 2024/05/15
@desc: Dashboard View Implementation
'''

from django.views import View
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Count, Q
from django.db import transaction
from django.db.utils import OperationalError, ProgrammingError
from django.core.exceptions import ValidationError, FieldDoesNotExist
from django.apps import apps
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from django.utils import timezone

import json
import traceback
import datetime
import os
import re
from django.conf import settings

from utils.log import logger
from botend.models import (MonitorTask, TargetAuth, MonitorWebhook, WechatAccountTask,
                          WechatArticle, VulnMonitorTask, VulnData, RssMonitorTask,
                          RssArticle, WowArticle, SimcTask, SimcProfile, SimcSecondaryStatRule, WclAnalysisTask, SimcApl,
                          SimcBenchmarkPanel, SimcBenchmarkExecution)

from botend.services.simc_attribute_results import parse_attribute_result_filename
from botend.dashboard.permissions import (
    DashboardPermissionRequiredMixin,
    SECTION_PERMISSION_CODES,
    effective_dashboard_permissions,
    permission_catalog,
)


def _fmt_dt(dt):
    if not dt:
        return ''
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_default_timezone())
    return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M:%S')


# 模型描述映射
MODEL_DESCRIPTIONS = {
    'MonitorTask': '监控任务',
    'TargetAuth': '目标认证信息',
    'MonitorWebhook': '监控钩子',
    'WechatAccountTask': '微信公众号任务',
    'WechatArticle': '微信文章',
    'VulnMonitorTask': '漏洞监控任务',
    'VulnData': '漏洞数据',
    'RssMonitorTask': 'RSS监控任务',
    'RssArticle': 'RSS文章',
    'WowArticle': '魔兽文章',

    'SimcTask': 'SimC任务管理',
    'SimcProfile': 'SimC配置管理',
    'SimcSecondaryStatRule': '绿字转换比例（按职业）',
    'SimcMasteryCoefficient': '精通系数（按专精）',
    'PortalEvent': '活动信息',
    'PortalToolLink': '工具链接',
    'PortalMplusRun': '大秘境记录',
    'PortalPeakSpecRankRow': '巅峰榜（专精前3）',
    'VideoMonitorTarget': '视频监控目标',
    'PortalVideo': '视频信息',
    'GeWechatAuth': '微信登录信息',
    'GeWechatRoomList': '微信群列表',
    'GeWechatTask': '微信任务',
    'SimcApl': 'APL管理',
    'SimcContentTemplate': 'SimC模板',
    'SimcBackendBinary': 'SimC后端软件',
    'WclAnalysisTask': 'WCL分析任务',
    'SystemAlert': '系统报警',
    'WowWagoMonitorState': 'Wago监控状态',
    'WowSkillDiffReport': '职业技能变更报告',
    'WowHotfixReport': '热修全量报告',
    'WowDailyReport': '魔兽日报',
    'WowSpellSnapshot': '法术快照',
    'WowSpellEffectSnapshot': '法术效果快照',
    'WowSpellSnapshotState': '法术快照状态',
    'WowSpecSpellMapSnapshot': '专精法术映射快照',
    'PortalMplusSeasonCutoff': '大秘境分数线',
    'PortalMythicstatsDpsRow': 'DPS统计数据',
    'SeasonMeta': '赛季元数据',
    'PlayerSpecTopPlayer': '专精人物榜',
    'SpecDungeonRanking': 'M+副本排名数据',
    'SpecRaidRanking': '团本排名数据',
    'WowWagoBuildEvent': 'Wago版本事件',
    'WowWagoHotfixEvent': 'Wago热修事件',
    'SimcResourceVersion': 'SimC资源版本',
    'SimcAplSymbol': 'SimC APL字段',
    'SimcAplSymbolScope': 'SimC APL字段归属',
    'SimcTaskArtifact': 'SimC任务产物',
    'SimulationRun': 'SimC执行记录',
    'SimcAgent': 'SimC执行代理',
    'SimcAgentMaintenanceTask': 'SimC代理维护任务',
    'SimcAgentEnrollmentCode': 'SimC代理注册码',
    'SimcBenchmarkPanel': 'SimC基准面板',
    'SimcBenchmarkSpec': 'SimC基准专精',
    'SimcBenchmarkProfile': 'SimC基准玩家配置',
    'SimcBenchmarkScenario': 'SimC基准场景',
    'SimcBenchmarkCandidate': 'SimC基准候选项',
    'SimcBenchmarkExecution': 'SimC基准执行',
    'SimcBenchmarkCase': 'SimC基准用例',
    'SimcBenchmarkResult': 'SimC基准结果',
    'WowTalentVersion': '魔兽天赋版本',
    'WowTalentNodeMetadata': '魔兽天赋节点元数据',
    'WowItemSnapshot': '魔兽物品元数据快照',
    'MythicDungeonDataVersion': '大秘境数据版本',
    'MythicDungeonSelectionGroup': '大秘境选择分组',
    'MythicDungeonSpell': '大秘境法术',
    'MythicDungeon': '大秘境副本',
    'MythicDungeonSelectionMembership': '大秘境选择成员关系',
    'MythicDungeonFloor': '大秘境楼层',
    'MythicDungeonEnemy': '大秘境敌人',
    'MythicDungeonAbility': '大秘境技能',
    'MythicDungeonSpawn': '大秘境刷新点',
    'MythicDungeonPoi': '大秘境兴趣点',
    'MythicDungeonRoute': '大秘境路线',
    'MythicDungeonRouteShare': '大秘境路线分享',
    'MythicPlannerConfig': '大秘境规划器配置',

}

COMMON_FIELD_LABELS = {
    'id': 'ID',
    'name': '名称',
    'title': '标题',
    'target': '目标',
    'type': '类型',
    'status': '状态',
    'task_id': '任务ID',
    'task': '任务',
    'task_name': '任务名称',
    'task_type': '任务类型',
    'error_message': '错误信息',
    'extra': '扩展信息',
    'domain': '域名',
    'cookie': 'Cookie',
    'ext': '扩展信息',
    'is_login': '是否登录',
    'is_active': '是否启用',
    'is_zombie': '是否僵尸号',
    'account': '账号',
    'biz': '业务标识',
    'summary': '摘要',
    'url': '链接',
    'link': '链接',
    'url_hash': '链接哈希',
    'target_url_hash': '目标链接哈希',
    'author': '作者',
    'publish_time': '发布时间',
    'created_at': '创建时间',
    'updated_at': '更新时间',
    'create_time': '创建时间',
    'last_scan_time': '上次扫描时间',
    'last_spider_time': '上次抓取时间',
    'last_publish_time': '上次发布时间',
    'wait_time': '等待时间',
    'env_limit': '环境限制',
    'flag': '标记',
    'state': '状态',
    'tag': '标签',
    'description': '描述',
    'source': '来源',
    'category': '分类',
    'reference': '参考',
    'solutions': '解决方案',
    'severity': '严重等级',
    'score': '评分',
    'season': '赛季',
    'region': '区域',
    'class_slug': '职业标识',
    'class_name': '职业',
    'spec_slug': '专精标识',
    'spec_name': '专精',
    'spec_role': '角色定位',
    'rank': '排名',
    'character_name': '角色名',
    'score_color': '分数颜色',
    'rio_region_slug': 'RIO地区',
    'realm_slug': '服务器标识',
    'realm_name': '服务器',
    'cveid': 'CVE编号',
    'sid': '编号',
    'digest': '摘要',
    'cover': '封面',
    'content_html': '正文',
}

@method_decorator(login_required, name='dispatch')
class DashboardView(View):
    """
    处理Dashboard页面请求
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _get_model_map(self):
        return {
            name: entry['model']
            for name, entry in self._get_model_registry().items()
        }

    def _is_database_admin(self):
        user = getattr(self, 'request', None) and self.request.user
        return bool(user and user.is_authenticated and user.is_superuser)

    MODEL_SENSITIVE_FIELDS = {
        'GeWechatAuth': {'uuid', 'qrImgBase64'},
    }

    @classmethod
    def _is_sensitive_field(cls, field, model_name=None):
        name = field.name.lower()
        return (
            field.name in cls.MODEL_SENSITIVE_FIELDS.get(model_name, set())
            or name == 'cookie'
            or 'qrimgbase64' in name
            or bool(re.search(
                r'(^|_)(password|passwd|secret|token|credential|api_key|access_key|private_key|authorization)(_|$)',
                name,
            ))
        )

    @staticmethod
    def _model_description(model):
        return (
            MODEL_DESCRIPTIONS.get(model.__name__)
            or str(getattr(model._meta, 'verbose_name', '') or '').strip()
            or model.__name__
        )

    SIMC_DEDICATED_API_MODELS = {
        'SimcTask', 'SimcTaskArtifact', 'SimcProfile',
        'SimcContentTemplate', 'SimcSecondaryStatRule',
        'SimcMasteryCoefficient',
        'SimcApl', 'SimcBackendBinary',
        'MythicDungeonDataVersion', 'MythicDungeon', 'MythicDungeonFloor',
        'MythicDungeonEnemy', 'MythicDungeonSpell', 'MythicDungeonAbility',
        'MythicDungeonSpawn', 'MythicDungeonPoi',
        'MythicDungeonSelectionGroup', 'MythicDungeonSelectionMembership',
        'MythicDungeonRoute', 'MythicDungeonRouteShare', 'MythicPlannerConfig',
        'SimcBenchmarkPanel', 'SimcBenchmarkSpec', 'SimcBenchmarkProfile',
        'SimcBenchmarkScenario', 'SimcBenchmarkCandidate',
        'SimcBenchmarkExecution', 'SimcBenchmarkCase', 'SimcBenchmarkResult',
        'SimcResourceVersion', 'SimcAplSymbol', 'SimcAplSymbolScope', 'SimulationRun',
        'SimcAgent', 'SimcAgentMaintenanceTask', 'SimcAgentEnrollmentCode',
        'WclAnalysisTask',
        'WowWagoMonitorState', 'WowWagoBuildEvent', 'WowWagoHotfixEvent',
        'WowSpellSnapshot', 'WowSpellEffectSnapshot', 'WowSpellSnapshotState',
        'WowSpecSpellMapSnapshot', 'WowSkillDiffReport', 'WowHotfixReport',
        'WowDailyReport', 'WowTalentVersion', 'WowTalentNodeMetadata',
        'WowItemSnapshot', 'PortalMplusSeasonCutoff', 'PortalMythicstatsDpsRow',
        'PlayerSpecTopPlayer', 'SpecDungeonRanking', 'SpecRaidRanking',
        'GeWechatAuth',
    }

    def _get_model_registry(self):
        """返回 Dashboard 数据库功能的唯一模型注册表及读写能力。"""
        registry = {}
        for model in apps.get_app_config('botend').get_models():
            model_name = model.__name__
            # MODEL_DESCRIPTIONS 同时是显式 allowlist；未来模型默认不暴露。
            if (
                not model._meta.managed
                or model._meta.proxy
                or model_name not in MODEL_DESCRIPTIONS
            ):
                continue
            read_only = model_name in self.SIMC_DEDICATED_API_MODELS
            has_required_sensitive_field = any(
                self._is_sensitive_field(field, model_name)
                and not field.null and not field.blank and not field.has_default()
                for field in model._meta.fields
            )
            description = self._model_description(model)
            registry[model_name] = {
                'model': model,
                'description': description,
                'original_name': model._meta.db_table,
                'display_name': f'{description} - {model._meta.db_table}',
                'can_read': True,
                'can_create': not read_only and not has_required_sensitive_field,
                'can_update': not read_only,
                'can_delete': not read_only,
                'read_only_reason': '该模型由专用业务接口维护' if read_only else '',
            }
        return registry

    def _database_model_entry(self, table_name):
        return self._get_model_registry().get(table_name)

    def get_context_data(
        self,
        *,
        title='后台',
        page_name='dashboard',
        include_stats=True,
        include_table_counts=False,
    ):
        """构建可供 Dashboard 主页面和站内子页面复用的统一外壳上下文。"""
        tables_info = []
        registry = self._get_model_registry() if self._is_database_admin() else {}
        total_records = 0
        visible_entries = sorted(
            registry.values(),
            key=lambda entry: (entry['description'], entry['original_name']),
        )
        for entry in visible_entries:
            model = entry['model']
            model_name = model.__name__
            record_count = 0
            if include_table_counts:
                try:
                    record_count = model.objects.count()
                except (OperationalError, ProgrammingError):
                    record_count = 0
            total_records += record_count
            tables_info.append({
                'name': model_name,
                'description': entry['description'],
                'original_name': entry['original_name'],
                'display_name': entry['display_name'],
                'can_create': entry['can_create'],
                'can_update': entry['can_update'],
                'can_delete': entry['can_delete'],
                'read_only_reason': entry['read_only_reason'],
                'count': record_count,
            })
        return {
            'title': title,
            'page_name': page_name,
            'tables_info': tables_info,
            'total_tables': len(visible_entries),
            'total_records': total_records,
            'stats': self.calculate_dashboard_stats() if include_stats else {},
        }

    def get(self, request):
        """
        处理GET请求，渲染仪表盘页面
        """
        try:
            permissions = effective_dashboard_permissions(request.user)
            section = request.GET.get('section', '').strip()
            permission_code = SECTION_PERMISSION_CODES.get(section)
            if section and not permission_code:
                return JsonResponse({'status': 'error', 'message': '未知 Dashboard 页面'}, status=404)
            if permission_code and permission_code not in permissions:
                return JsonResponse({'status': 'error', 'message': '无权访问该 Dashboard 页面'}, status=403)
            catalog = permission_catalog()
            context = self.get_context_data()
            context['dashboard_permissions'] = sorted(permissions)
            context['dashboard_permission_catalog'] = catalog
            context['dashboard_default_section'] = section or next(
                (item['section'] for item in catalog if item['code'] in permissions),
                '',
            )
            return render(request, 'dashboard/index.html', context)
        except Exception as e:
            logger.error(f"Dashboard view error: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"status": "error", "message": str(e)})
    
    def post(self, request):
        """
        处理POST请求，用于接收Dashboard的数据提交和AJAX请求
        """
        try:
            # 存储 request 以便子方法访问
            self.request = request
            # 请求体可能包含 Cookie、Token 等敏感字段，不记录原始 JSON。
            logger.info("Dashboard POST请求: bytes=%s", len(request.body))
            
            # 解析JSON数据
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError as e:
                logger.error(f"JSON解析错误: {str(e)}")
                return JsonResponse({"status": "error", "message": f"JSON解析错误: {str(e)}"})
            
            # 获取操作类型
            action = data.get('action')
            if not action:
                return JsonResponse({"status": "error", "message": "缺少action参数"})

            if (
                action in {'get_table_data', 'create_table_row', 'update_table_row', 'delete_table_row'}
                and not self._is_database_admin()
            ):
                return JsonResponse(
                    {"status": "error", "message": "数据库管理仅限工作人员"},
                    status=403,
                )

            required_permission = {
                'list_log_files': 'system.logs',
                'read_log_file': 'system.logs',
                'force_run_task': 'dashboard.home',
            }.get(action)
            if required_permission and required_permission not in effective_dashboard_permissions(request.user):
                return JsonResponse(
                    {"status": "error", "message": "无权访问该 Dashboard 页面"},
                    status=403,
                )

            # 根据操作类型处理请求
            if action == 'get_table_data':
                return self.get_table_data(data)
            elif action == 'get_wow_article_detail':
                return self.get_wow_article_detail(data)
            elif action == 'update_table_row':
                return self.update_table_row(data)
            elif action == 'delete_table_row':
                return self.delete_table_row(data)
            elif action == 'create_table_row':
                return self.create_table_row(data)
            elif action == 'list_log_files':
                return self.list_log_files(data)
            elif action == 'read_log_file':
                return self.read_log_file(data)
            elif action == 'force_run_task':
                task_id = data.get('task_id')
                if not task_id:
                    return JsonResponse({'success': False, 'error': '缺少 task_id'})
                try:
                    task = MonitorTask.objects.get(id=task_id)
                    task.last_scan_time = datetime.datetime(2000, 1, 1)  # force scheduler to pick it up
                    task.save(update_fields=['last_scan_time'])
                    return JsonResponse({'success': True, 'message': f'任务 {task.name} 已标记重跑，将在下个调度周期执行'})
                except MonitorTask.DoesNotExist:
                    return JsonResponse({'success': False, 'error': '任务不存在'})
            else:
                return JsonResponse({"status": "error", "message": f"未知操作: {action}"})
            
        except Exception as e:
            logger.error(f"Dashboard post error: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"status": "error", "message": str(e)})
    
    def get_table_data(self, data):
        """
        获取指定表的数据
        """
        try:
            # 获取表名
            table_name = data.get('table_name')
            if not table_name:
                return JsonResponse({"status": "error", "message": "缺少table_name参数"})

            # 获取分页参数
            try:
                page = max(1, int(data.get('page', 1)))
                page_size = min(200, max(10, int(data.get('page_size', 50))))
            except (TypeError, ValueError):
                return JsonResponse({"status": "error", "message": "分页参数无效"}, status=400)
            
            # 获取搜索参数
            search_query = data.get('search', '').strip()
            simc_spec_filter = data.get('simc_spec', '').strip()
            simc_fight_style_filter = data.get('simc_fight_style', '').strip()
            wow_source_filter = data.get('wow_source', '').strip()
            wow_category_filter = data.get('wow_category', '').strip()
            
            logger.info(f"获取表数据: {table_name}, page: {page}, page_size: {page_size}, search: {search_query}")
            
            # 从统一注册表取得模型与能力。
            registry_entry = self._database_model_entry(table_name)
            if not registry_entry or not registry_entry['can_read']:
                return JsonResponse({"status": "error", "message": f"未知表名: {table_name}"}, status=404)
            model = registry_entry['model']
            
            # 获取字段名和字段类型信息
            fields = [field.name for field in model._meta.fields]
            field_types = {}
            field_labels = {}
            for field in model._meta.fields:
                field_type = field.__class__.__name__
                sensitive = self._is_sensitive_field(field, table_name)
                field_editable = bool(
                    getattr(field, 'editable', True)
                    and not getattr(field, 'primary_key', False)
                    and not getattr(field, 'auto_now', False)
                    and not getattr(field, 'auto_now_add', False)
                    and not sensitive
                    and registry_entry['can_update']
                )

                # 处理默认值，确保可以JSON序列化
                default_value = getattr(field, 'default', None)
                if default_value is not None:
                    # 如果默认值是函数或其他不可序列化的对象，转换为字符串或None
                    try:
                        json.dumps(default_value)  # 测试是否可以序列化
                    except (TypeError, ValueError):
                        default_value = None  # 不可序列化的默认值设为None
                
                # 处理 choices，统一返回前端可直接渲染的 [{value, label}]
                choices = None
                raw_choices = getattr(field, 'choices', None)
                if raw_choices:
                    choices = []
                    for choice_value, choice_label in raw_choices:
                        if isinstance(choice_label, (list, tuple)):
                            for nested_value, nested_label in choice_label:
                                choices.append({'value': nested_value, 'label': str(nested_label)})
                        else:
                            choices.append({'value': choice_value, 'label': str(choice_label)})
                
                field_types[field.name] = {
                    'type': field_type,
                    'null': field.null,
                    'blank': field.blank,
                    'max_length': getattr(field, 'max_length', None),
                    'default': default_value,
                    'help_text': str(getattr(field, 'help_text', '') or ''),
                    'choices': choices,
                    'primary_key': getattr(field, 'primary_key', False),
                    'editable': field_editable,
                    'read_only': not field_editable,
                    'sensitive': sensitive,
                    'auto_now': getattr(field, 'auto_now', False),
                    'auto_now_add': getattr(field, 'auto_now_add', False),
                }

                verbose_name = str(getattr(field, 'verbose_name', '') or '').strip()
                if (
                    (not verbose_name)
                    or (verbose_name == field.name)
                    or (re.match(r'^[\x00-\x7F]+$', verbose_name) and field.name in COMMON_FIELD_LABELS)
                ):
                    verbose_name = COMMON_FIELD_LABELS.get(field.name, field.name)
                field_labels[field.name] = verbose_name
            
            # 计算分页偏移量
            offset = (page - 1) * page_size
            
            # 创建搜索过滤条件
            def apply_search_filter(queryset, search_fields):
                if search_query:
                    search_conditions = Q()
                    for field in search_fields:
                        search_conditions |= Q(**{f"{field}__icontains": search_query})
                    return queryset.filter(search_conditions)
                return queryset
            
            # 统一模型查询：所有注册模型使用相同的分页、搜索和字段契约。
            try:
                pk_name = model._meta.pk.name
                queryset = model.objects.all().order_by(f'-{pk_name}')
                if table_name == 'WowArticle':
                    if wow_source_filter:
                        queryset = queryset.filter(source=wow_source_filter)
                    if wow_category_filter:
                        queryset = queryset.filter(category=wow_category_filter)
                elif table_name == 'SimcProfile':
                    if simc_spec_filter:
                        queryset = queryset.filter(spec__icontains=simc_spec_filter)

                search_fields = [
                    field.name for field in model._meta.fields
                    if not self._is_sensitive_field(field, table_name)
                    and field.__class__.__name__ in {'CharField', 'TextField', 'EmailField', 'SlugField'}
                ]
                if search_query:
                    search_conditions = Q()
                    for field_name in search_fields:
                        search_conditions |= Q(**{f'{field_name}__icontains': search_query})
                    try:
                        pk_value = model._meta.pk.to_python(search_query)
                    except (TypeError, ValueError, ValidationError):
                        pk_value = None
                    if pk_value is not None:
                        search_conditions |= Q(**{pk_name: pk_value})
                    queryset = queryset.filter(search_conditions)

                total_count = queryset.count()
                total_pages = max(1, (total_count + page_size - 1) // page_size)
                page = min(page, total_pages)
                offset = (page - 1) * page_size
                items = list(queryset.values(*fields)[offset:offset + page_size])
            except Exception as e:
                logger.error(f"获取表数据错误: {str(e)}\n{traceback.format_exc()}")
                return JsonResponse({"status": "error", "message": f"获取表数据错误: {str(e)}"})
            
            # 处理日期时间字段，转换为字符串
            for item in items:
                for key, value in item.items():
                    if isinstance(value, datetime.datetime):
                        dt = value
                        if timezone.is_naive(dt):
                            dt = timezone.make_aware(dt, timezone.get_default_timezone())
                        item[key] = timezone.localtime(dt).strftime('%Y-%m-%d %H:%M:%S')
                    elif isinstance(value, (datetime.date, datetime.time)):
                        item[key] = value.strftime('%Y-%m-%d %H:%M:%S')
                    elif hasattr(value, 'strftime'):
                        item[key] = value.strftime('%Y-%m-%d %H:%M:%S')
                if item.get('author') == 'LMonitor':
                    item['author'] = ''
                for field in model._meta.fields:
                    if self._is_sensitive_field(field, table_name) and field.name in item:
                        item[field.name] = '••••••' if item[field.name] not in (None, '') else ''

            # 计算分页信息
            total_pages = max(1, (total_count + page_size - 1) // page_size)
            
            # 返回数据
            table_description = self._model_description(model)
            original_name = model._meta.db_table
            resp = {
                "status": "success",
                "data": items,
                "fields": fields,
                "field_types": field_types,
                "field_labels": field_labels,
                "search_fields": search_fields,
                "table_description": table_description,
                "table_original_name": original_name,
                "table_display_name": registry_entry['display_name'],
                "capabilities": {
                    "can_create": registry_entry['can_create'],
                    "can_update": registry_entry['can_update'],
                    "can_delete": registry_entry['can_delete'],
                    "read_only_reason": registry_entry['read_only_reason'],
                },
                "total_count": total_count,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages
            }
            if table_name == 'WowArticle':
                sources = list(
                    model.objects.exclude(source__isnull=True).exclude(source='').values_list('source', flat=True).distinct()
                )
                categories = list(
                    model.objects.exclude(category__isnull=True).exclude(category='').values_list('category', flat=True).distinct()
                )
                resp["wow_filter_options"] = {
                    "sources": sorted(sources),
                    "categories": sorted(categories),
                }
            return JsonResponse(resp)
            
        except Exception as e:
            logger.error(f"获取表数据异常: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"status": "error", "message": f"获取表数据异常: {str(e)}"})
    

    def get_wow_article_detail(self, data):
        """返回后台新闻详情阅读所需字段，避免列表接口携带大块正文。"""
        try:
            article_id = data.get('id')
            if not article_id:
                return JsonResponse({"status": "error", "message": "缺少文章ID"})
            article = WowArticle.objects.filter(id=article_id).values(
                'id', 'title', 'title_cn', 'url', 'author', 'publish_time', 'description',
                'content', 'content_cn', 'content_blocks', 'content_blocks_cn',
                'source', 'category', 'reply_count'
            ).first()
            if not article:
                return JsonResponse({"status": "error", "message": "文章不存在"})
            for key, value in list(article.items()):
                if isinstance(value, datetime.datetime):
                    dt = value
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt, timezone.get_default_timezone())
                    article[key] = timezone.localtime(dt).strftime('%Y-%m-%d %H:%M:%S')
                elif isinstance(value, (datetime.date, datetime.time)):
                    article[key] = value.strftime('%Y-%m-%d %H:%M:%S')
            if article.get('author') == 'LMonitor':
                article['author'] = ''
            return JsonResponse({"status": "success", "data": article})
        except Exception as e:
            logger.error(f"获取魔兽文章详情异常: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"status": "error", "message": f"获取文章详情异常: {str(e)}"})

    @transaction.atomic
    def update_table_row(self, data):
        """使用模型字段白名单更新单行；只读、未知或敏感字段整次拒绝。"""
        table_name = data.get('table_name')
        row_id = data.get('row_id')
        update_data = data.get('update_data')
        if not table_name or row_id in (None, '') or not isinstance(update_data, dict) or not update_data:
            return JsonResponse({"status": "error", "message": "缺少必要参数"}, status=400)

        registry_entry = self._database_model_entry(table_name)
        if not registry_entry:
            return JsonResponse({"status": "error", "message": f"未找到表: {table_name}"}, status=404)
        if not registry_entry['can_update']:
            return JsonResponse({"status": "error", "message": registry_entry['read_only_reason'] or "该表不允许编辑"}, status=403)
        model = registry_entry['model']

        fields_to_update = []
        converted_values = {}
        for field_name, raw_value in update_data.items():
            try:
                field = model._meta.get_field(field_name)
            except FieldDoesNotExist:
                return JsonResponse({"status": "error", "message": f"未知字段: {field_name}"}, status=400)
            if (
                field.primary_key or not field.editable
                or getattr(field, 'auto_now', False) or getattr(field, 'auto_now_add', False)
                or self._is_sensitive_field(field, table_name)
            ):
                return JsonResponse({"status": "error", "message": f"字段 {field_name} 不允许编辑"}, status=400)
            try:
                value = raw_value
                if value == '' and field.null:
                    value = None
                elif field.__class__.__name__ == 'JSONField' and isinstance(value, str):
                    value = json.loads(value) if value.strip() else (None if field.null else field.get_default())
                elif field.__class__.__name__ == 'BooleanField' and isinstance(value, str):
                    normalized = value.strip().lower()
                    if normalized not in {'true', 'false', '1', '0', 'yes', 'no', 'on', 'off'}:
                        raise ValidationError('必须是布尔值')
                    value = normalized in {'true', '1', 'yes', 'on'}
                else:
                    value = field.to_python(value)
                field.validate(value, None)
                field.run_validators(value)
            except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
                return JsonResponse({"status": "error", "message": f"字段 {field_name} 的值无效: {exc}"}, status=400)
            converted_values[field.attname if field.is_relation else field.name] = value
            fields_to_update.append(field.name)

        try:
            instance = model.objects.select_for_update().get(pk=row_id)
        except model.DoesNotExist:
            return JsonResponse({"status": "error", "message": f"未找到ID为{row_id}的记录"}, status=404)

        for attribute, value in converted_values.items():
            setattr(instance, attribute, value)
        try:
            instance.validate_unique()
            instance.validate_constraints()
            instance.save(update_fields=fields_to_update)
        except ValidationError as exc:
            return JsonResponse({"status": "error", "message": f"数据校验失败: {exc}"}, status=400)
        except Exception as exc:
            logger.error(f"更新表数据异常: {str(exc)}\n{traceback.format_exc()}")
            return JsonResponse({"status": "error", "message": f"更新失败: {str(exc)}"}, status=400)

        logger.info("更新表数据成功: %s, row_id=%s, fields=%s", table_name, row_id, fields_to_update)
        return JsonResponse({"status": "success", "message": "更新成功"})
    
    @transaction.atomic
    def delete_table_row(self, data):
        """
        删除表格行数据
        """
        try:
            # 获取参数
            table_name = data.get('table_name')
            row_id = data.get('row_id')
            
            if not table_name or not row_id:
                return JsonResponse({"status": "error", "message": "缺少必要参数"})
            
            logger.info(f"删除表数据: {table_name}, row_id: {row_id}")

            registry_entry = self._database_model_entry(table_name)
            if not registry_entry:
                return JsonResponse({"status": "error", "message": f"未找到表: {table_name}"}, status=404)
            if not registry_entry['can_delete']:
                return JsonResponse({"status": "error", "message": registry_entry['read_only_reason'] or "该表不允许删除"}, status=403)
            model = registry_entry['model']
            
            # 查找要删除的记录
            try:
                pk_name = model._meta.pk.name
                instance = model.objects.get(**{pk_name: row_id})
            except model.DoesNotExist:
                return JsonResponse({"status": "error", "message": f"未找到ID为{row_id}的记录"})
            
            # 删除记录
            instance.delete()
            
            return JsonResponse({"status": "success", "message": "删除成功"})
            
        except Exception as e:
            logger.error(f"删除表数据异常: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"status": "error", "message": f"删除失败: {str(e)}"})
    
    def calculate_dashboard_stats(self):
        """
        计算仪表盘统计数据
        """
        try:
            from django.utils import timezone
            from datetime import timedelta
            
            now = timezone.now()
            today = now.date()
            week_ago = now - timedelta(days=7)
            month_ago = now - timedelta(days=30)
            
            # 监控任务统计
            active_monitor_tasks = MonitorTask.objects.filter(is_active=True).count()
            total_monitor_tasks = MonitorTask.objects.count()
            
            # 漏洞数据统计
            total_vulns = VulnData.objects.count()
            high_severity_vulns = VulnData.objects.filter(severity__gte=7).count()
            recent_vulns = VulnData.objects.filter(publish_time__gte=week_ago).count()
            
            # 微信文章统计
            total_wechat_articles = WechatArticle.objects.count()
            recent_wechat_articles = WechatArticle.objects.filter(publish_time__gte=week_ago).count()
            active_wechat_accounts = WechatAccountTask.objects.filter(is_zombie=0).count()
            
            # RSS文章统计
            total_rss_articles = RssArticle.objects.count()
            recent_rss_articles = RssArticle.objects.filter(publish_time__gte=week_ago).count()
            active_rss_tasks = RssMonitorTask.objects.filter(is_active=True).count()
            

            
            # 系统活跃度统计
            recent_activity_score = (
                recent_vulns * 3 + 
                recent_wechat_articles * 2 + 
                recent_rss_articles * 1
            )
            
            return {
                'monitor_tasks': {
                    'active': active_monitor_tasks,
                    'total': total_monitor_tasks,
                    'percentage': round((active_monitor_tasks / total_monitor_tasks * 100) if total_monitor_tasks > 0 else 0, 1)
                },
                'vulnerabilities': {
                    'total': total_vulns,
                    'high_severity': high_severity_vulns,
                    'recent': recent_vulns,
                    'high_severity_percentage': round((high_severity_vulns / total_vulns * 100) if total_vulns > 0 else 0, 1)
                },
                'wechat': {
                    'total_articles': total_wechat_articles,
                    'recent_articles': recent_wechat_articles,
                    'active_accounts': active_wechat_accounts
                },
                'rss': {
                    'total_articles': total_rss_articles,
                    'recent_articles': recent_rss_articles,
                    'active_tasks': active_rss_tasks
                },

                'activity': {
                    'score': recent_activity_score,
                    'level': 'high' if recent_activity_score > 50 else 'medium' if recent_activity_score > 20 else 'low'
                }
            }
        except Exception as e:
            logger.error(f"计算统计数据失败: {str(e)}")
            return {}
    
    @transaction.atomic
    def create_table_row(self, data):
        """按照统一注册表和字段白名单创建记录。"""
        table_name = data.get('table_name')
        create_data = data.get('create_data')
        if not table_name or not isinstance(create_data, dict) or not create_data:
            return JsonResponse({"status": "error", "message": "缺少必要参数"}, status=400)

        registry_entry = self._database_model_entry(table_name)
        if not registry_entry:
            return JsonResponse({"status": "error", "message": f"未找到表: {table_name}"}, status=404)
        if not registry_entry['can_create']:
            return JsonResponse({"status": "error", "message": registry_entry['read_only_reason'] or "该表不允许通用新增"}, status=403)
        model = registry_entry['model']
        converted_data = {}
        for field_name, raw_value in create_data.items():
            try:
                field = model._meta.get_field(field_name)
            except FieldDoesNotExist:
                return JsonResponse({"status": "error", "message": f"未知字段: {field_name}"}, status=400)
            if (
                field.primary_key or not field.editable
                or getattr(field, 'auto_now', False) or getattr(field, 'auto_now_add', False)
                or self._is_sensitive_field(field, table_name)
            ):
                return JsonResponse({"status": "error", "message": f"字段 {field_name} 不允许新增"}, status=400)
            try:
                value = raw_value
                if value == '' and field.null:
                    value = None
                elif field.__class__.__name__ == 'JSONField' and isinstance(value, str):
                    value = json.loads(value) if value.strip() else (None if field.null else field.get_default())
                elif field.__class__.__name__ == 'BooleanField' and isinstance(value, str):
                    normalized = value.strip().lower()
                    if normalized not in {'true', 'false', '1', '0', 'yes', 'no', 'on', 'off'}:
                        raise ValidationError('必须是布尔值')
                    value = normalized in {'true', '1', 'yes', 'on'}
                else:
                    value = field.to_python(value)
                field.validate(value, None)
                field.run_validators(value)
            except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
                return JsonResponse({"status": "error", "message": f"字段 {field_name} 的值无效: {exc}"}, status=400)
            converted_data[field.attname if field.is_relation else field.name] = value

        if 'user_id' not in converted_data and any(field.attname == 'user_id' for field in model._meta.fields):
            converted_data['user_id'] = self.request.user.id

        try:
            instance = model(**converted_data)
            # 旧模型中存在 null=True 但 blank=False 的字段；未提交时数据库允许 NULL，
            # 不应让表单语义的 blank 校验阻断通用新增。显式提交的值仍完整校验。
            omitted_nullable_fields = [
                field.name
                for field in model._meta.fields
                if (
                    field.null
                    and field.name not in create_data
                    and field.attname not in converted_data
                    and getattr(instance, field.attname) is None
                )
            ]
            instance.full_clean(exclude=omitted_nullable_fields)
            instance.save()
        except ValidationError as exc:
            return JsonResponse({"status": "error", "message": f"数据校验失败: {exc}"}, status=400)
        except Exception as exc:
            logger.error("创建记录失败: %s\n%s", exc, traceback.format_exc())
            return JsonResponse({"status": "error", "message": f"创建记录失败: {exc}"}, status=400)

        logger.info("成功创建记录: %s, id=%s", table_name, instance.pk)
        return JsonResponse({
            "status": "success",
            "message": "记录创建成功",
            "data": {"id": instance.pk},
        })

    def _get_logs_dir(self):
        return os.path.realpath(os.path.join(settings.BASE_DIR, 'logs'))

    def _resolve_log_path(self, filename):
        filename = (filename or '').strip()
        if not filename:
            raise ValueError('缺少 filename 参数')
        if not filename.endswith('.log'):
            raise ValueError('只允许读取 .log 文件')
        if os.path.basename(filename) != filename:
            raise ValueError('文件名不合法')

        logs_dir = self._get_logs_dir()
        file_path = os.path.realpath(os.path.join(logs_dir, filename))
        if not file_path.startswith(logs_dir + os.sep):
            raise ValueError('文件路径不合法')
        if not os.path.exists(file_path):
            raise FileNotFoundError('文件不存在')
        if not os.path.isfile(file_path):
            raise ValueError('不是有效的文件')
        return logs_dir, file_path

    @staticmethod
    def _count_file_lines(file_path):
        count = 0
        last_byte = b''
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                count += chunk.count(b'\n')
                last_byte = chunk[-1:]
        if os.path.getsize(file_path) > 0 and last_byte != b'\n':
            count += 1
        return count

    def list_log_files(self, data):
        """
        列出 logs 目录下的 .log 文件，按文件修改时间倒序返回。
        """
        try:
            logs_dir = self._get_logs_dir()
            if not os.path.isdir(logs_dir):
                return JsonResponse({"status": "success", "data": [], "count": 0})

            log_files = []
            for filename in os.listdir(logs_dir):
                if not filename.endswith('.log'):
                    continue
                try:
                    _, file_path = self._resolve_log_path(filename)
                    stat_info = os.stat(file_path)
                    file_size = stat_info.st_size
                    mtime = stat_info.st_mtime
                    mtime_dt = datetime.datetime.fromtimestamp(mtime)

                    # 行数用于展示，不读取文本内容；超大文件也用二进制块计数，避免一次性载入内存。
                    try:
                        line_count = self._count_file_lines(file_path)
                    except Exception:
                        line_count = -1

                    log_files.append({
                        'filename': filename,
                        'size': file_size,
                        'size_human': self._format_size(file_size),
                        'mtime': mtime,
                        'mtime_human': mtime_dt.strftime('%Y-%m-%d %H:%M:%S'),
                        'line_count': line_count,
                    })
                except Exception as e:
                    logger.warning(f"获取日志文件信息失败 {filename}: {str(e)}")
                    continue

            log_files.sort(key=lambda x: x['mtime'], reverse=True)
            return JsonResponse({"status": "success", "data": log_files, "count": len(log_files)})

        except Exception as e:
            logger.error(f"列出日志文件失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"status": "error", "message": f"列出日志文件失败: {str(e)}"})

    def read_log_file(self, data):
        """
        正序读取指定日志文件内容，支持分页。
        """
        try:
            filename = data.get('filename', '').strip()
            try:
                page = int(data.get('page', 1) or 1)
            except (TypeError, ValueError):
                page = 1
            try:
                page_size = int(data.get('page_size', 300) or 300)
            except (TypeError, ValueError):
                page_size = 300

            page = max(page, 1)
            page_size = max(1, min(page_size, 1000))

            try:
                _, file_path = self._resolve_log_path(filename)
            except FileNotFoundError as e:
                return JsonResponse({"status": "error", "message": str(e)})
            except ValueError as e:
                return JsonResponse({"status": "error", "message": str(e)})

            total_lines = self._count_file_lines(file_path)
            total_pages = max(1, (total_lines + page_size - 1) // page_size)
            if page > total_pages:
                page = total_pages

            start_line = (page - 1) * page_size + 1
            end_line = min(page * page_size, total_lines)
            selected_lines = []

            try:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    for line_no, line in enumerate(f, start=1):
                        if line_no < start_line:
                            continue
                        if line_no > end_line:
                            break
                        selected_lines.append({
                            'line_no': line_no,
                            'text': line.rstrip('\n\r'),
                        })
            except Exception as e:
                logger.error(f"读取日志文件失败 {filename}: {str(e)}\n{traceback.format_exc()}")
                return JsonResponse({"status": "error", "message": f"读取文件失败: {str(e)}"})

            stat_info = os.stat(file_path)
            mtime_dt = datetime.datetime.fromtimestamp(stat_info.st_mtime)

            return JsonResponse({
                "status": "success",
                "data": {
                    "lines": selected_lines,
                    "page": page,
                    "page_size": page_size,
                    "total_lines": total_lines,
                    "total_pages": total_pages,
                    "filename": filename,
                    "size": stat_info.st_size,
                    "size_human": self._format_size(stat_info.st_size),
                    "mtime_human": mtime_dt.strftime('%Y-%m-%d %H:%M:%S'),
                }
            })

        except Exception as e:
            logger.error(f"读取日志文件异常: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"status": "error", "message": f"读取日志文件异常: {str(e)}"})

    @staticmethod
    def _format_size(size_bytes):
        """格式化文件大小"""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.2f} KB"
        if size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.2f} MB"
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


@method_decorator(login_required, name='dispatch')
class SimcWorkbenchDetailPageView(DashboardPermissionRequiredMixin, View):
    """Authenticated HTML shell; safe result details are loaded through the API."""

    model_by_kind = {'tasks': SimcTask}
    dashboard_permission = 'simc.history'

    def get(self, request, kind, object_id):
        model = self.model_by_kind.get(kind)
        if model is None:
            return HttpResponse(status=404)
        obj = get_object_or_404(model, id=object_id)
        return render(request, 'dashboard/simc_detail.html', {
            'detail_kind': kind,
            'detail_id': obj.id,
            'detail_title': obj.name,
        })


@method_decorator(login_required, name='dispatch')
class SimcBenchmarkPanelEditPageView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'simc.benchmarks'

    def get(self, request, panel_id):
        panel = get_object_or_404(SimcBenchmarkPanel, pk=panel_id)
        return render(request, 'dashboard/simc_benchmark_panel_edit.html', {
            'panel_id': panel.pk,
            'panel_name': panel.name,
        })


@method_decorator(login_required, name='dispatch')
class SimcBenchmarkConfigPageView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'simc.benchmarks'

    def get(self, request, panel_id):
        panel = get_object_or_404(SimcBenchmarkPanel, pk=panel_id)
        return render(request, 'dashboard/simc_benchmark_config.html', {
            'panel_id': panel.pk,
            'panel_name': panel.name,
        })


@method_decorator(login_required, name='dispatch')
class SimcBenchmarkExecutionPageView(DashboardPermissionRequiredMixin, View):
    """Execution result shell; private means unlisted in Portal, not restricted here."""

    dashboard_permission = 'simc.benchmarks'

    def get(self, request, execution_id):
        execution = get_object_or_404(SimcBenchmarkExecution, pk=execution_id)
        return render(request, 'dashboard/simc_benchmark_execution.html', {
            'execution_id': execution.pk,
            'panel_name': execution.panel.name,
        })


@method_decorator(login_required, name='dispatch')
class SimcResultView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'simc.history'
    """
    处理SimC自定义结果查看页面请求
    """
    
    def get(self, request):
        """
        渲染SimC结果查看页面
        """
        try:
            return render(request, 'simc_result_view.html')
        except Exception as e:
            logger.error(f"渲染SimC结果页面失败: {str(e)}")
            logger.error(traceback.format_exc())
            return HttpResponse("页面加载失败", status=500)


@method_decorator(login_required, name='dispatch')
class SimcAttributeAnalysisView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'simc.history'
    """
    处理SimC属性模拟分析页面请求
    """
    
    def get(self, request):
        """
        渲染SimC属性模拟分析页面
        """
        try:
            return render(request, 'simc_attribute_analysis.html')
        except Exception as e:
            logger.error(f"渲染SimC属性模拟分析页面失败: {str(e)}")
            logger.error(traceback.format_exc())
            return HttpResponse("页面加载失败", status=500)


@method_decorator(login_required, name='dispatch')
class SimcRegularCompareView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'simc.history'
    """
    处理SimC常规模拟对比页面请求
    """
    
    def get(self, request):
        try:
            return render(request, 'simc_regular_compare.html')
        except Exception as e:
            logger.error(f"渲染SimC常规模拟对比页面失败: {str(e)}")
            logger.error(traceback.format_exc())
            return HttpResponse("页面加载失败", status=500)


@method_decorator(login_required, name='dispatch')
class SimcAttributeAnalysisSSRView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'simc.history'
    """
    属性模拟分析SSR页面：后端渲染对比结果，无需前端JS计算
    """
    def get(self, request):
        try:
            task_id = request.GET.get('task_id')
            if not task_id:
                return HttpResponse("缺少任务ID参数", status=400)
            
            # 组装分析数据（复用API中的解析思路）
            from django.conf import settings
            from botend.models import SimcTask
            import os
            import re
            import requests
            
            try:
                task = SimcTask.objects.get(id=task_id, is_active=True)
            except SimcTask.DoesNotExist:
                return HttpResponse("任务不存在或无权限访问", status=404)
            
            if task.task_type != 2 or not task.result_file:
                return HttpResponse("该任务不是属性模拟或尚无结果文件", status=400)
            
            result_files = [x.strip() for x in task.result_file.split(',') if x.strip()]
            oss_config = getattr(settings, 'OSS_CONFIG', {})
            base_url = oss_config.get('base_url', '')
            
            def read_file_content(result_file):
                # 先OSS
                if base_url:
                    try:
                        resp = requests.get(base_url + result_file, timeout=30)
                        if resp.status_code == 200:
                            return resp.text
                    except requests.RequestException:
                        pass
                # 再本地
                local_file_path = os.path.join(settings.BASE_DIR, 'static', 'simc_results', result_file)
                if os.path.exists(local_file_path):
                    with open(local_file_path, 'r', encoding='utf-8') as f:
                        return f.read()
                return None
            
            def extract_dps(html):
                try:
                    if isinstance(html, bytes):
                        try:
                            html = html.decode('utf-8', errors='replace')
                        except Exception:
                            html = str(html)
                    m = re.search(r':\s*([\d,]+)\s*dps', html, re.IGNORECASE)
                    if m:
                        return int(m.group(1).replace(',', ''))
                    try:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(html, 'html.parser')
                        player = soup.find(class_='player')
                        if player:
                            h2 = player.find('h2')
                            if h2:
                                mm = re.search(r':\s*([\d,]+)\s*dps', h2.get_text(), re.IGNORECASE)
                                if mm:
                                    return int(mm.group(1).replace(',', ''))
                    except ImportError:
                        return None
                except Exception:
                    return None
                return None
            
            def translate_attr_name(name):
                mapping = {
                    'gear_crit': '暴击',
                    'gear_haste': '急速',
                    'gear_mastery': '精通',
                    'gear_versatility': '全能',
                    'crit': '暴击',
                    'haste': '急速',
                    'mastery': '精通',
                    'versatility': '全能',
                }
                return mapping.get(name, name)

            analysis = []
            for rf in result_files:
                parsed = parse_attribute_result_filename(rf)
                if not parsed or parsed['task_id'] != task.id:
                    continue
                attr1_name = parsed['attr1_name']
                attr1_value = parsed['attr1_value']
                attr2_name = parsed['attr2_name']
                attr2_value = parsed['attr2_value']

                content = read_file_content(rf)
                if not content:
                    continue
                dps_val = extract_dps(content)
                if dps_val is None:
                    continue

                analysis.append({
                    'file_name': rf,
                    'attr1_name': translate_attr_name(attr1_name),
                    'attr1_value': attr1_value,
                    'attr2_name': translate_attr_name(attr2_name),
                    'attr2_value': attr2_value,
                    'dps': dps_val
                })
            
            # 排序
            def sort_key(x):
                v = x['attr1_value']
                return (0, v) if isinstance(v, int) else (1, str(v))
            analysis.sort(key=sort_key)
            
            if not analysis:
                return HttpResponse("未能解析到有效的分析数据", status=500)
            
            dps_list = [i['dps'] for i in analysis]
            max_dps = max(dps_list)
            min_dps = min(dps_list)
            avg_dps = sum(dps_list) / len(dps_list)
            above_avg = sum(1 for d in dps_list if d > avg_dps)
            best = next(i for i in analysis if i['dps'] == max_dps)
            worst = next(i for i in analysis if i['dps'] == min_dps)
            improvement_abs = max_dps - min_dps
            improvement_percent = (improvement_abs * 100.0 / min_dps) if min_dps else 0.0

            budget_values = [
                int(item['attr1_value']) + int(item['attr2_value'])
                for item in analysis
                if isinstance(item['attr1_value'], int) and isinstance(item['attr2_value'], int)
            ]
            total_budget = budget_values[0] if budget_values else 0
            budget_is_fixed = bool(budget_values) and len(set(budget_values)) == 1

            near_optimal_configs = []
            for item in analysis:
                delta_from_best = max_dps - item['dps']
                delta_percent = (delta_from_best / max_dps * 100.0) if max_dps > 0 else 0.0
                item['delta_from_best_abs'] = delta_from_best
                item['delta_from_best_percent'] = delta_percent
                if delta_percent <= 0.2:
                    near_optimal_configs.append(item)

            spread_narrow = (improvement_percent <= 0.5) if improvement_percent is not None else False
            
            results_by_dps = sorted(analysis, key=lambda x: x.get('dps', 0), reverse=True)

            context = {
                'task_id': task.id,
                'task_name': task.name,
                'results': analysis,
                'results_by_dps': results_by_dps,
                'stats': {
                    'max_dps': max_dps,
                    'min_dps': min_dps,
                    'avg_dps': avg_dps,
                    'above_avg': above_avg,
                    'count': len(analysis),
                    'best': best,
                    'worst': worst,
                    'improvement_abs': improvement_abs,
                    'improvement_percent': improvement_percent,
                    'total_budget': total_budget,
                    'budget_is_fixed': budget_is_fixed,
                    'near_optimal_count': len(near_optimal_configs),
                    'spread_narrow': spread_narrow,
                }
            }
            
            return render(request, 'simc_attribute_analysis_ssr.html', context)
        except Exception as e:
            logger.error(f"渲染属性模拟SSR页面失败: {str(e)}")
            logger.error(traceback.format_exc())
            return HttpResponse("页面加载失败", status=500)


@method_decorator(login_required, name='dispatch')
class WclAnalysisPageView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'tools.wcl-analysis'

    def get(self, request):
        try:
            tasks = WclAnalysisTask.objects.filter(is_active=True).order_by('-created_at')[:30]
            task_list = []
            for t in tasks:
                task_list.append({
                    'id': t.id,
                    'wcl_url': t.wcl_url,
                    'status': t.status,
                    'summary': t.summary or '',
                    'created_at': _fmt_dt(t.created_at),
                    'report_url': f"/wcl-analysis/report/{t.id}/?token={t.access_token}"
                })
            return render(request, 'wcl_analysis.html', {'tasks': task_list})
        except Exception as e:
            logger.error(f"WCL分析输入页渲染失败: {str(e)}\n{traceback.format_exc()}")
            return HttpResponse("页面加载失败", status=500)


@method_decorator(login_required, name='dispatch')
class WclAnalysisListView(View):
    def get(self, request):
        try:
            tasks = WclAnalysisTask.objects.filter(is_active=True).order_by('-created_at')[:100]
            task_list = []
            for t in tasks:
                task_list.append({
                    'id': t.id,
                    'wcl_url': t.wcl_url,
                    'status': t.status,
                    'summary': t.summary or '',
                    'created_at': _fmt_dt(t.created_at),
                    'report_url': f"/wcl-analysis/report/{t.id}/?token={t.access_token}"
                })
            return render(request, 'wcl_analysis_list.html', {'tasks': task_list})
        except Exception as e:
            logger.error(f"WCL分析列表页渲染失败: {str(e)}\n{traceback.format_exc()}")
            return HttpResponse("页面加载失败", status=500)


class WclAnalysisReportView(View):
    def get(self, request, task_id):
        try:
            token = (request.GET.get('token') or '').strip()
            task = WclAnalysisTask.objects.filter(id=task_id, is_active=True).first()
            if not task:
                return HttpResponse("任务不存在", status=404)
            if not token or token != task.access_token:
                return HttpResponse("无权限访问该报告", status=403)

            if task.status != 2 or not task.report_html_file:
                return render(request, 'wcl_analysis_report.html', {
                    'task': task,
                    'token': token,
                    'status': task.status,
                    'error_message': task.error_message or '',
                    'report_html': ''
                })

            report_path = os.path.join(settings.BASE_DIR, 'static', 'wcl_reports', task.report_html_file)
            if not os.path.exists(report_path):
                return render(request, 'wcl_analysis_report.html', {
                    'task': task,
                    'token': token,
                    'status': 3,
                    'error_message': '报告文件不存在',
                    'report_html': ''
                })

            with open(report_path, 'r', encoding='utf-8') as f:
                report_html = f.read()
            return render(request, 'wcl_analysis_report.html', {
                'task': task,
                'token': token,
                'status': task.status,
                'error_message': task.error_message or '',
                'report_html': report_html
            })
        except Exception as e:
            logger.error(f"WCL分析报告页渲染失败: {str(e)}\n{traceback.format_exc()}")
            return HttpResponse("页面加载失败", status=500)
