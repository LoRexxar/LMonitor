"""Restricted, side-effect-free validation through the production SimC binary.

This module deliberately validates an APL as an input fragment.  It never creates a
simulation task/run/artifact and it does not accept profile-level directives which
could turn validation into arbitrary SimC configuration execution.
"""
from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


# Only APL assignments are accepted. Everything else is a profile directive.
_APL_ASSIGNMENT = re.compile(r"^\s*(?:actions(?:\.[A-Za-z0-9_-]+)?|variables(?:\.[A-Za-z0-9_-]+)?)\s*\+?=", re.I)
_FORBIDDEN_DIRECTIVE = re.compile(
    r"^\s*(?:include|option|html|output|log|input|threads|iterations|fixed_time|vary_combat_length|report|export|validate_only)\s*=", re.I
)
_MAX_DIAGNOSTIC_BYTES = 4096


@dataclass(frozen=True)
class ValidatorLimits:
    timeout_seconds: float = 5.0
    max_source_bytes: int = 256 * 1024
    max_output_bytes: int = 256 * 1024


class AuthoritativeValidationError(Exception):
    """A safe, user-facing failure of authoritative validation."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RestrictedSimcValidator:
    """Run a pinned SimC executable with a bounded and private input file."""

    def __init__(self, binary: str, *, catalog_revision: str | None = None,
                 binary_revision: str | None = None, temp_root: str | None = None,
                 limits: ValidatorLimits | None = None, runner=subprocess.Popen):
        self.binary = os.path.realpath(binary)
        self.catalog_revision = catalog_revision
        self.binary_revision = binary_revision
        default_root = os.path.join(tempfile.gettempdir(), f'lmonitor-apl-validation-{os.getuid()}')
        self.temp_root = os.path.realpath(temp_root or default_root)
        self.limits = limits or ValidatorLimits()
        self.runner = runner

    def validate(self, source: str, *, validation_context: Mapping[str, Any] | None = None) -> dict:
        context = dict(validation_context or {})
        raw = str(source or '')
        supplied_input = context.get('validation_input')
        if supplied_input is not None and len(str(supplied_input).encode('utf-8')) > self.limits.max_source_bytes:
            return self._failure('validation_input_too_large', 'Composed validation input exceeds the validation limit.')
        validation_input = str(supplied_input or raw)
        if len(raw.encode('utf-8')) > self.limits.max_source_bytes:
            return self._failure('source_too_large', 'APL source exceeds the validation limit.')
        for number, line in enumerate(validation_input.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if _FORBIDDEN_DIRECTIVE.match(line):
                return self._failure('profile_directive_forbidden',
                                     f'Profile directive is not allowed (line {number}).')
        for number, line in enumerate(raw.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if _FORBIDDEN_DIRECTIVE.match(line) or not _APL_ASSIGNMENT.match(line):
                return self._failure('profile_directive_forbidden',
                                     f'Only actions/variables assignments are allowed (line {number}).')
        expected = context.get('catalog_revision', self.catalog_revision)
        actual = context.get('binary_revision', self.binary_revision)
        if expected and actual and str(expected) != str(actual):
            return self._failure('stale_binary', 'SimC binary revision does not match the catalog revision.')
        if not os.path.isfile(self.binary) or not os.access(self.binary, os.X_OK):
            return self._failure('binary_unavailable', 'Authoritative SimC binary is unavailable.')
        root = Path(self.temp_root)
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = root.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                return self._failure('temp_directory_error', 'Controlled validation directory is insecure.')
        except OSError:
            return self._failure('temp_directory_error', 'Could not create a controlled validation directory.')
        try:
            with tempfile.TemporaryDirectory(prefix='apl-validation-', dir=str(root)) as work:
                input_path = Path(work) / 'apl.simc'
                stdout_path = Path(work) / 'stdout'
                stderr_path = Path(work) / 'stderr'
                for path in (input_path, stdout_path, stderr_path):
                    path.touch(mode=0o600)
                    os.chmod(path, 0o600)
                input_path.write_text(validation_input, encoding='utf-8')
                command = [self.binary, f'input={input_path}', 'strict_parsing=1',
                           'iterations=1', 'threads=1', 'fixed_time=1',
                           'vary_combat_length=0']
                try:
                    with stdout_path.open('wb') as out, stderr_path.open('wb') as err:
                        process = self.runner(command, stdout=out, stderr=err,
                                              start_new_session=True, cwd=work)
                        deadline = time.monotonic() + self.limits.timeout_seconds
                        exceeded = False
                        while process.poll() is None:
                            if (stdout_path.stat().st_size > self.limits.max_output_bytes or
                                    stderr_path.stat().st_size > self.limits.max_output_bytes):
                                exceeded = True
                                self._terminate(process)
                                break
                            if time.monotonic() >= deadline:
                                self._terminate(process)
                                process.wait()
                                return self._failure('timeout', 'Authoritative SimC validation timed out.')
                            time.sleep(0.005)
                        process.wait()
                        exceeded = (
                            stdout_path.stat().st_size > self.limits.max_output_bytes or
                            stderr_path.stat().st_size > self.limits.max_output_bytes
                        )
                    stdout = stdout_path.read_bytes()[:self.limits.max_output_bytes]
                    stderr = stderr_path.read_bytes()[:self.limits.max_output_bytes]
                    if exceeded:
                        return self._failure('output_too_large', 'SimC validation output exceeds the limit.')
                except OSError:
                    return self._failure('binary_unavailable', 'Authoritative SimC binary could not be started.')
                text = (stdout + b'\n' + stderr).decode('utf-8', errors='replace')
                return {
                    'authoritative_valid': process.returncode == 0,
                    'authoritative_status': 'valid' if process.returncode == 0 else 'invalid',
                    'diagnostics': self._diagnostics(text, process.returncode),
                    'binary_revision': actual,
                    'catalog_revision': expected,
                }
        except OSError:
            return self._failure('temp_directory_error', 'Could not create a controlled validation directory.')

    def _bounded(self, value):
        if value is None:
            return b''
        if len(value) > self.limits.max_output_bytes:
            return None
        return value

    @staticmethod
    def _terminate(process):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (AttributeError, OSError, ProcessLookupError):
            try:
                process.kill()
            except (AttributeError, OSError):
                pass

    @staticmethod
    def _diagnostics(text, returncode):
        if returncode == 0:
            return []
        # Never expose filesystem layout or the executable path in user diagnostics.
        text = re.sub(r'(?<![A-Za-z0-9_])/(?:[^\s:]+/?)+', '<path>', text)
        message = text.strip().encode('utf-8')[:_MAX_DIAGNOSTIC_BYTES].decode('utf-8', 'ignore')
        return [{'source': 'authoritative', 'severity': 'error', 'code': 'simc-parse-error',
                 'message': message or 'SimC rejected the APL.'}]

    @staticmethod
    def _failure(code, message):
        return {'authoritative_valid': False, 'authoritative_status': 'error',
                'authoritative_error': {'code': code, 'message': message}, 'diagnostics': []}


def validate_authoritatively(source, **kwargs):
    """Small stable function facade used by the API and tests."""
    binary = kwargs.pop('binary', None)
    if not binary:
        return RestrictedSimcValidator._failure('binary_unavailable', 'Authoritative SimC binary is unavailable.')
    context = kwargs.pop('validation_context', None)
    return RestrictedSimcValidator(binary, **kwargs).validate(
        source, validation_context=context)
