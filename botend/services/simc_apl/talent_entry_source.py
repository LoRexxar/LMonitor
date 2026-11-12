"""Resolve Entry -> intrinsic SpellID from the selected, hash-bound DB2 source.

Backfilled nodes may store a VisibleSpellID in both spell columns. That must not
break the original TraitDefinition.SpellID identity used by SimC, or make a lone
unrelated Entry an acceptable fallback.
"""
import csv
import hashlib
import io
import json
from pathlib import Path
from botend.wow.branches import data_branch


def definition_spell_ids(version, entry_ids):
    wanted = {i for i in entry_ids if type(i) is int and i > 0}
    if not version or not version.source_dir or not wanted:
        return {}
    try:
        root = Path(version.source_dir)
        path = root / 'manifest.json'
        if path.is_symlink() or path.stat().st_size > 200_000:
            return {}
        raw = path.read_bytes()
        if len(root.name) == 64 and all(c in '0123456789abcdef' for c in root.name):
            if hashlib.sha256(raw).hexdigest() != root.name:
                return {}
        manifest = json.loads(raw)
        entries, definitions = {}, {}
        for key, name, table, locale in (
                ('TraitNodeEntry', 'TraitNodeEntry.csv', 'TraitNodeEntry', 'zhCN'),
                ('TraitDefinition.enUS', 'TraitDefinition_enUS.csv', 'TraitDefinition', 'enUS')):
            fact = manifest[key]
            if fact['identity'] != {'branch': data_branch(version.branch), 'build': version.current_build,
                                    'locale': locale, 'table': table}:
                return {}
            path = root / name
            if path.is_symlink() or path.stat().st_size > 64_000_000:
                return {}
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != fact['sha256']:
                return {}
            count = 0
            definition_ids = set(entries.values())
            for row in csv.DictReader(io.StringIO(body.decode('utf-8-sig'), newline='')):
                count += 1
                identity = int(row['ID'])
                if key == 'TraitNodeEntry' and identity in wanted:
                    if identity in entries:
                        return {}
                    entries[identity] = int(row['TraitDefinitionID'])
                elif key == 'TraitDefinition.enUS' and identity in definition_ids:
                    if identity in definitions:
                        return {}
                    definitions[identity] = int(row.get('SpellID') or 0)
            if type(fact['rows']) is not int or count != fact['rows']:
                return {}
        return {entry: definitions[d] for entry, d in entries.items() if definitions.get(d, 0) > 0}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
