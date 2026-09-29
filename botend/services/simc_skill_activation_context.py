"""Bounded replacement-action contexts; DBC candidates need runtime proof.

No name matching, talent powerset, or blanket all-talents actor is used.  These
helpers are independent of Django and preserve the target talent separately
from the fixed action-unlocking context. Missing mapping is not readiness proof.
"""


def discover_replacement_candidates(catalog, talents):
    """Read the existing scope-export protocol, not a guessed Trigger relation.

    Aura 332 (Override Action Spell) stores replacement in value. A positive
    misc1 identifies the original; zero leaves it unspecified (e.g. mask-based
    records). Preserve zero rather than inventing an original SpellID. Readiness
    is proved against the replacement action, not the optional original ID.
    The owner of that aura need not unlock the action: selectors
    affecting its driver are candidates too, but must pass runtime readiness.
    """
    rows = catalog.get('talent_catalog')
    if not isinstance(rows, list):
        raise ValueError('Activation contexts require the full DBC talent_catalog.')
    entries = {t.node_id for t in talents}
    rows = [r for r in rows if r.get('trait_entry_id') in entries]
    relations = {}
    for row in rows:
        for effect in row.get('effects') or []:
            if effect.get('type') != 6 or effect.get('subtype') != 332:
                continue
            original, replacement = effect.get('misc1'), effect.get('value')
            if (type(original) is not int or original < 0
                    or type(replacement) not in (int, float)
                    or replacement <= 0 or int(replacement) != replacement):
                raise ValueError('Invalid DBC replacement spell relation.')
            key = (row['spell_id'], original, int(replacement))
            relations[key] = {'driver_spell_id': row['spell_id'],
                              'original_spell_id': original,
                              'replacement_spell_id': int(replacement),
                              'effect_id': effect.get('id'),
                              'effect_index': effect.get('index'),
                              'evidence': 'dbc_override_action_spell_332'}
    candidates = []
    for row in rows:
        affected = {s['spell_id'] for e in row.get('dbc_scope_effects') or []
                    for s in e.get('affected_spells') or []}
        for (driver, _, _), relation in sorted(relations.items()):
            if row['spell_id'] == driver or driver in affected:
                candidates.append({**relation, 'trait_entry_id': row['trait_entry_id']})
    return candidates


def _key(traits):
    return tuple(sorted((t.tree_type, t.node_id, max(1, int(getattr(t, 'max_points', 1) or 1))) for t in traits))


def _union(*groups):
    """Do not resolve a choice conflict by silently replacing the context."""
    result, entries, choices, subtrees = [], {}, {}, set()
    for group in groups:
        for trait in group:
            if trait.node_id in entries:
                if _key([entries[trait.node_id]]) != _key([trait]):
                    return None
                continue
            choice = getattr(trait, 'talent_id', None)
            if choice and choice in choices and choices[choice] != trait.node_id:
                return None
            subtree = getattr(trait, 'db2_subtree_id', None)
            if trait.tree_type == 'hero' and subtree:
                subtrees.add(subtree)
                if len(subtrees) > 1:
                    return None
            entries[trait.node_id] = trait
            if choice:
                choices[choice] = trait.node_id
            result.append(trait)
    return result


def validate_activation_pair(reference, selected, target_entry):
    """Reject hidden auto-selected talents before attributing a measured delta."""
    before = set(reference.get('selected_trait_ids') or [])
    after = set(selected.get('selected_trait_ids') or [])
    if after - before != {target_entry} or before - after:
        raise ValueError(
            f'Activation pair is not an exact marginal talent comparison: '
            f'target={target_entry}, added={sorted(after - before)}, removed={sorted(before - after)}.'
        )


