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
import logging
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
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = '1.3.0'
PROTOCOL_VERSION = 1
MAX_REPORT_BYTES = 20 * 1024 * 1024
COMPLETION_TEXT_MAX_BYTES = 256 * 1024
COMPLETION_ATTEMPTS = 3
TRUSTED_REPOSITORY_URLS = {
    'git@github.com:LoRexxar/LMonitor.git',
    'https://github.com/LoRexxar/LMonitor.git',
}
UPDATE_BRANCH = 'master'
TRUSTED_SIMC_REPOSITORY_URLS = {
    'git@github.com:simulationcraft/simc.git',
    'https://github.com/simulationcraft/simc.git',
}
LOGGER_NAME = 'lmonitor.simc_agent'


def configure_logging(log_path: str, *, max_bytes: int = 10 * 1024 * 1024,
                      backup_count: int = 5) -> logging.Logger:
    """Create a process-local rotating log without exposing Agent credentials."""
    path = Path(log_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    file_handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8',
    )
    os.chmod(path, 0o600)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


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
    simc_source_path: str = ''
    auto_update_simc: bool = True
    simc_update_interval_seconds: float = 1800.0
    simc_compile_threads: int = 2
    log_path: str = ''

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
            'simc_source_path', 'log_path',
        }
        for field in string_fields & set(values):
            if type(values[field]) is not str:
                raise ConfigError(f'{field} must be a string')
        for field in ('allow_insecure_http', 'auto_update', 'auto_update_simc'):
            if field in values and type(values[field]) is not bool:
                raise ConfigError(f'{field} must be a boolean')
        for field in ('poll_interval_seconds', 'request_timeout_seconds', 'max_run_seconds',
                      'simc_update_interval_seconds'):
            if field in values:
                value = values[field]
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ConfigError(f'{field} must be a positive finite number')
        if ('simc_compile_threads' in values
                and (type(values['simc_compile_threads']) is not int
                     or values['simc_compile_threads'] < 1 or values['simc_compile_threads'] > 64)):
            raise ConfigError('simc_compile_threads must be an integer between 1 and 64')
        config = cls(**values)
        if not config.simc_source_path:
            inferred_source = Path(config.simc_path).expanduser().resolve().parent.parent
            if (inferred_source / '.git').exists():
                config = cls(**{**config.__dict__, 'simc_source_path': str(inferred_source)})
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
        if ((not Path(config.simc_path).is_file() or not os.access(config.simc_path, os.X_OK))
                and not config.simc_source_path):
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
        if 'log_path' not in raw:
            raw['log_path'] = str(Path(path).resolve().with_suffix('.log'))
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
        self.logger = logging.getLogger(LOGGER_NAME)
        self._last_simc_check = 0.0
        self._lease_block_until = 0.0

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
        binary = Path(self.config.simc_path)
        binary_available = binary.is_file() and os.access(binary, os.X_OK)
        current_version = ''
        marker = Path(str(binary) + '.lmonitor-build.json')
        if binary_available:
            try:
                metadata = json.loads(marker.read_text(encoding='utf-8'))
                revision = metadata.get('revision') if isinstance(metadata, dict) else None
                if isinstance(revision, str) and re.fullmatch(r'[0-9a-fA-F]{7,64}', revision):
                    current_version = revision.lower()
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        return {
            'status': status, 'platform': self.config.platform,
            'agent_version': VERSION, 'protocol_version': PROTOCOL_VERSION,
            'capabilities': {'max_concurrent_runs': 1},
            'instance_id': self.instance_id, 'current_version': current_version,
            'binary_available': binary_available,
        }

    @staticmethod
    def _command(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False,
                stdin=subprocess.DEVNULL,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise APIError(f'command failed: {exc}') from exc
        if result.returncode != 0:
            detail = _utf8_tail((result.stderr or result.stdout).strip(), 4000)
            raise APIError(f'command failed ({command[0]}): {detail or "unsuccessful exit"}')
        return result

    def _maintain_simc(self, *, force: bool = False,
                       required_revision: str | None = None) -> bool:
        """Check upstream and build a verified replacement while no Run lease is held."""
        if time.monotonic() < self._lease_block_until:
            self.logger.info('skipping SimC maintenance while a Run lease may still be live')
            return False
        if not self.config.auto_update_simc or not self.config.simc_source_path:
            return False
        now = time.monotonic()
        if not force and now - self._last_simc_check < self.config.simc_update_interval_seconds:
            return False
        self._last_simc_check = now
        binary_entry = Path(self.config.simc_path).expanduser()
        if binary_entry.is_symlink():
            raise APIError('SimC automatic update refused for a symlink binary entry')
        source_entry = Path(self.config.simc_source_path).expanduser()
        if source_entry.is_symlink():
            raise APIError('SimC automatic update refused for a symlink source checkout')
        source = source_entry.resolve()
        if not source.is_dir() or not (source / '.git').exists() or (source / '.git').is_symlink():
            raise APIError('SimC automatic update requires a local Git checkout')

        def git(*arguments: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
            return self._command(['git', '-C', str(source), *arguments], timeout=timeout)

        if git('status', '--porcelain').stdout.strip():
            raise APIError('SimC automatic update refused because the source working tree is not clean')
        remote = git('config', '--get', 'remote.origin.url').stdout.strip()
        if remote not in TRUSTED_SIMC_REPOSITORY_URLS:
            raise APIError('SimC automatic update refused because origin is not trusted')
        branch = git('branch', '--show-current').stdout.strip()
        if not re.fullmatch(r'[0-9A-Za-z._/-]{1,100}', branch) or branch.startswith(('/', '-')):
            raise APIError('SimC automatic update requires a named safe branch')
        git('fetch', '--prune', 'origin', branch, timeout=300)
        local_revision = git('rev-parse', 'HEAD').stdout.strip().lower()
        upstream_revision = git('rev-parse', f'origin/{branch}').stdout.strip().lower()
        if not re.fullmatch(r'[0-9a-f]{7,64}', upstream_revision):
            raise APIError('SimC upstream returned an invalid revision')

        target_revision = upstream_revision
        if required_revision is not None:
            target_revision = str(required_revision).strip().lower()
            if not re.fullmatch(r'[0-9a-f]{40}', target_revision):
                raise APIError('control plane returned an invalid required SimC revision')
            try:
                git('merge-base', '--is-ancestor', target_revision, f'origin/{branch}')
            except APIError as exc:
                raise APIError(
                    'required SimC revision is not available on the trusted upstream branch',
                ) from exc

        report = self._report()
        if (local_revision == target_revision and report['binary_available']
                and report['current_version'] == target_revision):
            self.logger.info('SimC is current at %s', target_revision)
            return False
        if local_revision != target_revision:
            self.logger.info('updating SimC source %s -> %s', local_revision, target_revision)
            if required_revision is None:
                git('pull', '--ff-only', 'origin', branch, timeout=300)
            else:
                # The control plane freezes Tasks against an exact SimC commit. A
                # clean managed checkout may therefore need to move forward or
                # backward within the trusted branch instead of following HEAD.
                git('reset', '--hard', target_revision, timeout=300)
        revision = git('rev-parse', 'HEAD').stdout.strip().lower()
        if revision != target_revision:
            raise APIError('SimC source update did not reach the required revision')

        target = Path(self.config.simc_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        self.logger.info('compiling SimC revision %s', revision)
        with tempfile.TemporaryDirectory(prefix='.lmonitor-simc-build-', dir=str(source)) as build:
            self._command([
                'cmake', '-S', str(source), '-B', build, '-G', 'Ninja',
                '-DBUILD_GUI=OFF', '-DCMAKE_BUILD_TYPE=Release',
                '-DCMAKE_CXX_FLAGS_RELEASE=-O1 -DNDEBUG',
            ], timeout=300)
            self._command([
                'cmake', '--build', build, '--target', 'simc', '-j',
                str(self.config.simc_compile_threads),
            ], timeout=3600)
            candidate = Path(build) / 'simc'
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise APIError('SimC build did not produce an executable binary')
            probe = self._command([str(candidate), '--version'], timeout=30)
            if 'SimulationCraft' not in (probe.stdout + probe.stderr):
                raise APIError('compiled SimC binary failed its version probe')
            fd, temporary = tempfile.mkstemp(prefix=f'.{target.name}.', dir=str(target.parent))
            try:
                with os.fdopen(fd, 'wb') as output, candidate.open('rb') as source_binary:
                    while True:
                        chunk = source_binary.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o755)
                os.replace(temporary, target)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        marker = Path(str(target) + '.lmonitor-build.json')
        marker_tmp = marker.with_name(f'.{marker.name}.{uuid.uuid4().hex}')
        marker_tmp.write_text(json.dumps({'revision': revision}), encoding='utf-8')
        os.chmod(marker_tmp, 0o600)
        os.replace(marker_tmp, marker)
        self.logger.info('SimC revision %s compiled and activated', revision)
        return True

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
        self.logger.info('updating Agent %s -> %s', VERSION, required_version)
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
        self.logger.info('Agent update verified; restarting')
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
        # Never race it with an opposite failed terminal state or update binaries
        # before the lease expires; the run loop enters its conservative lease fence.
        raise APIError('completion result is uncertain after retries')

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

    def _maintain_simc_with_heartbeats(self, *, force: bool = False,
                                       required_revision: str | None = None) -> bool:
        stopped = threading.Event()

        def report_while_building() -> None:
            while not stopped.wait(max(1.0, self.heartbeat_interval)):
                try:
                    self.heartbeat('degraded')
                except Exception as exc:
                    self.logger.warning('status heartbeat failed during SimC maintenance: %s', exc)

        thread = threading.Thread(
            target=report_while_building, name='simc-maintenance-heartbeat', daemon=True,
        )
        thread.start()
        try:
            return self._maintain_simc(
                force=force, required_revision=required_revision,
            )
        finally:
            stopped.set()
            thread.join(timeout=2)

    def run(self, once: bool = False) -> None:
        self.register()
        self.logger.info(
            'Agent %s registered; instance=%s backend=%s',
            VERSION, self.instance_id, self.config.backend_identifier or 'enrollment-bound',
        )
        while not self.stop_event.is_set():
            lease_wait = self._lease_block_until - time.monotonic()
            if lease_wait > 0:
                try:
                    self.heartbeat('busy')
                except Exception as exc:
                    self.logger.warning('status heartbeat failed while waiting for lease expiry: %s', exc)
                if once:
                    return
                self.stop_event.wait(min(self.config.poll_interval_seconds, lease_wait))
                continue
            maintenance_error = None
            try:
                self._maintain_simc_with_heartbeats()
            except Exception as exc:
                maintenance_error = exc
                self.logger.exception('SimC maintenance failed: %s', exc)
            try:
                self.heartbeat('degraded' if maintenance_error else 'online')
                job = self.claim()
                if job:
                    self.logger.info('claimed Task %s Run %s', job.get('task_id'), job.get('run_id'))
                    self.heartbeat('busy')
                    try:
                        self.execute_job(job)
                    except Exception:
                        self._lease_block_until = time.monotonic() + self.lease_seconds
                        raise
                    else:
                        self._lease_block_until = 0.0
                    self.logger.info('finished Task %s Run %s', job.get('task_id'), job.get('run_id'))
                    self.heartbeat('online')
                elif once:
                    return
            except APIError as exc:
                self.logger.error('control-plane operation failed: %s', exc)
                if exc.status == 426 and exc.details.get('code') == 'agent_update_required':
                    self._self_update(exc.details.get('required_version'))
                if exc.status == 409 and exc.details.get('code') == 'simc_update_required':
                    self._maintain_simc_with_heartbeats(
                        force=True,
                        required_revision=exc.details.get('required_version'),
                    )
                if once:
                    raise
            except Exception:
                self.logger.exception('unexpected Agent runtime failure')
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
        config = AgentConfig.load(args.config)
        log_path = config.log_path or str(
            Path(config.token_path).with_name('simc-agent.log')
        )
        logger = configure_logging(log_path)
        consumer = SimcAgentConsumer(config)
        signal.signal(signal.SIGINT, consumer.stop)
        signal.signal(signal.SIGTERM, consumer.stop)
        consumer.run(once=args.once)
    except (ConfigError, APIError) as exc:
        active_logger = logging.getLogger(LOGGER_NAME)
        if active_logger.handlers:
            active_logger.exception('fatal Agent error: %s', exc)
        else:
            print(f'[simc-agent] fatal: {exc}', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
