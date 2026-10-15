"""Native selector graph operations; caller owns evidence and final level math.

Consumer: TrinityCore ItemBonusMgr.cpp GetBonusListsForItem (428-455).
MinItemLevel selects a DELTA bonus, never an overrideItemLevel. QualitySet
chooses quality only. Default group selection depends on native iteration order;
DB2/CSV ID order does not establish unordered_multimap order.
"""

class SelectorUnresolved(ValueError):
    """Missing or ambiguous native selector input; never guess a level/rank."""


def _integer(row, field):
    value = row.get(field)
    if type(value) is not int:
        raise SelectorUnresolved(f'invalid typed {field}')
    return value


def selector_bonus_ids(selector_id, sparse, *, read_table):
    """read_table(table, field, key) returns validated complete typed rows.

    Pass the shared core's evidence-recording read function, not a raw provider.
    Returned bonus IDs must flow through shared ItemBonus/squish/scaling math.
    """
    def one(table, key):
        rows = read_table(table, 'ID', key)
        if rows is None or len(rows) != 1:
            raise SelectorUnresolved(f'{table}: expected one referenced row')
        return rows[0]
    selector = one('ItemLevelSelector', selector_id)
    level = _integer(selector, 'MinItemLevel')
    delta = level - _integer(sparse, 'ItemLevel')
    if not -32768 <= delta <= 32767:
        raise SelectorUnresolved('selector delta outside native int16 range')
    delta_rows = read_table('ItemBonusListLevelDelta', 'ItemLevelDelta', delta)
    if delta_rows is None or len(delta_rows) != 1:
        raise SelectorUnresolved('missing or ambiguous level delta bonus')
    bonuses = [_integer(delta_rows[0], 'ID')]
    quality_set = _integer(selector, 'ItemLevelSelectorQualitySetID')
    selected_quality = None
    if quality_set:
        thresholds = one('ItemLevelSelectorQualitySet', quality_set)
        quality = 4 if level >= _integer(thresholds, 'IlvlEpic') else 3 if level >= _integer(thresholds, 'IlvlRare') else 2
        rows = read_table('ItemLevelSelectorQuality', 'ParentILSQualitySetID', quality_set)
        if rows is None:
            raise SelectorUnresolved('missing quality list')
        candidates = sorted(rows, key=lambda r: _integer(r, 'Quality'))
        if len({_integer(r, 'Quality') for r in candidates}) != len(candidates):
            raise SelectorUnresolved('ambiguous equal-quality selector entries')
        selected_quality = next((r for r in candidates if r['Quality'] >= quality), None)
        if selected_quality:
            bonuses.append(_integer(selected_quality, 'QualityItemBonusListID'))
    if _integer(selector, 'AzeriteUnlockMappingSetID'):
        raise SelectorUnresolved('azerite unlock consumer requires complete mapping and inventory input')
    return dict(selector_id=selector_id, bonus_ids=bonuses, delta=delta,
                quality_entry_id=selected_quality['ID'] if selected_quality else None,
                rule='native_selector_delta_bonus_and_quality_lower_bound')


def default_group_entry(rows, *, sequence_level, native_order_proven=False):
    """Apply native first matching entry, not min(SequenceValue).

    Caller must prove generation sequence (native GetBonusListsForItem passes 0;
    tree-specific overrides/points-mod inputs may alter it). Do not assert order
    from CSV sorting: TrinityCore storage is unordered_multimap.
    """
    if type(sequence_level) is not int or rows is None:
        raise SelectorUnresolved('native sequence input missing')
    candidates = []
    for row in rows:
        value = _integer(row, 'SequenceValue')
        if (sequence_level > 0 or value <= 0) and sequence_level != value:
            continue
        candidates.append(row)
    if not candidates:
        raise SelectorUnresolved('no matching native sequence entry')
    if len(candidates) > 1 and not native_order_proven:
        raise SelectorUnresolved('native group iteration order unproven; CSV order is not native order')
    return candidates[0]
