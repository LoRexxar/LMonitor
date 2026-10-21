"""调查结果合同：保留 v8 冻结读取，v9 按已核实行为证据分别分级。

本模块不访问数据库、网络或发布结果。模型只能选择假设、证据引用和待决
问题，不能改写事实、主体、时序、范围或证据极性。未知不作反证，完整反证
与未知项始终可见。行为证据等级与收费、动机等未核实信息分别展示。
输入必须是可信的后端冻结证据，来源文本只作为数据并在展示时转义。
"""
from copy import deepcopy
import re

from botend.services.player_observation_ai import compact_ai_input


SCHEMA_VERSION = 8
CURRENT_SCHEMA_VERSION = 9
BEHAVIOR_HYPOTHESES = {'assistance': 'carrying', 'being_carried': 'being_carried',
    'joint_progress': 'joint_progress', 'key_access': 'key_access',
    'external_assistance': 'high_score_collaborator'}
ASSESSMENT_LABELS = {
    'supported': '有支持线索', 'possible': '相容解释',
    'weakened': '反证削弱', 'unknown': '待核查',
}
LIMITATIONS = (
    '仅比较所引公开样本；未选择的假设不等于被否定。',
    '当前观测不能升级为跑前记录，本人事实不能推广为全队事实；'
    '团队通关不是独立完成的证明。未知不作为反证。',
    '钥匙归属与交易未知，不能据此确认或否定交易。',
    '所有判断均为低置信候选，不证明私人动机。',
)


class InvalidContract(ValueError):
    """A malformed response or incomplete authoritative evidence, fail closed."""


def _require(condition, code):
    if not condition:
        raise InvalidContract(code)


def _ids(values, allowed=None, *, maximum=None, exact=False):
    _require(isinstance(values, list) and all(isinstance(v, str) for v in values), 'invalid_ids')
    _require(len(values) == len(set(values)), 'duplicate_ids')
    if maximum is not None:
        _require(len(values) <= maximum, 'too_many_ids')
    if allowed is not None:
        _require(set(values) <= set(allowed), 'invalid_polarity_or_id')
        if exact:
            _require(set(values) == set(allowed), 'missing_counter_ids')


def _index(rows):
    _require(isinstance(rows, list), 'invalid_authoritative_rows')
    result = {}
    for row in rows:
        _require(isinstance(row, dict) and isinstance(row.get('id'), str)
                 and row['id'] not in result, 'invalid_authoritative_id')
        result[row['id']] = row
    return result


