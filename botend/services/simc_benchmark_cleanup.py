"""Reachability planner for pruning superseded SimC Benchmark history.

The planner is intentionally read-only.  Destructive management commands must
rebuild and fingerprint the plan immediately before applying it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone as datetime_timezone
import hashlib
import json
import re

from django.utils import timezone

from botend.models import (
    SimcBenchmarkCase,
    SimcBenchmarkExecution,
    SimcBenchmarkPanel,
    SimcBenchmarkResult,
    SimcTask,
    SimcTaskArtifact,
    SimulationRun,
)
from botend.services.simc_benchmark_config import build_execution_plan
from botend.services.simc_benchmark_execution import (
    _candidate_input_identity,
    _coordinate_input_identity,
    _reusable_candidate_tasks_by_coordinate,
)


IN_FLIGHT_EXECUTION_STATUSES = (
    SimcBenchmarkExecution.STATUS_PENDING,
    SimcBenchmarkExecution.STATUS_RUNNING,
)
IN_FLIGHT_TASK_STATUSES = (0, 1)


@dataclass(frozen=True)
class BenchmarkCleanupPlan:
    protected_task_ids: frozenset[int]
    deletable_task_ids: frozenset[int]
    deletable_case_ids: frozenset[int]
    deletable_execution_ids: frozenset[int]
    artifact_ids: frozenset[int]
    run_ids: frozenset[int]
    result_ids: frozenset[int]
    object_keys: tuple[str, ...]
    state_rows: tuple[tuple[str, tuple[str, ...], tuple[tuple, ...]], ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    report_bytes: int = 0

    @property
    def fingerprint(self) -> str:
        payload = {
            'deletable_task_ids': sorted(self.deletable_task_ids),
            'deletable_case_ids': sorted(self.deletable_case_ids),
            'deletable_execution_ids': sorted(self.deletable_execution_ids),
            'artifact_ids': sorted(self.artifact_ids),
            'run_ids': sorted(self.run_ids),
            'result_ids': sorted(self.result_ids),
            'object_keys': list(self.object_keys),
            'state_rows': self.state_rows,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(encoded).hexdigest()

    def summary(self) -> dict:
        return {
            'protected_tasks': len(self.protected_task_ids),
            'deletable_tasks': len(self.deletable_task_ids),
            'deletable_cases': len(self.deletable_case_ids),
            'deletable_executions': len(self.deletable_execution_ids),
            'deletable_runs': len(self.run_ids),
            'deletable_artifacts': len(self.artifact_ids),
            'deletable_results': len(self.result_ids),
            'report_bytes': self.report_bytes,
            'object_keys': len(self.object_keys),
            'warnings': list(self.warnings),
            'fingerprint': self.fingerprint,
        }

    def state_manifest(self) -> dict:
        return {
            name: {
                'fields': list(fields),
                'rows': [list(row) for row in rows],
            }
            for name, fields, rows in self.state_rows
        }


def _stable_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _state_rows(queryset, *fields):
    return tuple(
        tuple(_stable_value(value) for value in row)
        for row in queryset.order_by('id').values_list(*fields)
    )


def _explicit_execution_ids() -> set[int]:
    roots: set[int] = set()
    for active_id, published_id, baseline_id in SimcBenchmarkPanel.objects.values_list(
        'active_execution_id', 'published_execution_id', 'aggregate_baseline_execution_id',
    ):
        roots.update(value for value in (active_id, published_id, baseline_id) if value)
    return roots


def _current_projection_task_ids(panel, warnings: list[str]) -> set[int]:
    try:
        plan = build_execution_plan(panel, validate_for_execution=False, lock=False)
    except Exception as exc:
        warnings.append(f'panel={panel.id}: plan unavailable ({type(exc).__name__}: {exc})')
        # Conservative fallback: an invalid live configuration must not turn all
        # of that Panel's history into garbage.
        return set(
            SimcBenchmarkCase.objects.filter(execution__panel=panel, task_id__isnull=False)
            .values_list('task_id', flat=True)
        )

    reusable = _reusable_candidate_tasks_by_coordinate(panel)
    selected: set[int] = set()
    for coordinate in plan.get('cases') or ():
        matches = reusable.get(_coordinate_input_identity(coordinate), {})
        for candidate in coordinate.get('candidates') or ():
            match = matches.get(_candidate_input_identity(candidate))
            if match:
                selected.add(match['task'].id)
    return selected


def _source_ancestor_closure(seed_ids: set[int], benchmark_task_ids: set[int]) -> set[int]:
    source_by_task = dict(SimcTask.objects.values_list('id', 'source_task_id'))
    protected = set(seed_ids)
    frontier = list(seed_ids)
    while frontier:
        task_id = frontier.pop()
        source_id = source_by_task.get(task_id)
        if source_id and source_id in benchmark_task_ids and source_id not in protected:
            protected.add(source_id)
            frontier.append(source_id)
    return protected


def _artifact_object_key(path: str) -> str:
    value = str(path or '').strip().lstrip('/')
    if value.startswith('static/'):  # tolerate legacy DB rows with static/ prefix
        value = value[7:]
    if value.startswith('simc_agent_results/'):
        return value if AGENT_OBJECT_RE.fullmatch(value) else ''
    if value.startswith('simc_results/'):
        filename = value[len('simc_results/'):]
        allowed = (ROOT_RUN_RE, ROOT_TASK_RE, ROOT_ATTRIBUTE_RE, ROOT_UUID_RE)
        return filename if any(regex.fullmatch(filename) for regex in allowed) else ''
    return ''


def build_cleanup_plan() -> BenchmarkCleanupPlan:
    """Build the minimum safe deletion set from current durable roots."""
    warnings: list[str] = []
    benchmark_case_rows = list(
        SimcBenchmarkCase.objects.values_list('id', 'execution_id', 'task_id')
    )
    benchmark_task_ids = {task_id for _, _, task_id in benchmark_case_rows if task_id}

    explicit_execution_ids = _explicit_execution_ids()
    in_flight_execution_ids = set(
        SimcBenchmarkExecution.objects.filter(status__in=IN_FLIGHT_EXECUTION_STATUSES)
        .values_list('id', flat=True)
    )
    protected_task_ids = set(
        SimcBenchmarkCase.objects.filter(
            execution_id__in=explicit_execution_ids | in_flight_execution_ids,
            task_id__isnull=False,
        ).values_list('task_id', flat=True)
    )
    protected_task_ids.update(
        SimcTask.objects.filter(id__in=benchmark_task_ids, current_status__in=IN_FLIGHT_TASK_STATUSES)
        .values_list('id', flat=True)
    )
    protected_task_ids.update(
        SimcTask.objects.filter(id__in=benchmark_task_ids, favorite_relations__isnull=False)
        .values_list('id', flat=True)
    )

    for panel in SimcBenchmarkPanel.objects.order_by('id').iterator():
        protected_task_ids.update(_current_projection_task_ids(panel, warnings))

    # A non-Benchmark rerun may still point to an old Benchmark task.
    protected_task_ids.update(
        SimcTask.objects.exclude(id__in=benchmark_task_ids)
        .filter(source_task_id__in=benchmark_task_ids)
        .values_list('source_task_id', flat=True)
    )
    protected_task_ids = _source_ancestor_closure(protected_task_ids, benchmark_task_ids)
    deletable_task_ids = benchmark_task_ids - protected_task_ids

    durable_execution_ids = explicit_execution_ids | in_flight_execution_ids
    deletable_case_ids = {
        case_id for case_id, execution_id, task_id in benchmark_case_rows
        if task_id in deletable_task_ids or (task_id is None and execution_id not in durable_execution_ids)
    }

    kept_execution_ids = {
        execution_id for case_id, execution_id, _ in benchmark_case_rows
        if case_id not in deletable_case_ids
    }
    all_execution_ids = set(SimcBenchmarkExecution.objects.values_list('id', flat=True))
    deletable_execution_ids = all_execution_ids - kept_execution_ids - durable_execution_ids

    artifacts = SimcTaskArtifact.objects.filter(task_id__in=deletable_task_ids)
    artifact_rows = list(artifacts.values_list('id', 'file_path', 'file_size'))
    candidate_object_keys = {
        key for _, path, _ in artifact_rows
        if (key := _artifact_object_key(path))
    }
    protected_object_keys = {
        key
        for path in SimcTaskArtifact.objects.exclude(task_id__in=deletable_task_ids)
        .values_list('file_path', flat=True)
        if (key := _artifact_object_key(path))
    }
    object_keys = tuple(sorted(candidate_object_keys - protected_object_keys))
    run_ids = frozenset(
        SimulationRun.objects.filter(task_id__in=deletable_task_ids)
        .values_list('id', flat=True)
    )
    results = SimcBenchmarkResult.objects.filter(case_id__in=deletable_case_ids)
    result_ids = frozenset(results.values_list('id', flat=True))
    report_bytes = sum(
        int(size or 0) for _, path, size in artifact_rows
        if _artifact_object_key(path) in object_keys
    )
    artifact_ids = frozenset(row[0] for row in artifact_rows)
    state_rows = (
        (
            'executions',
            ('id', 'panel_id', 'status', 'config_hash', 'result_hash', 'completed_at'),
            _state_rows(
                SimcBenchmarkExecution.objects.filter(id__in=deletable_execution_ids),
                'id', 'panel_id', 'status', 'config_hash', 'result_hash', 'completed_at',
            ),
        ),
        (
            'cases',
            ('id', 'execution_id', 'task_id', 'status', 'coordinate_hash'),
            _state_rows(
                SimcBenchmarkCase.objects.filter(id__in=deletable_case_ids),
                'id', 'execution_id', 'task_id', 'status', 'coordinate_hash',
            ),
        ),
        (
            'tasks',
            ('id', 'current_status', 'source_task_id', 'is_active', 'modified_time'),
            _state_rows(
                SimcTask.objects.filter(id__in=deletable_task_ids),
                'id', 'current_status', 'source_task_id', 'is_active', 'modified_time',
            ),
        ),
        (
            'runs',
            ('id', 'task_id', 'status', 'input_hash', 'completion_id', 'lease_expires_at'),
            _state_rows(
                SimulationRun.objects.filter(id__in=run_ids),
                'id', 'task_id', 'status', 'input_hash', 'completion_id', 'lease_expires_at',
            ),
        ),
        (
            'artifacts',
            ('id', 'task_id', 'run_id', 'file_path', 'file_size', 'content_hash'),
            _state_rows(
                SimcTaskArtifact.objects.filter(id__in=artifact_ids),
                'id', 'task_id', 'run_id', 'file_path', 'file_size', 'content_hash',
            ),
        ),
        (
            'results',
            ('id', 'case_id', 'candidate_key', 'dps'),
            _state_rows(results, 'id', 'case_id', 'candidate_key', 'dps'),
        ),
    )

    return BenchmarkCleanupPlan(
        protected_task_ids=frozenset(protected_task_ids),
        deletable_task_ids=frozenset(deletable_task_ids),
        deletable_case_ids=frozenset(deletable_case_ids),
        deletable_execution_ids=frozenset(deletable_execution_ids),
        artifact_ids=artifact_ids,
        run_ids=run_ids,
        result_ids=result_ids,
        object_keys=object_keys,
        state_rows=state_rows,
        warnings=tuple(warnings),
        report_bytes=report_bytes,
    )


AGENT_OBJECT_RE = re.compile(
    r'^simc_agent_results/simc_task_(?P<task>[1-9][0-9]*)_run_(?P<run>[1-9][0-9]*)\.html$'
)
ROOT_RUN_RE = re.compile(r'^simc_task_(?P<task>[1-9][0-9]*)_run_(?P<run>[1-9][0-9]*)\.html$')
ROOT_TASK_RE = re.compile(r'^simc_task_(?P<task>[1-9][0-9]*)\.html$')
ROOT_ATTRIBUTE_RE = re.compile(
    r'^(?P<task>[1-9][0-9]*)_gear_(?:crit|haste|mastery|versatility)_[0-9]+'
    r'_gear_(?:crit|haste|mastery|versatility)_[0-9]+\.html$'
)
ROOT_UUID_RE = re.compile(r'^[0-9a-f]{32}(?:_run_(?P<run>[1-9][0-9]*))?\.html$')


@dataclass(frozen=True)
class OrphanReportObject:
    key: str
    size: int
    last_modified: str
    reason: str


def _aware_datetime(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=datetime_timezone.utc)
    return value


def _list_oss_objects(*, prefix='', delimiter=None):
    from botend.services.simc_agent_oss import _client

    oss, client, bucket = _client()
    token = None
    while True:
        result = client.list_objects_v2(oss.ListObjectsV2Request(
            bucket=bucket,
            prefix=prefix or None,
            delimiter=delimiter,
            continuation_token=token,
            max_keys=1000,
        ))
        yield from (getattr(result, 'contents', None) or ())
        if not getattr(result, 'is_truncated', False):
            break
        token = getattr(result, 'next_continuation_token', None)
        if not token:
            raise RuntimeError('OSS listing was truncated without a continuation token')


def list_oss_orphan_reports(*, minimum_age_days: int = 7) -> tuple[OrphanReportObject, ...]:
    """List old report objects with neither an Artifact nor an in-flight owner."""
    if minimum_age_days < 1:
        raise ValueError('minimum_age_days must be at least 1')
    cutoff = timezone.now() - timezone.timedelta(days=minimum_age_days)
    referenced = {
        key for path in SimcTaskArtifact.objects.values_list('file_path', flat=True)
        if (key := _artifact_object_key(path))
    }
    task_status = dict(SimcTask.objects.values_list('id', 'current_status'))
    run_rows = {
        run_id: (task_id, status)
        for run_id, task_id, status in SimulationRun.objects.values_list('id', 'task_id', 'status')
    }
    found: list[OrphanReportObject] = []

    def add_if_old(row, reason):
        modified = _aware_datetime(getattr(row, 'last_modified', None))
        if modified is None or modified >= cutoff:
            return
        found.append(OrphanReportObject(
            key=str(row.key),
            size=int(getattr(row, 'size', 0) or 0),
            last_modified=modified.isoformat(),
            reason=reason,
        ))

    for row in _list_oss_objects(prefix='simc_agent_results/'):
        key = str(row.key)
        if key in referenced:
            continue
        match = AGENT_OBJECT_RE.fullmatch(key)
        if not match:
            continue
        task_id, run_id = int(match.group('task')), int(match.group('run'))
        run = run_rows.get(run_id)
        if run and run[0] == task_id and run[1] in {'pending', 'running'}:
            continue
        if task_status.get(task_id) in IN_FLIGHT_TASK_STATUSES:
            continue
        reason = 'terminal_unregistered_run' if run and run[0] == task_id else 'missing_task_or_run'
        add_if_old(row, reason)

    for row in _list_oss_objects(delimiter='/'):
        key = str(row.key)
        if not key.endswith('.html') or key in referenced:
            continue
        task_id = None
        run_id = None
        reason = ''
        for regex in (ROOT_RUN_RE, ROOT_TASK_RE, ROOT_ATTRIBUTE_RE):
            match = regex.fullmatch(key)
            if match:
                task_id = int(match.group('task'))
                run_id = int(match.groupdict().get('run') or 0) or None
                reason = 'unregistered_legacy_task_report'
                break
        if not reason:
            match = ROOT_UUID_RE.fullmatch(key)
            if not match:
                continue
            run_id = int(match.group('run') or 0) or None
            reason = 'unregistered_legacy_uuid_report'
        if task_id and task_status.get(task_id) in IN_FLIGHT_TASK_STATUSES:
            continue
        if run_id and run_id in run_rows and run_rows[run_id][1] in {'pending', 'running'}:
            continue
        add_if_old(row, reason)

    return tuple(sorted(found, key=lambda row: row.key))
