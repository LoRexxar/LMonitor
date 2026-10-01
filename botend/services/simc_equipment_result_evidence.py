"""Read-only, JSON-freezable equipment-effect evidence from verified report HTML.

The caller owns artifact SHA/lease verification and persistence. Store the return
value in ``result_summary['equipment_effect_evidence']`` at completion, or call
this same helper explicitly when backfilling an old immutable artifact. This
module does not fetch artifacts, access the DB, or alter Run status.
"""
import math
import re

from bs4 import BeautifulSoup

from botend.services.simc_player_config import (
    EQUIPMENT_SLOTS, EQUIPMENT_SLOT_ALIASES, _parse_line,
)
from botend.services.simc_result_analysis import parse_simc_html_report


_ROLES = ('driver', 'buff', 'damage')
_MAX_IDS = 128
_MAX_ROWS = 256


def _ids(values):
    if values is None:
        return []
    if isinstance(values, (int, str)) and not isinstance(values, bool):
        values = [values]
    if not isinstance(values, (list, tuple, set)) or len(values) > _MAX_IDS:
        raise ValueError('invalid ID collection')
    result = set()
    for value in values:
        if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]*', str(value)):
            raise ValueError('invalid positive integer ID')
        result.add(int(value))
    return sorted(result)


def _number(value, percent=False):
    text = str(value or '').replace(',', '').strip()
    if percent:
        text = text.removesuffix('%')
    if not re.fullmatch(r'\+?\d+(?:\.\d+)?', text):
        return None
    number = float(text)
    return number if math.isfinite(number) else None


def _slot(value):
    value = str(value or '').strip().lower()
    return EQUIPMENT_SLOT_ALIASES.get(value, value)


def _equipment(values):
    item = values.get('id')
    if item is not None and not re.fullmatch(r'\d+', item):
        raise ValueError('invalid item ID')
    item_id = int(item) if item is not None and re.fullmatch(r'\d+', item) else None
    bonus = values.get('bonus_id')
    return {'item_id': item_id if item_id else None,
            'bonus_ids': _ids(bonus.split('/')) if bonus else []}


def _native_document(report_html):
    """Use the existing parser on only one player's native evidence sections.

    Pruning unrelated sections avoids UI item-metadata enrichment (and therefore
    DB reads), large APL/timeline projections, and the parser's global-table
    fallback. Spell identities still come exclusively from native hyperlinks.
    """
    if not isinstance(report_html, str) or not report_html:
        return {}, False
    soup = BeautifulSoup(report_html, 'html.parser')
    player = soup.find(class_='player')
    if player is None:
        return {}, False
    toggle = player.find('div', class_='toggle-content', recursive=False)
    deferred = toggle.find('script', attrs={'type': 'text/x-deferred-html'}) if toggle else None
    detail = player
    if deferred:
        detail = BeautifulSoup(deferred.string or deferred.decode_contents(), 'html.parser')
    elif toggle:
        text = toggle.get_text('', strip=False)
        if '<' in text and '>' in text:
            detail = BeautifulSoup(text, 'html.parser')
    damage = next((table for table in detail.select('table.sc.sort')
                   if table.find('th') and table.find('th').get_text(' ', strip=True) == 'Damage Stats'), None)
    dynamic = next((table for table in detail.select('table.sc')
                    if any(th.get_text(' ', strip=True) == 'Dynamic Buffs'
                           for th in table.find_all('th'))), None)
    constant = next((table for table in detail.select('table.sc')
                     if table.find('th') and table.find('th').get_text(' ', strip=True) == 'Constant Buffs'), None)
    profiles = [section for section in detail.select('div.player-section')
                if section.find(['h2', 'h3']) and
                section.find(['h2', 'h3']).get_text(' ', strip=True) == 'Profile']
    # The UI parser intentionally omits child actions. Include their own native
    # spell IDs/counts here, using its same metric parser instead of duplicating it.
    if damage:
        for row in damage.select('tr.toprow.childrow'):
            row['class'] = [name for name in row.get('class', []) if name != 'childrow']
    parts = [node for node in (damage, dynamic, constant, *profiles) if node is not None]
    document = parse_simc_html_report('<div class="player">' + ''.join(map(str, parts)) + '</div>')
    complete = bool(damage is not None and (dynamic is not None or constant is not None) and len(profiles) == 1)
    if damage:
        headers = {th.get_text(' ', strip=True) for th in damage.select('thead th')}
        complete = complete and {'Damage Stats', 'DPS', 'Count'} <= headers
        complete = complete and len(document.get('abilities', [])) == len(damage.select('tr.toprow'))
    if dynamic:
        complete = complete and any(
            [th.get_text(' ', strip=True) for th in row.find_all('th', recursive=False)] ==
            ['Dynamic Buffs', 'Start', 'Refresh', 'Total', 'Start', 'Trigger',
             'Duration', 'Uptime', 'Benefit', 'Overflow', 'Expiry']
            for row in dynamic.select('thead tr'))
        complete = complete and len(document.get('buffs', {}).get('dynamic', [])) == len(
            [body for body in dynamic.find_all('tbody', recursive=False)
             if body.find('tr', class_='right', recursive=False)])
    return document, complete


