"""Validate Wago Hotfix payloads before using them as DB2 change facts.

The current-build DB2 row supplies column *names*, never Hotfix before/after values.
Wago groups consecutive ``_0``, ``_1`` ... columns into positional arrays.
An unrecognized layout is unresolved rather than guessed.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping


_ARRAY_ZERO = re.compile(r'^(.*)_0$')


def _text(value):
    if value is None or isinstance(value, (list, dict)):
        return None
    return str(value)


def decode_hotfix_row(payload, schema_row: Mapping, *, record_id: int):
    if not isinstance(payload, list) or not isinstance(schema_row, Mapping):
        return None
    keys = list(schema_row)
    if not keys or keys[0] != 'ID':
        return None
    result = {}
    col = 0
    pos = 0
    while col < len(keys):
        if pos >= len(payload):
            return None
        name = keys[col]
        match = _ARRAY_ZERO.fullmatch(name)
        if match:
            base = match.group(1)
            members = []
            while col + len(members) < len(keys) and keys[col + len(members)] == f'{base}_{len(members)}':
                members.append(keys[col + len(members)])
            if len(members) > 1:
                values = payload[pos]
                if not isinstance(values, list) or len(values) != len(members):
                    return None
                for key, value in zip(members, values):
                    result[key] = _text(value)
                col += len(members)
                pos += 1
                continue
        # Wago 的旧记录省略内嵌 ID；此时使用已经过精确筛选的来源 record_id。
        # 非空但不匹配的 ID 仍在下方拒绝，不能覆盖错误身份。
        result[name] = str(record_id) if name == 'ID' and payload[pos] == '' else _text(payload[pos])
        col += 1
        pos += 1
    if pos != len(payload) or any(v is None for v in result.values()):
        return None
    try:
        valid = int(result.get('ID') or 0) == int(record_id)
    except (TypeError, ValueError):
        valid = False
    return result if valid else None


def hotfix_record_identity(row):
    """Build is part of a physical record's scope, never a tie breaker."""
    return (
        str(row.get('region_id') or ''), str(row.get('locale') or ''),
        str(row.get('table_name') or '').lower(), str(row.get('record_id') or ''),
        str(row.get('build') or ''),
    )


def previous_hotfix_row(rows, target):
    """Return the last unambiguous source row in the same exact build scope.

    A different build is not a verified predecessor, even with an older push.
    Cross-build migration needs separate proven lineage, not numeric proximity.
    """

    scoped = [row for row in rows if isinstance(row, dict)
              and hotfix_record_identity(row) == hotfix_record_identity(target)
              and int(row.get('push_id') or 0) < int(target.get('push_id') or 0)]
    if not scoped:
        return None
    latest_push = max(int(row.get('push_id') or 0) for row in scoped)
    latest = [row for row in scoped if int(row.get('push_id') or 0) == latest_push]
    def comparable_payload(row):
        data = row.get('data')
        if isinstance(data, list) and data and data[0] == '':
            return [int(row['record_id']), *data[1:]]
        return data
    # 同一版本的旧格式空 ID 可归一化；相互矛盾的来源仍然拒绝。
    if not latest or not isinstance(latest[0].get('data'), list) or any(
        comparable_payload(row) != comparable_payload(latest[0])
        or row.get('status') != latest[0].get('status') for row in latest
    ):
        return None
    return latest[0]


def previous_hotfix_payload(rows, target):
    row = previous_hotfix_row(rows, target)
    return row.get('data') if row else None


def field_changes(before: Mapping | None, after: Mapping | None):
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return []
    return [
        {'field': field, 'before': before.get(field, '旧值未核实'), 'after': value}
        for field, value in after.items()
        if field != 'ID' and (field not in before or before[field] != value)
    ]


