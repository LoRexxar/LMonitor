import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.models import (
    SimcBenchmarkCandidate,
    SimcBenchmarkCase,
    SimcBenchmarkExecution,
    SimcBenchmarkPanel,
    SimcBenchmarkProfile,
    SimcBenchmarkPurgeTask,
    SimcBenchmarkResult,
    SimcBenchmarkScenario,
    SimcBenchmarkSpec,
    SimcTask,
    SimcTaskArtifact,
    SimcTaskFavorite,
    SimulationRun,
)
from botend.services.simc_benchmark_cleanup import _artifact_object_key
from utils.log import logger


class PurgeBlocked(Exception):
    def __init__(self, message, data=None):
        super().__init__(message)
        self.data = data or {}


class PurgeConflict(Exception):
    pass


class PurgeValidation(Exception):
    pass


ACTIVE_PURGE_STATUSES = (
    SimcBenchmarkPurgeTask.STATUS_PENDING,
    SimcBenchmarkPurgeTask.STATUS_RUNNING,
    SimcBenchmarkPurgeTask.STATUS_CLEANUP_PENDING,
    SimcBenchmarkPurgeTask.STATUS_CLEANING,
    SimcBenchmarkPurgeTask.STATUS_RESTORE_PENDING,
    SimcBenchmarkPurgeTask.STATUS_RESTORING,
)
TRANSITIONAL_PURGE_STATUSES = (
    SimcBenchmarkPurgeTask.STATUS_RUNNING,
    SimcBenchmarkPurgeTask.STATUS_CLEANING,
    SimcBenchmarkPurgeTask.STATUS_RESTORING,
)
CLAIM_STALE_AFTER = timedelta(hours=6)
EXECUTION_LOCK_PREFIX = 'lmonitor:simc-benchmark-purge:'
QUARANTINE_PERSIST_BATCH_SIZE = 100


@dataclass(frozen=True)
class PanelPurgePlan:
    panel_id: int
    panel_name: str
    task_ids: tuple
    object_keys: tuple
    counts: dict
    fingerprint: str
    panel_state: dict

    def public_data(self):
        return {
            'panel_id': self.panel_id,
            'panel_name': self.panel_name,
            'counts': self.counts,
            'fingerprint': self.fingerprint,
        }

    def stored_data(self):
        return {
            **self.public_data(),
            'task_ids': list(self.task_ids),
            'object_keys': list(self.object_keys),
            'panel_state': self.panel_state,
        }


def _all_rows(queryset):
    """Canonical snapshot of every concrete database field in id order."""
    fields = [field.attname for field in queryset.model._meta.concrete_fields]
    return [
        dict(zip(fields, row))
        for row in queryset.order_by('id').values_list(*fields)
    ]


def _artifact_reference(path):
    """Return (deletable OSS key, stable controlled-path identity)."""
    key = _artifact_object_key(path)
    if key:
        return key, f'oss:{key}'
    value = str(path or '').strip().lstrip('/')
    if value.startswith('static/'):
        value = value[7:]
    if (
        value.endswith('.html')
        and '..' not in value
        and value.startswith(('simc_agent_results/', 'simc_results/'))
    ):
        return '', f'path:{value}'
    return '', ''


def _panel_task_ids(panel, direct_task_ids):
    """Resolve the comparison-task source graph owned by this Panel creator."""
    owned = {int(task_id) for task_id in direct_task_ids if task_id is not None}
    if not owned:
        return ()
    rows = list(
        SimcTask.objects.filter(
            user_id=panel.created_by_id,
            mode='comparison',
        ).values_list('id', 'source_task_id')
    )
    eligible_ids = {task_id for task_id, _source_task_id in rows}
    invalid_direct = sorted(owned - eligible_ids)
    if invalid_direct:
        raise PurgeBlocked(
            'Panel 引用了不属于其创建者的非 Benchmark Task，拒绝删除',
            {'invalid_direct_task_ids': invalid_direct},
        )
    changed = True
    while changed:
        changed = False
        for task_id, source_task_id in rows:
            if (
                task_id in owned
                and source_task_id in eligible_ids
                and source_task_id not in owned
            ):
                owned.add(source_task_id)
                changed = True
            elif source_task_id in owned and task_id not in owned:
                owned.add(task_id)
                changed = True
    task_ids = tuple(sorted(owned))
    foreign_cases = list(
        SimcBenchmarkCase.objects.filter(task_id__in=task_ids)
        .exclude(execution__panel_id=panel.id)
        .order_by('id').values_list('id', flat=True)
    )
    if foreign_cases:
        raise PurgeBlocked(
            'Panel 的 Benchmark Task 同时被其他 Panel 引用，拒绝删除',
            {'foreign_case_ids': foreign_cases},
        )
    return task_ids


