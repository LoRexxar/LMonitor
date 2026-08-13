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
from django.db.models import CharField, Count, Prefetch, Q, Value
from django.db.models.functions import Concat
from django.utils import timezone

from botend.constants.wow import CLASS_CN, SPEC_CN, SPEC_ICON
from botend.models import (
    SimcBenchmarkCandidate, SimcBenchmarkCase, SimcBenchmarkExecution,
    SimcBenchmarkPanel, SimcBenchmarkProfile, SimcBenchmarkResult,
    SimcBenchmarkScenario, SimcTask, SimcTaskArtifact, SimulationRun,
)
from botend.services.simc_agent_oss import (
    ReportStorageError, public_legacy_report_url, public_report_url,
)
from botend.services.simc_benchmark_config import (
    MAX_PANEL_CONFIG_BYTES, SIMC_FIGHT_STYLES, SIMC_RAID_BUFFS,
    build_execution_plan,
)
from botend.services.simc_composer import SIMC_CLASS_RAID_BUFFS
from botend.services.simc_player_config import parse_manual_player_config
from botend.services.simc_task_service import (
    TaskCreationError, TaskPreparedResourceChanged, TaskValidationUnavailable,
    create_task, prepare_task_creation,
)
from botend.services.task_rerun import create_rerun, TaskRerunError
from botend.wow.talents.default_versions import DEFAULT_TALENT_VERSIONS

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
_SPEC_CN_BY_NORMALIZED_NAME = {
    re.sub(r'[^a-z0-9]', '', name.lower()): label
    for name, label in SPEC_CN.items()
}
_SPEC_ICON_BY_NORMALIZED_KEY = {
    f"{re.sub(r'[^a-z0-9]', '', class_name.lower())}_"
    f"{re.sub(r'[^a-z0-9]', '', spec_name.lower())}": icon_url
    for (class_name, spec_name), icon_url in SPEC_ICON.items()
}
_TALENT_VERSION_BY_BRANCH = {
    item['branch']: item['key'] for item in DEFAULT_TALENT_VERSIONS
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


def _spec_display_name(value, spec_key=None):
    """Format known specializations as Chinese ``spec-class`` read-model text."""
    text = str(value or '').strip()
    normalized = re.sub(r'[^a-z0-9]', '', text.lower())
    direct = _SPEC_CN_BY_NORMALIZED_NAME.get(normalized)
    if not direct:
        for name, label in sorted(_SPEC_CN_BY_NORMALIZED_NAME.items(), key=lambda item: -len(item[0])):
            if normalized.endswith(name):
                direct = label
                break
    if not direct:
        normalized_key = re.sub(r'[^a-z0-9]', '', str(spec_key or '').lower())
        for name, label in sorted(_SPEC_CN_BY_NORMALIZED_NAME.items(), key=lambda item: -len(item[0])):
            if normalized_key.endswith(name):
                direct = label
                break
    if not direct:
        return text

    class_name = ''
    normalized_key = re.sub(r'[^a-z0-9]', '', str(spec_key or '').lower())
    for name, label in CLASS_CN.items():
        if normalized_key.startswith(re.sub(r'[^a-z0-9]', '', name.lower())):
            class_name = label
            break
    return f'{direct}-{class_name}' if class_name else direct


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


def _safe_snapshot(panel, plan, *, execution_mode='supplement'):
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
        'execution_mode': execution_mode,
        # A full rerun is an atomic replacement from the panel's perspective:
        # its output is eligible for the aggregate only after every frozen input
        # has succeeded and the Execution has received its publication seal.
        # The explicit marker keeps pre-policy historical executions readable
        # without retroactively changing their established projection.
        'result_publication': 'atomic_full' if execution_mode == 'full' else 'incremental',
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


def _preflight_error(coordinate, exc):
    """Project a coordinate-bound, path-scrubbed validator failure."""
    detail = getattr(exc, 'details', None)
    messages = []
    if isinstance(detail, dict):
        diagnostics = detail.get('diagnostics')
        if isinstance(diagnostics, list):
            messages.extend(
                item.get('message') for item in diagnostics
                if isinstance(item, dict) and item.get('message')
            )
        messages.extend(value for value in (
            detail.get('error'), detail.get('message'),
        ) if value)
        nested = detail.get('details')
        if isinstance(nested, dict):
            authoritative = nested.get('authoritative_error')
            if isinstance(authoritative, dict):
                messages.extend(value for value in (
                    authoritative.get('code'), authoritative.get('message'),
                ) if value)
    reason = '; '.join(str(item) for item in messages) or str(exc)
    coordinate_label = ' / '.join(str(coordinate[key]) for key in (
        'spec_key', 'scenario_key', 'profile_key',
    ))
    resource_label = (
        f'Profile #{coordinate["profile_id"]}, APL #{coordinate["apl_id"]}, '
        f'Template #{coordinate["template_id"]}, Backend #{coordinate["backend_id"]}'
    )
    safe_reason = _safe_error(reason) or '预检失败'
    return f'{coordinate_label} ({resource_label}): {safe_reason}'


def _candidate_input_identity(candidate):
    """Only executable candidate input participates in cross-execution reuse."""
    return _canonical_hash({
        'key': candidate.get('candidate_key'),
        'candidate_type': candidate.get('candidate_type'),
        'candidate_params': candidate.get('candidate_params'),
    })


def _candidate_item_level(candidate):
    """Read optional item level from the frozen executable candidate input."""
    params = candidate.get('candidate_params')
    if not isinstance(params, dict):
        return None
    gear_swap = params.get('gear_swap')
    if not isinstance(gear_swap, dict):
        gear_swap = params
    for key in ('item_level', 'ilevel'):
        value = gear_swap.get(key)
        if value is None:
            continue
        try:
            item_level = int(value)
        except (TypeError, ValueError):
            continue
        if item_level > 0:
            return item_level
    raw_value = str(gear_swap.get('raw_value') or '')
    match = re.search(r'(?:^|,)\s*(?:ilevel|item_level)=(\d+)(?=,|$)', raw_value, re.I)
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


def _candidate_item_id(candidate):
    """Read the canonical item id used to group item-level variants in result charts."""
    params = candidate.get('candidate_params')
    if not isinstance(params, dict):
        return None
    gear_swap = params.get('gear_swap')
    if not isinstance(gear_swap, dict):
        gear_swap = params
    for key in ('item_id', 'id'):
        value = gear_swap.get(key)
        if value is None:
            continue
        try:
            item_id = int(value)
        except (TypeError, ValueError):
            continue
        if item_id > 0:
            return item_id
    raw_value = str(gear_swap.get('raw_value') or '')
    match = re.search(r'(?:^|,)\s*(?:id|item_id)=(\d+)(?=,|$)', raw_value, re.I)
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


def _candidate_item_variant_key(candidate):
    """Stable display identity for item attributes, excluding only item level."""
    params = deepcopy(candidate.get('candidate_params'))
    if not isinstance(params, dict):
        return None
    gear_swap = params.get('gear_swap')
    if not isinstance(gear_swap, dict):
        gear_swap = params
    gear_swap.pop('ilevel', None)
    gear_swap.pop('item_level', None)
    if gear_swap.get('raw_value'):
        gear_swap['raw_value'] = re.sub(
            r'(^|,)\s*(?:ilevel|item_level)=\d+(?=,|$)',
            r'\1ilevel=*',
            str(gear_swap['raw_value']),
            flags=re.I,
        )
    canonical = json.dumps(
        {'type': candidate.get('candidate_type'), 'params': params},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:20]


def _task_candidate_identities(task):
    mode_params = task.mode_params if isinstance(task.mode_params, dict) else {}
    manifest = mode_params.get('request_manifest') if isinstance(mode_params, dict) else None
    candidates = manifest.get('candidates') if isinstance(manifest, dict) else None
    if not isinstance(candidates, list):
        return {}
    return {
        candidate.get('candidate_key'): _candidate_input_identity(candidate)
        for candidate in candidates
        if isinstance(candidate, dict) and isinstance(candidate.get('candidate_key'), str)
    }


def _task_candidate_identities_through_source_chain(task):
    """Merge frozen candidate identities from a retry Task and its provenance."""
    tasks = []
    current = task
    seen = set()
    while current is not None and current.pk not in seen:
        tasks.append(current)
        seen.add(current.pk)
        if current.source_task_id is None:
            break
        current = SimcTask.objects.select_related('source_task').get(pk=current.source_task_id)
    identities = {}
    for source in reversed(tasks):
        identities.update(_task_candidate_identities(source))
    return identities


def _normalized_simulation_params_for_identity(params):
    """Canonicalize historical and explicit raid-buff schemas for result reuse.

    Composer historically treated an absent ``raid_buffs`` field as the actor's
    class raid buff.  The explicit schema represents that same behavior as an
    empty extra-buff list plus ``use_class_raid_buff=true``.  When only an
    explicit list existed historically, the equivalent toggle is false.
    """
    normalized = deepcopy(params) if isinstance(params, dict) else {}
    has_raid_buffs = 'raid_buffs' in normalized
    has_class_toggle = 'use_class_raid_buff' in normalized
    if not has_raid_buffs and not has_class_toggle:
        normalized['raid_buffs'] = []
        normalized['use_class_raid_buff'] = True
    elif has_raid_buffs and not has_class_toggle:
        normalized['use_class_raid_buff'] = False
    elif has_class_toggle and not has_raid_buffs:
        normalized['raid_buffs'] = []
    return normalized


def _coordinate_input_identity(coordinate):
    return _canonical_hash({
        'spec_key': coordinate['spec_key'], 'scenario_key': coordinate['scenario_key'],
        'profile_key': coordinate['profile_key'], 'profile_id': coordinate['profile_id'],
        'apl_id': coordinate['apl_id'], 'template_id': coordinate['template_id'],
        'backend_id': coordinate['backend_id'],
        'simulation_params': _normalized_simulation_params_for_identity(
            coordinate['simulation_params'],
        ),
    })


def _execution_contributes_to_projection(execution):
    """Every independently sealed candidate may contribute to the live projection.

    Execution status remains diagnostic: a partial/failed full rerun does not move
    the full-snapshot publication boundary, but its successful immutable candidate
    rows supersede older rows while failed/missing candidates keep falling back.
    Historical ``atomic_full`` markers use this corrected per-candidate policy too.
    """
    return True


def _latest_source_tasks_by_coordinate(panel, coordinate_filter=None):
    """Load the latest frozen Task for each coordinate, including failed Cases."""
    case_filters = {'execution__panel_id': panel.pk, 'task__isnull': False}
    if isinstance(coordinate_filter, dict):
        for key in ('spec_key', 'profile_key', 'scenario_key'):
            value = coordinate_filter.get(key)
            if value:
                case_filters[key] = str(value)
    cases = SimcBenchmarkCase.objects.filter(**case_filters)
    cases = cases.select_related(
        'task', 'task__profile_version', 'execution',
    ).order_by(
        '-execution_id', '-id',
    )
    source_tasks = {}
    for case in cases:
        if not _execution_contributes_to_projection(case.execution):
            continue
        task = case.task
        coordinate = _coordinate_input_identity({
            'spec_key': case.spec_key, 'scenario_key': case.scenario_key,
            'profile_key': case.profile_key, 'profile_id': task.profile_id,
            'apl_id': task.apl_id, 'template_id': task.template_id,
            'backend_id': task.backend_id, 'simulation_params': task.simulation_params or {},
        })
        source_tasks.setdefault(coordinate, task)
    return source_tasks


def _reusable_candidate_tasks_by_coordinate(panel, coordinate_filter=None):
    """Load the newest successful provenance for each coordinate/candidate identity.

    A full rerun is an asynchronous replacement. Its successful candidates must
    supersede older rows individually, while candidates that are still pending or
    failed continue to fall back to the last successful immutable Result. Ordering
    newest-first and deduplicating by executable identity provides that atomic
    per-candidate replacement without deleting historical rows.
    """
    coordinates = {}
    case_filters = {
        'execution__panel_id': panel.pk,
        'results__isnull': False,
    }
    if isinstance(coordinate_filter, dict):
        for key in ('spec_key', 'profile_key', 'scenario_key'):
            value = coordinate_filter.get(key)
            if value:
                case_filters[key] = str(value)
    cases = SimcBenchmarkCase.objects.filter(**case_filters)
    cases = cases.select_related(
        'task', 'task__profile_version', 'execution',
    ).prefetch_related('results').order_by(
        '-execution_id', '-id',
    ).distinct()
    for case in cases:
        if not _execution_contributes_to_projection(case.execution):
            continue
        task = case.task
        if task is None:
            continue
        coordinate = _coordinate_input_identity({
            'spec_key': case.spec_key, 'scenario_key': case.scenario_key,
            'profile_key': case.profile_key, 'profile_id': task.profile_id,
            'apl_id': task.apl_id, 'template_id': task.template_id,
            'backend_id': task.backend_id, 'simulation_params': task.simulation_params or {},
        })
        candidates = coordinates.setdefault(coordinate, {})
        identities = _task_candidate_identities_through_source_chain(task)
        for result in case.results.all():
            identity = identities.get(result.candidate_key)
            if identity and identity not in candidates:
                candidates[identity] = {'task': task, 'result': result}
    return coordinates


def _candidate_raw_report_urls(reusable_by_coordinate):
    task_ids = {
        match['task'].id
        for candidates in reusable_by_coordinate.values()
        for match in candidates.values()
    }
    if not task_ids:
        return {}
    urls = {}
    artifacts = SimcTaskArtifact.objects.filter(
        task_id__in=task_ids,
        run_id__isnull=False,
        artifact_type='html_report',
    ).select_related('run').order_by('-created_at', '-id')
    for artifact in artifacts:
        lookup = (artifact.task_id, artifact.run.candidate_key)
        if lookup in urls:
            continue
        try:
            if str(artifact.file_path or '').startswith('simc_agent_results/'):
                url = public_report_url(artifact.file_path)
            else:
                url = public_legacy_report_url(artifact.file_path)
        except ReportStorageError:
            continue
        urls[lookup] = url
    return urls


def _candidate_source_run(task, candidate_key):
    """Resolve the actual immutable Run through the retry provenance chain."""
    current = task
    seen = set()
    while current is not None and current.pk not in seen:
        seen.add(current.pk)
        run = SimulationRun.objects.filter(
            task_id=current.pk, candidate_key=candidate_key, status='completed',
        ).order_by('-sequence', '-id').first()
        if run is not None:
            return run
        current = current.source_task
    return None


def _reusable_candidate_tasks(panel, coordinate, reusable_by_coordinate=None):
    """Return finalized Task provenance by full frozen coordinate/candidate input identity."""
    if reusable_by_coordinate is None:
        reusable_by_coordinate = _reusable_candidate_tasks_by_coordinate(panel)
    return reusable_by_coordinate.get(_coordinate_input_identity(coordinate), {})


def _incremental_coordinates(panel, plan):
    """Schedule only candidate input identities absent from immutable successful results."""
    rows = []
    reusable_by_coordinate = _reusable_candidate_tasks_by_coordinate(panel)
    for coordinate in plan['cases']:
        reusable = _reusable_candidate_tasks(panel, coordinate, reusable_by_coordinate)
        missing = [candidate for candidate in coordinate['candidates']
                   if _candidate_input_identity(candidate) not in reusable]
        if missing:
            row = deepcopy(coordinate)
            row['candidates'] = missing
            row['_source_task'] = next(iter(reusable.values()))['task'] if reusable else None
            rows.append(row)
    return rows


def _plan_for_coordinates(plan, coordinates):
    plan = deepcopy(plan)
    plan['cases'] = coordinates
    plan['case_count'] = len(coordinates)
    plan['run_count'] = sum(len(item['candidates']) for item in coordinates)
    return plan


def create_execution(panel, trigger='manual', scheduled_slot=None, requested_by=None,
                     execution_mode='supplement'):
    """Create either a full fresh baseline or a failure/missing-result supplement."""
    if execution_mode not in {'full', 'supplement'}:
        _validation_error('execution_mode 必须是 full 或 supplement', 'execution_mode')
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
    preflight_errors = {}
    for coordinate in optimistic_plan['cases']:
        key = (coordinate['backend_id'], coordinate['profile_id'],
               coordinate['apl_id'], coordinate['template_id'])
        if key in prepared_by_resources or key in preflight_errors:
            continue
        try:
            prepared_by_resources[key] = prepare_task_creation(
                current_panel.created_by_id, coordinate['profile_id'],
                coordinate['template_id'], coordinate['apl_id'],
                backend_id=coordinate['backend_id'],
                is_admin=True,
            )
        except TaskPreparedResourceChanged as exc:
            raise BenchmarkExecutionConflict(
                'Benchmark execution preflight is temporarily unavailable'
            ) from exc
        except (TaskValidationUnavailable, TaskCreationError) as exc:
            if execution_mode != 'full':
                if isinstance(exc, TaskValidationUnavailable):
                    raise BenchmarkExecutionConflict(
                        'Benchmark execution preflight is temporarily unavailable'
                    ) from exc
                _validation_error(str(exc))
            preflight_errors[key] = exc

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
        incremental_coordinates = _incremental_coordinates(locked_panel, locked_plan)
        # Full rerun schedules the entire frozen surface. Results remain readable
        # from the previous execution until each replacement candidate succeeds.
        execution_coordinates = (locked_plan['cases'] if execution_mode == 'full'
                                 else incremental_coordinates)
        # Execution freezes only work it owns.  The Panel configuration remains
        # authoritative for display; completed immutable Results are reused there.
        incremental_plan = _plan_for_coordinates(locked_plan, execution_coordinates)
        snapshot = _safe_snapshot(
            locked_panel, incremental_plan, execution_mode=execution_mode,
        )
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
            for coordinate in execution_coordinates:
                key = (coordinate['backend_id'], coordinate['profile_id'],
                       coordinate['apl_id'], coordinate['template_id'])
                preflight_error = preflight_errors.get(key)
                if preflight_error is not None:
                    SimcBenchmarkCase.objects.create(
                        execution=execution, task=None,
                        status=SimcBenchmarkExecution.STATUS_FAILED,
                        error_detail=_preflight_error(coordinate, preflight_error),
                        spec_key=coordinate['spec_key'], scenario_key=coordinate['scenario_key'],
                        profile_key=coordinate['profile_key'], spec_label=coordinate['spec_label'],
                        scenario_label=coordinate['scenario_label'],
                        profile_label=coordinate['profile_label'],
                        coordinate_hash=_coordinate_hash(coordinate),
                    )
                    continue
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
                    is_admin=True,
                )
                source_task = coordinate.get('_source_task')
                if source_task is not None:
                    task.source_task = source_task
                    task.save(update_fields=['source_task'])
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
        if not execution_coordinates:
            # A supplement with no missing frozen inputs is a successful no-op.
            # Complete it immediately without publishing an empty Execution over
            # the existing aggregate/public result surface.
            execution.status = SimcBenchmarkExecution.STATUS_SUCCESS
            execution.completed_at = timezone.now()
            execution.save(update_fields=['status', 'completed_at'])
            locked_panel.active_execution = None
            locked_panel.save(update_fields=['active_execution'])
        elif not execution.cases.filter(task__isnull=False).exists():
            execution.status = SimcBenchmarkExecution.STATUS_FAILED
            execution.completed_at = timezone.now()
            execution.save(update_fields=['status', 'completed_at'])
            locked_panel.active_execution = None
            locked_panel.save(update_fields=['active_execution'])
        return execution


