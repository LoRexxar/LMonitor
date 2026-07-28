"""Authentication, enrollment and heartbeat operations for independent SimC agents."""
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from botend.models import SimcAgent, SimcAgentEnrollmentCode

HOST_RE = re.compile(r'^[0-9a-f]{32,128}$')
SLUG_RE = re.compile(r'^[-a-zA-Z0-9_]+$')
TOKEN_ID_RE = re.compile(r'^[A-Za-z0-9_-]{16,32}$')
TOKEN_SECRET_RE = re.compile(r'^[A-Za-z0-9_-]{43,128}$')
MAX_AUTHORIZATION_LENGTH = 512
TOKEN_HASH_PREFIX = 'sha256$'
DUMMY_TOKEN_HASH = TOKEN_HASH_PREFIX + ('0' * 64)
STATUSES = {'online', 'busy', 'degraded'}
REGISTER_FIELDS = {'host_identifier', 'backend_identifier', 'name', 'platform', 'agent_version',
                   'protocol_version', 'capabilities', 'instance_id', 'current_version', 'binary_available'}
REGISTER_REQUIRED = REGISTER_FIELDS - {'name', 'backend_identifier'}
HEARTBEAT_FIELDS = {'status', 'platform', 'agent_version', 'protocol_version', 'capabilities',
                    'instance_id', 'current_version', 'binary_available'}
REPORT_FIELDS = ('platform', 'agent_version', 'protocol_version', 'capabilities', 'instance_id',
                 'current_version', 'binary_available')


class AgentAPIError(Exception):
    def __init__(self, message, status=400, details=None):
        self.message, self.status, self.details = message, status, details or {}
        super().__init__(message)


@dataclass(frozen=True)
class RegistrationResult:
    agent: SimcAgent
    agent_token: str | None
    first_registration: bool