def build_panel_purge_plan(panel_id):
    panel = SimcBenchmarkPanel.objects.filter(pk=panel_id).first()
    if panel is None:
        raise SimcBenchmarkPanel.DoesNotExist

    executions_qs = SimcBenchmarkExecution.objects.filter(panel_id=panel_id)
    execution_ids = list(executions_qs.order_by('id').values_list('id', flat=True))
    cases_qs = SimcBenchmarkCase.objects.filter(execution_id__in=execution_ids)
    case_ids = list(cases_qs.order_by('id').values_list('id', flat=True))
    direct_task_ids = set(cases_qs.exclude(task_id=None).values_list('task_id', flat=True))
    task_ids = _panel_task_ids(panel, direct_task_ids)

    active_task_ids = list(
        SimcTask.objects.filter(id__in=task_ids, current_status__in=(0, 1))
        .order_by('id').values_list('id', flat=True)
    )
    active_execution_ids = list(
        executions_qs.filter(status__in=(
            SimcBenchmarkExecution.STATUS_PENDING,
            SimcBenchmarkExecution.STATUS_RUNNING,
        )).order_by('id').values_list('id', flat=True)
    )
    active_run_ids = list(
        SimulationRun.objects.filter(
            task_id__in=task_ids,
            status__in=('pending', 'running'),
        ).order_by('id').values_list('id', flat=True)
    )
    if active_task_ids or active_execution_ids or active_run_ids:
        raise PurgeBlocked(
            'Panel 仍有待执行或执行中的 Benchmark 工作，拒绝删除',
            {
                'active_task_ids': active_task_ids,
                'active_execution_ids': active_execution_ids,
                'active_run_ids': active_run_ids,
            },
        )

    artifacts_qs = SimcTaskArtifact.objects.filter(task_id__in=task_ids)
    artifact_rows = _all_rows(artifacts_qs)
    candidate_by_reference = {}
    unsupported_by_reference = {}
    for row in artifact_rows:
        if row['artifact_type'] != 'html_report':
            continue
        key, reference = _artifact_reference(row['file_path'])
        if key:
            candidate_by_reference[reference] = key
        elif reference:
            unsupported_by_reference.setdefault(reference, []).append(row['id'])
        elif row['file_path']:
            unsupported_by_reference.setdefault(f"unsupported:{row['id']}", []).append(row['id'])

    external_references = set()
    if candidate_by_reference or unsupported_by_reference:
        external_paths = SimcTaskArtifact.objects.exclude(
            task_id__in=task_ids,
        ).filter(artifact_type='html_report').values_list('file_path', flat=True)
        for path in external_paths.iterator(chunk_size=2000):
            _key, reference = _artifact_reference(path)
            if reference:
                external_references.add(reference)

    unsupported_artifact_ids = sorted({
        artifact_id
        for reference, artifact_ids in unsupported_by_reference.items()
        if reference not in external_references
        for artifact_id in artifact_ids
    })
    if unsupported_artifact_ids:
        raise PurgeBlocked(
            'Panel 包含不在受控 OSS 报告命名范围内且未被其他 Task 引用的 HTML Artifact，拒绝不完整清理',
            {'unsupported_artifact_ids': unsupported_artifact_ids},
        )

    shared_keys = {
        key for reference, key in candidate_by_reference.items()
        if reference in external_references
    }
    object_keys = tuple(sorted(set(candidate_by_reference.values()) - shared_keys))
    retained_reruns_qs = SimcTask.objects.exclude(id__in=task_ids).filter(
        source_task_id__in=task_ids,
    )

    graph = {
        'schema': 'simc-benchmark-panel-purge/v2',
        'panel': _all_rows(SimcBenchmarkPanel.objects.filter(pk=panel_id)),
        'specs': _all_rows(SimcBenchmarkSpec.objects.filter(panel_id=panel_id)),
        'profiles': _all_rows(SimcBenchmarkProfile.objects.filter(panel_spec__panel_id=panel_id)),
        'scenarios': _all_rows(SimcBenchmarkScenario.objects.filter(panel_id=panel_id)),
        'candidates': _all_rows(SimcBenchmarkCandidate.objects.filter(panel_id=panel_id)),
        'executions': _all_rows(executions_qs),
        'cases': _all_rows(cases_qs),
        'results': _all_rows(SimcBenchmarkResult.objects.filter(case_id__in=case_ids)),
        'tasks': _all_rows(SimcTask.objects.filter(id__in=task_ids)),
        'runs': _all_rows(SimulationRun.objects.filter(task_id__in=task_ids)),
        'artifacts': artifact_rows,
        'favorites': _all_rows(SimcTaskFavorite.objects.filter(task_id__in=task_ids)),
        'retained_reruns': _all_rows(retained_reruns_qs),
        'object_keys': object_keys,
        'shared_object_keys': sorted(shared_keys),
    }
    canonical = json.dumps(
        graph, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str,
    ).encode('utf-8')
    fingerprint = hashlib.sha256(canonical).hexdigest()
    counts = {
        'panels': 1,
        'executions': len(graph['executions']),
        'cases': len(graph['cases']),
        'results': len(graph['results']),
        'tasks': len(graph['tasks']),
        'runs': len(graph['runs']),
        'artifacts': len(graph['artifacts']),
        'favorites': len(graph['favorites']),
        'oss_objects': len(object_keys),
        'retained_reruns_detached': len(graph['retained_reruns']),
    }
    panel_state = {
        'is_active': panel.is_active,
        'schedule_enabled': panel.schedule_enabled,
        'next_run_at': panel.next_run_at.isoformat() if panel.next_run_at else None,
    }
    return PanelPurgePlan(
        panel_id=panel.id,
        panel_name=panel.name,
        task_ids=task_ids,
        object_keys=object_keys,
        counts=counts,
        fingerprint=fingerprint,
        panel_state=panel_state,
    )


