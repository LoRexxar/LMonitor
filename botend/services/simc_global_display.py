"""Pure projection of frozen global-scope facts; never rewrites snapshot facts.

TraitEntry is an ownership join, not an effect identity. Physical DBC components
and the complete activation/value state identify a display row. Provenance stays
in ``sources`` rather than creating another copy of the same modifier.
"""
import copy
import json
import re

from botend.constants.simc_effect_ownership import global_effect_matches_owner


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _unique(rows):
    return list({_json(row): copy.deepcopy(row) for row in rows}.values())


def _trait_entry(row):
    """Decode legacy wire IDs only; applicability still requires a frozen join.

    New static sources export trait_entry_id. Old snapshots and the exporter
    reviewed catalog encode it in these exact wire formats (not metadata PKs).
    A malformed or contradictory explicit identity must never fall back.
    """
    identifier = str(row.get('effect_id', ''))
    match = re.fullmatch(r'dbc_global_talent:([1-9][0-9]*)', identifier)
    if identifier.startswith('dbc_global_talent:') and not match:
        return None
    if not match:
        match = re.fullmatch(r'reviewed_scope:天赋:[1-9][0-9]*:([1-9][0-9]*)', identifier)
        if identifier.startswith('reviewed_scope:天赋:') and not match:
            return None
    legacy = int(match[1]) if match else None
    if 'trait_entry_id' in row:
        value = row['trait_entry_id']
        if type(value) is not int or value <= 0 or (legacy is not None and legacy != value):
            return None
        return value
    return legacy


def _parts(row):
    return frozenset(
        (part.get('spell_id'), part.get('effect_index'), part.get('effect_id'))
        for part in row.get('global_components') or []
        if isinstance(part, dict) and all(type(part.get(key)) is int and part[key] > 0
                                        for key in ('spell_id', 'effect_index', 'effect_id'))
    )


def _component_match(row, catalog):
    """Resolve old aggregated indexes only against exact frozen components."""
    parts = _parts(catalog)
    for part in row.get('global_components') or []:
        indexes = part.get('effect_indices') or [part.get('effect_index')]
        for index in indexes:
            matches = [p for p in parts if p[:2] == (part.get('spell_id'), index)]
            if len(matches) != 1 or (part.get('effect_id') is not None and part['effect_id'] != matches[0][2]):
                return False
    return True


def _conditions(row):
    # Presentation enrichment is not activation evidence. All other condition
    # fields (including unknown future native qualifiers) remain in the key.
    ignored = {'name', 'name_zh', 'description', 'description_zh', 'display_name', 'icon', 'icon_url'}
    result = []
    for condition in row.get('runtime_conditions') or []:
        item = {key: value for key, value in condition.items() if key not in ignored}
        item.setdefault('stacks', 1)
        result.append(item)
    return sorted(result, key=_json)


def _state(row):
    state = {key: row.get(key) for key in (
        'hero_subtree_id', 'hero_subtree_ids', 'configuration', 'talent_configuration',
        'rank', 'stacks', 'scope', 'affected_target_counts', 'activation_conditions',
        'value_status', 'dbc_base_multiplier', 'dbc_base_multiplier_range',
    ) if row.get(key) is not None}
    state['runtime_conditions'] = _conditions(row)
    state['source_spell_ids'] = sorted(row.get('source_spell_ids') or [])
    state['scenario_tokens'] = sorted(row.get('scenario_tokens') or [])
    if row.get('runtime_condition'):
        state['runtime_condition'] = row['runtime_condition']
    state['projections'] = sorted(row.get('projections') or [], key=_json)
    state['effect_details'] = sorted(row.get('effect_details') or [], key=_json)
    return _json(state)


def _sources(row):
    return copy.deepcopy(row.get('sources') or [{k: v for k, v in row.items() if k != 'sources'}])


def _merge_evidence(target, source):
    for field in ('local_components', 'local_skill_bindings', 'effect_details'):
        target[field] = _unique([*(target.get(field) or []), *(source.get(field) or [])])
    target['partial_state'] = target.get('partial_state') is True or source.get('partial_state') is True
    evidence = [v for row in (target, source) for v in str(row.get('local_scope_evidence') or '').split('；') if v]
    if evidence:
        target['local_scope_evidence'] = '；'.join(dict.fromkeys(evidence))
    target['sources'] = _unique([*_sources(target), *_sources(source)])