def _copy_failed_runs_for_retry(source_task, rerun_task, include_completed=False):
    """Freeze only non-completed Runs into a retry Task.

    Completed Run results remain on ``source_task`` and are deliberately not copied:
    a retry Task is executable work, not a synthetic full Task history.
    """
    source_runs = list(SimulationRun.objects.filter(
        task_id=source_task.pk,
    ).order_by('sequence', 'id'))
    expected = _expected_candidate_keys(source_task) or []
    by_key = {run.candidate_key: run for run in source_runs}
    retry_candidates, retry_runs = [], []
    for candidate_key in expected:
        run = by_key.get(candidate_key)
        if run is not None and run.status == 'completed' and not include_completed:
            continue
        candidate = {
            'candidate_key': candidate_key,
            'candidate_label': run.candidate_label if run else candidate_key,
            'round_number': run.round_number if run else 1,
            'candidate_params': deepcopy(run.candidate_params) if run else {},
            'display_metadata': deepcopy(run.display_metadata) if run else {},
        }
        retry_candidates.append(candidate)
        retry_runs.append(SimulationRun(
            task=rerun_task, sequence=len(retry_runs) + 1, status='pending',
            candidate_key=candidate_key, candidate_label=candidate['candidate_label'],
            round_number=candidate['round_number'], candidate_params=candidate['candidate_params'],
            display_metadata=candidate['display_metadata'],
        ))
    if not retry_runs:
        _validation_error('失败子任务没有可重跑的 Run', 'execution')
    mode_params = deepcopy(rerun_task.mode_params) or {}
    mode_params['initial_candidates'] = deepcopy(retry_candidates)
    manifest = mode_params.get('request_manifest')
    if isinstance(manifest, dict):
        manifest['candidates'] = deepcopy(retry_candidates)
    rerun_task.mode_params = mode_params
    rerun_task.save(update_fields=['mode_params'])
    SimulationRun.objects.bulk_create(retry_runs)


