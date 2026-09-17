"""统一投影装备名称、图标、属性、效果、来源与 Tooltip。"""
from __future__ import annotations

import re

from django.db.models import Exists, OuterRef

from botend.constants.wow import localize_gear_source
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.templatetags.wow_tags import wow_icon_oss_url
from botend.services.gear_builder_tier_sources import tier_set_sources
from botend.services.wow_item_text import (
    EFFECT_PREFIX, ENHANCEMENTS, STAT_NAMES, separate_item_text,
)


STAT_LABELS = {
    'strength': '力量', 'agility': '敏捷', 'intellect': '智力', 'stamina': '耐力',
    'armor': '护甲', 'bonus_armor': '额外护甲', 'crit': '暴击', 'haste': '急速',
    'mastery': '精通', 'versatility': '全能', 'leech': '吸血',
    'avoidance': '闪避', 'speed': '速度', 'weapon_dps': '武器秒伤',
    'min_damage': '最低伤害', 'max_damage': '最高伤害',
    'stragiint': '力量／敏捷／智力', 'agiint': '敏捷／智力',
    'stragi': '力量／敏捷', 'strint': '力量／智力',
}
LAYOUT_STAT_ALIASES = {
    'bonus_armor': ('额外护甲', 'Bonus Armor'),
    'weapon_dps': ('武器秒伤', 'Damage Per Second'),
    'min_damage': ('最低伤害',),
    'max_damage': ('最高伤害',),
    'stragiint': ('力量', '敏捷', '智力', 'Strength', 'Agility', 'Intellect'),
    'agiint': ('敏捷', '智力', 'Agility', 'Intellect'),
    'stragi': ('力量', '敏捷', 'Strength', 'Agility'),
    'strint': ('力量', '智力', 'Strength', 'Intellect'),
}
LAYOUT_STATIC_BOUNDARY = re.compile(
    r'^(?:["“”‘’「『]|装备唯一|Unique-Equipped|拾取后绑定|Binds\b|'
    r'史诗钥石|Mythic Keystone|升级\s*[:：]|Upgrade\s*[:：]|需要|Requires\b|'
    r'耐久|Durability\b|售价|Sell Price\b|掉落于|Dropped by\b|'
    r'来源\s*[:：]|Source\s*[:：]|职业\s*[:：]|Classes?\s*[:：]|'
    r'种族\s*[:：]|Races?\s*[:：]|插槽|Socket\b)',
    re.I,
)
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


def exact_build_variant_is_complete(variant, build):
    """中央目录中 PTR 精确构建变体的可展示资格。"""
    metadata = variant.metadata if isinstance(variant.metadata, dict) else {}
    effects = variant.effects_json if isinstance(variant.effects_json, list) else []
    if not metadata.get('ptr_preview'):
        return True
    if (
        metadata.get('stats_status') != 'exact_build_simc'
        or metadata.get('effects_status') != 'exact_build_db2_simc'
        or str(metadata.get('game_build') or '') != str(build or '')
        or not isinstance(variant.stats_json, dict) or not variant.stats_json
        or not effects
    ):
        return False
    return all(
        isinstance(effect, dict)
        and str(effect.get('game_build') or '') == str(build or '')
        and not effect.get('unresolved_tokens')
        and bool(str(effect.get('description_zh') or effect.get('description') or '').strip())
        for effect in effects
    )


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


def _stat_alias_matches(line, keys):
    folded = line.casefold()
    matches = []
    for key in keys:
        aliases = (*STAT_NAMES.get(key, ()), *LAYOUT_STAT_ALIASES.get(key, ()))
        lengths = [len(alias) for alias in aliases if alias.casefold() in folded]
        if lengths:
            matches.append((max(lengths), key))
    return matches