def _lock_plan_rows(panel_id, task_ids):
    execution_ids = list(
        SimcBenchmarkExecution.objects.filter(panel_id=panel_id)
        .order_by('id').values_list('id', flat=True)
    )
    case_ids = list(
        SimcBenchmarkCase.objects.filter(execution_id__in=execution_ids)
        .order_by('id').values_list('id', flat=True)
    )
    querysets = (
        SimcBenchmarkPanel.objects.filter(pk=panel_id),
        SimcBenchmarkSpec.objects.filter(panel_id=panel_id),
        SimcBenchmarkProfile.objects.filter(panel_spec__panel_id=panel_id),
        SimcBenchmarkScenario.objects.filter(panel_id=panel_id),
        SimcBenchmarkCandidate.objects.filter(panel_id=panel_id),
        SimcBenchmarkExecution.objects.filter(id__in=execution_ids),
        SimcBenchmarkCase.objects.filter(id__in=case_ids),
        SimcBenchmarkResult.objects.filter(case_id__in=case_ids),
        SimcTask.objects.filter(id__in=task_ids),
        SimulationRun.objects.filter(task_id__in=task_ids),
        SimcTaskArtifact.objects.filter(task_id__in=task_ids),
        SimcTaskFavorite.objects.filter(task_id__in=task_ids),
        SimcTask.objects.exclude(id__in=task_ids).filter(source_task_id__in=task_ids),
    )
    for queryset in querysets:
        list(queryset.select_for_update().order_by('id').values_list('id', flat=True))


def _new_batch_id():
    return f'panel-{timezone.now().strftime("%Y%m%dT%H%M%SZ")}-{uuid.uuid4().hex[:12]}'


def panel_has_active_purge(panel_id):
    return SimcBenchmarkPurgeTask.objects.filter(
        panel_id=panel_id,
        status__in=ACTIVE_PURGE_STATUSES,
    ).exists()