def _same_catalog_family(left, right):
    # A matching name/token/spell alone is insufficient. Catalog rows for a
    # talent and its buff can overlap, but disjoint or conflicting DBC records
    # must never be unioned just because they share an owner spell.
    if set(left.get('source_spell_ids') or []) != set(right.get('source_spell_ids') or []):
        return False
    # Only the catalog's generic source-kind captions are interchangeable.
    # A textual health/equipment/etc. qualifier is still activation evidence,
    # even when structured runtime_conditions are also present.
    generic_conditions = {
        'talent': '启用相应天赋时', 'buff': '自身效果生效时',
        'debuff': '自身施加的目标效果生效时',
    }
    conditions = [row.get('runtime_condition') or '' for row in (left, right)]
    for index, row in enumerate((left, right)):
        if conditions[index] == generic_conditions.get(row.get('source_kind')):
            conditions[index] = ''
    if conditions[0] != conditions[1]:
        return False
    for key in ('source_class', 'specializations', 'hero_subtree_id', 'hero_subtree_ids',
                'runtime_conditions', 'configuration', 'talent_configuration',
                'rank', 'stacks', 'scope', 'affected_target_counts',
                'activation_conditions', 'scenario_tokens', 'value_status',
                'dbc_base_multiplier', 'dbc_base_multiplier_range', 'projections'):
        if left.get(key) != right.get(key):
            return False
    state_kinds = {source.get('source_kind') for row in (left, right)
                   for source in (row.get('sources') or [row])
                   if source.get('source_kind') in {'buff', 'debuff'}}
    if len(state_kinds) > 1:
        return False
    a, b = _parts(left), _parts(right)
    if not a & b:
        return False
    by_index = {}
    for spell, index, physical in a | b:
        key = (spell, index)
        if key in by_index and by_index[key] != physical:
            return False
        by_index[key] = physical
    # The same physical component with different native values is not a union.
    for x in left.get('effect_details') or []:
        for y in right.get('effect_details') or []:
            if (x.get('source_spell_id'), x.get('effect_index')) == (y.get('source_spell_id'), y.get('effect_index')):
                if x != y:
                    return False
    return True


def _is_static(row):
    return row.get('scope_evidence') == 'dbc_global_damage_talent' or str(row.get('effect_id', '')).startswith('dbc_global_talent:')


def _review_bound(row):
    return _is_static(row) or row.get('source_type') == 'reviewed_scope' or (
        row.get('excluded_before_probe') is True
        and row.get('scope_evidence') == 'declared_global_damage_state'
    )


def _definition_can_attach(definition, runtime):
    """A reviewed talent definition is not an additional active multiplier.

    Only its reviewed self/target-state alternative can carry that definition. Do not
    infer arbitrary activation from equal labels/tokens, discard rank-dependent
    values, or promote an unconditioned runtime observation into a buff.
    """
    if not _is_static(definition) or runtime.get('source_type') != 'runtime_state':
        return False
    if definition.get('runtime_conditions') or definition.get('value_status'):
        return False
    if definition.get('activation_conditions') or definition.get('scenario_tokens'):
        return False
    text = definition.get('runtime_condition') or ''
    if text and text != '启用相应天赋时' and not text.startswith('全局增伤分量在职业初始化前排除；'):
        return False
    if _parts(definition) != _parts(runtime):
        return False
    for key in ('hero_subtree_id', 'hero_subtree_ids', 'configuration', 'talent_configuration',
                'rank', 'stacks', 'scope', 'affected_target_counts'):
        if definition.get(key) != runtime.get(key):
            return False
    conditions = _conditions(runtime)
    if len(conditions) != 1 or conditions[0].get('scope') not in {'self', 'target'}:
        return False
    spell = conditions[0].get('spell_id')
    if spell not in (definition.get('source_spell_ids') or []):
        return False
    # Require an actual reviewed state with matching scope and physical evidence.
    kind = 'buff' if conditions[0]['scope'] == 'self' else 'debuff'
    if not any(source.get('source_kind') == kind and spell in (source.get('source_spell_ids') or [])
               and _parts(source) & _parts(definition) for source in runtime.get('sources') or []):
        return False
    projections = definition.get('projections') or []
    return not projections or projections == (runtime.get('projections') or [])


def reviewed_static_source_matches_owner(actor, effect, *, class_name):
    """Generation gate before attaching any reviewed owner/local metadata."""
    entry = _trait_entry(effect)
    if entry is None:
        return False
    spec = actor.get('specialization') or actor.get('spec')
    if not global_effect_matches_owner(effect, class_name, spec):
        return False
    for fact in actor.get('reviewed_global_effects') or []:
        if not isinstance(fact, dict) or _trait_entry(fact) != entry:
            continue
        owners = set(fact.get('source_spell_ids') or [])
        if not owners or not owners <= set(effect.get('source_spell_ids') or []):
            continue
        if effect.get('owner_spell_id') is not None and effect['owner_spell_id'] not in owners:
            continue
        if global_effect_matches_owner(fact, class_name, spec) and _component_match(effect, fact):
            return True
    return False


