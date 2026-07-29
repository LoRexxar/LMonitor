"""HTTP endpoints for independent SimC agents (machine authentication only)."""

from django.conf import settings
from django.db import IntegrityError
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from botend.models import SimcAgent, SimcAgentEnrollmentCode, SimcBackendBinary, SimulationRun
from botend.services.simc_agent_control import (
    AgentAPIError,
    authenticate_bearer,
    create_enrollment_code,
    enrollment_code_state,
    heartbeat_agent,
    parse_json_object,
    register_agent,
    revoke_enrollment_code,
)
from botend.services.simc_run_control import (
    claim_run, complete_run, heartbeat_run, request_report_upload,
    validate_completion_metadata,
)


# Two 256 KiB output fields remain below this independent JSON cap even
# under worst-case six-byte JSON escaping; HTML is categorically excluded.
METADATA_MAX_BYTES = 3 * 1024 * 1024 + 8192


def _error_response(exc):
    response = JsonResponse({
        **exc.details, 'success': False, 'error': exc.message,
    }, status=exc.status)
    response['Cache-Control'] = 'no-store'
    response['Pragma'] = 'no-cache'
    return response


def _no_store(response):
    response['Cache-Control'] = 'no-store'
    response['Pragma'] = 'no-cache'
    return response


def _agent_response(agent):
    return {
        'id': agent.pk, 'host_identifier': agent.host_identifier,
        'name': agent.name, 'status': agent.status,
        'backend': {'id': agent.backend_id, 'identifier': agent.backend.identifier},
    }


def _parse_request_json(request, *, max_bytes=65536):
    if request.content_type != 'application/json':
        raise AgentAPIError('Content-Type must be application/json')
    content_length = request.META.get('CONTENT_LENGTH', '')
    if isinstance(content_length, str) and content_length.isdigit():
        if int(content_length) > max_bytes:
            raise AgentAPIError('JSON payload is too large', 413)
    return parse_json_object(request.body, max_bytes=max_bytes)


@method_decorator(csrf_exempt, name='dispatch')
class SimcAgentRegisterAPIView(View):
    http_method_names = ['post', 'options']

    def post(self, request):
        try:
            payload = _parse_request_json(request)
            result = register_agent(payload, request.headers.get('Authorization', ''))
        except AgentAPIError as exc:
            return _error_response(exc)
        except IntegrityError:
            return _error_response(AgentAPIError('Agent registration conflict', 409))

        body = {
            'success': True,
            'agent': _agent_response(result.agent),
            'heartbeat_interval_seconds': int(getattr(
                settings, 'SIMC_AGENT_HEARTBEAT_INTERVAL_SECONDS', 30
            )),
            'lease_seconds': int(getattr(settings, 'SIMC_AGENT_LEASE_SECONDS', 90)),
        }
        if result.agent_token is not None:
            body['agent_token'] = result.agent_token
        return _no_store(JsonResponse(body, status=201 if result.first_registration else 200))


@method_decorator(csrf_exempt, name='dispatch')
class SimcAgentHeartbeatAPIView(View):
    http_method_names = ['post', 'options']

    def post(self, request):
        try:
            payload = _parse_request_json(request)
            agent = heartbeat_agent(payload, request.headers.get('Authorization', ''))
        except AgentAPIError as exc:
            return _error_response(exc)
        return _no_store(JsonResponse({'success': True, 'agent': _agent_response(agent)}))


@method_decorator(csrf_exempt, name='dispatch')
class SimcAgentJobClaimAPIView(View):
    http_method_names = ['post', 'options']

    def post(self, request):
        try:
            result = claim_run(_parse_request_json(request), request.headers.get('Authorization', ''))
        except AgentAPIError as exc:
            return _error_response(exc)
        if result is None:
            return _no_store(JsonResponse({}, status=204))
        return _no_store(JsonResponse(result))


@method_decorator(csrf_exempt, name='dispatch')
class SimcAgentJobHeartbeatAPIView(View):
    http_method_names = ['post', 'options']

    def post(self, request, run_id):
        try:
            run = heartbeat_run(run_id, _parse_request_json(request), request.headers.get('Authorization', ''))
        except AgentAPIError as exc:
            return _error_response(exc)
        return _no_store(JsonResponse({
            'run_id': run.pk, 'lease_expires_at': run.lease_expires_at.isoformat(),
        }))


@method_decorator(csrf_exempt, name='dispatch')
class SimcAgentJobReportUploadAPIView(View):
    http_method_names = ['post', 'options']

    def post(self, request, run_id):
        try:
            result = request_report_upload(
                run_id, _parse_request_json(request),
                request.headers.get('Authorization', ''),
            )
        except AgentAPIError as exc:
            return _error_response(exc)
        return _no_store(JsonResponse(result))


@method_decorator(csrf_exempt, name='dispatch')
class SimcAgentJobCompleteAPIView(View):
    http_method_names = ['post', 'options']

    def post(self, request, run_id):
        try:
            metadata = _parse_request_json(request, max_bytes=METADATA_MAX_BYTES)
            validate_completion_metadata(metadata)
            result = complete_run(
                run_id, metadata, request.headers.get('Authorization', ''),
            )
        except AgentAPIError as exc:
            return _error_response(exc)
        return _no_store(JsonResponse(result))


