"""Bounded, disposable JSON identity cache; never a source of Task facts.

Fixed Task slots bound retention without request-time directory scans. Collisions
are misses, not expiry. Fingerprints and algorithm versions are always checked.
"""
import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.db.models import Func, TextField

from .simc_benchmark_result_snapshot import _lock, _write

VERSION = 'candidate-input-v1'
MAX_BYTES = 256 * 1024
SLOTS = 4096


class CandidateFingerprint(Func):
    arity = 1
    output_field = TextField()

    def as_mysql(self, compiler, connection, **extra_context):
        sql, params = compiler.compile(self.source_expressions[0])
        return f"SHA2(COALESCE(CAST({sql} AS CHAR), 'sql-null'), 256)", params

    def as_sqlite(self, compiler, connection, **extra_context):
        # SQLite tests execute the real JSON extraction; only SHA is in Python.
        sql, params = compiler.compile(self.source_expressions[0])
        return f"JSON_QUOTE({sql})", params

    def convert_value(self, value, expression, connection):
        if connection.vendor == 'sqlite':
            return hashlib.sha256(str(value).encode()).hexdigest()
        return value


def cache_path(task_id, fingerprint):
    key = hashlib.sha256(f'{VERSION}:{task_id}:{fingerprint}'.encode()).hexdigest()
    root = Path(getattr(settings, 'SIMC_BENCHMARK_IDENTITY_CACHE_ROOT',
                        Path(settings.BASE_DIR) / 'var' / 'benchmark-identities'))
    for public in [getattr(settings, 'MEDIA_ROOT', None), getattr(settings, 'STATIC_ROOT', None)]:
        if public and root.resolve().is_relative_to(Path(public).resolve()):
            raise ValueError('Identity cache must be private')
    return root / f'{int(task_id) % SLOTS:04x}.json', key


def load(task_id, fingerprint):
    try:
        path, key = cache_path(task_id, fingerprint)
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        digest = data.pop('digest', None)
        if digest != hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest():
            return None
        if (not isinstance(data, dict) or data.get('key') != key
                or type(data.get('complete')) is not bool
                or not isinstance(data.get('queried_keys'), list)
                or not all(isinstance(k, str) for k in data['queried_keys'])
                or not isinstance(data.get('identities'), dict)
                or not all(isinstance(k, str) and isinstance(v, str) and len(v) == 64
                           and all(c in '0123456789abcdef' for c in v)
                           for k, v in data['identities'].items())
                or not set(data['identities']).issubset(data['queried_keys'])):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def store(task_id, fingerprint, identities, queried_keys, *, complete=False):
    try:
        path, key = cache_path(task_id, fingerprint)
        # Nonblocking lock: contention may lose a cache opportunity, never data.
        with _lock(path.with_suffix('.lock'), blocking=False) as locked:
            if not locked:
                return
            previous = load(task_id, fingerprint)
            merged = dict(previous['identities']) if previous else {}
            merged.update(identities)
            queried = set(previous['queried_keys']) if previous else set()
            queried.update(queried_keys)
            queried.update(merged)
            data = {'key': key, 'identities': merged, 'queried_keys': sorted(queried),
                    'complete': complete or bool(previous and previous['complete'])}
            data['digest'] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            if len(json.dumps(data).encode()) <= MAX_BYTES:
                _write(path, data)
    except (OSError, ValueError, TypeError):
        pass
