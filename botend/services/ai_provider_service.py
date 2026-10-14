"""Central AI configuration projection; no probes, prompts, or credential logging.

SECRET_KEY rotation requires resetting/re-entering database keys. Runtime evidence is
best effort and fenced to the configuration captured before the provider request.
"""
import base64
import hashlib
import hmac
import json
import math
from types import MappingProxyType
from urllib.parse import urlsplit

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.Random import get_random_bytes
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import OperationalError, ProgrammingError, transaction
from django.utils import timezone

PROVIDERS = {'glm': ('GLM', 'ZHIYU_API_CONFIG', True, 10),
             'mimo': ('MiMo', 'MIMO_API_CONFIG', False, 20),
             'codex': ('Codex', 'CODEX_API_CONFIG', True, 30)}
# Administrative priority cannot widen a provider's existing business scope.
PROVIDER_SCOPES = MappingProxyType({'glm': ('general', 'translation'),
                                   'mimo': ('general', 'translation'),
                                   'codex': ('translation',)})
OVERRIDES = ('enabled', 'priority', 'model', 'base_url', 'request_timeout_seconds', 'api_key_encrypted')
STATE_LABELS = {'unverified': '尚未验证', 'disabled': '已停用', 'incomplete': '配置不完整',
                'healthy': '正常', 'authentication_error': '认证失败', 'rate_limited': '限流或额度不足',
                'certificate_error': '证书校验失败', 'connection_error': '连接异常',
                'provider_error': '供应商异常', 'response_error': '响应异常'}
# Arbitrary provider code fields can echo secrets. Persist only recognized identifiers.
SAFE_ERROR_CODES = {'invalid_api_key', 'authentication_error', 'insufficient_quota', 'rate_limit_exceeded',
                    '1301', '1302', '1303', '1304', '1305', '1308', '1309', '1310', '1311',
                    '1110', '1111', '1112', '1113', '1000', '1001', '1002', '1003', '1004',
                    '1210', '1211', '1212', '1213', '1214', '1215', '1216'}


class ConfigurationConflict(Exception):
    pass


def _derived_key(context):
    return HKDF(str(settings.SECRET_KEY).encode('utf-8'), 32,
                b'LMonitor AI provider management v1', SHA256, context=context)


def _encrypt_key(value, provider):
    nonce = get_random_bytes(16)
    cipher = AES.new(_derived_key(b'credentials'), AES.MODE_GCM, nonce=nonce, mac_len=16)
    cipher.update(provider.encode('ascii'))
    ciphertext, tag = cipher.encrypt_and_digest(value.encode('utf-8'))
    return 'aesgcm:v1:' + base64.b64encode(nonce + tag + ciphertext).decode('ascii')


def _decrypt_key(value, provider):
    prefix = 'aesgcm:v1:'
    if not value.startswith(prefix):
        raise ValueError('Unsupported AI credential envelope')
    raw = base64.b64decode(value[len(prefix):], validate=True)
    if len(raw) < 32:
        raise ValueError('Invalid AI credential envelope')
    cipher = AES.new(_derived_key(b'credentials'), AES.MODE_GCM, nonce=raw[:16], mac_len=16)
    cipher.update(provider.encode('ascii'))
    return cipher.decrypt_and_verify(raw[32:], raw[16:32]).decode('utf-8')


def _runtime_signature(config):
    values = {field: config.get(field) for field in ('enabled', 'model', 'fallback_models', 'base_url',
                                                   'coding_base_url', 'api_key', 'request_timeout_seconds')}
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hmac.new(_derived_key(b'runtime-signature'), payload, hashlib.sha256).hexdigest()


def _baseline_configs():
    result = {}
    for key, (_, setting, enabled, priority) in PROVIDERS.items():
        config = dict(getattr(settings, setting, {}) or {})
        config.setdefault('enabled', enabled)
        config.setdefault('priority', priority)
        config.setdefault('request_timeout_seconds', 90)
        config['_scopes'] = PROVIDER_SCOPES[key]
        result[key] = config
    return result