def _is_source_stat_row(line):
    """Only standalone numeric stat rows qualify; prose mentioning stats does not."""
    value = str(line or '').strip().strip('()[]')
    if re.fullmatch(r'[\d,.]+\s*-\s*[\d,.]+\s*(?:damage|伤害)', value, re.I):
        return True
    if re.fullmatch(r'[\d,.]+\s*(?:damage per second|每秒伤害|武器秒伤)', value, re.I):
        return True
    match = re.fullmatch(r'\+?\s*[\d,.]+\s*(?:点\s*)?(?P<labels>.+)', value)
    if not match:
        return False
    remainder = match.group('labels').strip().strip('()[]')
    aliases = sorted(
        {alias for names in (*STAT_NAMES.values(), *LAYOUT_STAT_ALIASES.values()) for alias in names},
        key=len,
        reverse=True,
    )
    for alias in aliases:
        remainder = re.sub(re.escape(alias), '', remainder, flags=re.I)
    remainder = re.sub(r'\b(?:or|and)\b|[\s或和与及、,/&+／]+', '', remainder, flags=re.I)
    return not remainder


def _source_stat_keys(line):
    """Classify a source tooltip stat row independently of current variant values."""
    if not _is_source_stat_row(line):
        return []
    folded = line.casefold()
    if any(token in folded for token in ('damage per second', '每秒伤害', '武器秒伤')):
        return ['weapon_dps']
    if 'damage' in folded or '伤害' in line:
        return ['min_damage', 'max_damage']
    return [key for _length, key in _stat_alias_matches(line, (*STAT_NAMES, *LAYOUT_STAT_ALIASES))]


def _layout_stat_keys(line, remaining_keys):
    matches = _stat_alias_matches(line, remaining_keys)
    if not matches:
        return []
    longest = max(length for length, _key in matches)
    return [key for length, key in matches if length == longest]


def _layout_text_shape(line):
    value = re.sub(r'[\W\d_]+', '', str(line or '').casefold())
    return re.sub(r'^(?:装备|使用|被动|效果|equip|use|passive|effect)', '', value)


def _consume_layout_shape(shape_parts, line):
    """Consume one source line across current effect shapes despite line-wrap differences."""
    remaining = list(shape_parts)
    source_shape = _layout_text_shape(line)
    if not source_shape:
        return None
    while source_shape and remaining:
        current_shape = remaining[0]
        if current_shape.startswith(source_shape):
            suffix = current_shape[len(source_shape):]
            if suffix:
                remaining[0] = suffix
            else:
                remaining.pop(0)
            source_shape = ''
        elif source_shape.startswith(current_shape):
            source_shape = source_shape[len(current_shape):]
            remaining.pop(0)
        else:
            return None
    return remaining if not source_shape else None


def _clean_layout_text(text, names=()):
    """只清除目录噪声并保留槽位、类型、换行和原始布局顺序。"""
    value = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    value = re.sub(r'(?:物品等级|Item Level)\s*[:：]?\s*[\d,.]+', '', value, flags=re.I)
    value = re.sub(
        r'(?:最大叠加|最大堆叠|Max(?:imum)? Stack(?: Size)?)\s*[:：]?\s*[\d,]+',
        '', value, flags=re.I,
    )
    value = re.sub(
        r'(?:售价|Sell Price)\s*[:：]?\s*(?:[\d,.]+\s*(?:金币?|银币?|铜币?|gold|silver|copper)?\s*)+',
        '', value, flags=re.I,
    ).strip()
    for name in filter(None, names):
        value = re.sub(r'^' + re.escape(str(name)) + r'(?:\s+|$)', '', value).strip()
    return value


def _clean_layout_line(line):
    value = str(line or '').strip()
    if not value:
        return ''
    if re.fullmatch(r'(?:物品等级|Item Level)\s*[:：]?\s*[\d,.]+', value, re.I):
        return ''
    if re.fullmatch(
        r'(?:售价|Sell Price)\s*[:：]?\s*(?:[\d,.]+\s*(?:金币?|银币?|铜币?|gold|silver|copper)?\s*)+',
        value,
        re.I,
    ):
        return ''
    return value


