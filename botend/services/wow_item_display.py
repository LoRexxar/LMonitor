"""统一投影装备名称、图标、属性、效果、来源与 Tooltip。"""
from __future__ import annotations

from django.db.models import Exists, OuterRef

from botend.constants.wow import localize_gear_source
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.templatetags.wow_tags import wow_icon_oss_url


STAT_LABELS = {
    'strength': '力量', 'agility': '敏捷', 'intellect': '智力', 'stamina': '耐力',
    'armor': '护甲', 'bonus_armor': '额外护甲', 'crit': '暴击', 'haste': '急速',
    'mastery': '精通', 'versatility': '全能', 'leech': '吸血',
    'avoidance': '闪避', 'speed': '速度', 'weapon_dps': '武器秒伤',
    'min_damage': '最低伤害', 'max_damage': '最高伤害',
}
EQUIPMENT_TYPES = {
    WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
    WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT,
}
PRIMARY_STAT_KEYS = {'strength', 'agility', 'intellect'}
INTELLECT_SPECS = {
    'paladin_holy', 'priest_discipline', 'priest_holy', 'priest_shadow',
    'shaman_elemental', 'shaman_restoration', 'mage_arcane', 'mage_fire', 'mage_frost',
    'warlock_affliction', 'warlock_demonology', 'warlock_destruction',
    'monk_mistweaver', 'druid_balance', 'druid_restoration',
    'demonhunter_devourer',
    'evoker_devastation', 'evoker_preservation', 'evoker_augmentation',
}
AGILITY_CLASSES = {'hunter', 'rogue', 'demonhunter'}
AGILITY_SPECS = {
    'shaman_enhancement', 'monk_brewmaster', 'monk_windwalker',
    'druid_feral', 'druid_guardian',
}


