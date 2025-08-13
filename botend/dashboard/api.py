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

from utils.log import logger
from botend.models import SimcAplKeywordPair, UserAplStorage, SimcTask, SimcProfile
from django.db import models


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
            
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': '无效的JSON数据'
            })
        except Exception as e:
            logger.error(f"文本转换API错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'获取APL详情失败: {str(e)}'
            })


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
                tasks_data.append({
                    'id': task.id,
                    'name': task.name,
                    'simc_profile_id': task.simc_profile_id,
                    'current_status': task.current_status,
                    'result_file': task.result_file,
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
            
            # 创建新任务
            task = SimcTask.objects.create(
                user_id=request.user.id,
                name=name,
                simc_profile_id=simc_profile_id,
                current_status=current_status,
                result_file=result_file
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
            
            # 更新任务
            task.name = name
            task.simc_profile_id = simc_profile_id
            task.current_status = current_status
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
    
    def convert_apl_to_cn(self, text):
        """
        """
        try:
            # 获取所有关键字对
            keyword_pairs = SimcAplKeywordPair.objects.filter(is_active=True)
            
            # 按APL关键字长度降序排列，优先替换更长的关键字
            keyword_pairs = sorted(keyword_pairs, key=lambda x: len(x.apl_keyword), reverse=True)
            
            result = text
            for pair in keyword_pairs:
                # 从SimC转换到APL
                result = result.replace(pair.apl_keyword, pair.cn_keyword)
            
            return result
            
        except Exception as e:
            logger.error(f"APL2CN错误: {str(e)}")
            raise e
    
    def convert_cn_to_apl(self, text):
        """
        """
        try:
            # 获取所有关键字对
            keyword_pairs = SimcAplKeywordPair.objects.filter(is_active=True)
            
            # 按中文关键字长度降序排列，优先替换更长的关键字
            keyword_pairs = sorted(keyword_pairs, key=lambda x: len(x.cn_keyword), reverse=True)
            
            result = text
            for pair in keyword_pairs:
                # 从APL转换到SimC
                result = result.replace(pair.cn_keyword, pair.apl_keyword)
            
            return result
            
        except Exception as e:
            logger.error(f"CN2APL错误: {str(e)}")
            raise e


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
    
    def get(self, request):
        """获取SimC配置列表"""
        try:
            profiles = SimcProfile.objects.filter(
                user_id=request.user.id,
                is_active=True
            ).order_by('-id')
            
            profile_list = []
            for profile in profiles:
                profile_list.append({
                    'id': profile.id,
                    'name': profile.name,
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
            logger.error(f"获取SimC配置列表失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '获取SimC配置列表失败'
            })
    
    def post(self, request):
        """创建新的SimC配置"""
        try:
            data = json.loads(request.body)
            
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
            
            # 创建新配置
            profile = SimcProfile.objects.create(
                user_id=request.user.id,
                name=name,
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
            
            return JsonResponse({
                'success': True,
                'message': 'SimC配置创建成功',
                'data': {
                    'id': profile.id,
                    'name': profile.name
                }
            })
            
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


@method_decorator([csrf_exempt, login_required], name='dispatch')
class SimcTemplateAPIView(View):
    """
    SimC模板文件API
    """
    
    def get(self, request):
        """获取SimC模板文件内容"""
        try:
            from django.conf import settings
            import os
            
            # 获取模板文件路径
            template_path = getattr(settings, 'SIMC_TEMPLATE_PATH', None)
            if not template_path:
                # 默认路径
                template_path = os.path.join(settings.BASE_DIR, 'LMonitor', 'simc_template.txt')
            
            # 读取模板文件内容
            if os.path.exists(template_path):
                with open(template_path, 'r', encoding='utf-8') as f:
                    template_content = f.read()
                
                return JsonResponse({
                    'success': True,
                    'template': template_content
                })
            else:
                return JsonResponse({
                    'success': False,
                    'error': '模板文件不存在'
                })
                
        except Exception as e:
            logger.error(f"获取SimC模板失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '获取SimC模板失败'
            })