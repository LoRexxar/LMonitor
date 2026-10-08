"""Private, immutable conditional evidence transport; no public URL surface.

Callers must authorize the current Task/Run lease under their own state lock.
This service validates the supplied binding; it does not query/transition runs.
"""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import timedelta
from urllib.parse import urlsplit, parse_qs

from django.utils import timezone
from simc_conditional_evidence import CHUNK_BYTES, MAX_COMPRESSED_BYTES, PROTOCOL_VERSION, EvidenceError, decode
from botend.services.simc_agent_oss import (
    _config, ReportStorageError, ReportValidationError, ReportLeaseExpiredError,
)

EVIDENCE_PREFIX = 'simc_conditional_evidence/v1/'
CONTENT_TYPE = 'application/gzip'
MAX_TICKET_SECONDS = 900
_SHA256 = re.compile(r'[0-9a-f]{64}')
# Existing server lease digest, NEVER the raw bearer lease token.
_FENCE = re.compile(r'sha256\$[0-9a-f]{64}')


def _private_client():
    # Independent private client; same SDK credential-provider contract as HTML.
    try:
        endpoint = _config().get('endpoint', '')
        if endpoint and '://' in endpoint and urlsplit(endpoint).scheme != 'https':
            raise ReportStorageError('Private evidence requires HTTPS OSS endpoint')
        import alibabacloud_oss_v2 as oss
        from botend.services.simc_oss_signer import ContentLengthSignerV4
        config = _config()
        client_config = oss.config.load_default()
        client_config.credentials_provider = oss.credentials.StaticCredentialsProvider(
            access_key_id=config['access_key_id'], access_key_secret=config['access_key_secret'])
        client_config.region = config['region']
        if endpoint:
            client_config.endpoint = endpoint
        client_config.disable_ssl = False
        return oss, oss.Client(client_config, signer=ContentLengthSignerV4()), config['bucket_name']
    except Exception:
        raise ReportStorageError('Private evidence OSS client unavailable') from None


def object_key_for_run(run, *, lease_fence: str) -> str:
    if not isinstance(lease_fence, str) or not _FENCE.fullmatch(lease_fence):
        raise ReportValidationError('Invalid evidence lease fence')
    task_id, run_id = getattr(run, 'task_id', None), getattr(run, 'pk', None)
    if any(type(value) is not int or value <= 0 for value in (task_id, run_id)):
        raise ReportValidationError('Invalid evidence Task/Run identity')
    fence_hash = hashlib.sha256(lease_fence.encode('ascii')).hexdigest()
    return f'{EVIDENCE_PREFIX}task_{task_id}/run_{run_id}/{fence_hash}.json.gz'


def _identity(size, sha256):
    if type(size) is not int or not 0 < size <= MAX_COMPRESSED_BYTES:
        raise ReportValidationError('Invalid compressed evidence size')
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise ReportValidationError('Invalid evidence SHA-256')