def build_contract(evidence):
    """Derive the v8 selection menu from existing canonical plan/template rules.

    No separate fact catalog: every reference resolves against evidence.evidence.
    A fact's exact sentence supplies its type/subject/time/reference domain; no
    model-supplied overrides of these dimensions exist in the response schema.
    """
    _require(isinstance(evidence, dict), 'invalid_evidence')
    metrics = evidence.get('metrics')
    _require(isinstance(metrics, dict), 'invalid_plan')
    plan = metrics.get('investigation')
    _require(isinstance(plan, dict) and plan.get('triggered') is True, 'not_triggered')
    facts = _index(evidence.get('evidence'))
    for fact in facts.values():
        _require(isinstance(fact.get('text'), str) and bool(fact['text'].strip()), 'invalid_fact')
    hypotheses = _index(plan.get('hypotheses'))
    questions = _index(plan.get('questions'))
    # Validate every authority row before compacting: malformed rows must neither
    # leak native type errors nor disappear when the transport menu is bounded.
    for hypothesis in hypotheses.values():
        _require(isinstance(hypothesis.get('label'), str), 'invalid_label')
        _require(isinstance(hypothesis.get('status', ''), str), 'invalid_status')
        for field in ('support_ids', 'counter_ids', 'unknown_ids'):
            _ids(hypothesis.get(field), facts)
        support, counter, unknown = (set(hypothesis[k]) for k in ('support_ids', 'counter_ids', 'unknown_ids'))
        _require(not (support & counter or support & unknown or counter & unknown), 'ambiguous_polarity')
        _require(len(counter) <= 80, 'too_many_counters')
    for question in questions.values():
        _require(all(isinstance(question.get(field), str) for field in ('kind', 'reason')),
                 'invalid_question')
    for field in ('scope', 'stop_reason'):
        _require(isinstance(plan.get(field, ''), str), 'invalid_' + field)
    # Reuse the existing menu priority and assessment rules, not a forked table.
    # Calling it on a plan-only projection also supports already compact captures.
    projection = compact_ai_input({'metrics': {'investigation': deepcopy(plan)}})
    templates = projection['metrics']['investigation']['response_templates']
    menu = []
    required = []
    for template in templates:
        hypothesis = hypotheses[template['id']]
        support, counter, unknown = (set(hypothesis[k]) for k in ('support_ids', 'counter_ids', 'unknown_ids'))
        menu.append({
            'id': hypothesis['id'], 'label': hypothesis['label'],
            'assessment_options': deepcopy(template['assessment_options']),
            'support_ids': deepcopy(hypothesis['support_ids']),
            'counter_ids': deepcopy(hypothesis['counter_ids']),
            'unknown_ids': deepcopy(hypothesis['unknown_ids']),
        })
        if hypothesis['id'] == 'external_assistance' and (
                support or counter or any(':high:' in ref for ref in unknown)):
            required.append(hypothesis['id'])
    _require(2 <= len(menu) <= 4, 'invalid_menu_size')
    version = CURRENT_SCHEMA_VERSION if evidence.get('prompt_version') == 9 else SCHEMA_VERSION
    if version == CURRENT_SCHEMA_VERSION:
        grades = metrics.get('behavior_review', {}).get('evidence_strengths', {})
        for item in menu:
            item['evidence_strength'] = deepcopy(grades.get(BEHAVIOR_HYPOTHESES.get(item['id'])))
            if (item['evidence_strength'] and item['evidence_strength']['level'] == 'strong'
                    and item['support_ids'] and not item['counter_ids']):
                item['assessment_options'] = ['supported']
    return {'schema_version': version, 'confidence': '分项评估' if version == 9 else '低',
            'hypotheses': menu, 'required_hypothesis_ids': required,
            'next_question_ids': list(questions),
            'limits': {'hypotheses_min': 2, 'hypotheses_max': 4,
                       'support_ids_max': 3, 'unknown_ids_max': 3,
                       'counter_ids_max': 80, 'next_question_ids_max': 3},
            'response_fields': ['schema_version', 'confidence', 'hypotheses', 'next_question_ids'],
            'hypothesis_fields': ['id', 'assessment', 'support_ids', 'counter_ids', 'unknown_ids']}


def validate_investigation(value, evidence):
    """Return an independent validated copy, never mutate either input.

    Row order and bounded evidence selection express AI emphasis. Assessment is
    an inference annotation, not a backend fact or a publishable label.
    """
    contract = build_contract(evidence)
    _require(isinstance(value, dict) and set(value) == set(contract['response_fields']), 'invalid_schema')
    _require(type(value['schema_version']) is int and value['schema_version'] == contract['schema_version'],
             'invalid_schema_version')
    _require(value['confidence'] == contract['confidence'], 'invalid_confidence')
    rows = value['hypotheses']
    _require(isinstance(rows, list) and 2 <= len(rows) <= 4, 'invalid_hypothesis_count')
    menu = {h['id']: h for h in contract['hypotheses']}
    seen = set()
    for row in rows:
        _require(isinstance(row, dict) and set(row) == set(contract['hypothesis_fields']), 'invalid_hypothesis_schema')
        hid = row['id']
        _require(isinstance(hid, str) and hid in menu and hid not in seen, 'invalid_hypothesis')
        seen.add(hid)
        hypothesis = menu[hid]
        _require(isinstance(row['assessment'], str)
                 and row['assessment'] in hypothesis['assessment_options'], 'invalid_assessment')
        _ids(row['support_ids'], hypothesis['support_ids'], maximum=3)
        _ids(row['unknown_ids'], hypothesis['unknown_ids'], maximum=3)
        _ids(row['counter_ids'], hypothesis['counter_ids'], maximum=80, exact=True)
        _require(bool(row['support_ids'] or row['unknown_ids'] or row['counter_ids']), 'empty_basis')
        if row['assessment'] == 'supported':
            _require(bool(row['support_ids']), 'unsupported_assessment')
        if row['assessment'] == 'weakened':
            _require(bool(row['counter_ids']), 'unknown_is_not_counter')
    _require(set(contract['required_hypothesis_ids']) <= seen, 'missing_external_assistance')
    _ids(value['next_question_ids'], contract['next_question_ids'], maximum=3)
    return deepcopy(value)


