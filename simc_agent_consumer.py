#!/usr/bin/env python3
"""Standalone SimC execution agent.

Requires only Python 3 and a SimulationCraft binary. It never imports Django or
opens the LMonitor database; all coordination goes through the control-plane API.
"""
from __future__ import annotations

import argparse
import base64
import shutil
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = '1.4.0'
PROTOCOL_VERSION = 1
MAX_REPORT_BYTES = 20 * 1024 * 1024
COMPLETION_TEXT_MAX_BYTES = 256 * 1024
# A short foreground retry only covers a brief response loss.  Durable delivery
# is handled by the local completion outbox before the Agent can claim again.
COMPLETION_ATTEMPTS = 3
COMPLETION_RETRY_DELAY_SECONDS = 0.25
COMPLETION_RETRY_MAX_DELAY_SECONDS = 2.0
TRUSTED_REPOSITORY_URLS = {
    'git@github.com:LoRexxar/LMonitor.git',
    'https://github.com/LoRexxar/LMonitor.git',
}
# LMonitor itself is updated from master. SimC is a separate checkout and its
# production branch is midnight; never infer this from the checkout's current
# branch (an old agent may have been initialized on master).
UPDATE_BRANCH = 'master'
SIMC_UPDATE_BRANCH = 'midnight'
TRUSTED_SIMC_REPOSITORY_URLS = {
    'git@github.com:simulationcraft/simc.git',
    'https://github.com/simulationcraft/simc.git',
}
# SimC's HTML report renderer itself requires an installed ``en_US`` locale.
# Do not overwrite the host locale here: setting ``LC_ALL=C`` makes the renderer
# attempt unavailable POSIX ``en_US`` locale names and fail before producing HTML.
LOGGER_NAME = 'lmonitor.simc_agent'
SIMC_HTML_REPORT_SOURCE = Path('engine') / 'report' / 'report_html_sim.cpp'
SIMC_HTML_LOCALE_PATCH_VERSION = 1
SIMC_HTML_LOCALE_FALLBACK = '''  catch ( const std::runtime_error& )
  {
    // backup spelling for CI
    try
    {
      std::locale::global( std::locale( "en_US.utf8" ) );
    }
    catch ( const std::runtime_error& )
    {
      std::locale::global( std::locale::classic() );
    }
  }'''


def prepare_simc_html_build_source(source: Path, build_source: Path) -> None:
    """Copy SimC source and patch the renderer only when its source is present.

    Production SimC checkouts contain the HTML renderer.  Treat an absent source
    as an older/alternate layout so the maintenance path retains its existing
    build behavior; the isolated locale workaround is simply unavailable there.
    """
    shutil.copytree(source, build_source, symlinks=True, copy_function=shutil.copyfile)
    report_path = build_source / SIMC_HTML_REPORT_SOURCE
    if not report_path.is_file():
        return
    try:
        report = report_path.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        raise APIError(f'cannot read SimC HTML report renderer: {exc}') from exc
    original = '''  catch ( const std::runtime_error& )
  {
    // backup spelling for CI
    std::locale::global( std::locale( "en_US.utf8" ) );
  }'''
    if original not in report:
        raise APIError('SimC HTML locale fallback source is not recognized')
    report_path.write_text(report.replace(original, SIMC_HTML_LOCALE_FALLBACK, 1), encoding='utf-8')


