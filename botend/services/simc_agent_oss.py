"""Public OSS direct-upload protocol for standalone SimC Agent reports."""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.utils import timezone


MAX_REPORT_BYTES = 20 * 1024 * 1024
REPORT_PREFIX = 'simc_agent_results/'
REPORT_KEY_RE = re.compile(
    r'^simc_agent_results/simc_task_[1-9][0-9]*_run_[1-9][0-9]*\.html$'
)
LEGACY_REPORT_RE = re.compile(r'^[0-9a-f]{32}_run_[1-9][0-9]*\.html$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')


class ReportStorageError(RuntimeError):
    pass


class ReportValidationError(ReportStorageError):
    """The uploaded object exists but does not match the issued report identity."""


class ReportLeaseExpiredError(ReportStorageError):
    """The current Run lease cannot safely cover another signed upload."""


def _config():
    config = getattr(settings, 'OSS_CONFIG', {}) or {}
    required = ('access_key_id', 'access_key_secret', 'region', 'bucket_name')
    missing = [name for name in required if not config.get(name)]
    if missing:
        raise ReportStorageError('OSS_CONFIG missing: ' + ','.join(missing))
    return config


def _client():
    try:
        import alibabacloud_oss_v2 as oss
    except Exception as exc:
        raise ReportStorageError('OSS SDK is unavailable') from exc
    config = _config()
    credentials = oss.credentials.StaticCredentialsProvider(
        access_key_id=config['access_key_id'],
        access_key_secret=config['access_key_secret'],
    )
    client_config = oss.config.load_default()
    client_config.credentials_provider = credentials
    client_config.region = config['region']
    if config.get('endpoint'):
        client_config.endpoint = config['endpoint']
    # Agent upload tickets must never transmit report data over plaintext HTTP.
    client_config.disable_ssl = False
    return oss, oss.Client(client_config), config['bucket_name']


def object_key_for_run(run) -> str:
    from botend.services.simc_artifacts import agent_result_filename_for_run

    filename = agent_result_filename_for_run(run.task, run)
    if not filename:
        raise ReportStorageError('Run has no valid report filename')
    return REPORT_PREFIX + filename


def issue_upload_ticket(run, *, size: int, sha256: str, content_md5: str,
                        lease_fence: str, lease_expires_at) -> dict:
    """Issue one short-lived, checksum-bound HTTPS PUT URL for this fenced Run."""
    oss, client, bucket = _client()
    object_key = object_key_for_run(run)
    request = oss.PutObjectRequest(
        bucket=bucket,
        key=object_key,
        content_type='text/html; charset=utf-8',
        content_length=size,
        content_md5=content_md5,
        metadata={'sha256': sha256, 'lease-fence': lease_fence},
        object_acl='public-read',
        forbid_overwrite=True,
    )
    remaining_seconds = int((lease_expires_at - timezone.now()).total_seconds())
    if remaining_seconds <= 0:
        raise ReportLeaseExpiredError('Run lease expired before upload signing')
    lifetime = min(15 * 60, remaining_seconds)
    result = client.presign(request, expires=timedelta(seconds=lifetime))
    url = str(result.url or '')
    if not url.startswith('https://'):
        raise ReportStorageError('OSS presign did not produce an HTTPS URL')
    return {
        'object_key': object_key,
        'url': url,
        'method': 'PUT',
        'headers': dict(result.signed_headers or {}),
        'expires_at': result.expiration.isoformat() if result.expiration else None,
    }


def verify_uploaded_report(*, object_key: str, size: int, sha256: str,
                           lease_fence: str) -> None:
    """Verify the signed object metadata without downloading the report body."""
    if not isinstance(object_key, str) or not REPORT_KEY_RE.fullmatch(object_key):
        raise ReportValidationError('Invalid Agent report object key')
    oss, client, bucket = _client()
    try:
        result = client.head_object(oss.HeadObjectRequest(bucket=bucket, key=object_key))
    except Exception as exc:
        status_code = getattr(exc, 'status_code', None)
        error_code = str(getattr(exc, 'code', '') or getattr(exc, 'error_code', ''))
        if status_code == 404 or error_code in {'NoSuchKey', 'NotFound', 'NoSuchObject'}:
            raise ReportValidationError('OSS report object does not exist') from exc
        raise ReportStorageError('OSS report object is unavailable') from exc
    metadata = {str(key).lower(): str(value) for key, value in (result.metadata or {}).items()}
    if int(result.content_length or -1) != size:
        raise ReportValidationError('OSS report size mismatch')
    if metadata.get('sha256', '').lower() != sha256:
        raise ReportValidationError('OSS report SHA-256 metadata mismatch')
    if metadata.get('lease-fence', '') != lease_fence:
        raise ReportValidationError('OSS report lease fence mismatch')
    content_type = str(result.content_type or '').lower().replace(' ', '')
    if content_type != 'text/html;charset=utf-8':
        raise ReportValidationError('OSS report Content-Type mismatch')


