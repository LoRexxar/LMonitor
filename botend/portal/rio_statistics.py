"""Raider.IO 数据观察页面与只读 API。"""
from django.http import JsonResponse, FileResponse, Http404
import re
import json
from django.shortcuts import render
from django.views import View

from botend.services import rio_statistics as service
from botend.services.rio_statistics_storage import StorageError


class RioStatisticsView(View):
    def get(self, request):
        return render(request, 'portal/rio_statistics.html')


class RioStatisticsAPIView(View):
    def get(self, request, module='catalog'):
        try:
            if module not in ('catalog', *service.MODULES):
                return JsonResponse({'success': False, 'message': '未知统计模块'}, status=404)
            from botend.services.rio_statistics_views import read_public, http_response
            body = read_public(module, request.GET, status_only=module == 'catalog' and request.GET.get('status') == '1')
            return http_response(request, body)
        except StorageError:
            return JsonResponse({'success': False, 'message': '统计缓存暂不可用，后台将检查恢复。'}, status=503)
        except ValueError as exc:
            message = str(exc) if re.search(r'[\u3400-\u9fff]', str(exc)) else '筛选条件无效，请重新选择。'
            return JsonResponse({'success': False, 'message': message}, status=400)
        except (TypeError, KeyError):
            return JsonResponse({'success': False, 'message': '筛选条件无效，请重新选择。'}, status=400)


class RioStatisticsMapView(View):
    def get(self, request, image_key):
        if not re.fullmatch(r'[0-9a-f]{64}', image_key):
            raise Http404
        path = service.root() / 'maps' / (image_key + '.webp')
        if not path.is_file():
            raise Http404
        response = FileResponse(path.open('rb'), content_type='image/webp')
        response['Cache-Control'] = 'public, max-age=86400, immutable'
        return response


class RioStatisticsPrepareView(View):
    def post(self, request):
        if len(request.body) > 8192:
            return JsonResponse({'success': False, 'message': '筛选条件过多'}, status=400)
        try:
            body = json.loads(request.body)
            if not isinstance(body, dict) or body.get('module') != 'heatmaps' or not isinstance(body.get('params'), dict):
                raise ValueError('只支持登记有效的大秘境筛选')
            meta = service.current_catalog()
            if meta.get('scope_state') != 'ready':
                raise ValueError('当前赛季资料尚未就绪')
            coordinate = service.coordinate('heatmaps', body['params'], meta=meta)
            from botend.services.rio_statistics_queries import subscribe
            result = subscribe(coordinate, client=request.META.get('REMOTE_ADDR', ''))
        except (ValueError, TypeError, KeyError) as exc:
            return JsonResponse({'success': False, 'message': str(exc) or '筛选条件无效'}, status=400)
        return JsonResponse({'success': True, 'data': result})