def _text(value):
    # Plain authoritative text; escape exactly once at the final DOM boundary.
    # Never mark this safe: templates autoescape and JS consumers use textContent.
    return value


def _fact(row):
    url = row.get('run_url', '')
    # No reconstruction from IDs, no arbitrary source URLs, no redirects/userinfo.
    if not isinstance(url, str) or not re.fullmatch(
            r'https://raider\.io/mythic-plus-runs/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+', url):
        url = ''
    return {'id': row['id'], 'text': _text(row['text']), 'run_url': url}


def render_investigation(value, evidence):
    """Deterministic public-view-shaped Chinese projection plus raw audit value.

    Entire canonical sentences retain numeric details and all temporal/subject
    qualifications. No NLP paraphrasing and no inference from teamrun to solo.
    Unknown means assessment pending, never that an established fact vanished.
    """
    validated = validate_investigation(value, evidence)
    graded = validated['schema_version'] == 9
    grades = evidence['metrics'].get('behavior_review', {}).get('evidence_strengths', {})
    plan = evidence['metrics']['investigation']
    hypotheses = {h['id']: h for h in plan['hypotheses']}
    refs = {e['id']: e for e in evidence['evidence']}
    questions = {q['id']: q for q in plan['questions']}
    rows = []
    for item in validated['hypotheses']:
        h = hypotheses[item['id']]
        supports = [_fact(refs[eid]) for eid in item['support_ids']]
        # Backend order for complete counters/unknowns, AI ordering for emphasis.
        counters = [_fact(refs[eid]) for eid in h['counter_ids']]
        unknowns = [_fact(refs[eid]) for eid in h['unknown_ids']]
        emphasized = [_fact(refs[eid]) for eid in item['unknown_ids']]
        label = ASSESSMENT_LABELS[item['assessment']]
        strength = grades.get(BEHAVIOR_HYPOTHESES.get(item['id'])) if graded else None
        explanation = ([f"比较判断：{label}；行为证据：{strength['label']}。"] if strength else
                       [f'比较判断：{label}（待核实的解释）。'] if graded else
                       [f'比较判断：{label}（低置信；不是事实定论）。'])
        for title, facts in [('支持线索', supports), ('反证', counters), ('重点未决项', emphasized)]:
            if facts:
                explanation.append(title + '：' + '；'.join(f'「{f["text"]}」' for f in facts))
        if not counters:
            explanation.append('本假设未列出反证；这不表示已获证实。')
        explanation.append('未决项不作反证，完整未知项见下列证据；仅限各条事实的主体与时序。')
        rows.append({'id': item['id'], 'label': _text(h['label']),
                     **({'evidence_strength': deepcopy(strength)} if graded else {}),
                     'assessment': item['assessment'], 'assessment_label': label,
                     'explanation': ''.join(explanation), 'evidence': supports,
                     'counter_evidence': counters, 'unknown_evidence': unknowns,
                     'emphasized_unknown_ids': deepcopy(item['unknown_ids'])})
    summary = '已有线索／待核查：' + '；'.join(
        row['label'] + '—' + row['assessment_label'] for row in rows)
    summary += ('。行为证据按项分级；收费、动机和长期身份仍未核实，未知不作反证。' if graded else
                '。判断均为低置信，解释可以并存，未选假设未被否定。')
    from botend.services.player_observation_strength import CONTEXT_UNKNOWNS
    return {'schema_version': validated['schema_version'], 'triggered': True,
            'status': 'clues_pending', 'status_label': '已有线索／待核查',
            'source_kind': 'ai_inference', 'summary': summary, 'confidence': validated['confidence'],
            **({'context_unknowns': list(CONTEXT_UNKNOWNS)} if graded else {}),
            'hypotheses': rows, 'scope': _text(plan.get('scope', '')),
            'questions': [{k: _text(questions[qid][k]) for k in ('id', 'kind', 'reason')}
                          for qid in validated['next_question_ids']],
            'stop_reason': plan.get('stop_reason', ''),
            'limitations': (list(LIMITATIONS[:-1]) + ['行为模式的证据等级不代表收费或私人动机已获证实。']
                            if graded else list(LIMITATIONS)),
            'structured_investigation': validated}