def cancel_execution(execution, requested_by=None):
    """Cancel one Execution without stopping the shared worker.

    The transaction is the fencing boundary: Tasks become unclaimable and active
    Run leases are invalidated before the Execution is made terminal. Agent/local
    workers must treat the resulting Task/Run state as cancellation and discard
    any late completion.
    """
    requester_id = getattr(requested_by, 'id', requested_by)
    with transaction.atomic():
        locked = SimcBenchmarkExecution.objects.select_for_update().select_related(
            'panel',
        ).get(pk=execution.pk)
        panel = SimcBenchmarkPanel.objects.select_for_update().get(pk=locked.panel_id)
        if requester_id != panel.created_by_id:
            raise PermissionDenied('Only the Panel owner may cancel this Execution')
        if locked.completed_at is not None:
            return locked

        now = timezone.now()
        cases = list(SimcBenchmarkCase.objects.select_for_update().filter(
            execution_id=locked.pk,
        ).select_related('task'))
        task_ids = [case.task_id for case in cases if case.task_id is not None]
        tasks = SimcTask.objects.select_for_update().filter(pk__in=task_ids).in_bulk()
        runs = list(SimulationRun.objects.select_for_update().filter(
            task_id__in=task_ids,
        ))

        changed_runs = []
        for run in runs:
            if run.status not in ('completed', 'failed', 'cancelled', 'canceled'):
                run.status = 'cancelled'
                run.error_detail = run.error_detail or 'Execution cancelled by owner'
                run.completed_at = now
                run.lease_token_hash = ''
                run.lease_expires_at = None
                run.lease_heartbeat_at = None
                run.lease_instance_id = ''
                run.lease_agent = None
                changed_runs.append(run)
        if changed_runs:
            SimulationRun.objects.bulk_update(changed_runs, [
                'status', 'error_detail', 'completed_at', 'lease_token_hash',
                'lease_expires_at', 'lease_heartbeat_at', 'lease_instance_id',
                'lease_agent',
            ])

        changed_tasks = []
        changed_cases = []
        for case in cases:
            task = tasks.get(case.task_id)
            if task is not None and task.current_status not in TASK_TERMINAL:
                task.current_status = TASK_CANCELLED
                task.is_active = False
                task.error_detail = task.error_detail or 'Execution cancelled by owner'
                task.completed_at = now
                changed_tasks.append(task)
            if task is None or task.current_status != TASK_SUCCESS:
                case.status = SimcBenchmarkExecution.STATUS_CANCELLED
                case.error_detail = case.error_detail or 'Execution cancelled by owner'
                changed_cases.append(case)
            elif case.status != SimcBenchmarkExecution.STATUS_SUCCESS:
                case.status = SimcBenchmarkExecution.STATUS_SUCCESS
                case.error_detail = ''
                changed_cases.append(case)
        if changed_tasks:
            SimcTask.objects.bulk_update(changed_tasks, [
                'current_status', 'is_active', 'error_detail', 'completed_at',
            ])
        if changed_cases:
            SimcBenchmarkCase.objects.bulk_update(changed_cases, ['status', 'error_detail'])

        locked.status = SimcBenchmarkExecution.STATUS_CANCELLED
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