def task_has_active_panel_purge(task_id):
    try:
        wanted = int(task_id)
    except (TypeError, ValueError):
        return False
    plans = SimcBenchmarkPurgeTask.objects.filter(
        status__in=ACTIVE_PURGE_STATUSES,
    ).values_list('plan', flat=True)
    for plan in plans.iterator(chunk_size=100):
        if wanted in {
            int(value) for value in (plan or {}).get('task_ids', ())
            if str(value).isdigit()
        }:
            return True
    return False


def artifact_key_has_active_panel_purge(path):
    key = _artifact_object_key(path)
    if not key:
        return False
    plans = SimcBenchmarkPurgeTask.objects.filter(
        status__in=ACTIVE_PURGE_STATUSES,
    ).values_list('plan', flat=True)
    return any(
        key in set((plan or {}).get('object_keys', ()))
        for plan in plans.iterator(chunk_size=100)
    )


def queue_panel_purge(panel_id, fingerprint, requested_by_id):
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise PurgeValidation('fingerprint 格式无效')
    with transaction.atomic():
        panel = SimcBenchmarkPanel.objects.select_for_update().filter(pk=panel_id).first()
        if panel is None:
            raise SimcBenchmarkPanel.DoesNotExist
        if SimcBenchmarkPurgeTask.objects.select_for_update().filter(
            panel_id=panel_id,
            status__in=ACTIVE_PURGE_STATUSES,
        ).exists():
            raise PurgeConflict('该 Panel 已有清理任务在执行')

        initial = build_panel_purge_plan(panel_id)
        _lock_plan_rows(panel_id, initial.task_ids)
        confirmed = build_panel_purge_plan(panel_id)
        if confirmed.fingerprint != fingerprint:
            raise PurgeConflict('清理范围已变化，请重新预览并确认新的 fingerprint')

        panel.is_active = False
        panel.schedule_enabled = False
        panel.next_run_at = None
        panel.save(update_fields=['is_active', 'schedule_enabled', 'next_run_at', 'updated_at'])
        locked = build_panel_purge_plan(panel_id)
        stored_plan = locked.stored_data()
        stored_plan['panel_state'] = initial.panel_state
        stored_plan['confirmation_fingerprint'] = confirmed.fingerprint
        stored_plan['locked_fingerprint'] = locked.fingerprint
        return SimcBenchmarkPurgeTask.objects.create(
            panel=panel,
            panel_id_snapshot=panel.id,
            panel_name=panel.name,
            requested_by_id=requested_by_id,
            status=SimcBenchmarkPurgeTask.STATUS_PENDING,
            fingerprint=confirmed.fingerprint,
            batch_id=_new_batch_id(),
            plan=stored_plan,
        )


def _oss_command():
    from botend.management.commands.cleanup_simc_benchmark_history import Command
    return Command()


def _execution_lock_name(job_id):
    return f'{EXECUTION_LOCK_PREFIX}{int(job_id)}'


def _try_acquire_execution_lock(job_id):
    """Fence OSS side effects while allowing recovery after a dead DB connection."""
    if connection.vendor != 'mysql':
        return True
    with connection.cursor() as cursor:
        cursor.execute('SELECT GET_LOCK(%s, 0)', [_execution_lock_name(job_id)])
        row = cursor.fetchone()
    return bool(row and row[0] == 1)


def _release_execution_lock(job_id):
    if connection.vendor != 'mysql':
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT RELEASE_LOCK(%s)', [_execution_lock_name(job_id)])
    except Exception:
        logger.exception('[Benchmark purge] failed to release execution lock for job %s', job_id)