def parse_json_object(raw_body, *, max_bytes=65536):
    if len(raw_body) > max_bytes:
        raise AgentAPIError('JSON payload is too large', 413)
    try:
        payload = json.loads(raw_body.decode('utf-8'), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise AgentAPIError('Malformed JSON payload')
    if not isinstance(payload, dict):
        raise AgentAPIError('JSON payload must be an object')
    return payload


def _strict_fields(payload, allowed, required=()):
    unknown, missing = set(payload) - allowed, set(required) - set(payload)
    if unknown:
        raise AgentAPIError(f'Unknown field: {sorted(unknown)[0]}')
    if missing:
        raise AgentAPIError(f'Missing field: {sorted(missing)[0]}')


def _string(payload, field, limit, *, required=True, allow_blank=False):
    value = payload.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (not allow_blank and not value) or len(value) > limit:
        raise AgentAPIError(f'{field} has invalid length')
    return value


def _positive(payload, field):
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AgentAPIError(f'{field} must be a positive integer')
    return value


def _boolean(payload, field):
    value = payload.get(field)
    if not isinstance(value, bool):
        raise AgentAPIError(f'{field} must be a boolean')
    return value


def _capabilities(payload):
    value = payload.get('capabilities')
    if not isinstance(value, dict):
        raise AgentAPIError('capabilities must be a JSON object')
    try:
        raw = json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise AgentAPIError('capabilities must contain valid JSON values')
    if len(raw) > 16384:
        raise AgentAPIError('capabilities is too large')
    return value


def validate_registration_payload(payload):
    _strict_fields(payload, REGISTER_FIELDS, REGISTER_REQUIRED)
    host = _string(payload, 'host_identifier', 128)
    identifier = _string(payload, 'backend_identifier', 64, required=False)
    if not HOST_RE.fullmatch(host):
        raise AgentAPIError('host_identifier must be 32-128 lowercase hexadecimal characters')
    if identifier is not None and not SLUG_RE.fullmatch(identifier):
        raise AgentAPIError('backend_identifier has invalid format')
    return {'host_identifier': host, 'backend_identifier': identifier,
            'name': _string(payload, 'name', 100, required=False),
            'platform': _string(payload, 'platform', 32),
            'agent_version': _string(payload, 'agent_version', 64, allow_blank=True),
            'protocol_version': _positive(payload, 'protocol_version'),
            'capabilities': _capabilities(payload),
            'instance_id': _string(payload, 'instance_id', 128, allow_blank=True),
            'current_version': _string(payload, 'current_version', 128, allow_blank=True),
            'binary_available': _boolean(payload, 'binary_available')}


def validate_heartbeat_payload(payload):
    _strict_fields(payload, HEARTBEAT_FIELDS)
    values = {}
    if 'status' in payload:
        status = _string(payload, 'status', 16)
        if status not in STATUSES:
            raise AgentAPIError('status has invalid value')
        values['status'] = status
    validators = {'platform': lambda: _string(payload, 'platform', 32),
                  'agent_version': lambda: _string(payload, 'agent_version', 64, allow_blank=True),
                  'protocol_version': lambda: _positive(payload, 'protocol_version'),
                  'capabilities': lambda: _capabilities(payload),
                  'instance_id': lambda: _string(payload, 'instance_id', 128, allow_blank=True),
                  'current_version': lambda: _string(payload, 'current_version', 128, allow_blank=True),
                  'binary_available': lambda: _boolean(payload, 'binary_available')}
    for field, validator in validators.items():
        if field in payload:
            values[field] = validator()
    return values


def _parse_authorization(value, scheme):
    if not isinstance(value, str) or len(value) > MAX_AUTHORIZATION_LENGTH:
        raise AgentAPIError(f'{scheme} authentication required', 401)
    parts = value.split(' ')
    if len(parts) != 2 or parts[0].lower() != scheme.lower() or not parts[1]:
        raise AgentAPIError(f'{scheme} authentication required', 401)
    return parts[1]


def _token_digest(secret):
    return TOKEN_HASH_PREFIX + hashlib.sha256(secret.encode('ascii')).hexdigest()


def authenticate_bearer(authorization, *, lock=False):
    token = _parse_authorization(authorization, 'Bearer')
    if token.count('.') != 1:
        raise AgentAPIError('Invalid agent token', 401)
    token_id, secret = token.split('.', 1)
    if not TOKEN_ID_RE.fullmatch(token_id) or not TOKEN_SECRET_RE.fullmatch(secret):
        raise AgentAPIError('Invalid agent token', 401)
    qs = SimcAgent.objects.select_related('backend')
    if lock:
        qs = qs.select_for_update()
    try:
        agent = qs.get(token_id=token_id)
    except SimcAgent.DoesNotExist:
        constant_time_compare(_token_digest(secret), DUMMY_TOKEN_HASH)
        raise AgentAPIError('Invalid agent token', 401)
    if bool(agent.token_id) != bool(agent.token_hash):
        raise AgentAPIError('Agent has inconsistent token state', 409)
    if not constant_time_compare(_token_digest(secret), agent.token_hash or DUMMY_TOKEN_HASH):
        raise AgentAPIError('Invalid agent token', 401)
    return agent


def create_enrollment_code(*, backend, created_by, expires_in_seconds=1800):
    if (not isinstance(expires_in_seconds, int) or isinstance(expires_in_seconds, bool)
            or not 300 <= expires_in_seconds <= 86400):
        raise AgentAPIError('expires_in_seconds must be an integer between 300 and 86400')
    code_id, secret = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
    row = SimcAgentEnrollmentCode.objects.create(
        code_id=code_id,
        secret_hash=_token_digest(secret),
        backend=backend,
        created_by=created_by,
        expires_at=timezone.now() + timedelta(seconds=expires_in_seconds),
    )
    return row, f'lmsa_enroll_{code_id}.{secret}'


def enrollment_code_state(row, now=None):
    now = now or timezone.now()
    if row.consumed_at:
        return 'consumed'
    if row.revoked_at:
        return 'revoked'
    if row.expires_at <= now:
        return 'expired'
    return 'active'


def revoke_enrollment_code(code_pk):
    with transaction.atomic():
        try:
            row = SimcAgentEnrollmentCode.objects.select_for_update().get(pk=code_pk)
        except SimcAgentEnrollmentCode.DoesNotExist:
            raise AgentAPIError('Enrollment code not found', 404)
        if row.consumed_at:
            raise AgentAPIError('Consumed enrollment code cannot be revoked', 409)
        if row.revoked_at is None:
            row.revoked_at = timezone.now()
            row.save(update_fields=['revoked_at'])
        return row


def _authenticate_enrollment(authorization, identifier, now):
    token = _parse_authorization(authorization, 'Enrollment')
    if not token.startswith('lmsa_enroll_') or token.count('.') != 1:
        raise AgentAPIError('Invalid enrollment code', 401)
    code_id, secret = token[len('lmsa_enroll_'):].split('.', 1)
    if not TOKEN_ID_RE.fullmatch(code_id) or not TOKEN_SECRET_RE.fullmatch(secret):
        raise AgentAPIError('Invalid enrollment code', 401)
    try:
        row = SimcAgentEnrollmentCode.objects.select_for_update().select_related('backend').get(code_id=code_id)
    except SimcAgentEnrollmentCode.DoesNotExist:
        constant_time_compare(_token_digest(secret), DUMMY_TOKEN_HASH)
        raise AgentAPIError('Invalid enrollment code', 401)
    if not constant_time_compare(_token_digest(secret), row.secret_hash or DUMMY_TOKEN_HASH):
        raise AgentAPIError('Invalid enrollment code', 401)
    if row.revoked_at:
        raise AgentAPIError('Invalid enrollment code', 401)
    if row.consumed_at:
        raise AgentAPIError('Invalid enrollment code', 401)
    if row.expires_at <= now:
        raise AgentAPIError('Invalid enrollment code', 401)
    if identifier is not None and row.backend.identifier != identifier:
        raise AgentAPIError('Invalid enrollment code', 401)
    return row


def _issue_token(agent):
    token_id, secret = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
    agent.token_id, agent.token_hash = token_id, _token_digest(secret)
    return f'{token_id}.{secret}'


def _apply_report(agent, values, now, registration=False):
    if registration:
        agent.status = SimcAgent.STATUS_ONLINE
    elif 'status' in values:
        agent.status = values['status']
    for field in REPORT_FIELDS:
        if field in values:
            setattr(agent, field, values[field])
    agent.last_seen_at = now


def register_agent(payload, authorization):
    values = validate_registration_payload(payload)
    bearer = isinstance(authorization, str) and authorization.split(' ', 1)[0].lower() == 'bearer'
    with transaction.atomic():
        now = timezone.now()
        enrollment_code = None
        if bearer:
            agent = authenticate_bearer(authorization, lock=True)
            if agent.host_identifier != values['host_identifier']:
                raise AgentAPIError('Agent token does not match host_identifier', 409)
            if (values['backend_identifier'] is not None
                    and agent.backend.identifier != values['backend_identifier']):
                raise AgentAPIError('Host is bound to a different backend_identifier', 409)
            token, first = None, False
        else:
            enrollment_code = _authenticate_enrollment(
                authorization, values['backend_identifier'], now,
            )
            backend = enrollment_code.backend
            agent = SimcAgent.objects.select_for_update().filter(host_identifier=values['host_identifier']).first()
            if agent:
                if agent.backend_id != backend.pk:
                    raise AgentAPIError('Host is bound to a different backend_identifier', 409)
                if bool(agent.token_id) != bool(agent.token_hash):
                    raise AgentAPIError('Agent has inconsistent token state', 409)
                if agent.token_id:
                    raise AgentAPIError('Agent is already registered; retry with its existing Bearer token', 409)
            else:
                agent = SimcAgent(backend=backend, host_identifier=values['host_identifier'])
            token, first = _issue_token(agent), True
            agent.registered_at = now
        if values['name'] is not None:
            agent.name = values['name']
        _apply_report(agent, values, now, registration=True)
        agent.save()
        if enrollment_code is not None:
            enrollment_code.consumed_at = now
            enrollment_code.consumed_by_agent = agent
            enrollment_code.save(update_fields=['consumed_at', 'consumed_by_agent'])
        return RegistrationResult(agent, token, first)


def heartbeat_agent(payload, authorization):
    values = validate_heartbeat_payload(payload)
    with transaction.atomic():
        agent = authenticate_bearer(authorization, lock=True)
        _apply_report(agent, values, timezone.now())
        agent.save()
        return agent