def rerun_failed_cases(execution, requested_by=None, case_id=None):
    """Create a retry Execution and materialize validation failures per Case.

    Cases with a frozen source Task keep the immutable Task-rerun path.  A Case
    rejected before Task creation is rebuilt from the Panel's current coordinate,
    so Profile/APL fixes can take effect; another rejection becomes a failed child
    Case instead of aborting the whole rerun request.
    """
    requester_id = getattr(requested_by, 'id', requested_by)

    preliminary_source = SimcBenchmarkExecution.objects.select_related('panel').get(pk=execution.pk)
    preliminary_panel = preliminary_source.panel
    if requester_id != preliminary_panel.created_by_id:
        raise PermissionDenied('Only the Panel owner may rerun failed benchmark cases')
    if preliminary_source.completed_at is None:
        raise BenchmarkExecutionConflict('Only a completed Execution can be rerun')
    case_filter = {'execution': preliminary_source}
    if case_id is None:
        case_filter['status__in'] = (
            SimcBenchmarkExecution.STATUS_FAILED,
            SimcBenchmarkExecution.STATUS_PARTIAL,
            SimcBenchmarkExecution.STATUS_CANCELLED,
        )
    else:
        case_filter['pk'] = case_id
    preliminary_failed_cases = list(SimcBenchmarkCase.objects.filter(
        **case_filter,
    ).select_related('task').order_by('id'))
    if case_id is not None and not preliminary_failed_cases:
        _validation_error('指定的 Benchmark 子任务不存在', 'case_id')
    if not preliminary_failed_cases:
        _validation_error('没有可重跑的失败子任务', 'execution')

    current_plan = None
    current_plan_identity = None
    current_coordinates = {}
    resource_key_by_coordinate = {}
    prepared_by_resources = {}
    preflight_errors = {}
    if any(case.task_id is None for case in preliminary_failed_cases):
        current_plan = build_execution_plan(preliminary_panel, lock=False)
        current_plan_identity = _canonical_hash(current_plan)
        current_coordinates = {
            (row['spec_key'], row['scenario_key'], row['profile_key']): row
            for row in current_plan['cases']
        }
        for case in preliminary_failed_cases:
            if case.task_id is not None:
                continue
            coordinate_key = (case.spec_key, case.scenario_key, case.profile_key)
            coordinate = current_coordinates.get(coordinate_key)
            if coordinate is None:
                continue
            resource_key = (
                coordinate['backend_id'], coordinate['profile_id'],
                coordinate['apl_id'], coordinate['template_id'],
            )
            resource_key_by_coordinate[coordinate_key] = resource_key
            if resource_key in prepared_by_resources or resource_key in preflight_errors:
                continue
            try:
                prepared_by_resources[resource_key] = prepare_task_creation(
                    preliminary_panel.created_by_id, coordinate['profile_id'],
                    coordinate['template_id'], coordinate['apl_id'],
                    backend_id=coordinate['backend_id'],
                    is_admin=True,
                )
            except (TaskPreparedResourceChanged, TaskValidationUnavailable,
                    TaskCreationError) as exc:
                preflight_errors[resource_key] = exc

    with transaction.atomic():
        source = SimcBenchmarkExecution.objects.select_for_update().get(pk=execution.pk)
        panel = SimcBenchmarkPanel.objects.select_for_update().get(pk=source.panel_id)
        if requester_id != panel.created_by_id:
            raise PermissionDenied('Only the Panel owner may rerun failed benchmark cases')
        if source.completed_at is None:
            raise BenchmarkExecutionConflict('Only a completed Execution can be rerun')
        case_filter = {'execution': source}
        if case_id is None:
            case_filter['status__in'] = (
                SimcBenchmarkExecution.STATUS_FAILED,
                SimcBenchmarkExecution.STATUS_PARTIAL,
                SimcBenchmarkExecution.STATUS_CANCELLED,
            )
        else:
            case_filter['pk'] = case_id
        failed_cases = list(SimcBenchmarkCase.objects.filter(
            **case_filter,
        ).select_related('task').order_by('id'))
        if not failed_cases:
            _validation_error('没有可重跑的失败子任务', 'execution')
        if current_plan is not None:
            locked_current_plan = build_execution_plan(panel, lock=True)
            if _canonical_hash(locked_current_plan) != current_plan_identity:
                raise BenchmarkExecutionConflict(
                    'Panel configuration changed during failed-case preflight'
                )
        if panel.active_execution_id:
            active = panel.active_execution
            if active and active.completed_at is None:
                raise BenchmarkExecutionConflict('Panel already has an unfinished benchmark execution')

        source_cases = {(case.spec_key, case.scenario_key, case.profile_key): case
                        for case in SimcBenchmarkCase.objects.filter(execution=source)
                        .select_related('task').order_by('id')}
        failed_coordinates = {
            (case.spec_key, case.scenario_key, case.profile_key)
            for case in failed_cases
        }
        # A retry retains the full frozen logical surface.  It schedules Tasks only
        # for failed coordinates, while independently persisted Case Results from
        # earlier Executions remain available to the incremental aggregate.
        snapshot = deepcopy(source.config_snapshot)
        snapshot['execution_mode'] = 'supplement'
        snapshot['source_execution_id'] = source.pk
        snapshot_coordinates = {
            (case_data.get('spec_key'), case_data.get('scenario_key'),
             case_data.get('profile_key'))
            for case_data in snapshot.get('cases', [])
        }
        if not failed_coordinates.issubset(snapshot_coordinates):
            _validation_error('失败子任务缺少冻结坐标', 'execution')
        new_execution = SimcBenchmarkExecution.objects.create(
            panel=panel, trigger=SimcBenchmarkExecution.TRIGGER_MANUAL,
            config_snapshot=snapshot,
            display_metadata=deepcopy(source.display_metadata),
            config_hash=_canonical_hash(snapshot),
        )
        panel.active_execution = new_execution
        panel.save(update_fields=['active_execution'])
        for case_data in snapshot['cases']:
            coordinate = (
                case_data['spec_key'], case_data['scenario_key'], case_data['profile_key'],
            )
            if coordinate not in failed_coordinates:
                continue
            source_case = source_cases[coordinate]
            if source_case.task_id is None:
                current_coordinate = current_coordinates.get(coordinate)
                if current_coordinate is None:
                    error_detail = (
                        f'{source_case.spec_key} / {source_case.scenario_key} / '
                        f'{source_case.profile_key}: 当前 Panel 配置中已找不到该坐标；'
                        '请恢复对应专精、场景和 Profile 后重跑'
                    )
                    SimcBenchmarkCase.objects.create(
                        execution=new_execution, task=None,
                        status=SimcBenchmarkExecution.STATUS_FAILED,
                        error_detail=_safe_error(error_detail),
                        spec_key=source_case.spec_key,
                        scenario_key=source_case.scenario_key,
                        profile_key=source_case.profile_key,
                        spec_label=source_case.spec_label,
                        scenario_label=source_case.scenario_label,
                        profile_label=source_case.profile_label,
                        coordinate_hash=source_case.coordinate_hash,
                    )
                    continue
                resource_key = resource_key_by_coordinate[coordinate]
                preflight_error = preflight_errors.get(resource_key)
                if preflight_error is not None:
                    SimcBenchmarkCase.objects.create(
                        execution=new_execution, task=None,
                        status=SimcBenchmarkExecution.STATUS_FAILED,
                        error_detail=_preflight_error(current_coordinate, preflight_error),
                        spec_key=source_case.spec_key,
                        scenario_key=source_case.scenario_key,
                        profile_key=source_case.profile_key,
                        spec_label=current_coordinate['spec_label'],
                        scenario_label=current_coordinate['scenario_label'],
                        profile_label=current_coordinate['profile_label'],
                        coordinate_hash=_coordinate_hash(current_coordinate),
                    )
                    continue
                try:
                    task = create_task(
                        user_id=panel.created_by_id,
                        name=_task_name(panel.pk, new_execution.pk, current_coordinate),
                        profile_id=current_coordinate['profile_id'],
                        template_id=current_coordinate['template_id'],
                        apl_id=current_coordinate['apl_id'],
                        backend_id=current_coordinate['backend_id'],
                        mode='comparison',
                        simulation_params=deepcopy(current_coordinate['simulation_params']),
                        mode_params={'request_manifest': {
                            'candidates': deepcopy(current_coordinate['candidates']),
                        }},
                        candidates=deepcopy(current_coordinate['candidates']),
                        prepared=prepared_by_resources[resource_key],
                        is_admin=True,
                    )
                except (TaskPreparedResourceChanged, TaskValidationUnavailable,
                        TaskCreationError) as exc:
                    SimcBenchmarkCase.objects.create(
                        execution=new_execution, task=None,
                        status=SimcBenchmarkExecution.STATUS_FAILED,
                        error_detail=_preflight_error(current_coordinate, exc),
                        spec_key=source_case.spec_key,
                        scenario_key=source_case.scenario_key,
                        profile_key=source_case.profile_key,
                        spec_label=current_coordinate['spec_label'],
                        scenario_label=current_coordinate['scenario_label'],
                        profile_label=current_coordinate['profile_label'],
                        coordinate_hash=_coordinate_hash(current_coordinate),
                    )
                    continue
                SimcBenchmarkCase.objects.create(
                    execution=new_execution, task=task,
                    spec_key=source_case.spec_key,
                    scenario_key=source_case.scenario_key,
                    profile_key=source_case.profile_key,
                    spec_label=current_coordinate['spec_label'],
                    scenario_label=current_coordinate['scenario_label'],
                    profile_label=current_coordinate['profile_label'],
                    coordinate_hash=_coordinate_hash(current_coordinate),
                )
                continue
            try:
                task = create_rerun(
                    source_task_id=source_case.task_id,
                    user_id=panel.created_by_id,
                )
            except TaskRerunError as exc:
                _validation_error(str(exc), 'execution')
            # Retry Tasks contain only failed executable candidates.  Completed
            # candidates remain immutable provenance on the source Task/Case.
            task.name = _task_name(panel.pk, new_execution.pk, case_data)
            task.save(update_fields=['name'])
            _copy_failed_runs_for_retry(
                source_case.task, task,
                include_completed=case_id is not None,
            )
            SimcBenchmarkCase.objects.create(
                execution=new_execution, task=task,
                spec_key=source_case.spec_key, scenario_key=source_case.scenario_key,
                profile_key=source_case.profile_key, spec_label=source_case.spec_label,
                scenario_label=source_case.scenario_label, profile_label=source_case.profile_label,
                coordinate_hash=source_case.coordinate_hash,
            )
        if not new_execution.cases.filter(task__isnull=False).exists():
            new_execution.status = SimcBenchmarkExecution.STATUS_FAILED
            new_execution.completed_at = timezone.now()
            new_execution.save(update_fields=['status', 'completed_at'])
            panel.active_execution = None
            panel.save(update_fields=['active_execution'])
        return new_execution


def rerun_case(execution, case_id, requested_by=None):
    """Rerun exactly one frozen Benchmark Case as an independent Execution."""
    return rerun_failed_cases(execution, requested_by=requested_by, case_id=case_id)


