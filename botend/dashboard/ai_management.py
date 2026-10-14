"""Superuser-only AI settings API. CSRF middleware deliberately remains enabled."""
import json

from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.http import JsonResponse
from django.views import View

from botend.services.ai_provider_service import (
    ConfigurationConflict, PROVIDERS, get_ai_provider_management_data,
    update_ai_provider_configuration,
)


class AIProviderManagementAPIView(View):
    http_method_names = ['get', 'patch', 'options']

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return JsonResponse({'status': 'error', 'message': '仅超级管理员可管理 AI 配置'}, status=403)
        try:
            return super().dispatch(request, *args, **kwargs)
        except (DatabaseError, ValueError):
            # Never include database errors, encrypted values, or incoming payloads.
            return JsonResponse({'status': 'error', 'message': 'AI 配置暂不可用'}, status=503)

    def get(self, request, provider_key=None):
        if provider_key is not None:
            return JsonResponse({'status': 'error', 'message': '此端点仅接受 PATCH'}, status=405)
        return JsonResponse({'status': 'success', 'data': get_ai_provider_management_data()})

    def patch(self, request, provider_key=None):
        if provider_key not in PROVIDERS:
            return JsonResponse({'status': 'error', 'message': '未知 AI 供应商'}, status=404)
        try:
            payload = json.loads(request.body)
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({'status': 'error', 'message': '请求必须是 JSON 对象'}, status=400)
        try:
            update_ai_provider_configuration(provider_key, payload, request.user)
        except ValidationError as error:
            return JsonResponse({'status': 'error', 'message': '；'.join(error.messages)}, status=400)
        except ConfigurationConflict:
            return JsonResponse({'status': 'error', 'message': '配置已更新，请刷新后重试'}, status=409)
        return JsonResponse({'status': 'success', 'data': get_ai_provider_management_data()})
