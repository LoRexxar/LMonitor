"""中央 WoW 物品目录制品的校验、合并与完整性判定。"""
from copy import deepcopy
import re

from django.utils import timezone

from botend.models import WowItemSnapshot, WowItemVariantSnapshot


INVENTORY_SLOTS = {
    1: 'head', 2: 'neck', 3: 'shoulders', 5: 'chest', 6: 'waist',
    7: 'legs', 8: 'feet', 9: 'wrists', 10: 'hands', 11: 'finger',
    12: 'trinket', 13: 'main_hand', 14: 'off_hand', 15: 'main_hand',
    16: 'back', 17: 'main_hand', 20: 'chest', 21: 'main_hand',
    22: 'off_hand', 23: 'off_hand', 25: 'main_hand', 26: 'main_hand',
}


def upsert_journal_base_item_facts(rows, build):
    """把手册 DB2 已验证的缺失基础事实补入中央物品表，不创建装备变体。"""
    loot_by_id = {}
    for instance in rows:
        for encounter in instance.get('encounters') or []:
            for loot in encounter.get('loot') or []:
                loot_by_id.setdefault(int(loot['item_id']), loot)
    existing_by_id = {
        int(item.item_id): item
        for item in WowItemSnapshot.objects.filter(item_id__in=loot_by_id)
    }
    for item_id, loot in loot_by_id.items():
        existing = existing_by_id.get(item_id)
        metadata = deepcopy(existing.metadata if existing else {})
        metadata['journal_builds'] = sorted({
            *[str(value) for value in metadata.get('journal_builds') or [] if value],
            str(build),
        })
        journal_item = deepcopy(metadata.get('journal_item') or {})
        for key in ('icon', 'required_level', 'bonding', 'stat_types'):
            value = loot.get(key)
            if value not in (None, '', [], 0):
                journal_item[key] = deepcopy(value)
        metadata['journal_item'] = journal_item
        inventory_type = int(loot.get('slot') or 0)
        item_class_id = int(loot.get('class_id') or 0)
        is_localized = loot.get('source') == 'wago'
        defaults = {
            'name': (existing.name if existing else '') or ('' if is_localized else loot.get('name') or ''),
            'name_zh': (existing.name_zh if existing else '') or (loot.get('name') or '' if is_localized else ''),
            'description': (existing.description if existing else '') or ('' if is_localized else loot.get('description') or ''),
            'description_zh': (existing.description_zh if existing else '') or (loot.get('description') or '' if is_localized else ''),
            'icon': existing.icon if existing else '',
            'quality': (existing.quality if existing and existing.quality else int(loot.get('quality') or 0)),
            'source': (existing.source if existing and existing.source else 'wago-db2-journal'),
            'catalog_type': (
                existing.catalog_type if existing and existing.catalog_type
                else 'equipment' if inventory_type and item_class_id in {2, 4} else 'misc'
            ),
            'inventory_type': (existing.inventory_type if existing and existing.inventory_type else inventory_type),
            'slot_key': (existing.slot_key if existing and existing.slot_key else INVENTORY_SLOTS.get(inventory_type, '')),
            'item_class_id': (existing.item_class_id if existing and existing.item_class_id else item_class_id),
            'item_subclass_id': (
                existing.item_subclass_id if existing and existing.item_subclass_id else int(loot.get('subclass_id') or 0)
            ),
            'armor_type': existing.armor_type if existing else '',
            'weapon_type': existing.weapon_type if existing else '',
            'allowable_class_mask': (
                existing.allowable_class_mask if existing and existing.allowable_class_mask
                else int(loot.get('class_mask') if loot.get('class_mask') is not None else -1)
            ),
            'eligible_specs': deepcopy(existing.eligible_specs if existing else []),
            'unique_group': existing.unique_group if existing else '',
            'effect_refs': deepcopy(existing.effect_refs if existing else []),
            'simc_token': existing.simc_token if existing else '',
            'metadata': metadata,
            'updated_at': timezone.now(),
        }
        if existing:
            changed = []
            for field, value in defaults.items():
                if field == 'updated_at':
                    continue
                if getattr(existing, field) != value:
                    setattr(existing, field, value)
                    changed.append(field)
            if changed:
                existing.updated_at = defaults['updated_at']
                existing.save(update_fields=[*changed, 'updated_at'])
        else:
            WowItemSnapshot.objects.create(item_id=item_id, **defaults)
    return len(loot_by_id)


def exact_build_variant_payload_is_complete(*, build, metadata, stats, effects):
    """校验 PTR 制品中的属性与特效都来自声明的精确构建。"""
    metadata = metadata if isinstance(metadata, dict) else {}
    if (
        not metadata.get('ptr_preview')
        or metadata.get('stats_status') != 'exact_build_simc'
        or metadata.get('effects_status') != 'exact_build_db2_simc'
        or str(metadata.get('game_build') or '') != str(build or '')
        or not isinstance(stats, dict) or not stats
        or not isinstance(effects, list) or not effects
    ):
        return False
    return all(
        isinstance(effect, dict)
        and str(effect.get('game_build') or '') == str(build or '')
        and not effect.get('unresolved_tokens')
        and bool(str(effect.get('description_zh') or effect.get('description') or '').strip())
        for effect in effects
    )


