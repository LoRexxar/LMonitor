"""Validate report activation against frozen facts; never fetch or rewrite inputs."""
from copy import deepcopy
import re

from botend.services.simc_equipment_result_evidence import (
    _equipment, _ids, _slot, extract_equipment_effect_evidence,
)
from botend.services.simc_player_config import EQUIPMENT_SLOTS, EQUIPMENT_SLOT_ALIASES, _parse_line


_EFFECT_KEYS = ('equipment_effect_policy', 'equipment_effect_control', 'effect_baseline_key')
_TARGET_KEYS = {'slot', 'item_id', 'game_build', 'required_bonus_ids',
                'driver_spell_ids', 'event_spell_ids', 'fact_hash'}


def is_equipment_effect_candidate(params):
    """Ordinary DPS, baseline and trinket Runs must keep their existing summaries."""
    return isinstance(params, dict) and any(params.get(key) for key in _EFFECT_KEYS)


def _result(status, reason, *, control=False):
    return {'schema_version': 1, 'status': status,
            'valid': True if status == 'valid' else False if status == 'invalid' else None,
            'mode': 'control' if control else 'normal',
            'reason': reason, 'reason_codes': [reason] if reason else []}


def _add_reason(evidence, reason, status):
    if reason not in evidence['reason_codes']:
        evidence['reason_codes'].append(reason)
    # Unknown runtime evidence must never soften a demonstrated structural error.
    if evidence['status'] != 'invalid' or status == 'invalid':
        evidence['status'] = status
        evidence['valid'] = False if status == 'invalid' else None
    evidence['reason'] = '; '.join(evidence['reason_codes'])


def _positive_ids(value):
    if (not isinstance(value, list) or len(value) > 128
            or any(type(item) is not int or item <= 0 for item in value)):
        raise ValueError('invalid ID list')
    return sorted(set(value))


