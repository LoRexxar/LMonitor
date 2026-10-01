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


def _validate_expected_report(report_html, candidate_params, *, _parsed_native=None):
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
    evidence = extract_equipment_effect_evidence(report_html, validation_params, {'driver': event_ids},
                                                 _parsed_native=_parsed_native)
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


def _validate_native_structure(report_html, params, proof, *, _parsed_native=None):
    """Check paired native facts and bind the selected gear to actual HTML Profile."""
    import hashlib
    import json
    import math
    from simc_equipment_control import native_proof_marker, validate_effect_policy
    from botend.services.simc_equipment_result_evidence import _native_document

    native_proof_marker(proof)  # Bound even direct local callers, reject NaN.
    proof = deepcopy(proof)
    for key in ('original', 'normal', 'control'):
        roster = proof[key]['items']
        normalized = {_slot(slot): item for slot, item in roster.items()}
        if len(normalized) != len(roster):
            raise ValueError('native alias collision')
        proof[key]['items'] = normalized
    for target in proof['targets']:
        target['slot'] = _slot(target['slot'])
    proof['background_removed'] = [_slot(slot) for slot in proof['background_removed']]
    validate_effect_policy(params)
    mode = 'control' if params.get('equipment_effect_control') is True else 'normal'
    if (set(proof) != {'schema_version', 'scope', 'mode', 'targets', 'background_removed',
                      'rules_hash', 'original', 'normal', 'control', 'target_sets'}
            or type(proof['schema_version']) is not int or proof['schema_version'] != 1
            or proof['scope'] != 'equipment_effect_combination' or proof['mode'] != mode):
        raise ValueError('native scope mismatch')
    rules = params['equipment_effect_policy']['rules']
    if proof['rules_hash'] != hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(',', ':')).encode()).hexdigest():
        raise ValueError('native rules mismatch')
    structural = extract_equipment_effect_evidence(report_html, params, _parsed_native=_parsed_native)
    target_ids = {row['slot']: row['expected']['item_id'] for row in structural['targets']}
    if (not isinstance(proof['targets'], list) or len(proof['targets']) != len(target_ids)
            or not target_ids or any(type(row.get('item_id')) is not int or row['item_id'] <= 0
                    or set(row) != {'slot', 'item_id'} for row in proof['targets'])
            or {row['slot']: row['item_id'] for row in proof['targets']} != target_ids):
        raise ValueError('native target mismatch')
    groups = {}
    for group in ('original', 'normal', 'control'):
        snapshot = proof[group]
        if set(snapshot) != {'items', 'sets'} or not isinstance(snapshot['items'], dict):
            raise ValueError('native snapshot invalid')
        roster = snapshot['items']
        if not roster or len(roster) > len(EQUIPMENT_SLOTS) or not set(roster) <= EQUIPMENT_SLOTS:
            raise ValueError('native roster invalid')
        for item in roster.values():
            if (set(item) != {'item_id', 'bonus_ids', 'profile_value', 'static', 'effects'}
                    or type(item['item_id']) is not int or item['item_id'] < 0
                    or not isinstance(item['profile_value'], str) or '\n' in item['profile_value']
                    or not isinstance(item['effects'], list) or len(item['effects']) > 128):
                raise ValueError('native item invalid')
            _positive_ids(item['bonus_ids'])
            static = item['static']
            if (set(static) != {'stats', 'weapon', 'attachments'} or not isinstance(static['stats'], dict)
                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in static['stats'].values())
                    or set(static['attachments']) != {'gems', 'enchant', 'addon', 'temporary_enchant'}
                    or any(not isinstance(value, str) for value in static['attachments'].values())
                    or static['weapon'] is not None and (not isinstance(static['weapon'], (list, tuple)) or len(static['weapon']) != 4)):
                raise ValueError('native static invalid')
            _, _, fields = _parse_line('gear=' + item['profile_value'])
            if _equipment(fields) != {'item_id': item['item_id'] or None, 'bonus_ids': item['bonus_ids']}:
                raise ValueError('native profile identity mismatch')
            for effect in item['effects']:
                if (set(effect) != {'source', 'type', 'driver'} or effect['source'] != 'item'
                        or effect['type'] not in ('equip', 'use') or type(effect['driver']) is not int or effect['driver'] <= 0):
                    raise ValueError('native effect invalid')
        sets = snapshot['sets']
        if (not isinstance(sets, list) or len(sets) > 128
                or any(not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str)
                       or not re.fullmatch(r'[a-z0-9_]+', row[0]) or type(row[1]) is not int or not 1 <= row[1] <= 8 for row in sets)):
            raise ValueError('native sets invalid')
        groups[group] = roster
    original, normal, control = (groups[key] for key in ('original', 'normal', 'control'))
    if not set(original) == set(normal) == set(control) or not set(target_ids) <= set(original):
        raise ValueError('native roster mismatch')
    background = proof['background_removed']
    if (not isinstance(background, list) or len(set(background)) != len(background)
            or not set(background) <= set(original) - set(target_ids)):
        raise ValueError('native background invalid')
    for slot, before in original.items():
        if not before['static'] == normal[slot]['static'] == control[slot]['static']:
            raise ValueError('native static mismatch')
        if slot in background or slot in target_ids:
            if control[slot]['effects'] or control[slot]['item_id'] or control[slot]['bonus_ids']:
                raise ValueError('native control not removed')
        if slot not in target_ids and normal[slot] != control[slot]:
            raise ValueError('native background mismatch')
        if slot in background and normal[slot]['effects']:
            raise ValueError('native background not removed')
        if slot in target_ids and (before['item_id'] != target_ids[slot]
                or normal[slot]['item_id'] != target_ids[slot] or before['effects'] != normal[slot]['effects']):
            raise ValueError('native target effects mismatch')
    normal_sets = {tuple(row) for row in proof['normal']['sets']}
    control_sets = {tuple(row) for row in proof['control']['sets']}
    known_sets = {(row['name'], row['pieces']) for row in rules['sets']}
    target_sets = {tuple(row) for row in proof['target_sets']}
    if (not target_sets <= normal_sets - control_sets or not target_sets <= known_sets
            or not normal_sets ^ control_sets <= known_sets):
        raise ValueError('native set mismatch')
    for name, pieces in target_sets:
        rule = next(row for row in rules['sets'] if (row['name'], row['pieces']) == (name, pieces))
        if (not set(target_ids.values()).intersection(rule['items'])
                or sum(item['item_id'] in rule['items'] for item in normal.values()) < pieces):
            raise ValueError('native set target mismatch')
    if not any(normal[slot]['effects'] for slot in target_ids) and not target_sets:
        raise ValueError('native effects absent')
    expectation = params.get('equipment_effect_expectation')
    for target in (expectation or {}).get('targets', []):
        item = normal[_slot(target['slot'])]
        if (not set(target['required_bonus_ids']) <= set(item['bonus_ids'])
                or not set(target['driver_spell_ids']) <= {row['driver'] for row in item['effects']}):
            raise ValueError('native central activation mismatch')
    document, complete = _native_document(report_html) if _parsed_native is None else _parsed_native
    profile = '\n'.join(block for section in document.get('sections', [])
                        if section.get('key') == 'profile' for block in section.get('text_blocks', []))
    actual = {}
    for line in profile.splitlines():
        slot, _, fields = _parse_line(line)
        slot = _slot(slot)
        if slot in EQUIPMENT_SLOTS:
            if slot in actual:
                raise ValueError('report gear ambiguous')
            actual[slot] = fields
    if not profile or not complete:
        return None, structural
    selected = groups[mode]
    for slot, item in selected.items():
        _, _, expected = _parse_line('gear=' + item['profile_value'])
        if actual.get(slot) != expected:
            raise ValueError('report native profile mismatch')
    if set(actual) != set(selected):
        raise ValueError('report native roster mismatch')
    # These reasons concern candidates/Profile, not global spell attribution.
    errors = set(structural['reason_codes']) - {'expected_effect_spell_ids_missing', 'report_evidence_incomplete'}
    if errors:
        raise ValueError('report candidate mismatch')
    return True, structural