def issue_upload_ticket(run, *, size: int, sha256: str, content_md5: str,
                        lease_fence: str, lease_expires_at) -> dict:
    """Sign an immutable private PUT, with absolute expiry no later than lease."""
    _identity(size, sha256)
    key = object_key_for_run(run, lease_fence=lease_fence)
    try:
        md5 = base64.b64decode(content_md5, validate=True)
        if len(md5) != 16 or base64.b64encode(md5).decode('ascii') != content_md5:
            raise ValueError
    except (ValueError, TypeError):
        raise ReportValidationError('Invalid evidence Content-MD5') from None
    now = timezone.now()
    if lease_expires_at is None or timezone.is_naive(lease_expires_at):
        raise ReportLeaseExpiredError('Evidence lease requires aware expiry')
    expiration = min(lease_expires_at, now + timedelta(seconds=MAX_TICKET_SECONDS)).replace(microsecond=0)
    if expiration <= now:
        raise ReportLeaseExpiredError('Evidence lease expired before signing')
    oss, client, bucket = _private_client()
    request = oss.PutObjectRequest(bucket=bucket, key=key, content_type=CONTENT_TYPE,
        content_length=size, content_md5=content_md5, acl='private', forbid_overwrite=True,
        metadata={'sha256': sha256, 'lease-fence': lease_fence, 'evidence-version': str(PROTOCOL_VERSION)})
    try:
        result = client.presign(request, expiration=expiration)
        url = str(result.url or '')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        if result.expiration is None or result.expiration > expiration or result.expiration <= timezone.now():
            raise ValueError
        headers = dict(result.signed_headers or {})
        # Require both the actual query binding and the SDK-returned wire header.
        query = parse_qs(parsed.query)
        if (result.method != 'PUT' or query.get('x-oss-signature-version') != ['OSS4-HMAC-SHA256']
                or not query.get('x-oss-signature')
                or 'content-length' not in query.get('x-oss-additional-headers', [''])[0].split(';')):
            raise ValueError
        required = {'content-length': str(size), 'content-type': CONTENT_TYPE, 'content-md5': content_md5,
            'x-oss-object-acl':'private', 'x-oss-forbid-overwrite':'true',
            'x-oss-meta-sha256':sha256, 'x-oss-meta-lease-fence':lease_fence,
            'x-oss-meta-evidence-version':str(PROTOCOL_VERSION)}
        actual = {k.lower(): str(v) for k, v in headers.items()}
        if any(actual.get(k) != v for k, v in required.items()):
            raise ValueError
    except Exception:
        raise ReportStorageError('Private evidence signing contract failed') from None
    return {'protocol_version': PROTOCOL_VERSION, 'object_key': key, 'url': url,
            'method': 'PUT', 'headers': headers, 'expires_at': result.expiration.isoformat()}


def _metadata(result, size, sha256, fence):
    if result.content_length != size or result.content_type != CONTENT_TYPE:
        raise ReportValidationError('Evidence size or Content-Type mismatch')
    # SDK iter_bytes transparently decodes Content-Encoding: reject it BEFORE iteration.
    if getattr(result, 'content_encoding', None) not in (None, ''):
        raise ReportValidationError('Evidence Content-Encoding is forbidden')
    meta = result.metadata or {}
    if (meta.get('sha256') != sha256 or meta.get('lease-fence') != fence
            or meta.get('evidence-version') != str(PROTOCOL_VERSION)):
        raise ReportValidationError('Evidence digest, version or lease fence mismatch')


def download_evidence(run, *, object_key: str, expected_size: int,
                      expected_sha256: str, expected_lease_fence: str) -> dict[str, str]:
    """ACL + HEAD + GET metadata checks, bounded hash-verified body, strict decode.

    Never accepts a URL; never falls back on permission denial or public reads.
    Rechecks ACL after reading; operational policy must forbid external mutation.
    """
    _identity(expected_size, expected_sha256)
    if object_key != object_key_for_run(run, lease_fence=expected_lease_fence):
        raise ReportValidationError('Evidence object does not match Task/Run/fence')
    oss, client, bucket = _private_client()
    try:
        def check_acl():
            acl = client.get_object_acl(oss.GetObjectAclRequest(bucket=bucket, key=object_key))
            if acl.acl != 'private':
                raise ReportValidationError('Evidence object ACL must be explicitly private')
        check_acl()
        head = client.head_object(oss.HeadObjectRequest(bucket=bucket, key=object_key))
        _metadata(head, expected_size, expected_sha256, expected_lease_fence)
        result = client.get_object(oss.GetObjectRequest(bucket=bucket, key=object_key, accept_encoding='identity'))
        if result.body is None:
            raise ReportValidationError('Evidence body unavailable')
        with result.body:
            _metadata(result, expected_size, expected_sha256, expected_lease_fence)
            body = bytearray()
            digest = hashlib.sha256()
            for chunk in result.body.iter_bytes(block_size=CHUNK_BYTES):
                if len(body) + len(chunk) > expected_size:
                    raise ReportValidationError('Evidence download exceeds expected size')
                body.extend(chunk)
                digest.update(chunk)
        if len(body) != expected_size or digest.hexdigest() != expected_sha256:
            raise ReportValidationError('Evidence body size or SHA-256 mismatch')
        check_acl()
        return decode(bytes(body))
    except ReportValidationError:
        raise
    except EvidenceError:
        raise ReportValidationError('Evidence envelope validation failed') from None
    except Exception:
        # SDK exceptions may contain credentials, signed URLs or response text.
        raise ReportStorageError('Private evidence object unavailable') from None
