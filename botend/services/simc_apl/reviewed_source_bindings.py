"""Reviewed C++ bindings fill unlearned-actor gaps, never infer identities by name.

Data is pinned to an exact upstream revision and game build. Each typed scope has
its source blob digest and verbatim line evidence in a tracked, reviewable file.
A different binary revision/build must acquire its own evidence, not reuse this.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


DATA = Path(__file__).resolve().parents[2] / 'data' / 'simc_apl_source_bindings'
KINDS = frozenset({'action', 'buff', 'debuff', 'dot', 'cooldown', 'talent'})


def _source_path(value):
    return (isinstance(value, str) and value.startswith('engine/')
            and bool(re.fullmatch(r'engine/[A-Za-z0-9_./-]+\.(?:cpp|hpp|inc)', value))
            and '..' not in PurePosixPath(value).parts)


def _key(row):
    return tuple(row.get(k) or '' for k in
                 ('class_name', 'spec', 'hero_tree', 'symbol_kind', 'token'))


def load_reviewed_source_bindings(revision, build):
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        return {}
    path = DATA / f'{revision}.json'
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 2_000_000:
        raise ValueError('invalid reviewed source binding archive')
    data = json.loads(path.read_text(encoding='utf-8'))
    if type(data.get('schema_version')) is not int or data['schema_version'] != 1 or data.get('simc_revision') != revision:
        raise ValueError('invalid reviewed source binding archive identity')
    if data.get('game_build') != build:
        return {}
    bindings = data.get('bindings')
    if not isinstance(bindings, list) or len(bindings) > 512:
        raise ValueError('invalid reviewed source binding count')
    result = {}
    for row in bindings:
        if not isinstance(row, dict):
            raise ValueError('invalid reviewed source binding row')
        sid = row.get('spell_id')
        source = row.get('source_path', '')
        evidence = row.get('evidence')
        identity = _key(row)
        if (row.get('symbol_kind') not in KINDS or type(sid) is not int or sid <= 0
                or any(not isinstance(row.get(k), str) or not re.fullmatch(r'[a-z][a-z0-9_]*', row[k])
                       for k in ('class_name', 'spec', 'token'))
                or row.get('hero_tree') is not None and (not isinstance(row['hero_tree'], str)
                                                       or not re.fullmatch(r'[a-z][a-z0-9_]*', row['hero_tree']))
                or not _source_path(source)
                or not isinstance(row.get('source_blob_sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', row['source_blob_sha256'])
                or not isinstance(evidence, list) or not evidence
                or identity in result):
            raise ValueError('invalid or duplicate reviewed source binding')
        files = row.get('source_files', {source: row['source_blob_sha256']})
        if (not isinstance(files, dict) or files.get(source) != row['source_blob_sha256']
                or any(not _source_path(name) or not isinstance(value, str)
                       or not re.fullmatch(r'[0-9a-f]{64}', value)
                       for name, value in files.items())):
            raise ValueError('invalid reviewed source binding blob manifest')
        for excerpt in evidence:
            if (not isinstance(excerpt, dict) or type(excerpt.get('start_line')) is not int
                    or type(excerpt.get('end_line')) is not int
                    or not 1 <= excerpt['start_line'] <= excerpt['end_line']
                    or not isinstance(excerpt.get('text'), str) or not excerpt['text'].strip()
                    or len(excerpt['text'].split('\n')) != excerpt['end_line'] - excerpt['start_line'] + 1):
                raise ValueError('invalid reviewed source binding evidence')
            if not _source_path(excerpt.get('source_path', source)) or excerpt.get('source_path', source) not in files:
                raise ValueError('reviewed source excerpt lacks a pinned source blob')
        if not re.search(rf'\b{sid}\b', '\n'.join(e['text'] for e in evidence)):
            raise ValueError('reviewed source binding has no literal ID evidence')
        trait_fields = ('trait_id', 'trait_node_entry_id', 'trait_definition_id')
        if row['symbol_kind'] == 'talent':
            for field in trait_fields:
                value = row.get(field)
                if type(value) is not int or value <= 0 or not re.search(
                        rf'\b{value}\b', '\n'.join(e['text'] for e in evidence)):
                    raise ValueError('reviewed talent binding lacks exact Node/Entry/Definition evidence')
        elif any(row.get(field) is not None for field in trait_fields):
            raise ValueError('non-talent source binding cannot carry trait identity')
        roles = row.get('runtime_role_resolution')
        if roles is not None:
            children = roles.get('background_spell_ids') if isinstance(roles, dict) else None
            observed = roles.get('runtime_candidates') if isinstance(roles, dict) else None
            if (row['symbol_kind'] != 'action' or not isinstance(roles, dict)
                    or roles.get('foreground_spell_id') != sid
                    or roles.get('identity_source') != 'runtime_action'
                    or roles.get('identity_reason') != 'ambiguous_spell_identity'
                    or not isinstance(children, list) or not children
                    or any(type(value) is not int or value <= 0 or value == sid for value in children)
                    or len(set(children)) != len(children)
                    or not isinstance(observed, list) or any(type(value) is not int for value in observed)
                    or len(observed) != len(children) + 1 or set(observed) != {sid, *children}
                    or any(not re.search(rf'\b{value}\b', '\n'.join(e['text'] for e in evidence))
                           for value in children)):
                raise ValueError('invalid reviewed foreground/background source evidence')
        result[identity] = row
    return result


def runtime_role_resolution_matches(fact, binding):
    """An exact reviewed parent/child collision is not arbitrary ID ambiguity."""
    roles = binding.get('runtime_role_resolution')
    return bool(roles and _key(fact) == _key(binding)
                and fact.get('source') == 'simc_manifest' and fact.get('spell_id') is None
                and fact.get('symbol_kind') == 'action'
                and fact.get('identity_source') == roles['identity_source']
                and fact.get('identity_reason') == roles['identity_reason']
                and set(fact.get('identity_candidates') or []) == set(roles['runtime_candidates']))


def enrich_reviewed_source_bindings(facts, revision, build, *, observed_typed=(), identity_diagnostics=()):
    bindings = load_reviewed_source_bindings(revision, build)
    facts = list(facts)
    present = {_key(fact) for fact in facts}
    diagnostics = {}
    for row in identity_diagnostics:
        key = _key(row)
        if key in diagnostics and diagnostics[key] != row:
            raise ValueError('reviewed source binding conflicts with duplicate runtime diagnostics')
        diagnostics[key] = row
    # Some expression factories return no diagnostic row at all. Only a typed
    # demand in this actual APL corpus can admit its separately reviewed fact.
    for key in sorted((set(observed_typed) & set(bindings)) - present):
        binding = bindings[key]
        fact = {field: binding[field] for field in
                ('class_name', 'spec', 'hero_tree', 'symbol_kind', 'token')}
        diagnostic = diagnostics.get(key, {})
        facts.append(dict(fact, spell_id=diagnostic.get('spell_id'), trait_id=None, source='simc_manifest',
                          identity_source=diagnostic.get('identity_source', 'simc_manifest'),
                          identity_candidates=diagnostic.get('identity_candidates', []),
                          identity_reason=diagnostic.get('identity_reason', ''), aliases=[], options=[]))
    output = []
    for fact in facts:
        binding = bindings.get(_key(fact))
        if not binding or fact.get('source') not in {'simc_manifest', 'system_apl'}:
            output.append(fact)
            continue
        sid = binding['spell_id']
        diagnostic = diagnostics.get(_key(fact))
        if diagnostic and (diagnostic.get('spell_id') not in (None, sid)
                           or set(diagnostic.get('identity_candidates') or []) - {sid}
                           or 'ambiguous' in str(diagnostic.get('identity_reason') or '').lower()):
            raise ValueError(f'reviewed source binding conflicts with runtime diagnostics: {_key(fact)!r}')
        current = fact.get('spell_id')
        candidates = set(fact.get('identity_candidates') or [])
        roles_match = runtime_role_resolution_matches(fact, binding)
        if (current is not None and current != sid or candidates - {sid} and not roles_match
                or fact.get('trait_id') not in (None, binding.get('trait_id'))
                or 'ambiguous' in str(fact.get('identity_reason') or '').lower() and not roles_match):
            raise ValueError(f'reviewed source binding conflicts with runtime identity: {_key(fact)!r}')
        if current is not None and (fact['symbol_kind'] != 'talent' or fact.get('trait_id') is not None):
            output.append(fact)  # Preserve actual native evidence and provenance.
            continue
        proof = hashlib.sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True,
                                          separators=(',', ':')).encode()).hexdigest()
        output.append(dict(fact, spell_id=sid, trait_id=binding.get('trait_id'), identity_source='reviewed_simc_source',
                           identity_reason=f'source_binding:{proof}', identity_candidates=[sid]))
    return output
