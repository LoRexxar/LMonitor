"""Server-side lifecycle for fixed-step four-stat SimC optimization.

The browser creates round one only.  The Worker calls ``advance_attribute_search``
after a complete round; this function either appends the next immutable Run set to
the same Task or persists the final recommendation.
"""
from django.db import transaction

from botend.models import SimcTask
from botend.services.simc_task_service import append_candidate_runs


ATTRIBUTE_STATS = ('crit', 'haste', 'mastery', 'versatility')
ATTRIBUTE_SEARCH_STEP = 50
ATTRIBUTE_DPS_TOLERANCE = 1.0
MAX_ATTRIBUTE_SEARCH_ROUNDS = 20


def attribute_variants(values, round_number=1, mark_base=True):
    base = {stat: int(values[stat]) for stat in ATTRIBUTE_STATS}
    if min(base.values()) < 0:
        raise ValueError('属性寻优绿字不能为负数')
    rows = [('基准属性', base, bool(mark_base), {
        'type': 'attribute', 'algorithm': 'four_stat_pairwise_hill_climb',
        'algorithm_version': 2, 'round': round_number, 'step': ATTRIBUTE_SEARCH_STEP,
        'total_rating': sum(base.values()), 'move': {'type': 'baseline'},
    })]
    for source in ATTRIBUTE_STATS:
        if base[source] < ATTRIBUTE_SEARCH_STEP:
            continue
        for target in ATTRIBUTE_STATS:
            if source == target:
                continue
            ratings = dict(base)
            ratings[source] -= ATTRIBUTE_SEARCH_STEP
            ratings[target] += ATTRIBUTE_SEARCH_STEP
            rows.append((f'{source} -{ATTRIBUTE_SEARCH_STEP} / {target} +{ATTRIBUTE_SEARCH_STEP}', ratings, False, {
                'type': 'attribute', 'algorithm': 'four_stat_pairwise_hill_climb',
                'algorithm_version': 2, 'round': round_number, 'step': ATTRIBUTE_SEARCH_STEP,
                'total_rating': sum(base.values()),
                'move': {'from': source, 'to': target, 'transfer': ATTRIBUTE_SEARCH_STEP},
            }))
    return rows


def _signature(ratings):
    return tuple(int(ratings[stat]) for stat in ATTRIBUTE_STATS)


def _neighborhood_signature(ratings, is_center, move):
    move = move if isinstance(move, dict) else {}
    return (
        _signature(ratings), bool(is_center), str(move.get('from') or ''),
        str(move.get('to') or ''), int(move.get('transfer') or 0),
        str(move.get('type') or ''),
    )


def _run_ratings(run):
    params = run.candidate_params if isinstance(run.candidate_params, dict) else {}
    summary = run.result_summary if isinstance(run.result_summary, dict) else {}
    ratings = params.get('attribute_ratings') or {}
    if params.get('candidate_type') == 'attribute_baseline_probe':
        ratings = summary.get('gear_ratings') or {}
    normalized = {stat: int(ratings[stat]) for stat in ATTRIBUTE_STATS}
    if min(normalized.values()) < 0:
        raise ValueError('属性寻优绿字不能为负数')
    return normalized


