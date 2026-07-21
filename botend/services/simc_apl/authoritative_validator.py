"""Restricted, side-effect-free validation through the production SimC binary.

This module deliberately validates an APL as an input fragment.  It never creates a
simulation task/run/artifact and it does not accept profile-level directives which
could turn validation into arbitrary SimC configuration execution.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_PROFILE_DIRECTIVE = re.compile(r"^\s*(?:option|include)\s*=", re.I)


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
        self.temp_root = os.path.realpath(temp_root or tempfile.gettempdir())
        self.limits = limits or ValidatorLimits()
        self.runner = runner

    def validate(self, source: str, *, validation_context: Mapping[str, Any] | None = None) -> dict:
        context = dict(validation_context or {})
        raw = str(source or '')
        if len(raw.encode('utf-8')) > self.limits.max_source_bytes:
            return self._failure('source_too_large', 'APL source exceeds the validation limit.')
        for number, line in enumerate(raw.splitlines(), 1):
            if _PROFILE_DIRECTIVE.match(line):
                return self._failure('profile_directive_forbidden',
                                     f'Profile-level option/include is forbidden (line {number}).')
        expected = context.get('catalog_revision', self.catalog_revision)
        actual = context.get('binary_revision', self.binary_revision)
        if expected and actual and str(expected) != str(actual):
            return self._failure('stale_binary', 'SimC binary revision does not match the catalog revision.')
        if not os.path.isfile(self.binary) or not os.access(self.binary, os.X_OK):
            return self._failure('binary_unavailable', 'Authoritative SimC binary is unavailable.')
        root = Path(self.temp_root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix='apl-validation-', dir=str(root)) as work:
                input_path = Path(work) / 'apl.simc'
                input_path.write_text(raw, encoding='utf-8')
                os.chmod(input_path, 0o600)
                # Keep this command stable: future validate_only=1 support can replace
                # only this argument list without changing the service/API contract.
                command = [self.binary, f'input={input_path}', 'strict=1', 'validate_only=1']
                try:
                    process = self.runner(command, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, start_new_session=True)
                    stdout, stderr = process.communicate(timeout=self.limits.timeout_seconds)
                except subprocess.TimeoutExpired:
                    self._terminate(process)
                    return self._failure('timeout', 'Authoritative SimC validation timed out.')
                except OSError:
                    return self._failure('binary_unavailable', 'Authoritative SimC binary could not be started.')
                stdout = self._bounded(stdout)
                stderr = self._bounded(stderr)
                if stdout is None or stderr is None:
                    self._terminate(process)
                    return self._failure('output_too_large', 'SimC validation output exceeds the limit.')
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
        return [{'source': 'authoritative', 'severity': 'error', 'code': 'simc-parse-error',
                 'message': text.strip() or 'SimC rejected the APL.'}]

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