def download_report_html(
    object_key: str,
    *,
    expected_size: int,
    expected_sha256: str,
    expected_lease_fence: str,
) -> tuple[str, str]:
    """Download one completion-verified Agent report for read-only analysis."""
    if not isinstance(object_key, str) or not REPORT_KEY_RE.fullmatch(object_key):
        raise ReportValidationError('Invalid Agent report object key')
    if not isinstance(expected_size, int) or isinstance(expected_size, bool):
        raise ReportValidationError('Invalid Agent report size')
    if expected_size <= 0 or expected_size > MAX_REPORT_BYTES:
        raise ReportValidationError('Agent report size is outside the allowed range')
    if expected_sha256 and not SHA256_RE.fullmatch(str(expected_sha256)):
        raise ReportValidationError('Invalid Agent report SHA-256')
    if not isinstance(expected_lease_fence, str) or not expected_lease_fence:
        raise ReportValidationError('Invalid Agent report lease fence')

    oss, client, bucket = _client()
    try:
        result = client.get_object(oss.GetObjectRequest(bucket=bucket, key=object_key))
        if result.body is None:
            raise ReportValidationError('OSS report body is unavailable')
        chunks = []
        body_size = 0
        digest = hashlib.sha256()
        with result.body:
            if int(result.content_length or -1) != expected_size:
                raise ReportValidationError('OSS report size mismatch')
            content_type = str(result.content_type or '').lower().replace(' ', '')
            if content_type != 'text/html;charset=utf-8':
                raise ReportValidationError('OSS report Content-Type mismatch')
            metadata = {str(key).lower(): str(value) for key, value in (result.metadata or {}).items()}
            object_sha256 = metadata.get('sha256', '').lower()
            if not SHA256_RE.fullmatch(object_sha256):
                raise ReportValidationError('OSS report SHA-256 metadata mismatch')
            if expected_sha256 and object_sha256 != expected_sha256.lower():
                raise ReportValidationError('OSS report SHA-256 metadata mismatch')
            if metadata.get('lease-fence') != expected_lease_fence:
                raise ReportValidationError('OSS report lease fence mismatch')
            for chunk in result.body.iter_bytes():
                body_size += len(chunk)
                if body_size > expected_size or body_size > MAX_REPORT_BYTES:
                    raise ReportValidationError('OSS report body size mismatch')
                chunks.append(chunk)
                digest.update(chunk)
    except ReportValidationError:
        raise
    except Exception as exc:
        status_code = getattr(exc, 'status_code', None)
        error_code = str(getattr(exc, 'code', '') or getattr(exc, 'error_code', ''))
        if status_code == 404 or error_code in {'NoSuchKey', 'NotFound', 'NoSuchObject'}:
            raise ReportValidationError('OSS report object does not exist') from exc
        raise ReportStorageError('OSS report object is unavailable') from exc

    if body_size != expected_size:
        raise ReportValidationError('OSS report body size mismatch')
    if digest.hexdigest() != object_sha256:
        raise ReportValidationError('OSS report body SHA-256 mismatch')
    return b''.join(chunks).decode('utf-8', errors='replace'), object_sha256


def _validated_public_base_url() -> str:
    config = getattr(settings, 'OSS_CONFIG', {}) or {}
    base_url = str(config.get('base_url') or '').strip()
    parsed = urlsplit(base_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ReportStorageError('OSS_CONFIG base_url must be a plain HTTPS origin/path')
    report_host = parsed.hostname.lower().rstrip('.')
    application_hosts = [
        str(host).strip().lower().rstrip('.')
        for host in (getattr(settings, 'ALLOWED_HOSTS', []) or [])
        if str(host).strip()
    ]
    if '*' in application_hosts:
        raise ReportStorageError(
            'OSS report origin cannot be verified with wildcard ALLOWED_HOSTS',
        )
    for application_host in application_hosts:
        if application_host.startswith('.'):
            suffix = application_host[1:]
            if report_host == suffix or report_host.endswith(application_host):
                raise ReportStorageError('OSS report HTML must use a separate origin')
        elif report_host == application_host:
            raise ReportStorageError('OSS report HTML must use a separate origin')
    return base_url.rstrip('/')


def public_report_url(object_key: str) -> str:
    if not isinstance(object_key, str) or not REPORT_KEY_RE.fullmatch(object_key):
        raise ReportStorageError('Invalid Agent report object key')
    encoded = '/'.join(quote(part) for part in object_key.split('/'))
    return _validated_public_base_url() + '/' + encoded


def public_legacy_report_url(file_path: str) -> str:
    """Build the OSS URL for a local Worker report uploaded by basename."""
    normalized = str(file_path or '').replace('\\', '/').strip()
    if not normalized.startswith('simc_results/'):
        raise ReportStorageError('Invalid legacy report path')
    filename = normalized.rsplit('/', 1)[-1]
    if not LEGACY_REPORT_RE.fullmatch(filename):
        raise ReportStorageError('Invalid legacy report filename')
    return _validated_public_base_url() + '/' + quote(filename)
