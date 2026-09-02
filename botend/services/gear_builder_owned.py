"""职业配装器已有装备的账号级存储服务。"""

from __future__ import annotations

import hashlib
import json

from django.db import transaction

from botend.models import GearBuilderOwnedItem, WowItemVariantSnapshot
from botend.services.gear_builder import (
    GearBuilderError,
    SLOT_LABELS,
    active_season,
    serialize_item,
    serialize_variant,
)


def _fingerprint(payload):
    identity = {
        'variant_id': int(payload.get('variant_id') or 0),
        'item_id': int(payload.get('item_id') or 0),
        'slot': str(payload.get('slot') or ''),
        'item_level': int(payload.get('item_level') or 0),
        'bonus_ids': sorted(str(value) for value in (payload.get('bonus_ids') or [])),
        'selected_stats': sorted(str(value) for value in (payload.get('selected_stats') or payload.get('crafted_stats') or [])),
        'enhancements': payload.get('enhancements') or {},
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _snapshot_from_payload(payload, variant=None):
    if variant:
        item = serialize_item(variant.item, [variant])
        return {
            'item': item,
            'variant': serialize_variant(variant),
            'name': item['name'],
            'stats': serialize_variant(variant).get('stats') or {},
            'sources': serialize_variant(variant).get('sources') or [],
        }
    supplied = payload.get('snapshot') if isinstance(payload.get('snapshot'), dict) else {}
    return {
        'name': str(supplied.get('name') or payload.get('name') or f"物品 #{int(payload.get('item_id') or 0)}")[:255],
        'stats': supplied.get('stats') if isinstance(supplied.get('stats'), dict) else {},
        'sources': supplied.get('sources') if isinstance(supplied.get('sources'), list) else [],
        'external': True,
    }


def serialize_owned_item(row, class_name='', spec_name=''):
    variant = row.variant
    snapshot = row.snapshot_json if isinstance(row.snapshot_json, dict) else {}
    item_payload = None
    variant_payload = None
    if variant:
        item_payload = serialize_item(variant.item, [variant], class_name, spec_name)
        variant_payload = serialize_variant(variant, class_name, spec_name)
    elif isinstance(snapshot.get('item'), dict):
        item_payload = snapshot['item']
        variant_payload = snapshot.get('variant')
    return {
        'id': row.id,
        'item_id': int(row.item_id or 0),
        'slot': row.slot_key,
        'slot_label': SLOT_LABELS.get(row.slot_key, row.slot_key),
        'item_level': int(row.item_level or 0),
        'batch_key': row.batch_key,
        'source': row.source,
        'quantity': int(row.quantity or 1),
        'bonus_ids': row.bonus_ids or [],
        'selected_stats': row.selected_stats or [],
        'enhancements': row.enhancements_json or {},
        'snapshot': snapshot,
        'item': item_payload,
        'variant': variant_payload,
        'name': (item_payload or {}).get('name') or snapshot.get('name') or f'物品 #{row.item_id}',
        'external': not bool(variant),
        'updated_at': row.updated_at.isoformat(),
    }


def list_owned_items(user, *, class_name='', spec_name='', slot=''):
    rows = GearBuilderOwnedItem.objects.filter(user=user).select_related('variant__item')
    if slot:
        rows = rows.filter(slot_key=slot)
    return [serialize_owned_item(row, class_name, spec_name) for row in rows]


@transaction.atomic
def save_owned_item(user, payload, *, set_quantity=False):
    if not isinstance(payload, dict):
        raise GearBuilderError('已有装备内容无效')
    variant_id = int(payload.get('variant_id') or 0)
    variant = None
    if variant_id:
        variant = WowItemVariantSnapshot.objects.select_related('item').filter(id=variant_id).first()
        if not variant:
            raise GearBuilderError('装备变体不存在或已被清理')
    item_id = int(payload.get('item_id') or (variant.item.item_id if variant else 0))
    slot = str(payload.get('slot') or (variant.item.slot_key if variant else ''))
    if item_id <= 0 or not slot:
        raise GearBuilderError('已有装备缺少物品或槽位')
    source = str(payload.get('source') or GearBuilderOwnedItem.SOURCE_MANUAL)
    allowed_sources = {value for value, _label in GearBuilderOwnedItem.SOURCE_CHOICES}
    if source not in allowed_sources:
        source = GearBuilderOwnedItem.SOURCE_MANUAL
    normalized = {
        **payload,
        'variant_id': variant_id,
        'item_id': item_id,
        'slot': slot,
        'item_level': int(payload.get('item_level') or (variant.item_level if variant else 0)),
    }
    fingerprint = _fingerprint(normalized)
    season = active_season()
    row, created = GearBuilderOwnedItem.objects.get_or_create(
        user=user,
        fingerprint=fingerprint,
        defaults={
            'variant': variant,
            'item_id': item_id,
            'slot_key': slot,
            'item_level': normalized['item_level'],
            'batch_key': variant.batch_key if variant else (season.gear_batch_key if season else ''),
            'source': source,
            'bonus_ids': payload.get('bonus_ids') or (variant.bonus_ids if variant else []),
            'selected_stats': payload.get('selected_stats') or payload.get('crafted_stats') or [],
            'enhancements_json': payload.get('enhancements') or {},
            'snapshot_json': _snapshot_from_payload(normalized, variant),
        },
    )
    if not created:
        requested_quantity = max(1, int(payload.get('quantity') or 1))
        row.quantity = min(99, requested_quantity if set_quantity else int(row.quantity or 1) + requested_quantity)
        row.save(update_fields=['quantity', 'updated_at'])
    return serialize_owned_item(row), created


@transaction.atomic
def save_owned_items(user, rows):
    if not isinstance(rows, list) or len(rows) > 300:
        raise GearBuilderError('SimC 已有装备数量无效')
    grouped = {}
    for payload in rows:
        if not isinstance(payload, dict):
            raise GearBuilderError('已有装备内容无效')
        identity = _fingerprint(payload)
        if identity not in grouped:
            grouped[identity] = {**payload, 'quantity': 0}
        grouped[identity]['quantity'] += max(1, int(payload.get('quantity') or 1))
    saved = []
    for payload in grouped.values():
        item, _created = save_owned_item(user, payload, set_quantity=True)
        saved.append(item)
    return saved


def delete_owned_item(user, owned_id):
    row = GearBuilderOwnedItem.objects.filter(user=user, id=owned_id).first()
    if not row:
        raise GearBuilderError('已有装备不存在')
    row.delete()
