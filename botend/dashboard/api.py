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

import json
import traceback

from utils.log import logger
from botend.models import SimcAplKeywordPair
from django.db import models


@method_decorator(csrf_exempt, name='dispatch')
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
                'error': f'转换失败: {str(e)}'
            })
    
    def convert_apl_to_cn(self, text):
        """
        """
        try:
            # 获取所有关键字对
            keyword_pairs = SimcAplKeywordPair.objects.filter(is_active=True)
            
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
            
            result = text
            for pair in keyword_pairs:
                # 从APL转换到SimC
                result = result.replace(pair.cn_keyword, pair.apl_keyword)
            
            return result
            
        except Exception as e:
            logger.error(f"CN2APL错误: {str(e)}")
            raise e


@method_decorator(csrf_exempt, name='dispatch')
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