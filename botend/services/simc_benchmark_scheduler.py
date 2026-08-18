"""Durable scheduling and reconciliation for SimC benchmark executions."""
from __future__ import annotations

import re
import threading
from datetime import timedelta, timezone as datetime_timezone

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone

from botend.models import (
    SimcBenchmarkCase, SimcBenchmarkExecution, SimcBenchmarkPanel,
)
from botend.services.simc_benchmark_execution import (
    BenchmarkExecutionConflict, create_execution, reconcile_execution,
    reconcile_execution_case,
)

_ERROR_LIMIT = 120
_PATH = re.compile(r'(?:[A-Za-z]:[\\/]|/)(?:[^\s;:,]+[\\/])*[^\s;:,]*')
_PERMANENT_ERRORS = (ValidationError, PermissionDenied)
_reconcile_cursor = 0
_reconcile_cursor_lock = threading.Lock()


def _normalize(value):
    """Use the same durable slot representation as create_execution."""
    if not timezone.is_aware(value):
        value = timezone.make_aware(value, datetime_timezone.utc)
    return value.astimezone(datetime_timezone.utc).replace(microsecond=0)


def _safe_error(exc):
    """Return a bounded diagnostic without configuration, paths, or exception text."""
    # Exception messages may contain generated SimC input.  The class is sufficient
    # for operators to classify the failure while logs retain the private details.
    name = exc.__class__.__name__
    name = _PATH.sub('[redacted]', name)
    name = re.sub(r'[^A-Za-z0-9_.-]', '', name)
    return (name or 'Error')[:_ERROR_LIMIT]


def _advance_panel(panel_id, slot, now):
    """CAS-advance a slot under lock, respecting concurrent administrator edits."""
    with transaction.atomic():
        panel = SimcBenchmarkPanel.objects.select_for_update().get(pk=panel_id)
        current = _normalize(panel.next_run_at) if panel.next_run_at is not None else None
        if (not panel.is_active or not panel.schedule_enabled or current != slot):
            return False
        interval = panel.interval_seconds
        # The DB constraint is authoritative too, but guard corrupt/legacy rows.
        if type(interval) is not int or interval <= 0:
            raise ValidationError({'interval_seconds': 'must be greater than zero'})
        reference = _normalize(now)
        elapsed = max(0, (reference - slot).total_seconds())
        periods = int(elapsed // interval) + 1
        panel.last_scheduled_at = slot
        panel.next_run_at = slot + timedelta(seconds=periods * interval)
        panel.save(update_fields=['last_scheduled_at', 'next_run_at', 'updated_at'])
        return True


def schedule_due_panels(now=None, batch_size=20):
    """Create at most one execution per due Panel and phase-align its next slot."""
    now = now or timezone.now()
    if not timezone.is_aware(now):
        raise ValueError('now must be timezone-aware')
    limit = max(0, int(batch_size))
    panel_rows = list(SimcBenchmarkPanel.objects.filter(
        is_active=True,
        schedule_enabled=True,
        next_run_at__isnull=False,
        next_run_at__lte=now,
    ).order_by('next_run_at', 'id').values_list('id', 'next_run_at')[:limit])
    result = {
        'selected': len(panel_rows), 'scheduled': 0, 'advanced': 0,
        'failed': 0, 'errors': [],
    }
    for panel_id, raw_slot in panel_rows:
        slot = _normalize(raw_slot)
        try:
            panel = SimcBenchmarkPanel.objects.get(pk=panel_id)
            create_execution(
                panel,
                trigger=SimcBenchmarkExecution.TRIGGER_SCHEDULE,
                scheduled_slot=slot,
                # A panel schedule promises a fresh aggregate at every slot.
                # Supplement mode is deliberately reserved for an explicit
                # operator request to fill missing candidate results.
                execution_mode='full',
            )
        except _PERMANENT_ERRORS as exc:
            result['failed'] += 1
            result['errors'].append({'panel_id': panel_id, 'error': _safe_error(exc)})
            # A scheduled slot is not consumed until it has a durable Execution.
            # Skipping invalid definitions made next_run_at look healthy while the
            # promised full Execution was never created or observable.
            continue
        except (BenchmarkExecutionConflict, DatabaseError) as exc:
            result['failed'] += 1
            result['errors'].append({'panel_id': panel_id, 'error': _safe_error(exc)})
            continue
        except Exception as exc:
            result['failed'] += 1
            result['errors'].append({'panel_id': panel_id, 'error': _safe_error(exc)})
            continue

        result['scheduled'] += 1
        try:
            if _advance_panel(panel_id, slot, now):
                result['advanced'] += 1
        except Exception as exc:
            # The execution is durable.  Leaving the slot untouched makes the next
            # scheduler pass find the winner and retry only the CAS advancement.
            result['failed'] += 1
            result['errors'].append({
                'panel_id': panel_id, 'error': _safe_error(exc), 'stage': 'advance',
            })
    return result


def reconcile_pending_executions(batch_size=50):
    """Best-effort round-robin sweep of nonterminal executions."""
    global _reconcile_cursor
    limit = max(0, int(batch_size))
    # A process-local cursor needs no schema and independently wraps in every worker.
    # Serialize selection/movement so re-entrant maintenance cannot move it backwards.
    with _reconcile_cursor_lock:
        ids = list(SimcBenchmarkExecution.objects.filter(
            completed_at__isnull=True, id__gt=_reconcile_cursor,
        ).order_by('id').values_list('id', flat=True)[:limit])
        if not ids and limit:
            ids = list(SimcBenchmarkExecution.objects.filter(
                completed_at__isnull=True,
            ).order_by('id').values_list('id', flat=True)[:limit])
        if ids:
            _reconcile_cursor = ids[-1]
        next_cursor = _reconcile_cursor
    result = {
        'selected': len(ids), 'reconciled': 0, 'failed': 0, 'errors': [],
        'next_cursor': next_cursor,
    }
    for execution_id in ids:
        try:
            execution = SimcBenchmarkExecution.objects.get(pk=execution_id)
            reconcile_execution(execution)
            result['reconciled'] += 1
        except Exception as exc:
            result['failed'] += 1
            result['errors'].append({
                'execution_id': execution_id, 'error': _safe_error(exc),
            })
    return result


def reconcile_execution_for_task(task_id):
    """Reconcile the Execution currently authoritative for one benchmark Task."""
    case = SimcBenchmarkCase.objects.select_related('execution').filter(task_id=task_id).first()
    if case is None:
        return None
    return reconcile_execution_case(case.execution, case.pk)
