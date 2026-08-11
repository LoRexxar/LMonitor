"""Server-side lifecycle for progressive four-stat SimC optimization.

The browser creates round one only.  The Worker calls ``advance_attribute_search``
after a complete round; this function either appends the next immutable Run set to
the same Task or persists the final recommendation.
"""
import math

from django.db import transaction

from botend.models import SimcTask
from botend.services.simc_task_service import append_candidate_runs


ATTRIBUTE_STATS = ('crit', 'haste', 'mastery', 'versatility')
ATTRIBUTE_SEARCH_STEPS = (100, 50, 20)
ATTRIBUTE_SEARCH_STEP = ATTRIBUTE_SEARCH_STEPS[0]
ATTRIBUTE_MARGINAL_AMOUNTS = (20, 50, 100)
ATTRIBUTE_DPS_TOLERANCE = 1.0
MAX_ATTRIBUTE_SEARCH_ROUNDS = 20

ATTRIBUTE_STAT_LABELS = {
    'crit': '暴击',
    'haste': '急速',
    'mastery': '精通',
    'versatility': '全能',
}


def _normalized_step(step):
    try:
        value = int(step)
    except (TypeError, ValueError):
        raise ValueError('属性寻优步长无效')
    if value not in ATTRIBUTE_SEARCH_STEPS:
        raise ValueError(
            f'属性寻优步长必须是 {"、".join(str(item) for item in ATTRIBUTE_SEARCH_STEPS)} 之一'
        )
    return value


def _finer_step(step):
    step = _normalized_step(step)
    index = ATTRIBUTE_SEARCH_STEPS.index(step)
    return ATTRIBUTE_SEARCH_STEPS[index + 1] if index + 1 < len(ATTRIBUTE_SEARCH_STEPS) else None


def attribute_variants(values, round_number=1, mark_base=True, step=None):
    step = _normalized_step(ATTRIBUTE_SEARCH_STEP if step is None else step)
    base = {stat: int(values[stat]) for stat in ATTRIBUTE_STATS}
    if min(base.values()) < 0:
        raise ValueError('属性寻优绿字不能为负数')
    rows = [('基准属性', base, bool(mark_base), {
        'type': 'attribute', 'algorithm': 'four_stat_pairwise_hill_climb',
        'algorithm_version': 3, 'round': round_number, 'step': step,
        'total_rating': sum(base.values()), 'move': {'type': 'baseline'},
    })]
    for source in ATTRIBUTE_STATS:
        if base[source] < step:
            continue
        for target in ATTRIBUTE_STATS:
            if source == target:
                continue
            ratings = dict(base)
            ratings[source] -= step
            ratings[target] += step
            rows.append((f'{source} -{step} / {target} +{step}', ratings, False, {
                'type': 'attribute', 'algorithm': 'four_stat_pairwise_hill_climb',
                'algorithm_version': 3, 'round': round_number, 'step': step,
                'total_rating': sum(base.values()),
                'move': {'from': source, 'to': target, 'transfer': step},
            }))
    return rows


def _marginal_gain_candidates(ratings, round_number):
    base = {stat: int(ratings[stat]) for stat in ATTRIBUTE_STATS}
    candidates = []
    for stat in ATTRIBUTE_STATS:
        for amount in ATTRIBUTE_MARGINAL_AMOUNTS:
            measured = dict(base)
            measured[stat] += amount
            candidates.append({
                'candidate_key': f'marginal-{stat}-{amount}',
                'candidate_label': f'{ATTRIBUTE_STAT_LABELS[stat]} +{amount}',
                'round_number': round_number,
                'candidate_params': {
                    'candidate_type': 'attribute_ratings',
                    'is_base': False,
                    'attribute_ratings': measured,
                    'search': {
                        'type': 'attribute_marginal_gain',
                        'algorithm': 'single_stat_marginal_gain',
                        'algorithm_version': 1,
                        'round': round_number,
                        'marginal_gain': {'stat': stat, 'amount': amount},
                    },
                },
            })
    return candidates


def _marginal_gain_metadata(run):
    params = run.candidate_params if isinstance(run.candidate_params, dict) else {}
    search = params.get('search') if isinstance(params.get('search'), dict) else {}
    marginal = search.get('marginal_gain')
    return marginal if search.get('type') == 'attribute_marginal_gain' and isinstance(marginal, dict) else None