def validate_equipment_effect_report(report_html, candidate_params):
    """Return JSON-freezable valid/invalid/unverified without changing Run semantics.

    Frozen targets cover *every* candidate slot. Each normal target requires its
    activation bonuses in the actual Profile and at least one effective native
    event in its frozen spell family. Controls use the evidence helper's source
    attribution rules: shared background events are unknown, never an error.
    No current metadata/DB lookup or inferred spell identity can authorize gain.
    """
    params = candidate_params if isinstance(candidate_params, dict) else {}
    control = params.get('equipment_effect_control') is True
    expectation = params.get('equipment_effect_expectation')
    if expectation is None:
        return _result('unverified', 'equipment_effect_expectation_missing', control=control)
    try:
        if (not isinstance(expectation, dict)
                or type(expectation.get('schema_version')) is not int
                or expectation['schema_version'] != 1
                or not isinstance(expectation.get('targets'), list)
                or len(expectation['targets']) > len(EQUIPMENT_SLOTS)):
            raise ValueError('invalid expectation')
        targets = {}
        for target in expectation['targets']:
            if (not isinstance(target, dict) or not _TARGET_KEYS.issubset(target)
                    or not isinstance(target['slot'], str)
                    or _slot(target['slot']) not in EQUIPMENT_SLOTS
                    or type(target['item_id']) is not int or target['item_id'] <= 0
                    or not isinstance(target['game_build'], str)
                    or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', target['game_build'])
                    or not isinstance(target['fact_hash'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', target['fact_hash'])):
                raise ValueError('invalid target')
            slot = _slot(target['slot'])
            if slot in targets:
                raise ValueError('duplicate target')
            targets[slot] = {**target, 'slot': slot,
                **{key: _positive_ids(target[key]) for key in
                   ('required_bonus_ids', 'driver_spell_ids', 'event_spell_ids')}}
        swaps = params.get('gear_swaps')
        if swaps is None:
            swaps = [params.get('gear_swap')]
        if (not isinstance(swaps, list) or not swaps
                or len(swaps) > len(EQUIPMENT_SLOTS)):
            raise ValueError('invalid candidate targets')
        validation_swaps, candidate_slots = [], set()
        for swap in swaps:
            if not isinstance(swap, dict):
                raise ValueError('invalid candidate target')
            slot = _slot(swap.get('slot'))
            if slot not in EQUIPMENT_SLOTS or slot in candidate_slots:
                raise ValueError('invalid candidate slot')
            candidate_slots.add(slot)
            raw = str(swap.get('raw_value') or '')
            raw_key = raw.split('=', 1)[0].strip()
            if raw_key in EQUIPMENT_SLOTS | set(EQUIPMENT_SLOT_ALIASES):
                raw_slot, _, fields = _parse_line(raw)
                if _slot(raw_slot) != slot:
                    raise ValueError('candidate raw slot mismatch')
            else:
                _, _, fields = _parse_line('gear=' + raw)
            candidate = _equipment(fields)
            explicit_item = swap.get('item_id', swap.get('id'))
            if explicit_item is not None:
                ids = _ids(explicit_item)
                if len(ids) != 1 or candidate['item_id'] not in (None, ids[0]):
                    raise ValueError('candidate raw item mismatch')
                candidate['item_id'] = ids[0]
            bonus = swap.get('bonus_ids', swap.get('bonus_id'))
            if bonus is not None:
                ids = _ids(re.split(r'[/;: ]+', bonus) if isinstance(bonus, str) else bonus)
                if candidate['bonus_ids'] and candidate['bonus_ids'] != ids:
                    raise ValueError('candidate raw bonus mismatch')
                candidate['bonus_ids'] = ids
            if candidate['item_id'] is None:
                raise ValueError('candidate item missing')
            target = targets.get(slot)
            if target and candidate['item_id'] != target['item_id']:
                raise ValueError('frozen target item mismatch')
            validation_swap = deepcopy(swap)
            if target:
                # Supplement only this detached validation view. Historical input,
                # provenance and the caller's params remain byte-for-byte unchanged.
                bonuses = sorted(set(candidate['bonus_ids']) | set(target['required_bonus_ids']))
                raw = re.sub(r'(?:^|,)bonus_id=[^,]*', '', raw)
                if bonuses:
                    raw += ',bonus_id=' + '/'.join(map(str, bonuses))
                validation_swap.update(raw_value=raw, bonus_ids=bonuses)
                validation_swap.pop('bonus_id', None)
            validation_swaps.append(validation_swap)
        if set(targets) - candidate_slots:
            raise ValueError('frozen target slot mismatch')
    except (ValueError, TypeError, AttributeError, OverflowError):
        return _result('invalid', 'equipment_effect_expectation_invalid', control=control)

    event_ids = sorted({spell for target in targets.values() for spell in target['event_spell_ids']})
    validation_params = {**params, 'gear_swaps': validation_swaps}
    evidence = extract_equipment_effect_evidence(report_html, validation_params, {'driver': event_ids})
    if set(targets) != candidate_slots or any(not target['event_spell_ids'] or not target['driver_spell_ids']
                                             for target in targets.values()):
        _add_reason(evidence, 'equipment_effect_expectation_incomplete', 'unverified')
    if not control:
        active_ids = {row['spell_id'] for key in ('actions', 'buffs')
                      for row in evidence[key] if row['effective']}
        for row in evidence['targets']:
            target = targets.get(row['slot'])
            if target:
                row['activation_expectation'] = deepcopy(target)
                row['event_status'] = 'valid' if active_ids.intersection(target['event_spell_ids']) else 'unverified'
                observed = row.get('observed')
                if observed and not set(target['required_bonus_ids']).issubset(observed['bonus_ids']):
                    _add_reason(evidence, 'target_bonus_mismatch', 'invalid')
                if row['event_status'] != 'valid':
                    _add_reason(evidence, 'equipment_effect_target_events_missing', 'unverified')
    return evidence
