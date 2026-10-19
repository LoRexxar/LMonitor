"""AI transport projection after full factual joins; never the stored evidence.

Keep every canonical evidence item unchanged. Present a bounded menu of valid
hypotheses with locally scoped citation templates, without changing stored facts.
The source ledger and complete eight-dungeon proofs remain in the frozen central
publication. Removing their duplicated intermediate matrices cannot remove a
shared prior run: those links have already produced canonical evidence IDs.
"""
from copy import deepcopy


def compact_ai_input(evidence):
    result = deepcopy({key: value for key, value in evidence.items() if key != 'metrics'})
    source = evidence.get('metrics', {})
    metrics = {key: deepcopy(source[key]) for key in (
        'highest_timed_level', 'observed_spec_count', 'team_pattern') if key in source}
    review = source.get('behavior_review')
    if isinstance(review, dict):
        structure = review.get('structure', {})
        summary = {key: deepcopy(structure[key]) for key in (
            'codes', 'recent_runs', 'roster_runs', 'untimed_runs', 'observed_config_switches') if key in structure}
        summary['lineup_frequencies'] = [
            {key: deepcopy(row[key]) for key in ('count', 'run_ids') if key in row}
            for row in structure.get('lineups', [])]
        metrics['behavior_review'] = {key: deepcopy(review[key]) for key in (
            'version', 'triggered', 'supported', 'witnesses', 'questions', 'limitations') if key in review}
        metrics['behavior_review']['structure'] = summary
        metrics['behavior_review']['proof_scope'] = '逐场完整坚韧证明存于冻结证据；此处只引用后端已计算的事实，不允许从未观测记录作否定推断。'
    if isinstance(source.get('investigation'), dict):
        metrics['investigation'] = deepcopy({key: value for key, value in source['investigation'].items()
                                            if key not in ('evidence', 'comparison_groups', 'core_members')})
        plan = metrics['investigation']
        if plan.get('triggered'):
            # Bound the transport menu, not the stored comparison ledger. Keep
            # key supply as a competing explanation and prioritize witnessed
            # directions; omitted hypotheses remain unassessed, not disproved.
            by_id = {row['id']: row for row in plan.get('hypotheses', [])}
            priority = ['key_access']
            external = by_id.get('external_assistance', {})
            if (external.get('support_ids') or external.get('counter_ids')
                    or any(':high:' in ref for ref in external.get('unknown_ids', []))):
                priority.append('external_assistance')
            priority += [row['id'] for row in by_id.values() if row.get('status') == 'supported']
            priority += ['assistance', 'routine_group', 'later_progress'] + list(by_id)
            chosen = list(dict.fromkeys(key for key in priority if key in by_id))[:4]
            plan['hypotheses'] = [by_id[key] for key in chosen]
            plan['response_templates'] = []
            for hypothesis in plan['hypotheses']:
                supports = hypothesis.get('support_ids', [])
                unknowns = hypothesis.get('unknown_ids', [])
                citations = list(dict.fromkeys(supports[:2] + unknowns[:1]))
                if not citations:
                    citations = unknowns[:3]
                assessments = ['possible', 'unknown']
                if supports and hypothesis.get('status') != 'unknown' and hypothesis['id'] != 'key_access':
                    assessments.append('supported')
                if hypothesis.get('counter_ids'):
                    assessments.append('weakened')
                plan['response_templates'].append({
                    'id': hypothesis['id'], 'assessment_options': assessments,
                    'evidence_ids': citations,
                    'counter_evidence_ids': deepcopy(hypothesis.get('counter_ids', [])),
                })
            supported = (review or {}).get('supported', {})
            result['eligible_labels'] = [row for row in result.get('eligible_labels', [])
                                         if row['code'] == 'recent_inquiry' or row['code'] in supported]
            codes = {row['code'] for row in result['eligible_labels']}
            result['conclusion_options'] = {key: value for key, value in result.get('conclusion_options', {}).items()
                                            if set(value.get('required_codes', [])) <= codes}
    result['metrics'] = metrics
    return result
