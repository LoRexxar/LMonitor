"""Dashboard 今日魔兽顶层板块显示配置接口。"""

import json

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views import View

from botend.dashboard.permissions import DashboardPermissionRequiredMixin
from botend.models import WowTodayCardSetting, WowTodaySectionSetting, WowTodaySnapshot
from botend.services.wow_today_service import (
    default_public_section_visibility,
    ensure_wow_today_card_settings,
    ensure_wow_today_section_settings,
    public_card_key,
    public_card_preference_key,
    public_section_key,
    wow_today_sections_for_snapshot,
)


def _iso_datetime(value):
    if not value:
        return None
    return timezone.localtime(value).isoformat()


class DashboardWowTodaySectionAPIView(DashboardPermissionRequiredMixin, View):
    """发现并编辑 Today in WoW 板块及每张卡片的显示配置。"""

    dashboard_permission = 'reports.wow-today-settings'
    kind_labels = {
        'lines': '内容列表',
        'token': '数值卡片',
        'timer': '时间卡片',
    }

    @staticmethod
    def _latest_snapshot():
        return (
            WowTodaySnapshot.objects.filter(region='na', game_version='retail')
            .order_by('-snapshot_date', '-fetched_at', '-id')
            .first()
        )

    @staticmethod
    def _snapshot_sections(snapshot):
        if snapshot is None:
            return []
        return wow_today_sections_for_snapshot(snapshot)

    def _response_payload(self, snapshot):
        sections = self._snapshot_sections(snapshot)
        section_settings = {
            row.section_key: row
            for row in ensure_wow_today_section_settings(sections)
        }
        card_settings = {
            (row.section_key, row.card_key): row
            for row in ensure_wow_today_card_settings(sections)
        }
        records = []
        for source_index, section in enumerate(sections):
            section_key = public_section_key(section)
            row = section_settings[section_key]
            modules = section.get('modules') if isinstance(section.get('modules'), list) else []
            cards = []
            for card_source_index, card in enumerate(modules):
                card_key = public_card_key(section_key, card)
                card_row = card_settings[(section_key, card_key)]
                items = card.get('items') if isinstance(card.get('items'), list) else []
                preview_items = [
                    str(item.get('name') or '').strip()
                    for item in items
                    if isinstance(item, dict) and str(item.get('name') or '').strip()
                ][:3]
                kind = str(card.get('kind') or 'lines').strip().lower()
                cards.append({
                    'key': card_key,
                    'preference_key': public_card_preference_key(section_key, card_key),
                    'source_name': card_row.source_name or str(card.get('name') or '').strip(),
                    'display_name': card_row.display_name,
                    'effective_name': card_row.display_name or card_row.source_name or str(card.get('name') or '').strip(),
                    'is_visible': card_row.is_visible,
                    'default_visible': True,
                    'sort_order': card_row.sort_order,
                    'source_index': card_source_index,
                    'kind': kind,
                    'kind_label': self.kind_labels.get(kind, '内容卡片'),
                    'item_count': len(items),
                    'preview_items': preview_items,
                    'source_url': str(card.get('url') or '').strip(),
                    'updated_at': _iso_datetime(card_row.updated_at),
                })
            cards.sort(key=lambda item: (item['sort_order'], item['source_index']))
            records.append({
                'key': section_key,
                'source_name': row.source_name or str(section.get('name') or '').strip(),
                'display_name': row.display_name,
                'effective_name': row.display_name or row.source_name or str(section.get('name') or '').strip(),
                'is_visible': row.is_visible,
                'default_visible': default_public_section_visibility(section),
                'sort_order': row.sort_order,
                'source_index': source_index,
                'card_count': len(cards),
                'visible_card_count': sum(1 for card in cards if card['is_visible']),
                'cards': cards,
                'updated_at': _iso_datetime(row.updated_at),
            })
        records.sort(key=lambda item: (item['sort_order'], item['source_index']))
        all_cards = [card for section in records for card in section['cards']]
        return {
            'success': True,
            'snapshot': None if snapshot is None else {
                'id': snapshot.id,
                'snapshot_date': snapshot.snapshot_date.isoformat(),
                'fetched_at': _iso_datetime(snapshot.fetched_at),
                'expansion_name': snapshot.expansion_name or '当前版本',
                'region_name': '北美',
                'game_version_name': '正式服',
            },
            'records': records,
            'summary': {
                'section_total': len(records),
                'section_visible': sum(1 for item in records if item['is_visible']),
                'card_total': len(all_cards),
                'card_visible': sum(1 for card in all_cards if card['is_visible']),
                'card_effective_visible': sum(
                    1
                    for section in records
                    if section['is_visible']
                    for card in section['cards']
                    if card['is_visible']
                ),
            },
        }

    def get(self, request):
        return JsonResponse(self._response_payload(self._latest_snapshot()))

    def patch(self, request):
        snapshot = self._latest_snapshot()
        if snapshot is None:
            return JsonResponse({'success': False, 'error': '尚无可配置的今日魔兽快照'}, status=409)
        try:
            payload = json.loads(request.body or b'{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            return JsonResponse({'success': False, 'error': '请求内容不是有效 JSON'}, status=400)
        sections_payload = payload.get('sections') if isinstance(payload, dict) else None
        if not isinstance(sections_payload, list) or len(sections_payload) > 100:
            return JsonResponse({'success': False, 'error': 'sections 必须是不超过 100 项的数组'}, status=400)

        source_sections = self._snapshot_sections(snapshot)
        source_by_key = {public_section_key(section): section for section in source_sections}
        request_keys = []
        normalized = []
        for section_index, item in enumerate(sections_payload):
            if not isinstance(item, dict):
                return JsonResponse({'success': False, 'error': f'第 {section_index + 1} 个板块配置无效'}, status=400)
            section_key = str(item.get('key') or '').strip()
            display_name = str(item.get('display_name') or '').strip()
            is_visible = item.get('is_visible')
            cards_payload = item.get('cards')
            if section_key not in source_by_key:
                return JsonResponse({'success': False, 'error': f'未知板块：{section_key or "空 key"}'}, status=400)
            if section_key in request_keys:
                return JsonResponse({'success': False, 'error': f'板块重复：{section_key}'}, status=400)
            if len(display_name) > 150:
                return JsonResponse({'success': False, 'error': f'板块名称不能超过 150 个字符：{section_key}'}, status=400)
            if not isinstance(is_visible, bool):
                return JsonResponse({'success': False, 'error': f'板块显示状态必须是布尔值：{section_key}'}, status=400)
            if not isinstance(cards_payload, list) or len(cards_payload) > 200:
                return JsonResponse({'success': False, 'error': f'板块卡片列表无效：{section_key}'}, status=400)

            source_cards = source_by_key[section_key].get('modules') or []
            source_cards_by_key = {
                public_card_key(section_key, card): card
                for card in source_cards
                if isinstance(card, dict)
            }
            request_card_keys = []
            normalized_cards = []
            for card_index, card_item in enumerate(cards_payload):
                if not isinstance(card_item, dict):
                    return JsonResponse({'success': False, 'error': f'{section_key} 的第 {card_index + 1} 张卡片配置无效'}, status=400)
                card_key = str(card_item.get('key') or '').strip()
                card_display_name = str(card_item.get('display_name') or '').strip()
                card_is_visible = card_item.get('is_visible')
                if card_key not in source_cards_by_key:
                    return JsonResponse({'success': False, 'error': f'未知卡片：{section_key}/{card_key or "空 key"}'}, status=400)
                if card_key in request_card_keys:
                    return JsonResponse({'success': False, 'error': f'卡片重复：{section_key}/{card_key}'}, status=400)
                if len(card_display_name) > 150:
                    return JsonResponse({'success': False, 'error': f'卡片名称不能超过 150 个字符：{section_key}/{card_key}'}, status=400)
                if not isinstance(card_is_visible, bool):
                    return JsonResponse({'success': False, 'error': f'卡片显示状态必须是布尔值：{section_key}/{card_key}'}, status=400)
                request_card_keys.append(card_key)
                normalized_cards.append((card_key, card_display_name, card_is_visible, (card_index + 1) * 10))
            if set(request_card_keys) != set(source_cards_by_key):
                return JsonResponse({'success': False, 'error': f'{section_key} 的卡片列表已变化，请刷新后再保存'}, status=409)

            request_keys.append(section_key)
            normalized.append({
                'key': section_key,
                'display_name': display_name,
                'is_visible': is_visible,
                'sort_order': (section_index + 1) * 10,
                'cards': normalized_cards,
            })
        if set(request_keys) != set(source_by_key):
            return JsonResponse({'success': False, 'error': '板块列表已变化，请刷新后再保存'}, status=409)

        with transaction.atomic():
            ensure_wow_today_section_settings(source_sections)
            ensure_wow_today_card_settings(source_sections)
            locked_sections = {
                row.section_key: row
                for row in WowTodaySectionSetting.objects.select_for_update().filter(section_key__in=request_keys)
            }
            locked_cards = {
                (row.section_key, row.card_key): row
                for row in WowTodayCardSetting.objects.select_for_update().filter(section_key__in=request_keys)
            }
            for section_item in normalized:
                section_key = section_item['key']
                row = locked_sections[section_key]
                row.display_name = section_item['display_name']
                row.is_visible = section_item['is_visible']
                row.sort_order = section_item['sort_order']
                row.save(update_fields=('display_name', 'is_visible', 'sort_order', 'updated_at'))
                for card_key, display_name, is_visible, sort_order in section_item['cards']:
                    card_row = locked_cards[(section_key, card_key)]
                    card_row.display_name = display_name
                    card_row.is_visible = is_visible
                    card_row.sort_order = sort_order
                    card_row.save(update_fields=('display_name', 'is_visible', 'sort_order', 'updated_at'))
        return JsonResponse(self._response_payload(snapshot))