def summarize_panel_coverage_counts(panels):
    """Return list-safe full-surface coverage with grouped database aggregates.

    A supplement Execution owns only missing candidate Runs, so it must never
    replace the Panel's full-surface denominator or hide immutable Results from
    earlier Executions.  The list selects the aggregate baseline (or, for legacy
    Panels, the largest frozen full-surface snapshot), then counts distinct
    coordinate/candidate Result identities at and after that boundary in SQL.
    It deliberately does not build a plan or load Result rows into Python.
    """
    panels = list(panels)
    coverage = {
        panel.pk: {
            'aggregate_baseline_execution_id': panel.aggregate_baseline_execution_id,
            'coordinates': 0,
            'candidate_runs': 0,
            'current_plan_runs': 0,
            'plan_delta_runs': None,
            'available_results': 0,
            'missing_results': 0,
            'source_executions': [],
        }
        for panel in panels
    }
    if not panels:
        return coverage

    panel_ids = [panel.pk for panel in panels]
    scenario_counts = {
        row['panel_id']: row['count']
        for row in SimcBenchmarkScenario.objects.filter(
            panel_id__in=panel_ids, is_enabled=True,
        ).values('panel_id').annotate(count=Count('id'))
    }
    profile_counts = list(SimcBenchmarkProfile.objects.filter(
        panel_spec__panel_id__in=panel_ids,
        panel_spec__is_enabled=True,
        is_enabled=True,
    ).values(
        'panel_spec__panel_id', 'panel_spec__spec_key',
    ).annotate(count=Count('id')))
    candidates_by_panel = {}
    for row in SimcBenchmarkCandidate.objects.filter(
        panel_id__in=panel_ids, is_enabled=True,
    ).values('panel_id', 'spec_keys'):
        candidates_by_panel.setdefault(row['panel_id'], []).append(row['spec_keys'])

    for row in profile_counts:
        panel_id = row['panel_spec__panel_id']
        spec_key = row['panel_spec__spec_key']
        applicable_candidates = sum(
            1
            for spec_keys in candidates_by_panel.get(panel_id, [])
            if not spec_keys or spec_key in spec_keys
        )
        coverage[panel_id]['current_plan_runs'] += (
            row['count']
            * scenario_counts.get(panel_id, 0)
            * (1 + applicable_candidates)
        )

    # Old Panels predate aggregate_baseline_execution.  Their largest frozen
    # snapshot is the complete surface; a later supplement is only its delta.
    executions_by_panel = {}
    for row in SimcBenchmarkExecution.objects.filter(panel__in=panels).values(
        'id', 'panel_id', 'config_snapshot',
    ):
        executions_by_panel.setdefault(row['panel_id'], []).append(row)

    boundaries = {}
    for panel in panels:
        rows = executions_by_panel.get(panel.pk, [])
        selected = next((row for row in rows if row['id'] == panel.aggregate_baseline_execution_id), None)
        if selected is None:
            selected = max(
                rows,
                key=lambda row: (
                    int((row['config_snapshot'] or {}).get('run_count') or 0),
                    int((row['config_snapshot'] or {}).get('case_count') or 0),
                    row['id'],
                ),
                default=None,
            )
        if selected is None:
            continue
        snapshot = selected['config_snapshot'] if isinstance(selected['config_snapshot'], dict) else {}
        item = coverage[panel.pk]
        item['aggregate_baseline_execution_id'] = selected['id']
        item['coordinates'] = int(snapshot.get('case_count') or 0)
        item['candidate_runs'] = int(snapshot.get('run_count') or 0)
        item['plan_delta_runs'] = item['current_plan_runs'] - item['candidate_runs']
        item['missing_results'] = item['candidate_runs']
        boundaries[panel.pk] = selected['id']

    # Count the latest available logical result surface across all immutable
    # executions. A replacement Execution is asynchronous, so its missing or
    # failed candidates must not hide an older successful row.
    identity_expression = Concat(
        'case__coordinate_hash', Value(':'), 'candidate_key',
        output_field=CharField(),
    )
    boundary_filter = Q()
    for panel_id in boundaries:
        boundary_filter |= Q(case__execution__panel_id=panel_id)
    grouped_results = SimcBenchmarkResult.objects.filter(boundary_filter).values(
        'case__execution__panel_id',
    ).annotate(available=Count(identity_expression, distinct=True))
    for row in grouped_results:
        panel_id = row['case__execution__panel_id']
        item = coverage[panel_id]
        result_count = row['available'] or 0
        item['available_results'] = result_count
        item['missing_results'] = max(0, item['candidate_runs'] - result_count)

    return coverage


def summarize_incremental_panel_coverage(panel):
    """Summarize the current Panel's full logical surface and reusable Results.

    This is deliberately separate from any one Execution snapshot: retry Executions
    can own only a small subset of Cases while older immutable Results still cover
    the rest of the Panel's current 96-coordinate plan.
    """
    plan = build_execution_plan(panel)
    reusable_by_coordinate = _reusable_candidate_tasks_by_coordinate(panel)
    available_results = 0
    source_counts = {}
    for coordinate in plan['cases']:
        reusable = _reusable_candidate_tasks(panel, coordinate, reusable_by_coordinate)
        for candidate in coordinate['candidates']:
            match = reusable.get(_candidate_input_identity(candidate))
            if match is None:
                continue
            available_results += 1
            execution_id = match['result'].case.execution_id
            source_counts[execution_id] = source_counts.get(execution_id, 0) + 1
    candidate_runs = sum(len(coordinate['candidates']) for coordinate in plan['cases'])
    return {
        'aggregate_baseline_execution_id': panel.aggregate_baseline_execution_id,
        'coordinates': len(plan['cases']),
        'candidate_runs': candidate_runs,
        'available_results': available_results,
        'missing_results': max(0, candidate_runs - available_results),
        'source_executions': [
            {'execution_id': execution_id, 'results': count}
            for execution_id, count in sorted(source_counts.items())
        ],
    }


def _selected_plan_coordinate(cases, requested):
    """Resolve possibly-invalid URL keys to the first compatible plan coordinate."""
    if not cases:
        return None
    requested = requested if isinstance(requested, dict) else {}
    spec_key = str(requested.get('spec_key') or '')
    matching_specs = [row for row in cases if row['spec_key'] == spec_key]
    spec_cases = matching_specs or [row for row in cases if row['spec_key'] == cases[0]['spec_key']]

    profile_key = str(requested.get('profile_key') or '')
    matching_profiles = [row for row in spec_cases if row['profile_key'] == profile_key]
    profile_cases = matching_profiles or [
        row for row in spec_cases if row['profile_key'] == spec_cases[0]['profile_key']
    ]

    scenario_key = str(requested.get('scenario_key') or '')
    return next(
        (row for row in profile_cases if row['scenario_key'] == scenario_key),
        profile_cases[0],
    )


def _coordinate_option(coordinate):
    params = coordinate.get('simulation_params') or {}
    return {
        'spec_key': coordinate['spec_key'],
        'scenario_key': coordinate['scenario_key'],
        'profile_key': coordinate['profile_key'],
        'labels': {
            'spec': _spec_display_name(coordinate['spec_label'], coordinate['spec_key']),
            'scenario': coordinate['scenario_label'],
            'profile': coordinate['profile_label'],
        },
        'scenario_detail': {
            'desired_targets': params.get('desired_targets', 1),
            'max_time': params.get('max_time', 300),
        },
    }


def _profile_detail_from_payload(payload, spec_key):
    """Project the immutable Profile snapshot used by a result-producing Task."""
    payload = payload if isinstance(payload, dict) else {}
    detail = parse_manual_player_config(
        payload.get('player_equipment') or '', payload.get('spec') or spec_key,
    )
    identity = detail.get('identity')
    if isinstance(identity, dict):
        class_key, _, fallback_spec_key = str(spec_key or '').partition('_')
        identity['class_name'] = identity.get('class_name') or class_key
        identity['spec_key'] = identity.get('spec') or fallback_spec_key
        identity['spec'] = _spec_display_name(identity.get('spec'), spec_key)
    if not detail['talents']['build_code']:
        detail['talents']['build_code'] = payload.get('talent') or ''
    is_ptr = payload.get('use_ptr') is True
    detail['is_ptr'] = is_ptr
    branch = 'ptr' if is_ptr else 'retail'
    detail['talent_version'] = _TALENT_VERSION_BY_BRANCH.get(branch, '')
    return detail


def _simulation_detail_from_task(task, fallback_params, profile_detail):
    """Project the effective combat and Buff facts frozen on the result Task."""
    params = task.simulation_params if task is not None else fallback_params
    params = params if isinstance(params, dict) else {}
    fight_style = str(params.get('fight_style') or 'Patchwerk')
    fight_style_labels = dict(SIMC_FIGHT_STYLES)
    raid_buff_labels = dict(SIMC_RAID_BUFFS)

    def labeled_buffs(values):
        return [
            {'value': value, 'label': raid_buff_labels.get(value, value)}
            for value in values
        ]

    # Preserve the Composer's historical three-state contract: a request with
    # neither key enables the actor's own class Buff, while explicit raid_buffs
    # without the newer toggle means extras only.
    use_class_raid_buff = (
        params.get('use_class_raid_buff') is True
        if 'use_class_raid_buff' in params
        else 'raid_buffs' not in params
    )
    identity = (profile_detail or {}).get('identity') or {}
    class_name = str(identity.get('class_name') or '').strip().lower()
    class_raid_buffs = SIMC_CLASS_RAID_BUFFS.get(class_name, ()) if use_class_raid_buff else ()
    raid_buffs = params.get('raid_buffs') or ()
    return {
        'fight_style': {
            'value': fight_style,
            'label': fight_style_labels.get(fight_style, fight_style),
        },
        'desired_targets': params.get('desired_targets', 1),
        'max_time': params.get('max_time', 300),
        'vary_combat_length': params.get('vary_combat_length'),
        'enemy_type': params.get('enemy_type'),
        'iterations': params.get('iterations', 10000),
        'target_error': params.get('target_error'),
        'use_class_raid_buff': use_class_raid_buff,
        'class_raid_buffs': labeled_buffs(class_raid_buffs),
        'raid_buffs': labeled_buffs(raid_buffs),
        'source_task_id': task.pk if task is not None else None,
    }


