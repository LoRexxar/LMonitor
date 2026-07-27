import hashlib
import json
import math

from django.db import migrations
from django.utils import timezone


TASK_NAMES = {0: 'pending', 1: 'running', 2: 'success', 3: 'failed', 5: 'cancelled'}
RUN_NAMES = {
    'pending': 'pending', 'running': 'running', 'completed': 'success',
    'failed': 'failed', 'cancelled': 'cancelled', 'canceled': 'cancelled',
}


def _hash(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    ).encode('utf-8')).hexdigest()


def _case_status(task, runs, expected):
    task_status = TASK_NAMES.get(task.current_status, 'failed')
    if task_status in ('failed', 'cancelled', 'pending', 'running'):
        return task_status
    actual = [run.candidate_key for run in runs]
    statuses = [RUN_NAMES.get(run.status, 'failed') for run in runs]
    if not expected or len(actual) != len(set(actual)):
        return 'failed'
    if actual != expected:
        if actual == expected[:len(actual)]:
            return 'running' if statuses and any(s != 'pending' for s in statuses) else 'pending'
        return 'failed'
    if any(s in ('pending', 'running') for s in statuses):
        return 'running'
    if all(s == 'success' for s in statuses):
        return 'success'
    if all(s == 'cancelled' for s in statuses):
        return 'cancelled'
    if any(s == 'success' for s in statuses):
        return 'partial'
    return 'failed'


def _layout(execution):
    snapshot = execution.config_snapshot
    try:
        if (not isinstance(snapshot, dict) or not execution.config_hash
                or _hash(snapshot) != execution.config_hash
                or snapshot.get('version') != 2
                or not isinstance(snapshot.get('cases'), list)):
            return None
    except (TypeError, ValueError):
        return None
    result = {}
    for row in snapshot['cases']:
        if not isinstance(row, dict):
            return None
        coordinate = tuple(row.get(key) for key in ('spec_key', 'scenario_key', 'profile_key'))
        keys = row.get('candidate_keys')
        if (not all(isinstance(value, str) and value for value in coordinate)
                or not isinstance(keys, list) or not keys
                or any(not isinstance(key, str) or not key for key in keys)
                or len(keys) != len(set(keys)) or coordinate in result):
            return None
        result[coordinate] = keys
    if (snapshot.get('case_count') != len(result)
            or snapshot.get('run_count') != sum(len(keys) for keys in result.values())):
        return None
    return result


