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
from django.apps import apps
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator

import json
import traceback
import datetime

from utils.log import logger
from botend.models import (MonitorTask, TargetAuth, MonitorWebhook, WechatAccountTask, 
                          WechatArticle, VulnMonitorTask, VulnData, RssMonitorTask, 
                          RssArticle, WowArticle, SimcAplKeywordPair, SimcTask, SimcProfile)

# 模型描述映射
MODEL_DESCRIPTIONS = {
    'MonitorTask': '监控任务',
    'TargetAuth': '目标认证信息',
    'MonitorWebhook': '监控Webhook',
    'WechatAccountTask': '微信公众号任务',
    'WechatArticle': '微信文章',
    'VulnMonitorTask': '漏洞监控任务',
    'VulnData': '漏洞数据',
    'RssMonitorTask': 'RSS监控任务',
    'RssArticle': 'RSS文章',
    'WowArticle': 'Wow文章',
    'SimcAplKeywordPair': '关键字管理',
    'SimcTask': 'SimC任务管理',
    'SimcProfile': 'SimC配置管理',

}

@method_decorator(login_required, name='dispatch')
class DashboardView(View):
    """
    处理Dashboard页面请求
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
    
    def get(self, request):
        """
        处理GET请求，渲染仪表盘页面
        """
        try:
            # 获取所有数据库表信息
            tables_info = []
            
            # 获取所有已定义的模型
            models = [
                MonitorTask, TargetAuth, WechatAccountTask, 
                WechatArticle, VulnMonitorTask, VulnData, RssMonitorTask, 
                RssArticle, WowArticle, SimcAplKeywordPair, SimcTask, SimcProfile
            ]
            
            total_records = 0
            for model in models:
                model_name = model.__name__
                record_count = model.objects.count()
                total_records += record_count
                tables_info.append({
                    'name': model_name,
                    'description': MODEL_DESCRIPTIONS.get(model_name, model_name),
                    'count': record_count
                })
            
            # 计算更有意义的统计数据
            stats = self.calculate_dashboard_stats()
            
            context = {
                'title': 'LMonitor Dashboard',
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
            elif action == 'update_table_row':
                return self.update_table_row(data)
            elif action == 'delete_table_row':
                return self.delete_table_row(data)
            elif action == 'create_table_row':
                return self.create_table_row(data)
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
            
            logger.info(f"获取表数据: {table_name}, page: {page}, page_size: {page_size}, search: {search_query}")
            
            # 模型映射
            model_map = {
                'MonitorTask': MonitorTask,
                'TargetAuth': TargetAuth,
                'MonitorWebhook': MonitorWebhook,
                'WechatAccountTask': WechatAccountTask,
                'WechatArticle': WechatArticle,
                'VulnMonitorTask': VulnMonitorTask,
                'VulnData': VulnData,
                'RssMonitorTask': RssMonitorTask,
                'RssArticle': RssArticle,
                'WowArticle': WowArticle,
                'SimcAplKeywordPair': SimcAplKeywordPair,
                'SimcTask': SimcTask,
                'SimcProfile': SimcProfile,

            }
            
            # 检查表名是否有效
            if table_name not in model_map:
                return JsonResponse({"status": "error", "message": f"未知表名: {table_name}"})
            
            # 获取模型类
            model = model_map[table_name]
            
            # 获取字段名和字段类型信息
            fields = [field.name for field in model._meta.fields]
            field_types = {}
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
                
                # 处理choices，确保可以JSON序列化
                choices = getattr(field, 'choices', None)
                if choices is not None:
                    try:
                        json.dumps(choices)  # 测试是否可以序列化
                    except (TypeError, ValueError):
                        choices = None  # 不可序列化的choices设为None
                
                field_types[field.name] = {
                    'type': field_type,
                    'null': field.null,
                    'blank': field.blank,
                    'max_length': getattr(field, 'max_length', None),
                    'default': default_value,
                    'help_text': getattr(field, 'help_text', ''),
                    'choices': choices
                }
            
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
                    queryset = model.objects.values('id', 'title', 'url', 'author', 'publish_time', 'biz').order_by('-publish_time')
                    queryset = apply_search_filter(queryset, ['title', 'author', 'url'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                elif table_name == 'VulnData':
                    queryset = model.objects.values('id', 'cveid', 'title', 'score', 'publish_time', 'link').order_by('-publish_time')
                    queryset = apply_search_filter(queryset, ['cveid', 'title', 'link'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                elif table_name == 'RssArticle':
                    queryset = model.objects.values('id', 'title', 'url', 'author', 'publish_time').order_by('-publish_time')
                    queryset = apply_search_filter(queryset, ['title', 'author', 'url'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                elif table_name == 'WowArticle':
                    queryset = model.objects.values('id', 'title', 'url', 'author', 'publish_time').order_by('-publish_time')
                    queryset = apply_search_filter(queryset, ['title', 'author', 'url'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                elif table_name == 'GeWechatAuth':
                    queryset = model.objects.values('id', 'appId', 'uuid', 'create_time', 'login_status').order_by('-create_time')
                    queryset = apply_search_filter(queryset, ['appId', 'uuid'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                elif table_name == 'SimcAplKeywordPair':
                    queryset = model.objects.values('id', 'apl_keyword', 'cn_keyword', 'description', 'is_active', 'create_time').order_by('-create_time')
                    queryset = apply_search_filter(queryset, ['apl_keyword', 'cn_keyword', 'description'])
                    total_count = queryset.count()
                    items = list(queryset[offset:offset + page_size])
                else:
                    queryset = model.objects.values().order_by('-id')
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
                    if hasattr(value, 'strftime'):
                        item[key] = value.strftime('%Y-%m-%d %H:%M:%S')
                    elif isinstance(value, (datetime.datetime, datetime.date)):
                        item[key] = value.strftime('%Y-%m-%d %H:%M:%S')
            
            # 计算分页信息
            total_pages = (total_count + page_size - 1) // page_size
            
            # 返回数据
            return JsonResponse({
                "status": "success", 
                "data": items,
                "fields": fields,
                "field_types": field_types,
                "total_count": total_count,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages
            })
            
        except Exception as e:
            logger.error(f"获取表数据异常: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"status": "error", "message": f"获取表数据异常: {str(e)}"})
    
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
            model_map = {
                'MonitorTask': MonitorTask,
                'TargetAuth': TargetAuth,
                'MonitorWebhook': MonitorWebhook,
                'WechatAccountTask': WechatAccountTask,
                'WechatArticle': WechatArticle,
                'VulnMonitorTask': VulnMonitorTask,
                'VulnData': VulnData,
                'RssMonitorTask': RssMonitorTask,
                'RssArticle': RssArticle,
                'WowArticle': WowArticle,
                'SimcAplKeywordPair': SimcAplKeywordPair,
                'SimcTask': SimcTask,

            }
            
            # 获取模型
            model = model_map.get(table_name)
            if not model:
                return JsonResponse({"status": "error", "message": f"未找到表: {table_name}"})
            
            # 查找要更新的记录
            try:
                # 尝试使用id字段查找
                if hasattr(model, 'id'):
                    instance = model.objects.get(id=row_id)
                else:
                    # 如果没有id字段，使用第一个字段
                    fields = [f.name for f in model._meta.fields]
                    if fields:
                        filter_kwargs = {fields[0]: row_id}
                        instance = model.objects.get(**filter_kwargs)
                    else:
                        return JsonResponse({"status": "error", "message": "无法确定主键字段"})
            except model.DoesNotExist:
                return JsonResponse({"status": "error", "message": f"未找到ID为{row_id}的记录"})
            
            # 更新字段
            for field_name, field_value in update_data.items():
                if hasattr(instance, field_name):
                    # 获取字段类型
                    field = instance._meta.get_field(field_name)
                    
                    # 根据字段类型转换值
                    if field.__class__.__name__ == 'BooleanField':
                        if isinstance(field_value, str):
                            field_value = field_value.lower() in ('true', '1', 'yes', 'on')
                        elif isinstance(field_value, bool):
                            pass  # 已经是布尔值
                        else:
                            field_value = bool(field_value)
                    elif field.__class__.__name__ in ['IntegerField', 'BigIntegerField', 'SmallIntegerField']:
                        if field_value != '' and field_value is not None:
                            field_value = int(field_value)
                    elif field.__class__.__name__ in ['FloatField', 'DecimalField']:
                        if field_value != '' and field_value is not None:
                            field_value = float(field_value)
                    
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
            model_map = {
                'MonitorTask': MonitorTask,
                'TargetAuth': TargetAuth,
                'MonitorWebhook': MonitorWebhook,
                'WechatAccountTask': WechatAccountTask,
                'WechatArticle': WechatArticle,
                'VulnMonitorTask': VulnMonitorTask,
                'VulnData': VulnData,
                'RssMonitorTask': RssMonitorTask,
                'RssArticle': RssArticle,
                'WowArticle': WowArticle,
                'SimcAplKeywordPair': SimcAplKeywordPair,
                'SimcTask': SimcTask,

            }
            
            # 获取模型
            model = model_map.get(table_name)
            if not model:
                return JsonResponse({"status": "error", "message": f"未找到表: {table_name}"})
            
            # 查找要删除的记录
            try:
                # 尝试使用id字段查找
                if hasattr(model, 'id'):
                    instance = model.objects.get(id=row_id)
                else:
                    # 如果没有id字段，使用第一个字段
                    fields = [f.name for f in model._meta.fields]
                    if fields:
                        filter_kwargs = {fields[0]: row_id}
                        instance = model.objects.get(**filter_kwargs)
                    else:
                        return JsonResponse({"status": "error", "message": "无法确定主键字段"})
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
            model_map = {
                'MonitorTask': MonitorTask,
                'TargetAuth': TargetAuth,
                'MonitorWebhook': MonitorWebhook,
                'WechatAccountTask': WechatAccountTask,
                'WechatArticle': WechatArticle,
                'VulnMonitorTask': VulnMonitorTask,
                'VulnData': VulnData,
                'RssMonitorTask': RssMonitorTask,
                'RssArticle': RssArticle,
                'WowArticle': WowArticle,
                'SimcAplKeywordPair': SimcAplKeywordPair,
                'SimcTask': SimcTask,
                'SimcProfile': SimcProfile,
            }
            
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
                        if hasattr(model, key):
                            filtered_data[key] = value
                
                # 创建记录
                instance = model.objects.create(**filtered_data)
                
                logger.info(f"成功创建记录: {table_name}, id: {instance.id}")
                
                return JsonResponse({
                    "status": "success", 
                    "message": "记录创建成功",
                    "data": {"id": instance.id}
                })
                
            except Exception as e:
                logger.error(f"创建记录失败: {str(e)}\n{traceback.format_exc()}")
                return JsonResponse({"status": "error", "message": f"创建记录失败: {str(e)}"})
                
        except Exception as e:
            logger.error(f"创建表数据错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({"status": "error", "message": f"创建数据错误: {str(e)}"})