def _spec_icon_url(spec_key):
    class_key, _, spec_name = str(spec_key or '').lower().partition('_')
    normalized = (
        f"{re.sub(r'[^a-z0-9]', '', class_key)}_"
        f"{re.sub(r'[^a-z0-9]', '', spec_name)}"
    )
    return _SPEC_ICON_BY_NORMALIZED_KEY.get(normalized, '')


def serialize_incremental_panel_results(panel, *, coordinate_filter=None,
                                        scenario_filter=None, spec_filter=None,
                                        include_coordinate_options=False):
    """Aggregate reusable Results for all, one spec, coordinate, or scenario."""
    plan = build_execution_plan(panel, lock=False)
    plan_cases = plan['cases']
    if spec_filter is not None:
        plan_cases = [row for row in plan_cases if row['spec_key'] == str(spec_filter)]
    selected = (
        _selected_plan_coordinate(plan_cases, coordinate_filter)
        if coordinate_filter is not None else None
    )
    selected_scenario = None
    if coordinate_filter is None and scenario_filter is not None and plan_cases:
        requested_scenario = str(scenario_filter or '')
        selected_scenario = next(
            (row['scenario_key'] for row in plan_cases
             if row['scenario_key'] == requested_scenario),
            plan_cases[0]['scenario_key'],
        )
    if selected is not None:
        projected_cases = [selected]
        selected_filter = {
            key: selected[key] for key in ('spec_key', 'profile_key', 'scenario_key')
        }
    elif selected_scenario is not None:
        projected_cases = [
            row for row in plan_cases if row['scenario_key'] == selected_scenario
        ]
        selected_filter = {'scenario_key': selected_scenario}
    else:
        projected_cases = [] if coordinate_filter is not None else plan_cases
        selected_filter = None
    reusable_by_coordinate = _reusable_candidate_tasks_by_coordinate(panel, selected_filter)
    report_urls = _candidate_raw_report_urls(reusable_by_coordinate)
    source_tasks_by_coordinate = _latest_source_tasks_by_coordinate(panel, selected_filter)
    profiles = {
        row.profile_id: row.profile
        for spec in panel.specs.prefetch_related('profiles__profile')
        for row in spec.profiles.all()
        if selected is None or row.profile_id == selected['profile_id']
    }
    profile_details = {}
    coordinates = []
    for coordinate in projected_cases:
        profile_id = coordinate['profile_id']
        reusable = _reusable_candidate_tasks(panel, coordinate, reusable_by_coordinate)
        source_task = next(
            (
                reusable[identity]['task']
                for candidate in coordinate['candidates']
                for identity in [_candidate_input_identity(candidate)]
                if identity in reusable
            ),
            source_tasks_by_coordinate.get(_coordinate_input_identity(coordinate)),
        )
        profile_version = source_task.profile_version if source_task is not None else None
        detail_key = (profile_id, profile_version.pk if profile_version is not None else None)
        if detail_key not in profile_details:
            if profile_version is not None:
                profile_details[detail_key] = _profile_detail_from_payload(
                    profile_version.payload, coordinate['spec_key'],
                )
            else:
                profile = profiles.get(profile_id)
                profile_details[detail_key] = (
                    _profile_detail_from_payload({
                        'player_equipment': profile.player_equipment,
                        'spec': profile.spec,
                        'talent': profile.talent,
                        'use_ptr': profile.use_ptr,
                    }, coordinate['spec_key'])
                    if profile is not None else None
                )
            if profile_details[detail_key] is not None:
                profile_details[detail_key]['profile_id'] = profile_id
        scenario_params = next(
            (match['task'].simulation_params or {} for match in reusable.values()),
            coordinate['simulation_params'],
        )
        rows = []
        coordinate_audit = None
        for candidate in coordinate['candidates']:
            match = reusable.get(_candidate_input_identity(candidate))
            if match:
                result_task = match['task']
                source_run = _candidate_source_run(result_task, candidate['candidate_key'])
                effect = candidate.get('effect') or ''
                row = {
                    'key': candidate['candidate_key'],
                    'label': candidate['candidate_label'],
                    'type': candidate['candidate_type'],
                    'icon_url': candidate['icon_url'],
                    'source_label': candidate['source_label'],
                    'dps': float(match['result'].dps), 'task_id': result_task.pk,
                    'source_result_id': match['result'].pk,
                }
                if effect:
                    row['effect'] = effect
                report_url = report_urls.get((match['task'].pk, candidate['candidate_key']))
                if report_url:
                    row['raw_report_url'] = report_url
                item_id = _candidate_item_id(candidate)
                item_level = _candidate_item_level(candidate)
                if item_id is not None and item_level is not None:
                    row['item_id'] = item_id
                    row['item_variant_key'] = _candidate_item_variant_key(candidate)
                if item_level is not None:
                    row['item_level'] = item_level
                rows.append(row)
                if candidate['candidate_key'] == 'baseline':
                    manifest = source_run.resource_manifest if source_run is not None else {}
                    manifest = manifest if isinstance(manifest, dict) else {}
                    coordinate_audit = {
                        'profile_identity': (
                            result_task.profile_version.content_hash
                            if result_task.profile_version_id else None
                        ),
                        'apl_identity': (
                            result_task.apl_version.content_hash
                            if result_task.apl_version_id else None
                        ),
                        'apl_label': result_task.apl.name,
                        'template_identity': (
                            result_task.template_version.content_hash
                            if result_task.template_version_id else None
                        ),
                        'backend_version': manifest.get('backend_version'),
                        'simulation_params': result_task.simulation_params or {},
                    }
        coordinate_payload = {
            'spec_key': coordinate['spec_key'], 'scenario_key': coordinate['scenario_key'],
            'profile_key': coordinate['profile_key'],
            'spec_icon_url': _spec_icon_url(coordinate['spec_key']),
            'labels': {
                'spec': _spec_display_name(coordinate['spec_label'], coordinate['spec_key']),
                'scenario': coordinate['scenario_label'],
                'profile': coordinate['profile_label'],
            },
            'profile_detail': profile_details[detail_key],
            'simulation_detail': _simulation_detail_from_task(
                source_task, coordinate['simulation_params'], profile_details[detail_key],
            ),
            'scenario_detail': {
                'desired_targets': scenario_params.get('desired_targets', 1),
                'max_time': scenario_params.get('max_time', 300),
            },
            'candidates': rows,
        }
        if coordinate_audit is not None:
            coordinate_payload['audit'] = coordinate_audit
        coordinates.append(coordinate_payload)
    payload = {'panel_id': panel.pk, 'coordinates': coordinates}
    if include_coordinate_options:
        payload['coordinate_options'] = [_coordinate_option(row) for row in plan_cases]
    return payload


def serialize_panel_apl_ranking_results(panel, *, spec_key, scenario_key):
    """Project auditable historical baseline APL results for one bounded coordinate."""
    panel_spec = panel.specs.filter(spec_key=spec_key, is_enabled=True).first()
    if panel_spec is None:
        return []
    results = SimcBenchmarkResult.objects.filter(
        case__execution__panel_id=panel.pk,
        case__spec_key=spec_key,
        case__scenario_key=scenario_key,
        case__task__isnull=False,
        candidate_key='baseline',
    ).select_related(
        'case', 'case__task', 'case__task__profile_version',
        'case__task__apl_version', 'case__task__template_version', 'case__task__apl',
    ).order_by('-case__execution_id', '-case_id', '-id')

    def project(result):
        task = result.case.task
        source_run = _candidate_source_run(task, 'baseline')
        manifest = source_run.resource_manifest if source_run is not None else {}
        manifest = manifest if isinstance(manifest, dict) else {}
        if not (task.profile_version_id and task.apl_version_id and task.template_version_id):
            return None
        backend_version = manifest.get('backend_version')
        apl_payload = task.apl_version.payload or {}
        return {
            'spec_key': result.case.spec_key,
            'spec_label': result.case.spec_label,
            'profile_key': result.case.profile_key,
            'profile_label': result.case.profile_label,
            'scenario_label': result.case.scenario_label,
            'apl_key': task.apl_version.content_hash,
            'apl_label': apl_payload.get('name') or task.apl.name,
            '_apl_id': task.apl_id,
            'dps': float(result.dps),
            'source_result_id': result.pk,
            'resource_versions': {
                'profile': task.profile_version.content_hash,
                'template': task.template_version.content_hash,
                'apl': task.apl_version.content_hash,
                'backend': backend_version,
            },
            'simulation_params': task.simulation_params or {},
        }

    raw_results = list(results)
    # Pick the newest result for each mutable APL before auditing it. An invalid
    # newest result must not silently fall back to an older APL version.
    latest_by_apl = {}
    for result in raw_results:
        apl_id = result.case.task.apl_id
        latest_by_apl.setdefault(apl_id, result)
    projected = [project(result) for result in latest_by_apl.values()]
    projected = [row for row in projected if row is not None]
    if raw_results and not projected:
        return None
    anchor = next((row for row in projected if row['_apl_id'] == panel_spec.apl_id), None)
    if anchor is None:
        return []
    anchor_identity = (
        anchor['resource_versions']['profile'], anchor['resource_versions']['template'],
        anchor['resource_versions']['backend'],
        json.dumps(anchor['simulation_params'], sort_keys=True, separators=(',', ':'), ensure_ascii=False),
    )
    rankings = []
    seen_apls = set()
    for row in projected:
        identity = (
            row['resource_versions']['profile'], row['resource_versions']['template'],
            row['resource_versions']['backend'],
            json.dumps(row['simulation_params'], sort_keys=True, separators=(',', ':'), ensure_ascii=False),
        )
        # APL identity is the mutable SimcApl row, not one immutable version.
        # The queryset is newest-first, so the first comparable row wins.
        if identity != anchor_identity or row['_apl_id'] in seen_apls:
            continue
        seen_apls.add(row['_apl_id'])
        row.pop('_apl_id', None)
        rankings.append(row)
    return rankings


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
    """Return ordered executable candidates, or None if the frozen state is unusable."""
    mode_params = task.mode_params if isinstance(task.mode_params, dict) else {}
    candidates = mode_params.get('initial_candidates')
    if not isinstance(candidates, list) or not candidates:
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


