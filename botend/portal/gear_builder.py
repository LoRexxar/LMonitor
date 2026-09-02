"""Portal 职业配装器页面与只读 API。"""

from __future__ import annotations

import json

from django.http import JsonResponse
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import ensure_csrf_cookie

from botend.services.gear_builder import (
    GearBuilderError,
    bootstrap_payload,
    catalog_items,
    enhancement_items,
    hydrate_shared_state,
    import_simc_profile,
    resolve_crafted_variant,
)
from botend.services.gear_builder_storage import (
    GearBuilderStorageError,
    create_short_link,
    delete_user_loadout,
    get_user_loadout,
    list_user_loadouts,
    resolve_short_link,
    save_user_loadout,
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


@method_decorator(ensure_csrf_cookie, name='dispatch')
class PortalGearBuilderView(View):
    def get(self, request, share_token=''):
        return render(request, 'portal/gear_builder.html', {
            'initial_share_token': str(share_token or ''),
        })


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
                excluded_sources=request.GET.getlist('exclude_sources'),
                excluded_stats=request.GET.getlist('exclude_stats'),
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


class PortalGearBuilderShareResolveAPIView(View):
    def post(self, request):
        try:
            body = _json_body(request)
            payload = hydrate_shared_state(
                share_version=body.get('v'),
                class_name=body.get('c') or 'Warrior',
                spec_name=body.get('s') or 'Fury',
                batch_key=body.get('b') or '',
                entries=body.get('e') or [],
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


class PortalGearBuilderOnlineLoadoutAPIView(View):
    def _unauthorized(self):
        return _error_response('请登录后使用线上配装', status=401)

    def get(self, request, loadout_id=None):
        if not request.user.is_authenticated:
            return self._unauthorized()
        try:
            if loadout_id is None:
                return JsonResponse({'success': True, 'loadouts': list_user_loadouts(request.user)})
            return JsonResponse({'success': True, 'loadout': get_user_loadout(request.user, loadout_id)})
        except GearBuilderStorageError as exc:
            return _error_response(exc, status=404)

    def post(self, request, loadout_id=None):
        if not request.user.is_authenticated:
            return self._unauthorized()
        try:
            body = _json_body(request)
            payload = save_user_loadout(
                request.user,
                name=body.get('name'),
                code=body.get('code'),
                loadout_id=loadout_id or body.get('id'),
            )
        except (GearBuilderError, GearBuilderStorageError) as exc:
            return _error_response(exc)
        return JsonResponse({'success': True, 'loadout': payload})

    def delete(self, request, loadout_id=None):
        if not request.user.is_authenticated:
            return self._unauthorized()
        try:
            if loadout_id is None:
                raise GearBuilderStorageError('缺少线上配装 ID')
            delete_user_loadout(request.user, loadout_id)
        except GearBuilderStorageError as exc:
            return _error_response(exc, status=404)
        return JsonResponse({'success': True})


class PortalGearBuilderShortLinkAPIView(View):
    def post(self, request):
        if not request.user.is_authenticated:
            return _error_response('请登录后创建配装短链接', status=401)
        try:
            body = _json_body(request)
            row = create_short_link(request.user, body.get('code'))
        except (GearBuilderError, GearBuilderStorageError) as exc:
            return _error_response(exc)
        return JsonResponse({'success': True, 'token': row.token})


class PortalGearBuilderShortLinkDetailAPIView(View):
    def get(self, request, share_token):
        try:
            payload = resolve_short_link(share_token)
        except GearBuilderStorageError as exc:
            return _error_response(exc, status=404)
        return JsonResponse({'success': True, **payload})
