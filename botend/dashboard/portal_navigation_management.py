"""Portal 站内导航专用管理接口。"""

import json
import re

from django.db import transaction
from django.http import JsonResponse
from django.views import View

from botend.dashboard.permissions import DashboardPermissionRequiredMixin
from botend.models import PortalNavigationGroup, PortalNavigationItem


GROUP_KEY_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')


def _is_internal_url(value):
    return bool(value) and value.startswith('/') and not value.startswith('//') and '\\' not in value


class DashboardPortalNavigationAPIView(DashboardPermissionRequiredMixin, View):
    """按分组和单条入口管理首页导航。"""

    dashboard_permission = 'portal.navigation'

    @staticmethod
    def _payload():
        groups = PortalNavigationGroup.objects.prefetch_related('items').order_by('sort_order', 'id')
        records = []
        for group in groups:
            items = [
                {
                    'id': item.id,
                    'name': item.name,
                    'url': item.url,
                    'desc': item.desc,
                    'icon_key': item.icon_key,
                    'badge': item.badge,
                    'badge_tone': item.badge_tone,
                    'sort_order': item.sort_order,
                    'is_active': item.is_active,
                }
                for item in sorted(group.items.all(), key=lambda value: (value.sort_order, value.id))
            ]
            records.append({
                'id': group.id,
                'key': group.key,
                'name': group.name,
                'description': group.description,
                'icon_key': group.icon_key,
                'sort_order': group.sort_order,
                'items': items,
            })
        all_items = [item for group in records for item in group['items']]
        return {
            'success': True,
            'records': records,
            'summary': {
                'group_total': len(records),
                'item_total': len(all_items),
                'item_active': sum(1 for item in all_items if item['is_active']),
            },
        }

    def get(self, request):
        return JsonResponse(self._payload())

    def patch(self, request):
        try:
            payload = json.loads(request.body or b'{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            return JsonResponse({'success': False, 'error': '请求内容不是有效 JSON'}, status=400)
        groups = payload.get('groups') if isinstance(payload, dict) else None
        if not isinstance(groups, list) or len(groups) > 30:
            return JsonResponse({'success': False, 'error': 'groups 必须是不超过 30 项的数组'}, status=400)

        normalized = []
        group_ids = set()
        group_keys = set()
        item_ids = set()
        for group_index, raw_group in enumerate(groups):
            if not isinstance(raw_group, dict):
                return JsonResponse({'success': False, 'error': f'第 {group_index + 1} 个分组无效'}, status=400)
            group_id = raw_group.get('id')
            if group_id is not None and (not isinstance(group_id, int) or group_id <= 0 or group_id in group_ids):
                return JsonResponse({'success': False, 'error': f'第 {group_index + 1} 个分组 ID 无效或重复'}, status=400)
            key = str(raw_group.get('key') or '').strip().lower()
            name = str(raw_group.get('name') or '').strip()
            description = str(raw_group.get('description') or '').strip()
            icon_key = str(raw_group.get('icon_key') or '').strip()
            raw_items = raw_group.get('items')
            if not GROUP_KEY_PATTERN.fullmatch(key) or key in group_keys:
                return JsonResponse({'success': False, 'error': f'分组标识无效或重复：{key or "空值"}'}, status=400)
            if not name or len(name) > 100 or len(description) > 300 or len(icon_key) > 48:
                return JsonResponse({'success': False, 'error': f'分组“{name or key}”的名称或说明长度无效'}, status=400)
            if not isinstance(raw_items, list) or len(raw_items) > 120:
                return JsonResponse({'success': False, 'error': f'分组“{name}”的入口列表无效'}, status=400)

            normalized_items = []
            for item_index, raw_item in enumerate(raw_items):
                if not isinstance(raw_item, dict):
                    return JsonResponse({'success': False, 'error': f'“{name}”的第 {item_index + 1} 个入口无效'}, status=400)
                item_id = raw_item.get('id')
                if item_id is not None and (not isinstance(item_id, int) or item_id <= 0 or item_id in item_ids):
                    return JsonResponse({'success': False, 'error': f'“{name}”存在无效或重复的入口 ID'}, status=400)
                item_name = str(raw_item.get('name') or '').strip()
                url = str(raw_item.get('url') or '').strip()
                desc = str(raw_item.get('desc') or '').strip()
                item_icon = str(raw_item.get('icon_key') or '').strip()
                badge = str(raw_item.get('badge') or '').strip()
                badge_tone = str(raw_item.get('badge_tone') or 'default').strip()
                if not item_name or len(item_name) > 200:
                    return JsonResponse({'success': False, 'error': f'“{name}”第 {item_index + 1} 个入口名称无效'}, status=400)
                if not _is_internal_url(url) or len(url) > 1000:
                    return JsonResponse({'success': False, 'error': f'“{item_name}”只能填写以 / 开头的站内地址'}, status=400)
                if len(desc) > 500 or len(item_icon) > 48 or len(badge) > 32 or badge_tone not in {'default', 'new'}:
                    return JsonResponse({'success': False, 'error': f'“{item_name}”的说明、图标或徽标无效'}, status=400)
                if not isinstance(raw_item.get('is_active'), bool):
                    return JsonResponse({'success': False, 'error': f'“{item_name}”的显示状态无效'}, status=400)
                if item_id is not None:
                    item_ids.add(item_id)
                normalized_items.append({
                    'id': item_id,
                    'name': item_name,
                    'url': url,
                    'desc': desc,
                    'icon_key': item_icon,
                    'badge': badge,
                    'badge_tone': badge_tone,
                    'sort_order': (item_index + 1) * 10,
                    'is_active': raw_item['is_active'],
                })
            if group_id is not None:
                group_ids.add(group_id)
            group_keys.add(key)
            normalized.append({
                'id': group_id,
                'key': key,
                'name': name,
                'description': description,
                'icon_key': icon_key or 'globe',
                'sort_order': (group_index + 1) * 10,
                'items': normalized_items,
            })

        with transaction.atomic():
            existing_groups = {row.id: row for row in PortalNavigationGroup.objects.select_for_update()}
            existing_items = {row.id: row for row in PortalNavigationItem.objects.select_for_update()}
            if not group_ids.issubset(existing_groups) or not item_ids.issubset(existing_items):
                return JsonResponse({'success': False, 'error': '导航数据已变化，请刷新后再保存'}, status=409)
            retained_group_ids = set()
            retained_item_ids = set()
            for group_data in normalized:
                items_data = group_data.pop('items')
                group_id = group_data.pop('id')
                if group_id is None:
                    group = PortalNavigationGroup.objects.create(**group_data)
                else:
                    group = existing_groups[group_id]
                    for field, value in group_data.items():
                        setattr(group, field, value)
                    group.save()
                retained_group_ids.add(group.id)
                for item_data in items_data:
                    item_id = item_data.pop('id')
                    if item_id is None:
                        item = PortalNavigationItem.objects.create(group=group, **item_data)
                    else:
                        item = existing_items[item_id]
                        item.group = group
                        for field, value in item_data.items():
                            setattr(item, field, value)
                        item.save()
                    retained_item_ids.add(item.id)
            PortalNavigationItem.objects.exclude(id__in=retained_item_ids).delete()
            PortalNavigationGroup.objects.exclude(id__in=retained_group_ids).delete()
        return JsonResponse(self._payload())
