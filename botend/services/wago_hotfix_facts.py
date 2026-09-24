"""Validate Wago Hotfix payloads before using them as DB2 change facts.

The current-build DB2 row supplies column *names*, never Hotfix before/after values.
Wago groups consecutive ``_0``, ``_1`` ... columns into positional arrays.
An unrecognized layout is unresolved rather than guessed.
"""

from __future__ import annotations

import re
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
        result[name] = _text(payload[pos])
        col += 1
        pos += 1
    if pos != len(payload) or any(v is None for v in result.values()):
        return None
    try:
        valid = int(result.get('ID') or 0) == int(record_id)
    except (TypeError, ValueError):
        valid = False
    return result if valid else None


def previous_hotfix_row(rows, target):
    """Return the last unambiguous source row in the same exact record scope."""
    def identity(row):
        return (
            str(row.get('region_id') or ''), str(row.get('locale') or ''),
            str(row.get('table_name') or '').lower(), str(row.get('record_id') or ''),
        )

    scoped = [row for row in rows if isinstance(row, dict)
              and identity(row) == identity(target)
              and int(row.get('push_id') or 0) < int(target.get('push_id') or 0)]
    if not scoped:
        return None
    latest_push = max(int(row.get('push_id') or 0) for row in scoped)
    latest = [row for row in scoped if int(row.get('push_id') or 0) == latest_push]
    if not latest or not isinstance(latest[0].get('data'), list) or any(
        row.get('data') != latest[0]['data'] or row.get('build') != latest[0].get('build') for row in latest
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
        if after is not None:
            try:
                predecessor = previous_for(source)
            except Exception:
                predecessor = None
            if isinstance(predecessor, Mapping) and 'data' in predecessor:
                before = decode_hotfix_row(predecessor['data'], schema_for(predecessor), record_id=record_id)
            else:
                before = decode_hotfix_row(predecessor, schema, record_id=record_id)
        facts.append({
            'source': source,
            'before': before,
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
        else:
            fields = [
                {'field': name, 'before': '旧值未核实', 'after': value}
                for name, value in after.items()
                if name not in identity_fields and str(value) not in ('', '0', '0.0')
            ]
        if not fields:
            continue
        meta = {'PushID': push_id}
        if fact.get('source_build'):
            meta['SourceBuild'] = fact['source_build']
        if table == 'spelleffect':
            meta['EffectIndex'] = effect_index
            if 'Effect' in after:
                meta['Effect'] = after['Effect']
        spells.setdefault(spell_id, {'tables': set(), 'diffs': {}})
        spells[spell_id]['tables'].add(table)
        spells[spell_id]['diffs'].setdefault(table, []).append({
            'id': record_id,
            'action': 'changed' if fact.get('before_verified') else 'observed',
            'meta': meta,
            'fields': fields,
        })
    return spells