def materialize_activation_pair(pair, load_actor):
    """Load one bounded high/low pair, enforce marginality, retain only C roots.

    load_actor(health, canonical_name) can be a spool-backed loader. The result
    uses the existing variant wire plus activation_context; the product layer
    must retain that context as prerequisites rather than as the target talent.
    """
    talent = pair['talent']
    result = {'talent': {
        'id': talent.pk, 'node_id': talent.node_id, 'tree_type': talent.tree_type,
        'hero_subtree_id': getattr(talent, 'db2_subtree_id', None),
        **{key: str(getattr(talent, key, '') or '') for key in (
            'name', 'name_zh', 'description', 'description_zh')},
    }, 'activation_context': pair['activation_context']}
    allowed = set(pair['activation_context']['action_spell_ids'])
    fixed = set(pair['activation_context']['trait_entry_ids'])
    high_selection = None
    for health, suffix in ((100, 'high'), (34, 'low')):
        before = load_actor(health, pair['reference_name'])
        after = load_actor(health, pair['selected_name'])
        if before is None or after is None:
            raise ValueError('Missing activation-context paired actor.')
        validate_activation_pair(before, after, talent.node_id)
        before_ids = set(before.get('selected_trait_ids') or [])
        after_ids = set(after.get('selected_trait_ids') or [])
        if not fixed <= before_ids or not fixed <= after_ids:
            raise ValueError('Activation context missing from runtime selection.')
        for key, actual in (('expected_reference_trait_entry_ids', before_ids),
                            ('expected_selected_trait_entry_ids', after_ids)):
            if key in pair and actual != set(pair[key]):
                raise ValueError(f'Activation runtime selection mismatch: {key}, '
                                 f'expected={pair[key]}, actual={sorted(actual)}.')
        selection = (before_ids, after_ids)
        if high_selection is not None and selection != high_selection:
            raise ValueError('Activation fixed selection differs between target-health probes.')
        high_selection = selection
        for actor, key, effectiveness in (
                (before, 'reference_' + suffix, 'inactive'), (after, suffix, 'active')):
            result[key] = {**actor, 'talent_effectiveness': effectiveness,
                           'actions': [a for a in actor.get('actions') or []
                                       if (a.get('reporting_root_spell_id') or a.get('spell_id')) in allowed]}
    return result


def prove_hero_anchor_selectors(ordinary_actors, load_actor, implicit_nodes,
                                implicit_trait_entry_ids=()):
    """Bind a unique configured subtree to a metadata anchor using ordinary probes.

    Never learn from context actors being validated. A witness must have an
    exact explicit + baseline + one anchor selection at BOTH target healths.
    Ambiguous mappings in either direction provide no permission.
    """
    anchors = {n.node_id for n in implicit_nodes if n.tree_type == 'hero_anchor'}
    baseline = set(implicit_trait_entry_ids)
    witnesses = {}
    for config in ordinary_actors:
        traits = config['selected_talents']
        trees = {t.db2_subtree_id for t in traits
                 if t.tree_type == 'hero' and getattr(t, 'db2_subtree_id', None)}
        if len(trees) != 1:
            continue
        high = load_actor(100, config['name'])
        low = load_actor(34, config['name'])
        if high is None or low is None:
            continue
        actual = set(high.get('selected_trait_ids') or [])
        if actual != set(low.get('selected_trait_ids') or []):
            continue
        expected = baseline | {t.node_id for t in traits}
        extra = actual - expected
        if not expected <= actual or len(extra) != 1 or not extra <= anchors:
            continue
        witnesses.setdefault(next(iter(trees)), set()).update(extra)
    reverse = {}
    for tree, selectors in witnesses.items():
        for selector in selectors:
            reverse.setdefault(selector, set()).add(tree)
    return {tree: next(iter(selectors)) for tree, selectors in witnesses.items()
            if len(selectors) == 1 and len(reverse[next(iter(selectors))]) == 1}


