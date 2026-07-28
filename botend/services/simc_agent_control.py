"""Authentication, enrollment and heartbeat operations for independent SimC agents."""
import hashlib
import json
import re
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from botend.models import SimcAgent, SimcBackendBinary

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
REGISTER_REQUIRED = REGISTER_FIELDS - {'name'}
HEARTBEAT_FIELDS = {'status', 'platform', 'agent_version', 'protocol_version', 'capabilities',
                    'instance_id', 'current_version', 'binary_available'}
REPORT_FIELDS = ('platform', 'agent_version', 'protocol_version', 'capabilities', 'instance_id',
                 'current_version', 'binary_available')


class AgentAPIError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status
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
    host, identifier = _string(payload, 'host_identifier', 128), _string(payload, 'backend_identifier', 64)
    if not HOST_RE.fullmatch(host):
        raise AgentAPIError('host_identifier must be 32-128 lowercase hexadecimal characters')
    if not SLUG_RE.fullmatch(identifier):
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


def _authenticate_enrollment(authorization, identifier):
    scoped = getattr(settings, 'SIMC_AGENT_ENROLLMENT_TOKENS', {})
    configured = scoped.get(identifier, '') if scoped else getattr(settings, 'SIMC_AGENT_ENROLLMENT_TOKEN', '')
    if not configured:
        raise AgentAPIError('Agent enrollment is not configured', 503)
    if not constant_time_compare(str(configured), _parse_authorization(authorization, 'Enrollment')):
        raise AgentAPIError('Invalid enrollment token', 401)


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
    if not bearer:
        _authenticate_enrollment(authorization, values['backend_identifier'])
    with transaction.atomic():
        now = timezone.now()
        if bearer:
            agent = authenticate_bearer(authorization, lock=True)
            if agent.host_identifier != values['host_identifier']:
                raise AgentAPIError('Agent token does not match host_identifier', 409)
            if agent.backend.identifier != values['backend_identifier']:
                raise AgentAPIError('Host is bound to a different backend_identifier', 409)
            token, first = None, False
        else:
            backend, _ = SimcBackendBinary.objects.select_for_update().get_or_create(
                identifier=values['backend_identifier'],
                defaults={'name': values['backend_identifier'], 'simc_path': '', 'auto_update': False, 'is_active': True},
            )
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
        return RegistrationResult(agent, token, first)


def heartbeat_agent(payload, authorization):
    values = validate_heartbeat_payload(payload)
    with transaction.atomic():
        agent = authenticate_bearer(authorization, lock=True)
        _apply_report(agent, values, timezone.now())
        agent.save()
        return agent