def validate_equipment_effect_report(report_html, candidate_params, *, native_proof=None):
    """Native structure validates the comparison, not whether an HTML proc fired.

    Without native proof the legacy event-evidence contract stays unchanged.
    Central frozen bonus/driver requirements are never bypassed by native proof.
    """
    from botend.services.simc_equipment_result_evidence import _native_document
    # Share immutable parsed facts only within this completion, not across Runs.
    parsed_native = _native_document(report_html) if native_proof is not None else None
    expected = _validate_expected_report(report_html, candidate_params, _parsed_native=parsed_native)
    if native_proof is None:
        return expected
    params = candidate_params if isinstance(candidate_params, dict) else {}
    control = params.get('equipment_effect_control') is True
    if expected['status'] == 'invalid':
        return expected
    try:
        verified, structural = _validate_native_structure(report_html, params, native_proof,
                                                           _parsed_native=parsed_native)
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError):
        return _result('invalid', 'equipment_native_proof_invalid', control=control)
    if verified is None:
        return _result('unverified', 'report_evidence_incomplete', control=control)
    # Initialization proves a valid isolation, not that a known expected combat
    # event happened. Do not rank simulation noise when that positive evidence
    # is missing; native per-slot proof can resolve control-source ambiguity only.
    result = (deepcopy(expected) if not control and params.get('equipment_effect_expectation')
              and expected['status'] != 'valid' else _result('valid', '', control=control))
    result.update(validation_basis='native_structure', native_proof=deepcopy(native_proof),
                  targets=structural['targets'], html_event_validation=expected,
                  event_status='triggered' if not control and expected['status'] == 'valid'
                  else 'source_unverified' if 'control_effect_source_unverified' in expected['reason_codes']
                  else 'unverified')
    return result