class SimcAgentManagementListAPIView(View):
    http_method_names = ['get']

    def get(self, request):
        if not _staff_required(request):
            return _no_store(JsonResponse({'success': False, 'error': 'Staff access required'}, status=403))
        from django.utils import timezone
        now = timezone.now()
        timeout = max(1, int(getattr(settings, 'SIMC_AGENT_ONLINE_TIMEOUT_SECONDS', 90)))
        leases_by_agent = {}
        for row in SimulationRun.objects.filter(
            status='running', lease_agent__isnull=False, lease_expires_at__gt=now,
        ).select_related('task').order_by('lease_agent_id', 'lease_expires_at', 'id'):
            leases_by_agent.setdefault(row.lease_agent_id, []).append(row)
        rows = []
        for agent in SimcAgent.objects.select_related('backend').order_by('id'):
            agent_leases = leases_by_agent.get(agent.pk, [])
            lease_payloads = [
                {'run_id': lease.pk, 'task_id': lease.task_id,
                 'expires_at': lease.lease_expires_at.isoformat(),
                 'instance_id': lease.lease_instance_id}
                for lease in agent_leases
            ]
            rows.append({
                'id': agent.pk, 'name': agent.name,
                'backend': {'id': agent.backend_id, 'identifier': agent.backend.identifier,
                            'name': agent.backend.name},
                'is_active': agent.is_active, 'status': agent.status,
                'online': agent.is_online(timeout_seconds=timeout, now=now),
                'platform': agent.platform, 'agent_version': agent.agent_version,
                'agent_revision': agent.agent_revision,
                'protocol_version': agent.protocol_version, 'current_version': agent.current_version,
                'capabilities': agent.capabilities, 'binary_available': agent.binary_available,
                'last_seen_at': agent.last_seen_at.isoformat() if agent.last_seen_at else None,
                'registered_at': agent.registered_at.isoformat() if agent.registered_at else None,
                'leases': lease_payloads,
                # Keep this compatibility summary for existing management clients.
                'lease': lease_payloads[0] if lease_payloads else None,
            })
        return _no_store(JsonResponse({'success': True, 'data': rows}))


class SimcAgentManagementActiveAPIView(View):
    http_method_names = ['post']

    def post(self, request, agent_id):
        if not _staff_required(request):
            return _no_store(JsonResponse({'success': False, 'error': 'Staff access required'}, status=403))
        try:
            payload = _parse_request_json(request)
            if set(payload) != {'is_active'} or not isinstance(payload['is_active'], bool):
                raise AgentAPIError('JSON payload must be exactly {"is_active": bool}')
            agent = SimcAgent.objects.get(pk=agent_id)
        except AgentAPIError as exc:
            return _error_response(exc)
        except SimcAgent.DoesNotExist:
            return _error_response(AgentAPIError('Agent not found', 404))
        agent.is_active = payload['is_active']
        agent.save(update_fields=['is_active', 'updated_at'])
        return _no_store(JsonResponse({'success': True, 'id': agent.pk,
                                       'is_active': agent.is_active}))


def _staff_required(request):
    return request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser)


def _serialize_enrollment_code(row):
    return {
        'id': row.pk,
        'backend': {
            'id': row.backend_id,
            'identifier': row.backend.identifier,
            'name': row.backend.name,
        },
        'status': enrollment_code_state(row),
        'created_by': row.created_by.get_username() if row.created_by else None,
        'created_at': row.created_at.isoformat(),
        'expires_at': row.expires_at.isoformat(),
        'consumed_at': row.consumed_at.isoformat() if row.consumed_at else None,
        'consumed_by_agent_id': row.consumed_by_agent_id,
        'revoked_at': row.revoked_at.isoformat() if row.revoked_at else None,
    }


class SimcAgentEnrollmentCodeListAPIView(View):
    http_method_names = ['get', 'post']

    def get(self, request):
        if not _staff_required(request):
            return _no_store(JsonResponse({'success': False, 'error': 'Staff access required'}, status=403))
        rows = SimcAgentEnrollmentCode.objects.select_related(
            'backend', 'created_by', 'consumed_by_agent',
        ).order_by('-created_at', '-id')[:100]
        return _no_store(JsonResponse({
            'success': True,
            'data': [_serialize_enrollment_code(row) for row in rows],
            'backends': [
                {'id': row.pk, 'identifier': row.identifier, 'name': row.name}
                for row in SimcBackendBinary.objects.filter(is_active=True).order_by('id')
            ],
        }))

    def post(self, request):
        if not _staff_required(request):
            return _no_store(JsonResponse({'success': False, 'error': 'Staff access required'}, status=403))
        try:
            payload = _parse_request_json(request)
            if set(payload) != {'backend_identifier', 'expires_in_seconds'}:
                raise AgentAPIError('JSON payload fields are invalid')
            identifier = payload['backend_identifier']
            if not isinstance(identifier, str) or not identifier:
                raise AgentAPIError('backend_identifier is required')
            try:
                backend = SimcBackendBinary.objects.get(identifier=identifier, is_active=True)
            except SimcBackendBinary.DoesNotExist:
                raise AgentAPIError('Backend not found')
            row, plaintext = create_enrollment_code(
                backend=backend,
                created_by=request.user,
                expires_in_seconds=payload['expires_in_seconds'],
            )
        except AgentAPIError as exc:
            return _error_response(exc)
        data = _serialize_enrollment_code(row)
        data['enrollment_code'] = plaintext
        return _no_store(JsonResponse({'success': True, 'data': data}, status=201))


class SimcAgentEnrollmentCodeRevokeAPIView(View):
    http_method_names = ['post']

    def post(self, request, code_id):
        if not _staff_required(request):
            return _no_store(JsonResponse({'success': False, 'error': 'Staff access required'}, status=403))
        try:
            payload = _parse_request_json(request)
            if payload:
                raise AgentAPIError('JSON payload must be empty')
            row = revoke_enrollment_code(code_id)
            row = SimcAgentEnrollmentCode.objects.select_related(
                'backend', 'created_by', 'consumed_by_agent',
            ).get(pk=row.pk)
        except AgentAPIError as exc:
            return _error_response(exc)
        return _no_store(JsonResponse({'success': True, 'data': _serialize_enrollment_code(row)}))
