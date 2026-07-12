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
from django.shortcuts import render, redirect
from django.db.models import Count, Q
from django.db.utils import OperationalError, ProgrammingError
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
                          RssArticle, WowArticle, SimcAplKeywordPair, SimcTask, SimcProfile, SimcSecondaryStatRule, WclAnalysisTask)

from botend.services.simc_attribute_results import parse_attribute_result_filename


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
    'SimcAplKeywordPair': '关键字管理',
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
    'UserAplStorage': 'APL保存记录',
    'SimcContentTemplate': 'SimC模板/APL',
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
        return {model.__name__: model for model in apps.get_app_config('botend').get_models()}
    
    def get(self, request):
        """
        处理GET请求，渲染仪表盘页面
        """
        try:
            # 获取所有数据库表信息
            tables_info = []
            
            # 获取所有已定义的模型
            models = list(self._get_model_map().values())
            
            total_records = 0
            for model in models:
                model_name = model.__name__
                try:
                    record_count = model.objects.count()
                except (OperationalError, ProgrammingError):
                    record_count = 0
                total_records += record_count
                tables_info.append({
                    'name': model_name,
                    'description': MODEL_DESCRIPTIONS.get(model_name) or str(getattr(model._meta, 'verbose_name', '')) or model_name,
                    'count': record_count
                })
            
            # 计算更有意义的统计数据
            stats = self.calculate_dashboard_stats()
            
            context = {
                'title': '后台',
                'page_name': 'dashboard',
                'tables_info': tables_info,
                'total_tables': len(models),
                'total_records': total_records,
                'stats': stats
            }
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
            # 记录请求信息，便于调试
            logger.info(f"Dashboard POST请求: {request.body.decode('utf-8')[:200]}")
            
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
            page = int(data.get('page', 1))
            page_size = int(data.get('page_size', 50))
            
            # 获取搜索参数
            search_query = data.get('search', '').strip()
            simc_spec_filter = data.get('simc_spec', '').strip()
            simc_fight_style_filter = data.get('simc_fight_style', '').strip()
            wow_source_filter = data.get('wow_source', '').strip()
            wow_category_filter = data.get('wow_category', '').strip()
            
            logger.info(f"获取表数据: {table_name}, page: {page}, page_size: {page_size}, search: {search_query}")
            
            # 模型映射
            model_map = self._get_model_map()
            
            # 检查表名是否有效
            if table_name not in model_map:
                return JsonResponse({"status": "error", "message": f"未知表名: {table_name}"})
            
            # 获取模型类
            model = model_map[table_name]
            
            # 获取字段名和字段类型信息
            fields = [field.name for field in model._meta.fields]
            field_types = {}
            field_labels = {}
            for field in model._meta.fields:
                field_type = field.__class__.__name__
                
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
                    'editable': getattr(field, 'editable', True),
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
            
            # 根据表名获取对应的数据
            try:
                if table_name == 'WechatArticle':
                    queryset = model.objects.values('id', 'title', 'url', 'author', 'publish_time', 'biz').order_by('-id')
                    queryset = apply_search_filter(queryset, ['title', 'author', 'url'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                elif table_name == 'VulnData':
                    queryset = model.objects.values('id', 'cveid', 'title', 'score', 'publish_time', 'link').order_by('-id')
                    queryset = apply_search_filter(queryset, ['cveid', 'title', 'link'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                elif table_name == 'RssArticle':
                    queryset = model.objects.values('id', 'title', 'url', 'author', 'publish_time').order_by('-id')
                    queryset = apply_search_filter(queryset, ['title', 'author', 'url'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                elif table_name == 'WowArticle':
                    base_queryset = model.objects.values(
                        'id', 'title', 'title_cn', 'url', 'author', 'publish_time', 'description',
                        'source', 'category', 'reply_count'
                    ).order_by('-publish_time', '-id')
                    if wow_source_filter:
                        base_queryset = base_queryset.filter(source=wow_source_filter)
                    if wow_category_filter:
                        base_queryset = base_queryset.filter(category=wow_category_filter)
                    base_queryset = apply_search_filter(base_queryset, ['title', 'title_cn', 'description', 'author', 'url'])
                    total_count = base_queryset.count()
                    items = list(base_queryset[offset:offset + page_size])
                elif table_name == 'GeWechatAuth':
                    queryset = model.objects.values('id', 'appId', 'uuid', 'create_time', 'login_status').order_by('create_time')
                    queryset = apply_search_filter(queryset, ['appId', 'uuid'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                elif table_name == 'SimcAplKeywordPair':
                    queryset = model.objects.values('id', 'apl_keyword', 'cn_keyword', 'description', 'is_active', 'create_time').order_by('create_time')
                    queryset = apply_search_filter(queryset, ['apl_keyword', 'cn_keyword', 'description'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                elif table_name == 'SimcProfile':
                    queryset = model.objects.values(
                        'id', 'name', 'spec',
                        'player_config_mode', 'battlenet_region', 'battlenet_realm',
                        'battlenet_character', 'player_equipment',
                        'talent', 'gear_strength', 'gear_crit',
                        'gear_haste', 'gear_mastery', 'gear_versatility', 'is_active'
                    ).filter(is_active=True).order_by('-id')
                    queryset = apply_search_filter(queryset, ['name', 'spec', 'talent', 'battlenet_realm', 'battlenet_character', 'player_equipment'])
                    if simc_spec_filter:
                        queryset = queryset.filter(spec__icontains=simc_spec_filter)
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                elif table_name == 'SimcSecondaryStatRule':
                    from botend.models import SimcSecondaryStatRule as RuleModel
                    queryset = RuleModel.objects.values(
                        'id', 'class_name', 'crit_per_percent', 'haste_per_percent',
                        'mastery_per_percent', 'versatility_per_percent'
                    ).order_by('class_name')
                    queryset = apply_search_filter(queryset, ['class_name'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                elif table_name == 'SimcMasteryCoefficient':
                    from botend.models import SimcMasteryCoefficient as McModel
                    queryset = McModel.objects.values(
                        'id', 'spec', 'mastery_coefficient'
                    ).order_by('spec')
                    queryset = apply_search_filter(queryset, ['spec'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                else:
                    pk_name = model._meta.pk.name
                    queryset = model.objects.values().order_by(f'-{pk_name}')
                    # 对于通用表，尝试搜索所有文本字段
                    text_fields = [field.name for field in model._meta.fields if field.__class__.__name__ in ['CharField', 'TextField']]
                    queryset = apply_search_filter(queryset, text_fields)
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
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
            
            # 计算分页信息
            total_pages = (total_count + page_size - 1) // page_size
            
            # 返回数据
            table_description = MODEL_DESCRIPTIONS.get(table_name) or str(getattr(model._meta, 'verbose_name', '') or '').strip() or table_name
            resp = {
                "status": "success", 
                "data": items,
                "fields": fields,
                "field_types": field_types,
                "field_labels": field_labels,
                "table_description": table_description,
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

    def update_table_row(self, data):
        """
        更新表格行数据
        """
        try:
            # 获取参数
            table_name = data.get('table_name')
            row_id = data.get('row_id')
            update_data = data.get('update_data')
            
            if not table_name or not row_id or not update_data:
                return JsonResponse({"status": "error", "message": "缺少必要参数"})
            
            logger.info(f"更新表数据: {table_name}, row_id: {row_id}, data: {update_data}")
            
            # 模型映射
            model_map = self._get_model_map()
            
            # 获取模型
            model = model_map.get(table_name)
            if not model:
                return JsonResponse({"status": "error", "message": f"未找到表: {table_name}"})
            
            # 查找要更新的记录
            try:
                pk_name = model._meta.pk.name
                instance = model.objects.get(**{pk_name: row_id})
            except model.DoesNotExist:
                return JsonResponse({"status": "error", "message": f"未找到ID为{row_id}的记录"})
            
            # 更新字段
            for field_name, field_value in update_data.items():
                if hasattr(instance, field_name):
                    # 获取字段类型
                    field = instance._meta.get_field(field_name)
                    field_type = field.__class__.__name__

                    if getattr(field, 'primary_key', False) or not getattr(field, 'editable', True) or getattr(field, 'auto_now', False) or getattr(field, 'auto_now_add', False):
                        continue
                    
                    # 根据字段类型转换值
                    if field_type == 'BooleanField':
                        if isinstance(field_value, str):
                            field_value = field_value.lower() in ('true', '1', 'yes', 'on')
                        elif isinstance(field_value, bool):
                            pass  # 已经是布尔值
                        else:
                            field_value = bool(field_value)
                    elif field_type in ['IntegerField', 'BigIntegerField', 'SmallIntegerField', 'PositiveIntegerField', 'PositiveSmallIntegerField', 'AutoField', 'BigAutoField']:
                        if field_value != '' and field_value is not None:
                            field_value = int(field_value)
                    elif field_type in ['FloatField', 'DecimalField']:
                        if field_value != '' and field_value is not None:
                            field_value = float(field_value)
                    elif field_type == 'JSONField':
                        if isinstance(field_value, str):
                            raw_value = field_value.strip()
                            if raw_value == '':
                                field_value = None if field.null else field.get_default()
                            else:
                                try:
                                    field_value = json.loads(raw_value)
                                except json.JSONDecodeError:
                                    return JsonResponse({
                                        "status": "error",
                                        "message": f"字段 {field_name} 不是合法 JSON，已取消更新"
                                    })
                    
                    setattr(instance, field_name, field_value)
            
            # 保存更改
            instance.save()
            
            return JsonResponse({"status": "success", "message": "更新成功"})
            
        except Exception as e:
            logger.error(f"更新表数据异常: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"status": "error", "message": f"更新失败: {str(e)}"})
    
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
            
            # 模型映射
            model_map = self._get_model_map()
            
            # 获取模型
            model = model_map.get(table_name)
            if not model:
                return JsonResponse({"status": "error", "message": f"未找到表: {table_name}"})
            
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
    
    def create_table_row(self, data):
        """
        创建新的表格行数据
        """
        try:
            # 获取参数
            table_name = data.get('table_name')
            create_data = data.get('create_data')
            
            if not table_name or not create_data:
                return JsonResponse({"status": "error", "message": "缺少必要参数"})
            
            logger.info(f"创建表数据: {table_name}, data: {create_data}")
            
            # 模型映射
            model_map = self._get_model_map()
            
            # 获取模型
            model = model_map.get(table_name)
            if not model:
                return JsonResponse({"status": "error", "message": f"未找到表: {table_name}"})
            
            # 创建新记录
            try:
                # 过滤掉空值和无效字段
                filtered_data = {}
                for key, value in create_data.items():
                    if value is not None and value != '':
                        # 检查字段是否存在于模型中
                        try:
                            field = model._meta.get_field(key)
                        except Exception:
                            continue
                        if getattr(field, 'primary_key', False) or not getattr(field, 'editable', True) or getattr(field, 'auto_now', False) or getattr(field, 'auto_now_add', False):
                            continue
                        field_type = field.__class__.__name__
                        if field_type == 'JSONField' and isinstance(value, str):
                            try:
                                value = json.loads(value)
                            except json.JSONDecodeError:
                                return JsonResponse({
                                    "status": "error",
                                    "message": f"字段 {key} 不是合法 JSON，已取消创建"
                                })
                        elif field_type == 'BooleanField':
                            if isinstance(value, str):
                                value = value.lower() in ('true', '1', 'yes', 'on')
                            else:
                                value = bool(value)
                        elif field_type in ['IntegerField', 'BigIntegerField', 'SmallIntegerField', 'PositiveIntegerField', 'PositiveSmallIntegerField']:
                            value = int(value)
                        elif field_type in ['FloatField', 'DecimalField']:
                            value = float(value)
                        filtered_data[key] = value
                
                # 自动填充 user_id（如果模型有该字段且请求已登录）
                if 'user_id' not in filtered_data and hasattr(model, 'user_id') and hasattr(self.request, 'user') and self.request.user.is_authenticated:
                    filtered_data['user_id'] = self.request.user.id
                
                # 创建记录
                instance = model.objects.create(**filtered_data)
                
                instance_id = getattr(instance, 'id', instance.pk)
                logger.info(f"成功创建记录: {table_name}, id: {instance_id}")
                
                return JsonResponse({
                    "status": "success", 
                    "message": "记录创建成功",
                    "data": {"id": instance_id}
                })
                
            except Exception as e:
                logger.error(f"创建记录失败: {str(e)}\n{traceback.format_exc()}")
                return JsonResponse({"status": "error", "message": f"创建记录失败: {str(e)}"})
                
        except Exception as e:
            logger.error(f"创建表数据错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"status": "error", "message": f"创建数据错误: {str(e)}"})

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
class SimcResultView(View):
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
class SimcAttributeAnalysisView(View):
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
class SimcRegularCompareView(View):
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
class SimcAttributeAnalysisSSRView(View):
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
                task = SimcTask.objects.get(id=task_id, user_id=request.user.id, is_active=True)
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
                    'attr1_name': attr1_name,
                    'attr1_value': attr1_value,
                    'attr2_name': attr2_name,
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
            
            # 增加相对性能百分比，供模板渲染进度条
            for item in analysis:
                if max_dps == min_dps:
                    item['relative_percent'] = 100.0
                else:
                    item['relative_percent'] = (item['dps'] - min_dps) * 100.0 / (max_dps - min_dps)
            
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
                }
            }
            
            return render(request, 'simc_attribute_analysis_ssr.html', context)
        except Exception as e:
            logger.error(f"渲染属性模拟SSR页面失败: {str(e)}")
            logger.error(traceback.format_exc())
            return HttpResponse("页面加载失败", status=500)


@method_decorator(login_required, name='dispatch')
class WclAnalysisPageView(View):
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