def client_baseline_changes(before, after):
    """客户端基表仅用于明确标注的对照，忽略 Wago 浮点序列化末位误差。"""
    changes = []
    for item in field_changes(before, after):
        old, new = item['before'], item['after']
        try:
            left, right = Decimal(str(old)), Decimal(str(new))
            if left.is_finite() and right.is_finite():
                if left == right:
                    continue
                if (left != left.to_integral_value() or right != right.to_integral_value()) and abs(left - right) <= max(abs(left), abs(right)) * Decimal('1e-10'):
                    continue
        except InvalidOperation:
            pass
        changes.append(item)
    return changes


def build_hotfix_facts(rows, *, schema_for, previous_for):
    """One canonical, source-identified fact set for both report projections."""
    facts = []
    for source in rows:
        record_id = int(source['record_id'])
        try:
            schema = schema_for(source)
        except Exception:
            schema = None
        after = decode_hotfix_row(source.get('data'), schema, record_id=record_id)
        before = None
        predecessor = None
        if after is not None:
            try:
                predecessor = previous_for(source)
            except Exception:
                predecessor = None
            if isinstance(predecessor, Mapping) and 'data' in predecessor:
                try:
                    before = decode_hotfix_row(predecessor['data'], schema_for(predecessor), record_id=record_id)
                except Exception:
                    before = None
            else:
                before = decode_hotfix_row(predecessor, schema, record_id=record_id)
        facts.append({
            'source': source,
            'before': before,
            'before_source': dict(predecessor) if before is not None and isinstance(predecessor, Mapping) else None,
            'after': after,
            'changes': field_changes(before, after),
            'before_verified': before is not None,
            'after_verified': after is not None,
        })
    return facts


def project_class_spell_changes(facts, *, is_class_spell):
    """Project source facts to the existing class report's per-record shape."""
    spells = {}
    identity_fields = {'ID'}  # SpellID/EffectIndex changes affect the actual skill or effect slot.
    for fact in facts:
        source = fact.get('source') or {}
        after = fact.get('after')
        if not isinstance(after, dict):
            continue
        table = str(source.get('table_name') or '').strip().lower()
        if not table.startswith('spell'):
            continue
        try:
            spell_id = int(after.get('SpellID') or (after.get('ID') if table in ('spell', 'spellname', 'spelldescription', 'spellmisc') else 0) or 0)
            record_id = int(source.get('record_id') or 0)
            push_id = int(source.get('push_id') or 0)
            effect_index = int(after.get('EffectIndex') or 0)
        except (TypeError, ValueError):
            continue
        if spell_id <= 0 or record_id <= 0 or not is_class_spell(spell_id, source):
            continue
        if fact.get('before_verified'):
            changes = fact.get('changes') or []
            fields = [dict(item) for item in changes if item.get('field') not in identity_fields]
            action = 'changed'
        elif fact.get('client_baseline'):
            fields = client_baseline_changes(fact['client_baseline'], after)
            action = 'baseline'
        else:
            fields = [
                {'field': name, 'before': '旧值未核实', 'after': value}
                for name, value in after.items()
                if name not in identity_fields and str(value) not in ('', '0', '0.0')
            ]
            action = 'observed'
        if not fields:
            continue
        meta = {'PushID': push_id}
        if fact.get('source_build'):
            meta['SourceBuild'] = fact['source_build']
        if fact.get('before_source'):
            meta['BeforePushID'] = fact['before_source'].get('push_id')
            meta['BeforeSourceBuild'] = fact.get('before_source_build') or fact['before_source'].get('build')
        if action == 'baseline':
            meta['ComparisonKind'] = 'client_baseline'
        elif fact.get('client_baseline') == {}:
            meta['BaselineMissing'] = True
        if table == 'spelleffect':
            meta['EffectIndex'] = effect_index
            if 'Effect' in after:
                meta['Effect'] = after['Effect']
        spells.setdefault(spell_id, {'tables': set(), 'diffs': {}})
        spells[spell_id]['tables'].add(table)
        spells[spell_id]['diffs'].setdefault(table, []).append({
            'id': record_id,
            'action': action,
            'meta': meta,
            'fields': fields,
        })
    return spells