def _claim_next_job():
    stale_before = timezone.now() - CLAIM_STALE_AFTER
    locked_job_id = None
    try:
        with transaction.atomic():
            job = SimcBenchmarkPurgeTask.objects.select_for_update().filter(
                Q(status__in=(
                    SimcBenchmarkPurgeTask.STATUS_PENDING,
                    SimcBenchmarkPurgeTask.STATUS_CLEANUP_PENDING,
                    SimcBenchmarkPurgeTask.STATUS_RESTORE_PENDING,
                )) | Q(
                    status__in=TRANSITIONAL_PURGE_STATUSES,
                    claimed_at__lt=stale_before,
                ) | Q(
                    status__in=TRANSITIONAL_PURGE_STATUSES,
                    claimed_at__isnull=True,
                    updated_at__lt=stale_before,
                ),
            ).order_by('created_at', 'id').first()
            if job is None:
                return None
            if not _try_acquire_execution_lock(job.id):
                return None
            locked_job_id = job.id
            previous = job.status
            if previous in (
                SimcBenchmarkPurgeTask.STATUS_CLEANUP_PENDING,
                SimcBenchmarkPurgeTask.STATUS_CLEANING,
            ):
                phase = 'cleanup'
                job.status = SimcBenchmarkPurgeTask.STATUS_CLEANING
            elif previous in (
                SimcBenchmarkPurgeTask.STATUS_RESTORE_PENDING,
                SimcBenchmarkPurgeTask.STATUS_RESTORING,
            ):
                phase = 'restore'
                job.status = SimcBenchmarkPurgeTask.STATUS_RESTORING
            else:
                phase = 'execute'
                job.status = SimcBenchmarkPurgeTask.STATUS_RUNNING
            token = uuid.uuid4().hex
            now = timezone.now()
            job.claim_token = token
            job.claimed_at = now
            if phase == 'execute':
                job.attempts += 1
            if job.started_at is None:
                job.started_at = now
            job.save(update_fields=[
                'status', 'claim_token', 'claimed_at', 'attempts', 'started_at', 'updated_at',
            ])
        return job.id, token, phase
    except Exception:
        if locked_job_id is not None:
            _release_execution_lock(locked_job_id)
        raise


def _persist_quarantine(job_id, token, quarantine_map, expected_status):
    updated = SimcBenchmarkPurgeTask.objects.filter(
        pk=job_id,
        claim_token=token,
        status=expected_status,
    ).update(
        quarantine_map=quarantine_map,
        claimed_at=timezone.now(),
    )
    if updated != 1:
        raise PurgeConflict('后台清理任务所有权已失效')


def _refresh_claim(job_id, token, expected_status):
    updated = SimcBenchmarkPurgeTask.objects.filter(
        pk=job_id,
        claim_token=token,
        status=expected_status,
    ).update(claimed_at=timezone.now())
    if updated != 1:
        raise PurgeConflict('后台清理任务所有权已失效')


def _delete_database(job_id, token):
    with transaction.atomic():
        job = SimcBenchmarkPurgeTask.objects.select_for_update().get(
            pk=job_id,
            claim_token=token,
            status=SimcBenchmarkPurgeTask.STATUS_RUNNING,
        )
        panel = SimcBenchmarkPanel.objects.select_for_update().filter(pk=job.panel_id).first()
        if panel is None:
            raise PurgeConflict('Panel 在数据库清理前已不存在')
        task_ids = tuple(job.plan.get('task_ids') or ())
        _lock_plan_rows(panel.id, task_ids)
        current = build_panel_purge_plan(panel.id)
        if current.fingerprint != job.plan.get('locked_fingerprint'):
            raise PurgeConflict('后台执行前清理范围已变化')

        SimcTaskArtifact.objects.filter(task_id__in=task_ids).delete()
        SimcTaskFavorite.objects.filter(task_id__in=task_ids).delete()
        SimcTask.objects.filter(id__in=task_ids).delete()
        panel.delete()
        updated = SimcBenchmarkPurgeTask.objects.filter(
            pk=job.id,
            claim_token=token,
            status=SimcBenchmarkPurgeTask.STATUS_RUNNING,
        ).update(
            status=SimcBenchmarkPurgeTask.STATUS_CLEANUP_PENDING,
            claim_token='',
            claimed_at=None,
            error_detail='',
        )
        if updated != 1:
            raise PurgeConflict('数据库提交前清理任务所有权已失效')


def _referenced_object_keys(object_keys):
    wanted = set(object_keys)
    if not wanted:
        return set()
    referenced = set()
    paths = SimcTaskArtifact.objects.filter(
        artifact_type='html_report',
    ).values_list('file_path', flat=True)
    for path in paths.iterator(chunk_size=2000):
        key, _reference = _artifact_reference(path)
        if key in wanted:
            referenced.add(key)
            if referenced == wanted:
                break
    return referenced


