"""登录用户的辅助配装：确定性组合搜索与可选 AI 解释。"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from botend.constants.wow import localize_gear_source
from botend.models import GearBuilderOwnedItem, WowItemVariantSnapshot
from botend.services.gear_builder import (
    ADDITIONAL_SOCKET_SLOTS,
    EQUIPMENT_SLOTS,
    GearBuilderError,
    SLOT_FAMILIES,
    SLOT_LABELS,
    _resolve_crafted_rows,
    _source_track_is_valid,
    active_season,
    canonical_spec,
    normalize_stats,
    secondary_stat_conversion_rules,
    serialize_item,
    serialize_variant,
    slot_matches,
    spec_matches,
    stats_for_identity,
)
from botend.services.gear_builder_owned import list_owned_items


SECONDARY = ('crit', 'haste', 'mastery', 'versatility')
PLAN_LABELS = {
    'prefer_owned': '优先已有装备',
    'all': '全装备池',
    'dungeon': '仅地下城装备',
}
FLASKS = {
    'none': {'key': 'none', 'name': '不使用属性合剂', 'stats': {}},
    'crit': {'key': 'crit', 'name': '破碎残阳合剂', 'item_id': 241328, 'stats': {'crit': 165}},
    'haste': {'key': 'haste', 'name': '血骑士合剂', 'item_id': 241324, 'stats': {'haste': 165}},
    'mastery': {'key': 'mastery', 'name': '魔导师合剂', 'item_id': 241326, 'stats': {'mastery': 165}},
}


def _add_stats(base, extra):
    result = {key: float(base.get(key) or 0) for key in SECONDARY}
    for key in SECONDARY:
        result[key] += float((extra or {}).get(key) or 0)
    return result


def _conversion(class_name, spec_name):
    return secondary_stat_conversion_rules().get(f'{class_name}:{spec_name}') or {
        'crit_per_percent': 180, 'haste_per_percent': 170,
        'mastery_per_percent': 180, 'versatility_per_percent': 205,
        'mastery_coefficient': 1,
    }


def _percentages(stats, conversion):
    mastery_coefficient = float(conversion.get('mastery_coefficient') or 1)
    return {
        'crit': 5 + float(stats.get('crit') or 0) / float(conversion.get('crit_per_percent') or 1),
        'haste': float(stats.get('haste') or 0) / float(conversion.get('haste_per_percent') or 1),
        'mastery': (8 + float(stats.get('mastery') or 0) / float(conversion.get('mastery_per_percent') or 1)) * mastery_coefficient,
        'versatility': float(stats.get('versatility') or 0) / float(conversion.get('versatility_per_percent') or 1),
    }


def _distance(stats, target, conversion):
    percentages = _percentages(stats, conversion)
    return sum((float(percentages[key]) - float(target.get(key) or 0)) ** 2 for key in SECONDARY) ** 0.5


def _target_ratings(target, conversion):
    coefficient = float(conversion.get('mastery_coefficient') or 1)
    return {
        'crit': max(0, float(target.get('crit') or 0) - 5) * float(conversion.get('crit_per_percent') or 1),
        'haste': max(0, float(target.get('haste') or 0)) * float(conversion.get('haste_per_percent') or 1),
        'mastery': max(0, float(target.get('mastery') or 0) / coefficient - 8) * float(conversion.get('mastery_per_percent') or 1),
        'versatility': max(0, float(target.get('versatility') or 0)) * float(conversion.get('versatility_per_percent') or 1),
    }


def _source_types(variant):
    return {
        str(row.get('type') or '').casefold()
        for row in (variant.source_json or []) if isinstance(row, dict)
    }


def _candidate(variant, class_name, spec_name, selected_stats=(), owned_id=None, owned_quantity=1):
    stats = stats_for_identity(variant.stats_json, variant.metadata, class_name, spec_name)
    effects = variant.effects_json or []
    selected_stats = list(selected_stats or [])
    if variant.variant_type == WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT:
        stats, selected_stats, effects = _resolve_crafted_rows(
            variant, selected_stats, None, class_name, spec_name,
        )
    return {
        'variant': variant,
        'stats': {key: float(stats.get(key) or 0) for key in SECONDARY},
        'selected_stats': selected_stats,
        'effects': effects,
        'owned_id': owned_id,
        'owned_quantity': max(1, int(owned_quantity or 1)),
        'unique_group': variant.unique_group or variant.item.unique_group or '',
        'max_equipped': int(variant.max_equipped or 0),
        'two_handed': int(variant.item.inventory_type or 0) == 17,
    }


def _variant_candidates(variant, class_name, spec_name, owned_id=None, selected_stats=(), owned_quantity=1):
    if variant.variant_type != WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT:
        return [_candidate(variant, class_name, spec_name, owned_id=owned_id, owned_quantity=owned_quantity)]
    options = variant.crafting_options if isinstance(variant.crafting_options, dict) else {}
    count = max(1, int(options.get('stat_count') or 2))
    pool = [str(value) for value in (options.get('stat_pool') or SECONDARY) if value in SECONDARY]
    requested = [value for value in selected_stats if value in pool]
    choices = [tuple(requested)] if len(requested) == count else combinations(pool, count)
    return [_candidate(variant, class_name, spec_name, choice, owned_id, owned_quantity) for choice in choices]


def _current_pool(class_name, spec_name):
    season = active_season()
    if not season or not season.gear_batch_key:
        raise GearBuilderError('当前赛季装备目录尚未同步')
    rows = WowItemVariantSnapshot.objects.filter(
        season=season,
        batch_key=season.gear_batch_key,
        variant_type__in=(
            WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
            WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT,
        ),
    ).select_related('item').order_by('-item_level', '-crafting_quality')
    # 同一物品只保留最高装等变体；品级变化不改变用户设定的目标绿字方向。
    best = {}
    for variant in rows:
        if not _source_track_is_valid(variant):
            continue
        key = (variant.item_id, variant.variant_type)
        best.setdefault(key, variant)
    return list(best.values()), season


def _owned_pool(user, class_name, spec_name):
    result = defaultdict(list)
    rows = GearBuilderOwnedItem.objects.filter(user=user, variant__isnull=False).select_related('variant__item')
    for row in rows:
        variant = row.variant
        for slot, _label in EQUIPMENT_SLOTS:
            if row.slot_key and SLOT_FAMILIES.get(slot, slot) != SLOT_FAMILIES.get(row.slot_key, row.slot_key):
                continue
            if not slot_matches(variant, slot, class_name, spec_name) or not spec_matches(
                variant.item, class_name, spec_name, variant, slot,
            ):
                continue
            result[slot].extend(_variant_candidates(
                variant, class_name, spec_name, row.id, row.selected_stats or (), row.quantity,
            ))
    return result


def _fixed_entries(raw_equipment, class_name, spec_name):
    entries = {}
    raw_equipment = raw_equipment if isinstance(raw_equipment, dict) else {}
    variant_ids = []
    for row in raw_equipment.values():
        if isinstance(row, dict):
            variant_ids.append(int((row.get('variant') or {}).get('id') or row.get('variant_id') or 0))
            variant_ids.append(int(((row.get('embellishment') or {}).get('variant') or {}).get('id') or 0))
            variant_ids.append(int(((row.get('enchant') or {}).get('variant') or {}).get('id') or 0))
            variant_ids.extend(int((gem.get('variant') or {}).get('id') or 0) for gem in (row.get('gems') or []) if isinstance(gem, dict))
    variants = {
        row.id: row for row in WowItemVariantSnapshot.objects.filter(id__in=variant_ids).select_related('item')
    }
    for slot, row in raw_equipment.items():
        if slot not in SLOT_LABELS or not isinstance(row, dict):
            continue
        variant_id = int((row.get('variant') or {}).get('id') or row.get('variant_id') or 0)
        variant = variants.get(variant_id)
        if not variant or not slot_matches(variant, slot, class_name, spec_name):
            continue
        selected = row.get('selectedStats') or row.get('selected_stats') or []
        try:
            candidate = _variant_candidates(variant, class_name, spec_name, selected_stats=selected)[0]
        except GearBuilderError:
            options = variant.crafting_options if isinstance(variant.crafting_options, dict) else {}
            pool = [value for value in (options.get('stat_pool') or SECONDARY) if value in SECONDARY]
            candidate = _candidate(
                variant, class_name, spec_name,
                pool[:max(1, int(options.get('stat_count') or 2))],
            )
        candidate['owned_id'] = -1  # 用户主动锁定的装备不计入缺失清单。
        candidate['fixed_enhancements'] = {
            'embellishment': variants.get(int((((row.get('embellishment') or {}).get('variant') or {}).get('id')) or 0)),
            'enchant': variants.get(int((((row.get('enchant') or {}).get('variant') or {}).get('id')) or 0)),
            'gems': [variants.get(int((gem.get('variant') or {}).get('id') or 0)) for gem in (row.get('gems') or []) if isinstance(gem, dict)],
            'added_socket': bool(row.get('addedSocket') or row.get('added_socket')),
        }
        candidate['fixed_enhancements']['gems'] = [value for value in candidate['fixed_enhancements']['gems'] if value]
        entries[slot] = candidate
    return entries


def _compatible(state, candidate, slot, identity):
    owned_id = candidate.get('owned_id')
    if owned_id and owned_id > 0 and state['owned'].get(owned_id, 0) >= candidate.get('owned_quantity', 1):
        return False
    group = candidate.get('unique_group')
    if group and candidate.get('max_equipped') and state['unique'].get(group, 0) >= candidate['max_equipped']:
        return False
    if slot == 'off_hand':
        main = state['equipment'].get('main_hand')
        if main and main.get('two_handed') and identity not in {'Warrior:Fury'}:
            return False
    if slot == 'main_hand' and candidate.get('two_handed') and identity not in {'Warrior:Fury'}:
        offhand = state['equipment'].get('off_hand')
        if offhand:
            return False
    return True


def _beam_plan(mode, current_variants, owned, fixed, class_name, spec_name, target, conversion):
    identity = f'{class_name}:{spec_name}'
    pools = {}
    for slot, _label in EQUIPMENT_SLOTS:
        if slot in fixed:
            continue
        normal = []
        for variant in current_variants:
            if mode == 'dungeon' and 'mythic_plus' not in _source_types(variant):
                continue
            if not slot_matches(variant, slot, class_name, spec_name) or not spec_matches(
                variant.item, class_name, spec_name, variant, slot,
            ):
                continue
            normal.extend(_variant_candidates(variant, class_name, spec_name))
        pools[slot] = (owned.get(slot) or normal) if mode == 'prefer_owned' else normal

    initial_stats = {key: 0.0 for key in SECONDARY}
    unique = defaultdict(int)
    for candidate in fixed.values():
        initial_stats = _add_stats(initial_stats, candidate['stats'])
        if candidate.get('unique_group'):
            unique[candidate['unique_group']] += 1
    beam = [{'equipment': dict(fixed), 'stats': initial_stats, 'unique': dict(unique), 'owned': {}}]
    target_ratings = _target_ratings(target, conversion)
    total_slots = len(EQUIPMENT_SLOTS)
    processed = len(fixed)
    for slot, _label in EQUIPMENT_SLOTS:
        if slot in fixed:
            continue
        choices = pools.get(slot) or [None]
        if slot == 'off_hand' and None not in choices:
            choices = [*choices, None]
        expanded = []
        for state in beam:
            for candidate in choices:
                if candidate is None:
                    expanded.append(state)
                    continue
                if not _compatible(state, candidate, slot, identity):
                    continue
                stats = _add_stats(state['stats'], candidate['stats'])
                equipment = {**state['equipment'], slot: candidate}
                next_unique = dict(state['unique'])
                if candidate.get('unique_group'):
                    next_unique[candidate['unique_group']] = next_unique.get(candidate['unique_group'], 0) + 1
                next_owned = dict(state['owned'])
                if candidate.get('owned_id') and candidate['owned_id'] > 0:
                    next_owned[candidate['owned_id']] = next_owned.get(candidate['owned_id'], 0) + 1
                expanded.append({'equipment': equipment, 'stats': stats, 'unique': next_unique, 'owned': next_owned})
        if not expanded:
            expanded = beam
        processed += 1
        progress = processed / total_slots
        expanded.sort(key=lambda row: sum(
            ((row['stats'][key] - target_ratings[key] * progress) / max(1, target_ratings[key], 250)) ** 2
            for key in SECONDARY
        ))
        beam = expanded[:600]
    if not beam:
        return {'equipment': dict(fixed), 'stats': initial_stats}
    return min(beam, key=lambda row: _distance(row['stats'], target, conversion))


def _enhancement_variants(season):
    rows = WowItemVariantSnapshot.objects.filter(
        season=season,
        batch_key=season.gear_batch_key,
        variant_type__in=(WowItemVariantSnapshot.TYPE_EMBELLISHMENT, WowItemVariantSnapshot.TYPE_GEM, WowItemVariantSnapshot.TYPE_ENCHANT),
    ).select_related('item').order_by('-crafting_quality', '-item__quality', '-item_id')
    highest = {}
    for row in rows:
        if row.variant_type != WowItemVariantSnapshot.TYPE_EMBELLISHMENT and int(row.item.quality or 0) < 3:
            continue
        metadata = row.metadata if isinstance(row.metadata, dict) else {}
        family = str(metadata.get('simc_name') or row.item.simc_token or row.item.name or row.item_id).casefold()
        family = family.rsplit('_', 1)[0] if family.rsplit('_', 1)[-1].isdigit() else family
        highest.setdefault((row.variant_type, family), row)
    result = defaultdict(list)
    for row in highest.values():
        result[row.variant_type].append(row)
    return result


def _best_stat_option(options, stats, target, conversion):
    best = None
    best_score = _distance(stats, target, conversion)
    for row in options:
        candidate_stats = _add_stats(stats, normalize_stats(row.stats_json))
        score = _distance(candidate_stats, target, conversion)
        if score < best_score:
            best, best_score = row, score
    return best


def _apply_enhancements(
    plan, season, class_name, spec_name, target, conversion,
    include_gems, include_enchants, lock_gems=True, lock_enchants=True,
):
    variants = _enhancement_variants(season)
    stats = dict(plan['stats'])
    enhancements = {}
    embellishment_count = 0
    for slot, candidate in plan['equipment'].items():
        variant = candidate['variant']
        payload = serialize_variant(variant, class_name, spec_name)
        fixed = candidate.get('fixed_enhancements') or {}
        preserve_gems = lock_gems or not include_gems
        preserve_enchant = lock_enchants or not include_enchants
        slot_data = {
            'embellishment': fixed.get('embellishment'),
            'gems': list(fixed.get('gems') or []) if preserve_gems else [],
            'enchant': fixed.get('enchant') if preserve_enchant else None,
            'added_socket': bool(fixed.get('added_socket')),
        }
        if slot_data['embellishment']:
            stats = _add_stats(stats, normalize_stats(slot_data['embellishment'].stats_json))
            embellishment_count += 1
        elif variant.variant_type == WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT and embellishment_count < 2:
            compatible = [row for row in variants[WowItemVariantSnapshot.TYPE_EMBELLISHMENT]
                          if slot_matches(row, slot, class_name, spec_name)]
            embellishment = _best_stat_option(compatible, stats, target, conversion)
            if embellishment:
                stats = _add_stats(stats, normalize_stats(embellishment.stats_json))
                slot_data['embellishment'] = embellishment
                embellishment_count += 1
        for gem in slot_data['gems']:
            stats = _add_stats(stats, normalize_stats(gem.stats_json))
        if slot_data['enchant']:
            stats = _add_stats(stats, normalize_stats(slot_data['enchant'].stats_json))
        if include_gems:
            capacity = int(payload.get('socket_count') or 0)
            if slot_data['added_socket']:
                capacity += 1
            elif slot in ADDITIONAL_SOCKET_SLOTS:
                capacity += 1
                slot_data['added_socket'] = True
            compatible = [row for row in variants[WowItemVariantSnapshot.TYPE_GEM]
                          if slot_matches(row, slot, class_name, spec_name)]
            for _index in range(max(0, capacity - len(slot_data['gems']))):
                gem = _best_stat_option(compatible, stats, target, conversion)
                if not gem:
                    break
                stats = _add_stats(stats, normalize_stats(gem.stats_json))
                slot_data['gems'].append(gem)
        if include_enchants and not slot_data['enchant']:
            compatible = [row for row in variants[WowItemVariantSnapshot.TYPE_ENCHANT]
                          if slot_matches(row, slot, class_name, spec_name)
                          and spec_matches(row.item, class_name, spec_name, row, slot)]
            enchant = _best_stat_option(compatible, stats, target, conversion)
            if enchant:
                stats = _add_stats(stats, normalize_stats(enchant.stats_json))
                slot_data['enchant'] = enchant
        enhancements[slot] = slot_data
    plan['stats'] = stats
    plan['enhancements'] = enhancements


def _choose_flask(plan, target, conversion, flask_key):
    choices = list(FLASKS.values()) if flask_key == 'auto' else [FLASKS.get(flask_key, FLASKS['none'])]
    best = min(choices, key=lambda row: _distance(_add_stats(plan['stats'], row['stats']), target, conversion))
    plan['stats'] = _add_stats(plan['stats'], best['stats'])
    plan['flask'] = best


def _source_label(variant):
    sources = [localize_gear_source(row) for row in (variant.source_json or []) if isinstance(row, dict)]
    if not sources:
        return '来源待补充'
    row = sources[0]
    values = [row.get('type_zh'), row.get('instance_zh'), row.get('encounter_zh'), row.get('difficulty_zh'), row.get('profession_zh')]
    return ' · '.join(str(value) for value in values if value) or '来源待补充'


def _serialize_plan(mode, plan, target, conversion, class_name, spec_name):
    equipment = {}
    missing = []
    owned_count = 0
    for slot, candidate in plan['equipment'].items():
        variant = candidate['variant']
        item = serialize_item(variant.item, [variant], class_name, spec_name)
        variant_payload = serialize_variant(variant, class_name, spec_name)
        enhancement = plan.get('enhancements', {}).get(slot, {})
        gems = []
        for gem in enhancement.get('gems') or []:
            gems.append({'item': serialize_item(gem.item, [gem], class_name, spec_name), 'variant': serialize_variant(gem, class_name, spec_name)})
        enchant = enhancement.get('enchant')
        embellishment = enhancement.get('embellishment')
        equipment[slot] = {
            'item': item,
            'variant': variant_payload,
            'itemLevel': variant.item_level,
            'selectedStats': candidate.get('selected_stats') or [],
            'resolvedStats': candidate.get('stats') or None,
            'resolvedEffects': candidate.get('effects') or None,
            'embellishment': {'item': serialize_item(embellishment.item, [embellishment], class_name, spec_name), 'variant': serialize_variant(embellishment, class_name, spec_name)} if embellishment else None,
            'gems': gems,
            'enchant': {'item': serialize_item(enchant.item, [enchant], class_name, spec_name), 'variant': serialize_variant(enchant, class_name, spec_name)} if enchant else None,
            'addedSocket': bool(enhancement.get('added_socket')),
            'external': False,
        }
        if candidate.get('owned_id'):
            owned_count += 1
        else:
            missing.append({
                'slot': slot,
                'slot_label': SLOT_LABELS.get(slot, slot),
                'name': item['name'],
                'item_level': variant.item_level,
                'source': _source_label(variant),
            })
    percentages = _percentages(plan['stats'], conversion)
    return {
        'key': mode,
        'name': PLAN_LABELS[mode],
        'distance': round(_distance(plan['stats'], target, conversion), 2),
        'stats': {key: round(float(plan['stats'].get(key) or 0), 2) for key in SECONDARY},
        'percentages': {key: round(value, 2) for key, value in percentages.items()},
        'equipment': equipment,
        'owned_count': owned_count,
        'equipped_count': len(equipment),
        'missing_items': missing,
        'flask': plan.get('flask') or FLASKS['none'],
    }


def _fallback_explanation(plans):
    best = min(plans, key=lambda row: row['distance'])
    return f"最接近目标的是“{best['name']}”，综合偏差 {best['distance']}。优先已有装备方案会先锁定可用的已有物品，另外两套方案更适合比较潜在提升与需要补齐的来源。"


def _ai_explanation(plans, target):
    from core.glm import GLMClient
    summary = [{
        '方案': row['name'], '偏差': row['distance'], '最终百分比': row['percentages'],
        '已有件数': row['owned_count'], '缺失装备': len(row['missing_items']),
    } for row in plans]
    prompt = (
        '你是魔兽世界配装助手。只基于下面确定性计算结果，用中文写120字以内的比较建议；'
        '不得新增装备、数值或来源。\n'
        f'目标={target}\n方案={summary}'
    )
    return (GLMClient().send_message(prompt, max_tokens=220, thinking_type='disabled') or '').strip()


def assistant_bootstrap(user, class_name='Warrior', spec_name='Fury'):
    class_name, spec_name = canonical_spec(class_name, spec_name)
    season = active_season()
    return {
        'class_name': class_name,
        'spec_name': spec_name,
        'catalog': {
            'available': bool(season and season.gear_batch_key),
            'batch_key': season.gear_batch_key if season else '',
            'season_name': season.season_name if season else '',
        },
        'owned_items': list_owned_items(user, class_name=class_name, spec_name=spec_name),
        'flasks': [{'key': 'auto', 'name': '自动选择最接近目标'}, *FLASKS.values()],
    }


def optimize_loadouts(user, payload):
    class_name, spec_name = canonical_spec(payload.get('class_name') or 'Warrior', payload.get('spec_name') or 'Fury')
    raw_target = payload.get('target') if isinstance(payload.get('target'), dict) else {}
    target = {key: max(0, min(200, float(raw_target.get(key) or 0))) for key in SECONDARY}
    if not any(target.values()):
        raise GearBuilderError('请至少填写一个目标属性百分比')
    current, season = _current_pool(class_name, spec_name)
    conversion = _conversion(class_name, spec_name)
    fixed = _fixed_entries(payload.get('equipment'), class_name, spec_name)
    owned = _owned_pool(user, class_name, spec_name)
    plans = []
    for mode in ('prefer_owned', 'all', 'dungeon'):
        plan = _beam_plan(mode, current, owned, fixed, class_name, spec_name, target, conversion)
        _apply_enhancements(
            plan, season, class_name, spec_name, target, conversion,
            bool(payload.get('include_gems', True)), bool(payload.get('include_enchants', True)),
            bool(payload.get('lock_gems', True)), bool(payload.get('lock_enchants', True)),
        )
        _choose_flask(plan, target, conversion, str(payload.get('flask') or 'auto'))
        plans.append(_serialize_plan(mode, plan, target, conversion, class_name, spec_name))
    explanation = _fallback_explanation(plans)
    ai_used = False
    if payload.get('use_ai'):
        try:
            ai_text = _ai_explanation(plans, target)
            if ai_text:
                explanation = ai_text
                ai_used = True
        except Exception:
            pass
    return {
        'target': target,
        'plans': plans,
        'explanation': explanation,
        'ai_used': ai_used,
        'fixed_slots': list(fixed),
        'catalog': {'batch_key': season.gear_batch_key, 'season_name': season.season_name},
    }
