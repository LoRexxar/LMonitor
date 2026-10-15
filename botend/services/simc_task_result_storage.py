"""Reference-only Task result storage; completed Run owns the result facts.

Legacy Task-only results remain readable. Compaction is conservative and reversible
from the exact same Task's durable Runs; it never changes execution inputs or Runs.
"""
import json
from copy import deepcopy

from django.db import transaction

RESULT_MARKER = '_run_result_refs'
CANDIDATE_MARKER = '_result_ref'


def candidate_references(runs):
    return [dict(run_id=r.pk, candidate_key=r.candidate_key, candidate_label=r.candidate_label,
                 round_number=r.round_number, status=r.status, **{CANDIDATE_MARKER: 1}) for r in runs]


def result_reference(runs):
    runs = list(runs)
    completed = [r for r in runs if r.status == 'completed']
    return {RESULT_MARKER: 1, 'shape': 'single' if len(runs) == len(completed) == 1 else 'runs',
            'run_ids': [r.pk for r in completed]}


def _runs(task, runs=None):
    return {r.pk: r for r in (runs if runs is not None else task.simulation_runs.order_by('sequence'))
            if r.task_id == task.pk}


def _parsed(task):
    value = json.loads(task.result_summary) if task.result_summary else {}
    if not isinstance(value, dict):
        raise ValueError('Task result is not an object')
    return value


def resolve_task_result(task, *, runs=None):
    """Explicit compatibility read; callers needing only DPS should read one Run."""
    value = _parsed(task)
    if RESULT_MARKER not in value:
        return value
    ids = value.get('run_ids')
    if (value.get(RESULT_MARKER) != 1 or value.get('shape') not in ('single', 'runs')
            or not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids)
            or len(set(ids)) != len(ids) or (value['shape'] == 'single' and len(ids) != 1)):
        raise ValueError('Invalid Task result references')
    rows = _runs(task, runs)
    if any(i not in rows or rows[i].status != 'completed' for i in ids):
        raise ValueError('Missing or foreign completed Run result')
    results = [rows[i].result_summary or {} for i in ids]
    return results[0] if value['shape'] == 'single' else {'runs': results}


def resolve_task_analysis(task, *, runs=None):
    value = deepcopy(task.analysis_result)
    if not isinstance(value, dict):
        return value
    rows = None
    for candidate in value.get('candidates', []):
        if not isinstance(candidate, dict) or CANDIDATE_MARKER not in candidate:
            continue
        if candidate[CANDIDATE_MARKER] != 1 or 'result_summary' in candidate:
            raise ValueError('Invalid candidate result reference')
        rows = rows if rows is not None else _runs(task, runs)
        run = rows.get(candidate.get('run_id'))
        if run is None:
            raise ValueError('Missing or foreign candidate Run')
        candidate.pop(CANDIDATE_MARKER)
        candidate['result_summary'] = run.result_summary or {}
    return value


def _size(summary, analysis):
    return len((summary or '').encode('utf-8')) + len(json.dumps(analysis, ensure_ascii=False).encode('utf-8'))


@transaction.atomic
def compact_task_results(task_id, *, apply=False, restore=False):
    from botend.models import SimcTask
    task = SimcTask.objects.select_for_update().get(pk=task_id)
    result = {'task_id': task.pk, 'status': 'unchanged'}
    if task.current_status not in (2, 3):
        return {**result, 'status': 'active'}
    runs = list(task.simulation_runs.select_for_update().order_by('sequence'))
    if not runs or any(r.status not in ('completed', 'failed', 'cancelled') for r in runs):
        return {**result, 'status': 'unresolved', 'reason': 'no complete terminal Run closure'}
    summary, analysis = task.result_summary, deepcopy(task.analysis_result)
    before = _size(summary, analysis)
    try:
        if not isinstance(analysis, dict) or not isinstance(analysis.get('candidates', []), list):
            raise ValueError('Unsupported analysis structure')
        if restore:
            if RESULT_MARKER in _parsed(task):
                summary = json.dumps(resolve_task_result(task, runs=runs), ensure_ascii=False)
            analysis = resolve_task_analysis(task, runs=runs)
        else:
            if summary:
                parsed = _parsed(task)
                if RESULT_MARKER in parsed:
                    resolve_task_result(task, runs=runs)
                else:
                    expected = result_reference(runs)
                    reference_task = deepcopy(task)
                    reference_task.result_summary = json.dumps(expected)
                    if parsed != resolve_task_result(reference_task, runs=runs):
                        raise ValueError('Task result differs from its Run closure')
                    summary = reference_task.result_summary
            by_id = _runs(task, runs)
            for candidate in analysis.get('candidates', []):
                if not isinstance(candidate, dict):
                    raise ValueError('Invalid candidate')
                if CANDIDATE_MARKER in candidate:
                    if candidate[CANDIDATE_MARKER] != 1 or candidate.get('run_id') not in by_id or 'result_summary' in candidate:
                        raise ValueError('Invalid existing candidate reference')
                elif 'result_summary' in candidate:
                    row = by_id.get(candidate.get('run_id'))
                    if row is None or candidate['result_summary'] != (row.result_summary or {}):
                        raise ValueError('Candidate differs from its Run result')
                    candidate.pop('result_summary')
                    candidate[CANDIDATE_MARKER] = 1
    except (TypeError, ValueError) as exc:
        return {**result, 'status': 'unresolved', 'reason': str(exc)}
    after = _size(summary, analysis)
    result.update(before_bytes=before, after_bytes=after, saved_bytes=before-after)
    if summary == task.result_summary and analysis == task.analysis_result:
        return result
    result['status'] = ('restored' if restore else 'compacted') if apply else ('would_restore' if restore else 'would_compact')
    if apply:
        # Do not update modified_time: representation-only migration must not
        # invalidate published results or requeue completed simulations.
        SimcTask.objects.filter(pk=task.pk).update(result_summary=summary, analysis_result=analysis)
    return result