def _complete_cleanup(job_id, token, command, quarantine_map):
    try:
        referenced = _referenced_object_keys(quarantine_map)
        if referenced:
            command._restore_objects({
                key: descriptor for key, descriptor in quarantine_map.items()
                if key in referenced
            })
            updated = SimcBenchmarkPurgeTask.objects.filter(
                pk=job_id,
                claim_token=token,
                status=SimcBenchmarkPurgeTask.STATUS_CLEANING,
            ).update(
                status=SimcBenchmarkPurgeTask.STATUS_CLEANUP_PENDING,
                claim_token='',
                claimed_at=None,
                error_detail=(
                    '数据库清理后检测到新的 OSS 引用；原对象已恢复，'
                    '隔离副本保留待引用解除后重试'
                ),
            )
            if updated != 1:
                raise PurgeConflict('保留晚引用隔离副本时任务所有权已失效')
            return
        if quarantine_map:
            # Source deletion is deliberately post-commit: concurrent cleanup
            # attempts then converge on the same final OSS state.
            _refresh_claim(job_id, token, SimcBenchmarkPurgeTask.STATUS_CLEANING)
            command._delete_objects(quarantine_map)
            # A disconnected Worker may lose its advisory lock while the OSS
            # request is still in flight. Never let that old owner destroy the
            # recovery copy after a new owner has restored the source object.
            _refresh_claim(job_id, token, SimcBenchmarkPurgeTask.STATUS_CLEANING)
        command._purge_quarantine_objects(quarantine_map)
    except Exception as exc:
        SimcBenchmarkPurgeTask.objects.filter(
            pk=job_id,
            claim_token=token,
            status=SimcBenchmarkPurgeTask.STATUS_CLEANING,
        ).update(
            status=SimcBenchmarkPurgeTask.STATUS_CLEANUP_PENDING,
            claim_token='',
            claimed_at=None,
            error_detail=f'数据库已清理，OSS 隔离副本待重试: {exc}',
        )
        logger.exception('[Benchmark purge] quarantine cleanup pending for job %s', job_id)
        return
    SimcBenchmarkPurgeTask.objects.filter(
        pk=job_id,
        claim_token=token,
        status=SimcBenchmarkPurgeTask.STATUS_CLEANING,
    ).update(
        status=SimcBenchmarkPurgeTask.STATUS_SUCCEEDED,
        claim_token='',
        claimed_at=None,
        quarantine_map={},
        error_detail='',
        completed_at=timezone.now(),
    )


def _restore_panel_and_fail(job_id, token, command, quarantine_map, root_error):
    try:
        if quarantine_map:
            command._restore_objects(quarantine_map)
            command._purge_quarantine_objects(quarantine_map)
    except Exception as exc:
        SimcBenchmarkPurgeTask.objects.filter(
            pk=job_id,
            claim_token=token,
            status=SimcBenchmarkPurgeTask.STATUS_RESTORING,
        ).update(
            status=SimcBenchmarkPurgeTask.STATUS_RESTORE_PENDING,
            claim_token='',
            claimed_at=None,
            error_detail=f'{root_error}; OSS 恢复或隔离副本清理待重试: {exc}',
        )
        logger.exception('[Benchmark purge] restore pending for job %s', job_id)
        return

    with transaction.atomic():
        job = SimcBenchmarkPurgeTask.objects.select_for_update().get(
            pk=job_id,
            claim_token=token,
            status=SimcBenchmarkPurgeTask.STATUS_RESTORING,
        )
        panel = SimcBenchmarkPanel.objects.select_for_update().filter(pk=job.panel_id).first()
        if panel is not None:
            state = job.plan.get('panel_state') or {}
            panel.is_active = bool(state.get('is_active'))
            panel.schedule_enabled = bool(state.get('schedule_enabled'))
            raw_next = state.get('next_run_at')
            panel.next_run_at = parse_datetime(raw_next) if raw_next else None
            panel.save(update_fields=[
                'is_active', 'schedule_enabled', 'next_run_at', 'updated_at',
            ])
        job.status = SimcBenchmarkPurgeTask.STATUS_FAILED
        job.claim_token = ''
        job.claimed_at = None
        job.error_detail = root_error
        job.completed_at = timezone.now()
        job.quarantine_map = {}
        job.save(update_fields=[
            'status', 'claim_token', 'claimed_at', 'error_detail', 'completed_at',
            'quarantine_map', 'updated_at',
        ])