def _layout_detail_lines(layout, stats, effect_groups, fallback):
    """Replace source stat/effect rows in place, retaining per-item tooltip order."""
    normalized_stats = _normalize_stats(stats)
    remaining_keys = list(normalized_stats)
    ordered_stat_keys = []
    rows = []
    effect_index = 0
    pending_effect_shapes = []
    in_source_effect_block = False
    last_effect_index = None
    last_stat_index = None

    raw_layout = str(layout or '').replace('\r\n', '\n').replace('\r', '\n')
    layout_lines = raw_layout.split('\n') if raw_layout.strip() else []
    for raw_line in layout_lines:
        line = _clean_layout_line(raw_line)
        if not line:
            if in_source_effect_block and not str(raw_line or '').strip():
                in_source_effect_block = False
                pending_effect_shapes = []
            continue
        if pending_effect_shapes:
            consumed_shapes = _consume_layout_shape(pending_effect_shapes, line)
            if consumed_shapes is not None:
                pending_effect_shapes = consumed_shapes
                continue
            pending_effect_shapes = []
        if EFFECT_PREFIX.match(line):
            if effect_index < len(effect_groups):
                group = effect_groups[effect_index]
                rows.extend(group)
                group_shapes = [shape for shape in map(_layout_text_shape, group) if shape]
                consumed_shapes = _consume_layout_shape(group_shapes, line)
                pending_effect_shapes = (
                    consumed_shapes if consumed_shapes is not None else group_shapes[1:]
                )
                last_effect_index = len(rows)
                effect_index += 1
            in_source_effect_block = True
            continue
        if in_source_effect_block:
            if not (_is_source_stat_row(line) or LAYOUT_STATIC_BOUNDARY.match(line)):
                continue
            in_source_effect_block = False
        source_keys = _source_stat_keys(line)
        if source_keys:
            for key in _layout_stat_keys(line, remaining_keys):
                rows.append(f'+{_format_number(normalized_stats[key])} {STAT_LABELS.get(key, key)}')
                remaining_keys.remove(key)
                ordered_stat_keys.append(key)
            last_stat_index = len(rows)
            continue
        rows.append(line)

    remaining_stat_lines = [
        f'+{_format_number(normalized_stats[key])} {STAT_LABELS.get(key, key)}'
        for key in remaining_keys
    ]
    if remaining_stat_lines:
        insert_at = last_stat_index if last_stat_index is not None else 0
        rows[insert_at:insert_at] = remaining_stat_lines
        ordered_stat_keys.extend(remaining_keys)
        if last_effect_index is not None and insert_at <= last_effect_index:
            last_effect_index += len(remaining_stat_lines)
        last_stat_index = insert_at + len(remaining_stat_lines)

    remaining_effect_lines = [
        line
        for group in effect_groups[effect_index:]
        for line in group
    ]
    if remaining_effect_lines:
        insert_at = last_effect_index if last_effect_index is not None else (last_stat_index or 0)
        rows[insert_at:insert_at] = remaining_effect_lines

    if not layout_lines and fallback:
        rows.extend(
            line.strip()
            for line in str(fallback).replace('\r\n', '\n').replace('\r', '\n').split('\n')
            if line.strip()
        )
    return rows, ordered_stat_keys


