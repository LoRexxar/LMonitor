"""Authoritative Run-level control plane shared by local and remote SimC execution."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from botend.models import (
    SimcBenchmarkCase, SimcTask, SimcTaskArtifact, SimulationRun,
)
from botend.services.simc_agent_control import AgentAPIError, TOKEN_HASH_PREFIX, authenticate_bearer
from botend.services.simc_benchmark_scheduler import reconcile_execution_for_task
from botend.services.simc_task_service import initialize_task_runs
from botend.services.task_resolver import resolve_task

LEASE_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{43,128}$')
COMPLETION_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,64}$')
TERMINAL = ('completed', 'failed')
logger = logging.getLogger(__name__)


def _lease_seconds():
    return max(15, min(3600, int(getattr(settings, 'SIMC_AGENT_LEASE_SECONDS', 90))))


def _lease_digest(token):
    return TOKEN_HASH_PREFIX + hashlib.sha256(token.encode('ascii')).hexdigest()


def _check_agent_ready(agent, now):
    if not agent.backend.is_active:
        raise AgentAPIError('Backend is disabled', 403)
    if not agent.is_active:
        raise AgentAPIError('Agent is disabled', 403)
    if not agent.binary_available:
        raise AgentAPIError('Agent binary is unavailable', 403)
    timeout = max(1, int(getattr(settings, 'SIMC_AGENT_ONLINE_TIMEOUT_SECONDS', 90)))
    if not agent.is_online(timeout_seconds=timeout, now=now):
        raise AgentAPIError('Agent is offline', 403)


def _output_filename(run):
    from botend.services.simc_artifacts import agent_result_filename_for_run
    filename = agent_result_filename_for_run(run.task, run)
    if not filename:
        raise AgentAPIError('Task has no valid report filename', 409)
    return filename


def build_frozen_run_input(task, run, output_filename=None):
    """Compose one immutable Run from version snapshots and controlled candidate params."""
    from botend.controller.plugins.simc.SimcMonitor import SimcMonitor, _composer_identity
    from botend.services.simc_composer import SimcComposer

    resolved = resolve_task(task)
    profile_payload = resolved.profile_payload
    profile_spec = (resolved.resource_metadata.get('profile') or {}).get('spec', 'fury')
    composer_spec, composer_class = _composer_identity(
        resolved.simulation_params.get('spec') or profile_spec
    )
    filename = output_filename or _output_filename(run)
    request = {
        'spec': composer_spec,
        '_trusted_class_name': composer_class,
        'fight_style': resolved.simulation_params.get('fight_style', 'Patchwerk'),
        'time': resolved.simulation_params.get('max_time', 300),
        'target_count': resolved.simulation_params.get('desired_targets', 1),
        'iterations': resolved.simulation_params.get('iterations', 10000),
        'target_error': resolved.simulation_params.get('target_error'),
        'vary_combat_length': resolved.simulation_params.get('vary_combat_length'),
        'enemy_type': resolved.simulation_params.get('enemy_type'),
        'player_import_mode': profile_payload.get('player_config_mode', ''),
        'player_equipment': profile_payload.get('player_equipment', ''),
        'battlenet_region': profile_payload.get('battlenet_region', ''),
        'battlenet_realm': profile_payload.get('battlenet_realm', ''),
        'battlenet_character': profile_payload.get('battlenet_character', ''),
        'talent': profile_payload.get('talent', ''),
        'gear_strength': profile_payload.get('gear_strength'),
        'gear_crit': profile_payload.get('gear_crit'),
        'gear_haste': profile_payload.get('gear_haste'),
        'gear_mastery': profile_payload.get('gear_mastery'),
        'gear_versatility': profile_payload.get('gear_versatility'),
        'base_template_content': resolved.template_content or '',
        'override_action_list': resolved.apl_content or '',
        '_result_file_path': filename,
    }
    request = SimcMonitor.apply_candidate_overrides(request, run.candidate_params)
    code, composition_manifest, error = SimcComposer(task.user_id).compose(request)
    if error or code is None:
        raise ValueError(error or 'SimC composition failed')
    code = SimcMonitor.ensure_result_file_directive(code, filename)
    serializable = asdict(composition_manifest) if is_dataclass(composition_manifest) else (composition_manifest or {})
    talent_candidate = None
    if isinstance(run.candidate_params, dict) and isinstance(run.candidate_params.get('talent_candidate'), dict):
        raw = run.candidate_params['talent_candidate']
        talent_candidate = {key: raw.get(key) for key in ('name', 'talent', 'source')}
    manifest = {
        **(resolved.resource_metadata or {}),
        'composition_manifest': serializable,
        'talent_candidate': talent_candidate,
        'output_filename': filename,
    }
    return code, manifest


def runtime_threads(task):
    from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
    return SimcMonitor.runtime_threads(task)


def claim_run(payload, authorization):
    if set(payload) != {'instance_id'}:
        unknown = set(payload) - {'instance_id'}
        raise AgentAPIError('Unknown field: ' + sorted(unknown)[0] if unknown else 'Missing field: instance_id')
    instance_id = payload.get('instance_id')
    if not isinstance(instance_id, str) or not instance_id or len(instance_id) > 128:
        raise AgentAPIError('instance_id has invalid length')

    # Authentication outside the mutation transaction discovers the backend. It
    # is repeated under the Agent lock after Task -> Run locks are acquired.
    discovered_agent = authenticate_bearer(authorization, lock=False)
    _check_agent_ready(discovered_agent, timezone.now())
    with transaction.atomic():
        now = timezone.now()
        expired_run = SimulationRun.objects.filter(
            task_id=OuterRef('pk'), status='running', lease_expires_at__lte=now,
        )
        task = (SimcTask.objects.select_for_update().filter(
            backend_id=discovered_agent.backend_id, is_active=True, current_status=1,
            execution_owner=SimcTask.EXECUTION_OWNER_AGENT,
            simulation_runs__status='pending',
        ).annotate(has_expired_run=Exists(expired_run)).filter(
            has_expired_run=False,
        ).order_by('create_time', 'id').first())
        if task is None:
            benchmark = SimcBenchmarkCase.objects.filter(task_id=OuterRef('pk'))
            task = (SimcTask.objects.select_for_update().filter(
                backend_id=discovered_agent.backend_id, is_active=True, current_status=0,
                execution_owner__in=(SimcTask.EXECUTION_OWNER_UNASSIGNED,
                                     SimcTask.EXECUTION_OWNER_AGENT),
            ).annotate(
                is_benchmark=Exists(benchmark), has_expired_run=Exists(expired_run),
            ).filter(has_expired_run=False).order_by(
                'is_benchmark', 'create_time', 'id',
            ).first())

        # Keep one global order for control-plane mutations: Task -> Run -> Agent.
        locked_runs = []
        if task is not None:
            locked_runs = list(SimulationRun.objects.select_for_update().filter(
                task=task,
            ).order_by('id'))
        agent = authenticate_bearer(authorization, lock=True)
        _check_agent_ready(agent, now)
        if agent.pk != discovered_agent.pk or (task is not None and task.backend_id != agent.backend_id):
            raise AgentAPIError('Agent backend changed during claim', 409)
        if SimulationRun.objects.filter(
            lease_agent=agent, status='running', lease_expires_at__gt=now,
        ).exists():
            return None
        if task is None:
            return None
        if any(run.status == 'running' and run.lease_expires_at is not None
               and run.lease_expires_at <= now for run in locked_runs):
            # Stale recovery owns retry semantics for the entire Task.
            return None

        if task.current_status == 0:
            task.current_status = 1
            task.execution_owner = SimcTask.EXECUTION_OWNER_AGENT
            task.started_at = now
            task.completed_at = None
            task.save(update_fields=[
                'current_status', 'execution_owner', 'started_at',
                'completed_at', 'modified_time',
            ])
            initialize_task_runs(task, expected_started_at=now)
            locked_runs = list(SimulationRun.objects.select_for_update().filter(
                task=task,
            ).order_by('id'))
        if task.execution_owner != SimcTask.EXECUTION_OWNER_AGENT:
            return None
        run = next((row for row in sorted(locked_runs, key=lambda row: (row.sequence, row.pk))
                    if row.status == 'pending'), None)
        if run is None:
            raise AgentAPIError('Task has no pending Run', 409)

        token = secrets.token_urlsafe(32)
        expires = now + timedelta(seconds=_lease_seconds())
        try:
            code, manifest = build_frozen_run_input(task, run, _output_filename(run))
        except (ValueError, TypeError) as exc:
            raise AgentAPIError('Unable to compose frozen Run input', 409) from exc
        digest = hashlib.sha256(code.encode('utf-8')).hexdigest()
        run.status = 'running'
        run.started_at = now
        run.completed_at = None
        run.error_detail = None
        run.input_hash = digest
        run.resource_manifest = manifest
        run.lease_token_hash = _lease_digest(token)
        run.lease_expires_at = expires
        run.lease_heartbeat_at = now
        run.lease_instance_id = instance_id
        run.lease_agent = agent
        run.completion_id = ''
        run.save(update_fields=[
            'status', 'started_at', 'completed_at', 'error_detail', 'input_hash',
            'resource_manifest', 'lease_token_hash', 'lease_expires_at',
            'lease_heartbeat_at', 'lease_instance_id', 'lease_agent', 'completion_id',
        ])
        agent.status = agent.STATUS_BUSY
        agent.last_seen_at = now
        agent.instance_id = instance_id
        agent.save(update_fields=['status', 'last_seen_at', 'instance_id', 'updated_at'])
        timeout = max(1, int(getattr(settings, 'SIMC_AGENT_RUN_TIMEOUT_SECONDS', 300)))
        return {
            'run_id': run.pk, 'task_id': task.pk, 'sequence': run.sequence,
            'input': code, 'input_hash': digest, 'output_filename': _output_filename(run),
            'threads': runtime_threads(task), 'timeout_seconds': timeout,
            'lease_token': token, 'lease_expires_at': expires.isoformat(),
            'agent_id': agent.pk,
        }


def _validate_fence(run, agent, token, instance_id, now, *, require_unexpired=True):
    if run.lease_agent_id != agent.pk:
        raise AgentAPIError('Run is leased to another agent', 403)
    if not isinstance(token, str) or not LEASE_TOKEN_RE.fullmatch(token):
        raise AgentAPIError('Lease conflict', 409)
    if not constant_time_compare(_lease_digest(token), run.lease_token_hash or ''):
        raise AgentAPIError('Lease conflict', 409)
    if run.lease_instance_id != instance_id:
        raise AgentAPIError('Lease conflict', 409)
    if require_unexpired and (run.lease_expires_at is None or run.lease_expires_at <= now):
        raise AgentAPIError('Lease expired', 409)


def heartbeat_run(run_id, payload, authorization):
    if set(payload) != {'lease_token', 'instance_id'}:
        raise AgentAPIError('Heartbeat fields are invalid')
    instance_id = payload.get('instance_id')
    if not isinstance(instance_id, str) or not instance_id or len(instance_id) > 128:
        raise AgentAPIError('instance_id has invalid length')
    discovered_agent = authenticate_bearer(authorization, lock=False)
    task_id = SimulationRun.objects.filter(pk=run_id).values_list('task_id', flat=True).first()
    if task_id is None:
        raise AgentAPIError('Run not found', 404)
    with transaction.atomic():
        now = timezone.now()
        try:
            task = SimcTask.objects.select_for_update().get(pk=task_id)
            run = SimulationRun.objects.select_for_update().get(pk=run_id, task=task)
        except (SimcTask.DoesNotExist, SimulationRun.DoesNotExist):
            raise AgentAPIError('Run not found', 404)
        agent = authenticate_bearer(authorization, lock=True)
        if agent.pk != discovered_agent.pk:
            raise AgentAPIError('Agent identity changed during heartbeat', 409)
        if task.execution_owner != SimcTask.EXECUTION_OWNER_AGENT:
            raise AgentAPIError('Task is not agent-owned', 409)
        if run.status != 'running':
            raise AgentAPIError('Run is not running', 409)
        _validate_fence(run, agent, payload.get('lease_token'), instance_id, now)
        run.lease_heartbeat_at = now
        run.lease_expires_at = now + timedelta(seconds=_lease_seconds())
        run.save(update_fields=['lease_heartbeat_at', 'lease_expires_at'])
        task.modified_time = now
        task.save(update_fields=['modified_time'])
        agent.last_seen_at = now
        agent.status = agent.STATUS_BUSY
        agent.instance_id = instance_id
        agent.save(update_fields=['last_seen_at', 'status', 'instance_id', 'updated_at'])
        return run


def validate_completion_metadata(payload):
    allowed = {'lease_token', 'instance_id', 'completion_id', 'status', 'stdout', 'stderr'}
    if set(payload) != allowed:
        raise AgentAPIError('Completion metadata fields are invalid')
    for field, limit in (('instance_id', 128), ('completion_id', 64), ('stdout', 1024 * 1024), ('stderr', 1024 * 1024)):
        value = payload.get(field)
        if not isinstance(value, str) or (field in ('instance_id', 'completion_id') and not value) or len(value.encode('utf-8')) > limit:
            raise AgentAPIError(f'{field} is invalid', 413 if field in ('stdout', 'stderr') else 400)
    if payload['status'] not in TERMINAL:
        raise AgentAPIError('status has invalid value')
    if not COMPLETION_RE.fullmatch(payload['completion_id']):
        raise AgentAPIError('completion_id has invalid format')
    return payload


def _result_root():
    # Agent output is untrusted remote-node content. Keep it outside the public
    # static tree; it is exposed only through the authenticated Artifact preview
    # endpoint, which attaches the report CSP/sandbox headers.
    configured = getattr(settings, 'SIMC_AGENT_RESULT_ROOT', '')
    return Path(configured or (Path(settings.BASE_DIR) / 'var' / 'simc_agent_results')).resolve()


def _finalize_task(task, now):
    runs = list(SimulationRun.objects.select_for_update().filter(task=task).order_by('sequence'))
    if any(run.status not in TERMINAL for run in runs):
        task.current_status = 1
        task.completed_at = None
        task.save(update_fields=['current_status', 'completed_at', 'modified_time'])
        return
    if task.mode == 'attribute_sweep' and runs:
        from botend.services.simc_attribute_search import advance_attribute_search
        latest = max(run.round_number for run in runs)
        current = [run for run in runs if run.round_number == latest]
        if all(run.status == 'completed' for run in current):
            advancement = advance_attribute_search(task.pk, expected_started_at=task.started_at)
            if advancement.get('appended') or advancement.get('awaiting'):
                task.current_status = 1
                task.completed_at = None
                task.save(update_fields=['current_status', 'completed_at', 'modified_time'])
                return
    completed = [run for run in runs if run.status == 'completed']
    failed = [run for run in runs if run.status == 'failed']
    task.analysis_result = {
        **(task.analysis_result if isinstance(task.analysis_result, dict) else {}),
        'total': len(runs), 'succeeded': len(completed), 'failed': len(failed),
        'candidates': [{'run_id': row.pk, 'candidate_key': row.candidate_key,
                        'candidate_label': row.candidate_label, 'round_number': row.round_number,
                        'status': row.status, 'result_summary': row.result_summary or {}} for row in runs],
    }
    task.completed_at = now
    # Match the established local worker contract: comparison Tasks succeed when
    # at least one candidate succeeds; attribute search is all-or-nothing.
    if completed and not (task.mode == 'attribute_sweep' and failed):
        task.current_status = 2
        task.error_detail = None
        summary = completed[0].result_summary or {} if len(runs) == 1 else {'runs': [r.result_summary or {} for r in completed]}
        task.result_summary = json.dumps(summary, ensure_ascii=False)
        artifacts = list(SimcTaskArtifact.objects.filter(task=task, artifact_type='html_report').order_by('run__sequence'))
        task.result_file = ','.join(row.file_path.rsplit('/', 1)[-1] for row in artifacts)
    else:
        task.current_status = 3
        task.error_detail = '; '.join(filter(None, (row.error_detail for row in failed)))[:8000] or 'Run failed'
    task.save(update_fields=['current_status', 'completed_at', 'error_detail', 'result_summary',
                             'result_file', 'analysis_result', 'modified_time'])


def complete_run(run_id, metadata, report, authorization):
    # Reject unauthenticated callers before consuming/spooling their upload. The
    # transaction below authenticates again under lock before mutating state.
    discovered_agent = authenticate_bearer(authorization, lock=False)
    task_id = SimulationRun.objects.filter(pk=run_id).values_list('task_id', flat=True).first()
    if task_id is None:
        raise AgentAPIError('Run not found', 404)
    metadata = validate_completion_metadata(metadata)
    status = metadata['status']
    if status == 'completed' and report is None:
        raise AgentAPIError('completed status requires report')
    if status == 'failed' and report is not None:
        raise AgentAPIError('failed status must not include report')

    temp_path = None
    final_path = None
    if report is not None:
        content_type = str(getattr(report, 'content_type', '') or '').lower().split(';', 1)[0]
        if content_type not in {'text/html', 'application/xhtml+xml'}:
            raise AgentAPIError('report Content-Type is not allowed')
        if int(getattr(report, 'size', 0) or 0) > 20 * 1024 * 1024:
            raise AgentAPIError('report is too large', 413)
        root = _result_root()
        root.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix='.agent-upload-', suffix='.tmp', dir=root)
        size = 0
        try:
            with os.fdopen(fd, 'wb') as target:
                prefix = b''
                for chunk in report.chunks():
                    size += len(chunk)
                    if size > 20 * 1024 * 1024:
                        raise AgentAPIError('report is too large', 413)
                    if len(prefix) < 512:
                        prefix += chunk[:512 - len(prefix)]
                    target.write(chunk)
            if size < 13 or not re.match(br'\s*(?:<!doctype\s+html\b|<html\b)', prefix, re.IGNORECASE):
                raise AgentAPIError('report content is not HTML')
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    try:
        with transaction.atomic():
            now = timezone.now()
            try:
                task = SimcTask.objects.select_for_update().get(pk=task_id)
                run = SimulationRun.objects.select_for_update().get(pk=run_id, task=task)
            except (SimcTask.DoesNotExist, SimulationRun.DoesNotExist):
                raise AgentAPIError('Run not found', 404)
            agent = authenticate_bearer(authorization, lock=True)
            if agent.pk != discovered_agent.pk:
                raise AgentAPIError('Agent identity changed during completion', 409)
            if task.execution_owner != SimcTask.EXECUTION_OWNER_AGENT:
                raise AgentAPIError('Task is not agent-owned', 409)
            _validate_fence(run, agent, metadata['lease_token'], metadata['instance_id'], now,
                            require_unexpired=run.status == 'running')
            if run.status in TERMINAL:
                if run.completion_id == metadata['completion_id']:
                    return {'run_id': run.pk, 'status': run.status, 'idempotent': True}
                raise AgentAPIError('Completion conflict', 409)
            if run.status != 'running':
                raise AgentAPIError('Run is not running', 409)

            if status == 'completed':
                from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
                summary = SimcMonitor.validate_simulation_semantics(metadata['stdout'])
                # Preserve existing parsing semantics but require at least a parseable DPS.
                if summary.get('dps') is None or not re.search(r'\bDPS=', metadata['stdout']):
                    raise AgentAPIError('SimC result does not contain DPS')
                root = _result_root()
                filename = _output_filename(run)
                final_path = (root / filename).resolve()
                try:
                    final_path.relative_to(root)
                except ValueError:
                    raise AgentAPIError('Invalid report destination', 500)
                final_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp_path, final_path)
                temp_path = None
                SimcTaskArtifact.objects.update_or_create(
                    task=task, run=run, artifact_type='html_report',
                    defaults={'file_path': f'simc_agent_results/{filename}',
                              'file_size': final_path.stat().st_size},
                )
                run.result_summary = summary
                run.error_detail = None
            else:
                detail = (metadata['stderr'].strip() or metadata['stdout'].strip() or 'Agent execution failed')
                run.error_detail = detail[:8000]
                run.result_summary = None
            run.status = status
            run.completed_at = now
            run.completion_id = metadata['completion_id']
            run.save(update_fields=['status', 'completed_at', 'completion_id', 'result_summary', 'error_detail'])
            _finalize_task(task, now)
            agent.last_seen_at = now
            agent.status = agent.STATUS_ONLINE
            agent.save(update_fields=['last_seen_at', 'status', 'updated_at'])
        try:
            reconcile_execution_for_task(task.pk)
        except (IntegrityError, ValueError):
            # Projection is derived and retryable; never invalidate authoritative completion.
            logger.exception('Benchmark projection reconcile failed for completed task %s', task.pk)
        return {'run_id': run.pk, 'status': run.status, 'idempotent': False}
    except Exception:
        if final_path is not None and final_path.exists():
            final_path.unlink()
        raise
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
