"""Read-only display policy for validated frozen carrying facts.

Admission/ownership/privacy and frequency validation belong to the caller. This
helper never refreshes a source, infers missing events, or changes a publication.
Only explicitly identified carrying presentation fields are replaced; evidence
and the other labels retain their original facts and ordering.
"""
from copy import deepcopy

from botend.services.player_observation_carrying_policy import (
    MIN_DISTINCT_BENEFICIARIES, MIN_VERIFIED_RUNS, SAMPLE_LIMIT, WINDOW_DAYS,
)
from botend.services.player_observation_strength import LEVELS, RULES


EVENT_LABEL = '协助上分事件线索（未达模式门槛）'
RULE_NOTE = (f'当前模式门槛：同一四人组合在分析当时近{WINDOW_DAYS}天、最多{SAMPLE_LIMIT}场样本中，'
             f'至少{MIN_VERIFIED_RUNS}个独立场次已核实协助上分，且至少{MIN_DISTINCT_BENEFICIARIES}名不同第五人受益。'
             '未达门槛仅保留事件线索，不确认稳定4带1模式。')
LEGACY_NOTE = f'旧结果仅保存代表事件，无法据此确认达到{MIN_VERIFIED_RUNS}场；已保存数量不是全部协助事件总数。'
MISSING = '未保存，无法精确回溯'


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _display_frequency(frequency):
    raw = frequency if isinstance(frequency, dict) else {}
    counts = {key: _count(raw.get(key)) for key in (
        'verified_runs', 'observed_runs', 'unknown_runs', 'distinct_beneficiaries')}
    legacy = raw.get('legacy', True) is not False
    supported = (raw.get('supported') is True and not legacy and not raw.get('invalid')
                 and counts['verified_runs'] is not None
                 and counts['verified_runs'] >= MIN_VERIFIED_RUNS
                 and counts['distinct_beneficiaries'] is not None
                 and counts['distinct_beneficiaries'] >= MIN_DISTINCT_BENEFICIARIES
                 and counts['observed_runs'] is not None
                 and counts['verified_runs'] <= counts['observed_runs'] <= SAMPLE_LIMIT)
    return {**deepcopy(raw), **counts, 'legacy': legacy, 'supported': bool(supported),
            'window_days': WINDOW_DAYS, 'sample_limit': SAMPLE_LIMIT,
            'min_verified_runs': MIN_VERIFIED_RUNS,
            'min_distinct_beneficiaries': MIN_DISTINCT_BENEFICIARIES,
            'policy_note': RULE_NOTE, 'legacy_note': LEGACY_NOTE if legacy else '',
            'missing_label': MISSING}


def _grade(old, frequency, has_proof):
    old = old if isinstance(old, dict) else {}
    # A genuinely excluded/unverifiable sample is not resurrected as an event.
    level = (old.get('level') if not has_proof and old.get('level') in ('excluded', 'unverifiable') else
             'partial' if has_proof and (frequency['verified_runs'] or 0) >= 2 else
             'limited' if has_proof else 'insufficient')
    label, confidence = LEVELS[level]
    return {**deepcopy(old), 'level': level, 'label': label, 'confidence': confidence,
            'verified_runs': frequency['verified_runs'], 'distinct_beneficiaries': frequency['distinct_beneficiaries']}


