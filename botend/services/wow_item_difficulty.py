"""Exact-build initial-drop level/quality projection (no ORM/network writes).

Type49 is ItemScalingConfig, NOT Curve. Native Item.cpp uses config.ItemLevel
as the curve input, not RequiredLevel or player level. Creation-conditional
bonuses are excluded from the initial-drop contract and retained as diagnostics.
This projection does not claim random sockets/stats/creation upgrades are known.
"""
from collections.abc import Callable, Mapping
from math import floor, isfinite
import hashlib
import json


class _Incomplete(ValueError):
    pass


def resolve_difficulty_bonus_facts(item_id: int, item_context: int, build: str,
                                   *, tables: Callable) -> dict:
    if type(item_id) is not int or item_id <= 0:
        raise ValueError('item_id must be a positive integer')
    if type(item_context) is not int or item_context < 0:
        raise ValueError('item_context must be a nonnegative catalog context')
    if not isinstance(build, str) or not build:
        raise ValueError('exact build required')
    out: dict = dict(item_id=item_id, item_context=item_context, build=build,
               rule_build=build, provider='wago', status='unresolved',
               bonus_ids=[], item_level=None, quality=None, proven_bonus_ids=[],
               proven_quality=None, curve_ids=[], evidence=[], unresolved=[],
               diagnostics=[], initial_groups=[], scaling_configs=[])
    bonuses, qualities, rules, base_overrides, selectors = set(), [], [], [], set()
    cache = {}

    def issue(table, field, key, reason, *, blocking=True):
        d = dict(table=table, field=field, key=key, reason=reason)
        target = out['unresolved' if blocking else 'diagnostics']
        if d not in target:
            target.append(d)

    def read(table, field, key):
        identity = table, field, key
        if identity in cache:
            return cache[identity]
        try:
            d = tables(table, field, key)
            if not isinstance(d, Mapping) or d.get('build') != build:
                raise _Incomplete('missing or cross-build envelope')
            exact_filter = {} if field is None and key is None else {field: key}
            if d.get('table') != table or d.get('filter') != exact_filter:
                raise _Incomplete('exact table/filter identity missing')
            rows = d.get('rows')
            if not isinstance(rows, list) or type(d.get('total')) is not int or len(rows) != d['total']:
                raise _Incomplete('incomplete pagination/total')
            if any(not isinstance(r, Mapping) or (field is not None and r.get(field) != key) or type(r.get('ID')) is not int for r in rows):
                raise _Incomplete('row/filter identity or ID invalid')
            if field is None and d.get('source_total', d['total']) != d['total']:
                raise _Incomplete('unfiltered source_total mismatch')
            if len({r['ID'] for r in rows}) != len(rows):
                raise _Incomplete('duplicate row IDs')
            identity_body = dict(build=build, table=table, filter=exact_filter, total=d['total'], rows=rows)
            canonical = json.dumps(identity_body, sort_keys=True, separators=(',', ':'), allow_nan=False)
            out['evidence'].append({**identity_body, 'provider': 'wago',
                'field': field, 'key': key, 'url': d.get('url'),
                'source_sha256': hashlib.sha256(canonical.encode()).hexdigest(),
                **{f: d[f] for f in ('source_total', 'file_sha256') if f in d}})
            cache[identity] = rows
            return rows
        except (ValueError, KeyError, TypeError, OSError) as exc:
            issue(table, field, key, str(exc))
            cache[identity] = None
            return None

    def one(table, key):
        rows = read(table, 'ID', key)
        if rows is not None and len(rows) != 1:
            issue(table, 'ID', key, 'expected exactly one referenced row')
        return rows[0] if rows and len(rows) == 1 else None

    def group_initial(node):
        group = node['ChildItemBonusListGroupID']
        rows = read('ItemBonusListGroupEntry', 'ItemBonusListGroupID', group)
        base = one('ItemBonusListGroup', group)
        # Flags 0x1 = INACCESSIBLE_FOR_UPGRADES, 0x2 = IGNORE_FOR_PVP_ITEM_LEVEL.
        # A unique accessible, no-cost entry is the initial acquisition rank;
        # no-cost expansion rows with Flags=3 are explicitly excluded. This is
        # not min(SequenceValue), nor a fabricated public points-mod DB2 table.
        candidates = [r for r in (rows or []) if r.get('Flags') == 2
                      and r.get('ItemExtendedCostID') == 0
                      and r.get('ItemLogicalCostGroupID') == 0
                      and r.get('PlayerConditionID') == 0
                      and r.get('ItemLevelSelectorID') == 0
                      and type(r.get('SequenceValue')) is int and r['SequenceValue'] > 0]
        if not base or base.get('ItemExtendedCostID') != 0 or len(candidates) != 1:
            issue('ItemBonusTreeNode', 'IblGroupPointsModSetID', node['IblGroupPointsModSetID'],
                  f'group {group}: unique accessible no-cost initial sequence not proven')
            return
        selected = candidates[0]
        bonuses.add(selected['ItemBonusListID'])
        out['initial_groups'].append(dict(group_id=group, entry_id=selected['ID'],
             sequence_value=selected['SequenceValue'], bonus_id=selected['ItemBonusListID'],
             points_mod_set_id=node['IblGroupPointsModSetID'],
             rule='unique_accessible_no_cost_entry', flags=selected['Flags'],
             points_mod_scope='server generation input; not interpreted as a DB2 table'))

    def inspect_excluded(row, ancestors):
        """Prove excluded optional branches cannot modify numeric level/quality.

        Read their real effects without adding their bonus IDs. Type37 is a
        creation conversion hook (native NYI), not a scalar item-level bonus;
        it remains explicitly outside this same-item initial-drop projection.
        """
        if row.get('ChildItemLevelSelectorID') or row.get('ChildItemBonusListGroupID'):
            issue('ItemBonusTreeNode', 'ID', row['ID'], 'conditional level selector/group unresolved')
        bonus = row.get('ChildItemBonusListID')
        if bonus:
            effects = read('ItemBonus', 'ParentItemBonusListID', bonus)
            safe = {0, 2, 4, 5, 6, 7, 9, 10, 12, 14, 15, 16, 20, 21, 22,
                    23, 24, 25, 26, 28, 29, 30, 31, 32, 35, 37, 38, 39, 40, 41, 44, 45, 46, 47}
            if effects is not None:
                for effect in effects:
                    if effect.get('Type') not in safe:
                        issue('ItemBonus', 'ID', effect['ID'],
                              'conditional branch may affect level/quality; gate input required')
                    else:
                        issue('ItemBonus', 'ID', effect['ID'],
                              f'excluded conditional Type {effect["Type"]}; no scalar level/quality effect', blocking=False)
        tree = row.get('ChildItemBonusTreeID')
        if tree:
            if tree in ancestors:
                issue('ItemBonusTreeNode', 'ParentItemBonusTreeID', tree, 'conditional cycle')
                return
            children = read('ItemBonusTreeNode', 'ParentItemBonusTreeID', tree)
            for child in children or []:
                inspect_excluded(child, ancestors | {tree})

    def visit(tree, ancestors):
        if tree in ancestors:
            issue('ItemBonusTreeNode', 'ParentItemBonusTreeID', tree, 'cycle')
            return
        rows = read('ItemBonusTreeNode', 'ParentItemBonusTreeID', tree)
        if rows is None:
            return
        from botend.services.wow_item_difficulty_tree import tree_applies, TreeUnresolved
        try:
            applicability = tree_applies(tree, item_id, item_context, read_table=read)
        except TreeUnresolved as exc:
            issue('ItemBonusTree', 'ID', tree, str(exc))
            return
        if not applicability['applies']:
            issue('ItemBonusTree', 'ID', tree, applicability['reason'], blocking=False)
            return
        # Native ApplyBonusTreeHelper returns when the tree has no node entry.
        # A verified complete empty envelope is a terminal/no-op; None is not.
        for row in rows:
            ctx = row.get('ItemContext')
            if type(ctx) is not int:
                issue('ItemBonusTreeNode', 'ID', row['ID'], 'missing ItemContext')
                continue
            if ctx not in (0, item_context):
                continue
            gated = False
            for field in ('ItemCreationContextGroupID', 'MinMythicPlusLevel', 'MaxMythicPlusLevel', 'Flags'):
                value = row.get(field)
                if type(value) is not int:
                    issue('ItemBonusTreeNode', 'ID', row['ID'], f'missing gate {field}')
                    gated = True
                elif value != 0:
                    issue('ItemBonusTreeNode', field, value,
                          f'node {row["ID"]}: conditional creation/roll rule excluded from initial drop', blocking=False)
                    gated = True
            if gated:
                inspect_excluded(row, ancestors | {tree})
                continue
            edges = ('ChildItemBonusTreeID', 'ChildItemBonusListID', 'ChildItemLevelSelectorID',
                     'ChildItemBonusListGroupID', 'IblGroupPointsModSetID')
            if any(type(row.get(f)) is not int or row[f] < 0 for f in edges):
                issue('ItemBonusTreeNode', 'ID', row['ID'], 'missing/invalid edge field')
                continue
            if row['ChildItemBonusTreeID']:
                visit(row['ChildItemBonusTreeID'], ancestors | {tree})
            elif row['ChildItemBonusListID']:
                bonuses.add(row['ChildItemBonusListID'])
            elif row['ChildItemLevelSelectorID']:
                selectors.add(row['ChildItemLevelSelectorID'])
            elif row['ChildItemBonusListGroupID']:
                group_initial(row)

    def fixed_base(sparse=None, *, scaling_level=None):
        """Native base + Type1, followed by every applicable same-build squish.

        (table, None, None) requests the complete unfiltered era envelope.
        A static base never substitutes for an unknown player scaling rule.
        """
        sparse = sparse if sparse is not None else one('ItemSparse', item_id)
        if sparse is None:
            return out
        from urllib.parse import parse_qs, urlsplit
        source = next(d for d in out['evidence'] if d['table'] == 'ItemSparse')
        try:
            url = urlsplit(source['url'])
            q = parse_qs(url.query, keep_blank_values=True)
            english = (url.scheme == 'https' and url.netloc == 'wago.tools' and not url.fragment
                and q.get('build') == [build] and q.get('locale') == ['enUS']
                and ((url.path == '/api/db2-find/ItemSparse'
                      and q.get('filter[ID]') == ['exact:' + str(item_id)]
                      and set(q) == {'build', 'locale', 'filter[ID]'})
                     or (url.path == '/db2/ItemSparse/csv' and set(q) == {'build', 'locale'}
                         and type(source.get('source_total')) is int and source['source_total'] > 0
                         and isinstance(source.get('file_sha256'), str)
                         and len(source['file_sha256']) == 64
                         and all(c in '0123456789abcdef' for c in source['file_sha256']))))
        except (TypeError, ValueError):
            english = False
        if not english:
            issue('ItemSparse', 'ID', item_id, 'exact enUS source identity required')
        for f in ('PlayerLevelToItemLevelCurveID', 'ItemLevelOffsetCurveID',
                  'ItemLevelOffsetItemLevel', 'ContentTuningID'):
            if scaling_level is None and (type(sparse.get(f)) is not int or sparse[f] != 0):
                issue('ItemSparse', f, item_id, 'dynamic scaling input not proven; fixed base forbidden')
        for f, low, high in (('ItemLevel', 1, None), ('OverallQualityID', 0, 7),
                             ('ItemSquishEraID', 0, None), ('InventoryType', 1, None),
                             ('RequiredLevel', 0, None)):
            v = sparse.get(f)
            if type(v) is not int or v < low or (high is not None and v > high):
                issue('ItemSparse', f, item_id, 'invalid typed base value')
        if out['unresolved']:
            return out
        level = sparse['ItemLevel'] if scaling_level is None else scaling_level
        if base_overrides and scaling_level is None:
            priority = min(p for p, _ in base_overrides)
            values = {v for p, v in base_overrides if p == priority}
            if len(values) != 1:
                issue('ItemBonus', 'Type', 42, 'equal-priority base override conflict')
                return out
            level = values.pop()
        if scaling_level is None:
            level += additive
        eras = read('ItemSquishEra', None, None)
        if not eras:
            issue('ItemSquishEra', 'ID', sparse['ItemSquishEraID'], 'complete squish era table required')
            return out
        try:
            major, minor, patch = map(int, build.split('.')[:3])
            current_patch = major * 10000 + minor * 100 + patch
        except ValueError:
            issue('ItemSquishEra', 'Patch', build, 'invalid rule-build patch identity')
            return out
        evaluations = []
        for era in sorted(eras, key=lambda r: r['ID']):
            if any(type(era.get(f)) is not int or era[f] < 0 for f in ('Patch', 'CurveID', 'Flags')):
                issue('ItemSquishEra', 'ID', era['ID'], 'invalid typed era rule')
                continue
            if era['Flags'] & ~3:
                issue('ItemSquishEra', 'Flags', era['ID'], 'unknown squish flags')
                continue
            if era['ID'] <= sparse['ItemSquishEraID'] or era['Flags'] & 1 or era['Patch'] > current_patch or not era['CurveID']:
                continue
            cid = era['CurveID']
            curve = one('Curve', cid)
            points = read('CurvePoint', 'CurveID', cid)
            if not curve or curve.get('Type') != 0 or not points:
                issue('Curve', 'ID', cid, 'missing or nonlinear fixed-base curve')
                return out
            if any(type(p.get('OrderIndex')) is not int or
                   any(type(p.get(f)) not in (int, float) or not isfinite(p[f])
                       for f in ('Pos_0', 'Pos_1')) for p in points):
                issue('CurvePoint', 'CurveID', cid, 'invalid typed interpolation points')
                return out
            pairs = [(p['Pos_0'], p['Pos_1']) for p in sorted(points, key=lambda p: p['OrderIndex'])]
            if any(a[0] >= b[0] for a, b in zip(pairs, pairs[1:])):
                issue('CurvePoint', 'CurveID', cid, 'nonmonotonic interpolation points')
                return out
            before = level
            if level <= pairs[0][0]:
                y = pairs[0][1]
            elif level >= pairs[-1][0]:
                y = pairs[-1][1]
            else:
                a, b = next((a, b) for a, b in zip(pairs, pairs[1:]) if a[0] <= level <= b[0])
                y = a[1] + (level - a[0]) * (b[1] - a[1]) / (b[0] - a[0])
            level = floor(y + 0.5)
            out['curve_ids'].append(cid)
            evaluations.append(dict(era_id=era['ID'], curve_id=cid, input=before, result=level))
        quality = max(qualities) if qualities else sparse['OverallQualityID']
        out['proven_quality'] = quality
        if level <= 0:
            issue('ItemSparse', 'ItemLevel', item_id, 'nonpositive evaluated base level')
        if not out['unresolved']:
            out.update(status='resolved', initial_drop_kind='fixed_base', item_level=level,
                       quality=quality, bonus_ids=sorted(bonuses),
                       base_rule=dict(rule=('native_base_override_increment_then_squish' if scaling_level is None
                                            else 'native_direct_offset_then_squish'),
                                      item_sparse=dict(sparse), squish_evaluations=evaluations))
        return out

    additive = 0
    assignments = read('ItemXBonusTree', 'ItemID', item_id)
    if assignments == []:
        # Native ITEM_FLAG4_COSMETIC_ITEM is DB2 ItemSparse.Flags_3 bit 0x10000.
        # A complete empty assignment is not itself a level rule. Only explicitly
        # non-scaling cosmetics may use the typed same-build base values.
        from urllib.parse import parse_qs, urlsplit
        sparse = one('ItemSparse', item_id)
        if sparse is None:
            return out
        if type(sparse.get('Flags_3')) is int and sparse['Flags_3'] >= 0 and not sparse['Flags_3'] & 0x10000:
            return fixed_base(sparse)
        sparse_source = next(d for d in out['evidence'] if d['table'] == 'ItemSparse')
        try:
            url = urlsplit(sparse_source['url'])
            query = parse_qs(url.query, keep_blank_values=True)
            english = (url.scheme == 'https' and url.netloc == 'wago.tools' and not url.fragment
                       and query.get('locale') == ['enUS'] and query.get('build') == [build]
                       and ((url.path == '/api/db2-find/ItemSparse'
                             and query.get('filter[ID]') == ['exact:' + str(item_id)]
                             and set(query) == {'build', 'locale', 'filter[ID]'})
                            or (url.path == '/db2/ItemSparse/csv' and set(query) == {'build', 'locale'}
                                and type(sparse_source.get('source_total')) is int
                                and sparse_source['source_total'] >= sparse_source['total'] > 0
                                and isinstance(sparse_source.get('file_sha256'), str)
                                and len(sparse_source['file_sha256']) == 64
                                and all(c in '0123456789abcdef' for c in sparse_source['file_sha256']))))
        except (TypeError, ValueError):
            english = False
        flags = sparse.get('Flags_3')
        curves = ('PlayerLevelToItemLevelCurveID', 'ItemLevelOffsetCurveID',
                  'ItemLevelOffsetItemLevel')
        if not english:
            issue('ItemSparse', 'ID', item_id, 'exact enUS source identity required')
        if type(flags) is not int or flags < 0 or not flags & 0x10000:
            issue('ItemSparse', 'Flags_3', item_id, 'no typed cosmetic flag')
        for field in curves:
            if type(sparse.get(field)) is not int or sparse[field] != 0:
                issue('ItemSparse', field, item_id, 'explicit zero scaling field required')
        for field, minimum, maximum in (('ItemLevel', 1, None), ('OverallQualityID', 0, 7),
                                        ('RequiredLevel', 0, None), ('InventoryType', 1, None)):
            value = sparse.get(field)
            if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
                issue('ItemSparse', field, item_id, 'invalid typed fixed base value')
        if not out['unresolved']:
            out.update(status='resolved', initial_drop_kind='fixed_cosmetic',
                       item_level=sparse['ItemLevel'], quality=sparse['OverallQualityID'],
                       proven_quality=sparse['OverallQualityID'],
                       base_rule=dict(rule='cosmetic_flag_and_explicit_zero_scaling_with_empty_assignment',
                                      cosmetic_flag=0x10000, item_sparse=dict(sparse)))
        return out
    if assignments is not None:
        for row in assignments:
            tree = row.get('ItemBonusTreeID')
            if type(tree) is int and tree > 0:
                visit(tree, set())
            else:
                issue('ItemXBonusTree', 'ID', row['ID'], 'invalid root tree')
    if len(selectors) > 1:
        issue('ItemLevelSelector', 'ID', sorted(selectors), 'multiple non-equivalent selectors; native application order needed')
    elif selectors:
        from botend.services.wow_item_difficulty_selector import selector_bonus_ids, SelectorUnresolved
        selector_id = next(iter(selectors))
        sparse = one('ItemSparse', item_id)
        if sparse is not None:
            try:
                selector = selector_bonus_ids(selector_id, sparse, read_table=read)
                bonuses.update(selector['bonus_ids'])
                out['selector_rule'] = selector
            except SelectorUnresolved as exc:
                issue('ItemLevelSelector', 'ID', selector_id, str(exc))

    # Effects with no level/quality change. In particular Type34 Value_1 is
    # group metadata, not an absolute item level, and Type26 is socket binding.
    no_level = {0, 2, 4, 5, 6, 7, 8, 9, 10, 12, 14, 15, 16, 17, 18, 19, 20,
                21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 38,
                39, 40, 41, 44, 45, 46, 47}
    additive = 0
    for bonus in sorted(bonuses):
        rows = read('ItemBonus', 'ParentItemBonusListID', bonus)
        if not rows:
            if rows is not None:
                issue('ItemBonus', 'ParentItemBonusListID', bonus, 'referenced bonus empty')
            continue
        for row in sorted(rows, key=lambda r: (r.get('OrderIndex', 0), r['ID'])):
            kind, value = row.get('Type'), row.get('Value_0')
            if type(kind) is not int or type(value) is not int:
                issue('ItemBonus', 'ID', row['ID'], 'invalid Type/Value_0')
            elif kind == 3 and 0 <= value <= 8:
                qualities.append(value)
            elif kind in (11, 13, 48, 49, 51):
                priority = row.get('Value_3') if kind == 48 else row.get('Value_1')
                if type(priority) is not int:
                    issue('ItemBonus', 'ID', row['ID'], 'missing scaling priority')
                else:
                    rules.append((priority, row))
            elif kind == 53:
                issue('ItemBonus', 'ID', row['ID'], 'scaling increment priority unsupported')
            elif kind == 1:
                additive += value
            elif kind == 42:
                if type(row.get('Value_1')) is not int or value <= 0:
                    issue('ItemBonus', 'ID', row['ID'], 'invalid base override/priority')
                else:
                    base_overrides.append((row['Value_1'], value))
            elif kind not in no_level:
                issue('ItemBonus', 'ID', row['ID'], f'Type {kind} level/quality effect unsupported')
    out['proven_bonus_ids'] = sorted(bonuses)
    out['proven_quality'] = max(qualities) if qualities else None
    if not rules:
        return fixed_base()
    priority = min(p for p, _ in rules)
    selected = [r for p, r in rules if p == priority]
    if len({(r['Type'], r['Value_0'], r.get('Value_1'), r.get('Value_2'), r.get('Value_3')) for r in selected}) != 1:
        issue('ItemBonus', 'Value_1', priority, 'equal-priority scaling conflict; application order needed')
        return out
    row = selected[0]
    if row['Type'] in (11, 13):
        # Value_0 is not an item level. Native sets tuning and player curve
        # from Value_2/3; FIXED only captures a creation level if a curve exists.
        if any(type(row.get(f)) is not int or row[f] < 0 for f in ('Value_2', 'Value_3')):
            issue('ItemBonus', 'ID', row['ID'], 'typed scaling tuning/curve inputs required')
            return out
        if row['Value_3']:
            issue('ItemBonus', 'ID', row['ID'], 'creation/player level needed for player-level curve')
            return out
        sparse = one('ItemSparse', item_id)
        if sparse is None:
            return out
        sparse = {**sparse, 'PlayerLevelToItemLevelCurveID': 0, 'ContentTuningID': 0}
        return fixed_base(sparse)
    if row['Type'] == 48:
        # Native uses Value_0 directly as Curve.ID, Value_1 as its input;
        # unlike Type49 there is no ItemOffsetCurve lookup or additive Type1.
        cid, x = row['Value_0'], row.get('Value_1')
        if cid <= 0 or type(x) is not int or x < 0:
            issue('ItemBonus', 'ID', row['ID'], 'invalid direct offset curve/input')
            return out
        curve = one('Curve', cid)
        points = read('CurvePoint', 'CurveID', cid)
        if not curve or curve.get('Type') != 0 or not points:
            issue('Curve', 'ID', cid, 'missing or nonlinear direct offset curve')
            return out
        if any(type(p.get('OrderIndex')) is not int or any(type(p.get(f)) not in (int, float)
               or not isfinite(p[f]) for f in ('Pos_0', 'Pos_1')) for p in points):
            issue('CurvePoint', 'CurveID', cid, 'invalid typed interpolation points')
            return out
        pairs = [(p['Pos_0'], p['Pos_1']) for p in sorted(points, key=lambda p: p['OrderIndex'])]
        if any(a[0] >= b[0] for a, b in zip(pairs, pairs[1:])):
            issue('CurvePoint', 'CurveID', cid, 'nonmonotonic interpolation points')
            return out
        if x <= pairs[0][0]:
            y = pairs[0][1]
        elif x >= pairs[-1][0]:
            y = pairs[-1][1]
        else:
            a, b = next((a, b) for a, b in zip(pairs, pairs[1:]) if a[0] <= x <= b[0])
            y = a[1] + (x-a[0]) * (b[1]-a[1]) / (b[0]-a[0])
        level = floor(y + 0.5)
        out['curve_ids'].append(cid)
        out['level_evaluation'] = dict(input=x, curve_id=cid, offset=0, result=level,
                                      ignored_non_scaling_increment=additive)
        return fixed_base(scaling_level=level)
    if row['Type'] != 49:
        issue('ItemBonus', 'ID', row['ID'], 'creation/player level needed for Type51 scaling config')
        return out
    config = one('ItemScalingConfig', row['Value_0'])
    if not config:
        return out
    out['scaling_configs'].append(config)
    if type(config.get('Flags')) is not int or config['Flags'] not in (0, 1):
        issue('ItemScalingConfig', 'ID', config['ID'], 'content-tuning/squish flag evaluation needed')
        return out
    offset = one('ItemOffsetCurve', config['ItemOffsetCurveID'])
    if not offset:
        return out
    curve_id = offset['CurveID']
    out['curve_ids'] = [curve_id]
    curve = one('Curve', curve_id)
    points = read('CurvePoint', 'CurveID', curve_id)
    if not curve or not points:
        issue('CurvePoint', 'CurveID', curve_id, 'missing interpolation points')
        return out
    if curve.get('Type') != 0:
        issue('Curve', 'ID', curve_id, 'nonlinear interpolation unsupported')
        return out
    points = sorted(points, key=lambda p: p['OrderIndex'])
    x = config['ItemLevel']
    pairs = [(p['Pos_0'], p['Pos_1']) for p in points]
    if not all(isfinite(v) for pair in pairs for v in pair) or any(a[0] >= b[0] for a, b in zip(pairs, pairs[1:])):
        issue('CurvePoint', 'CurveID', curve_id, 'invalid/nonmonotonic points')
        return out
    if x <= pairs[0][0]:
        y = pairs[0][1]
    elif x >= pairs[-1][0]:
        y = pairs[-1][1]
    else:
        a, b = next((a, b) for a, b in zip(pairs, pairs[1:]) if a[0] <= x <= b[0])
        y = a[1] + (x - a[0]) * (b[1] - a[1]) / (b[0] - a[0])
    # Native std::round for positive item levels. Type1 applies only on the
    # non-scaling path; it must not be added to a Type49 scaling configuration.
    level = offset['Offset'] + floor(y + 0.5)
    out['level_evaluation'] = dict(config_id=config['ID'], input=x, required_level=config['RequiredLevel'],
        curve_id=curve_id, offset=offset['Offset'], result=level, ignored_non_scaling_increment=additive)
    if level <= 0:
        issue('ItemScalingConfig', 'ID', config['ID'], 'nonpositive computed level')
    if out['proven_quality'] is None:
        sparse = one('ItemSparse', item_id)
        if sparse is not None and type(sparse.get('OverallQualityID')) is int and 0 <= sparse['OverallQualityID'] <= 7:
            out['proven_quality'] = sparse['OverallQualityID']
        else:
            issue('ItemSparse', 'OverallQualityID', item_id, 'no proven typed inherited quality')
    if not out['unresolved']:
        out.update(status='resolved', bonus_ids=sorted(bonuses), item_level=level, quality=out['proven_quality'])
    return out


def difficulty_drop_metadata(facts: dict, *, difficulty_id: int, instance_id: int) -> dict:
    """Central collector schema; caller provides authoritative journal IDs."""
    if facts.get('status') != 'resolved':
        raise ValueError('cannot publish unresolved initial drop')
    if any(type(v) is not int or v <= 0 for v in (difficulty_id, instance_id)):
        raise ValueError('positive journal difficulty and instance IDs required')
    result = dict(schema=1, difficulty_id=difficulty_id, item_context=facts['item_context'],
                  instance_id=instance_id, rule_build=facts['build'], quality=facts['quality'],
                  provider='wago', bonus_ids=facts['bonus_ids'])
    if facts.get('initial_drop_kind') == 'fixed_cosmetic':
        result['initial_drop_kind'] = 'fixed_cosmetic'
    return result