def plan_activation_context_pairs(talents, *, candidates, contexts,
                                  ordinary_reference_configs=None,
                                  existing_actors=(), implicit_trait_entry_ids=(),
                                  hero_anchor_selectors=None,
                                  max_contexts=16, max_pairs=2048):
    """Plan C+S(T) -> C+S(T)+T from already exported, successful actors.

    contexts maps candidate trait entry to {selected_talents, actor}. The actor
    is an actual export of that precise configuration, never a synthesized
    readiness record. Bounds fail closed: no successful-but-truncated plan.
    `pairs` retains the measured talent, explicit context and action allowlist;
    callers must restrict projection to those roots and validate runtime pairs.
    """
    # Configurations are already materialized by the ordinary actor planner.
    # Reapplying scaffold would restore choice entries it deliberately replaced.
    roots_by_entry, evidence_by_entry = {}, {}
    for candidate in candidates:
        entry = candidate['trait_entry_id']
        context = contexts.get(entry)
        if context is None:
            continue
        actor = context['actor']
        if entry not in set(actor.get('selected_trait_ids') or []):
            continue
        exported = {a.get('reporting_root_spell_id') or a.get('spell_id')
                    for a in actor.get('actions') or []}
        root = candidate['replacement_spell_id']
        if root not in exported:
            continue
        roots_by_entry.setdefault(entry, set()).add(root)
        evidence_by_entry.setdefault(entry, []).append(dict(candidate))
    proven = []
    for entry, roots in sorted(roots_by_entry.items()):
        traits = list(contexts[entry]['selected_talents'])
        proven.append((traits, roots, evidence_by_entry[entry]))
    # A larger closure bringing no additional roots is unnecessary. Keep
    # incomparable contexts, since they may be compatible with different T.
    proven.sort(key=lambda c: (len(c[0]), _key(c[0])))
    minimal = []
    for context in proven:
        keys = set(_key(context[0]))
        if any(set(_key(c[0])) <= keys and c[1] >= context[1] for c in minimal):
            continue
        minimal.append(context)
    if len(minimal) > max_contexts:
        raise ValueError('Activation context budget exceeded.')

    actors, pairs, skipped = [], [], []
    canonical = {_key(a['selected_talents']): a['name'] for a in existing_actors}

    def actor_name(traits):
        key = _key(traits)
        if key not in canonical:
            name = f'skill_damage_context_{len(actors)}'
            if name in canonical.values():
                raise ValueError('Activation actor namespace collision.')
            canonical[key] = name
            actors.append({'name': name, 'selected_talents': traits})
        return canonical[key]

    seen_pairs = set()
    for traits, roots, evidence in minimal:
        context_entries = {t.node_id for t in traits}
        for talent in talents:
            if talent.node_id in context_entries:
                continue
            ordinary = (ordinary_reference_configs[talent.pk]
                        if ordinary_reference_configs is not None else [])
            if context_entries <= {t.node_id for t in ordinary}:
                continue  # Already measured by S(T) -> S(T)+T.
            reference = _union(traits, ordinary)
            selected = _union(reference or [], [talent]) if reference is not None else None
            if selected is None:
                skipped.append({'talent_entry_id': talent.node_id,
                                'context_trait_entry_ids': sorted(context_entries),
                                'reason': 'incompatible_choice_or_subtree'})
                continue
            if talent.node_id in {t.node_id for t in reference}:
                continue
            identity = (_key(reference), _key(selected), tuple(sorted(roots)))
            if identity in seen_pairs:
                continue
            seen_pairs.add(identity)
            if len(pairs) >= max_pairs:
                raise ValueError('Activation pair budget exceeded.')
            def expected_selection(config):
                expected = set(implicit_trait_entry_ids) | {t.node_id for t in config}
                trees = {t.db2_subtree_id for t in config
                         if t.tree_type == 'hero' and getattr(t, 'db2_subtree_id', None)}
                if len(trees) == 1:
                    selector = (hero_anchor_selectors or {}).get(next(iter(trees)))
                    if selector is not None:
                        expected.add(selector)
                return sorted(expected)
            pairs.append({'talent': talent, 'reference_name': actor_name(reference),
                          'expected_reference_trait_entry_ids': expected_selection(reference),
                          'expected_selected_trait_entry_ids': expected_selection(selected),
                          'selected_name': actor_name(selected), 'activation_context': {
                              'trait_entry_ids': sorted(context_entries),
                              'traits': [{
                                  'trait_entry_id': t.node_id, 'talent_id': t.pk,
                                  'name': str(getattr(t, 'name', '') or ''),
                                  'name_zh': str(getattr(t, 'name_zh', '') or ''),
                                  'tree_type': t.tree_type,
                                  'hero_subtree_id': getattr(t, 'db2_subtree_id', None),
                              } for t in sorted(traits, key=lambda t: t.node_id)],
                              'action_spell_ids': sorted(roots), 'evidence': evidence}})
    return {'actors': actors, 'pairs': pairs, 'skipped': skipped,
            'context_count': len(minimal)}
