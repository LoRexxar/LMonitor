"""登录用户的辅助配装：确定性组合搜索与可选 AI 解释。"""

from __future__ import annotations

import re

from collections import defaultdict
from heapq import heappush, heapreplace
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
    embellishment_eligibility_reason,
    normalize_stats,
    secondary_stat_conversion_rules,
    serialize_item,
    serialize_variant,
    slot_matches,
    spec_matches,
    stats_for_identity,
)
from botend.services.gear_builder_owned import list_owned_items
from botend.services.wow_item_display import _effect_text


SECONDARY = ('crit', 'haste', 'mastery', 'versatility')
PLAN_LABELS = {
    'prefer_owned': '优先备选装备',
    'all': '全装备池',
    'dungeon': '不含团本装备',  # Retain the public mode key for saved clients.
}
SOURCE_PREFERENCE_LABELS = {'none': '不偏好来源', 'raid': '优先团本', 'mythic_plus': '优先大秘境'}
# Server-owned placement policy, not item-ID exceptions or UI ordering.
EMBELLISHMENT_SLOT_PRIORITY = {
    'wrists': 0, 'back': 0,
    'neck': 1, 'finger1': 1, 'finger2': 1,
    'shoulders': 2, 'hands': 2, 'waist': 2, 'feet': 2,
    'trinket1': 3, 'trinket2': 3,
    'head': 4, 'chest': 4, 'legs': 4, 'main_hand': 5, 'off_hand': 5,
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
    from botend.services.gear_builder_tier_sources import tier_set_sources
    sources = getattr(variant, '_assistant_sources', None)
    if sources is None:
        sources = tier_set_sources({**(variant.item.metadata or {}), **(variant.metadata or {})}, variant.item.slot_key)
    if sources is None:
        sources = variant.source_json or []
    return {str(row.get('type') or '').casefold() for row in sources
            if isinstance(row, dict) and row.get('type')}


def _acquisition_type(variant, mode='all', preference='none', *, recommendation=False):
    """Choose only an evidenced source; an alternative avoids spending delve quota."""
    # Generic reward mechanisms cannot certify an independent acquisition route.
    sources = _source_types(variant) - {'great_vault', 'bonus_roll', 'unknown'}
    if mode == 'dungeon':
        sources = sources - {'raid'}
    # Crafting is an authoritative variant type even in old catalogs without a source row.
    if variant.variant_type == WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT:
        sources = sources | {'crafted'}
    if recommendation:
        sources = {source for source in sources
                   if _source_track_is_valid(variant, source_types={source})
                   and (source != 'delve' or str(variant.upgrade_track or '').casefold() in {'myth', 'hero'})}
    alternatives = sources - {'delve'}
    if alternatives:
        return preference if preference in alternatives else sorted(alternatives)[0]
    return 'delve' if 'delve' in sources else ''


def _delve_cost(variant, source):
    return int(source == 'delve' and str(variant.upgrade_track or '').casefold() == 'myth')


def _has_equipment_effect(effects):
    # Set descriptions are repeated on every piece; they are not independent
    # per-item effects (a fifth tier piece must not gain fake effect priority).
    set_bonus = re.compile(r'^\s*[（(]\d+[)）]\s*(?:组合|套装|Set\b)', re.IGNORECASE)
    return any(text and not set_bonus.match(text) for text in map(_effect_text, effects))


def _candidate(variant, class_name, spec_name, selected_stats=(), owned_id=None, owned_quantity=1):
    stats = stats_for_identity(variant.stats_json, variant.metadata, class_name, spec_name)
    selected_stats = list(selected_stats or [])
    if variant.variant_type == WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT:
        stats, selected_stats, effects = _resolve_crafted_rows(
            variant, selected_stats, None, class_name, spec_name,
        )
    else:
        # Use Gear Builder's canonical separation, not raw JSON: ordinary
        # green-stat lines and empty effect rows are not special effects.
        effects = serialize_variant(variant, class_name, spec_name)['effects']
    return {
        'variant': variant,
        'stats': {key: float(stats.get(key) or 0) for key in SECONDARY},
        'selected_stats': selected_stats,
        'effects': effects,
        'effect_count': int(_has_equipment_effect(effects)),
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


def _current_pool(class_name, spec_name, *, allow_mythic_last_two=True):
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
    from botend.services.gear_assistant_sources import (
        mythic_raid_final_encounters, obtainable_without_mythic_last_two,
    )
    raids = mythic_raid_final_encounters() if not allow_mythic_last_two else {}
    # 先限制获取来源，再保留同一物品的最高可获取装等，不能丢掉低难度回退。
    best = {}
    for variant in rows:
        if not _source_track_is_valid(variant):
            continue
        if not allow_mythic_last_two:
            from botend.services.gear_builder_tier_sources import tier_set_sources
            sources = tier_set_sources({**(variant.item.metadata or {}), **(variant.metadata or {})}, variant.item.slot_key)
            if sources is None:
                sources = variant.source_json or []
            variant._assistant_sources = [source for source in sources
                                          if obtainable_without_mythic_last_two(variant, raids, sources=[source])]
            if not obtainable_without_mythic_last_two(variant, raids, sources=variant._assistant_sources):
                continue
        # Preserve distinct acquisition budgets: a lower nonraid/hero fallback
        # may be the highest *obtainable* choice after the plan's global caps.
        source = _acquisition_type(variant, recommendation=True)
        nonraid_source = _acquisition_type(variant, 'dungeon', recommendation=True)
        if not source:
            continue
        key = (variant.item_id, variant.variant_type, bool(nonraid_source),
               _delve_cost(variant, source), _delve_cost(variant, nonraid_source))
        best.setdefault(key, variant)
    return list(best.values()), season


def _owned_pool(user, class_name, spec_name):
    result = defaultdict(list)
    rows = GearBuilderOwnedItem.objects.filter(user=user, variant__isnull=False).select_related('variant__item')
    rows = list(rows)
    from botend.services.gear_builder import current_variants
    replacements = current_variants(row.variant for row in rows)
    for row in rows:
        variant = replacements[row.variant_id]
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
    from botend.services.gear_builder import current_variants
    variants = current_variants(variants.values())
    for slot, row in raw_equipment.items():
        if slot not in SLOT_LABELS or not isinstance(row, dict):
            continue
        variant_id = int((row.get('variant') or {}).get('id') or row.get('variant_id') or 0)
        variant = variants.get(variant_id)
        if not variant:
            continue
        if not slot_matches(variant, slot, class_name, spec_name) or not spec_matches(
            variant.item, class_name, spec_name, variant, slot,
        ):
            raise GearBuilderError(f'锁定的{SLOT_LABELS.get(slot, slot)}不适用于当前职业专精')
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
        if row.get('embellishment') and not candidate['fixed_enhancements']['embellishment']:
            raise GearBuilderError(f'锁定的{SLOT_LABELS.get(slot, slot)}美化资料不可用')
        entries[slot] = candidate
    _validate_embellishments(entries, class_name, spec_name)
    return entries


def _attachment(candidate):
    return candidate.get('selected_embellishment') or (candidate.get('fixed_enhancements') or {}).get('embellishment')


def _embellishment_cost(candidate):
    return int(bool(candidate['variant'].is_intrinsic_embellishment)) + int(bool(_attachment(candidate)))


def _candidate_options(candidate, slot, materials, class_name, spec_name, target, conversion, cache):
    """Search attachment placement with the carrier, not as a late greedy patch.

    All compatible reagents are considered, but only one representative per
    carrier/stat choice enters the beam: reagents share the same capacity cost.
    This bounds material fanout, including zero-static-stat proc materials.
    """
    variant = candidate['variant']
    attachment = _attachment(candidate)
    base = dict(candidate)
    if attachment:
        key = ('material-effect', attachment.id)
        if key not in cache:
            cache[key] = int(_has_equipment_effect(serialize_variant(attachment, class_name, spec_name)['effects']))
        base['effect_count'] = max(base['effect_count'], cache[key])
    result = [base]
    if _embellishment_cost(base) or variant.variant_type != WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT:
        return result
    key = ('compatible', variant.id, slot)
    if key not in cache:
        cache[key] = [row for row in materials if embellishment_eligibility_reason(
            variant, row, slot, class_name, spec_name) is None]
    compatible = cache[key]
    if not compatible:
        return result
    for row in compatible:
        key = ('material-effect', row.id)
        if key not in cache:
            cache[key] = int(_has_equipment_effect(serialize_variant(row, class_name, spec_name)['effects']))
    best = min(compatible, key=lambda row: (
        -max(base['effect_count'], cache[('material-effect', row.id)]),
        _distance(_add_stats(base['stats'], normalize_stats(row.stats_json)), target, conversion),
        -int(row.crafting_quality or 0), row.id,
    ))
    result.append({**base, 'selected_embellishment': best,
                   'effect_count': max(base['effect_count'], cache[('material-effect', best.id)])})
    return result


def _validate_embellishments(equipment, class_name, spec_name):
    count = sum(_embellishment_cost(candidate) for candidate in equipment.values())
    if count > 2:
        raise GearBuilderError('美化（含固有美化和附加美化）最多只能携带 2 件，请调整锁定装备')
    for slot, candidate in equipment.items():
        embellishment = _attachment(candidate)
        if embellishment:
            reason = embellishment_eligibility_reason(
                candidate['variant'], embellishment, slot, class_name, spec_name,
            )
            if reason:
                raise GearBuilderError(f'锁定的{SLOT_LABELS.get(slot, slot)}：{reason["reason"]}')
    return count


def _compatible(state, candidate, slot, identity):
    # Item identity spans upgrade variants, crafted stats, and owned copies.
    if candidate['variant'].item_id in state['item_ids']:
        return False
    if state['embellishment_count'] + _embellishment_cost(candidate) > 2:
        return False
    if state['delve_myth_count'] + candidate.get('delve_myth_cost', 0) > 2:
        return False
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
        # 完整配装要求副手槽也有实际装备；非泰坦之握专精的双手主手
        # 无法继续形成覆盖全部槽位的组合，因此不要让它进入搜索束。
        return False
    return True


def _extend_state(state, candidate, slot):
    unique = dict(state['unique'])
    if candidate.get('unique_group'):
        group = candidate['unique_group']
        unique[group] = unique.get(group, 0) + 1
    owned = dict(state['owned'])
    if candidate.get('owned_id') and candidate['owned_id'] > 0:
        owned_id = candidate['owned_id']
        owned[owned_id] = owned.get(owned_id, 0) + 1
    attachment = _attachment(candidate)
    stats = _add_stats(state['stats'], candidate['stats'])
    if attachment:
        stats = _add_stats(stats, normalize_stats(attachment.stats_json))
    return {
        'equipment': {**state['equipment'], slot: candidate},
        'item_ids': state['item_ids'] | {candidate['variant'].item_id},
        'stats': stats,
        'unique': unique, 'owned': owned,
        'effect_count': state['effect_count'] + candidate['effect_count'],
        'embellishment_count': state['embellishment_count'] + _embellishment_cost(candidate),
        'embellishment_slot_cost': state['embellishment_slot_cost'] + _embellishment_cost(candidate) * EMBELLISHMENT_SLOT_PRIORITY[slot],
        'delve_myth_count': state['delve_myth_count'] + candidate.get('delve_myth_cost', 0),
        'delve_lower_count': state['delve_lower_count'] + candidate.get('delve_lower_cost', 0),
        'source_preference_count': state['source_preference_count'] + candidate.get('source_preference_match', 0),
        'total_item_level': state['total_item_level'] + int(candidate['variant'].item_level or 0),
    }


def _prune_beam(states, rank, width=600):
    """Bound memory while preserving paths with spare embellishment capacity.

    A global top-N can discard spare embellishment/delve capacity before later
    mandatory items. Keep at most N in each of nine joint capacity buckets,
    then share N final places across them; expansions are streamed, not stored.
    """
    buckets = defaultdict(list)
    for index, state in enumerate(states):
        entry = (tuple(-value for value in rank(state)), -index, state)
        bucket = buckets[(state['embellishment_count'], state['delve_myth_count'])]
        if len(bucket) < width:
            heappush(bucket, entry)
        elif entry[:2] > bucket[0][:2]:
            heapreplace(bucket, entry)
    ordered = [sorted(bucket, reverse=True) for bucket in buckets.values()]
    result = []
    for index in range(width):
        for bucket in ordered:
            if index < len(bucket):
                result.append(bucket[index][2])
                if len(result) == width:
                    return result
    return result


def _beam_plan(mode, current_variants, owned, fixed, class_name, spec_name, target, conversion,
               *, materials=(), source_preference='none', cache=None):
    identity = f'{class_name}:{spec_name}'
    preference = 'none' if mode == 'dungeon' else source_preference
    cache = {} if cache is None else cache
    _validate_embellishments(fixed, class_name, spec_name)

    def prepare(candidate, slot, locked=False):
        # Locked items retain their proven acquisition alternatives, even in
        # nonraid mode; they are never silently unlocked/replaced.
        source = _acquisition_type(candidate['variant'], 'all' if locked else mode, preference,
                                   recommendation=not locked and not candidate.get('owned_id'))
        result = []
        for option in _candidate_options(candidate, slot, materials, class_name, spec_name, target, conversion, cache):
            cost = _delve_cost(option['variant'], source)
            result.append({**option, 'acquisition_source_type': source,
                           'delve_myth_cost': cost,
                           'delve_lower_cost': int(source == 'delve' and not cost),
                           'source_preference_match': int(preference != 'none' and source == preference)})
        return result

    # Filter per-mode sources before highest-level dedup. Delve myth and its
    # lower-budget fallback are separate obtainable choices under the cap.
    available = {}
    for variant in sorted(current_variants, key=lambda row: (-row.item_level, -row.crafting_quality, row.id)):
        source = _acquisition_type(variant, mode, preference, recommendation=True)
        if not source:
            continue
        key = (variant.item_id, variant.variant_type, _delve_cost(variant, source))
        available.setdefault(key, variant)
    pools = {}
    for slot, _label in EQUIPMENT_SLOTS:
        if slot in fixed:
            pools[slot] = prepare(fixed[slot], slot, locked=True)
            continue
        normal = []
        for variant in available.values():
            if not slot_matches(variant, slot, class_name, spec_name) or not spec_matches(
                variant.item, class_name, spec_name, variant, slot,
            ):
                continue
            key = ('candidates', variant.id)
            if key not in cache:
                cache[key] = _variant_candidates(variant, class_name, spec_name)
            normal.extend(cache[key])
        candidates = [*(owned.get(slot) or []), *normal] if mode == 'prefer_owned' else normal
        pools[slot] = [option for candidate in candidates for option in prepare(candidate, slot)]

    def empty_state():
        return {'equipment': {}, 'item_ids': frozenset(), 'stats': {key: 0.0 for key in SECONDARY}, 'unique': {}, 'owned': {},
                'effect_count': 0, 'embellishment_count': 0, 'embellishment_slot_cost': 0,
                'delve_myth_count': 0, 'delve_lower_count': 0, 'source_preference_count': 0,
                'total_item_level': 0}

    # Validate/reserve *all* original locks before searching optional additions.
    reserved = empty_state()
    for slot, _label in EQUIPMENT_SLOTS:
        if slot not in fixed:
            continue
        candidate = pools[slot][0]
        if candidate['variant'].item_id in reserved['item_ids']:
            raise GearBuilderError(
                f'锁定装备重复：同一物品只能装备 1 次（物品 ID {candidate["variant"].item.item_id}），请调整锁定装备'
            )
        if reserved['delve_myth_count'] + candidate['delve_myth_cost'] > 2:
            raise GearBuilderError('锁定的地下堡神话装备最多只能携带 2 件，请调整锁定装备')
        if not _compatible(reserved, candidate, slot, identity):
            raise GearBuilderError(f'锁定的{SLOT_LABELS.get(slot, slot)}与完整配装约束冲突，未生成不完整方案')
        reserved = _extend_state(reserved, candidate, slot)

    slots = [slot for slot, _ in EQUIPMENT_SLOTS if slot in fixed]
    slots += [slot for slot, _ in EQUIPMENT_SLOTS if slot not in fixed]
    for slot in slots:
        if not pools[slot]:
            raise GearBuilderError(f'“{PLAN_LABELS[mode]}”无法为{SLOT_LABELS.get(slot, slot)}找到可用装备，未生成不完整方案')
    # Optimistic remaining capacities cheaply eliminate impossible 0/1 paths
    # before pruning, while keeping both capacity dimensions in the beam.
    remaining_min, remaining_max, remaining_delve = [0], [0], [0]
    for slot in reversed(slots):
        costs = [_embellishment_cost(row) for row in pools[slot]]
        remaining_min.append(remaining_min[-1] + min(costs))
        remaining_max.append(remaining_max[-1] + max(costs))
        remaining_delve.append(remaining_delve[-1] + min(row['delve_myth_cost'] for row in pools[slot]))
    if remaining_max[-1] < 2:
        raise GearBuilderError(f'“{PLAN_LABELS[mode]}”无法满足美化必须携带 2 件，请检查锁定装备与制造配方资料')

    def priorities(row):
        return (-row['effect_count'], -row['source_preference_count'],
                -sum(row['owned'].values()) if mode == 'prefer_owned' else 0,
                row['embellishment_slot_cost'], row['delve_lower_count'])

    beam = [empty_state()]
    target_ratings = _target_ratings(target, conversion)
    for index, slot in enumerate(slots):
        progress = (index + 1) / len(slots)
        remaining = len(slots) - index - 1
        expanded = (
            _extend_state(state, candidate, slot)
            for state in beam for candidate in pools[slot]
            if _compatible(state, candidate, slot, identity)
            and state['embellishment_count'] + _embellishment_cost(candidate) + remaining_min[remaining] <= 2
            and state['embellishment_count'] + _embellishment_cost(candidate) + remaining_max[remaining] >= 2
            and state['delve_myth_count'] + candidate['delve_myth_cost'] + remaining_delve[remaining] <= 2
        )
        beam = _prune_beam(expanded, lambda row: (*priorities(row), sum(
            ((row['stats'][key] - target_ratings[key] * progress) / max(1, target_ratings[key], 250)) ** 2
            for key in SECONDARY), -row['total_item_level']))
        if not beam:
            raise GearBuilderError(
                f'“{PLAN_LABELS[mode]}”无法满足{SLOT_LABELS.get(slot, slot)}的装备约束'
                '（同一物品只能装备 1 次、美化必须携带 2 件、地下堡神话最多 2 件），未生成不完整方案'
            )
    plan = min(beam, key=lambda row: (*priorities(row), _distance(row['stats'], target, conversion), -row['total_item_level']))
    plan['embellishments_in_stats'] = True
    plan['source_preference'] = preference
    return plan


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
    include_gems, include_enchants, lock_gems=True, lock_enchants=True, *, variants=None,
):
    variants = _enhancement_variants(season) if variants is None else variants
    stats = dict(plan['stats'])
    enhancements = {}
    # Reserve every intrinsic item and locked attachment before greedy additions,
    # including attachments on slots that occur later in iteration order.
    embellishment_count = _validate_embellishments(plan['equipment'], class_name, spec_name)
    for slot, candidate in sorted(plan['equipment'].items(), key=lambda row: EMBELLISHMENT_SLOT_PRIORITY[row[0]]):
        variant = candidate['variant']
        payload = serialize_variant(variant, class_name, spec_name)
        fixed = candidate.get('fixed_enhancements') or {}
        preserve_gems = lock_gems or not include_gems
        preserve_enchant = lock_enchants or not include_enchants
        slot_data = {
            'embellishment': _attachment(candidate),
            'gems': list(fixed.get('gems') or []) if preserve_gems else [],
            'enchant': fixed.get('enchant') if preserve_enchant else None,
            'added_socket': bool(fixed.get('added_socket')),
        }
        if slot_data['embellishment']:
            if not plan.get('embellishments_in_stats'):
                stats = _add_stats(stats, normalize_stats(slot_data['embellishment'].stats_json))
        elif (variant.variant_type == WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT
              and not variant.is_intrinsic_embellishment and embellishment_count < 2):
            compatible = [row for row in variants[WowItemVariantSnapshot.TYPE_EMBELLISHMENT]
                          if embellishment_eligibility_reason(variant, row, slot, class_name, spec_name) is None]
            embellishment = min(compatible, key=lambda row: (
                -int(_has_equipment_effect(serialize_variant(row, class_name, spec_name)['effects'])),
                _distance(_add_stats(stats, normalize_stats(row.stats_json)), target, conversion),
            ), default=None)
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
    if embellishment_count != 2:
        raise GearBuilderError('美化必须携带 2 件，当前装备与制造配方无法满足，未生成不完整方案')
    plan['stats'] = stats
    plan['enhancements'] = enhancements
    plan['embellishment_count'] = embellishment_count


def _choose_flask(plan, target, conversion, flask_key):
    choices = list(FLASKS.values()) if flask_key == 'auto' else [FLASKS.get(flask_key, FLASKS['none'])]
    best = min(choices, key=lambda row: _distance(_add_stats(plan['stats'], row['stats']), target, conversion))
    plan['stats'] = _add_stats(plan['stats'], best['stats'])
    plan['flask'] = best


def _acquisition_sources(variant, source_type='', canonical_sources=None):
    available = getattr(variant, '_assistant_sources', None)
    if available is None:
        available = canonical_sources if canonical_sources is not None else variant.source_json or []
    sources = [localize_gear_source(row) for row in available if isinstance(row, dict)
               and (not source_type or str(row.get('type') or '').casefold() == source_type)]
    if not sources and source_type:
        sources = [localize_gear_source({'type': source_type})]
    return sources


def _source_label(variant, source_type='', canonical_sources=None):
    sources = _acquisition_sources(variant, source_type, canonical_sources)
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
        source_label = _source_label(variant, candidate.get('acquisition_source_type', ''), variant_payload['sources'])
        owned_id = candidate.get('owned_id')
        equipment[slot] = {
            'slot_label': SLOT_LABELS.get(slot, slot),
            'acquisition_source_label': source_label,
            'acquisition_sources': _acquisition_sources(variant, candidate.get('acquisition_source_type', ''), variant_payload['sources']),
            'selection_origin': 'locked' if owned_id == -1 else 'owned' if owned_id else 'catalog',
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
            'acquisition_source_type': candidate.get('acquisition_source_type', ''),
            'delve_myth_cost': candidate.get('delve_myth_cost', 0),
        }
        if candidate.get('owned_id'):
            owned_count += 1
        else:
            missing.append({
                'slot': slot,
                'slot_label': SLOT_LABELS.get(slot, slot),
                'name': item['name'],
                'item_level': variant.item_level,
                'source': source_label,
            })
    percentages = _percentages(plan['stats'], conversion)
    return {
        'key': mode,
        'name': PLAN_LABELS[mode],
        'source_preference': plan['source_preference'],
        'source_preference_label': ('不应用来源偏好' if mode == 'dungeon'
                                    else SOURCE_PREFERENCE_LABELS[plan['source_preference']]),
        'delve_myth_count': plan['delve_myth_count'],
        'distance': round(_distance(plan['stats'], target, conversion), 2),
        'stats': {key: round(float(plan['stats'].get(key) or 0), 2) for key in SECONDARY},
        'percentages': {key: round(value, 2) for key, value in percentages.items()},
        'equipment': equipment,
        'owned_count': owned_count,
        'equipped_count': len(equipment),
        'effect_count': sum(candidate['effect_count'] for candidate in plan['equipment'].values()),
        'embellishment_count': plan['embellishment_count'],
        'average_item_level': round(sum(
            int(candidate['variant'].item_level or 0) for candidate in plan['equipment'].values()
        ) / max(1, len(equipment)), 2),
        'missing_items': missing,
        'flask': plan.get('flask') or FLASKS['none'],
    }


def _fallback_explanation(plans):
    best = min(plans, key=lambda row: row['distance'])
    return (
        '各方案先满足锁定、来源、专精、唯一装备、美化必须携带2件及地下堡神话最多2件的约束。'
        '先优先携带特效装备，再按来源偏好、备选件数（仅优先备选装备方案）、美化小部位、少用地下堡英雄装备、目标绿字排序；'
        '美化优先腕部/背部，其次颈部/戒指。不含团本装备方案允许其他来源且不应用来源偏好，锁定装备保留。'
        '目录同物品取获取限制内最高装等，地下堡神话达到上限时保留英雄回退，绿字偏差相同优先总装等更高。'
        f"本次绿字偏差最小的是“{best['name']}”，综合偏差 {best['distance']}。特效件数与绿字匹配不代表DPS提升。"
    )


def _ai_explanation(plans, target):
    from core.glm import GLMClient
    summary = [{
        '方案': row['name'], '偏差': row['distance'], '最终百分比': row['percentages'],
        '备选件数': row['owned_count'], '缺失装备': len(row['missing_items']),
        '特效装备件数': row['effect_count'], '美化件数': row['embellishment_count'],
        '平均装等': row['average_item_level'], '来源偏好': row['source_preference_label'],
        '地下堡神话件数': row['delve_myth_count'],
    } for row in plans]
    prompt = (
        '你是魔兽世界配装助手。只基于下面确定性计算结果，用中文写120字以内的比较建议；'
        '不得新增装备、数值或来源。账号装备库统一称为“备选装备”。\n'
        '排序先满足硬约束，再依次考虑特效装备、来源偏好、备选件数（仅优先备选方案）、美化小部位、少用地下堡英雄、目标绿字。'
        '不含团本方案允许其他来源且忽略来源偏好，锁定保留。美化必须2件，优先腕部/背部，其次颈部/戒指；地下堡神话最多2件。'
        '同物品取获取限制内最高装等，地下堡神话上限允许英雄回退。同绿字偏差优先总装等，件数及属性匹配不代表DPS提升。\n'
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
    allow_mythic_last_two = payload.get('allow_mythic_last_two', True)
    if not isinstance(allow_mythic_last_two, bool):
        raise GearBuilderError('M 后二获取开关必须为布尔值')
    source_preference = payload.get('source_preference', 'none')
    if not isinstance(source_preference, str) or source_preference not in SOURCE_PREFERENCE_LABELS:
        raise GearBuilderError('来源偏好必须是 none、raid 或 mythic_plus 中的一个，不能同时选择')
    current, season = _current_pool(class_name, spec_name, allow_mythic_last_two=allow_mythic_last_two)
    conversion = _conversion(class_name, spec_name)
    fixed = _fixed_entries(payload.get('equipment'), class_name, spec_name)
    owned = _owned_pool(user, class_name, spec_name)
    plans = []
    enhancements = _enhancement_variants(season)
    candidate_cache = {}
    for mode in ('prefer_owned', 'all', 'dungeon'):
        plan = _beam_plan(mode, current, owned, fixed, class_name, spec_name, target, conversion,
                          materials=enhancements[WowItemVariantSnapshot.TYPE_EMBELLISHMENT],
                          source_preference=source_preference, cache=candidate_cache)
        _apply_enhancements(
            plan, season, class_name, spec_name, target, conversion,
            bool(payload.get('include_gems', True)), bool(payload.get('include_enchants', True)),
            bool(payload.get('lock_gems', True)), bool(payload.get('lock_enchants', True)),
            variants=enhancements,
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
        'allow_mythic_last_two': allow_mythic_last_two,
        'source_preference': source_preference,
        'catalog': {'batch_key': season.gear_batch_key, 'season_name': season.season_name},
    }