def _runs_through_source_chain(task):
    """Overlay retry Runs on their immutable source Task history by candidate key."""
    tasks = []
    current = task
    seen = set()
    while current is not None and current.pk not in seen:
        tasks.append(current)
        seen.add(current.pk)
        if current.source_task_id is None:
            break
        current = SimcTask.objects.select_related('source_task').get(pk=current.source_task_id)
    runs_by_key = {}
    for source in reversed(tasks):
        runs = getattr(source, '_benchmark_runs', None)
        if runs is None:
            runs = SimulationRun.objects.filter(task_id=source.pk).order_by('sequence', 'id')
        for run in runs:
            runs_by_key[run.candidate_key] = run
    return runs_by_key


def _summarize_live_execution(execution):
    """Derive state from Runs, using Task for abandoned Runs and zero-run terminal edges."""
    execution = _load_execution(execution)
    cases = execution._benchmark_cases
    expected_by_coordinate = dict(_snapshot_layout(execution) or [])
    count_names = ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
    counts = {name: 0 for name in count_names}
    run_counts = {name: 0 for name in ('pending', 'running', 'success', 'failed', 'cancelled')}
    rows = []
    total_runs = 0

    for case in cases:
        task = case.task
        if task is None:
            coordinate = (case.spec_key, case.scenario_key, case.profile_key)
            failed_runs = len(expected_by_coordinate.get(coordinate) or [])
            total_runs += failed_runs
            run_counts['failed'] += failed_runs
            counts['failed'] += 1
            rows.append({
                'spec_key': case.spec_key, 'scenario_key': case.scenario_key,
                'profile_key': case.profile_key, '_case_id': case.pk,
                'labels': {'spec': case.spec_label, 'scenario': case.scenario_label,
                           'profile': case.profile_label},
                'status': 'failed', 'task_id': None, 'task_status': 'failed',
                'task_status_label': '失败', 'task_progress': None,
                'error': case.error_detail or None, 'runs': [],
            })
            continue
        task_status = TASK_STATUS_NAMES.get(task.current_status, 'failed')
        coordinate = (case.spec_key, case.scenario_key, case.profile_key)
        expected_keys = expected_by_coordinate.get(coordinate) or _expected_candidate_keys(task)
        run_rows, effective_statuses = [], []
        errors = [task.error_detail]
        if task.source_task_id is None:
            ordered_runs = list(task._benchmark_runs)
        else:
            runs_by_key = _runs_through_source_chain(task)
            ordered_runs = [runs_by_key[key] for key in expected_keys if key in runs_by_key] \
                if expected_keys is not None else list(runs_by_key.values())
        for run in ordered_runs:
            total_runs += 1
            summary = run.result_summary if isinstance(run.result_summary, dict) else {}
            semantic_error = ''
            if summary.get('valid') is False:
                semantic_error = str(
                    summary.get('reason') or summary.get('error')
                    or 'SimC 结果语义校验未通过'
                ).strip()
            run_status = (
                'failed' if semantic_error
                else _effective_run_status(task_status, run.status)
            )
            effective_statuses.append(run_status)
            run_counts[run_status] += 1
            errors.extend((run.error_detail, semantic_error))
            run_rows.append({
                'key': run.candidate_key, 'label': run.candidate_label,
                # Live DPS is internal reconciliation input, never display output.
                'status': run_status, 'dps': None,
                '_raw_dps': None if semantic_error else summary.get('dps'),
            })
        actual_keys = [run.candidate_key for run in ordered_runs]
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
    expected_by_coordinate = dict(_snapshot_layout(execution) or [])
    failed_runs = 0
    rows = []
    for case in cases:
        status = case.status if case.status in counts else 'failed'
        counts[status] += 1
        task = case.task
        task_status = TASK_STATUS_NAMES.get(task.current_status, 'failed') if task else None
        if task is None and status == 'failed':
            failed_runs += len(expected_by_coordinate.get(
                (case.spec_key, case.scenario_key, case.profile_key),
            ) or [])
        rows.append({
            'spec_key': case.spec_key, 'scenario_key': case.scenario_key,
            'profile_key': case.profile_key, '_case_id': case.pk,
            'labels': {'spec': case.spec_label, 'scenario': case.scenario_label,
                       'profile': case.profile_label},
            'status': status, 'task_id': case.task_id,
            'task_status': task_status,
            'task_status_label': TASK_STATUS_LABELS.get(task_status, '未知'),
            'task_progress': task_progress(task),
            'error': case.error_detail or None, 'runs': [],
        })
    return {
        'id': execution.pk, 'status': execution.status,
        'created_at': execution.created_at, 'completed_at': execution.completed_at,
        'total_cases': len(cases), 'total_runs': failed_runs,
        **counts,
        'run_counts': {
            'pending': 0, 'running': 0, 'success': 0,
            'failed': failed_runs, 'cancelled': 0,
        },
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
    rows, result_runs = [], 0
    layout = _snapshot_layout(execution) or []
    expected_by_coordinate = dict(layout)
    synthetic_failed_runs = 0
    for case in cases:
        run_rows = [{
            'key': result.candidate_key,
            'label': labels.get(result.candidate_key, result.candidate_key),
            'status': 'success', 'dps': result.dps,
        } for result in case._persisted_results]
        result_runs += len(run_rows)
        if case.task_id is None and case.status == SimcBenchmarkExecution.STATUS_FAILED:
            synthetic_failed_runs += len(expected_by_coordinate.get(
                (case.spec_key, case.scenario_key, case.profile_key),
            ) or [])
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
            'error': case.error_detail or None, 'runs': run_rows,
        })
    names = ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
    counts = {name: 0 for name in names}
    for case in cases:
        counts[case.status if case.status in counts else 'failed'] += 1
    run_counts = {name: 0 for name in ('pending', 'running', 'success', 'failed', 'cancelled')}
    run_counts['success'] = result_runs
    run_counts['failed'] = synthetic_failed_runs
    total_runs = result_runs + synthetic_failed_runs
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
        snapshot = current.config_snapshot if isinstance(current.config_snapshot, dict) else {}
        is_failed_case_retry = (
            snapshot.get('execution_mode') == 'supplement'
            and type(snapshot.get('source_execution_id')) is int
        )
        valid = current.completed_at is not None
        if is_failed_case_retry:
            valid = valid and not current.result_hash and current.results_finalized_at is None
        else:
            valid = (
                valid
                and current.results_finalized_at is not None
                and isinstance(current.result_hash, str)
                and len(current.result_hash) == 64
            )
        for case in summary['cases']:
            coordinate = (case['spec_key'], case['scenario_key'], case['profile_key'])
            keys = [run['key'] for run in case['runs']]
            valid = (
                valid
                and case['status'] == SimcBenchmarkExecution.STATUS_SUCCESS
                and coordinate not in seen
                and expected.get(coordinate) == keys
                and all(
                    isinstance(run['dps'], (int, float))
                    and not isinstance(run['dps'], bool)
                    and math.isfinite(run['dps'])
                    and run['dps'] > 0
                    for run in case['runs']
                )
            )
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
        if is_failed_case_retry:
            valid = valid and bool(seen) and seen.issubset(expected)
        else:
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