def _completed_round_rows(round_runs):
    rows = []
    for run in round_runs:
        if run.status != 'completed':
            raise ValueError('当前属性搜索轮次必须全部成功后才能续轮')
        params = run.candidate_params if isinstance(run.candidate_params, dict) else {}
        summary = run.result_summary if isinstance(run.result_summary, dict) else {}
        try:
            normalized = _run_ratings(run)
            dps = float(summary['dps'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('当前属性搜索轮次存在无法解析 DPS 或绿字的执行')
        try:
            dps_error = max(0.0, float(summary['dps_error']))
        except (KeyError, TypeError, ValueError):
            dps_error = None
        rows.append({
            'ratings': normalized, 'dps': dps, 'dps_error': dps_error,
            'is_center': bool(params.get('is_base')),
            'move': ((params.get('search') or {}).get('move') or {}),
        })
    if len(rows) < 2:
        raise ValueError('当前属性搜索轮次没有足够完成结果')
    centers = [row for row in rows if row['is_center']]
    if len(centers) != 1:
        raise ValueError('当前属性搜索轮次必须包含且仅包含一个基准点')
    expected = attribute_variants(centers[0]['ratings'], round_runs[0].round_number)
    actual_signatures = [
        _neighborhood_signature(row['ratings'], row['is_center'], row['move']) for row in rows
    ]
    expected_signatures = [
        _neighborhood_signature(ratings, is_center, candidate.get('move') or {})
        for _label, ratings, is_center, candidate in expected
    ]
    if len(actual_signatures) != len(set(actual_signatures)) or set(actual_signatures) != set(expected_signatures):
        raise ValueError('当前属性搜索轮次候选邻域不完整或存在重复')
    return rows, centers[0]


@transaction.atomic
def advance_attribute_search(task_id, expected_started_at=None):
    """Advance one fully measured round; idempotent while the next round is pending."""
    task = SimcTask.objects.select_for_update().select_related(
        'profile_version', 'template_version', 'apl_version',
    ).get(
        pk=task_id, is_active=True, mode='attribute_sweep',
    )
    if expected_started_at is None or (
        task.current_status != 1 or task.started_at != expected_started_at
    ):
        raise ValueError('属性寻优执行租约已失效')
    if not all((task.profile_id, task.template_id, task.apl_id,
                task.profile_version_id, task.template_version_id, task.apl_version_id)):
        raise ValueError('当前属性搜索任务引用不完整')
    version_pairs = (
        (task.profile_version, 'profile', task.profile_id),
        (task.template_version, 'template', task.template_id),
        (task.apl_version, 'apl', task.apl_id),
    )
    if any(version.resource_type != resource_type or version.resource_id != resource_id
           for version, resource_type, resource_id in version_pairs):
        raise ValueError('当前属性搜索任务资源版本不一致')
    runs = list(task.simulation_runs.select_for_update().order_by('sequence'))
    if not runs:
        raise ValueError('属性搜索任务缺少执行记录')
    current_round = max(run.round_number for run in runs)
    round_runs = [run for run in runs if run.round_number == current_round]
    if any(run.status in ('pending', 'running') for run in round_runs):
        return {'appended': 0, 'converged': False, 'awaiting': True}

    probe_runs = [
        run for run in round_runs
        if isinstance(run.candidate_params, dict)
        and run.candidate_params.get('candidate_type') == 'attribute_baseline_probe'
    ]
    if len(round_runs) == 1 and len(probe_runs) == 1:
        probe = probe_runs[0]
        if probe.status != 'completed':
            raise ValueError('装备属性基准探测执行失败，无法生成属性邻域')
        try:
            ratings = _run_ratings(probe)
            summary = probe.result_summary if isinstance(probe.result_summary, dict) else {}
            baseline_dps = float(summary['dps'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('SimC 基准报告未返回完整的 Gear Amount 四属性')
        variants = attribute_variants(ratings, current_round)
        candidates = [{
            'candidate_key': f'round-{current_round}-candidate-{index}',
            'candidate_label': label,
            'round_number': current_round,
            'candidate_params': {
                'candidate_type': 'attribute_ratings', 'is_base': is_center,
                'attribute_ratings': candidate_ratings, 'search': candidate,
            },
        } for index, (label, candidate_ratings, is_center, candidate) in enumerate(
            variants[1:], 1
        )]
        if not candidates:
            recommendation = {
                'ratings': ratings, 'step': ATTRIBUTE_SEARCH_STEP,
                'round': current_round, 'dps': baseline_dps, 'converged': True,
                'stop_reason': 'insufficient_rating_for_50_transfer',
            }
            task.analysis_result = {
                **(task.analysis_result if isinstance(task.analysis_result, dict) else {}),
                'attribute_search': recommendation,
            }
            task.save(update_fields=['analysis_result', 'modified_time'])
            return {'appended': 0, 'converged': True, 'recommendation': recommendation}
        created = append_candidate_runs(
            task,
            candidates,
            round_number=current_round,
            expected_started_at=expected_started_at,
        )
        return {
            'appended': len(created), 'converged': False,
            'run_ids': [run.id for run in created],
            'baseline_ratings': ratings,
        }

    rows, center = _completed_round_rows(round_runs)
    best_neighbor = max((row for row in rows if not row['is_center']), key=lambda row: row['dps'])
    combined_error = (
        center['dps_error'] + best_neighbor['dps_error']
        if center['dps_error'] is not None and best_neighbor['dps_error'] is not None
        else ATTRIBUTE_DPS_TOLERANCE
    )
    improvement_threshold = max(ATTRIBUTE_DPS_TOLERANCE, combined_error)
    improved = best_neighbor['dps'] > center['dps'] + improvement_threshold
    winner = best_neighbor if improved else center
    recommendation = {
        'ratings': winner['ratings'], 'step': ATTRIBUTE_SEARCH_STEP,
        'round': current_round if not improved else current_round + 1, 'dps': winner['dps'],
        'converged': not improved,
        'stop_reason': '' if improved else 'local_optimum_50_pairwise',
    }
    if improved:
        next_round = current_round + 1
        visited = set()
        for run in runs:
            params = run.candidate_params if isinstance(run.candidate_params, dict) else {}
            if not params.get('is_base'):
                continue
            try:
                visited.add(_signature(_run_ratings(run)))
            except (KeyError, TypeError, ValueError):
                continue
        if current_round >= MAX_ATTRIBUTE_SEARCH_ROUNDS:
            recommendation.update(
                converged=True, stop_reason='max_rounds_reached', round=current_round,
            )
        elif _signature(winner['ratings']) in visited:
            recommendation.update(
                converged=True, stop_reason='cycle_detected', round=current_round,
            )

    if recommendation['converged']:
        task.analysis_result = {
            **(task.analysis_result if isinstance(task.analysis_result, dict) else {}),
            'attribute_search': recommendation,
        }
        task.save(update_fields=['analysis_result', 'modified_time'])
        return {'appended': 0, 'converged': True, 'recommendation': recommendation}

    next_round = current_round + 1
    candidates = [{
        'candidate_key': f'round-{next_round}-candidate-{index}',
        'candidate_label': label,
        'round_number': next_round,
        'candidate_params': {
            'candidate_type': 'attribute_ratings', 'is_base': is_center,
            'attribute_ratings': ratings, 'search': candidate,
        },
    } for index, (label, ratings, is_center, candidate) in enumerate(
        attribute_variants(winner['ratings'], next_round)
    )]
    created = append_candidate_runs(
        task,
        candidates,
        round_number=next_round,
        expected_started_at=expected_started_at,
    )
    return {
        'appended': len(created), 'converged': False,
        'run_ids': [run.id for run in created], 'recommendation': recommendation,
    }