def _positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _number(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0
    return int(parsed) if parsed.is_integer() else parsed


def _normalize_stats(raw):
    raw = raw if isinstance(raw, dict) else {}
    result = {}
    for key, value in raw.items():
        if key in STAT_LABELS:
            parsed = value.get('rating', value.get('value', 0)) if isinstance(value, dict) else value
            parsed = _number(parsed)
            if parsed:
                result[key] = parsed
    for group_name in ('primary', 'secondary', 'tertiary', 'weapon'):
        group = raw.get(group_name)
        if isinstance(group, dict):
            result.update(_normalize_stats(group))
    return result


def _primary_stat_for_identity(*, primary_stat='', spec_key='', class_name='', spec_name=''):
    explicit = str(primary_stat or '').strip().casefold()
    if explicit in PRIMARY_STAT_KEYS:
        return explicit
    normalized_class = ''.join(ch for ch in str(class_name or '').casefold() if ch.isalnum())
    normalized_spec = ''.join(ch for ch in str(spec_name or '').casefold() if ch.isalnum())
    normalized_key = str(spec_key or '').strip().casefold().replace('-', '_').replace(' ', '_')
    if not normalized_class and '_' in normalized_key:
        normalized_class = normalized_key.partition('_')[0]
    if not normalized_key and normalized_class:
        normalized_key = f'{normalized_class}_{normalized_spec}' if normalized_spec else normalized_class
    if normalized_key in INTELLECT_SPECS:
        return 'intellect'
    if normalized_class in AGILITY_CLASSES or normalized_key in AGILITY_SPECS:
        return 'agility'
    return 'strength' if normalized_key or normalized_class else ''


def _effect_text(effect):
    if isinstance(effect, str):
        return effect.strip()
    if not isinstance(effect, dict):
        return ''
    return str(
        effect.get('description_zh') or effect.get('description')
        or effect.get('name_zh') or effect.get('name') or ''
    ).strip()


def _rows(value):
    if value in (None, ''):
        return []
    return list(value) if isinstance(value, (tuple, list, set)) else [value]


def _format_number(value):
    parsed = _number(value)
    return f'{parsed:,}' if isinstance(parsed, int) else f'{parsed:,.2f}'.rstrip('0').rstrip('.')


def _source_text(source):
    if isinstance(source, str):
        return source.strip()
    if not isinstance(source, dict):
        return ''
    row = localize_gear_source(source)
    source_type = str(row.get('type_zh') or row.get('type') or '').strip()
    instance = str(row.get('instance_zh') or row.get('instance') or '').strip()
    encounter = str(
        row.get('encounter_zh') or row.get('boss_zh')
        or row.get('encounter') or row.get('boss') or ''
    ).strip()
    profession = str(row.get('profession_zh') or row.get('profession') or '').strip()
    difficulty = str(row.get('difficulty_zh') or row.get('difficulty') or '').strip()
    return ' · '.join(
        value for value in (source_type, instance, encounter, difficulty, profession) if value
    )


def _tooltip_text(*, item_level=0, stats=None, effects=None, sources=None, fallback=''):
    lines = []
    if _positive_int(item_level):
        lines.append(f'物品等级 {_positive_int(item_level)}')
    for key, value in _normalize_stats(stats).items():
        lines.append(f'+{_format_number(value)} {STAT_LABELS.get(key, key)}')
    effect_lines = []
    for effect in _rows(effects):
        effect_lines.extend(line.strip() for line in _effect_text(effect).replace('\r\n', '\n').split('\n') if line.strip())
    if effect_lines:
        lines.extend(effect_lines)
    elif fallback:
        lines.extend(line.strip() for line in str(fallback).replace('\r\n', '\n').split('\n') if line.strip())
    source_lines = []
    for source in _rows(sources):
        text = _source_text(source)
        if text and text not in source_lines:
            source_lines.append(text)
    if source_lines:
        lines.append(f'来源：{"；".join(source_lines[:2])}')
    seen = set()
    return '\n'.join(line for line in lines if not (line.casefold() in seen or seen.add(line.casefold())))


def item_display_metadata(
    item_id, snapshot=None, *, item_level=0, variant=None, stats=None, effects=None,
    sources=None, icon_size='small', primary_stat='',
):
    """返回三个装备入口共同消费的稳定展示契约。"""
    normalized_id = _positive_int(item_id) or None
    if variant is not None:
        item_level = _positive_int(item_level) or _positive_int(variant.item_level)
        stats = variant.stats_json if stats is None else stats
        effects = variant.effects_json if effects is None else effects
        sources = variant.source_json if sources is None else sources
    name = (snapshot.name if snapshot else "") or ""
    name_zh = (snapshot.name_zh if snapshot else "") or ""
    description = (snapshot.description if snapshot else "") or ""
    description_zh = (snapshot.description_zh if snapshot else "") or ""
    icon = (snapshot.icon if snapshot else "") or ""
    normalized_stats = _normalize_stats(stats)
    variant_metadata = (
        variant.metadata if variant is not None and isinstance(variant.metadata, dict) else {}
    )
    primary_values = (
        variant_metadata.get('primary_stat_values')
        if isinstance(variant_metadata.get('primary_stat_values'), dict) else {}
    )
    if primary_stat and (primary_values or variant_metadata.get('primary_stat_amount')):
        for key in PRIMARY_STAT_KEYS - {primary_stat}:
            normalized_stats.pop(key, None)
        primary_value = primary_values.get(primary_stat) or variant_metadata.get('primary_stat_amount')
        if _number(primary_value):
            normalized_stats[primary_stat] = _number(primary_value)
    normalized_effects = [text for text in (_effect_text(row) for row in _rows(effects)) if text]
    normalized_sources = [text for text in (_source_text(row) for row in _rows(sources)) if text]
    base_description = description_zh.strip() or description.strip()
    snapshot_metadata = snapshot.metadata if snapshot and isinstance(snapshot.metadata, dict) else {}
    expects_effect = bool(
        snapshot and (
            snapshot.slot_key == 'trinket'
            or snapshot.effect_refs
            or snapshot_metadata.get('requires_effect_mapping')
            or any(prefix in base_description.casefold() for prefix in ('装备：', '使用：', 'equip:', 'use:'))
        )
    )
    tooltip = _tooltip_text(
        item_level=item_level,
        stats=normalized_stats,
        effects=normalized_effects,
        sources=sources,
        fallback=base_description if variant is None else '',
    )
    return {
        "id": normalized_id,
        "item_id": normalized_id,
        "name": name,
        "name_zh": name_zh,
        "display_name": name_zh or name or (f"#{normalized_id}" if normalized_id else "未知物品"),
        "description": description,
        "description_zh": description_zh,
        "display_description": tooltip,
        "tooltip": tooltip,
        "item_level": _positive_int(item_level) or None,
        "stats": normalized_stats,
        "effects": normalized_effects,
        "sources": normalized_sources,
        "variant_id": getattr(variant, 'pk', None),
        "variant_key": str(getattr(variant, 'variant_key', '') or ''),
        "tooltip_complete": bool(normalized_stats or normalized_effects) and not (
            expects_effect and not normalized_effects
        ),
        "icon": icon,
        "icon_url": wow_icon_oss_url(icon, icon_size) if icon else "",
        "quality": (snapshot.quality if snapshot else 0) or 0,
        "wowhead_url": f"https://www.wowhead.com/cn/item={normalized_id}" if normalized_id else "",
    }


def _request_values(request):
    if isinstance(request, dict):
        item_id = request.get('item_id', request.get('id'))
        item_level = request.get('item_level', request.get('ilevel'))
        bonus_ids = request.get('bonus_ids', request.get('bonus_id'))
        primary_stat = _primary_stat_for_identity(
            primary_stat=request.get('primary_stat'),
            spec_key=request.get('spec_key'),
            class_name=request.get('class_name'),
            spec_name=request.get('spec_name', request.get('spec')),
        )
    else:
        values = list(request) if isinstance(request, (tuple, list)) else [request]
        item_id = values[0] if values else None
        item_level = values[1] if len(values) > 1 else None
        bonus_ids = values[2] if len(values) > 2 else None
        primary_stat = _primary_stat_for_identity(primary_stat=values[3] if len(values) > 3 else '')
    if isinstance(bonus_ids, str):
        bonus_ids = bonus_ids.replace(';', '/').replace(':', '/').split('/')
    elif not isinstance(bonus_ids, (tuple, list, set)):
        bonus_ids = [bonus_ids] if bonus_ids not in (None, '') else []
    return _positive_int(item_id), _positive_int(item_level), tuple(sorted({
        value for raw in (bonus_ids or []) for value in [_positive_int(raw)] if value
    })), primary_stat


def _variant_score(variant, item_level, bonus_ids):
    variant_bonus_ids = {_positive_int(value) for value in (variant.bonus_ids or [])}
    requested_bonus_ids = set(bonus_ids)
    return (
        int(_positive_int(variant.item_level) == item_level) if item_level else 0,
        len(variant_bonus_ids.intersection(requested_bonus_ids)),
        int(variant.variant_type in EQUIPMENT_TYPES),
        _positive_int(variant.item_level),
        -int(variant.pk or 0),
    )


def load_item_tooltip_metadata(requests):
    """按输入顺序批量匹配活动目录的具体装备变体。"""
    normalized = [_request_values(request) for request in (requests or [])]
    if not normalized:
        return []
    item_ids = {item_id for item_id, _item_level, _bonus_ids, _primary_stat in normalized if item_id}
    snapshots = {
        int(row.item_id): row for row in WowItemSnapshot.objects.filter(item_id__in=item_ids)
    }
    active = SeasonMeta.objects.filter(is_active=True).exclude(gear_batch_key='').annotate(
        has_gear_catalog=Exists(WowItemVariantSnapshot.objects.filter(
            season_id=OuterRef('pk'), batch_key=OuterRef('gear_batch_key'),
        )),
    )
    season = (
        active.filter(has_gear_catalog=True).order_by('-gear_synced_at', '-id').first()
        or active.order_by('-id').first()
    )
    variants_by_item = {}
    if season and item_ids:
        for variant in WowItemVariantSnapshot.objects.filter(
            season=season,
            batch_key=season.gear_batch_key,
            item__item_id__in=item_ids,
        ).select_related('item'):
            variants_by_item.setdefault(int(variant.item.item_id), []).append(variant)
    result = []
    for item_id, item_level, bonus_ids, primary_stat in normalized:
        candidates = variants_by_item.get(item_id, [])
        if item_level:
            exact = [row for row in candidates if _positive_int(row.item_level) == item_level]
            candidates = exact
        elif not bonus_ids:
            candidates = []
        variant = max(candidates, key=lambda row: _variant_score(row, item_level, bonus_ids), default=None)
        snapshot = snapshots.get(item_id) or getattr(variant, 'item', None)
        result.append(item_display_metadata(
            item_id, snapshot, item_level=item_level, variant=variant, primary_stat=primary_stat,
        ))
    return result


def load_item_display_metadata(item_ids):
    """Bulk-load item display metadata keyed by numeric item ID."""
    normalized_ids = set()
    for item_id in item_ids or ():
        try:
            normalized_ids.add(int(item_id))
        except (TypeError, ValueError):
            continue
    if not normalized_ids:
        return {}
    snapshots = {
        int(row.item_id): row
        for row in WowItemSnapshot.objects.filter(item_id__in=normalized_ids)
    }
    return {
        item_id: item_display_metadata(item_id, snapshots.get(item_id))
        for item_id in normalized_ids
    }
