"""职业配装器目录查询、制造解析与 SimC 映射服务。"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from math import floor

from django.db.models import Q

from botend.constants.wow import (
    CLASS_CN,
    CLASS_SPEC_MAP,
    SPEC_CN,
    SPEC_ICON,
    SPEC_ROLE,
    canonical_class_spec,
    localize_gear_source,
)
from botend.models import SeasonMeta, WowItemVariantSnapshot, WowWagoMonitorState
from botend.services.simc_player_config import parse_simc_player_profile
from botend.templatetags.wow_tags import wow_icon_oss_url


EQUIPMENT_SLOTS = (
    ('head', '头部'),
    ('neck', '颈部'),
    ('shoulders', '肩部'),
    ('back', '背部'),
    ('chest', '胸部'),
    ('wrists', '腕部'),
    ('hands', '手部'),
    ('waist', '腰部'),
    ('legs', '腿部'),
    ('feet', '脚部'),
    ('finger1', '戒指1'),
    ('finger2', '戒指2'),
    ('trinket1', '饰品1'),
    ('trinket2', '饰品2'),
    ('main_hand', '主手'),
    ('off_hand', '副手'),
)
SLOT_LABELS = dict(EQUIPMENT_SLOTS)
SLOT_FAMILIES = {
    'finger1': 'finger', 'finger2': 'finger',
    'trinket1': 'trinket', 'trinket2': 'trinket',
    'main_hand': 'weapon', 'off_hand': 'weapon',
}
STAT_KEYS = (
    'strength', 'agility', 'intellect', 'stamina', 'armor', 'bonus_armor',
    'crit', 'haste', 'mastery', 'versatility', 'leech', 'avoidance', 'speed',
    'weapon_dps', 'min_damage', 'max_damage',
)
STAT_LABELS = {
    'strength': '力量', 'agility': '敏捷', 'intellect': '智力', 'stamina': '耐力',
    'armor': '护甲', 'bonus_armor': '额外护甲', 'crit': '暴击', 'haste': '急速',
    'mastery': '精通', 'versatility': '全能', 'leech': '吸血',
    'avoidance': '闪避', 'speed': '速度', 'weapon_dps': '武器秒伤',
    'min_damage': '最低伤害', 'max_damage': '最高伤害',
}
UPGRADE_TRACK_LABELS = {'champion': '勇士', 'hero': '英雄', 'myth': '神话'}
CLASS_MASKS = {
    'warrior': 1, 'paladin': 2, 'hunter': 4, 'rogue': 8, 'priest': 16,
    'deathknight': 32, 'shaman': 64, 'mage': 128, 'warlock': 256,
    'monk': 512, 'druid': 1024, 'demonhunter': 2048, 'evoker': 4096,
}
INTELLECT_SPECS = {
    'Paladin:Holy', 'Priest:Discipline', 'Priest:Holy', 'Priest:Shadow',
    'Shaman:Elemental', 'Shaman:Restoration', 'Mage:Arcane', 'Mage:Fire', 'Mage:Frost',
    'Warlock:Affliction', 'Warlock:Demonology', 'Warlock:Destruction',
    'Monk:Mistweaver', 'Druid:Balance', 'Druid:Restoration',
    'Evoker:Devastation', 'Evoker:Preservation', 'Evoker:Augmentation',
}
AGILITY_CLASSES = {'Hunter', 'Rogue', 'DemonHunter'}
AGILITY_SPECS = {
    'Shaman:Enhancement', 'Monk:Brewmaster', 'Monk:Windwalker',
    'Druid:Feral', 'Druid:Guardian',
}
PRIMARY_STAT_KEYS = {'strength', 'agility', 'intellect'}
PRIMARY_STATS_BY_ITEM_MOD = {
    3: {'agility'}, 4: {'strength'}, 5: {'intellect'},
    71: {'strength', 'agility', 'intellect'},
    72: {'strength', 'agility'}, 73: {'agility', 'intellect'}, 74: {'strength', 'intellect'},
}
ARMOR_SUBCLASS_BY_CLASS = {
    'Mage': 1, 'Priest': 1, 'Warlock': 1,
    'DemonHunter': 2, 'Druid': 2, 'Monk': 2, 'Rogue': 2,
    'Evoker': 3, 'Hunter': 3, 'Shaman': 3,
    'DeathKnight': 4, 'Paladin': 4, 'Warrior': 4,
}
PRIMARY_ARMOR_INVENTORY_TYPES = {1, 3, 5, 6, 7, 8, 9, 10, 20}
WEAPON_SUBCLASSES_BY_CLASS = {
    'DeathKnight': {0, 1, 4, 5, 6, 7, 8},
    'DemonHunter': {0, 7, 9, 13, 15},
    'Druid': {4, 5, 6, 10, 13, 15},
    'Evoker': {4, 7, 10, 15},
    'Hunter': {0, 1, 2, 3, 6, 7, 8, 10, 13, 18},
    'Mage': {7, 10, 15, 19},
    'Monk': {0, 4, 6, 7, 10, 13},
    'Paladin': {0, 1, 4, 5, 6, 7, 8},
    'Priest': {4, 10, 15, 19},
    'Rogue': {0, 4, 7, 13, 15},
    'Shaman': {0, 4, 10, 13, 15},
    'Warlock': {7, 10, 15, 19},
    'Warrior': {0, 1, 4, 5, 6, 7, 8, 13, 15},
}
SPEC_WEAPON_INVENTORY_TYPES = {
    'Warrior:Arms': {17}, 'Warrior:Fury': {13, 17, 21}, 'Warrior:Protection': {13, 21},
    'Paladin:Holy': {13, 17, 21}, 'Paladin:Protection': {13, 21}, 'Paladin:Retribution': {17},
    'DeathKnight:Blood': {17}, 'DeathKnight:Frost': {13, 17, 21}, 'DeathKnight:Unholy': {17},
    'Hunter:BeastMastery': {15, 26}, 'Hunter:Marksmanship': {15, 26}, 'Hunter:Survival': {17},
    'Rogue:Assassination': {13, 21}, 'Rogue:Outlaw': {13, 21}, 'Rogue:Subtlety': {13, 21},
    'DemonHunter:Havoc': {13, 21}, 'DemonHunter:Vengeance': {13, 21}, 'DemonHunter:Devourer': {13, 21},
    'Shaman:Enhancement': {13, 21}, 'Shaman:Elemental': {13, 17, 21}, 'Shaman:Restoration': {13, 17, 21},
    'Monk:Brewmaster': {13, 17, 21}, 'Monk:Windwalker': {13, 17, 21}, 'Monk:Mistweaver': {13, 17, 21},
    'Druid:Balance': {13, 17, 21}, 'Druid:Feral': {17}, 'Druid:Guardian': {17}, 'Druid:Restoration': {13, 17, 21},
    'Mage:Arcane': {13, 17, 21, 26}, 'Mage:Fire': {13, 17, 21, 26}, 'Mage:Frost': {13, 17, 21, 26},
    'Priest:Discipline': {13, 17, 21, 26}, 'Priest:Holy': {13, 17, 21, 26}, 'Priest:Shadow': {13, 17, 21, 26},
    'Warlock:Affliction': {13, 17, 21, 26}, 'Warlock:Demonology': {13, 17, 21, 26}, 'Warlock:Destruction': {13, 17, 21, 26},
    'Evoker:Devastation': {13, 17, 21}, 'Evoker:Preservation': {13, 17, 21}, 'Evoker:Augmentation': {13, 17, 21},
}
DUAL_WIELD_SPECS = {
    'Warrior:Fury', 'DeathKnight:Frost', 'Rogue:Assassination', 'Rogue:Outlaw', 'Rogue:Subtlety',
    'DemonHunter:Havoc', 'DemonHunter:Vengeance', 'DemonHunter:Devourer', 'Shaman:Enhancement',
    'Monk:Brewmaster', 'Monk:Windwalker',
}
SHIELD_SPECS = {'Warrior:Protection', 'Paladin:Holy', 'Paladin:Protection', 'Shaman:Elemental', 'Shaman:Restoration'}
HELD_OFFHAND_SPECS = INTELLECT_SPECS - {'Paladin:Holy', 'Shaman:Elemental', 'Shaman:Restoration'}


class GearBuilderError(ValueError):
    pass


def _number(value, default=0):
    try:
        parsed = float(value)
        return int(parsed) if parsed.is_integer() else parsed
    except (TypeError, ValueError):
        return default


def normalize_stats(raw):
    """将导入器允许的扁平或分组属性统一成前端可累加结构。"""
    raw = raw if isinstance(raw, dict) else {}
    result = OrderedDict()
    for key in STAT_KEYS:
        if key in raw:
            result[key] = _number(raw.get(key))
    for group_name in ('primary', 'secondary', 'tertiary', 'weapon'):
        group = raw.get(group_name)
        if not isinstance(group, dict):
            continue
        for key, value in group.items():
            if key not in STAT_KEYS:
                continue
            if isinstance(value, dict):
                value = value.get('rating', value.get('value', 0))
            result[key] = _number(value)
    return {key: value for key, value in result.items() if value}


def stats_for_identity(raw, metadata, class_name='', spec_name=''):
    """把可随职业变化的主属性映射到当前职业专精，避免同时累计三种主属性。"""
    stats = normalize_stats(raw)
    identity = f'{class_name}:{spec_name}'
    if identity in INTELLECT_SPECS:
        primary = 'intellect'
    elif class_name in AGILITY_CLASSES or identity in AGILITY_SPECS:
        primary = 'agility'
    else:
        primary = 'strength'
    for key in PRIMARY_STAT_KEYS - {primary}:
        stats.pop(key, None)
    metadata = metadata if isinstance(metadata, dict) else {}
    values = metadata.get('primary_stat_values') if isinstance(metadata.get('primary_stat_values'), dict) else {}
    amount = values.get(primary) or metadata.get('primary_stat_amount') or 0
    if amount:
        stats[primary] = _number(amount)
    return stats


def active_season():
    return SeasonMeta.objects.filter(is_active=True).order_by('-updated_at', '-id').first()


def catalog_context(season=None):
    season = season or active_season()
    monitor = WowWagoMonitorState.objects.filter(branch='wow', locale='enUS', is_active=True).first()
    if not season:
        return {
            'available': False, 'season': None, 'batch_key': '',
            'game_build': str(getattr(monitor, 'build', '') or ''),
            'sync_status': 'missing_season', 'synced_at': None, 'sync_report': {},
        }
    return {
        'available': bool(season.gear_batch_key),
        'season': {
            'id': season.id,
            'key': season.season_key,
            'name': season.season_name,
        },
        'batch_key': season.gear_batch_key,
        'game_build': season.game_build or str(getattr(monitor, 'build', '') or ''),
        'sync_status': season.gear_sync_status or ('ready' if season.gear_batch_key else 'not_synced'),
        'synced_at': season.gear_synced_at.isoformat() if season.gear_synced_at else None,
        'sync_report': season.gear_sync_report or {},
    }


def specs_payload():
    payload = []
    for class_name, spec_names in CLASS_SPEC_MAP.items():
        payload.append({
            'key': class_name,
            'name': CLASS_CN.get(class_name, class_name),
            'specs': [{
                'key': spec_name,
                'name': SPEC_CN.get(spec_name, spec_name),
                'role': SPEC_ROLE.get((class_name, spec_name), 'dps'),
                'icon': SPEC_ICON.get((class_name, spec_name), ''),
            } for spec_name in spec_names],
        })
    return payload


def bootstrap_payload():
    season = active_season()
    sync_report = season.gear_sync_report if season and isinstance(season.gear_sync_report, dict) else {}
    catalog_rules = sync_report.get('catalog_rules') if isinstance(sync_report.get('catalog_rules'), dict) else {}
    return {
        'catalog': catalog_context(season),
        'classes': specs_payload(),
        'slots': [{'key': key, 'label': label, 'family': SLOT_FAMILIES.get(key, key)} for key, label in EQUIPMENT_SLOTS],
        'stats': [{'key': key, 'label': label} for key, label in STAT_LABELS.items()],
        'upgrade_tracks': [{'key': key, 'label': label} for key, label in UPGRADE_TRACK_LABELS.items()],
        'rules': {
            'state_version': 1,
            'share_version': 1,
            'max_share_length': 8000,
            'crafted_secondary_count': 2,
            'temporary_enchants_supported': False,
            **catalog_rules,
        },
    }


def canonical_spec(class_name, spec_name):
    identity = canonical_class_spec(class_name, spec_name)
    if not identity:
        raise GearBuilderError('未知职业或专精')
    return identity


def slot_matches(variant, slot, class_name='', spec_name=''):
    family = SLOT_FAMILIES.get(slot, slot)
    compatible = [str(value) for value in (variant.compatible_slots or []) if value]
    item_slot = str(variant.item.slot_key or '')
    matched = not compatible or slot in compatible or family in compatible or item_slot in (slot, family)
    if not matched and slot == 'off_hand' and f'{class_name}:{spec_name}' == 'Warrior:Fury':
        matched = int(variant.item.item_class_id or 0) == 2 and int(variant.item.inventory_type or 0) == 17
    return matched


def _expected_primary_stat(class_name, spec_name):
    identity = f'{class_name}:{spec_name}'
    if identity in INTELLECT_SPECS:
        return 'intellect'
    if class_name in AGILITY_CLASSES or identity in AGILITY_SPECS:
        return 'agility'
    return 'strength'


def _item_primary_options(item, variant=None):
    options = set()
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    options.update(str(value) for value in (metadata.get('primary_stat_options') or []) if value)
    for row in metadata.get('raidbots_stats_alloc') or []:
        if isinstance(row, dict):
            options.update(PRIMARY_STATS_BY_ITEM_MOD.get(int(row.get('id') or 0), set()))
    if variant:
        variant_metadata = variant.metadata if isinstance(variant.metadata, dict) else {}
        values = variant_metadata.get('primary_stat_values') if isinstance(variant_metadata.get('primary_stat_values'), dict) else {}
        options.update(key for key, value in values.items() if key in PRIMARY_STAT_KEYS and _number(value))
        options.update(key for key, value in normalize_stats(variant.stats_json).items() if key in PRIMARY_STAT_KEYS and _number(value))
    return options


def spec_matches(item, class_name, spec_name, variant=None, slot=''):
    class_mask = int(item.allowable_class_mask or 0)
    expected_mask = CLASS_MASKS.get(str(class_name or '').casefold(), 0)
    if class_mask > 0 and expected_mask and not class_mask & expected_mask:
        return False
    eligible = [str(value).casefold() for value in (item.eligible_specs or []) if value]
    identity = f'{class_name}:{spec_name}'
    if eligible:
        candidates = {
            identity.casefold(),
            f'{class_name}_{spec_name}'.casefold(),
            spec_name.casefold(),
        }
        if not candidates.intersection(eligible):
            return False

    item_class = int(item.item_class_id or 0)
    subclass = int(item.item_subclass_id or 0)
    inventory_type = int(item.inventory_type or 0)
    if item_class == 4 and inventory_type in PRIMARY_ARMOR_INVENTORY_TYPES:
        if subclass in {1, 2, 3, 4} and subclass != ARMOR_SUBCLASS_BY_CLASS.get(class_name):
            return False
    if item_class == 4 and subclass == 6 and identity not in SHIELD_SPECS:
        return False
    if item_class == 4 and inventory_type == 23 and identity not in HELD_OFFHAND_SPECS:
        return False
    if item_class == 2:
        if subclass not in WEAPON_SUBCLASSES_BY_CLASS.get(class_name, set()):
            return False
        allowed_inventory = SPEC_WEAPON_INVENTORY_TYPES.get(identity)
        if allowed_inventory and inventory_type not in allowed_inventory:
            return False
        if slot == 'off_hand' and inventory_type in {13, 17} and identity not in DUAL_WIELD_SPECS:
            return False

    primary_options = _item_primary_options(item, variant)
    if primary_options and _expected_primary_stat(class_name, spec_name) not in primary_options:
        return False
    return True


def _source_matches(variant, source_type):
    if not source_type or source_type == 'all':
        return True
    return any(str(row.get('type') or '').casefold() == source_type.casefold()
               for row in (variant.source_json or []) if isinstance(row, dict))


def serialize_variant(variant, class_name='', spec_name=''):
    item = variant.item
    metadata = {**(item.metadata or {}), **(variant.metadata or {})}
    metadata.setdefault('two_handed', int(item.inventory_type or 0) == 17)
    return {
        'id': variant.id,
        'key': variant.variant_key,
        'type': variant.variant_type,
        'item_id': item.item_id,
        'item_level': variant.item_level,
        'track': variant.upgrade_track,
        'track_label': UPGRADE_TRACK_LABELS.get(variant.upgrade_track, variant.upgrade_track),
        'track_rank': variant.track_rank,
        'track_max_rank': variant.track_max_rank,
        'crafting_quality': variant.crafting_quality,
        'bonus_ids': variant.bonus_ids or [],
        'compatible_slots': variant.compatible_slots or [],
        'socket_types': variant.socket_types or [],
        'socket_count': variant.socket_count,
        'stats': stats_for_identity(variant.stats_json, variant.metadata, class_name, spec_name),
        'effects': variant.effects_json or [],
        'sources': [localize_gear_source(row) for row in (variant.source_json or []) if isinstance(row, dict)],
        'crafting_options': variant.crafting_options or {},
        'unique_group': variant.unique_group or item.unique_group,
        'max_equipped': variant.max_equipped,
        'is_intrinsic_embellishment': variant.is_intrinsic_embellishment,
        'metadata': metadata,
    }


def serialize_item(item, variants, class_name='', spec_name=''):
    description = item.description_zh or item.description or ''
    return {
        'item_id': item.item_id,
        'name': item.name_zh or item.name or f'物品 #{item.item_id}',
        'name_en': item.name or '',
        'description': description,
        'icon': item.icon or '',
        'icon_url': wow_icon_oss_url(item.icon, 'medium') if item.icon else '',
        'quality': item.quality,
        'catalog_type': item.catalog_type,
        'slot': item.slot_key,
        'armor_type': item.armor_type,
        'weapon_type': item.weapon_type,
        'unique_group': item.unique_group,
        'simc_token': item.simc_token,
        'enchantment_id': item.enchantment_id,
        'metadata': item.metadata or {},
        'variants': [serialize_variant(variant, class_name, spec_name) for variant in variants],
    }


def _catalog_queryset(season, variant_types, query=''):
    qs = WowItemVariantSnapshot.objects.filter(
        season=season,
        batch_key=season.gear_batch_key,
        variant_type__in=variant_types,
    ).select_related('item')
    query = str(query or '').strip()
    if query:
        lookup = Q(item__name__icontains=query) | Q(item__name_zh__icontains=query)
        if query.isdigit():
            lookup |= Q(item__item_id=int(query))
        qs = qs.filter(lookup)
    return qs.order_by('-item_level', 'item__name_zh', 'item__name', 'variant_key')


def catalog_items(*, class_name, spec_name, slot, source_type='all', query='', page=1, page_size=60):
    class_name, spec_name = canonical_spec(class_name, spec_name)
    if slot not in SLOT_LABELS:
        raise GearBuilderError('未知装备槽位')
    season = active_season()
    if not season or not season.gear_batch_key:
        return {'items': [], 'total': 0, 'page': 1, 'page_size': page_size, 'catalog': catalog_context(season)}

    grouped = defaultdict(list)
    for variant in _catalog_queryset(
        season,
        (WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT, WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT),
        query,
    ):
        if not slot_matches(variant, slot, class_name, spec_name) or not spec_matches(
            variant.item, class_name, spec_name, variant, slot,
        ):
            continue
        if not _source_matches(variant, source_type):
            continue
        grouped[variant.item_id].append(variant)

    rows = [serialize_item(variants[0].item, variants, class_name, spec_name) for variants in grouped.values()]
    rows.sort(key=lambda row: (-max((v['item_level'] for v in row['variants']), default=0), row['name']))
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 60)))
    start = (page - 1) * page_size
    return {
        'items': rows[start:start + page_size],
        'total': len(rows),
        'page': page,
        'page_size': page_size,
        'catalog': catalog_context(season),
    }


def enhancement_items(*, class_name, spec_name, slot, equipment_variant_id=None):
    class_name, spec_name = canonical_spec(class_name, spec_name)
    if slot not in SLOT_LABELS:
        raise GearBuilderError('未知装备槽位')
    season = active_season()
    if not season or not season.gear_batch_key:
        return {'groups': {'embellishments': [], 'gems': [], 'enchants': []}, 'catalog': catalog_context(season)}
    equipment_variant = None
    if equipment_variant_id:
        equipment_variant = WowItemVariantSnapshot.objects.filter(
            id=equipment_variant_id,
            season=season,
            batch_key=season.gear_batch_key,
        ).select_related('item').first()

    groups = {'embellishments': [], 'gems': [], 'enchants': []}
    type_to_group = {
        WowItemVariantSnapshot.TYPE_EMBELLISHMENT: 'embellishments',
        WowItemVariantSnapshot.TYPE_GEM: 'gems',
        WowItemVariantSnapshot.TYPE_ENCHANT: 'enchants',
    }
    grouped = defaultdict(list)
    highest_quality = {}
    for variant in _catalog_queryset(season, tuple(type_to_group)):
        if not spec_matches(variant.item, class_name, spec_name, variant, slot):
            continue
        if variant.variant_type == WowItemVariantSnapshot.TYPE_EMBELLISHMENT:
            if not equipment_variant or equipment_variant.variant_type != WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT:
                continue
        if not slot_matches(variant, slot, class_name, spec_name):
            continue
        if variant.variant_type in (WowItemVariantSnapshot.TYPE_GEM, WowItemVariantSnapshot.TYPE_ENCHANT):
            if int(variant.item.quality or 0) < 3:
                continue
            metadata = variant.metadata if isinstance(variant.metadata, dict) else {}
            family = str(metadata.get('simc_name') or variant.item.simc_token or variant.item_id).casefold()
            family = family.rsplit('_', 1)[0] if family.rsplit('_', 1)[-1].isdigit() else family
            key = (variant.variant_type, family, str(metadata.get('category_name') or ''))
            score = (int(variant.crafting_quality or 0), int(variant.item.quality or 0), int(variant.item_id))
            if key not in highest_quality or score > highest_quality[key][0]:
                highest_quality[key] = (score, variant)
            continue
        grouped[(variant.variant_type, variant.item_id)].append(variant)
    for _score, variant in highest_quality.values():
        grouped[(variant.variant_type, variant.item_id)].append(variant)
    for (variant_type, _item_id), variants in grouped.items():
        groups[type_to_group[variant_type]].append(serialize_item(variants[0].item, variants, class_name, spec_name))
    for rows in groups.values():
        rows.sort(key=lambda row: row['name'])
    return {'groups': groups, 'catalog': catalog_context(season)}


def resolve_crafted_variant(*, variant_id, selected_stats=None, embellishment_variant_id=None,
                             class_name='Warrior', spec_name='Fury'):
    class_name, spec_name = canonical_spec(class_name, spec_name)
    season = active_season()
    if not season or not season.gear_batch_key:
        raise GearBuilderError('当前赛季装备目录尚未同步')
    variant = WowItemVariantSnapshot.objects.filter(
        id=variant_id,
        season=season,
        batch_key=season.gear_batch_key,
        variant_type=WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT,
    ).select_related('item').first()
    if not variant:
        raise GearBuilderError('制造装备变体不存在或已过期')
    options = variant.crafting_options or {}
    allowed = [str(value) for value in (options.get('stat_pool') or ('crit', 'haste', 'mastery', 'versatility'))]
    required_count = int(options.get('stat_count') or 2)
    selected = list(dict.fromkeys(str(value) for value in (selected_stats or []) if value))
    if len(selected) != required_count or any(value not in allowed for value in selected):
        raise GearBuilderError(f'请选择 {required_count} 项合法制造绿字')

    stats = stats_for_identity(variant.stats_json, variant.metadata, class_name, spec_name)
    total = _number(options.get('secondary_total') or (variant.stats_json or {}).get('secondary_total'))
    explicit = options.get('stat_values') if isinstance(options.get('stat_values'), dict) else {}
    if explicit:
        for key in selected:
            stats[key] = _number(explicit.get(key))
    elif total:
        base = floor(total / required_count)
        remainder = int(total - base * required_count)
        for index, key in enumerate(selected):
            stats[key] = base + (1 if index < remainder else 0)

    embellishment = None
    effects = list(variant.effects_json or [])
    if embellishment_variant_id:
        embellishment = WowItemVariantSnapshot.objects.filter(
            id=embellishment_variant_id,
            season=season,
            batch_key=season.gear_batch_key,
            variant_type=WowItemVariantSnapshot.TYPE_EMBELLISHMENT,
        ).select_related('item').first()
        if not embellishment or not slot_matches(embellishment, variant.item.slot_key or (variant.compatible_slots or [''])[0]):
            raise GearBuilderError('所选美化与该制造装备不兼容')
        effects.extend(embellishment.effects_json or [])

    return {
        'variant': serialize_item(variant.item, [variant], class_name, spec_name),
        'resolved_stats': stats,
        'selected_stats': selected,
        'effects': effects,
        'embellishment': serialize_item(embellishment.item, [embellishment], class_name, spec_name) if embellishment else None,
        'simc': {
            'item_id': variant.item.item_id,
            'ilevel': variant.item_level,
            'bonus_ids': variant.bonus_ids or [],
            'crafted_stats': selected,
            'crafting_quality': variant.crafting_quality,
        },
    }


def _simc_item_id(payload):
    if not isinstance(payload, dict):
        return 0
    return int(payload.get('item_id') or payload.get('id') or 0)


def import_simc_profile(profile_text):
    if not str(profile_text or '').strip():
        raise GearBuilderError('请粘贴 SimC Profile')
    parsed = parse_simc_player_profile(profile_text)
    profile = parsed.get('profile') or {}
    identity = profile.get('identity') or {}
    normalized_identity = canonical_class_spec(identity.get('class_name'), identity.get('spec'))
    equipment = profile.get('equipment') or []
    season = active_season()
    batch_key = season.gear_batch_key if season else ''
    item_ids = [_simc_item_id(row) for row in equipment]
    enhancer_ids = []
    for row in equipment:
        enhancer_ids.extend(_simc_item_id(gem) for gem in (row.get('gems') or []))
        enhancer_ids.append(_simc_item_id(row.get('enchant')))
    requested_ids = [value for value in item_ids + enhancer_ids if value]
    variants = list(WowItemVariantSnapshot.objects.filter(
        Q(item__item_id__in=requested_ids) | Q(item__enchantment_id__in=requested_ids),
        season=season,
        batch_key=batch_key,
    ).select_related('item')) if season and batch_key else []
    by_item = defaultdict(list)
    by_enchantment = defaultdict(list)
    for variant in variants:
        by_item[int(variant.item.item_id)].append(variant)
        if variant.item.enchantment_id:
            by_enchantment[int(variant.item.enchantment_id)].append(variant)

    mapped = []
    warnings = []
    for row in equipment:
        item_id = _simc_item_id(row)
        candidates = by_item.get(item_id, [])
        requested_bonus = {str(value) for value in (row.get('bonus_ids') or [])}
        requested_level = int(row.get('item_level') or 0)
        selected = next((value for value in candidates if requested_bonus and set(map(str, value.bonus_ids or [])) == requested_bonus), None)
        selected = selected or next((value for value in candidates if requested_level and value.item_level == requested_level), None)
        selected = selected or (candidates[0] if candidates else None)
        external = selected is None
        if external:
            warnings.append(f"{SLOT_LABELS.get(row.get('slot'), row.get('slot'))} 的物品 #{item_id} 不在当前目录中，已作为外部装备保留。")
        enchant_payload = row.get('enchant') or {}
        enchant_id = _simc_item_id(enchant_payload)
        enchant_candidates = by_item.get(enchant_id) or by_enchantment.get(enchant_id) or []
        gem_rows = []
        for gem in row.get('gems') or []:
            gem_id = _simc_item_id(gem)
            gem_variant = next((value for value in by_item.get(gem_id, []) if value.variant_type == WowItemVariantSnapshot.TYPE_GEM), None)
            gem_rows.append({
                'item_id': gem_id,
                'variant_id': gem_variant.id if gem_variant else None,
                'variant': serialize_variant(gem_variant) if gem_variant else None,
                'item': serialize_item(gem_variant.item, [gem_variant]) if gem_variant else None,
                'external': not bool(gem_variant),
                'name': gem.get('display_name') or f'#{gem_id}',
            })
        mapped.append({
            'slot': row.get('slot'),
            'item_id': item_id,
            'name': row.get('display_name') or f'#{item_id}',
            'item_level': requested_level,
            'variant_id': selected.id if selected else None,
            'variant': serialize_variant(selected) if selected else None,
            'item': serialize_item(selected.item, [selected]) if selected else None,
            'external': external,
            'bonus_ids': row.get('bonus_ids') or [],
            'crafted_stats': row.get('crafted_stats') or [],
            'crafting_quality': row.get('crafting_quality') or 0,
            'gems': gem_rows,
            'enchant': {
                'item_id': enchant_id,
                'variant_id': enchant_candidates[0].id if enchant_candidates else None,
                'variant': serialize_variant(enchant_candidates[0]) if enchant_candidates else None,
                'item': serialize_item(enchant_candidates[0].item, [enchant_candidates[0]]) if enchant_candidates else None,
                'external': bool(enchant_id and not enchant_candidates),
                'name': enchant_payload.get('display_name') or (f'#{enchant_id}' if enchant_id else ''),
            } if enchant_id else None,
            'raw_value': row.get('raw_value') or '',
        })
    return {
        'identity': {
            'class_name': normalized_identity[0] if normalized_identity else '',
            'spec_name': normalized_identity[1] if normalized_identity else '',
            'class_cn': CLASS_CN.get(normalized_identity[0], '') if normalized_identity else '',
            'spec_cn': SPEC_CN.get(normalized_identity[1], '') if normalized_identity else '',
        },
        'equipment': mapped,
        'warnings': warnings,
        'catalog': catalog_context(season),
    }