def _configuration_model():
    # Importing models during Django startup can recurse through the GLM singleton.
    if not apps.ready or not apps.is_installed('botend'):
        return None
    return apps.get_model('botend', 'AIProviderConfiguration')


def _missing_management_table(error, table):
    message = str(error).lower()
    # Only the exact new table missing during a rolling migration may fall back.
    return table.lower() in message and (
        ('no such table:' in message) or
        (getattr(error, 'args', (None,))[0] == 1146 and "doesn't exist" in message) or
        ('relation ' in message and 'does not exist' in message))


def _read_rows():
    model = _configuration_model()
    if model is None:
        return {}
    try:
        return {row.provider_key: row for row in model.objects.filter(provider_key__in=PROVIDERS)}
    except (OperationalError, ProgrammingError) as error:
        if _missing_management_table(error, model._meta.db_table):
            return {}
        raise


def _resolve_with_rows(rows):
    configs = _baseline_configs()
    for key, config in configs.items():
        row = rows.get(key)
        if row is not None:
            for field in OVERRIDES[:-1]:
                value = getattr(row, field)
                if value is not None:
                    config[field] = value
            if row.base_url is not None and key == 'glm':
                config['coding_base_url'] = row.base_url
            if row.api_key_encrypted is not None:
                config['api_key'] = _decrypt_key(row.api_key_encrypted, key) if row.api_key_encrypted else ''
        config['_management_version'] = row.version if row else 0
        config['_runtime_signature'] = _runtime_signature(config)
    return configs


def resolve_ai_configs():
    """Return fresh effective private configurations for the supported providers."""
    return _resolve_with_rows(_read_rows())


def _endpoint(key, config):
    return (config.get('coding_base_url') or config.get('base_url') or '') if key == 'glm' else config.get('base_url', '')


def _is_complete(key, config):
    try:
        timeout = float(config.get('request_timeout_seconds', 90))
        return bool(str(config.get('api_key') or '').strip() and str(config.get('model') or '').strip()
                    and str(_endpoint(key, config) or '').strip() and math.isfinite(timeout) and timeout > 0)
    except (ValueError, TypeError):
        return False


def get_ai_provider_management_data():
    rows = _read_rows()
    configs = _resolve_with_rows(rows)
    providers = []
    for key, config in configs.items():
        row = rows.get(key)
        complete = _is_complete(key, config)
        code = row.last_state if row and row.last_config_signature == config['_runtime_signature'] else 'unverified'
        if config['enabled'] is not True:
            code = 'disabled'
        elif not complete:
            code = 'incomplete'
        state = {'code': code, 'label': STATE_LABELS.get(code, STATE_LABELS['unverified']),
                 'last_finished_at': row.last_finished_at if row else None,
                 'last_success_at': row.last_success_at if row else None,
                 'last_failure_at': row.last_failure_at if row else None,
                 'latency_ms': row.last_latency_ms if row else None,
                 'last_model': row.last_model if row else '',
                 'last_http_status': row.last_http_status if row else None,
                 'last_error_code': row.last_error_code if row else ''}
        source = 'settings' if not row or row.api_key_encrypted is None else ('cleared' if row.api_key_encrypted == '' else 'database')
        providers.append({'provider_key': key, 'name': PROVIDERS[key][0], 'enabled': config['enabled'] is True,
                          'scopes': list(config['_scopes']),
                          'scope_label': '通用' if 'general' in config['_scopes'] else '文章翻译专用',
                          'priority': config['priority'], 'model': config.get('model', ''),
                          'base_url': _endpoint(key, config), 'request_timeout_seconds': config['request_timeout_seconds'],
                          'has_api_key': bool(config.get('api_key')), 'api_key_source': source,
                          'config_source': 'database' if row and any(getattr(row, f) is not None for f in OVERRIDES) else 'settings',
                          'version': config['_management_version'], 'effective': complete, 'state': state})
    providers.sort(key=lambda p: (p['priority'], p['provider_key']))
    successful = [p for p in providers if p['state']['last_success_at']]
    return {'providers': providers,
            'preferred_provider': next((p['provider_key'] for p in providers
                                        if p['enabled'] and p['effective'] and 'general' in p['scopes']), None),
            'preferred_translation_provider': next((p['provider_key'] for p in providers
                                                    if p['enabled'] and p['effective'] and 'translation' in p['scopes']), None),
            'last_successful_provider': max(successful, key=lambda p: p['state']['last_success_at'])['provider_key'] if successful else None}


