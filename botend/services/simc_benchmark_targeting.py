"""根据当前基准计划选择任务坐标或整件装备，保留比较所需的基准和对照。"""
from copy import deepcopy
import hashlib
import json
import re

from django.core.exceptions import ValidationError
from simc_equipment_control import ALIASES, candidate_swaps


def plan_hash(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def coordinate_key(coordinate):
    return plan_hash({key: coordinate[key] for key in ('spec_key', 'profile_key', 'scenario_key')})


def coordinate_label(coordinate):
    talent = (coordinate.get('resources') or {}).get('talent_string') or {}
    parts = [coordinate['spec_label'], coordinate['profile_label']]
    if talent.get('name'):
        parts.append(f"天赋：{talent['name']}（#{talent['id']}）")
    parts.append(coordinate['scenario_label'])
    return ' / '.join(parts)


def equipment_key(candidate):
    params = candidate.get('candidate_params') or {}
    if candidate.get('candidate_type') != 'gear_swap' or params.get('equipment_effect_control'):
        return None
    swaps = candidate_swaps(params)
    if not swaps:
        return None
    identity = sorted((ALIASES.get(row.get('slot'), row.get('slot')), row.get('item_id')) for row in swaps)
    return plan_hash(identity)


def rerun_options(plan):
    groups = {}
    coordinates = []
    for coordinate in plan['cases']:
        coordinates.append({'key': coordinate_key(coordinate),
            'label': coordinate_label(coordinate),
            'run_count': len(coordinate['candidates'])})
        for candidate in coordinate['candidates']:
            key = equipment_key(candidate)
            if key is None:
                continue
            group = groups.setdefault(key, {'key': key, 'label': re.sub(r'\s*·\s*\d+(?=\s*＋|$)', '',
                candidate['candidate_label']), 'candidate_keys': set(), 'item_levels': set()})
            group['candidate_keys'].add(candidate['candidate_key'])
            for swap in candidate_swaps(candidate['candidate_params']):
                if swap.get('item_level'):
                    group['item_levels'].add(swap['item_level'])
    return {'plan_hash': plan_hash(plan), 'coordinates': coordinates,
        'equipment': [{**row, 'candidate_keys': sorted(row['candidate_keys']),
                       'item_levels': sorted(row['item_levels'])} for row in groups.values()]}


def select_rerun_coordinates(plan, selection):
    if not isinstance(selection, dict) or set(selection) - {'coordinate_keys', 'equipment_keys'}:
        raise ValidationError({'selection': ['定向重跑范围无效']})
    options = rerun_options(plan)
    selected = {}
    for field, values in (('coordinate_keys', options['coordinates']), ('equipment_keys', options['equipment'])):
        keys = selection.get(field, [])
        if (not isinstance(keys, list) or len(keys) > len(values)
                or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys)
                or set(keys) - {row['key'] for row in values}):
            raise ValidationError({'selection': ['重跑范围已失效，请重新选择任务或装备']})
        selected[field] = set(keys)
    if not any(selected.values()):
        raise ValidationError({'selection': ['请至少选择一个任务或一件装备']})
    rows = []
    for coordinate in plan['cases']:
        if selected['coordinate_keys'] and coordinate_key(coordinate) not in selected['coordinate_keys']:
            continue
        candidates = coordinate['candidates']
        if selected['equipment_keys']:
            chosen = [candidate for candidate in candidates if equipment_key(candidate) in selected['equipment_keys']]
            if not chosen:
                continue
            keys = {candidate['candidate_key'] for candidate in chosen}
            keys.update((candidate.get('candidate_params') or {}).get('effect_baseline_key') for candidate in chosen)
            keys.add('baseline')
            candidates = [candidate for candidate in candidates if candidate['candidate_key'] in keys]
        row = deepcopy(coordinate)
        row['candidates'] = deepcopy(candidates)
        rows.append(row)
    if not rows:
        raise ValidationError({'selection': ['所选任务中没有可模拟的所选装备']})
    return rows


def rerun_preview(plan, selection):
    rows = select_rerun_coordinates(plan, selection)
    return {'plan_hash': plan_hash(plan), 'case_count': len(rows),
        'run_count': sum(len(row['candidates']) for row in rows),
        'cases': [{'label': coordinate_label(row),
                   'candidates': [candidate['candidate_label'] for candidate in row['candidates']]} for row in rows]}