def _collect_success_results(execution, live, *, require_complete_execution):
    """Extract immutable rows for complete successful Cases from a frozen snapshot.

    A Case is independently publishable to the Panel's aggregate once its frozen
    candidate collection is complete. ``require_complete_execution`` remains true
    only when creating the Execution-level publication seal.
    """
    layout = _snapshot_layout(execution)
    if layout is None or (require_complete_execution and live['status'] != 'success'):
        return None
    live_by_coordinate = {}
    for case in live['cases']:
        coordinate = (case['spec_key'], case['scenario_key'], case['profile_key'])
        if coordinate in live_by_coordinate:
            return None
        live_by_coordinate[coordinate] = case
    expected = {coordinate: keys for coordinate, keys in layout}
    live_coordinates = set(live_by_coordinate)
    expected_coordinates = set(expected)
    if require_complete_execution:
        if live_coordinates != expected_coordinates:
            return None
    elif not live_coordinates.issubset(expected_coordinates):
        return None

    rows = []
    for coordinate, candidate_keys in layout:
        if coordinate not in live_by_coordinate:
            if require_complete_execution:
                return None
            continue
        case = live_by_coordinate[coordinate]
        if type(case.get('_case_id')) is not int:
            return None
        runs_by_key = {run['key']: run for run in case['runs']}
        if len(runs_by_key) != len(case['runs']) or not set(runs_by_key).issubset(candidate_keys):
            return None
        if require_complete_execution and (
                case['status'] != 'success' or list(runs_by_key) != candidate_keys):
            return None
        for candidate_key in candidate_keys:
            run = runs_by_key.get(candidate_key)
            if run is None or run.get('status') != 'success':
                if require_complete_execution:
                    return None
                continue
            dps = run.get('_raw_dps')
            if (isinstance(dps, bool) or not isinstance(dps, (int, float))
                    or not math.isfinite(dps) or dps <= 0):
                if require_complete_execution:
                    return None
                continue
            rows.append({
                'case_id': case['_case_id'],
                'spec_key': coordinate[0], 'scenario_key': coordinate[1],
                'profile_key': coordinate[2],
                'spec_label': case['labels']['spec'],
                'scenario_label': case['labels']['scenario'],
                'profile_label': case['labels']['profile'],
                'status': case['status'], 'candidate_key': candidate_key,
                'dps': float(dps),
            })
    return rows


def backfill_completed_case_results(execution):
    """Restore missing immutable rows for historical successful Cases only.

    This deliberately does not alter Task/Run or Execution lifecycle fields.  It is
    for legacy terminal Executions created before incremental Case result sealing.
    """
    with transaction.atomic():
        locked = SimcBenchmarkExecution.objects.select_for_update().get(pk=execution.pk)
        if locked.completed_at is None:
            _validation_error('只能回填已完成的 Execution', 'execution')
        layout = _snapshot_layout(locked)
        if layout is None:
            _validation_error('Execution 缺少有效冻结快照', 'execution')
        expected = dict(layout)
        cases = list(SimcBenchmarkCase.objects.filter(
            execution_id=locked.pk,
        ).select_related('task').order_by('id'))
        rows = []
        existing = {
            (row.case_id, row.candidate_key): row.dps
            for row in SimcBenchmarkResult.objects.filter(case__execution_id=locked.pk)
        }
        for case in cases:
            coordinate = (case.spec_key, case.scenario_key, case.profile_key)
            candidate_keys = expected.get(coordinate)
            task = case.task
            runs_by_key = _runs_through_source_chain(task) if task is not None else {}
            ordered_runs = [runs_by_key[key] for key in candidate_keys if key in runs_by_key] \
                if candidate_keys is not None else []
            # A Result can be restored for every independently verified completed
            # candidate. The Case lifecycle remains unchanged unless reconcile owns it.
            if candidate_keys is None:
                continue
            case_rows = []
            for run in ordered_runs:
                summary = run.result_summary if isinstance(run.result_summary, dict) else {}
                dps = summary.get('dps')
                if run.status != 'completed':
                    continue
                if (isinstance(dps, bool) or not isinstance(dps, (int, float))
                        or not math.isfinite(dps) or dps <= 0):
                    continue
                expected_dps = float(dps)
                current_dps = existing.get((case.pk, run.candidate_key))
                if current_dps is not None:
                    if current_dps != expected_dps:
                        _validation_error('已有结果与冻结 Run DPS 不一致', 'execution')
                    continue
                case_rows.append(SimcBenchmarkResult(
                    case_id=case.pk, candidate_key=run.candidate_key, dps=expected_dps,
                ))
            if case_rows:
                rows.extend(case_rows)
        if rows:
            SimcBenchmarkResult.objects.bulk_create(rows)
        return len(rows)


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
        snapshot = locked.config_snapshot if isinstance(locked.config_snapshot, dict) else {}
        if (snapshot.get('execution_mode') == 'supplement'
                and snapshot.get('case_count') == 0
                and not locked.cases.exists()):
            # Older no-op supplements may predate immediate completion in
            # create_execution(). Close them without replacing the last
            # published Execution with an empty result surface.
            locked.status = SimcBenchmarkExecution.STATUS_SUCCESS
            locked.completed_at = timezone.now()
            locked.save(update_fields=['status', 'completed_at'])
            if panel.active_execution_id == locked.pk:
                panel.active_execution = None
                panel.save(update_fields=['active_execution'])
            return locked
        live = _summarize_live_execution(locked)
        live_status = live['status']
        case_state = {
            row['_case_id']: (
                row['status'],
                (row.get('error') or '')
                if row['status'] in ('partial', 'failed', 'cancelled') else '',
            )
            for row in live['cases'] if type(row.get('_case_id')) is int
        }
        persisted_cases = SimcBenchmarkCase.objects.filter(
            execution_id=locked.pk, pk__in=case_state,
        ).in_bulk()
        changed_cases = []
        for case_id, (status, error_detail) in case_state.items():
            case = persisted_cases.get(case_id)
            if case is None:
                continue
            if case.status != status or case.error_detail != error_detail:
                case.status = status
                case.error_detail = error_detail
                changed_cases.append(case)
        if changed_cases:
            SimcBenchmarkCase.objects.bulk_update(
                changed_cases, ['status', 'error_detail'],
            )
        if live_status in ('pending', 'running'):
            partial_rows = _collect_success_results(
                locked, live, require_complete_execution=False,
            )
            if partial_rows is not None:
                successful_case_ids = {row['case_id'] for row in partial_rows}
                SimcBenchmarkResult.objects.filter(
                    case__execution_id=locked.pk,
                ).exclude(case_id__in=successful_case_ids).delete()
                SimcBenchmarkResult.objects.filter(
                    case_id__in=successful_case_ids,
                ).delete()
                SimcBenchmarkResult.objects.bulk_create([
                    SimcBenchmarkResult(
                        case_id=row['case_id'], candidate_key=row['candidate_key'],
                        dps=row['dps'],
                    )
                    for row in partial_rows
                ])
            if locked.status != live_status:
                locked.status = live_status
                locked.save(update_fields=['status'])
            return locked

        now = timezone.now()
        partial_rows = _collect_success_results(
            locked, live, require_complete_execution=False,
        )
        if partial_rows is None:
            SimcBenchmarkResult.objects.filter(case__execution_id=locked.pk).delete()
        else:
            successful_case_ids = {row['case_id'] for row in partial_rows}
            SimcBenchmarkResult.objects.filter(
                case__execution_id=locked.pk,
            ).exclude(case_id__in=successful_case_ids).delete()
            SimcBenchmarkResult.objects.filter(
                case_id__in=successful_case_ids,
            ).delete()
            SimcBenchmarkResult.objects.bulk_create([
                SimcBenchmarkResult(
                    case_id=row['case_id'], candidate_key=row['candidate_key'],
                    dps=row['dps'],
                )
                for row in partial_rows
            ])
        if live_status != 'success':
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

        is_failed_case_retry = (
            snapshot.get('execution_mode') == 'supplement'
            and type(snapshot.get('source_execution_id')) is int
        )
        if is_failed_case_retry and partial_rows is not None:
            # A failed-case retry intentionally materializes only its failed Cases.
            # The partial collector has verified every materialized Case/candidate,
            # and those immutable rows can enter the incremental projection.  This
            # Execution must not claim the full-snapshot publication seal, even when
            # every coordinate in the source snapshot happened to fail.
            locked.status = SimcBenchmarkExecution.STATUS_SUCCESS
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

        rows = _collect_success_results(locked, live, require_complete_execution=True)
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

        SimcBenchmarkResult.objects.filter(case__execution_id=locked.pk).delete()
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
        if (snapshot.get('execution_mode') == 'full'
                and not newer_already_published
                and panel.aggregate_baseline_execution_id != locked.pk):
            # A full rerun becomes the aggregate denominator only after every
            # frozen Case has been verified and sealed successfully. Until then,
            # the previous successful full surface remains authoritative.
            panel.aggregate_baseline_execution = locked
            panel_fields.append('aggregate_baseline_execution')
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
    display_metadata = execution.display_metadata if isinstance(execution.display_metadata, dict) else {}
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
            display = display_metadata.get(run['key'])
            display = display if isinstance(display, dict) else {}
            seal_rows.append({
                'spec_key': row['spec_key'], 'scenario_key': row['scenario_key'],
                'profile_key': row['profile_key'],
                'spec_label': row['labels']['spec'],
                'scenario_label': row['labels']['scenario'],
                'profile_label': row['labels']['profile'],
                'status': row['status'], 'candidate_key': run['key'],
                'dps': run['dps'],
            })
            candidate_row = {
                'key': run['key'],
                'label': str(display.get('label') or candidate['label']),
                'type': candidate['candidate_type'],
                'icon_url': str(display.get('icon_url') or candidate['icon_url']),
                'source_label': candidate['source_label'],
                'status': run['status'], 'dps': run['dps'],
            }
            effect = str(display.get('effect') or candidate.get('effect') or '')
            if effect:
                candidate_row['effect'] = effect
            candidates.append(candidate_row)
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