def _validate_payload(payload):
    allowed = {'version', 'api_key', 'clear_api_key', 'reset', *OVERRIDES[:-1]}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValidationError('未知配置字段')
    if type(payload.get('version')) is not int or not 0 <= payload['version'] <= 2147483646:
        raise ValidationError('version 必须是非负整数')
    for name in ('clear_api_key', 'reset'):
        if name in payload and type(payload[name]) is not bool:
            raise ValidationError('配置开关必须是布尔值')
    if 'enabled' in payload and payload['enabled'] is not None and type(payload['enabled']) is not bool:
        raise ValidationError('enabled 必须是布尔值')
    if 'priority' in payload and payload['priority'] is not None:
        if type(payload['priority']) is not int or not 0 <= payload['priority'] <= 100000:
            raise ValidationError('priority 必须是 0 到 100000 的整数')
    if 'request_timeout_seconds' in payload and payload['request_timeout_seconds'] is not None:
        value = payload['request_timeout_seconds']
        if type(value) not in (int, float) or not 0 < value <= 600 or not math.isfinite(value):
            raise ValidationError('超时必须大于 0 且不超过 600 秒')
    if 'model' in payload and payload['model'] is not None:
        value = payload['model']
        if not isinstance(value, str) or not 1 <= len(value) <= 255 or any(c.isspace() or not c.isprintable() for c in value):
            raise ValidationError('model 必须是完整且无空白的模型标识')
    if 'base_url' in payload and payload['base_url'] is not None:
        value = payload['base_url']
        try:
            if not isinstance(value, str) or len(value) > 500 or any(c.isspace() for c in value):
                raise ValueError
            url = urlsplit(value)
            if url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None or '?' in value or '#' in value:
                raise ValueError
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError
            URLValidator(schemes=['https'])(value)
        except (ValueError, ValidationError):
            raise ValidationError('base_url 必须是无用户信息、查询参数或片段的 HTTPS 地址') from None
    if 'api_key' in payload:
        value = payload['api_key']
        if not isinstance(value, str) or len(value) > 8192 or (value and (value != value.strip() or any(c.isspace() or not c.isprintable() for c in value))):
            raise ValidationError('api_key 格式无效')
    if payload.get('clear_api_key') and payload.get('api_key'):
        raise ValidationError('不能同时设置和清除密钥')
    if payload.get('reset') and any(field in payload for field in allowed - {'version', 'reset'}):
        raise ValidationError('reset 不能与其他配置更新混用')


def update_ai_provider_configuration(provider_key, payload, user):
    if provider_key not in PROVIDERS:
        raise KeyError(provider_key)
    _validate_payload(payload)
    model = _configuration_model()
    with transaction.atomic():
        model.objects.get_or_create(provider_key=provider_key)
        row = model.objects.select_for_update().get(pk=provider_key)
        if row.version != payload['version']:
            raise ConfigurationConflict
        if payload.get('reset'):
            for field in OVERRIDES:
                setattr(row, field, None)
        else:
            for field in OVERRIDES[:-1]:
                if field in payload:
                    setattr(row, field, payload[field])
            if payload.get('clear_api_key'):
                row.api_key_encrypted = ''
            elif payload.get('api_key'):
                row.api_key_encrypted = _encrypt_key(payload['api_key'], provider_key)
        row.version += 1
        row.updated_by = user
        row.save(update_fields=[*OVERRIDES, 'version', 'updated_by', 'updated_at'])