def _tooltip_text(*, item_level=0, stats=None, effects=None, sources=None, fallback='', layout=''):
    lines = []
    if _positive_int(item_level):
        lines.append(f'物品等级 {_positive_int(item_level)}')
    effect_groups = [
        [line.strip() for line in re.split(r'\n|\s+·\s+', _effect_text(effect).replace('\r\n', '\n')) if line.strip()]
        for effect in _rows(effects)
    ]
    effect_groups = [group for group in effect_groups if group]
    detail_lines, _ordered_stat_keys = _layout_detail_lines(layout, stats, effect_groups, fallback)
    lines.extend(detail_lines)
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
    has_structured_projection = variant is not None or stats is not None or effects is not None
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
    tier_sources = tier_set_sources(
        {**(snapshot.metadata or {}), **variant_metadata} if snapshot else variant_metadata,
        snapshot.slot_key if snapshot else '',
    )
    if tier_sources is not None:
        sources = tier_sources
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
    tooltip_layout = _clean_layout_text(
        description_zh.strip() or description.strip(),
        (name, name_zh),
    )
    separated = separate_item_text(
        description=description, description_zh=description_zh, effects=_rows(effects),
        stats=normalized_stats, metadata=variant_metadata, names=(name, name_zh),
        enhancement=bool(snapshot and snapshot.catalog_type in ENHANCEMENTS),
        recover_description_effects=(
            not has_structured_projection
            or bool(snapshot and snapshot.catalog_type in ENHANCEMENTS)
        ),
    )
    description, description_zh = separated['description'], separated['description_zh']
    normalized_effects = [text for text in (_effect_text(row) for row in separated['effects']) if text]
    normalized_sources = [text for text in (_source_text(row) for row in _rows(sources)) if text]
    base_description = description_zh.strip() or description.strip()
    projection_layout = tooltip_layout if has_structured_projection else ''
    _layout_rows, ordered_stat_keys = _layout_detail_lines(
        projection_layout, normalized_stats, [], base_description,
    )
    stat_lines = [
        f'+{_format_number(normalized_stats[key])} {STAT_LABELS.get(key, key)}'
        for key in ordered_stat_keys
    ]
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
        fallback=base_description,
        layout=projection_layout,
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
        "stat_lines": stat_lines,
        "effects": normalized_effects,
        "effect_details": separated['effects'],
        "text_schema_version": 2,
        "effects_missing": bool(snapshot and snapshot.catalog_type in ENHANCEMENTS and not normalized_effects and not normalized_stats),
        "sources": normalized_sources,
        "variant_id": getattr(variant, 'pk', None),
        "variant_key": str(getattr(variant, 'variant_key', '') or ''),
        "game_build": str(getattr(variant, 'game_build', '') or ''),
        "variant_metadata": variant_metadata,
        "tooltip_complete": bool(normalized_stats or normalized_effects) and not (
            expects_effect and not normalized_effects
        ),
        "icon": icon,
        "icon_url": wow_icon_oss_url(icon, icon_size) if icon else "",
        "quality": (snapshot.quality if snapshot else 0) or 0,
        "catalog_type": (snapshot.catalog_type if snapshot else "") or "",
        "inventory_type": (snapshot.inventory_type if snapshot else 0) or 0,
        "slot_key": (snapshot.slot_key if snapshot else "") or "",
        "item_class_id": (snapshot.item_class_id if snapshot else 0) or 0,
        "item_subclass_id": (snapshot.item_subclass_id if snapshot else 0) or 0,
        "journal_item": snapshot_metadata.get('journal_item') or {},
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
        allow_default_variant = bool(request.get('allow_default_variant'))
        game_build = str(request.get('game_build') or '').strip()
        default_variant_order = str(
            request.get('default_variant_order') or 'highest'
        ).strip().casefold()
        require_complete_variant = bool(request.get('require_complete_variant'))
    else:
        values = list(request) if isinstance(request, (tuple, list)) else [request]
        item_id = values[0] if values else None
        item_level = values[1] if len(values) > 1 else None
        bonus_ids = values[2] if len(values) > 2 else None
        primary_stat = _primary_stat_for_identity(primary_stat=values[3] if len(values) > 3 else '')
        allow_default_variant = False
        game_build = ''
        default_variant_order = 'highest'
        require_complete_variant = False
    if isinstance(bonus_ids, str):
        bonus_ids = bonus_ids.replace(';', '/').replace(':', '/').split('/')
    elif not isinstance(bonus_ids, (tuple, list, set)):
        bonus_ids = [bonus_ids] if bonus_ids not in (None, '') else []
    return _positive_int(item_id), _positive_int(item_level), tuple(sorted({
        value for raw in (bonus_ids or []) for value in [_positive_int(raw)] if value
    })), primary_stat, allow_default_variant, game_build, default_variant_order, require_complete_variant


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
    item_ids = {
        item_id
        for (
            item_id, _item_level, _bonus_ids, _primary_stat,
            _allow_default_variant, _game_build, _default_order, _require_complete,
        ) in normalized
        if item_id
    }
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
    for (
        item_id, item_level, bonus_ids, primary_stat,
        allow_default_variant, game_build, default_order, require_complete,
    ) in normalized:
        candidates = variants_by_item.get(item_id, [])
        if game_build:
            candidates = [row for row in candidates if str(row.game_build or '') == game_build]
        if require_complete:
            candidates = [row for row in candidates if exact_build_variant_is_complete(row, game_build)]
        if item_level:
            exact = [row for row in candidates if _positive_int(row.item_level) == item_level]
            candidates = exact
        elif not bonus_ids and not allow_default_variant:
            candidates = []
        if default_order == 'lowest' and not item_level and not bonus_ids:
            variant = min(
                candidates,
                key=lambda row: (_positive_int(row.item_level), int(row.pk or 0)),
                default=None,
            )
        else:
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
