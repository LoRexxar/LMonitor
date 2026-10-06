"""Conservative, presentation-only ties for adjacent item-level inversions.

This is not a noise diagnosis or a joint confidence interval. Reported error
endpoints only permit a labelled display tie within a strict 0.1 percentage-point
budget. Actual Task and composition evidence must match; this does not attest an
actual binary build. No ORM, publication, cross-batch reading or reuse decisions
belong here. Call with one coordinate's already-publishable, fresh row mappings.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json


_LIMIT = Decimal('0.1')
_REQUIRED_SLOTS = ('talents', 'action_list', 'simulation_options', 'player_identity')
_OPTIONAL_SLOTS = ('additional_simc_input', 'stat_overrides')
_LABEL = '差异在报告误差范围内，按并列展示'


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _signature(run):
    if not isinstance(run, dict):
        return None
    if any(type(run.get(key)) is not int or run[key] <= 0 for key in ('task_id', 'run_id')):
        return None
    slots = run.get('composition_slot_hashes')
    if not isinstance(slots, dict):
        return None
    if any(not isinstance(slots.get(key), str) or not slots[key].strip() for key in _REQUIRED_SLOTS):
        return None
    # Compare all supplied slots, including future semantic slots. Missing nullable
    # slots and explicit null have the same meaning in the evidence reader.
    normalized = dict(slots)
    for key in _OPTIONAL_SLOTS:
        normalized.setdefault(key, None)
    if any(value is not None and (not isinstance(value, str) or not value.strip())
           for value in normalized.values()):
        return None
    return run['task_id'], tuple(sorted(normalized.items()))


def _group(row):
    if row.get('key') == 'baseline' or row.get('type', 'gear_swap') != 'gear_swap':
        return None
    item_id = row.get('item_id')
    variant = row.get('item_variant_key') or row.get('equipment_group_key')
    if type(item_id) is not int or item_id <= 0 or not isinstance(variant, str) or not variant:
        return None
    kind = row.get('comparison_kind')
    if kind == 'conditional_increment' and (not row.get('context_slots') or not row.get('changed_slots')):
        return None
    try:
        context = json.dumps({key: row.get(key) for key in (
            'comparison_kind', 'comparison_mode', 'context_slots', 'changed_slots',
            'equipment_group_key', 'item_variant_key',
        )}, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError):
        return None
    return item_id, variant, context


@dataclass
class _Point:
    row: dict
    value: Decimal
    low: Decimal
    high: Decimal
    signature: tuple
    effect: bool
    baseline: Decimal
    gain: Decimal | None


@dataclass
class _Block:
    points: list
    value: Decimal
    low: Decimal
    high: Decimal


def _point(row, evidence, coordinate_baseline):
    if not isinstance(evidence, dict):
        return None
    validation = row.get('effect_validation')
    if validation is not None and (not isinstance(validation, dict) or validation.get('status') != 'valid'):
        return None
    effect = ('effect_delta_percent' in row or row.get('comparison_mode') == 'equipment_effect'
              or validation is not None)
    normal = evidence.get('normal')
    signature = _signature(normal)
    if signature is None:
        return None
    error = _number(normal.get('dps_error'))
    dps = _number(row.get('dps'))
    if error is None or error < 0 or dps is None or dps <= 0:
        return None
    baseline = _number(row.get('baseline_dps'))
    gain = None
    if effect:
        if not isinstance(validation, dict) or validation.get('status') != 'valid':
            return None
        control = evidence.get('control')
        if _signature(control) != signature:
            return None
        control_error = _number(control.get('dps_error'))
        value = _number(row.get('effect_delta_percent'))
        gain = _number(row.get('gain_dps'))
        if (baseline is None or control_error is None or control_error < 0
                or baseline <= control_error or value is None or gain is None):
            return None
        low = ((dps - error) / (baseline + control_error) - 1) * 100
        high = ((dps + error) / (baseline - control_error) - 1) * 100
        # Do not lift a previously floored zero into a published positive effect.
        if value < _LIMIT:
            return None
    else:
        # A baseline row is authoritative for ordinary coordinate DPS, not a
        # per-item effect control. Explicit per-row baselines must agree with it.
        if coordinate_baseline is not None:
            if baseline is not None and baseline != coordinate_baseline:
                return None
            baseline = coordinate_baseline
        if baseline is None or baseline <= 0 or evidence.get('control') is not None:
            return None
        value, low, high = dps, dps - error, dps + error
    if not low <= value <= high:
        return None
    return _Point(row, value, low, high, signature, effect, baseline, gain)


def _merge(left, right):
    if left is None or right is None or left.value <= right.value:
        return None
    points = left.points + right.points
    first = points[0]
    if any(point.signature != first.signature or point.effect != first.effect for point in points):
        return None
    if first.effect:
        # Percentage drops with nondecreasing absolute gains are denominator
        # changes, not the item-level inversion this helper is allowed to tie.
        if any(a.value > b.value and a.gain <= b.gain for a, b in zip(points, points[1:])):
            return None
        scale = Decimal(1)
    else:
        if any(point.baseline != first.baseline for point in points):
            return None
        scale = Decimal(100) / first.baseline
    raw_values = [point.value for point in points]
    if (max(raw_values) - min(raw_values)) * scale >= _LIMIT:
        return None
    low, high = max(point.low for point in points), min(point.high for point in points)
    if low > high:
        return None
    value = min(high, max(low, sum(raw_values) / len(points)))
    if first.effect and value < _LIMIT:
        value = Decimal(0)
        if not low <= value <= high:
            return None
    return _Block(points, value, low, high)


def apply_noise_display(rows, evidence_by_key):
    """Mutate only derived display fields; return the original rows object.

    Adjusted ordinary rows gain ``display_dps``; adjusted effect rows replace
    only ``effect_delta_percent`` (the existing display-only field). Both gain
    ``noise_adjustment`` with the original display value and tied candidate keys.
    Untouched rows retain their original fields/eligibility. Raw dps, baseline,
    gain fields, validation and every evidence mapping are never modified.

    Missing/ambiguous item levels invalidate the identity group, and unusable
    evidence is an adjacency barrier, never removed before item-level sorting.
    Block merges require a common intersection and strict whole-block budget;
    a remaining significant or uncertain inversion is intentionally left alone.
    """
    baseline_rows = [row for row in rows if row.get('key') == 'baseline']
    coordinate_baseline = (_number(baseline_rows[0].get('dps')) if len(baseline_rows) == 1 else None)
    # Ambiguous/nonpositive coordinate baseline must not authorize ordinary ties.
    baseline_invalid = bool(baseline_rows) and (coordinate_baseline is None or coordinate_baseline <= 0)
    key_counts = Counter(row.get('key') for row in rows)
    groups = defaultdict(list)
    for row in rows:
        group = _group(row)
        if group is not None:
            groups[group].append(row)
    for group_rows in groups.values():
        levels = [_number(row.get('item_level')) for row in group_rows]
        if (any(level is None or level <= 0 or level != level.to_integral_value() for level in levels)
                or len(set(levels)) != len(levels)):
            continue
        stack, ordered = [], []
        for _, row in sorted(zip(levels, group_rows), key=lambda pair: pair[0]):
            ordered.append(row)
            key = row.get('key')
            point = (_point(row, evidence_by_key.get(key), coordinate_baseline)
                     if isinstance(key, str) and key and key_counts[key] == 1 else None)
            if point is not None and baseline_invalid and not point.effect:
                point = None
            stack.append(_Block([point], point.value, point.low, point.high) if point else None)
            while len(stack) > 1:
                merged = _merge(stack[-2], stack[-1])
                if merged is None:
                    break
                stack[-2:] = [merged]
        # Independent small ties can reverse an originally rising boundary
        # when the union cannot merge under the whole-block budget. Reject that
        # group's display projection rather than expanding the threshold or
        # changing any raw result. Include unusable-evidence rows as boundaries.
        proposed = {id(point.row): (point, block.value)
                    for block in stack if block is not None
                    for point in block.points}
        field = ('effect_delta_percent' if any(point.effect for point, _ in proposed.values())
                 else 'dps')
        boundary_inverted = False
        for left, right in zip(ordered, ordered[1:]):
            raw_left, raw_right = _number(left.get(field)), _number(right.get(field))
            if raw_left is None or raw_right is None:
                continue
            display_left = proposed[id(left)][1] if id(left) in proposed else raw_left
            display_right = proposed[id(right)][1] if id(right) in proposed else raw_right
            if raw_left <= raw_right and display_left > display_right:
                boundary_inverted = True
                break
        if boundary_inverted:
            continue
        for block in stack:
            if block is None or len(block.points) < 2:
                continue
            keys = [point.row['key'] for point in block.points]
            value = float(block.value)
            for point in block.points:
                field = 'effect_delta_percent' if point.effect else 'display_dps'
                point.row[field] = value
                point.row['noise_adjustment'] = {
                    'kind': 'within_reported_error', 'raw_display_value': float(point.value),
                    'display_value': value, 'candidate_keys': list(keys), 'label': _LABEL,
                }
    return rows
