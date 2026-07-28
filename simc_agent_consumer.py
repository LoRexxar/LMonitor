#!/usr/bin/env python3
"""Standalone SimC execution agent.

Requires only Python 3 and a SimulationCraft binary. It never imports Django or
opens the LMonitor database; all coordination goes through the control-plane API.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import math
import os
import platform as platform_module
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = '1.1.0'
PROTOCOL_VERSION = 1
MAX_REPORT_BYTES = 20 * 1024 * 1024
COMPLETION_TEXT_MAX_BYTES = 256 * 1024
COMPLETION_ATTEMPTS = 3
TRUSTED_REPOSITORY_URLS = {
    'git@github.com:LoRexxar/LMonitor.git',
    'https://github.com/LoRexxar/LMonitor.git',
}
UPDATE_BRANCH = 'master'


def _utf8_tail(value: str, max_bytes: int) -> str:
    encoded = value.encode('utf-8', errors='replace')
    if len(encoded) <= max_bytes:
        return encoded.decode('utf-8')
    return encoded[-max_bytes:].decode('utf-8', errors='ignore')


class ConfigError(ValueError):
    pass


class APIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None,
                 details: dict[str, Any] | None = None):
        self.status = status
        self.details = details or {}
        super().__init__(message)


def _stable_host_identifier() -> str:
    for path in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
        try:
            value = Path(path).read_text(encoding='ascii').strip()
            if value:
                return hashlib.sha256(value.encode('ascii')).hexdigest()
        except OSError:
            pass
    material = f'{platform_module.node()}:{uuid.getnode()}'
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class AgentConfig:
    simc_path: str
    server_url: str = 'https://wowdaily.cn'
    backend_identifier: str = ''
    token_path: str = ''
    enrollment_token: str = ''
    name: str = ''
    platform: str = ''
    host_identifier: str = ''
    poll_interval_seconds: float = 5.0
    request_timeout_seconds: float = 30.0
    max_run_seconds: float = 7200.0
    allow_insecure_http: bool = False
    auto_update: bool = True
    repository_path: str = ''

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> 'AgentConfig':
        if not isinstance(values, dict):
            raise ConfigError('configuration must be a JSON object')
        allowed = set(cls.__dataclass_fields__)
        unknown = set(values) - allowed
        if unknown:
            raise ConfigError(f'unknown configuration field: {sorted(unknown)[0]}')
        required = {'simc_path'}
        missing = required - set(values)
        if missing:
            raise ConfigError(f'missing configuration field: {sorted(missing)[0]}')
        string_fields = {
            'server_url', 'backend_identifier', 'simc_path', 'token_path',
            'enrollment_token', 'name', 'platform', 'host_identifier', 'repository_path',
        }
        for field in string_fields & set(values):
            if type(values[field]) is not str:
                raise ConfigError(f'{field} must be a string')
        for field in ('allow_insecure_http', 'auto_update'):
            if field in values and type(values[field]) is not bool:
                raise ConfigError(f'{field} must be a boolean')
        for field in ('poll_interval_seconds', 'request_timeout_seconds', 'max_run_seconds'):
            if field in values:
                value = values[field]
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ConfigError(f'{field} must be a positive finite number')
        config = cls(**values)
        if not config.token_path:
            config = cls(**{
                **config.__dict__,
                'token_path': str(Path.home() / '.local/state/lmonitor-simc-agent/agent.token'),
            })
        parsed = urlparse(config.server_url)
        if parsed.scheme not in ({'https'} if not config.allow_insecure_http else {'http', 'https'}):
            raise ConfigError('server_url must use HTTPS (or explicitly enable allow_insecure_http)')
        if not parsed.netloc or parsed.query or parsed.fragment:
            raise ConfigError('server_url is invalid')
        if len(config.backend_identifier) > 64:
            raise ConfigError('backend_identifier is invalid')
        if not Path(config.simc_path).is_file() or not os.access(config.simc_path, os.X_OK):
            raise ConfigError('simc_path must be an executable file')
        host = config.host_identifier or _stable_host_identifier()
        if len(host) < 32 or len(host) > 128 or any(ch not in '0123456789abcdef' for ch in host):
            raise ConfigError('host_identifier must be 32-128 lowercase hexadecimal characters')
        return cls(**{**config.__dict__, 'server_url': config.server_url.rstrip('/'),
                      'host_identifier': host,
                      'platform': config.platform or platform_module.system().lower()})

    @classmethod
    def load(cls, path: str) -> 'AgentConfig':
        try:
            raw = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f'cannot load configuration: {exc}') from exc
        if not isinstance(raw, dict):
            raise ConfigError('configuration root must be a JSON object')
        if 'token_path' not in raw:
            raw['token_path'] = str(Path(path).resolve().with_suffix('.token'))
        return cls.from_dict(raw)


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects instead of forwarding a credentialed request."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class HTTPTransport:
    def __init__(self, server_url: str, timeout: float):
        self.server_url = server_url.rstrip('/') + '/'
        self.timeout = timeout
        self._opener = build_opener(_NoRedirectHandler())

    def _request(self, request: Request) -> bytes | None:
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status == 204:
                    return None
                return response.read()
        except HTTPError as exc:
            detail = exc.read(65536).decode('utf-8', errors='replace')
            try:
                parsed_detail = json.loads(detail)
            except (json.JSONDecodeError, TypeError):
                parsed_detail = None
            details = parsed_detail if isinstance(parsed_detail, dict) else {}
            message = details.get('error') if isinstance(details.get('error'), str) else detail
            raise APIError(
                f'control plane returned HTTP {exc.code}: {message}', exc.code, details,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise APIError(f'control plane request failed: {exc}') from exc

    def json(self, *, path: str, payload: dict[str, Any], authorization: str) -> dict[str, Any] | None:
        body = json.dumps(payload, separators=(',', ':'), ensure_ascii=False,
                          allow_nan=False).encode('utf-8')
        request = Request(urljoin(self.server_url, path.lstrip('/')), data=body, method='POST', headers={
            'Authorization': authorization, 'Content-Type': 'application/json',
            'Accept': 'application/json', 'User-Agent': f'LMonitor-SimC-Agent/{VERSION}',
        })
        raw = self._request(request)
        if raw is None:
            return None
        try:
            result = json.loads(raw.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise APIError('control plane returned malformed JSON') from exc
        if not isinstance(result, dict):
            raise APIError('control plane returned a non-object JSON response')
        return result

    def put_bytes(self, *, url: str, body: bytes, headers: dict[str, str]) -> None:
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password
                or parsed.fragment or not parsed.hostname):
            raise APIError('control plane returned an unsafe OSS upload URL')
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address and (address.is_private or address.is_loopback or address.is_link_local
                        or address.is_reserved or address.is_unspecified):
            raise APIError('control plane returned an unsafe OSS upload URL')
        forbidden_headers = {
            'authorization', 'cookie', 'proxy-authorization', 'host', 'content-length',
            'transfer-encoding', 'connection', 'proxy-connection', 'upgrade', 'te', 'trailer',
        }
        if type(headers) is not dict or any(
            type(key) is not str or type(value) is not str
            or key.lower() in forbidden_headers
            for key, value in headers.items()
        ):
            raise APIError('control plane returned unsafe OSS upload headers')
        request = Request(url, data=body, method='PUT', headers={
            **headers, 'User-Agent': f'LMonitor-SimC-Agent/{VERSION}',
        })
        self._request(request)


class SimcAgentConsumer:
    def __init__(self, config: AgentConfig, transport: HTTPTransport | None = None):
        self.config = config
        self.transport = transport or HTTPTransport(config.server_url, config.request_timeout_seconds)
        self.instance_id = uuid.uuid4().hex
        self.agent_token = self._read_token()
        self.heartbeat_interval = 30.0
        self.lease_seconds = 90.0
        self.stop_event = threading.Event()

    @property
    def authorization(self) -> str:
        if not self.agent_token:
            raise ConfigError('agent has not been registered')
        return 'Bearer ' + self.agent_token

    def _read_token(self) -> str:
        path = Path(self.config.token_path)
        try:
            if path.is_symlink():
                raise ConfigError('token file must not be a symbolic link')
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            try:
                file_stat = os.fstat(fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ConfigError('token file must be a regular file')
                if stat.S_IMODE(file_stat.st_mode) & 0o077:
                    raise ConfigError('token file permissions must be 0600 or stricter')
                with os.fdopen(fd, 'r', encoding='ascii') as token_file:
                    fd = -1
                    return token_file.read().strip()
            finally:
                if fd >= 0:
                    os.close(fd)
        except FileNotFoundError:
            return ''
        except (OSError, UnicodeError) as exc:
            raise ConfigError(f'cannot read token file: {exc}') from exc

    def _save_token(self, token: str) -> None:
        path = Path(self.config.token_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ConfigError('token file must not be a symbolic link')
        fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            encoded = token.encode('ascii')
            written = 0
            while written < len(encoded):
                count = os.write(fd, encoded[written:])
                if count <= 0:
                    raise OSError('short write while saving token')
                written += count
            os.fsync(fd)
            os.close(fd)
            fd = -1
            if path.is_symlink():
                raise ConfigError('token file must not be a symbolic link')
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _report(self, status: str = 'online') -> dict[str, Any]:
        return {
            'status': status, 'platform': self.config.platform,
            'agent_version': VERSION, 'protocol_version': PROTOCOL_VERSION,
            'capabilities': {'max_concurrent_runs': 1},
            'instance_id': self.instance_id, 'current_version': '',
            'binary_available': True,
        }

    def register(self) -> None:
        payload = {**self._report(), 'host_identifier': self.config.host_identifier,
                   'name': self.config.name}
        if self.config.backend_identifier:
            payload['backend_identifier'] = self.config.backend_identifier
        payload.pop('status')
        authorization = self.authorization if self.agent_token else 'Enrollment ' + self.config.enrollment_token
        if not self.agent_token and not self.config.enrollment_token:
            raise ConfigError('enrollment_token is required for first registration')
        response = self.transport.json(path='/api/simc-agent/v1/register/', payload=payload,
                                       authorization=authorization)
        if not response:
            raise APIError('registration returned an empty response')
        issued = response.get('agent_token')
        if issued is not None:
            if self.agent_token:
                raise APIError('control plane unexpectedly rotated an existing token')
            if type(issued) is not str or not issued:
                raise APIError('registration returned an invalid agent token')
            self._save_token(issued)
            self.agent_token = issued
        self.heartbeat_interval = self._positive_number(
            response.get('heartbeat_interval_seconds', 30), 'heartbeat_interval_seconds')
        self.lease_seconds = self._positive_number(response.get('lease_seconds', 90), 'lease_seconds')

    @staticmethod
    def _positive_number(value: Any, name: str) -> float:
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise APIError(f'{name} must be a positive finite number')
        return float(value)

    def heartbeat(self, status: str = 'online') -> None:
        self.transport.json(path='/api/simc-agent/v1/heartbeat/', payload=self._report(status),
                            authorization=self.authorization)

    def claim(self) -> dict[str, Any] | None:
        return self.transport.json(path='/api/simc-agent/v1/jobs/claim/',
                                   payload={
                                       'instance_id': self.instance_id,
                                       'agent_version': VERSION,
                                       'protocol_version': PROTOCOL_VERSION,
                                   },
                                   authorization=self.authorization)

    def _self_update(self, required_version: Any) -> None:
        if not self.config.auto_update:
            raise APIError(
                f'agent {VERSION} must be updated to {required_version}; automatic updates are disabled',
            )
        if (type(required_version) is not str
                or not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z._+-]{0,63}', required_version)):
            raise APIError('control plane returned an invalid required agent version')
        repository = Path(
            self.config.repository_path or Path(__file__).resolve().parent
        ).expanduser().resolve()
        target = repository / 'simc_agent_consumer.py'
        if (not repository.is_dir() or not (repository / '.git').exists()
                or not target.is_file() or target.is_symlink()):
            raise APIError(
                'automatic update requires a Git checkout containing simc_agent_consumer.py; '
                'set repository_path or update the Agent manually',
            )
        if Path(__file__).resolve() != target.resolve():
            raise APIError(
                'automatic update refused because the running Agent is not the managed '
                'simc_agent_consumer.py checkout entry point',
            )

        def git(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
            try:
                result = subprocess.run(
                    ['git', '-C', str(repository), *arguments],
                    capture_output=True, text=True, timeout=timeout, check=False,
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise APIError(f'automatic Agent update failed: {exc}') from exc
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                raise APIError(f'automatic Agent update failed: {detail or "git exited unsuccessfully"}')
            return result

        status = git('status', '--porcelain', '--untracked-files=all', timeout=30)
        if status.stdout.strip():
            raise APIError('automatic Agent update refused because the Git checkout has local changes')
        remote_url = git('config', '--get', 'remote.origin.url', timeout=30).stdout.strip()
        if remote_url not in TRUSTED_REPOSITORY_URLS:
            raise APIError('automatic Agent update refused because origin is not the trusted repository')
        branch = git('branch', '--show-current', timeout=30).stdout.strip()
        if branch != UPDATE_BRANCH:
            raise APIError(f'automatic Agent update requires the {UPDATE_BRANCH} branch')
        print(f'[simc-agent] updating {VERSION} -> {required_version}', flush=True)
        git('pull', '--ff-only', 'origin', UPDATE_BRANCH, timeout=120)
        try:
            source = target.read_text(encoding='utf-8')
        except (OSError, UnicodeError) as exc:
            raise APIError(f'cannot verify updated Agent: {exc}') from exc
        match = re.search(r"^VERSION\s*=\s*['\"]([^'\"]+)['\"]\s*$", source, re.MULTILINE)
        if not match or match.group(1) != required_version:
            actual = match.group(1) if match else 'unknown'
            raise APIError(
                f'Git update completed but Agent version is {actual}, expected {required_version}',
            )
        print('[simc-agent] update verified; restarting', flush=True)
        try:
            os.execv(sys.executable, [sys.executable, str(target), *sys.argv[1:]])
        except OSError as exc:
            raise APIError(f'updated Agent could not restart: {exc}') from exc

    @staticmethod
    def _stop_process(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
            except OSError:
                pass

    @staticmethod
    def _lease_deadline(value: Any) -> float:
        if type(value) is not str or not value:
            raise APIError('lease_expires_at must be a timezone-aware timestamp')
        try:
            expires = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if expires.tzinfo is None:
                raise ValueError('timestamp has no timezone')
            remaining = expires.timestamp() - time.time()
        except (OverflowError, OSError, ValueError) as exc:
            raise APIError('lease_expires_at must be a timezone-aware timestamp') from exc
        if not math.isfinite(remaining) or remaining <= 0:
            raise APIError('claim lease has already expired')
        return time.monotonic() + remaining

    def _lease_heartbeat_loop(self, job: dict[str, Any], stopped: threading.Event,
                              lease_lost: threading.Event,
                              process: subprocess.Popen[Any], deadline: float) -> None:
        interval = max(1.0, min(self.heartbeat_interval, self.lease_seconds / 3))
        delay = min(interval, max(0.0, deadline - time.monotonic()))
        while not stopped.wait(delay):
            if time.monotonic() >= deadline:
                lease_lost.set()
                self._stop_process(process)
                return
            try:
                response = self.transport.json(
                    path=f"/api/simc-agent/v1/jobs/{job['run_id']}/heartbeat/",
                    payload={'lease_token': job['lease_token'], 'instance_id': self.instance_id},
                    authorization=self.authorization,
                )
                if not response:
                    raise APIError('lease heartbeat returned an empty response')
                deadline = self._lease_deadline(response.get('lease_expires_at'))
                delay = min(interval, max(0.0, deadline - time.monotonic()))
            except Exception as exc:
                if isinstance(exc, APIError) and exc.status in (403, 404, 409):
                    lease_lost.set()
                    self._stop_process(process)
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    lease_lost.set()
                    self._stop_process(process)
                    return
                delay = min(1.0, remaining)

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode('utf-8', errors='replace')
        return value if isinstance(value, str) else str(value or '')

    def _completion_json(self, run_id: int, metadata: dict[str, Any]) -> bool:
        for attempt in range(COMPLETION_ATTEMPTS):
            try:
                self.transport.json(
                    path=f'/api/simc-agent/v1/jobs/{run_id}/complete/',
                    payload=metadata, authorization=self.authorization,
                )
                return True
            except Exception as exc:
                if (isinstance(exc, APIError) and exc.status is not None
                        and 400 <= exc.status < 500):
                    return False
                if attempt + 1 == COMPLETION_ATTEMPTS:
                    return False
                self.stop_event.wait(0.25 * (2 ** attempt))
        return False

    def _upload_report(self, run_id: int, lease_token: str,
                       report_bytes: bytes, report_name: str) -> dict[str, Any]:
        sha256 = hashlib.sha256(report_bytes).hexdigest()
        content_md5 = base64.b64encode(
            hashlib.md5(report_bytes, usedforsecurity=False).digest()
        ).decode('ascii')
        payload = {
            'lease_token': lease_token, 'instance_id': self.instance_id,
            'size': len(report_bytes), 'sha256': sha256, 'content_md5': content_md5,
        }
        last_error: Exception | None = None
        descriptor: dict[str, Any] | None = None
        for attempt in range(COMPLETION_ATTEMPTS):
            try:
                ticket = self.transport.json(
                    path=f'/api/simc-agent/v1/jobs/{run_id}/report-upload/',
                    payload=payload, authorization=self.authorization,
                )
                if not isinstance(ticket, dict):
                    raise APIError('control plane returned an invalid OSS upload ticket')
                object_key = ticket.get('object_key')
                expected_key = 'simc_agent_results/' + report_name
                if object_key != expected_key or ticket.get('method') != 'PUT':
                    raise APIError('control plane returned an invalid OSS report identity')
                url, headers = ticket.get('url'), ticket.get('headers')
                if type(url) is not str or type(headers) is not dict:
                    raise APIError('control plane returned an invalid OSS upload ticket')
                descriptor = {'object_key': object_key, 'size': len(report_bytes), 'sha256': sha256}
                self.transport.put_bytes(url=url, body=report_bytes, headers=headers)
                return descriptor
            except Exception as exc:
                last_error = exc
                if isinstance(exc, APIError) and exc.status in (403, 404):
                    break
                if attempt + 1 < COMPLETION_ATTEMPTS:
                    self.stop_event.wait(0.25 * (2 ** attempt))
        # A PUT response can be lost after OSS persisted the object. Returning the
        # descriptor lets completion's authoritative HEAD check resolve that case.
        if descriptor is not None:
            return descriptor
        raise APIError(f'OSS report upload failed: {last_error}')

    def _complete(self, run_id: int, lease_token: str, completion_id: str,
                  status: str, stdout: str, stderr: str,
                  report_bytes: bytes | None, report_name: str | None) -> None:
        report = None
        if status == 'completed':
            try:
                if report_bytes is None or not report_name:
                    raise APIError('completed SimC run has no report')
                report = self._upload_report(
                    run_id, lease_token, report_bytes, report_name,
                )
            except Exception as exc:
                status = 'failed'
                stderr = (stderr + f'\n{exc}').strip()

        metadata = {
            'lease_token': lease_token, 'instance_id': self.instance_id,
            'completion_id': completion_id, 'status': status,
            'stdout': _utf8_tail(stdout, COMPLETION_TEXT_MAX_BYTES),
            'stderr': _utf8_tail(stderr, COMPLETION_TEXT_MAX_BYTES),
            'report': report if status == 'completed' else None,
        }
        if self._completion_json(run_id, metadata):
            return
        # A completed completion may have committed even when its response was lost.
        # Never race it with an opposite failed terminal state; leave an unresolved
        # Run to the authoritative lease-expiry/stale-recovery workflow.
        print('[simc-agent] completion result is uncertain after retries', flush=True)

    def execute_job(self, job: dict[str, Any]) -> None:
        # A valid run identity is the minimum needed to report malformed claims.
        if type(job) is not dict or type(job.get('run_id')) is not int or job['run_id'] <= 0:
            raise APIError('claim returned an invalid run_id')
        run_id = job['run_id']
        lease_token = job.get('lease_token')
        if type(lease_token) is not str or not lease_token:
            raise APIError('claim returned an invalid lease_token')
        completion_id = uuid.uuid4().hex
        stdout = ''
        stderr = ''
        report_bytes: bytes | None = None
        output_name: str | None = None
        status = 'failed'
        lease_lost = threading.Event()
        heartbeat_stop: threading.Event | None = None
        heartbeat_thread: threading.Thread | None = None

        try:
            output_name = job.get('output_filename')
            if type(output_name) is not str or Path(output_name).name != output_name:
                raise APIError('claim returned an unsafe output filename')
            suffix = f'_run_{run_id}.html'
            task_id_text = output_name.removeprefix('simc_task_').removesuffix(suffix)
            if (not output_name.startswith('simc_task_') or not output_name.endswith(suffix)
                    or not task_id_text.isdigit() or int(task_id_text) <= 0):
                raise APIError('claim returned an unsafe output filename')
            input_text = job.get('input')
            if type(input_text) is not str:
                raise APIError('claim input must be a string')
            expected_hash = job.get('input_hash')
            if (type(expected_hash) is not str or len(expected_hash) != 64
                    or any(ch not in '0123456789abcdef' for ch in expected_hash)):
                raise APIError('claim input_hash must be 64 lowercase hexadecimal characters')
            actual_hash = hashlib.sha256(input_text.encode('utf-8')).hexdigest()
            if expected_hash != actual_hash:
                raise APIError('claim input hash mismatch')
            timeout = min(
                self._positive_number(job.get('timeout_seconds'), 'timeout_seconds'),
                self.config.max_run_seconds,
            )
            lease_deadline = self._lease_deadline(job.get('lease_expires_at'))

            with tempfile.TemporaryDirectory(prefix=f'simc-agent-run-{run_id}-') as work:
                work_path = Path(work)
                input_path = work_path / f'run-{run_id}.simc'
                input_path.write_text(input_text, encoding='utf-8')
                process = subprocess.Popen(
                    [self.config.simc_path, input_path.name], cwd=work,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                heartbeat_stop = threading.Event()
                heartbeat_thread = threading.Thread(
                    target=self._lease_heartbeat_loop,
                    args=(job, heartbeat_stop, lease_lost, process, lease_deadline),
                    name=f'simc-lease-{run_id}', daemon=True,
                )
                heartbeat_thread.start()
                try:
                    raw_stdout, raw_stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self._stop_process(process)
                    raw_stdout, raw_stderr = process.communicate()
                    raw_stderr = self._text(raw_stderr) + '\nSimC execution timed out'
                stdout, stderr = self._text(raw_stdout), self._text(raw_stderr)
                if lease_lost.is_set():
                    return  # Never complete work after losing its fencing lease.

                report_path = work_path / output_name
                if process.returncode == 0 and report_path.is_file():
                    size = report_path.stat().st_size
                    if size > MAX_REPORT_BYTES:
                        stderr = (stderr + '\nSimC report exceeds the 20 MiB client limit').strip()
                    else:
                        report_bytes = report_path.read_bytes()
                        if len(report_bytes) > MAX_REPORT_BYTES:
                            report_bytes = None
                            stderr = (stderr + '\nSimC report exceeds the 20 MiB client limit').strip()
                        elif report_bytes.lstrip().lower().startswith((b'<!doctype html', b'<html')):
                            status = 'completed'
                        else:
                            report_bytes = None
                            stderr = (stderr + '\nSimC report is not HTML').strip()
                elif process.returncode == 0:
                    stderr = (stderr + '\nSimC did not create the requested HTML report').strip()
        except Exception as exc:
            stderr = (stderr + f'\nSimC agent error: {exc}').strip()

        try:
            self._complete(run_id, lease_token, completion_id, status, stdout, stderr,
                           report_bytes, output_name)
        finally:
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=3)

    def run(self, once: bool = False) -> None:
        self.register()
        while not self.stop_event.is_set():
            try:
                self.heartbeat('online')
                job = self.claim()
                if job:
                    self.heartbeat('busy')
                    self.execute_job(job)
                    self.heartbeat('online')
                elif once:
                    return
            except APIError as exc:
                print(f'[simc-agent] {exc}', flush=True)
                if exc.status == 426 and exc.details.get('code') == 'agent_update_required':
                    self._self_update(exc.details.get('required_version'))
                if once:
                    raise
            if once:
                return
            self.stop_event.wait(self.config.poll_interval_seconds)

    def stop(self, *_args: Any) -> None:
        self.stop_event.set()


def main() -> int:
    parser = argparse.ArgumentParser(description='Standalone LMonitor SimC Agent')
    parser.add_argument('--config', required=True, help='Path to agent JSON configuration')
    parser.add_argument('--once', action='store_true', help='Claim at most one Run and exit')
    args = parser.parse_args()
    try:
        consumer = SimcAgentConsumer(AgentConfig.load(args.config))
        signal.signal(signal.SIGINT, consumer.stop)
        signal.signal(signal.SIGTERM, consumer.stop)
        consumer.run(once=args.once)
    except (ConfigError, APIError) as exc:
        print(f'[simc-agent] fatal: {exc}', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