def normalize_reviewed_global_effects(actor, *, class_name):
    """Return an immutable, idempotent display list from this actor's catalog.

    Absence from a frozen reviewed catalog rejects review-bound sources only;
    independently classified pre-review runtime effects retain their semantics.
    """
    spec = actor.get('specialization') or actor.get('spec')
    facts = [f for f in actor.get('reviewed_global_effects') or [] if isinstance(f, dict)]
    talent_scopes = {}
    for fact in facts:
        if fact.get('source_kind') == 'talent' or _trait_entry(fact) is not None:
            for spell in fact.get('source_spell_ids') or []:
                if not fact.get('specializations'):
                    talent_scopes[spell] = None
                elif spell not in talent_scopes or talent_scopes[spell] is not None:
                    talent_scopes.setdefault(spell, set()).update(fact['specializations'])
    allowed = [f for f in facts if global_effect_matches_owner(f, class_name, spec, talent_scopes) and _parts(f)]
    groups = []
    for fact in allowed:
        row = copy.deepcopy(fact)
        row['sources'] = _sources(fact)
        # Catalog-only rows must already have the same evidence shape that
        # enrichment produces on a subsequent read-time projection.
        for field in ('local_components', 'local_skill_bindings', 'effect_details'):
            row[field] = row.get(field) or []
        row['partial_state'] = row.get('partial_state') is True
        entry = _trait_entry(fact)
        if entry is not None:
            row['trait_entry_id'] = entry
        # Merge connected overlapping records, never merely equal spell IDs.
        compatible = [g for g in groups if _same_catalog_family(g, row)]
        for other in reversed(compatible):
            # Earlier merges grow row: separate matches against the original
            # bridge do not prove that the accumulated groups are compatible.
            if not _same_catalog_family(other, row):
                continue
            _merge_evidence(other, row)
            other['global_components'] = _unique([*other['global_components'], *row['global_components']])
            groups.remove(other)
            row = other
        groups.append(row)

    result, covered = [], set()
    for original in actor.get('global_skill_effects') or []:
        if not isinstance(original, dict) or original.get('source_type') == 'specialization_passive':
            continue
        if not global_effect_matches_owner(original, class_name, spec, talent_scopes):
            continue
        source_ids = set(original.get('source_spell_ids') or [])
        candidates = []
        for index, group in enumerate(groups):
            owner_ids = set(group.get('source_spell_ids') or [])
            if not owner_ids or not owner_ids <= source_ids or not _component_match(original, group):
                continue
            if original.get('owner_spell_id') is not None and original['owner_spell_id'] not in owner_ids:
                continue
            if _is_static(original):
                entry = _trait_entry(original)
                if entry is None or not any(_trait_entry(s) == entry for s in group['sources']):
                    continue
            candidates.append((index, group))
        if not candidates:
            # Explicit reviewed ownership can disprove old runtime applicability
            # even when the caller supplied an unfiltered catalog.
            known = any(source_ids & set(f.get('source_spell_ids') or []) for f in facts)
            owner_allowed = any(source_ids & set(f.get('source_spell_ids') or []) for f in allowed)
            if _review_bound(original) or (known and not owner_allowed):
                continue
            result.append(copy.deepcopy(original))
            continue
        if len(candidates) != 1:
            # No physical selector: do not attach arbitrary values to several
            # disjoint records. Ownership was verified above; retain the
            # observation and catalog definitions without guessing their join.
            result.append(copy.deepcopy(original))
            continue
        index, catalog = candidates[0]
        covered.add(index)
        row = {**copy.deepcopy(catalog), **copy.deepcopy(original)}
        # Never inherit a different source's TraitEntry identity.
        row.pop('trait_entry_id', None)
        if _is_static(original) or original.get('source_type') == 'reviewed_scope':
            if _trait_entry(original) is not None:
                row['trait_entry_id'] = _trait_entry(original)
        row['sources'] = _sources(original)
        row['global_components'] = copy.deepcopy(catalog['global_components'])
        _merge_evidence(row, catalog)
        result.append(row)
    result.extend(copy.deepcopy(group) for index, group in enumerate(groups) if index not in covered)

    # Merge equal physical/value/activation rows. Never merge legacy observations
    # without exact DBC identity, or alter any lower action/variant facts.
    unique, positions = [], {}
    for row in result:
        parts = _parts(row)
        key = (tuple(sorted(parts)), _state(row)) if parts else None
        if key is not None and key in positions:
            _merge_evidence(unique[positions[key]], row)
        else:
            if key is not None:
                positions[key] = len(unique)
            unique.append(row)
    attached = set()
    for index, definition in enumerate(unique):
        for runtime in unique:
            if _definition_can_attach(definition, runtime):
                _merge_evidence(runtime, definition)
                attached.add(index)
    return [row for index, row in enumerate(unique) if index not in attached]
