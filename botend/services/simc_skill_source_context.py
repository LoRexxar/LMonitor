"""Readable provenance from localized, actor-local action facts.

A native parent is a *reporting* relationship, not proof of a guaranteed cast
component or proc trigger. This projection never changes actions, roots, damage,
variants or components. Call after product projection and localization.
"""
from collections import defaultdict


_MAX_SOURCE_DEPTH = 32
_UNRESOLVED_LABELS = {
    'missing_parent': '来源未解析',
    'ambiguous_parent': '来源存在歧义',
    'cyclic_parent': '来源链存在循环',
    'depth_limit': '来源链未完整解析',
}


def _text(value):
    return value if isinstance(value, str) else ''


def _fact(row):
    token = _text(row.get('token'))
    spell_id = row.get('spell_id')
    if not isinstance(spell_id, int) or isinstance(spell_id, bool) or spell_id <= 0:
        spell_id = None
    parent = _text(row.get('parent_token'))
    # Product roots may retain the same token as their native stat parent.
    parent = '' if parent == token else parent
    name = _text(row.get('display_name')) or _text(row.get('name')) or token
    return spell_id, parent, name


def _unresolved(token, reason):
    return {
        'token': token, 'spell_id': None, 'display_name': token,
        'relation': 'reporting_parent', 'status': 'unresolved', 'reason': reason,
        'display_label': f'{token}（{_UNRESOLVED_LABELS[reason]}）',
    }


def attach_skill_damage_source_context(actor):
    """Attach source_context in place and return actor (idempotent, no DB I/O).

    Resolve an exact parent token only when all matching actor action variants
    agree on its identity, readable name and ancestry. Missing/conflicting facts
    stay unresolved; neither token suffixes nor another actor supply identities.
    ``chain`` is ordered from the oldest known source to the immediate parent.
    Self-parent is a terminal; other cycles and long chains are bounded explicitly.
    """
    rows = [row for row in actor.get('actions', []) if isinstance(row, dict)]
    facts = defaultdict(set)
    for row in rows:
        token = _text(row.get('token'))
        if token:
            facts[token].add(_fact(row))

    by_label = defaultdict(list)
    for row in rows:
        row.pop('source_context', None)
        action_token = _text(row.get('token'))
        parent = _text(row.get('parent_token'))
        if not parent or parent == action_token:
            continue
        chain = []
        seen = {action_token}
        for _ in range(_MAX_SOURCE_DEPTH):
            if parent in seen:
                chain.append(_unresolved(parent, 'cyclic_parent'))
                break
            candidates = facts.get(parent, set())
            if len(candidates) != 1:
                chain.append(_unresolved(parent, 'ambiguous_parent' if candidates else 'missing_parent'))
                break
            spell_id, next_parent, name = next(iter(candidates))
            chain.append({
                'token': parent, 'spell_id': spell_id, 'display_name': name,
                'relation': 'reporting_parent', 'status': 'resolved',
                'display_label': name,
            })
            seen.add(parent)
            if not next_parent:
                break
            parent = next_parent
        else:
            chain.append(_unresolved(parent, 'depth_limit'))
        chain.reverse()
        resolved_count = sum(node['status'] == 'resolved' for node in chain)
        status = ('resolved' if resolved_count == len(chain)
                  else 'partial' if resolved_count else 'unresolved')
        label = '报告来源：' + ' → '.join(node['display_label'] for node in chain)
        context = {
            'action_token': action_token, 'relation': 'reporting_parent',
            'status': status, 'chain': chain, 'display_label': label,
        }
        row['source_context'] = context
        by_label[label].append(context)

    # Names can coincide even for different native source paths. Keep readable
    # names first, adding exact source identifiers only where labels collide.
    for contexts in by_label.values():
        paths = {tuple((node['token'], node['spell_id']) for node in context['chain'])
                 for context in contexts}
        if len(paths) > 1:
            for context in contexts:
                identity = ' → '.join(
                    f"{node['token']}" + (f" #{node['spell_id']}" if node['spell_id'] else '')
                    for node in context['chain']
                )
                context['display_label'] += f'（来源标识：{identity}）'
    return actor
