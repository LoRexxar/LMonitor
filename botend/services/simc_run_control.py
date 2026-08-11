"""Authoritative Run-level control plane shared by local and remote SimC execution."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import timedelta

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


def _agent_run_capacity(agent):
    """Return the bounded, explicitly advertised concurrent lease capacity."""
    capabilities = agent.capabilities if isinstance(agent.capabilities, dict) else {}
    value = capabilities.get('max_concurrent_runs', 1)
    return value if type(value) is int and 1 <= value <= 64 else 1


def _configured_agent_revision():
    """Return the exact deployed LMonitor revision required for standalone Agents.

    Some long-lived control-plane processes use the legacy ``settings.py``
    supplied only on deployed hosts.  When that untracked settings module does
    not define the gate itself, derive it from the checkout rather than silently
    allowing stale Agents to keep claiming Runs.
    """
    configured = str(getattr(settings, 'SIMC_AGENT_REQUIRED_REVISION', '') or '').strip().lower()
    if configured:
        return configured
    try:
        revision = subprocess.check_output(
            ['git', '-C', settings.BASE_DIR, 'rev-parse', 'HEAD'], text=True, timeout=5,
        ).strip().lower()
    except (OSError, subprocess.SubprocessError):
        return ''
    return revision if re.fullmatch(r'[0-9a-f]{40}', revision) else ''


def _check_agent_ready(agent, now):
    if not agent.backend.is_active:
        raise AgentAPIError('Backend is disabled', 403)
    if not agent.is_active:
        raise AgentAPIError('Agent is disabled', 403)
    if not agent.binary_available:
        raise AgentAPIError('Agent binary is unavailable', 403)
    required_agent_revision = _configured_agent_revision()
    if required_agent_revision and str(agent.agent_revision or '').strip().lower() != required_agent_revision:
        raise AgentAPIError('Agent update required', 426, {
            'code': 'agent_update_required', 'current_revision': str(agent.agent_revision or ''),
            'required_revision': required_agent_revision,
        })
    # SimC binaries are maintained independently by each standalone Agent.
    # Backend.current_version describes the local Django Worker only; using it
    # as a remote Agent claim gate couples two different execution planes and
    # can strand an otherwise healthy Agent in a 409 loop.  The Agent's
    # current_version remains telemetry, while task-specific binary pinning
    # (if introduced later) must be carried by the frozen Run itself.
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
    """Compose readable SimC input from a Run's current frozen task configuration."""
    from botend.controller.plugins.simc.SimcMonitor import SimcMonitor, _composer_identity
    from botend.services.simc_composer import SimcComposer

    resolved = resolve_task(task)
    profile_payload = resolved.profile_payload
    profile_spec = (resolved.resource_metadata.get('profile') or {}).get('spec', 'fury')
    composer_spec, composer_class = _composer_identity(
        resolved.simulation_params.get('spec') or profile_spec
    )
    filename = output_filename or task.result_file or f'{task.id}.html'
    request = {
        'spec': composer_spec,
        '_trusted_class_name': composer_class,
        'fight_style': resolved.simulation_params.get('fight_style', 'Patchwerk'),
        'time': resolved.simulation_params.get('max_time', 300),
        'target_count': resolved.simulation_params.get('desired_targets', 1),
        'iterations': resolved.simulation_params.get('iterations', 10000),
        'use_ptr': profile_payload.get('use_ptr') is True,
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
    if 'raid_buffs' in resolved.simulation_params:
        request['raid_buffs'] = list(resolved.simulation_params['raid_buffs'])
    if 'use_class_raid_buff' in resolved.simulation_params:
        request['use_class_raid_buff'] = resolved.simulation_params['use_class_raid_buff'] is True
    request = SimcMonitor.apply_candidate_overrides(request, run.candidate_params)
    code, composition_manifest, error = SimcComposer(task.user_id).compose(request)
    if error or code is None:
        raise ValueError(error or 'SimC composition failed')
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
    allowed_fields = {'instance_id', 'agent_version', 'agent_revision', 'protocol_version'}
    if set(payload) - allowed_fields or 'instance_id' not in payload:
        unknown = set(payload) - allowed_fields
        raise AgentAPIError('Unknown field: ' + sorted(unknown)[0] if unknown else 'Missing field: instance_id')
    instance_id = payload.get('instance_id')
    if not isinstance(instance_id, str) or not instance_id or len(instance_id) > 128:
        raise AgentAPIError('instance_id has invalid length')

    # Authentication outside the mutation transaction discovers the backend. It
    # is repeated under the Agent lock after Task -> Run locks are acquired.
    discovered_agent = authenticate_bearer(authorization, lock=False)
    agent_version = payload.get('agent_version')
    agent_revision = payload.get('agent_revision')
    protocol_version = payload.get('protocol_version')
    if agent_version is not None and (not isinstance(agent_version, str) or len(agent_version) > 64):
        raise AgentAPIError('agent_version has invalid length')
    if agent_revision is not None and (not isinstance(agent_revision, str)
                                       or not re.fullmatch(r'[0-9a-f]{40}', agent_revision)):
        raise AgentAPIError('agent_revision must be a 40-character Git commit')

    if (protocol_version is not None and (
            isinstance(protocol_version, bool) or not isinstance(protocol_version, int)
            or protocol_version < 1)):
        raise AgentAPIError('protocol_version must be a positive integer')
    # Legacy 1.0 Agents did not send claim-time version fields and do not contain
    # the updater. They remain claim-compatible for the one-time bootstrap rollout;
    # updater-capable Agents always send both fields and are strictly gated.
    if (agent_version is None) != (protocol_version is None):
        raise AgentAPIError('agent_version and protocol_version must be sent together')
    required_version = str(getattr(settings, 'SIMC_AGENT_REQUIRED_VERSION', '1.4.0'))
    required_revision = _configured_agent_revision()
    required_protocol = int(getattr(settings, 'SIMC_AGENT_PROTOCOL_VERSION', 1))
    mismatch = None
    if required_revision and agent_revision != required_revision:
        mismatch = AgentAPIError('Agent update required', 426, {
            'code': 'agent_update_required',
            'current_revision': agent_revision or '',
            'required_revision': required_revision,
            'required_version': required_version,
        })
    elif agent_version is not None and agent_version != required_version:
        mismatch = AgentAPIError('Agent update required', 426, {
            'code': 'agent_update_required',
            'current_version': agent_version,
            'required_version': required_version,
        })
    elif protocol_version is not None and protocol_version != required_protocol:
        mismatch = AgentAPIError('Agent protocol version is incompatible', 426, {
            'code': 'agent_protocol_mismatch',
            'current_protocol_version': protocol_version,
            'required_protocol_version': required_protocol,
            'required_version': required_version,
        })
    if mismatch is not None:
        # Never tell a process to replace/re-exec its code while this Agent identity
        # still owns a live fenced Run (including uncertain completion responses).
        if SimulationRun.objects.filter(
            lease_agent=discovered_agent, status='running', lease_expires_at__gt=timezone.now(),
        ).exists():
            return None
        raise mismatch
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
        live_run_count = SimulationRun.objects.filter(
            lease_agent=agent, status='running', lease_expires_at__gt=now,
        ).count()
        if live_run_count >= _agent_run_capacity(agent):
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


def _validate_fence(run, agent, token, instance_id, now, *, require_unexpired=True,
                    require_lease_agent=True):
    if require_lease_agent and run.lease_agent_id != agent.pk:
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


SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
CONTENT_MD5_RE = re.compile(r'^[A-Za-z0-9+/]{22}==$')
MAX_REPORT_BYTES = 20 * 1024 * 1024
COMPLETION_TEXT_MAX_BYTES = 256 * 1024


def _validate_report_identity(payload):
    if set(payload) != {'size', 'sha256', 'content_md5'}:
        raise AgentAPIError('Report upload fields are invalid')
    size = payload.get('size')
    sha256 = payload.get('sha256')
    content_md5 = payload.get('content_md5')
    if type(size) is not int or size < 13 or size > MAX_REPORT_BYTES:
        raise AgentAPIError('report size is invalid', 413 if type(size) is int and size > MAX_REPORT_BYTES else 400)
    if type(sha256) is not str or not SHA256_RE.fullmatch(sha256):
        raise AgentAPIError('report sha256 is invalid')
    if type(content_md5) is not str or not CONTENT_MD5_RE.fullmatch(content_md5):
        raise AgentAPIError('report content_md5 is invalid')
    return {'size': size, 'sha256': sha256, 'content_md5': content_md5}


def request_report_upload(run_id, payload, authorization):
    allowed = {'lease_token', 'instance_id', 'size', 'sha256', 'content_md5'}
    if set(payload) != allowed:
        raise AgentAPIError('Report upload fields are invalid')
    identity = _validate_report_identity({key: payload[key] for key in ('size', 'sha256', 'content_md5')})
    discovered_agent = authenticate_bearer(authorization, lock=False)
    task_id = SimulationRun.objects.filter(pk=run_id).values_list('task_id', flat=True).first()
    if task_id is None:
        raise AgentAPIError('Run not found', 404)
    with transaction.atomic():
        task = SimcTask.objects.select_for_update().get(pk=task_id)
        run = SimulationRun.objects.select_for_update().select_related('task').get(pk=run_id, task=task)
        agent = authenticate_bearer(authorization, lock=True)
        if agent.pk != discovered_agent.pk:
            raise AgentAPIError('Agent identity changed during report upload request', 409)
        if task.execution_owner != SimcTask.EXECUTION_OWNER_AGENT:
            raise AgentAPIError('Task is not agent-owned', 409)
        # A completed Run already has its one authoritative result.  A delayed
        # report retry is harmless but must not be signed again or left retrying
        # forever just because the original Agent identity later changed.
        if run.status in TERMINAL:
            return {'run_id': run.pk, 'status': run.status, 'already_completed': True}
        if run.status != 'running':
            raise AgentAPIError('Run is not running', 409)
        now = timezone.now()
        try:
            _validate_fence(run, agent, payload.get('lease_token'), payload.get('instance_id'), now,
                            require_lease_agent=False)
        except AgentAPIError as exc:
            if (exc.status == 409 and str(exc) == 'Lease conflict'
                    and isinstance(payload.get('lease_token'), str)
                    and LEASE_TOKEN_RE.fullmatch(payload['lease_token'])):
                # The Run is still live under a newer/different lease.  A stale
                # durable outbox must stop retrying rather than uploading a report
                # that can never become authoritative.
                return {'run_id': run.pk, 'status': run.status, 'already_completed': True}
            raise
        lease_fence = run.lease_token_hash
        lease_expires_at = run.lease_expires_at
    try:
        from botend.services.simc_agent_oss import (
            issue_upload_ticket, object_key_for_run, public_report_url,
        )
        # Fail before signing/uploading if the immutable report cannot later be
        # exposed from a separate HTTPS origin.
        public_report_url(object_key_for_run(run))
        return issue_upload_ticket(
            run, **identity, lease_fence=lease_fence,
            lease_expires_at=lease_expires_at,
        )
    except Exception as exc:
        from botend.services.simc_agent_oss import ReportLeaseExpiredError, ReportStorageError
        if isinstance(exc, ReportLeaseExpiredError):
            raise AgentAPIError(str(exc), 409) from exc
        if isinstance(exc, ReportStorageError):
            raise AgentAPIError(str(exc), 503) from exc
        logger.exception('OSS report upload ticket failed for Run %s', run_id)
        raise AgentAPIError('OSS report upload service is unavailable', 503) from exc


def _validate_completion_report(report):
    if not isinstance(report, dict) or set(report) != {'object_key', 'size', 'sha256'}:
        raise AgentAPIError('Completion report fields are invalid')
    size = report.get('size')
    sha256 = report.get('sha256')
    object_key = report.get('object_key')
    if type(size) is not int or size < 13 or size > MAX_REPORT_BYTES:
        raise AgentAPIError('report size is invalid', 413 if type(size) is int and size > MAX_REPORT_BYTES else 400)
    if type(sha256) is not str or not SHA256_RE.fullmatch(sha256):
        raise AgentAPIError('report sha256 is invalid')
    if type(object_key) is not str or not object_key.startswith('simc_agent_results/'):
        raise AgentAPIError('report object_key is invalid')
    return report


def validate_completion_metadata(payload):
    allowed = {'lease_token', 'instance_id', 'completion_id', 'status', 'stdout', 'stderr', 'report'}
    if set(payload) != allowed:
        raise AgentAPIError('Completion metadata fields are invalid')
    for field, limit in (('instance_id', 128), ('completion_id', 64),
                         ('stdout', COMPLETION_TEXT_MAX_BYTES),
                         ('stderr', COMPLETION_TEXT_MAX_BYTES)):
        value = payload.get(field)
        if not isinstance(value, str) or (field in ('instance_id', 'completion_id') and not value) or len(value.encode('utf-8')) > limit:
            raise AgentAPIError(f'{field} is invalid', 413 if field in ('stdout', 'stderr') else 400)
    if payload['status'] not in TERMINAL:
        raise AgentAPIError('status has invalid value')
    if not COMPLETION_RE.fullmatch(payload['completion_id']):
        raise AgentAPIError('completion_id has invalid format')
    if payload['status'] == 'completed':
        _validate_completion_report(payload['report'])
    elif payload['report'] is not None:
        raise AgentAPIError('failed status must not include report')
    return payload


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
            task.refresh_from_db(fields=['analysis_result'])
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


def complete_run(run_id, metadata, authorization):
    discovered_agent = authenticate_bearer(authorization, lock=False)
    metadata = validate_completion_metadata(metadata)
    status = metadata['status']

    # Authenticate and validate the Run fence before any OSS request. The locked
    # transaction below repeats this check before committing authoritative state.
    run_for_key = SimulationRun.objects.select_related('task', 'lease_agent').filter(pk=run_id).first()
    if run_for_key is None:
        raise AgentAPIError('Run not found', 404)
    task_id = run_for_key.task_id
    if run_for_key.task.execution_owner != SimcTask.EXECUTION_OWNER_AGENT:
        raise AgentAPIError('Task is not agent-owned', 409)
    # A terminal Run is immutable: the first valid completion won.  Any later
    # completion is only a duplicate acknowledgement so an Agent can discard a
    # locally durable outbox entry; it must not re-validate its old lease or
    # overwrite the authoritative result.
    if run_for_key.status in ('cancelled', 'canceled'):
        raise AgentAPIError('Run was cancelled', 409)
    if run_for_key.status in TERMINAL:
        return {'run_id': run_for_key.pk, 'status': run_for_key.status, 'idempotent': True}
    try:
        _validate_fence(
            run_for_key, discovered_agent, metadata['lease_token'], metadata['instance_id'],
            timezone.now(), require_unexpired=True, require_lease_agent=False,
        )
    except AgentAPIError as exc:
        if (exc.status == 409 and str(exc) == 'Lease conflict'
                and isinstance(metadata.get('lease_token'), str)
                and LEASE_TOKEN_RE.fullmatch(metadata['lease_token'])):
            # A stale durable terminal record cannot win against the current
            # lease; acknowledge it solely so its Agent removes the outbox file.
            return {'run_id': run_for_key.pk, 'status': run_for_key.status, 'idempotent': True}
        raise
    if run_for_key.status != 'running':
        raise AgentAPIError('Run is not running', 409)

    report = metadata['report']
    report_html = ''
    if status == 'completed':
        from botend.services.simc_agent_oss import (
            ReportStorageError, ReportValidationError, download_report_html,
            object_key_for_run, public_report_url, verify_uploaded_report,
        )
        expected_key = object_key_for_run(run_for_key)
        if report['object_key'] != expected_key:
            raise AgentAPIError('Report object does not belong to this Run', 409)
        try:
            public_report_url(expected_key)
            verify_uploaded_report(
                object_key=expected_key, size=report['size'], sha256=report['sha256'],
                lease_fence=run_for_key.lease_token_hash,
            )
            report_html, _report_sha256 = download_report_html(
                expected_key,
                expected_size=report['size'],
                expected_sha256=report['sha256'],
                expected_lease_fence=run_for_key.lease_token_hash,
            )
        except ReportValidationError as exc:
            raise AgentAPIError(str(exc), 422) from exc
        except ReportStorageError as exc:
            raise AgentAPIError(str(exc), 503) from exc

    with transaction.atomic():
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
        if run.status in ('cancelled', 'canceled'):
            raise AgentAPIError('Run was cancelled', 409)
        if run.status in TERMINAL:
            return {'run_id': run.pk, 'status': run.status, 'idempotent': True}
        now = timezone.now()
        _validate_fence(run, agent, metadata['lease_token'], metadata['instance_id'], now,
                        require_unexpired=True, require_lease_agent=False)
        if run.status != 'running':
            raise AgentAPIError('Run is not running', 409)

        if status == 'completed':
            from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
            summary = SimcMonitor.validate_simulation_semantics(
                metadata['stdout'],
                report_html=report_html,
                extract_gear_ratings=(
                    isinstance(run.candidate_params, dict)
                    and run.candidate_params.get('candidate_type')
                    == 'attribute_baseline_probe'
                ),
            )
            if summary.get('dps') is None or not re.search(r'\bDPS=', metadata['stdout']):
                raise AgentAPIError('SimC result does not contain DPS')
            SimcTaskArtifact.objects.update_or_create(
                task=task, run=run, artifact_type='html_report',
                defaults={
                    'file_path': report['object_key'],
                    'file_size': report['size'],
                    'content_hash': report['sha256'],
                },
            )
            run.result_summary = summary
            if summary.get('valid'):
                run.error_detail = None
            else:
                status = 'failed'
                run.error_detail = str(
                    summary.get('reason') or 'SimC结果语义无效'
                )[:8000]
        else:
            detail = (metadata['stderr'].strip() or metadata['stdout'].strip()
                      or 'Agent execution failed')
            run.error_detail = detail[:8000]
            run.result_summary = None
        run.status = status
        run.completed_at = now
        run.completion_id = metadata['completion_id']
        run.save(update_fields=['status', 'completed_at', 'completion_id',
                                'result_summary', 'error_detail'])
        _finalize_task(task, now)
        agent.last_seen_at = now
        agent.status = agent.STATUS_ONLINE
        agent.save(update_fields=['last_seen_at', 'status', 'updated_at'])
    try:
        reconcile_execution_for_task(task.pk)
    except (IntegrityError, ValueError):
        logger.exception('Benchmark projection reconcile failed for completed task %s', task.pk)
    return {'run_id': run.pk, 'status': run.status, 'idempotent': False}
