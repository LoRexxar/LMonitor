"""Transactional benchmark execution orchestration and safe result projection.

Task/Run are private execution inputs. Execution/Case/Result are the durable aggregate
and the only source used by dashboard and public read paths.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import timezone as datetime_timezone

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.utils import timezone

from botend.models import (
    SimcBenchmarkCase, SimcBenchmarkExecution, SimcBenchmarkPanel,
    SimcBenchmarkResult, SimcTask, SimulationRun,
)
from botend.services.simc_benchmark_config import (
    MAX_PANEL_CONFIG_BYTES, build_execution_plan,
)
from botend.services.simc_task_service import (
    TaskCreationError, TaskPreparedResourceChanged, TaskValidationUnavailable,
    create_task, prepare_task_creation,
)

TASK_PENDING = 0
TASK_RUNNING = 1
TASK_SUCCESS = 2
TASK_FAILED = 3
TASK_CANCELLED = 5
TASK_TERMINAL = frozenset((TASK_SUCCESS, TASK_FAILED, TASK_CANCELLED))
TASK_STATUS_NAMES = {
    TASK_PENDING: 'pending', TASK_RUNNING: 'running', TASK_SUCCESS: 'success',
    TASK_FAILED: 'failed', TASK_CANCELLED: 'cancelled',
}
TASK_STATUS_LABELS = {
    'pending': '待运行', 'running': '运行中', 'success': '成功',
    'failed': '失败', 'cancelled': '已取消',
}
RUN_STATUS_NAMES = {
    'pending': 'pending', 'running': 'running', 'completed': 'success',
    'failed': 'failed', 'cancelled': 'cancelled', 'canceled': 'cancelled',
}
_ERROR_LIMIT = 240
_ABSOLUTE_PATH = re.compile(
    r'(?:[A-Za-z]:[\\/]|/)(?:[^\s;:,]+[\\/])*[^\s;:,]*'
)


class BenchmarkExecutionConflict(RuntimeError):
    """Retryable preflight unavailability or resource drift during creation."""


def _validation_error(message, field=None):
    raise ValidationError({field: message} if field else message)


def _requester_id(requested_by):
    if requested_by is None:
        return None
    value = getattr(requested_by, 'pk', requested_by)
    if type(value) is not int or value <= 0:
        _validation_error('requested_by 必须是有效用户', 'requested_by')
    return value


def _normalize_trigger_slot(trigger, scheduled_slot):
    choices = {value for value, _label in SimcBenchmarkExecution.TRIGGER_CHOICES}
    if trigger not in choices:
        _validation_error('trigger 无效', 'trigger')
    if trigger == SimcBenchmarkExecution.TRIGGER_MANUAL:
        if scheduled_slot is not None:
            _validation_error('manual execution 不允许 scheduled_slot', 'scheduled_slot')
        return None
    if scheduled_slot is None:
        _validation_error('schedule execution 必须提供 scheduled_slot', 'scheduled_slot')
    if not timezone.is_aware(scheduled_slot):
        _validation_error('scheduled_slot 必须包含时区', 'scheduled_slot')
    # Scheduler slots have second precision.  This makes retries from stores with
    # different fractional-second precision identify the same logical slot.
    return scheduled_slot.astimezone(datetime_timezone.utc).replace(microsecond=0)


def _canonical_hash(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _safe_snapshot(panel, plan):
    """Freeze normalized definitions once; cases contain references only."""
    resources, profiles, cases = {}, {}, []
    candidate_definitions = [{
        'key': 'baseline', 'label': 'Baseline', 'candidate_type': 'base',
        'icon_url': '', 'source_label': '',
        'params': {'candidate_type': 'base', 'is_base': True},
    }] + deepcopy(plan['candidates'])
    for row in plan['cases']:
        resource_key = _canonical_hash({
            'backend_id': row['backend_id'], 'profile_id': row['profile_id'],
            'apl_id': row['apl_id'], 'template_id': row['template_id'],
        })
        resources.setdefault(resource_key, deepcopy(row['resources']))
        profiles.setdefault(row['profile_key'], {
            'key': row['profile_key'], 'label': row['profile_label'],
            'resource_key': resource_key,
        })
        cases.append({
            'spec_key': row['spec_key'], 'scenario_key': row['scenario_key'],
            'profile_key': row['profile_key'], 'resource_key': resource_key,
            'candidate_keys': [item['candidate_key'] for item in row['candidates']],
        })
    snapshot = {
        'version': 2, 'panel': deepcopy(plan['panel']),
        'specs': deepcopy(plan['specs']), 'scenarios': deepcopy(plan['scenarios']),
        'profiles': list(profiles.values()),
        'candidates': candidate_definitions, 'resources': resources,
        'cases': cases, 'case_count': plan['case_count'], 'run_count': plan['run_count'],
    }
    size = len(json.dumps(snapshot, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False).encode('utf-8'))
    if size > MAX_PANEL_CONFIG_BYTES:
        _validation_error(f'Execution snapshot exceeds {MAX_PANEL_CONFIG_BYTES} bytes')
    return snapshot


def _coordinate_hash(case):
    return _canonical_hash({
        'spec_key': case['spec_key'], 'scenario_key': case['scenario_key'],
        'profile_key': case['profile_key'],
    })


def _task_name(panel_id, execution_id, case):
    # Every component is a stable business key; ids disambiguate renamed panels.
    value = (
        f'benchmark panel-{panel_id} execution-{execution_id} '
        f'{case["spec_key"]} {case["scenario_key"]} {case["profile_key"]}'
    )
    return value[:200]


def create_execution(panel, trigger='manual', scheduled_slot=None, requested_by=None):
    """Preflight outside locks, then atomically persist from an identical locked plan."""
    slot = _normalize_trigger_slot(trigger, scheduled_slot)
    requester_id = _requester_id(requested_by)
    if trigger == SimcBenchmarkExecution.TRIGGER_MANUAL and requester_id is None:
        raise PermissionDenied('Manual benchmark execution requires the Panel owner')

    try:
        current_panel = SimcBenchmarkPanel.objects.get(pk=panel.pk)
    except SimcBenchmarkPanel.DoesNotExist:
        _validation_error('Panel 不存在', 'panel')
    if not current_panel.is_active:
        _validation_error('Panel 未启用，无法执行', 'panel')
    if requester_id is not None and requester_id != current_panel.created_by_id:
        raise PermissionDenied('Only the Panel owner may create an execution')
    if trigger == SimcBenchmarkExecution.TRIGGER_SCHEDULE and not current_panel.schedule_enabled:
        _validation_error('Panel 定时执行未启用', 'trigger')
    if slot is not None:
        winner = SimcBenchmarkExecution.objects.filter(
            panel=current_panel, scheduled_slot=slot,
        ).first()
        if winner is not None:
            return winner
    if trigger == SimcBenchmarkExecution.TRIGGER_MANUAL:
        active = SimcBenchmarkExecution.objects.filter(
            panel=current_panel, completed_at__isnull=True,
        ).first()
        if active is not None:
            return active

    # No row locks are held while SimC is executed. Deduplication intentionally
    # ignores scenario/candidate differences because APL validity is resource-bound.
    optimistic_plan = build_execution_plan(current_panel, lock=False)
    optimistic_identity = _canonical_hash(optimistic_plan)
    prepared_by_resources = {}
    try:
        for coordinate in optimistic_plan['cases']:
            key = (coordinate['backend_id'], coordinate['profile_id'],
                   coordinate['apl_id'], coordinate['template_id'])
            if key not in prepared_by_resources:
                prepared_by_resources[key] = prepare_task_creation(
                    current_panel.created_by_id, coordinate['profile_id'],
                    coordinate['template_id'], coordinate['apl_id'],
                    backend_id=coordinate['backend_id'],
                )
    except (TaskPreparedResourceChanged, TaskValidationUnavailable) as exc:
        raise BenchmarkExecutionConflict(
            'Benchmark execution preflight is temporarily unavailable'
        ) from exc
    except TaskCreationError as exc:
        _validation_error(str(exc))

    with transaction.atomic():
        try:
            locked_panel = SimcBenchmarkPanel.objects.select_for_update().get(pk=panel.pk)
        except SimcBenchmarkPanel.DoesNotExist:
            _validation_error('Panel 不存在', 'panel')
        if not locked_panel.is_active:
            _validation_error('Panel 未启用，无法执行', 'panel')
        if requester_id is not None and requester_id != locked_panel.created_by_id:
            raise PermissionDenied('Only the Panel owner may create an execution')
        if trigger == SimcBenchmarkExecution.TRIGGER_SCHEDULE and not locked_panel.schedule_enabled:
            _validation_error('Panel 定时执行未启用', 'trigger')
        if slot is not None:
            existing = SimcBenchmarkExecution.objects.filter(
                panel=locked_panel, scheduled_slot=slot,
            ).first()
            if existing is not None:
                return existing
        active = locked_panel.active_execution
        if active is not None and active.completed_at is not None:
            locked_panel.active_execution = None
            locked_panel.save(update_fields=['active_execution'])
            active = None
        if active is not None:
            if trigger == SimcBenchmarkExecution.TRIGGER_MANUAL:
                return active
            raise BenchmarkExecutionConflict(
                'Panel already has an unfinished benchmark execution'
            )

        locked_plan = build_execution_plan(locked_panel, lock=True)
        if _canonical_hash(locked_plan) != optimistic_identity:
            raise BenchmarkExecutionConflict(
                'Panel configuration changed during execution preflight'
            )
        snapshot = _safe_snapshot(locked_panel, locked_plan)
        config_hash = _canonical_hash(snapshot)

        # Only the Execution slot claim is caught. Any later Task/Case integrity
        # failure is unrelated and must abort the entire outer transaction.
        try:
            with transaction.atomic():
                execution = SimcBenchmarkExecution.objects.create(
                    panel=locked_panel, trigger=trigger, scheduled_slot=slot,
                    config_snapshot=snapshot, config_hash=config_hash,
                )
                locked_panel.active_execution = execution
                locked_panel.save(update_fields=['active_execution'])
        except IntegrityError:
            if slot is not None:
                winner = SimcBenchmarkExecution.objects.filter(
                    panel=locked_panel, scheduled_slot=slot,
                ).first()
                if winner is not None:
                    return winner
            winner = SimcBenchmarkExecution.objects.filter(
                panel=locked_panel, completed_at__isnull=True,
            ).first()
            if trigger == SimcBenchmarkExecution.TRIGGER_MANUAL and winner is not None:
                return winner
            raise

        try:
            for coordinate in locked_plan['cases']:
                key = (coordinate['backend_id'], coordinate['profile_id'],
                       coordinate['apl_id'], coordinate['template_id'])
                task = create_task(
                    user_id=locked_panel.created_by_id,
                    name=_task_name(locked_panel.pk, execution.pk, coordinate),
                    profile_id=coordinate['profile_id'], template_id=coordinate['template_id'],
                    apl_id=coordinate['apl_id'], backend_id=coordinate['backend_id'],
                    mode='comparison',
                    simulation_params=deepcopy(coordinate['simulation_params']),
                    mode_params={'request_manifest': {
                        'candidates': deepcopy(coordinate['candidates']),
                    }},
                    candidates=deepcopy(coordinate['candidates']),
                    prepared=prepared_by_resources[key],
                )
                case = SimcBenchmarkCase(
                    execution=execution, task=task,
                    spec_key=coordinate['spec_key'], scenario_key=coordinate['scenario_key'],
                    profile_key=coordinate['profile_key'], spec_label=coordinate['spec_label'],
                    scenario_label=coordinate['scenario_label'],
                    profile_label=coordinate['profile_label'],
                    coordinate_hash=_coordinate_hash(coordinate),
                )
                case.full_clean()
                case.save()
        except (TaskPreparedResourceChanged, TaskValidationUnavailable) as exc:
            raise BenchmarkExecutionConflict('Prepared task resources changed') from exc
        except TaskCreationError as exc:
            _validation_error(str(exc))
        return execution


def _safe_error(value):
    text = ' '.join(str(value or '').split())
    if not text:
        return None
    text = _ABSOLUTE_PATH.sub('[redacted]', text)
    # Never return accidental inline SimC input directives from an error payload.
    text = re.sub(r'(?i)\b(?:actions?|input|profile|player_equipment)\s*=\s*\S+',
                  '[redacted]', text)
    return text[:_ERROR_LIMIT]


def _execution_queryset():
    runs = SimulationRun.objects.order_by('sequence', 'id')
    cases = SimcBenchmarkCase.objects.select_related('task').prefetch_related(
        Prefetch('task__simulation_runs', queryset=runs, to_attr='_benchmark_runs'),
    ).order_by('id')
    return SimcBenchmarkExecution.objects.select_related('panel').prefetch_related(
        Prefetch('cases', queryset=cases, to_attr='_benchmark_cases'),
    )


def _load_execution(execution):
    try:
        return _execution_queryset().get(pk=execution.pk)
    except SimcBenchmarkExecution.DoesNotExist:
        _validation_error('Execution 不存在', 'execution')


def _expected_candidate_keys(task):
    """Return authoritative ordered candidate keys, or None if unusable.

    Historical/corrupt Tasks without the frozen request manifest deliberately fail
    closed. ``initial_candidates`` is executable state, not publication authority.
    """
    mode_params = task.mode_params if isinstance(task.mode_params, dict) else {}
    manifest = mode_params.get('request_manifest')
    candidates = manifest.get('candidates') if isinstance(manifest, dict) else None
    if not isinstance(candidates, list) or not candidates:
        return None
    keys = []
    for candidate in candidates:
        key = candidate.get('candidate_key') if isinstance(candidate, dict) else None
        if not isinstance(key, str) or not key or key in keys:
            return None
        keys.append(key)
    return keys


def _case_status(run_statuses, task_status, expected_keys, actual_keys):
    # Task lifecycle is authoritative: completed Runs cannot make any other Task
    # lifecycle state successful.
    if task_status in ('failed', 'cancelled'):
        return task_status
    if task_status == 'pending':
        return 'pending'
    if task_status == 'running':
        return 'running'

    # Success requires an exact ordered one-to-one Run collection. A proper prefix
    # is incomplete work; unknown, duplicate, reordered, and surplus keys are corrupt.
    if expected_keys is None:
        return 'failed'
    if len(actual_keys) != len(set(actual_keys)):
        return 'failed'
    if actual_keys != expected_keys:
        if actual_keys == expected_keys[:len(actual_keys)]:
            if any(item == 'running' for item in run_statuses):
                return 'running'
            if any(item == 'pending' for item in run_statuses):
                return 'running' if any(item != 'pending' for item in run_statuses) else 'pending'
            return 'pending'
        return 'failed'

    if not run_statuses:
        return 'pending'
    if all(item == 'pending' for item in run_statuses):
        return 'pending'
    if any(item in ('pending', 'running') for item in run_statuses):
        return 'running'
    if all(item == 'success' for item in run_statuses):
        return 'success'
    if all(item == 'cancelled' for item in run_statuses):
        return 'cancelled'
    if any(item == 'success' for item in run_statuses):
        return 'partial'
    return 'failed'


def _effective_run_status(task_status, raw_status):
    # Unknown persisted values are corruption, never evidence of live work.
    status = RUN_STATUS_NAMES.get(raw_status, 'failed')
    # Cancelled/failed Tasks are never claimed again by the worker. Their leftover
    # pending rows are therefore abandoned terminal work, not live work. Folding them
    # into the Task's terminal outcome avoids a permanently-running Execution while
    # remaining conservative (they can never contribute to success/publication).
    if status in ('pending', 'running') and task_status in ('failed', 'cancelled'):
        return task_status
    return status


def task_progress(task):
    """Return only trusted lifecycle progress persisted by the worker."""
    if task is None:
        return None
    status = getattr(task, 'current_status', None)
    if status == TASK_PENDING:
        return 0
    if status in TASK_TERMINAL:
        return 100
    if status == TASK_RUNNING:
        try:
            ext = json.loads(task.ext) if isinstance(task.ext, str) else (task.ext or {})
            progress = ext.get('progress') if isinstance(ext, dict) else None
            if isinstance(progress, (int, float)) and not isinstance(progress, bool) and 0 <= progress <= 100:
                return int(progress)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return None


def _summarize_live_execution(execution):
    """Derive state from Runs, using Task for abandoned Runs and zero-run terminal edges."""
    execution = _load_execution(execution)
    cases = execution._benchmark_cases
    count_names = ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
    counts = {name: 0 for name in count_names}
    run_counts = {name: 0 for name in ('pending', 'running', 'success', 'failed', 'cancelled')}
    rows = []
    total_runs = 0

    for case in cases:
        task = case.task
        if task is None:
            counts['failed'] += 1
            rows.append({
                'spec_key': case.spec_key, 'scenario_key': case.scenario_key,
                'profile_key': case.profile_key, '_case_id': case.pk,
                'labels': {'spec': case.spec_label, 'scenario': case.scenario_label,
                           'profile': case.profile_label},
                'status': 'failed', 'task_id': None, 'task_status': 'failed',
                'task_status_label': '失败', 'task_progress': None,
                'error': None, 'runs': [],
            })
            continue
        task_status = TASK_STATUS_NAMES.get(task.current_status, 'failed')
        expected_keys = _expected_candidate_keys(task)
        run_rows, effective_statuses = [], []
        errors = [task.error_detail]
        for run in task._benchmark_runs:
            total_runs += 1
            run_status = _effective_run_status(task_status, run.status)
            effective_statuses.append(run_status)
            run_counts[run_status] += 1
            errors.append(run.error_detail)
            summary = run.result_summary if isinstance(run.result_summary, dict) else {}
            run_rows.append({
                'key': run.candidate_key, 'label': run.candidate_label,
                # Live DPS is internal reconciliation input, never display output.
                'status': run_status, 'dps': None, '_raw_dps': summary.get('dps'),
            })
        actual_keys = [run.candidate_key for run in task._benchmark_runs]
        case_status = _case_status(
            effective_statuses, task_status, expected_keys, actual_keys,
        )
        counts[case_status] += 1
        safe_errors = [_safe_error(item) for item in errors]
        error = next((item for item in safe_errors if item), None)
        rows.append({
            'spec_key': case.spec_key, 'scenario_key': case.scenario_key,
            'profile_key': case.profile_key, '_case_id': case.pk,
            'labels': {
                'spec': case.spec_label, 'scenario': case.scenario_label,
                'profile': case.profile_label,
            },
            'status': case_status, 'task_id': task.pk, 'task_status': task_status,
            'task_status_label': TASK_STATUS_LABELS.get(task_status, '未知'),
            'task_progress': task_progress(task),
            'error': error, 'runs': run_rows,
        })

    total_cases = len(cases)
    if not total_cases or all(row['status'] == 'pending' for row in rows):
        status = 'pending'
    elif any(row['status'] in ('pending', 'running') for row in rows):
        status = 'running'
    elif all(row['status'] == 'success' for row in rows):
        status = 'success'
    elif all(row['status'] == 'cancelled' for row in rows):
        status = 'cancelled'
    elif not any(row['status'] in ('success', 'partial') for row in rows):
        status = 'failed'
    else:
        status = 'partial'

    return {
        'id': execution.pk, 'status': status,
        'created_at': execution.created_at, 'completed_at': execution.completed_at,
        'total_cases': total_cases, 'total_runs': total_runs,
        **counts, 'run_counts': run_counts, 'cases': rows,
    }


def _result_seal(rows, completed_at):
    completed_value = completed_at.isoformat() if completed_at is not None else None
    return _canonical_hash({'completed_at': completed_value, 'rows': [{
        'spec_key': row['spec_key'], 'scenario_key': row['scenario_key'],
        'profile_key': row['profile_key'], 'spec_label': row['spec_label'],
        'scenario_label': row['scenario_label'], 'profile_label': row['profile_label'],
        'status': row['status'], 'candidate_key': row['candidate_key'],
        'dps': float(row['dps']),
    } for row in rows]})


def _snapshot_layout(execution):
    snapshot = execution.config_snapshot
    try:
        hash_valid = (isinstance(snapshot, dict) and bool(execution.config_hash)
                      and execution.config_hash == _canonical_hash(snapshot))
    except (TypeError, ValueError):
        hash_valid = False
    if (not hash_valid or snapshot.get('version') != 2
            or not isinstance(snapshot.get('cases'), list)):
        return None
    layout = []
    for item in snapshot['cases']:
        if (not isinstance(item, dict)
                or not all(isinstance(item.get(key), str) and item[key]
                           for key in ('spec_key', 'scenario_key', 'profile_key'))
                or not isinstance(item.get('candidate_keys'), list)
                or not item['candidate_keys']
                or any(not isinstance(key, str) or not key for key in item['candidate_keys'])
                or len(item['candidate_keys']) != len(set(item['candidate_keys']))):
            return None
        layout.append(((item['spec_key'], item['scenario_key'], item['profile_key']),
                       item['candidate_keys']))
    if (len({coordinate for coordinate, _keys in layout}) != len(layout)
            or type(snapshot.get('case_count')) is not int
            or type(snapshot.get('run_count')) is not int
            or snapshot['case_count'] != len(layout)
            or snapshot['run_count'] != sum(len(keys) for _coordinate, keys in layout)):
        return None
    return layout


def _summarize_active_lifecycle(execution):
    """Project active progress from Case/Task lifecycle without exposing Run results."""
    cases = list(SimcBenchmarkCase.objects.filter(
        execution_id=execution.pk,
    ).select_related('task').order_by('id'))
    names = ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
    counts = {name: 0 for name in names}
    rows = []
    for case in cases:
        status = case.status if case.status in counts else 'failed'
        counts[status] += 1
        task = case.task
        task_status = TASK_STATUS_NAMES.get(task.current_status, 'failed') if task else None
        rows.append({
            'spec_key': case.spec_key, 'scenario_key': case.scenario_key,
            'profile_key': case.profile_key, '_case_id': case.pk,
            'labels': {'spec': case.spec_label, 'scenario': case.scenario_label,
                       'profile': case.profile_label},
            'status': status, 'task_id': case.task_id,
            'task_status': task_status,
            'task_status_label': TASK_STATUS_LABELS.get(task_status, '未知'),
            'task_progress': task_progress(task),
            'error': None, 'runs': [],
        })
    return {
        'id': execution.pk, 'status': execution.status,
        'created_at': execution.created_at, 'completed_at': execution.completed_at,
        'total_cases': len(cases), 'total_runs': 0,
        **counts,
        'run_counts': {name: 0 for name in ('pending', 'running', 'success', 'failed', 'cancelled')},
        'cases': rows,
    }


def _summarize_persisted_execution(execution):
    """Build terminal output solely from Execution/Case/Result aggregate tables."""
    result_qs = SimcBenchmarkResult.objects.order_by('case_id', 'id')
    cases = list(SimcBenchmarkCase.objects.filter(execution_id=execution.pk).prefetch_related(
        Prefetch('results', queryset=result_qs, to_attr='_persisted_results'),
    ).order_by('id'))
    definitions = execution.config_snapshot.get('candidates', []) \
        if isinstance(execution.config_snapshot, dict) else []
    labels = {item.get('key'): item.get('label') for item in definitions
              if isinstance(item, dict)}
    rows, total_runs = [], 0
    for case in cases:
        run_rows = [{
            'key': result.candidate_key,
            'label': labels.get(result.candidate_key, result.candidate_key),
            'status': 'success', 'dps': result.dps,
        } for result in case._persisted_results]
        total_runs += len(run_rows)
        rows.append({
            'spec_key': case.spec_key, 'scenario_key': case.scenario_key,
            'profile_key': case.profile_key, '_case_id': case.pk,
            'labels': {'spec': case.spec_label, 'scenario': case.scenario_label,
                       'profile': case.profile_label},
            'status': case.status,
            # A terminal summary is immutable: expose the frozen Case lifecycle,
            # never the mutable Task currently referenced by the Case.
            'task_id': case.task_id,
            'task_status': case.status,
            'task_status_label': TASK_STATUS_LABELS.get(case.status, '未知'),
            'task_progress': 100,
            'error': None, 'runs': run_rows,
        })
    names = ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
    counts = {name: 0 for name in names}
    for case in cases:
        counts[case.status if case.status in counts else 'failed'] += 1
    run_counts = {name: 0 for name in ('pending', 'running', 'success', 'failed', 'cancelled')}
    if execution.status == 'success':
        run_counts['success'] = total_runs
    return {
        'id': execution.pk, 'status': execution.status,
        'created_at': execution.created_at, 'completed_at': execution.completed_at,
        'total_cases': len(cases), 'total_runs': total_runs,
        **counts, 'run_counts': run_counts, 'cases': rows,
    }


def summarize_execution(execution):
    """Use live Task/Run state only before finalization; terminal reads are aggregate-only."""
    current = SimcBenchmarkExecution.objects.get(pk=execution.pk)
    if current.status in (
        SimcBenchmarkExecution.STATUS_PENDING,
        SimcBenchmarkExecution.STATUS_RUNNING,
    ):
        return _summarize_active_lifecycle(current)
    summary = _summarize_persisted_execution(current)
    layout = _snapshot_layout(current)
    if layout is None:
        summary['status'] = SimcBenchmarkExecution.STATUS_FAILED
        summary['run_counts']['success'] = 0
        summary['total_runs'] = 0
        for case in summary['cases']:
            case['runs'] = []
        return summary
    if current.status == SimcBenchmarkExecution.STATUS_SUCCESS:
        seal_rows = []
        expected = {coordinate: keys for coordinate, keys in layout}
        seen = set()
        valid = (current.completed_at is not None
                 and current.results_finalized_at is not None
                 and isinstance(current.result_hash, str)
                 and len(current.result_hash) == 64)
        for case in summary['cases']:
            coordinate = (case['spec_key'], case['scenario_key'], case['profile_key'])
            keys = [run['key'] for run in case['runs']]
            valid = valid and coordinate not in seen and expected.get(coordinate) == keys
            seen.add(coordinate)
            for run in case['runs']:
                seal_rows.append({
                    'spec_key': coordinate[0], 'scenario_key': coordinate[1],
                    'profile_key': coordinate[2],
                    'spec_label': case['labels']['spec'],
                    'scenario_label': case['labels']['scenario'],
                    'profile_label': case['labels']['profile'],
                    'status': case['status'], 'candidate_key': run['key'],
                    'dps': run['dps'],
                })
        valid = valid and seen == set(expected)
        try:
            valid = valid and _result_seal(seal_rows, current.completed_at) == current.result_hash
        except (TypeError, ValueError):
            valid = False
        if not valid:
            summary['status'] = SimcBenchmarkExecution.STATUS_FAILED
            summary['success'] = 0
            summary['failed'] = len(summary['cases'])
            summary['run_counts']['success'] = 0
            summary['total_runs'] = 0
            for case in summary['cases']:
                case['status'] = SimcBenchmarkExecution.STATUS_FAILED
                case['runs'] = []
    return summary


def _collect_success_results(execution, live):
    """Validate snapshot → Case → Task manifest → Run, then extract DPS once."""
    layout = _snapshot_layout(execution)
    if layout is None or live['status'] != 'success' or len(layout) != len(live['cases']):
        return None
    live_by_coordinate = {}
    for case in live['cases']:
        coordinate = (case['spec_key'], case['scenario_key'], case['profile_key'])
        if coordinate in live_by_coordinate:
            return None
        live_by_coordinate[coordinate] = case
    if set(live_by_coordinate) != {coordinate for coordinate, _keys in layout}:
        return None

    rows = []
    for coordinate, candidate_keys in layout:
        case = live_by_coordinate[coordinate]
        if ([run['key'] for run in case['runs']] != candidate_keys
                or type(case.get('_case_id')) is not int):
            return None
        for run in case['runs']:
            dps = run.get('_raw_dps')
            if (isinstance(dps, bool) or not isinstance(dps, (int, float))
                    or not math.isfinite(dps) or dps <= 0):
                return None
            rows.append({
                'case_id': case['_case_id'],
                'spec_key': coordinate[0], 'scenario_key': coordinate[1],
                'profile_key': coordinate[2],
                'spec_label': case['labels']['spec'],
                'scenario_label': case['labels']['scenario'],
                'profile_label': case['labels']['profile'],
                'status': case['status'], 'candidate_key': run['key'],
                'dps': float(dps),
            })
    return rows


def reconcile_execution(execution):
    """Finalize exactly once under Execution/Panel locks and publish monotonically."""
    with transaction.atomic():
        try:
            locked = SimcBenchmarkExecution.objects.select_for_update().get(pk=execution.pk)
        except SimcBenchmarkExecution.DoesNotExist:
            _validation_error('Execution 不存在', 'execution')
        panel = SimcBenchmarkPanel.objects.select_for_update().get(pk=locked.panel_id)
        if panel.published_execution_id:
            published = SimcBenchmarkExecution.objects.filter(
                pk=panel.published_execution_id,
            ).first()
            if published is None or published.panel_id != panel.pk:
                _validation_error('published_execution 不属于当前 Panel', 'published_execution')

        # Completion is the durable idempotency boundary: never inspect Tasks/Runs again.
        if locked.completed_at is not None:
            if panel.active_execution_id == locked.pk:
                panel.active_execution = None
                panel.save(update_fields=['active_execution'])
            return locked
        live = _summarize_live_execution(locked)
        live_status = live['status']
        case_statuses = {
            row['_case_id']: row['status'] for row in live['cases']
            if type(row.get('_case_id')) is int
        }
        for status in ('pending', 'running', 'success', 'partial', 'failed', 'cancelled'):
            ids = [case_id for case_id, value in case_statuses.items() if value == status]
            if ids:
                SimcBenchmarkCase.objects.filter(
                    execution_id=locked.pk, pk__in=ids,
                ).update(status=status)
        if live_status in ('pending', 'running'):
            if locked.status != live_status:
                locked.status = live_status
                locked.save(update_fields=['status'])
            return locked

        now = timezone.now()
        if live_status != 'success':
            SimcBenchmarkResult.objects.filter(case__execution_id=locked.pk).delete()
            locked.status = live_status
            locked.completed_at = now
            locked.result_hash = ''
            locked.results_finalized_at = None
            locked.save(update_fields=[
                'status', 'completed_at', 'result_hash', 'results_finalized_at',
            ])
            if panel.active_execution_id == locked.pk:
                panel.active_execution = None
                panel.save(update_fields=['active_execution'])
            return locked

        rows = _collect_success_results(locked, live)
        if rows is None:
            # Bad/missing/non-finite/non-positive DPS is terminal aggregate failure,
            # rather than an exception that leaves the scheduler retrying forever.
            SimcBenchmarkResult.objects.filter(case__execution_id=locked.pk).delete()
            SimcBenchmarkCase.objects.filter(execution_id=locked.pk).update(
                status=SimcBenchmarkExecution.STATUS_FAILED,
            )
            locked.status = SimcBenchmarkExecution.STATUS_FAILED
            locked.completed_at = now
            locked.result_hash = ''
            locked.results_finalized_at = None
            locked.save(update_fields=[
                'status', 'completed_at', 'result_hash', 'results_finalized_at',
            ])
            if panel.active_execution_id == locked.pk:
                panel.active_execution = None
                panel.save(update_fields=['active_execution'])
            return locked

        SimcBenchmarkResult.objects.bulk_create([
            SimcBenchmarkResult(case_id=row['case_id'], candidate_key=row['candidate_key'],
                                dps=row['dps'])
            for row in rows
        ])
        locked.status = SimcBenchmarkExecution.STATUS_SUCCESS
        locked.result_hash = _result_seal(rows, now)
        locked.results_finalized_at = now
        locked.completed_at = now
        locked.save(update_fields=[
            'status', 'result_hash', 'results_finalized_at', 'completed_at',
        ])
        published = panel.published_execution
        newer_already_published = published is not None and (
            published.created_at, published.pk
        ) > (locked.created_at, locked.pk)
        panel_fields = []
        if panel.active_execution_id == locked.pk:
            panel.active_execution = None
            panel_fields.append('active_execution')
        if not newer_already_published and panel.published_execution_id != locked.pk:
            panel.published_execution = locked
            panel_fields.append('published_execution')
        if panel_fields:
            panel.save(update_fields=panel_fields)
        return locked


def serialize_public_execution(panel_or_execution):
    """Serialize only a Panel's exact public, published Execution."""
    if isinstance(panel_or_execution, SimcBenchmarkExecution):
        requested_execution_id = panel_or_execution.pk
        panel_id = panel_or_execution.panel_id
    elif isinstance(panel_or_execution, SimcBenchmarkPanel):
        requested_execution_id = None
        panel_id = panel_or_execution.pk
    else:
        _validation_error('必须提供 Panel 或 Execution')

    panel = SimcBenchmarkPanel.objects.filter(pk=panel_id).first()
    if (panel is None or not panel.is_active or
            panel.published_execution_id is None or
            (requested_execution_id is not None and
             requested_execution_id != panel.published_execution_id)):
        return {'status': 'not_ready', 'execution': None}
    execution = SimcBenchmarkExecution.objects.filter(
        pk=panel.published_execution_id, panel_id=panel.pk,
    ).first()
    if execution is None:
        return {'status': 'not_ready', 'execution': None}
    raw_snapshot = execution.config_snapshot
    snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
    # ``config_hash`` is the publication seal, not optional historical metadata.
    # Validate it before deriving or inspecting any mutable Task/Run state.
    try:
        snapshot_hash_valid = (
            isinstance(raw_snapshot, dict)
            and isinstance(execution.config_hash, str)
            and bool(execution.config_hash)
            and execution.config_hash == _canonical_hash(snapshot)
        )
    except (TypeError, ValueError):
        snapshot_hash_valid = False
    if not snapshot_hash_valid:
        return {'status': 'not_ready', 'execution': None}

    if (execution.status != SimcBenchmarkExecution.STATUS_SUCCESS
            or execution.completed_at is None
            or execution.results_finalized_at is None
            or not isinstance(execution.result_hash, str)
            or len(execution.result_hash) != 64):
        return {'status': 'not_ready', 'execution': None}
    summary = _summarize_persisted_execution(execution)
    if summary['status'] != 'success':
        return {'status': 'not_ready', 'execution': None}
    panel_snapshot = snapshot.get('panel') if isinstance(snapshot.get('panel'), dict) else {}
    snapshot_cases = snapshot.get('cases') if isinstance(snapshot.get('cases'), list) else []
    definitions = snapshot.get('candidates') if isinstance(snapshot.get('candidates'), list) else []
    specs = snapshot.get('specs') if isinstance(snapshot.get('specs'), list) else []
    scenarios = snapshot.get('scenarios') if isinstance(snapshot.get('scenarios'), list) else []
    profiles = snapshot.get('profiles') if isinstance(snapshot.get('profiles'), list) else []
    # Public history is fail-closed. Never repair corrupt historical data from the
    # mutable Panel/Task rows, because doing so can publish a different benchmark.
    counts_valid = all(
        type(snapshot.get(key)) is int and snapshot[key] >= 0
        for key in ('case_count', 'run_count')
    )
    if (snapshot.get('version') != 2
            or not all(isinstance(panel_snapshot.get(key), str)
                       for key in ('name', 'slug', 'description'))
            or not panel_snapshot.get('name') or not panel_snapshot.get('slug')
            or not definitions or not snapshot_cases or not specs or not scenarios or not profiles
            or not counts_valid
            or snapshot['case_count'] != summary['total_cases']
            or snapshot['run_count'] != summary['total_runs']):
        return {'status': 'not_ready', 'execution': None}

    candidate_metadata = {}
    for item in definitions:
        required = {'key', 'label', 'candidate_type', 'icon_url', 'source_label', 'params'}
        if (not isinstance(item, dict)
                or not required.issubset(item)
                or set(item) - required - {'id'}
                or ('id' in item and
                    (type(item['id']) is not int or item['id'] <= 0))
                or not all(isinstance(item.get(key), str)
                           for key in required - {'params'})
                or not isinstance(item.get('params'), dict)
                or not item['key'] or not item['label'] or not item['candidate_type']
                or item['key'] in candidate_metadata):
            return {'status': 'not_ready', 'execution': None}
        candidate_metadata[item['key']] = item

    spec_labels = {}
    for item in specs:
        if (not isinstance(item, dict)
                or set(item) != {'id', 'class_name', 'spec_key', 'display_label'}
                or type(item.get('id')) is not int or item['id'] <= 0
                or not all(isinstance(item.get(key), str) and bool(item[key])
                           for key in ('class_name', 'spec_key', 'display_label'))
                or item['spec_key'] in spec_labels):
            return {'status': 'not_ready', 'execution': None}
        spec_labels[item['spec_key']] = item['display_label']

    scenario_labels = {}
    for item in scenarios:
        if (not isinstance(item, dict)
                or set(item) != {'id', 'key', 'label', 'simulation_params'}
                or type(item.get('id')) is not int or item['id'] <= 0
                or not isinstance(item.get('key'), str) or not item['key']
                or not isinstance(item.get('label'), str) or not item['label']
                or not isinstance(item.get('simulation_params'), dict)
                or item['key'] in scenario_labels):
            return {'status': 'not_ready', 'execution': None}
        scenario_labels[item['key']] = item['label']

    profile_labels = {}
    profile_resources = {}
    for item in profiles:
        if (not isinstance(item, dict)
                or set(item) != {'key', 'label', 'resource_key'}
                or not all(isinstance(item.get(key), str) and bool(item[key])
                           for key in ('key', 'label', 'resource_key'))
                or item['key'] in profile_labels):
            return {'status': 'not_ready', 'execution': None}
        profile_labels[item['key']] = item['label']
        profile_resources[item['key']] = item['resource_key']

    frozen_by_coordinate = {}
    for item in snapshot_cases:
        if (not isinstance(item, dict)
                or set(item) != {'spec_key', 'scenario_key', 'profile_key',
                                 'resource_key', 'candidate_keys'}
                or item.get('spec_key') not in spec_labels
                or item.get('scenario_key') not in scenario_labels
                or item.get('profile_key') not in profile_labels
                or item.get('resource_key') != profile_resources.get(item.get('profile_key'))
                or not isinstance(item.get('candidate_keys'), list)
                or not item['candidate_keys']
                or any(not isinstance(key, str) or not key
                       or key not in candidate_metadata for key in item['candidate_keys'])
                or len(item['candidate_keys']) != len(set(item['candidate_keys']))):
            return {'status': 'not_ready', 'execution': None}
        coordinate = (item['spec_key'], item['scenario_key'], item['profile_key'])
        if coordinate in frozen_by_coordinate:
            return {'status': 'not_ready', 'execution': None}
        frozen_by_coordinate[coordinate] = item

    summary_by_coordinate = {}
    for row in summary['cases']:
        coordinate = (row['spec_key'], row['scenario_key'], row['profile_key'])
        if coordinate in summary_by_coordinate:
            return {'status': 'not_ready', 'execution': None}
        summary_by_coordinate[coordinate] = row
    if set(frozen_by_coordinate) != set(summary_by_coordinate):
        return {'status': 'not_ready', 'execution': None}

    public_cases = []
    seal_rows = []
    for row in summary['cases']:
        coordinate = (row['spec_key'], row['scenario_key'], row['profile_key'])
        frozen = frozen_by_coordinate[coordinate]
        run_keys = [run['key'] for run in row['runs']]
        if (row['status'] != SimcBenchmarkExecution.STATUS_SUCCESS
                or frozen['candidate_keys'] != run_keys):
            return {'status': 'not_ready', 'execution': None}
        candidates = []
        for run in row['runs']:
            candidate = candidate_metadata[run['key']]
            seal_rows.append({
                'spec_key': row['spec_key'], 'scenario_key': row['scenario_key'],
                'profile_key': row['profile_key'],
                'spec_label': row['labels']['spec'],
                'scenario_label': row['labels']['scenario'],
                'profile_label': row['labels']['profile'],
                'status': row['status'], 'candidate_key': run['key'],
                'dps': run['dps'],
            })
            candidates.append({
                'key': run['key'],
                'label': candidate['label'],
                'type': candidate['candidate_type'],
                'icon_url': candidate['icon_url'],
                'source_label': candidate['source_label'],
                'status': run['status'], 'dps': run['dps'],
            })
        public_cases.append({
            'coordinates': {
                'spec_key': row['spec_key'], 'scenario_key': row['scenario_key'],
                'profile_key': row['profile_key'],
            },
            'labels': {
                'spec': spec_labels[coordinate[0]],
                'scenario': scenario_labels[coordinate[1]],
                'profile': profile_labels[coordinate[2]],
            },
            'status': row['status'],
            'candidates': candidates,
        })
    if _result_seal(seal_rows, execution.completed_at) != execution.result_hash:
        return {'status': 'not_ready', 'execution': None}
    return {
        'status': 'ready',
        'panel': {
            'slug': panel_snapshot['slug'], 'name': panel_snapshot['name'],
            'description': panel_snapshot['description'],
        },
        'execution': {
            'status': summary['status'],
            'completed_at': summary['completed_at'],
            'total_cases': summary['total_cases'], 'total_runs': summary['total_runs'],
            'success': summary['success'], 'partial': summary['partial'],
            'failed': summary['failed'], 'cancelled': summary['cancelled'],
            'cases': public_cases,
        },
    }