def _enter_restore(job_id, token, root_error, quarantine_map):
    updated = SimcBenchmarkPurgeTask.objects.filter(
        pk=job_id,
        claim_token=token,
        status=SimcBenchmarkPurgeTask.STATUS_RUNNING,
    ).update(
        status=SimcBenchmarkPurgeTask.STATUS_RESTORING,
        quarantine_map=quarantine_map,
        error_detail=root_error,
        claimed_at=timezone.now(),
    )
    if updated != 1:
        raise PurgeConflict('恢复阶段任务所有权已失效')


def _recover_partial_quarantine(job, token, command, quarantine_map, expected_status):
    recovered = command._recover_quarantine_objects(
        tuple(job.plan.get('object_keys') or ()), job.batch_id,
    )
    if recovered:
        quarantine_map.update(recovered)
        _persist_quarantine(job.id, token, quarantine_map, expected_status)
    return quarantine_map


def _process_claimed_purge(job_id, token, phase):
    job = SimcBenchmarkPurgeTask.objects.get(pk=job_id)
    command = _oss_command()
    quarantine_map = dict(job.quarantine_map or {})

    if phase == 'cleanup':
        _complete_cleanup(job.id, token, command, quarantine_map)
        return True

    if phase == 'restore':
        root_error = job.error_detail or '后台清理失败，正在恢复'
        try:
            quarantine_map = _recover_partial_quarantine(
                job, token, command, quarantine_map,
                SimcBenchmarkPurgeTask.STATUS_RESTORING,
            )
        except Exception as exc:
            SimcBenchmarkPurgeTask.objects.filter(
                pk=job.id, claim_token=token,
                status=SimcBenchmarkPurgeTask.STATUS_RESTORING,
            ).update(
                status=SimcBenchmarkPurgeTask.STATUS_RESTORE_PENDING,
                claim_token='', claimed_at=None,
                error_detail=f'{root_error}; 隔离证据恢复待重试: {exc}',
            )
            return True
        _restore_panel_and_fail(job.id, token, command, quarantine_map, root_error)
        return True

    try:
        object_keys = tuple(job.plan.get('object_keys') or ())
        if job.attempts > 1:
            quarantine_map = _recover_partial_quarantine(
                job, token, command, quarantine_map,
                SimcBenchmarkPurgeTask.STATUS_RUNNING,
            )
        pending_keys = [key for key in object_keys if key not in quarantine_map]
        for offset in range(0, len(pending_keys), QUARANTINE_PERSIST_BATCH_SIZE):
            batch = tuple(pending_keys[offset:offset + QUARANTINE_PERSIST_BATCH_SIZE])
            created = command._quarantine_objects(batch, job.batch_id)
            if created:
                quarantine_map.update(created)
            _refresh_claim(job.id, token, SimcBenchmarkPurgeTask.STATUS_RUNNING)
        _persist_quarantine(
            job.id, token, quarantine_map, SimcBenchmarkPurgeTask.STATUS_RUNNING,
        )
        # Keep every pre-commit OSS operation non-destructive. If DB ownership
        # or connectivity is lost, an old delayed Worker can only repeat copies.
        _delete_database(job.id, token)
        return True
    except Exception as exc:
        logger.exception('[Benchmark purge] job %s failed before database commit', job.id)
        root_error = str(exc)
        try:
            quarantine_map = _recover_partial_quarantine(
                job, token, command, quarantine_map,
                SimcBenchmarkPurgeTask.STATUS_RUNNING,
            )
            _enter_restore(job.id, token, root_error, quarantine_map)
        except Exception as recovery_exc:
            SimcBenchmarkPurgeTask.objects.filter(
                pk=job.id,
                claim_token=token,
                status=SimcBenchmarkPurgeTask.STATUS_RUNNING,
            ).update(
                status=SimcBenchmarkPurgeTask.STATUS_RESTORE_PENDING,
                claim_token='',
                claimed_at=None,
                quarantine_map=quarantine_map,
                error_detail=f'{root_error}; 隔离证据恢复待重试: {recovery_exc}',
            )
            return True
        _restore_panel_and_fail(job.id, token, command, quarantine_map, root_error)
        return True


def process_next_purge():
    claim = _claim_next_job()
    if claim is None:
        return False
    job_id, token, phase = claim
    try:
        return _process_claimed_purge(job_id, token, phase)
    finally:
        _release_execution_lock(job_id)