def agent_revision(repository: Path | None = None) -> str:
    repository = (repository or Path(__file__).resolve().parent).expanduser().resolve()
    try:
        result = subprocess.run(
            ['git', '-C', str(repository), 'rev-parse', 'HEAD'],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ''
    revision = result.stdout.strip().lower()
    return revision if result.returncode == 0 and re.fullmatch(r'[0-9a-f]{40}', revision) else ''


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
    if not _is_windows():
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


def _is_windows() -> bool:
    return os.name == 'nt'


def _is_executable_regular_file(path: Path) -> bool:
    """Validate an execution target without applying POSIX mode bits on Windows."""
    return path.is_file() and (_is_windows() or os.access(path, os.X_OK))


def _simc_binary_name() -> str:
    return 'simc.exe' if _is_windows() else 'simc'


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry where the platform exposes POSIX directory fsync."""
    if _is_windows():
        return
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


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


def _default_agent_name(host_identifier: str) -> str:
    """Return a server-valid, stable display name without exposing host data."""
    return f'simc-agent-{host_identifier[:12]}'


def _discover_simc_source_path(simc_path: str) -> Path | None:
    """Find the Git checkout containing a configured build output, if any."""
    binary = Path(simc_path).expanduser()
    for candidate in (binary.parent, *binary.parent.parents):
        git_entry = candidate / '.git'
        if git_entry.exists() and not git_entry.is_symlink():
            return candidate
    return None


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
    # Preserve the existing one-Run behavior by default.  Higher values are
    # an explicit deployment choice and are advertised to the control plane.
    max_concurrent_runs: int = 1
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
        if ('max_concurrent_runs' in values
                and (type(values['max_concurrent_runs']) is not int
                     or values['max_concurrent_runs'] < 1 or values['max_concurrent_runs'] > 64)):
            raise ConfigError('max_concurrent_runs must be an integer between 1 and 64')
        config = cls(**values)
        # The execution entry must always be an explicit binary path.  A
        # missing file is allowed only when a source checkout is configured so
        # the first maintenance cycle can build it; an existing entry must be
        # a regular executable file (directories and symlinks are rejected).
        binary = Path(config.simc_path).expanduser()
        if binary.exists():
            if binary.is_symlink() or not _is_executable_regular_file(binary):
                raise ConfigError('simc_path must point to an executable SimC binary file')
        elif not config.simc_source_path:
            raise ConfigError('simc_path must point to an executable SimC binary file')
        if not config.token_path:
            state_dir = (Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
                         / 'LMonitorSimCAgent' if _is_windows()
                         else Path.home() / '.local/state/lmonitor-simc-agent')
            config = cls(**{
                **config.__dict__,
                'token_path': str(state_dir / 'agent.token'),
            })
        parsed = urlparse(config.server_url)
        if parsed.scheme not in ({'https'} if not config.allow_insecure_http else {'http', 'https'}):
            raise ConfigError('server_url must use HTTPS (or explicitly enable allow_insecure_http)')
        if not parsed.netloc or parsed.query or parsed.fragment:
            raise ConfigError('server_url is invalid')
        if len(config.backend_identifier) > 64:
            raise ConfigError('backend_identifier is invalid')
        host = config.host_identifier or _stable_host_identifier()
        if len(host) < 32 or len(host) > 128 or any(ch not in '0123456789abcdef' for ch in host):
            raise ConfigError('host_identifier must be 32-128 lowercase hexadecimal characters')
        return cls(**{**config.__dict__, 'server_url': config.server_url.rstrip('/'),
                      'host_identifier': host,
                      'name': config.name or _default_agent_name(host),
                      'platform': config.platform or platform_module.system().lower()})

    @classmethod
    def load(cls, path: str) -> 'AgentConfig':
        config_path = Path(path).expanduser().resolve()
        try:
            raw = json.loads(config_path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f'cannot load configuration: {exc}') from exc
        if not isinstance(raw, dict):
            raise ConfigError('configuration root must be a JSON object')
        if 'token_path' not in raw:
            raw['token_path'] = str(config_path.with_suffix('.token'))
        if 'log_path' not in raw:
            raw['log_path'] = str(config_path.with_suffix('.log'))
        if 'simc_source_path' not in raw:
            discovered_source = _discover_simc_source_path(str(raw.get('simc_path', '')))
            raw['simc_source_path'] = str(
                discovered_source or (config_path.parent / 'simc-source')
            )
        config = cls.from_dict(raw)
        # A first-run config intentionally contains only its two required
        # fields.  On each successful load, materialize every omitted example
        # field so operators can inspect and edit the effective settings.
        if set(raw) != set(cls.__dataclass_fields__):
            _write_private_json(config_path, config.__dict__)
        return config


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically replace an Agent-owned JSON config with private permissions."""
    if path.is_symlink() or not path.is_file():
        raise ConfigError('configuration path must be a regular file')
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
    try:
        if not _is_windows():
            os.fchmod(fd, 0o600)
        payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8') + b'\n'
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count <= 0:
                raise OSError('short write while saving configuration')
            written += count
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise ConfigError(f'cannot save configuration defaults: {exc}') from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def ensure_configuration(path: str, *, interactive: bool) -> bool:
    """Create the first-run minimal config only after collecting both required values.

    Returns True when a new file was created.  Non-interactive callers fail closed
    rather than blocking a service/Task Scheduler launch waiting for stdin.
    """
    config_path = Path(path).expanduser().resolve()
    if config_path.exists():
        if config_path.is_symlink() or not config_path.is_file():
            raise ConfigError('configuration path must be a regular file')
        return False
    if not interactive:
        raise ConfigError(
            f'configuration does not exist: {config_path}; create it from simc_agent.example.json '
            'or start interactively once to initialize it'
        )
    try:
        enrollment_token = input('首次注册验证码（enrollment_token，必填）：').strip()
        simc_path = input('SimC 可执行文件完整路径（simc_path，必填）：').strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise ConfigError('首次配置已取消，未写入配置文件') from exc
    if not enrollment_token:
        raise ConfigError('enrollment token is required; configuration was not written')
    if not simc_path:
        raise ConfigError('simc_path is required; configuration was not written')

    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{config_path.name}.', dir=str(config_path.parent))
    try:
        if not _is_windows():
            os.fchmod(fd, 0o600)
        payload = json.dumps(
            {'enrollment_token': enrollment_token, 'simc_path': simc_path},
            ensure_ascii=False, indent=2,
        ).encode('utf-8') + b'\n'
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count <= 0:
                raise OSError('short write while saving configuration')
            written += count
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if config_path.exists() or config_path.is_symlink():
            raise ConfigError('configuration appeared while initializing; refusing to overwrite it')
        os.replace(temporary, config_path)
        _fsync_directory(config_path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    print(
        '已生成最小配置。可选配置及说明见 simc_agent.example.json 和 docs/windows-simc-agent.md。',
        flush=True,
    )
    return True


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
        self._maintenance_slot_path = Path(self.config.token_path).with_name('simc-maintenance-slots.json')
        self._completed_maintenance_slots = self._load_completed_maintenance_slots()
        self._maintenance_policy: dict[str, Any] | None = None
        self._lease_block_until = 0.0
        self._active_jobs: dict[int, Future[None]] = {}
        self._active_jobs_lock = threading.Lock()
        self.completion_outbox_path = Path(self.config.token_path).with_name('completion-outbox')

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
                if not _is_windows() and stat.S_IMODE(file_stat.st_mode) & 0o077:
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
            if not _is_windows():
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
            _fsync_directory(path.parent)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _report(self, status: str = 'online') -> dict[str, Any]:
        binary = Path(self.config.simc_path)
        binary_available = _is_executable_regular_file(binary)
        marker_revision = ''
        html_locale_patch_version = 0
        marker = Path(str(binary) + '.lmonitor-build.json')
        if binary_available:
            try:
                metadata = json.loads(marker.read_text(encoding='utf-8'))
                revision = metadata.get('revision') if isinstance(metadata, dict) else None
                if isinstance(revision, str) and re.fullmatch(r'[0-9a-fA-F]{7,64}', revision):
                    marker_revision = revision.lower()
                patch_version = metadata.get('html_locale_patch_version') if isinstance(metadata, dict) else None
                if type(patch_version) is int and patch_version >= 0:
                    html_locale_patch_version = patch_version
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        return {
            'status': status, 'platform': self.config.platform,
            'agent_version': VERSION, 'agent_revision': agent_revision(), 'protocol_version': PROTOCOL_VERSION,
            'capabilities': {'max_concurrent_runs': self.config.max_concurrent_runs},
            'instance_id': self.instance_id, 'current_version': marker_revision,
            'binary_available': binary_available,
            'html_locale_patch_version': html_locale_patch_version,
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
        # `auto_update_simc` controls proactive periodic updates. An exact
        # revision demanded by the control plane is mandatory for claiming a
        # frozen Run and must not be silently ignored because periodic updates
        # were disabled.
        report = self._report()
        needs_html_locale_patch = (
            report['binary_available']
            and report['html_locale_patch_version'] < SIMC_HTML_LOCALE_PATCH_VERSION
        )
        if (not self.config.auto_update_simc and required_revision is None
                and not needs_html_locale_patch):
            return False
        if not self.config.simc_source_path:
            return False
        now = time.monotonic()
        if (not force and not needs_html_locale_patch
                and now - self._last_simc_check < self.config.simc_update_interval_seconds):
            return False
        self._last_simc_check = now
        binary_entry = Path(self.config.simc_path).expanduser()
        if binary_entry.is_symlink():
            raise APIError('SimC automatic update refused for a symlink binary entry')
        source_entry = Path(self.config.simc_source_path).expanduser()
        if source_entry.is_symlink():
            raise APIError('SimC automatic update refused for a symlink source checkout')
        source = source_entry.resolve()
        if not source.is_dir() or not (source / '.git').exists():
            if required_revision is None:
                raise APIError('SimC automatic update requires a local Git checkout')
            if source.exists() and (not source.is_dir() or any(source.iterdir())):
                raise APIError('managed SimC source path must be absent or empty')
            source.parent.mkdir(parents=True, exist_ok=True)
            self.logger.info('creating managed SimC source checkout at %s', source)
            self._command([
                'git', 'clone', '--branch', SIMC_UPDATE_BRANCH, '--single-branch',
                'https://github.com/simulationcraft/simc.git', str(source),
            ], timeout=600)
        if not (source / '.git').is_dir() or (source / '.git').is_symlink():
            raise APIError('SimC automatic update requires a local Git checkout')

        def git(*arguments: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
            return self._command(['git', '-C', str(source), *arguments], timeout=timeout)

        remote = git('config', '--get', 'remote.origin.url').stdout.strip()
        if remote not in TRUSTED_SIMC_REPOSITORY_URLS:
            raise APIError('SimC automatic update refused because origin is not trusted')
        branch = git('branch', '--show-current').stdout.strip()
        if branch != SIMC_UPDATE_BRANCH:
            raise APIError(
                f'SimC automatic update requires the {SIMC_UPDATE_BRANCH} branch',
            )
        git('fetch', '--prune', 'origin', SIMC_UPDATE_BRANCH, timeout=300)
        local_revision = git('rev-parse', 'HEAD').stdout.strip().lower()
        upstream_revision = git('rev-parse', f'origin/{SIMC_UPDATE_BRANCH}').stdout.strip().lower()
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
                and report['current_version'] == target_revision
                and report['html_locale_patch_version'] >= SIMC_HTML_LOCALE_PATCH_VERSION):
            self.logger.info('SimC is current at %s', target_revision)
            return False
        if local_revision != target_revision or required_revision is not None:
            self.logger.info('fast-forwarding SimC source %s -> %s', local_revision, target_revision)
            git('pull', '--ff-only', '--force', 'origin', SIMC_UPDATE_BRANCH, timeout=300)
        revision = git('rev-parse', 'HEAD').stdout.strip().lower()
        if revision != target_revision:
            raise APIError('SimC source update did not reach the required revision')

        target = Path(self.config.simc_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        self.logger.info('compiling SimC revision %s', revision)
        with tempfile.TemporaryDirectory(
            prefix='.lmonitor-simc-build-', dir=str(source.parent),
        ) as build:
            build_source = Path(build) / 'source'
            prepare_simc_html_build_source(source, build_source)
            self._command([
                'cmake', '-S', str(build_source), '-B', build, '-G', 'Ninja',
                '-DBUILD_GUI=OFF', '-DCMAKE_BUILD_TYPE=Release',
                '-DCMAKE_CXX_FLAGS_RELEASE=-O1 -DNDEBUG',
            ], timeout=300)
            self._command([
                'cmake', '--build', build, '--target', 'simc', '-j',
                str(self.config.simc_compile_threads),
            ], timeout=3600)
            candidate = Path(build) / _simc_binary_name()
            if not _is_executable_regular_file(candidate):
                raise APIError('SimC build did not produce an executable binary')
            probe = self._command([str(candidate)], timeout=30)
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
                if not _is_windows():
                    os.chmod(temporary, 0o755)
                os.replace(temporary, target)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        marker = Path(str(target) + '.lmonitor-build.json')
        marker_tmp = marker.with_name(f'.{marker.name}.{uuid.uuid4().hex}')
        marker_tmp.write_text(json.dumps({
            'revision': revision,
            'html_locale_patch_version': SIMC_HTML_LOCALE_PATCH_VERSION,
        }), encoding='utf-8')
        if not _is_windows():
            os.chmod(marker_tmp, 0o600)
        os.replace(marker_tmp, marker)
        self.logger.info('SimC revision %s compiled and activated', revision)
        return True

    def _load_completed_maintenance_slots(self) -> set[tuple[int, str]]:
        try:
            raw = json.loads(self._maintenance_slot_path.read_text(encoding='utf-8'))
            if not isinstance(raw, list):
                return set()
            return {
                (item[0], item[1]) for item in raw
                if isinstance(item, list) and len(item) == 2 and type(item[0]) is int
                and isinstance(item[1], str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', item[1])
            }
        except (OSError, UnicodeError, json.JSONDecodeError):
            return set()

    def _save_completed_maintenance_slots(self) -> None:
        self._maintenance_slot_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([list(slot) for slot in sorted(self._completed_maintenance_slots)[-32:]])
        temporary = self._maintenance_slot_path.with_name(
            f'.{self._maintenance_slot_path.name}.{uuid.uuid4().hex}'
        )
        try:
            temporary.write_text(payload, encoding='utf-8')
            if not _is_windows():
                os.chmod(temporary, 0o600)
            os.replace(temporary, self._maintenance_slot_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _set_maintenance_policy(self, response: dict[str, Any]) -> None:
        policy = response.get('agent_maintenance_policy')
        self._maintenance_policy = policy if isinstance(policy, dict) else None

    @staticmethod
    def _maintenance_task(response: dict[str, Any]) -> int | None:
        task = response.get('agent_maintenance_task')
        if (not isinstance(task, dict) or set(task) != {'id', 'action'}
                or type(task.get('id')) is not int or task['id'] <= 0
                or task.get('action') != 'update_simc'):
            return None
        return task['id']

    def _report_maintenance_task(self, task_id: int, status: str) -> None:
        self.transport.json(path=f'/api/simc-agent/v1/maintenance-tasks/{task_id}/',
                            payload={'status': status}, authorization=self.authorization)

    def _run_dispatched_maintenance(self, response: dict[str, Any]) -> None:
        task_id = self._maintenance_task(response)
        if task_id is None:
            return
        self._report_maintenance_task(task_id, 'running')
        try:
            self._maintain_simc_with_heartbeats(force=True)
        except Exception:
            try:
                self._report_maintenance_task(task_id, 'failed')
            except Exception:
                self.logger.exception('failed to report dispatched SimC maintenance failure')
            raise
        self._report_maintenance_task(task_id, 'success')

    @staticmethod
    def _maintenance_slot(policy: dict[str, Any], now: datetime) -> tuple[int, str] | None:
        if policy.get('enabled') is not True:
            return None
        if policy.get('timezone') != 'Asia/Shanghai':
            return None
        daily_time = policy.get('daily_time')
        window_minutes = policy.get('window_minutes')
        revision = policy.get('policy_revision')
        if (not isinstance(daily_time, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', daily_time)
                or type(window_minutes) is not int or not 1 <= window_minutes <= 180
                or type(revision) is not int or revision < 1):
            return None
        local = now.astimezone(ZoneInfo('Asia/Shanghai'))
        hour, minute = (int(part) for part in daily_time.split(':'))
        start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not start <= local < start + timedelta(minutes=window_minutes):
            return None
        return revision, start.date().isoformat()

    def _should_run_scheduled_maintenance(self, policy: dict[str, Any] | None = None,
                                          *, now: datetime | None = None) -> bool:
        slot = self._maintenance_slot(policy or self._maintenance_policy or {}, now or datetime.now().astimezone())
        return slot is not None and slot not in self._completed_maintenance_slots

    def _mark_scheduled_maintenance_complete(self, policy: dict[str, Any] | None = None,
                                             *, now: datetime | None = None) -> None:
        slot = self._maintenance_slot(policy or self._maintenance_policy or {}, now or datetime.now().astimezone())
        if slot is not None:
            self._completed_maintenance_slots.add(slot)
            # Policy revisions and dates are tiny, but keep this process-local
            # cache bounded for agents that run unattended for many months.
            if len(self._completed_maintenance_slots) > 64:
                self._completed_maintenance_slots = set(sorted(self._completed_maintenance_slots)[-32:])
            self._save_completed_maintenance_slots()

    def _run_scheduled_maintenance_if_due(self) -> None:
        if not self._should_run_scheduled_maintenance():
            return
        self._maintain_simc_with_heartbeats(force=True)
        self._mark_scheduled_maintenance_complete()

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
        self._set_maintenance_policy(response)

    @staticmethod
    def _positive_number(value: Any, name: str) -> float:
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise APIError(f'{name} must be a positive finite number')
        return float(value)

    def heartbeat(self, status: str = 'online', *, maintenance: str | None = None) -> dict[str, Any]:
        payload = self._report(status)
        if maintenance is not None:
            payload['capabilities']['maintenance'] = maintenance
        response = self.transport.json(path='/api/simc-agent/v1/heartbeat/', payload=payload,
                                       authorization=self.authorization)
        if isinstance(response, dict):
            self._set_maintenance_policy(response)
            return response
        return {}

    def claim(self) -> dict[str, Any] | None:
        return self.transport.json(path='/api/simc-agent/v1/jobs/claim/',
                                   payload={
                                       'instance_id': self.instance_id,
                                       'agent_version': VERSION,
                                       'agent_revision': agent_revision(),
                                       'protocol_version': PROTOCOL_VERSION,
                                   },
                                   authorization=self.authorization)

    def _self_update(self, required_version: Any = None, *, required_revision: Any = None) -> None:
        if required_revision is not None:
            if type(required_revision) is not str or not re.fullmatch(r'[0-9a-f]{40}', required_revision):
                raise APIError('control plane returned an invalid required Agent revision')
        elif (type(required_version) is not str
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

        remote_url = git('config', '--get', 'remote.origin.url', timeout=30).stdout.strip()
        if remote_url not in TRUSTED_REPOSITORY_URLS:
            raise APIError('automatic Agent update refused because origin is not the trusted repository')
        branch = git('branch', '--show-current', timeout=30).stdout.strip()
        if branch != UPDATE_BRANCH:
            raise APIError(f'automatic Agent update requires the {UPDATE_BRANCH} branch')
        # An Agent checkout is disposable code, but its operator may have left
        # tracked edits beside it.  Preserve tracked edits in Git's local stash
        # before replacing code.  Agent runtime state is intentionally untracked
        # and must stay in place: stashing with --include-untracked removes token,
        # config and completion outbox paths from the running directory, then
        # reset/re-exec starts without its persistent identity.
        changes = git('status', '--porcelain', '--untracked-files=no', timeout=30).stdout
        if changes.strip():
            self.logger.warning('preserving tracked local Agent checkout changes in a pre-update stash')
            git('stash', 'push', '--message', 'lmonitor-agent-pre-update', timeout=120)
        self.logger.info('replacing Agent checkout with required revision %s', required_revision or required_version)
        git('fetch', '--force', 'origin', UPDATE_BRANCH, timeout=120)
        if required_revision is not None:
            fetched_revision = git('rev-parse', f'origin/{UPDATE_BRANCH}', timeout=30).stdout.strip()
            if fetched_revision != required_revision:
                raise APIError(
                    f'Git fetch completed but origin/{UPDATE_BRANCH} is {fetched_revision or "unknown"}, '
                    f'expected {required_revision}',
                )
            git('reset', '--hard', required_revision, timeout=120)
        else:
            git('pull', '--ff-only', '--force', 'origin', UPDATE_BRANCH, timeout=120)
        try:
            source = target.read_text(encoding='utf-8')
        except (OSError, UnicodeError) as exc:
            raise APIError(f'cannot verify updated Agent: {exc}') from exc
        actual_revision = agent_revision(repository)
        if required_revision is not None and actual_revision != required_revision:
            raise APIError(
                f'Git update completed but Agent revision is {actual_revision or "unknown"}, expected {required_revision}',
            )
        match = re.search(r"^VERSION\s*=\s*['\"]([^'\"]+)['\"]\s*$", source, re.MULTILINE)
        if required_revision is None and (not match or match.group(1) != required_version):
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

    def _completion_outbox_file(self, run_id: int, completion_id: str) -> Path:
        if type(run_id) is not int or run_id <= 0 or not re.fullmatch(r'[0-9a-f]{32}', completion_id):
            raise APIError('completion outbox identity is invalid')
        return self.completion_outbox_path / f'run-{run_id}-{completion_id}.json'

    def _save_completion_outbox(self, run_id: int, metadata: dict[str, Any],
                                report_name: str | None = None,
                                report_bytes: bytes | None = None) -> None:
        """Durably preserve an unacknowledged terminal completion before idling."""
        completion_id = metadata.get('completion_id')
        if not isinstance(completion_id, str):
            raise APIError('completion outbox identity is invalid')
        path = self._completion_outbox_file(run_id, completion_id)
        if set(metadata) != {'lease_token', 'instance_id', 'completion_id', 'status', 'stdout', 'stderr', 'report'}:
            raise APIError('completion outbox payload is invalid')
        if (report_name is None) != (report_bytes is None):
            raise APIError('completion outbox report is invalid')
        if report_bytes is not None and (not report_name or len(report_bytes) > MAX_REPORT_BYTES):
            raise APIError('completion outbox report is invalid')
        self.completion_outbox_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.completion_outbox_path.is_symlink() or not self.completion_outbox_path.is_dir():
            raise APIError('completion outbox must be a directory')
        encoded = json.dumps(
            {
                'run_id': run_id, 'metadata': metadata, 'report_name': report_name,
                'report_bytes': (base64.b64encode(report_bytes).decode('ascii')
                                 if report_bytes is not None else None),
            }, separators=(',', ':'), ensure_ascii=False, allow_nan=False,
        ).encode('utf-8')
        fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(self.completion_outbox_path))
        try:
            if not _is_windows():
                os.fchmod(fd, 0o600)
            os.write(fd, encoded)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, path)
            _fsync_directory(self.completion_outbox_path)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _load_completion_outbox(self, path: Path) -> tuple[int, dict[str, Any], str | None, bytes | None]:
        if path.is_symlink() or not path.is_file():
            raise APIError('completion outbox entry must be a regular file')
        if not _is_windows() and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise APIError('completion outbox entry permissions must be 0600 or stricter')
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise APIError(f'cannot read completion outbox entry: {exc}') from exc
        if not isinstance(value, dict) or set(value) != {'run_id', 'metadata', 'report_name', 'report_bytes'}:
            raise APIError('completion outbox entry is invalid')
        run_id, metadata = value['run_id'], value['metadata']
        report_name, encoded_report = value['report_name'], value['report_bytes']
        if type(run_id) is not int or run_id <= 0 or not isinstance(metadata, dict):
            raise APIError('completion outbox entry is invalid')
        if (report_name is None) != (encoded_report is None) or (report_name is not None and not isinstance(report_name, str)):
            raise APIError('completion outbox report is invalid')
        report_bytes = None
        if encoded_report is not None:
            if not isinstance(encoded_report, str):
                raise APIError('completion outbox report is invalid')
            try:
                report_bytes = base64.b64decode(encoded_report.encode('ascii'), validate=True)
            except (UnicodeError, ValueError) as exc:
                raise APIError('completion outbox report is invalid') from exc
            if len(report_bytes) > MAX_REPORT_BYTES:
                raise APIError('completion outbox report is invalid')
        completion_id = metadata.get('completion_id')
        if not isinstance(completion_id, str):
            raise APIError('completion outbox entry is invalid')
        expected = self._completion_outbox_file(run_id, completion_id)
        if path.name != expected.name:
            raise APIError('completion outbox entry identity is invalid')
        return run_id, metadata, report_name, report_bytes

    def flush_completion_outbox(self) -> bool:
        """Try all durable completions before claiming new work; retain failures."""
        if not self.completion_outbox_path.exists():
            return True
        if self.completion_outbox_path.is_symlink() or not self.completion_outbox_path.is_dir():
            raise APIError('completion outbox must be a directory')
        delivered = True
        for path in sorted(self.completion_outbox_path.glob('*.json')):
            run_id, metadata, report_name, report_bytes = self._load_completion_outbox(path)
            if report_bytes is not None and metadata.get('report') is None:
                if report_name is None:
                    raise APIError('completion outbox report is invalid')
                try:
                    report = self._upload_report(
                        run_id, metadata['lease_token'], report_bytes, report_name,
                    )
                except Exception as exc:
                    self.logger.warning('completion outbox report upload still pending for Run %s: %s', run_id, exc)
                    delivered = False
                    continue
                metadata['report'] = report
                self._save_completion_outbox(
                    run_id, metadata, report_name=report_name, report_bytes=report_bytes,
                )
            if not self._completion_json(run_id, metadata):
                self.logger.warning('completion outbox delivery still pending for Run %s', run_id)
                delivered = False
                continue
            path.unlink()
            self.logger.info('delivered completion outbox entry for Run %s', run_id)
        return delivered

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
                self.stop_event.wait(min(
                    COMPLETION_RETRY_MAX_DELAY_SECONDS,
                    COMPLETION_RETRY_DELAY_SECONDS * (2 ** attempt),
                ))
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
                    self.stop_event.wait(min(
                        COMPLETION_RETRY_MAX_DELAY_SECONDS,
                        COMPLETION_RETRY_DELAY_SECONDS * (2 ** attempt),
                    ))
        # A PUT response can be lost after OSS persisted the object. Returning the
        # descriptor lets completion's authoritative HEAD check resolve that case.
        if descriptor is not None:
            return descriptor
        raise APIError(f'OSS report upload failed: {last_error}')

    def _complete(self, run_id: int, lease_token: str, completion_id: str,
                  status: str, stdout: str, stderr: str,
                  report_bytes: bytes | None, report_name: str | None) -> None:
        report = None
        upload_pending = False
        if status == 'completed':
            if report_bytes is None or not report_name:
                raise APIError('completed SimC run has no report')
            try:
                report = self._upload_report(
                    run_id, lease_token, report_bytes, report_name,
                )
            except Exception as exc:
                # Preserve the successful simulation and its report locally.  A
                # later outbox drain retries the upload before reporting terminal
                # completion; it must never convert a network failure to failed.
                upload_pending = True
                self.logger.warning('report upload deferred in durable outbox for Run %s: %s', run_id, exc)

        metadata = {
            'lease_token': lease_token, 'instance_id': self.instance_id,
            'completion_id': completion_id, 'status': status,
            'stdout': _utf8_tail(stdout, COMPLETION_TEXT_MAX_BYTES),
            'stderr': _utf8_tail(stderr, COMPLETION_TEXT_MAX_BYTES),
            'report': report if status == 'completed' else None,
        }
        if not upload_pending and self._completion_json(run_id, metadata):
            return
        # A response can be lost after the server committed this same completion.
        # Persist the exact payload and fixed completion_id, then let a future
        # idle cycle retry it before any new claim or maintenance can run.
        self._save_completion_outbox(
            run_id, metadata,
            report_name=report_name if status == 'completed' else None,
            report_bytes=report_bytes if status == 'completed' else None,
        )
        self.logger.warning('completion delivery deferred in durable outbox for Run %s', run_id)

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
                    env=os.environ.copy(),
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
                    self.heartbeat('degraded', maintenance='simc_compile')
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
            try:
                if not self.flush_completion_outbox():
                    # Backlog delivery is deliberately non-blocking: a later Run
                    # completion gives it another batch retry opportunity, while
                    # an outage must not strand the whole Agent idle forever.
                    self.logger.warning('completion outbox still has pending entries; continuing to claim work')
            except Exception as exc:
                self.logger.exception('completion outbox delivery failed: %s', exc)
                if once:
                    raise
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
            # Existing binaries remain usable even if the next scheduled
            # maintenance window later fails.  A task claim never triggers a
            # version check or rebuild.
            maintenance_error = None
            try:
                self._run_scheduled_maintenance_if_due()
            except Exception as exc:
                maintenance_error = exc
                self.logger.exception('SimC maintenance failed: %s', exc)
            try:
                response = self.heartbeat('degraded' if maintenance_error else 'online')
                self._run_dispatched_maintenance(response)
                capacity = 1 if once else self.config.max_concurrent_runs
                with ThreadPoolExecutor(max_workers=capacity,
                                        thread_name_prefix='simc-agent-run') as executor:
                    while not self.stop_event.is_set():
                        while (not self.stop_event.is_set()
                               and len(self._active_jobs) < capacity):
                            job = self.claim()
                            if not job:
                                break
                            self.logger.info('claimed Task %s Run %s', job.get('task_id'), job.get('run_id'))
                            self.heartbeat('busy')
                            run_id = job.get('run_id')
                            future = executor.submit(self.execute_job, job)
                            if isinstance(run_id, int):
                                self._active_jobs[run_id] = future
                        if not self._active_jobs:
                            break
                        completed, _ = wait(tuple(self._active_jobs.values()),
                                            return_when=FIRST_COMPLETED)
                        for run_id, future in list(self._active_jobs.items()):
                            if future not in completed:
                                continue
                            try:
                                future.result()
                            except Exception:
                                self._lease_block_until = time.monotonic() + self.lease_seconds
                                raise
                            finally:
                                self._active_jobs.pop(run_id, None)
                        if once:
                            break
                    self._lease_block_until = 0.0
                if self._active_jobs:
                    self.heartbeat('busy')
                else:
                    self.heartbeat('online')
                if once:
                    return
            except APIError as exc:
                self.logger.error('control-plane operation failed: %s', exc)
                if exc.status == 426 and exc.details.get('code') == 'agent_update_required':
                    self._self_update(
                        exc.details.get('required_version'),
                        required_revision=exc.details.get('required_revision'),
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
    parser.add_argument(
        '--config', default=str(Path(__file__).resolve().with_name('simc_agent.json')),
        help='Path to agent JSON configuration (default: simc_agent.json beside this script)',
    )
    parser.add_argument('--once', action='store_true', help='Claim at most one Run and exit')
    args = parser.parse_args()
    try:
        ensure_configuration(args.config, interactive=sys.stdin.isatty())
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