def apply_carrying_frequency_policy(view, frequency):
    """Apply the current policy to a temporary public projection, not frozen JSON.

    A supported frequency preserves the exact frozen carrying reasons and proof.
    Otherwise only carrying judgements are downgraded; no new AI observation is
    invented even when the caller provides separately referenced event evidence.
    """
    if (not isinstance(view, dict) or view.get('status') not in ('ready', 'stale')
            or view.get('source_kind') == 'manual' or view.get('manual')
            or view.get('is_published') is False or view.get('review_status') == 'rejected'):
        return view
    frequency = _display_frequency(frequency)
    view['carrying_frequency'] = frequency
    # Replace only carrying rules; the rest of the frozen rules remain intact.
    rules = deepcopy(view.get('evidence_strength_rules') or {})
    rules['carrying'] = deepcopy(RULES['carrying'])
    view['evidence_strength_rules'] = rules
    if frequency['supported']:
        return view

    note = RULE_NOTE + (' ' + LEGACY_NOTE if frequency['legacy'] else '')
    has_proof = bool(frequency['verified_runs']) or bool(view.get('carrying_frequency_evidence'))
    carrying_rows = [item for key in ('display_judgements', 'observations', 'label_reviews')
                     for item in view.get(key) or [] if item.get('code') == 'carrying']
    has_proof = has_proof or any(item.get('evidence') for item in carrying_rows)
    investigation = view.get('investigation') or {}
    assistance = [h for h in investigation.get('hypotheses') or [] if h.get('id') == 'assistance']
    # Older assistance hypotheses also contain weak co-occurrence signals.
    # Only explicit carrying references are event proof, not arbitrary support.
    has_proof = has_proof or any(
        str(e.get('id', '')).startswith('behavior:carrying:')
        for h in assistance for e in h.get('evidence') or [])

    for key in ('display_judgements', 'observations', 'label_reviews'):
        if key not in view:
            continue
        rows = []
        for item in view[key] or []:
            if item.get('code') != 'carrying':
                rows.append(item)
                continue
            row = deepcopy(item)
            if key != 'label_reviews' and not has_proof:
                continue
            row['label'] = EVENT_LABEL if has_proof else '协助上分模式（频率不足）'
            row['reason'] = note
            row['note'] = '仅展示已保存事件证据；未达稳定模式频率门槛。' if has_proof else '频率不足，无法确认稳定模式。'
            if key == 'label_reviews':
                row['strength'] = _grade(row.get('strength'), frequency, has_proof)
                if row.get('status') == 'supported':
                    row['status'] = 'partial' if has_proof else 'unknown'
            else:
                row['evidence_strength'] = _grade(row.get('evidence_strength'), frequency, has_proof)
                row['confidence'] = row['evidence_strength']['confidence']
                if key == 'display_judgements':
                    # supported here means the event row has publishable proof,
                    # not that the stable carrying hypothesis is supported.
                    row['label'] = '行为线索'
                    row['value'] = EVENT_LABEL
                    row['supported'] = True
                    row['carrying_event_only'] = True
            rows.append(row)
        view[key] = rows

    if investigation and (assistance or carrying_rows):
        investigation = deepcopy(investigation)
        for hypothesis in investigation.get('hypotheses') or []:
            if hypothesis.get('id') != 'assistance':
                continue
            hypothesis['label'] = EVENT_LABEL if has_proof else '协助上分模式（频率不足）'
            hypothesis['evidence_strength'] = _grade(hypothesis.get('evidence_strength'), frequency, has_proof)
            hypothesis['assessment_label'] = '事件线索，未达模式门槛' if has_proof else '频率不足，无法确认模式'
            if hypothesis.get('assessment') == 'supported':
                hypothesis['assessment'] = 'possible' if has_proof else 'unknown'
            # No NLP rewrite of old model prose. Build the present explanation
            # explicitly from this rule and its already validated evidence.
            facts = '；'.join(str(e['text']) for e in hypothesis.get('evidence') or [] if e.get('text'))
            hypothesis['explanation'] = note + (f' 已保存支持事实：{facts}' if facts else '')
            if 'summary' in hypothesis:
                hypothesis['summary'] = note
        # An unstructured composite summary may repeat the old carrying claim.
        # Replace it deterministically while retaining all other hypothesis rows.
        others = [h.get('label', '') for h in investigation.get('hypotheses') or [] if h.get('id') != 'assistance']
        investigation['summary'] = note + (' 其他候选解释仍按下列分项展示：' + '、'.join(others) + '。' if others else '')
        view['investigation'] = investigation
    return view
