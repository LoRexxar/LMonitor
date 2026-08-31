"""Portal 职业配装器页面与只读 API。"""

from __future__ import annotations

import json

from django.http import JsonResponse
from django.shortcuts import render
from django.views import View

from botend.services.gear_builder import (
    GearBuilderError,
    bootstrap_payload,
    catalog_items,
    enhancement_items,
    import_simc_profile,
    resolve_crafted_variant,
)


def _json_body(request):
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GearBuilderError('请求内容不是有效 JSON')
    if not isinstance(body, dict):
        raise GearBuilderError('请求内容必须是 JSON 对象')
    return body


def _error_response(exc, status=400):
    return JsonResponse({'success': False, 'error': str(exc)}, status=status)


class PortalGearBuilderView(View):
    def get(self, request):
        return render(request, 'portal/gear_builder.html')


class PortalGearBuilderBootstrapAPIView(View):
    def get(self, request):
        return JsonResponse({'success': True, **bootstrap_payload()})


class PortalGearBuilderCatalogAPIView(View):
    def get(self, request):
        try:
            payload = catalog_items(
                class_name=request.GET.get('class') or 'Warrior',
                spec_name=request.GET.get('spec') or 'Fury',
                slot=request.GET.get('slot') or 'head',
                source_type=request.GET.get('source') or 'all',
                query=request.GET.get('q') or '',
                page=request.GET.get('page') or 1,
                page_size=request.GET.get('page_size') or 60,
            )
        except GearBuilderError as exc:
            return _error_response(exc)
        return JsonResponse({'success': True, **payload})


class PortalGearBuilderEnhancementsAPIView(View):
    def get(self, request):
        try:
            payload = enhancement_items(
                class_name=request.GET.get('class') or 'Warrior',
                spec_name=request.GET.get('spec') or 'Fury',
                slot=request.GET.get('slot') or 'head',
                equipment_variant_id=request.GET.get('variant_id') or None,
            )
        except GearBuilderError as exc:
            return _error_response(exc)
        return JsonResponse({'success': True, **payload})


class PortalGearBuilderCraftedResolveAPIView(View):
    def post(self, request):
        try:
            body = _json_body(request)
            payload = resolve_crafted_variant(
                variant_id=body.get('variant_id'),
                selected_stats=body.get('selected_stats') or [],
                embellishment_variant_id=body.get('embellishment_variant_id') or None,
                class_name=body.get('class_name') or 'Warrior',
                spec_name=body.get('spec_name') or 'Fury',
            )
        except GearBuilderError as exc:
            return _error_response(exc)
        return JsonResponse({'success': True, **payload})


class PortalGearBuilderSimcImportAPIView(View):
    def post(self, request):
        try:
            body = _json_body(request)
            payload = import_simc_profile(body.get('profile') or '')
        except GearBuilderError as exc:
            return _error_response(exc)
        except Exception:
            return _error_response('SimC Profile 解析失败，请确认内容来自当前版本 SimulationCraft。')
        return JsonResponse({'success': True, **payload})