def validate_item_catalog_payload(items, build):
    """校验中央目录增量，不依赖任何展示渠道。"""
    item_ids = [int(item.get('item_id') or 0) for item in items]
    if not items or 0 in item_ids or len(item_ids) != len(set(item_ids)):
        raise ValueError('物品目录含有无效或重复的物品 ID')
    for item in items:
        item_id = int(item['item_id'])
        icon = str(item.get('icon') or '').strip()
        if not re.fullmatch(r'[a-z0-9_]+', icon):
            raise ValueError(f'物品 {item_id} 的图标名不安全')
        if item.get('name_zh') and not any('\u3400' <= char <= '\u9fff' for char in item['name_zh']):
            raise ValueError(f'物品 {item_id} 的中文名不含中文字符')
        variants = item.get('variants') or []
        variant_keys = [str(variant.get('key') or '').strip() for variant in variants]
        if not variants or '' in variant_keys or len(variant_keys) != len(set(variant_keys)):
            raise ValueError(f'物品 {item_id} 含有无效或重复的变体身份')
        for variant in variants:
            if not exact_build_variant_payload_is_complete(
                build=build,
                metadata=variant.get('metadata'),
                stats=variant.get('stats'),
                effects=variant.get('effects'),
            ):
                raise ValueError(f'物品 {item_id} 缺少同构建 SimC 属性或特效')


def _item_defaults(item, existing=None):
    metadata = deepcopy(existing.metadata if existing else {})
    metadata.update(deepcopy(item.get('metadata') or {}))
    return {
        'name': item.get('name') or (existing.name if existing else ''),
        'name_zh': item.get('name_zh') or (existing.name_zh if existing else ''),
        'description': item.get('description') or (existing.description if existing else ''),
        'description_zh': item.get('description_zh') or (existing.description_zh if existing else ''),
        'icon': item.get('icon') or (existing.icon if existing else ''),
        'quality': int(item.get('quality') or 0),
        'source': item.get('source') or 'wago-ptr-db2',
        'catalog_type': item.get('catalog_type') or 'equipment',
        'inventory_type': int(item.get('inventory_type') or 0),
        'slot_key': item.get('slot_key') or '',
        'item_class_id': int(item.get('item_class_id') or 0),
        'item_subclass_id': int(item.get('item_subclass_id') or 0),
        'armor_type': item.get('armor_type') or '',
        'weapon_type': item.get('weapon_type') or '',
        'allowable_class_mask': int(item.get('allowable_class_mask') or 0),
        'eligible_specs': deepcopy(item.get('eligible_specs') or []),
        'unique_group': item.get('unique_group') or '',
        'effect_refs': deepcopy(item.get('effect_refs') or []),
        'simc_token': item.get('simc_token') or '',
        'metadata': metadata,
        'updated_at': timezone.now(),
    }


def upsert_item_catalog(items, season, build):
    """仅向活动中央目录追加/更新制品声明的物品事实。"""
    variant_count = 0
    for item_data in items:
        item_id = int(item_data['item_id'])
        existing = WowItemSnapshot.objects.filter(item_id=item_id).first()
        item, _ = WowItemSnapshot.objects.update_or_create(
            item_id=item_id,
            defaults=_item_defaults(item_data, existing),
        )
        for variant in item_data['variants']:
            WowItemVariantSnapshot.objects.update_or_create(
                season=season,
                batch_key=season.gear_batch_key,
                item=item,
                variant_key=variant['key'],
                defaults={
                    'game_build': build,
                    'variant_type': variant.get('type') or WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
                    'item_level': int(variant.get('item_level') or 0),
                    'upgrade_track': variant.get('upgrade_track') or '',
                    'track_rank': int(variant.get('track_rank') or 0),
                    'track_max_rank': int(variant.get('track_max_rank') or 0),
                    'bonus_ids': deepcopy(variant.get('bonus_ids') or []),
                    'compatible_slots': deepcopy(variant.get('compatible_slots') or []),
                    'socket_types': deepcopy(variant.get('socket_types') or []),
                    'socket_count': int(variant.get('socket_count') or 0),
                    'stats_json': deepcopy(variant.get('stats') or {}),
                    'effects_json': deepcopy(variant.get('effects') or []),
                    'source_json': deepcopy(variant.get('sources') or []),
                    'metadata': deepcopy(variant.get('metadata') or {}),
                },
            )
            variant_count += 1
    return variant_count


def item_catalog_is_complete(items, season, build):
    expected = {
        (int(item['item_id']), str(variant['key']))
        for item in items for variant in item['variants']
    }
    rows = WowItemVariantSnapshot.objects.filter(
        season=season,
        batch_key=season.gear_batch_key,
        item__item_id__in=[int(item['item_id']) for item in items],
    ).values_list(
        'item__item_id', 'variant_key', 'game_build', 'metadata', 'stats_json', 'effects_json',
    )
    actual = {}
    for item_id, variant_key, game_build, metadata, stats, effects in rows:
        if exact_build_variant_payload_is_complete(
            build=build, metadata=metadata, stats=stats, effects=effects,
        ):
            actual[(int(item_id), str(variant_key))] = str(game_build or '')
    return expected == set(actual) and all(actual[key] == build for key in expected)