def extract_equipment_effect_evidence(report_html, candidate_params, expected_effect_spell_ids=None):
    """Extract bounded evidence; return ``valid``, ``invalid`` or ``unverified``.

    ``expected_effect_spell_ids`` is an optional mapping of ``driver``, ``buff``
    and ``damage`` to positive integer IDs (a scalar or list). IDs within a role
    are alternatives; each supplied terminal role (buff/damage) needs at least
    one effective event to verify activation. Missing events are unverified,
    not proof that the effect is unimplemented. Drivers need not have their own
    HTML row when terminal events exist. A driver-only expectation needs an
    effective action/buff row. Mere row presence, execute attempts, names and
    tooltip text are not proof. Structural Profile mismatches remain invalid.

    Normal targets must match frozen gear_swap/gear_swaps slot, item ID and any
    explicitly specified bonus IDs. Controls must have an ID-/bonus-free target
    substitution. Shared global spell events cannot be attributed to that slot:
    they make a structurally correct control unverified, not invalid. An inactive
    row is allowed. Background preservation needs the caller's frozen-input/pair
    comparison; this single-report helper never removes or infers background gear.
    No identity mapping, including malformed mappings, is always unverified.
    """
    expected = {role: [] for role in _ROLES}
    reasons = []
    uncertainties = []
    identity_error = None
    try:
        if expected_effect_spell_ids is not None:
            if not isinstance(expected_effect_spell_ids, dict) or set(expected_effect_spell_ids) - set(_ROLES):
                raise ValueError('invalid role mapping')
            expected = {role: _ids(expected_effect_spell_ids.get(role)) for role in _ROLES}
    except ValueError:
        identity_error = 'expected_effect_spell_ids_invalid'
    known_identity = identity_error is None and any(expected.values())
    params = candidate_params if isinstance(candidate_params, dict) else {}
    control = params.get('equipment_effect_control') is True
    evidence = {'schema_version': 1, 'status': 'unverified', 'valid': None,
                'mode': 'control' if control else 'normal', 'reason': '', 'reason_codes': reasons,
                'expected_spell_ids': expected, 'targets': [], 'actions': [], 'buffs': [],
                'observed_spell_ids': {'actions': [], 'buffs': []}}
    try:
        document, complete = _native_document(report_html)
        profile = '\n'.join(block for section in document.get('sections', [])
                            if section.get('key') == 'profile' for block in section.get('text_blocks', []))
        slots = {}
        for line in profile.splitlines():
            key, _, values = _parse_line(line)
            key = _slot(key)
            if key in EQUIPMENT_SLOTS:
                slots.setdefault(key, []).append(_equipment(values))
        swaps = params.get('gear_swaps')
        if swaps is None:
            swaps = [params.get('gear_swap')]
        if not isinstance(swaps, list) or not swaps or len(swaps) > len(EQUIPMENT_SLOTS):
            swaps = []
        if not swaps:
            reasons.append('candidate_targets_missing')
        used_slots = set()
        for swap in swaps:
            if not isinstance(swap, dict):
                reasons.append('candidate_target_invalid')
                continue
            slot = _slot(swap.get('slot'))
            _, _, raw_fields = _parse_line('gear=' + str(swap.get('raw_value') or ''))
            # Accept the existing normalized ',id=...' as well as slot=id=... input.
            raw = str(swap.get('raw_value') or '')
            if re.match(r'^\s*[a-z_]+\s*=', raw) and raw.split('=', 1)[0].strip() in EQUIPMENT_SLOTS | set(EQUIPMENT_SLOT_ALIASES):
                raw_slot, _, raw_fields = _parse_line(raw)
                if _slot(raw_slot) != slot:
                    reasons.append('candidate_target_invalid')
            target = _equipment(raw_fields)
            explicit_item = swap.get('item_id', swap.get('id'))
            if explicit_item is not None:
                explicit = _ids(explicit_item)
                if len(explicit) != 1 or target['item_id'] not in (None, explicit[0]):
                    reasons.append('candidate_target_invalid')
                target['item_id'] = explicit[0] if explicit else None
            bonus = swap.get('bonus_ids', swap.get('bonus_id'))
            if bonus is not None:
                explicit_bonus = _ids(re.split(r'[/;: ]+', bonus) if isinstance(bonus, str) else bonus)
                if target['bonus_ids'] and target['bonus_ids'] != explicit_bonus:
                    reasons.append('candidate_target_invalid')
                target['bonus_ids'] = explicit_bonus
            observed_rows = slots.get(slot, [])
            observed = observed_rows[0] if len(observed_rows) == 1 else None
            evidence['targets'].append({'slot': slot, 'expected': target, 'observed': observed})
            if slot not in EQUIPMENT_SLOTS or slot in used_slots or target['item_id'] is None:
                reasons.append('candidate_target_invalid')
            used_slots.add(slot)
            if not observed_rows and profile:
                reasons.append('target_slot_missing')
            elif not observed_rows:
                pass  # Missing Profile is unknown evidence, not a wrong slot.
            elif len(observed_rows) != 1:
                reasons.append('target_slot_ambiguous')
            elif control:
                if observed['item_id'] is not None or observed['bonus_ids']:
                    reasons.append('control_target_not_disabled')
            else:
                if observed['item_id'] != target['item_id']:
                    reasons.append('target_item_mismatch')
                if observed['bonus_ids'] != target['bonus_ids']:
                    reasons.append('target_bonus_mismatch')
        selected_ids = set(sum(expected.values(), []))
        abilities = document.get('abilities', [])
        buffs = document.get('buffs', {})
        if len(abilities) + len(buffs.get('dynamic', [])) + len(buffs.get('constant', [])) > _MAX_ROWS:
            complete = False
        for row in abilities[:_MAX_ROWS]:
            if not row.get('spell_id'):
                continue
            spell_id = int(row['spell_id'])
            evidence['observed_spell_ids']['actions'].append(spell_id)
            if known_identity and spell_id not in selected_ids:
                continue
            details = row.get('details', {})
            counts = [_number(details.get(key)) for key in ('direct_results', 'tick_results')]
            successful = sum(value or 0 for value in counts) if any(value is not None for value in counts) else _number(row.get('count'))
            amount = _number(details.get('actual_amount'))
            dps = _number(row.get('dps'))
            effective = bool(successful and successful > 0 and (amount > 0 if amount is not None else dps is not None and dps > 0))
            evidence['actions'].append({'spell_id': spell_id, 'executes': _number(details.get('executes', row.get('execute'))),
                                        'successful_results': successful, 'actual_amount': amount,
                                        'dps': dps, 'effective': effective})
        for kind in ('dynamic', 'constant'):
            for row in buffs.get(kind, [])[:_MAX_ROWS]:
                if not row.get('spell_id'):
                    continue
                spell_id = int(row['spell_id'])
                evidence['observed_spell_ids']['buffs'].append(spell_id)
                if known_identity and spell_id not in selected_ids:
                    continue
                triggers = _number(row.get('trigger_count_total'))
                uptime = _number(row.get('uptime'), percent=True)
                effective = bool(triggers and triggers > 0 and uptime and uptime > 0)
                evidence['buffs'].append({'spell_id': spell_id, 'kind': kind, 'trigger_count': triggers,
                                          'uptime_pct': uptime, 'effective': effective})
        if not profile or not complete:
            uncertainties.append('report_evidence_incomplete')
        if known_identity and complete:
            active_actions = {row['spell_id'] for row in evidence['actions'] if row['effective']}
            active_buffs = {row['spell_id'] for row in evidence['buffs'] if row['effective']}
            if control:
                if active_actions or active_buffs or any(row['kind'] == 'constant' for row in evidence['buffs']):
                    uncertainties.append('control_effect_source_unverified')
            else:
                for role, active in (('damage', active_actions), ('buff', active_buffs)):
                    if expected[role] and not active.intersection(expected[role]):
                        uncertainties.append(role + '_events_missing')
                if not expected['damage'] and not expected['buff'] and not (active_actions | active_buffs).intersection(expected['driver']):
                    uncertainties.append('driver_events_missing')
    except (ValueError, TypeError, AttributeError, OverflowError):
        uncertainties.append('report_or_candidate_evidence_invalid')
    for role in evidence['observed_spell_ids']:
        evidence['observed_spell_ids'][role] = sorted(set(evidence['observed_spell_ids'][role]))
    if not known_identity:
        reasons.insert(0, identity_error or 'expected_effect_spell_ids_missing')
    else:
        evidence['status'] = 'invalid' if reasons else 'unverified' if uncertainties else 'valid'
        evidence['valid'] = False if reasons else None if uncertainties else True
    reasons.extend(uncertainties)
    evidence['reason_codes'] = list(dict.fromkeys(reasons))
    evidence['reason'] = '; '.join(evidence['reason_codes'])
    return evidence