def backfill_aggregate(apps, schema_editor):
    Execution = apps.get_model('botend', 'SimcBenchmarkExecution')
    Case = apps.get_model('botend', 'SimcBenchmarkCase')
    Result = apps.get_model('botend', 'SimcBenchmarkResult')
    Panel = apps.get_model('botend', 'SimcBenchmarkPanel')
    Run = apps.get_model('botend', 'SimulationRun')
    now = timezone.now()

    # Lock all Panels for the atomic migration. Both the legacy and current creation
    # paths lock the Panel, preventing an execution from appearing between recovery
    # and slot assignment while old application processes are still draining.
    panel_ids = list(Panel.objects.select_for_update().order_by('pk').values_list(
        'pk', flat=True,
    ))
    active_panel_ids = set(Execution.objects.filter(
        completed_at__isnull=True, panel_id__in=panel_ids,
    ).values_list('panel_id', flat=True).distinct())
    for panel_id in active_panel_ids:
        active = list(Execution.objects.filter(
            panel_id=panel_id, completed_at__isnull=True,
        ).order_by('-created_at', '-pk'))
        for duplicate in active[1:]:
            Case.objects.filter(execution_id=duplicate.pk).update(status='failed')
            duplicate.status = 'failed'
            duplicate.completed_at = now
            duplicate.result_hash = ''
            duplicate.results_finalized_at = None
            duplicate.save(update_fields=[
                'status', 'completed_at', 'result_hash', 'results_finalized_at',
            ])
            Panel.objects.filter(published_execution_id=duplicate.pk).update(
                published_execution_id=None,
            )
        Panel.objects.filter(pk=panel_id).update(
            active_execution_id=active[0].pk if active else None,
        )

    for execution in Execution.objects.filter(completed_at__isnull=False).order_by('pk'):
        layout = _layout(execution)
        cases = list(Case.objects.filter(execution_id=execution.pk).select_related(
            'task',
        ).order_by('pk'))
        rows = []
        statuses = []
        valid = layout is not None and len(cases) == len(layout)
        seen = set()
        for case in cases:
            coordinate = (case.spec_key, case.scenario_key, case.profile_key)
            expected = layout.get(coordinate) if layout is not None else None
            task = case.task
            runs = list(Run.objects.filter(task_id=case.task_id).order_by('sequence', 'pk'))
            manifest = task.mode_params if task is not None and isinstance(task.mode_params, dict) else {}
            manifest = manifest.get('request_manifest')
            candidates = manifest.get('candidates') if isinstance(manifest, dict) else None
            manifest_keys = [item.get('candidate_key') for item in candidates] \
                if isinstance(candidates, list) else None
            if (task is None or not expected or manifest_keys != expected or coordinate in seen):
                status = 'failed'
            else:
                status = _case_status(task, runs, expected)
            seen.add(coordinate)
            case.status = status
            case.save(update_fields=['status'])
            statuses.append(status)
            if status == 'success':
                for run in runs:
                    summary = run.result_summary if isinstance(run.result_summary, dict) else {}
                    dps = summary.get('dps')
                    if (isinstance(dps, bool) or not isinstance(dps, (int, float))
                            or not math.isfinite(dps) or dps <= 0):
                        valid = False
                        break
                    rows.append({
                        'case_id': case.pk, 'spec_key': coordinate[0],
                        'scenario_key': coordinate[1], 'profile_key': coordinate[2],
                        'spec_label': case.spec_label,
                        'scenario_label': case.scenario_label,
                        'profile_label': case.profile_label,
                        'status': status,
                        'candidate_key': run.candidate_key, 'dps': float(dps),
                    })
            valid = valid and status == 'success'
        valid = valid and seen == (set(layout) if layout is not None else set())
        Result.objects.filter(case__execution_id=execution.pk).delete()
        if valid and rows:
            Result.objects.bulk_create([
                Result(case_id=row['case_id'], candidate_key=row['candidate_key'], dps=row['dps'])
                for row in rows
            ])
            execution.status = 'success'
            execution.result_hash = _hash({'completed_at': execution.completed_at.isoformat(), 'rows': [{
                key: row[key] for key in (
                    'spec_key', 'scenario_key', 'profile_key', 'spec_label',
                    'scenario_label', 'profile_label', 'status', 'candidate_key', 'dps',
                )
            } for row in rows]})
            execution.results_finalized_at = execution.completed_at
        else:
            if statuses and all(status == 'cancelled' for status in statuses):
                execution.status = 'cancelled'
            elif any(status in ('success', 'partial') for status in statuses):
                execution.status = 'partial'
            else:
                execution.status = 'failed'
            # A historical completion can never remain live. Pending/running case rows
            # are conservatively terminalized because no valid publication was recovered.
            Case.objects.filter(
                execution_id=execution.pk, status__in=('pending', 'running'),
            ).update(status='failed')
            execution.result_hash = ''
            execution.results_finalized_at = None
            Panel.objects.filter(published_execution_id=execution.pk).update(
                published_execution_id=None,
            )
        execution.save(update_fields=['status', 'result_hash', 'results_finalized_at'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    # This is deliberately separated from 0132. MySQL DDL is not transactional; once
    # 0132 is recorded, this idempotent data migration can retry safely as one transaction.
    atomic = True
    dependencies = [('botend', '0132_simc_benchmark_results')]
    operations = [migrations.RunPython(backfill_aggregate, noop_reverse, atomic=True)]