def _append_marginal_gain_runs(task, recommendation, round_number, expected_started_at):
    recommendation = {
        **recommendation,
        'marginal_gain_status': 'pending',
        'marginal_gain_round': round_number,
        'marginal_gain_amounts': list(ATTRIBUTE_MARGINAL_AMOUNTS),
    }
    task.analysis_result = {
        **(task.analysis_result if isinstance(task.analysis_result, dict) else {}),
        'attribute_search': recommendation,
    }
    task.save(update_fields=['analysis_result', 'modified_time'])
    created = append_candidate_runs(
        task,
        _marginal_gain_candidates(recommendation['ratings'], round_number),
        round_number=round_number,
        expected_started_at=expected_started_at,
    )
    return {
        'appended': len(created),
        'converged': True,
        'awaiting_marginal_gains': True,
        'run_ids': [run.id for run in created],
        'recommendation': recommendation,
    }


def _complete_marginal_gains(task, round_runs):
    analysis = task.analysis_result if isinstance(task.analysis_result, dict) else {}
    persisted = analysis.get('attribute_search')
    if not isinstance(persisted, dict) or persisted.get('converged') is not True:
        raise ValueError('属性边际收益缺少已收敛的最优解')
    try:
        baseline_ratings = {
            stat: int(persisted['ratings'][stat]) for stat in ATTRIBUTE_STATS
        }
        baseline_dps = float(persisted['dps'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('属性边际收益缺少可解析的最优属性或基准 DPS')

    rows = []
    signatures = set()
    for run in round_runs:
        if run.status != 'completed':
            raise ValueError('属性边际收益必须全部成功后才能完成任务')
        marginal = _marginal_gain_metadata(run)
        summary = run.result_summary if isinstance(run.result_summary, dict) else {}
        try:
            stat = str(marginal['stat'])
            amount = int(marginal['amount'])
            ratings = _run_ratings(run)
            dps = float(summary['dps'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('属性边际收益存在无法解析的执行结果')
        if stat not in ATTRIBUTE_STATS or amount not in ATTRIBUTE_MARGINAL_AMOUNTS:
            raise ValueError('属性边际收益的属性或增量无效')
        expected_ratings = dict(baseline_ratings)
        expected_ratings[stat] += amount
        if ratings != expected_ratings or (stat, amount) in signatures:
            raise ValueError('属性边际收益候选与最优解不一致或存在重复')
        signatures.add((stat, amount))
        try:
            dps_error = max(0.0, float(summary['dps_error']))
        except (KeyError, TypeError, ValueError):
            dps_error = None
        dps_gain = dps - baseline_dps
        rows.append({
            'run_id': run.id,
            'stat': stat,
            'amount': amount,
            'ratings': ratings,
            'baseline_dps': baseline_dps,
            'dps': dps,
            'dps_error': dps_error,
            'dps_gain': dps_gain,
            'gain_percent': dps_gain / baseline_dps * 100 if baseline_dps else None,
        })

    expected = {
        (stat, amount)
        for stat in ATTRIBUTE_STATS
        for amount in ATTRIBUTE_MARGINAL_AMOUNTS
    }
    if signatures != expected:
        raise ValueError('属性边际收益候选集合不完整')
    order = {stat: index for index, stat in enumerate(ATTRIBUTE_STATS)}
    rows.sort(key=lambda row: (order[row['stat']], ATTRIBUTE_MARGINAL_AMOUNTS.index(row['amount'])))
    recommendation = {
        **persisted,
        'marginal_gain_status': 'completed',
        'marginal_gain_baseline_dps': baseline_dps,
        'marginal_gains': rows,
    }
    task.analysis_result = {**analysis, 'attribute_search': recommendation}
    task.save(update_fields=['analysis_result', 'modified_time'])
    return {
        'appended': 0,
        'converged': True,
        'recommendation': recommendation,
        'marginal_gains': rows,
    }


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
        search = params.get('search') if isinstance(params.get('search'), dict) else {}
        try:
            normalized = _run_ratings(run)
            dps = float(summary['dps'])
            step = _normalized_step(search.get('step'))
        except (KeyError, TypeError, ValueError):
            raise ValueError('当前属性搜索轮次存在无法解析 DPS、绿字或步长的执行')
        try:
            dps_error = max(0.0, float(summary['dps_error']))
        except (KeyError, TypeError, ValueError):
            dps_error = None
        rows.append({
            'ratings': normalized, 'dps': dps, 'dps_error': dps_error,
            'step': step, 'is_center': bool(params.get('is_base')),
            'move': (search.get('move') or {}),
        })
    if len(rows) < 2:
        raise ValueError('当前属性搜索轮次没有足够完成结果')
    steps = {row['step'] for row in rows}
    if len(steps) != 1:
        raise ValueError('当前属性搜索轮次步长不一致')
    current_step = steps.pop()
    centers = [row for row in rows if row['is_center']]
    if len(centers) != 1:
        raise ValueError('当前属性搜索轮次必须包含且仅包含一个基准点')
    expected = attribute_variants(
        centers[0]['ratings'], round_number=round_runs[0].round_number,
        step=current_step,
    )
    actual_signatures = [
        _neighborhood_signature(row['ratings'], row['is_center'], row['move']) for row in rows
    ]
    expected_signatures = [
        _neighborhood_signature(ratings, is_center, candidate.get('move') or {})
        for _label, ratings, is_center, candidate in expected
    ]
    if len(actual_signatures) != len(set(actual_signatures)) or set(actual_signatures) != set(expected_signatures):
        raise ValueError('当前属性搜索轮次候选邻域不完整或存在重复')
    return rows, centers[0], current_step


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

    marginal_runs = [run for run in round_runs if _marginal_gain_metadata(run) is not None]
    if marginal_runs:
        if len(marginal_runs) != len(round_runs):
            raise ValueError('属性边际收益轮次混入了搜索候选')
        return _complete_marginal_gains(task, marginal_runs)

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
        probe_params = probe.candidate_params if isinstance(probe.candidate_params, dict) else {}
        probe_search = probe_params.get('search') if isinstance(probe_params.get('search'), dict) else {}
        search_step = _normalized_step(probe_search.get('step', ATTRIBUTE_SEARCH_STEP))
        candidate_round = current_round
        variants = attribute_variants(
            ratings, round_number=candidate_round, step=search_step,
        )
        variant_rows = variants[1:]
        while not variant_rows:
            search_step = _finer_step(search_step)
            if search_step is None:
                recommendation = {
                    'ratings': ratings, 'step': ATTRIBUTE_SEARCH_STEPS[-1],
                    'round': current_round, 'dps': baseline_dps, 'converged': True,
                    'stop_reason': 'insufficient_rating_for_20_transfer',
                }
                return _append_marginal_gain_runs(
                    task, recommendation, current_round + 1, expected_started_at,
                )
            candidate_round = current_round + 1
            refined_variants = attribute_variants(
                ratings, round_number=candidate_round, step=search_step,
            )
            variant_rows = refined_variants if len(refined_variants) > 1 else []
        candidates = [{
            'candidate_key': f'round-{candidate_round}-candidate-{index}',
            'candidate_label': label,
            'round_number': candidate_round,
            'candidate_params': {
                'candidate_type': 'attribute_ratings', 'is_base': is_center,
                'attribute_ratings': candidate_ratings, 'search': candidate,
            },
        } for index, (label, candidate_ratings, is_center, candidate) in enumerate(
            variant_rows, 1
        )]
        created = append_candidate_runs(
            task,
            candidates,
            round_number=candidate_round,
            expected_started_at=expected_started_at,
        )
        return {
            'appended': len(created), 'converged': False,
            'run_ids': [run.id for run in created],
            'baseline_ratings': ratings, 'step': search_step,
        }

    rows, center, current_step = _completed_round_rows(round_runs)
    best_neighbor = max((row for row in rows if not row['is_center']), key=lambda row: row['dps'])
    combined_error = (
        math.hypot(center['dps_error'], best_neighbor['dps_error'])
        if center['dps_error'] is not None and best_neighbor['dps_error'] is not None
        else ATTRIBUTE_DPS_TOLERANCE
    )
    improvement_threshold = max(ATTRIBUTE_DPS_TOLERANCE, combined_error)
    improved = best_neighbor['dps'] > center['dps'] + improvement_threshold
    winner = best_neighbor if improved else center
    next_step = current_step if improved else _finer_step(current_step)
    converged = next_step is None
    recommendation = {
        'ratings': winner['ratings'],
        'step': current_step if converged else next_step,
        'round': current_round if converged else current_round + 1,
        'dps': winner['dps'],
        'converged': converged,
        'stop_reason': f'local_optimum_{current_step}_pairwise' if converged else (
            '' if improved else 'refining_step'
        ),
    }
    if current_round >= MAX_ATTRIBUTE_SEARCH_ROUNDS and not recommendation['converged']:
        recommendation.update(
            converged=True, stop_reason='max_rounds_reached',
            round=current_round, step=current_step,
        )
    elif improved:
        visited = set()
        for run in runs:
            params = run.candidate_params if isinstance(run.candidate_params, dict) else {}
            if not params.get('is_base'):
                continue
            try:
                visited.add(_signature(_run_ratings(run)))
            except (KeyError, TypeError, ValueError):
                continue
        if _signature(winner['ratings']) in visited:
            recommendation.update(
                converged=True, stop_reason='cycle_detected',
                round=current_round, step=current_step,
            )

    if recommendation['converged']:
        return _append_marginal_gain_runs(
            task, recommendation, current_round + 1, expected_started_at,
        )

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
        attribute_variants(
            winner['ratings'], round_number=next_round,
            step=recommendation['step'],
        )
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
