"""The public collection of confirmed character labels, not a source allowlist.

Source producers own identity, privacy, publication and freshness admission.
This module only merges their admitted judgements and maps the same collection
for compact leaderboard consumers. It never loads or regenerates source facts.
"""
from copy import deepcopy

DEFAULT_SCOPE = '仅限近期公开样本，不代表长期身份或组队动机。'
MANUAL_SCOPE = '人工审核标签，不代表独立来源已证实。'
DISPLAY_LABELS = {
    'raid_consumer': {'label': '团本经历', 'value': '异常团本经历'},
    'fixed_team': {'value': '固定5人车队'},
    'rotating_core': {'value': '固定4人+轮换玩家'},
}


def _confirmed(item):
    if not isinstance(item, dict) or not item.get('supported'):
        return False
    strength = item.get('evidence_strength') or {}
    return (isinstance(strength, dict) and strength.get('level') == 'strong'
            and strength.get('confidence') == '高')


def _unique_judgements(items):
    result, seen = [], set()
    for item in items:
        code = item.get('code')
        if not isinstance(code, str) or not code or code in seen:
            continue
        seen.add(code)
        projected = deepcopy(item)
        value = projected.get('value')
        if code in DISPLAY_LABELS and isinstance(value, str) and value:
            # Rename only the public copy; frozen facts and qualifiers stay intact.
            _old_name, separator, qualifier = value.partition(' · ')
            projected.update(DISPLAY_LABELS[code])
            projected['value'] += separator + qualifier
        result.append(projected)
    return result


def confirmed_character_judgements(view, *, include_stale=True):
    """Return one copy-only, stable-code-deduped collection for every consumer.

    M+ retains its ready/(optional) stale + supported/strong/high gates. An
    independent admitted rule judgement is not suppressed by M+ pending/error.
    Manual publication is an override: ONLY its own reviewed tags survive, never
    leftover AI display fields or independent rules. Public projections already
    enforce review admission; explicit editorial withdrawals also fail closed.
    """
    if not isinstance(view, dict) or view.get('status') in ('hidden', 'archived', 'rejected'):
        return []
    if view.get('is_published') is False or view.get('review_status') == 'rejected':
        return []
    statuses = ('ready', 'stale') if include_stale else ('ready',)
    manual = view.get('manual')
    if view.get('source_kind') == 'manual':
        if (view.get('status') not in statuses or not isinstance(manual, dict)
                or view.get('review_status', 'approved') != 'approved'):
            return []
        tags = manual.get('tags', [])
        if not isinstance(tags, list):
            return []
        displayed = {item.get('code'): item for item in view.get('display_judgements', [])
                     if isinstance(item, dict)}
        own = []
        for index, tag in enumerate(tags):
            if not isinstance(tag, dict) or not isinstance(tag.get('value'), str) or not tag['value']:
                continue
            code = f'manual:{index}'
            item = displayed.get(code)
            # Preserve projected evidence URLs only when this is the same own tag.
            if not item or any(item.get(key) != tag.get(key) for key in ('label', 'value')):
                item = {**tag, 'code': code, 'supported': True}
            own.append(item)
        return _unique_judgements(own)
    if manual:
        return []  # A draft must never inherit an old AI or rule publication.
    admitted = []
    if view.get('status') in statuses:
        admitted.extend(item for item in view.get('display_judgements', []) if _confirmed(item))
    raid = view.get('raid_judgement')
    if _confirmed(raid):
        admitted.append(raid)
    return _unique_judgements(admitted)


def confirmed_character_badges(view, *, include_stale=True):
    """Compact mapping of precisely the same confirmed collection as detail."""
    result = []
    for item in confirmed_character_judgements(view, include_stale=include_stale):
        value = item.get('value')
        label = value.split(' · ')[0] if isinstance(value, str) else ''
        if not label:
            continue
        strength = item.get('evidence_strength') or {}
        result.append({'code': item['code'], 'label': label, 'confidence': '高',
            'scope': strength.get('scope') or (MANUAL_SCOPE if view.get('source_kind') == 'manual'
                                               else DEFAULT_SCOPE)})
    return result


def frozen_badge_judgements(badges):
    """Adapt already admitted last-good M+ badges without revalidating AI facts."""
    return [{'code': badge['code'], 'value': badge['label'], 'supported': True,
             'evidence_strength': {'level': 'strong', 'confidence': badge['confidence'],
                                   'scope': badge['scope']}} for badge in badges]
