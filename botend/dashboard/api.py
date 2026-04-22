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
from django.http import JsonResponse
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
import threading
import uuid
from urllib.parse import urlparse, parse_qs
from django.utils import timezone
from django.template.loader import render_to_string

from django.conf import settings
from utils.log import logger
from botend.models import SimcAplKeywordPair, UserAplStorage, SimcTask, SimcProfile, SimcTemplate, WclAnalysisTask
from django.db import models
from core.glm import GLMClient


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
class SimcTaskAPIView(View):
    """
    SimC任务管理API
    """
    
    def get(self, request):
        """获取当前用户的SimC任务列表"""
        try:
            # 获取当前用户的所有SimC任务
            tasks = SimcTask.objects.filter(user_id=request.user.id, is_active=True).order_by('-modified_time')
            
            tasks_data = []
            for task in tasks:
                ext_detail = self._normalize_task_ext(task.task_type, task.ext)
                tasks_data.append({
                    'id': task.id,
                    'name': task.name,
                    'simc_profile_id': task.simc_profile_id,
                    'current_status': task.current_status,
                    'result_file': task.result_file,
                    'task_type': task.task_type,
                    'ext': task.ext,
                    'ext_detail': ext_detail,
                    'create_time': task.create_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'modified_time': task.modified_time.strftime('%Y-%m-%d %H:%M:%S'),
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
            current_status = data.get('current_status', 0)
            task_type = data.get('task_type', 1)
            ext = data.get('ext', '')
            regular_time = data.get('regular_time')
            regular_target_count = data.get('regular_target_count')
            selected_attributes = data.get('selected_attributes')
            attribute_step = data.get('attribute_step')
            
            if not name:
                return JsonResponse({
                    'success': False,
                    'error': '任务名称不能为空'
                })
            
            if not simc_profile_id:
                return JsonResponse({
                    'success': False,
                    'error': 'SimC配置不能为空'
                })
            
            # 验证SimC配置是否存在
            try:
                profile = SimcProfile.objects.get(
                    id=simc_profile_id,
                    user_id=request.user.id,
                    is_active=True
                )
            except SimcProfile.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': '指定的SimC配置不存在'
                })
            
            # 生成result_file
            timestamp = str(int(time.time()))
            content_to_hash = timestamp + name + str(request.user.id)
            result_file = hashlib.md5(content_to_hash.encode('utf-8')).hexdigest() + '.html'
            
            normalized_ext = self._build_task_ext(
                task_type=task_type,
                ext=ext,
                regular_time=regular_time,
                regular_target_count=regular_target_count,
                selected_attributes=selected_attributes,
                attribute_step=attribute_step
            )

            # 创建新任务
            task = SimcTask.objects.create(
                user_id=request.user.id,
                name=name,
                simc_profile_id=simc_profile_id,
                current_status=current_status,
                result_file=result_file,
                task_type=task_type,
                ext=normalized_ext
            )
            
            return JsonResponse({
                'success': True,
                'message': 'SimC任务创建成功',
                'data': {
                    'id': task.id,
                    'name': task.name,
                    'simc_profile_id': task.simc_profile_id,
                    'current_status': task.current_status,
                    'result_file': task.result_file,
                    'task_type': task.task_type,
                    'ext': task.ext,
                    'ext_detail': self._normalize_task_ext(task.task_type, task.ext),
                    'create_time': task.create_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'modified_time': task.modified_time.strftime('%Y-%m-%d %H:%M:%S'),
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
            current_status = data.get('current_status', 0)
            task_type = data.get('task_type', 1)
            ext = data.get('ext', '')
            regular_time = data.get('regular_time')
            regular_target_count = data.get('regular_target_count')
            selected_attributes = data.get('selected_attributes')
            attribute_step = data.get('attribute_step')
            
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
            
            if not simc_profile_id:
                return JsonResponse({
                    'success': False,
                    'error': 'SimC配置不能为空'
                })
            
            # 验证SimC配置是否存在
            try:
                profile = SimcProfile.objects.get(
                    id=simc_profile_id,
                    user_id=request.user.id,
                    is_active=True
                )
            except SimcProfile.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': '指定的SimC配置不存在'
                })
            
            # 获取任务并检查权限
            try:
                task = SimcTask.objects.get(id=task_id, user_id=request.user.id, is_active=True)
            except SimcTask.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': '任务不存在或无权限访问'
                })
            
            normalized_ext = self._build_task_ext(
                task_type=task_type,
                ext=ext,
                regular_time=regular_time,
                regular_target_count=regular_target_count,
                selected_attributes=selected_attributes,
                attribute_step=attribute_step
            )

            # 更新任务
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
                    'result_file': task.result_file,
                    'task_type': task.task_type,
                    'ext': task.ext,
                    'ext_detail': self._normalize_task_ext(task.task_type, task.ext),
                    'create_time': task.create_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'modified_time': task.modified_time.strftime('%Y-%m-%d %H:%M:%S'),
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
            
            # 生成新的结果文件名
            timestamp = str(int(time.time()))
            content_to_hash = timestamp + task.name + str(request.user.id)
            new_result_file = hashlib.md5(content_to_hash.encode('utf-8')).hexdigest() + '.html'
            
            # 重置任务状态
            task.current_status = 0  # 待处理
            task.result_file = new_result_file
            task.save()
            
            return JsonResponse({
                'success': True,
                'message': 'SimC任务重跑成功，任务已重新加入队列',
                'data': {
                    'id': task.id,
                    'name': task.name,
                    'simc_profile_id': task.simc_profile_id,
                    'current_status': task.current_status,
                    'result_file': task.result_file,
                    'task_type': task.task_type,
                    'ext': task.ext,
                    'ext_detail': self._normalize_task_ext(task.task_type, task.ext),
                    'create_time': task.create_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'modified_time': task.modified_time.strftime('%Y-%m-%d %H:%M:%S'),
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

    def _build_task_ext(self, task_type, ext, regular_time=None, regular_target_count=None, selected_attributes=None, attribute_step=None):
        ttype = int(task_type or 1)
        base = self._normalize_task_ext(ttype, ext)

        if ttype == 1:
            payload = {}
            if isinstance(base, dict):
                payload.update(base)
            if regular_time not in (None, ''):
                payload['regular_time'] = max(1, int(regular_time))
            if regular_target_count not in (None, ''):
                payload['regular_target_count'] = max(1, int(regular_target_count))
            return json.dumps(payload, ensure_ascii=False) if payload else ''

        if selected_attributes:
            base['selected_attributes'] = str(selected_attributes).strip()
        selected = str(base.get('selected_attributes') or '').strip()
        if not selected:
            raise Exception('属性模拟任务缺少属性组合')
        payload = {'selected_attributes': selected}
        step_value = attribute_step if attribute_step not in (None, '') else base.get('attribute_step')
        if step_value not in (None, ''):
            payload['attribute_step'] = max(1, int(step_value))
        return json.dumps(payload, ensure_ascii=False)


@method_decorator([csrf_exempt, login_required], name='dispatch')
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
                    'create_time': keyword.create_time.strftime('%Y-%m-%d %H:%M:%S') if keyword.create_time else ''
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
                    'create_time': keyword.create_time.strftime('%Y-%m-%d %H:%M:%S')
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


@method_decorator([csrf_exempt, login_required], name='dispatch')
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


@method_decorator([csrf_exempt, login_required], name='dispatch')
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


@method_decorator([csrf_exempt, login_required], name='dispatch')
class SimcProfileAPIView(View):
    """
    SimC配置管理API
    """
    
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
                        'fight_style': profile.fight_style,
                        'time': profile.time,
                        'target_count': profile.target_count,
                        'talent': profile.talent,
                        'action_list': profile.action_list,
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
                profiles = SimcProfile.objects.filter(
                    user_id=request.user.id,
                    is_active=True
                ).order_by('-id')
                
                profile_list = []
                for profile in profiles:
                    profile_list.append({
                        'id': profile.id,
                        'name': profile.name,
                        'spec': profile.spec,
                        'fight_style': profile.fight_style,
                        'time': profile.time,
                        'target_count': profile.target_count,
                        'talent': profile.talent,
                        'action_list': profile.action_list,
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
                    
                    # 复制配置数据
                    profile = SimcProfile.objects.create(
                        user_id=request.user.id,
                        name=name,
                        spec=source_profile.spec,
                        fight_style=source_profile.fight_style,
                        time=source_profile.time,
                        target_count=source_profile.target_count,
                        talent=source_profile.talent,
                        action_list=source_profile.action_list,
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
                # 创建新配置
                profile = SimcProfile.objects.create(
                    user_id=request.user.id,
                    name=name,
                    spec=(str(data.get('spec') or 'fury').strip().lower() or 'fury'),
                    fight_style=data.get('fight_style', 'Patchwerk'),
                    time=data.get('time', 40),
                    target_count=data.get('target_count', 1),
                    talent=data.get('talent', ''),
                    action_list=data.get('action_list', ''),
                    gear_strength=data.get('gear_strength', 93330),
                    gear_crit=data.get('gear_crit', 10730),
                    gear_haste=data.get('gear_haste', 18641),
                    gear_mastery=data.get('gear_mastery', 21785),
                    gear_versatility=data.get('gear_versatility', 6757),
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
            # 生成result_file
            timestamp = str(int(time.time()))
            content_to_hash = timestamp + profile.name + str(user_id)
            if selected_attributes:
                content_to_hash += selected_attributes
            if regular_time not in (None, ''):
                content_to_hash += str(regular_time)
            if regular_target_count not in (None, ''):
                content_to_hash += str(regular_target_count)
            if attribute_step not in (None, ''):
                content_to_hash += str(attribute_step)
            result_file = hashlib.md5(content_to_hash.encode('utf-8')).hexdigest() + '.html'
            
            # 根据任务类型生成任务名称
            if task_type == 2 and selected_attributes:
                task_name = f"{profile.name}_属性模拟_{selected_attributes}"
            else:
                task_name = f"{profile.name}_常规模拟"
            
            ext_payload = {}
            if task_type == 2:
                if selected_attributes:
                    ext_payload['selected_attributes'] = selected_attributes
                if attribute_step not in (None, ''):
                    ext_payload['attribute_step'] = max(1, int(attribute_step))
            else:
                if regular_time not in (None, ''):
                    ext_payload['regular_time'] = max(1, int(regular_time))
                if regular_target_count not in (None, ''):
                    ext_payload['regular_target_count'] = max(1, int(regular_target_count))

            # 创建SimcTask
            task = SimcTask.objects.create(
                user_id=user_id,
                name=task_name,
                simc_profile_id=profile.id,
                current_status=0,  # 待执行
                result_file=result_file,
                task_type=task_type,
                ext=json.dumps(ext_payload, ensure_ascii=False) if ext_payload else (selected_attributes or '')
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
            
            # 更新配置
            profile.name = name
            profile.spec = str(data.get('spec', profile.spec) or 'fury').strip().lower() or 'fury'
            profile.fight_style = data.get('fight_style', profile.fight_style)
            profile.time = data.get('time', profile.time)
            profile.target_count = data.get('target_count', profile.target_count)
            profile.talent = data.get('talent', profile.talent)
            profile.action_list = data.get('action_list', profile.action_list)
            profile.gear_strength = data.get('gear_strength', profile.gear_strength)
            profile.gear_crit = data.get('gear_crit', profile.gear_crit)
            profile.gear_haste = data.get('gear_haste', profile.gear_haste)
            profile.gear_mastery = data.get('gear_mastery', profile.gear_mastery)
            profile.gear_versatility = data.get('gear_versatility', profile.gear_versatility)
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


@method_decorator([csrf_exempt], name='dispatch')
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


@method_decorator([csrf_exempt], name='dispatch')
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
            
            # 获取任务信息
            try:
                task = SimcTask.objects.get(id=task_id)
            except SimcTask.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': '任务不存在'
                })
            
            if task.task_type != 2:
                return JsonResponse({
                    'success': False,
                    'error': '该任务不是属性模拟任务'
                })
            
            if not task.result_file:
                return JsonResponse({
                    'success': False,
                    'error': '任务尚未完成或无结果文件'
                })
            
            # 解析结果文件列表
            result_files = task.result_file.split(',')
            analysis_data = []
            
            # OSS配置
            oss_config = getattr(settings, 'OSS_CONFIG', {})
            base_url = oss_config.get('base_url', '')
            
            for result_file in result_files:
                result_file = result_file.strip()
                if not result_file:
                    continue
                
                try:
                    # 从文件名解析属性信息
                    # 格式: {任务ID}_{属性1}_{值1}_{属性2}_{值2}.html
                    filename_parts = result_file.replace('.html', '').split('_')
                    if len(filename_parts) >= 5:
                        attr1_name = filename_parts[2]
                        attr2_name = filename_parts[5]
                        
                        # 尝试转换为数字，如果失败则保持字符串
                        try:
                            attr1_value = int(filename_parts[3])
                        except ValueError:
                            attr1_value = filename_parts[3]
                        
                        try:
                            attr2_value = int(filename_parts[6])
                        except ValueError:
                            attr2_value = filename_parts[6]
                    else:
                        logger.warning(f"无法解析文件名格式: {result_file}")
                        continue
                    
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
            
            return JsonResponse({
                'success': True,
                'data': {
                    'task_name': task.name,
                    'task_id': task.id,
                    'results': analysis_data,
                    'total_count': len(analysis_data)
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
    
    def get(self, request):
        try:
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
                    'result_file': task.result_file,
                    'dps': parsed.get('dps'),
                    'character': parsed.get('character', {}),
                    'simulation': parsed.get('simulation', {}),
                    'talents': parsed.get('talents', {}),
                    'top_abilities': parsed.get('top_abilities', [])
                })
            
            if len(tasks_data) < 2:
                return JsonResponse({
                    'success': False,
                    'error': '可用于对比的任务不足2个',
                    'invalid': invalid,
                    'data': {
                        'tasks': tasks_data
                    }
                })
            
            return JsonResponse({
                'success': True,
                'data': {
                    'tasks': tasks_data,
                    'invalid': invalid
                }
            })
            
        except Exception as e:
            logger.error(f"常规模拟对比失败: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'对比失败: {str(e)}'
            })
    
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
                    result['top_abilities'] = [{
                        'name': a.get('name', ''),
                        'dps': a.get('dps', ''),
                        'dps_percent': a.get('dps_percent', '')
                    } for a in abilities[:12]]
            
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


@method_decorator([csrf_exempt, login_required], name='dispatch')
class SimcTemplateAPIView(View):
    """
    SimC模板API
    """
    
    def get(self, request):
        """获取SimC模板列表或单个模板内容"""
        try:
            template_id = request.GET.get('id')
            
            if template_id:
                # 获取单个模板的完整内容
                try:
                    template = SimcTemplate.objects.get(id=template_id)
                    return JsonResponse({
                        'success': True,
                        'id': template.id,
                        'template_content': template.template_content,
                        'spec': template.spec,
                        'is_active': template.is_active
                    })
                except SimcTemplate.DoesNotExist:
                    return JsonResponse({
                        'success': False,
                        'error': '模板不存在'
                    })
            else:
                # 获取所有模板的列表
                templates = SimcTemplate.objects.all().order_by('-id')
                template_list = []
                
                for template in templates:
                    # 获取模板内容的前100个字符作为预览
                    preview = template.template_content[:100] + '...' if len(template.template_content) > 100 else template.template_content
                    template_list.append({
                        'id': template.id,
                        'template_content': template.template_content,
                        'spec': template.spec,
                        'is_active': template.is_active
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
            template_content = data.get('template_content', '') or data.get('template', '')
            template_spec = (str(data.get('spec') or '').strip().lower() or None)
            
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
                template = SimcTemplate.objects.get(id=template_id)
                template.template_content = template_content
                if template_spec is not None:
                    template.spec = template_spec
                template.save()
                
                logger.info(f"SimC模板已更新: ID {template.id}")
                
                return JsonResponse({
                    'success': True,
                    'message': '模板更新成功'
                })
            except SimcTemplate.DoesNotExist:
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
                template = SimcTemplate.objects.get(id=template_id)
                template.is_active = is_active
                template.save()
                
                status_text = '启用' if is_active else '禁用'
                logger.info(f"SimC模板已{status_text}: ID {template.id}")
                
                return JsonResponse({
                    'success': True,
                    'message': f'模板{status_text}成功'
                })
            except SimcTemplate.DoesNotExist:
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
            template_content = data.get('template_content', '')
            template_spec = (str(data.get('spec') or '').strip().lower() or 'default')
            
            if not template_content:
                return JsonResponse({
                    'success': False,
                    'error': '模板内容不能为空'
                })
            
            # 创建新模板
            template = SimcTemplate.objects.create(
                template_content=template_content,
                spec=template_spec,
                is_active=False  # 新创建的模板默认为禁用状态
            )
            
            logger.info(f"SimC模板已创建: ID {template.id}")
            
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
            'created_at': task.created_at.strftime('%Y-%m-%d %H:%M:%S') if task.created_at else None,
            'updated_at': task.updated_at.strftime('%Y-%m-%d %H:%M:%S') if task.updated_at else None,
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