def _response_field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _tool_call_is_valid(tool):
    tool_id = _response_field(tool, 'id')
    function = _response_field(tool, 'function')
    name = _response_field(function, 'name')
    arguments = _response_field(function, 'arguments')
    if not isinstance(tool_id, str) or not tool_id or not isinstance(name, str) or not name:
        return False
    # Match the tool parser's json.loads input types, without importing GLM
    # during startup or validating a request-specific business schema.
    if not isinstance(arguments, (str, bytes, bytearray)):
        return False
    try:
        return isinstance(json.loads(arguments), dict)
    except (ValueError, TypeError):
        return False


def _response_is_healthy(response):
    choices = _response_field(response, 'choices')
    if not choices or not isinstance(choices, (list, tuple)):
        return False
    choice = choices[0]
    message = _response_field(choice, 'message')
    content, tools = _response_field(message, 'content'), _response_field(message, 'tool_calls')
    if tools:
        return isinstance(tools, (list, tuple)) and all(_tool_call_is_valid(tool) for tool in tools)
    return bool(isinstance(content, str) and content.strip())


def _classify_result(response, error):
    if error is None:
        return ('healthy' if _response_is_healthy(response) else 'response_error'), 200, ''
    from core.ai_errors import error_details, is_certificate_error, is_transient_connection_error
    status, code = error_details(error)
    safe_code = code if code in SAFE_ERROR_CODES else ''
    if is_certificate_error(error):
        state = 'certificate_error'
    elif status in (401, 403) or code in {'invalid_api_key', 'authentication_error', '1000', '1001', '1002', '1003', '1004'}:
        state = 'authentication_error'
    elif status == 429 or code in {'insufficient_quota', 'rate_limit_exceeded', '1302', '1303', '1304', '1305', '1308', '1309', '1310', '1311'}:
        state = 'rate_limited'
    elif is_transient_connection_error(error):
        state = 'connection_error'
    elif status and not 200 <= status < 300:
        state = 'provider_error'
    else:
        state = 'response_error'
    return state, status if status and 100 <= status <= 599 else None, safe_code


def record_ai_result(provider_key, config, *, response=None, error=None, latency_ms=None, model=None):
    """Persist only bounded diagnostics; never affect the business completion."""
    try:
        configuration_model = _configuration_model()
        if provider_key not in PROVIDERS or configuration_model is None:
            return
        version = config['_management_version']
        signature = config['_runtime_signature']
        state, status, code = _classify_result(response, error)
        now = timezone.now()
        # The configured primary model may differ from the actual fallback attempt.
        actual_model = model if isinstance(model, str) and len(model) <= 255 else ''
        latency = float(latency_ms) if latency_ms is not None else None
        if latency is not None and (not math.isfinite(latency) or latency < 0):
            latency = None
        with transaction.atomic():
            configuration_model.objects.get_or_create(provider_key=provider_key)
            row = configuration_model.objects.select_for_update().get(pk=provider_key)
            if row.version != version:
                return
            # Settings changes do not increment the database version. Fence late
            # outcomes against the current effective config under the same lock.
            current = _resolve_with_rows({provider_key: row})[provider_key]
            if current['_runtime_signature'] != signature:
                return
            # Finish-time ordering may differ from lock acquisition ordering.
            # A different signature must still replace old-config evidence.
            if (row.last_config_signature == signature and row.last_finished_at is not None
                    and row.last_finished_at > now):
                return
            row.last_state = state
            row.last_http_status = status
            row.last_error_code = code
            row.last_finished_at = now
            row.last_latency_ms = latency
            row.last_model = actual_model
            row.last_config_signature = signature
            if state == 'healthy':
                row.last_success_at = now
            else:
                row.last_failure_at = now
            row.save(update_fields=['last_state', 'last_http_status', 'last_error_code', 'last_finished_at',
                                    'last_latency_ms', 'last_model', 'last_config_signature',
                                    'last_success_at', 'last_failure_at'])
    except Exception:
        # Metrics are best effort: neither errors nor provider bodies may be logged.
        return
