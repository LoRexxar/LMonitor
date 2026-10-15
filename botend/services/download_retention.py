"""Conservative, bounded maintenance of one explicit table/locale .parts root.

Never traverses source CSV directories. Legacy/unproven attempts are report-only.
"""
import fcntl
import hashlib
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from itertools import islice
from pathlib import Path

import json
from .download_checkpoint import encoded, read_json, valid_rows, atomic_json, atomic_bytes, fsync_directory
from django.core import signing

CHECKPOINT = '.retention-checkpoint'
CHECKPOINT_SALT = 'lmonitor.download-retention.validation.v1'
CLOSURE_CAPACITY = 1024
MAX_CACHE_BYTES = 4 * 1024 * 1024
MAX_TREE_DEPTH = 3
MAX_IDENTITY_CHECKS = 128 * CLOSURE_CAPACITY


class _Capacity(ValueError):
    pass


class _Unsupported(ValueError):
    pass


def _lstat(path):
    budget = getattr(_LOCAL, 'identity_budget', None)
    if budget is not None:
        if budget.identity_checks >= MAX_IDENTITY_CHECKS:
            raise _Capacity('identity check capacity exceeded')
        budget.identity_checks += 1
    return path.lstat()


def _exists(path):
    try:
        _lstat(path)
        return True
    except FileNotFoundError:
        return False


def _is_dir(path):
    return stat.S_ISDIR(_lstat(path).st_mode)


@contextmanager
def _identity_limit(budget):
    previous = getattr(_LOCAL, 'identity_budget', None)
    _LOCAL.identity_budget = budget
    try:
        yield
    finally:
        _LOCAL.identity_budget = previous


def _identity(path):
    info = _lstat(path)
    return [info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns]


class _Paused(ValueError):
    pass

_LOCAL = threading.local()
HEX32 = re.compile(r'[0-9a-f]{32}')
HEX64 = re.compile(r'[0-9a-f]{64}')
BASE = {'identity', 'total', 'chunk_size', 'per_page', 'schema'}
GRACE_SECONDS = 48 * 3600


def safe_path(path):
    path = Path(path).absolute()
    if len(path.parts) > 32:
        raise _Capacity('absolute path depth exceeds limit')
    for p in (path, *path.parents):
        try:
            if stat.S_ISLNK(_lstat(p).st_mode):
                return False
        except FileNotFoundError:
            pass
    return True


@contextmanager
def download_lock(root):
    root = Path(root).absolute()
    if not safe_path(root):
        raise ValueError('linked download path')
    root.mkdir(parents=True, exist_ok=True)
    held = getattr(_LOCAL, 'held', {})
    _LOCAL.held = held
    key = str(root)
    if key in held:
        yield
        return
    fd = os.open(root / '.download.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_nlink != 1:
            raise ValueError('linked download lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        held[key] = fd
        try:
            yield
        finally:
            held.pop(key, None)
    finally:
        os.close(fd)


def _regular(path, max_bytes):
    info = _lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
        raise ValueError('unsafe file or byte budget')
    return info


def _entries(path, limit):
    with os.scandir(path) as entries:
        result = list(islice(entries, limit + 1))
    if len(result) > limit:
        raise ValueError('entry budget exceeded')
    return [Path(entry.path) for entry in result]


def check_root_budget(root, max_entries=1024, max_bytes=None):
    """Bound total tree enumeration before admitting a new attempt.

    Existing in-flight attempts deliberately bypass this admission check.
    Unknown files count toward capacity; links are never followed.
    Byte capacity is opt-in and independent of the per-GC read budget.
    """
    if max_entries < 0:
        raise ValueError('root entry budget exhausted by reservation')
    pending = [Path(root)] if Path(root).exists() else []
    count = size = 0
    while pending:
        directory = pending.pop()
        for path in _entries(directory, max_entries - count):
            count += 1
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            else:
                # Count legacy/link entries without following or reclaiming them.
                size += info.st_size
            if count > max_entries or (max_bytes is not None and size > max_bytes):
                raise ValueError('root budget exhausted; explicit maintenance required')


def _manifest(path, max_bytes, budget=None):
    _regular(path, max_bytes)
    value = budget.read(path) if budget else read_json(path)
    if not isinstance(value, dict) or set(value) not in (BASE | {'attempt', 'state'}, BASE | {'attempt', 'state', 'csv'}):
        raise ValueError('unknown manifest shape')
    identity = value['identity']
    if (value['schema'] != 2 or not isinstance(identity, dict)
            or set(identity) != {'branch', 'build', 'table', 'locale'}
            or not all(isinstance(v, str) and v for v in identity.values())
            or any(type(value[k]) is not int or value[k] <= 0 for k in ('total', 'chunk_size', 'per_page'))
            or value['chunk_size'] % value['per_page']
            or not isinstance(value['attempt'], str) or not HEX32.fullmatch(value['attempt'])
            or value['state'] not in ('downloading', 'complete')):
        raise ValueError('invalid identity/layout')
    expected_keys = BASE | {'attempt', 'state'} | ({'csv'} if value['state'] == 'complete' else set())
    if set(value) != expected_keys:
        raise ValueError('manifest state/shape mismatch')
    if value['state'] == 'complete':
        _validate_csv(value['csv'], value)
    expected = {key: value[key] for key in BASE}
    if hashlib.sha256(encoded(expected)).hexdigest() != path.parent.name:
        raise ValueError('fingerprint mismatch')
    return value


def _validate_csv(metadata, current):
    if (not isinstance(metadata, dict) or set(metadata) != {'rows', 'sha256', 'url', 'identity', 'headers'}
            or metadata['identity'] != current['identity'] or metadata['rows'] != current['total']
            or not isinstance(metadata['sha256'], str) or not HEX64.fullmatch(metadata['sha256'])
            or not isinstance(metadata['headers'], list) or not all(isinstance(h, str) for h in metadata['headers'])
            or not isinstance(metadata['url'], str)):
        raise ValueError('invalid CSV evidence')


class _Budget:
    def __init__(self, entries, size):
        self.entries = entries
        self.max_entries = entries
        self.max_bytes = size
        self.identity_checks = 0
        self.metadata_bytes = 0
        self.progress = 0
        self.minimum_bytes = 0
        self.minimum_entries = 0
        self.remaining = size
        self.read_bytes = 0
        self.cache = {'files': {}, 'directories': {}}

    def listing(self, path):
        # Cached names are only reusable for the identical directory inode and
        # change timestamps. Every returned member still passes lstat below.
        key = str(path.absolute())
        identity = _identity(path)
        saved = self.cache['directories'].get(key)
        if saved and saved[0] == identity:
            return [path / name for name in saved[1]]
        try:
            # Reserve the overflow probe inside, not beyond, this call's cap.
            if self.entries < 1:
                raise ValueError('entry budget exhausted')
            files = _entries(path, min(self.entries - 1, CLOSURE_CAPACITY))
        except ValueError as exc:
            available = self.entries
            self.entries = 0
            if available > CLOSURE_CAPACITY:
                raise _Capacity('directory exceeds closure capacity') from exc
            if available == self.max_entries:
                self.minimum_entries = available + 1
                raise _Unsupported('directory width requires larger enumeration budget') from exc
            raise _Paused(str(exc)) from exc
        self.entries -= len(files)
        files = sorted(files)
        self.cache['directories'][key] = [identity, [f.name for f in files]]
        self.progress += 1
        return files

    def closure(self, root):
        pending = [(root, 0)]
        count = 0
        while pending:
            directory, depth = pending.pop()
            if depth > MAX_TREE_DEPTH:
                raise _Capacity('directory depth exceeds closure limit')
            for path in self.listing(directory):
                count += 1
                if count > CLOSURE_CAPACITY:
                    raise _Capacity('closure capacity exceeded')
                if stat.S_ISDIR(_lstat(path).st_mode):
                    pending.append((path, depth + 1))
        self.validate_cache(root)

    def validate_cache(self, root):
        if not isinstance(self.cache, dict) or set(self.cache) != {'files', 'directories'}:
            raise _Capacity('invalid checkpoint cache schema')
        members = 0
        for section in ('files', 'directories'):
            values = self.cache[section]
            if not isinstance(values, dict) or len(values) > CLOSURE_CAPACITY + 1:
                raise _Capacity('checkpoint cache capacity exceeded')
            for key, value in values.items():
                if not isinstance(key, str) or len(key) > 4096:
                    raise _Capacity('invalid checkpoint path')
                relative = Path(key).relative_to(root.absolute())
                if '..' in relative.parts or len(relative.parts) > MAX_TREE_DEPTH + 1:
                    raise _Capacity('invalid checkpoint depth')
                if (not isinstance(value, list) or len(value) != 2
                        or not isinstance(value[0], list) or len(value[0]) != 7
                        or not all(type(n) is int for n in value[0])):
                    raise _Capacity('invalid checkpoint identity')
                if section == 'directories':
                    names = value[1]
                    if (not isinstance(names, list) or len(names) > CLOSURE_CAPACITY
                            or not all(isinstance(n, str) and n not in ('', '.', '..')
                                       and '/' not in n and len(n) <= 255 for n in names)
                            or len(set(names)) != len(names)):
                        raise _Capacity('invalid checkpoint membership')
                    members += len(names)
                elif not isinstance(value[1], dict):
                    raise _Capacity('invalid checkpoint metadata')
        if members > CLOSURE_CAPACITY:
            raise _Capacity('checkpoint cumulative membership exceeded')
        if len(json.dumps(self.cache, separators=(',', ':')).encode()) > MAX_CACHE_BYTES:
            raise _Capacity('checkpoint metadata capacity exceeded')

    def raw(self, path):
        info = _regular(path, self.remaining)
        with path.open('rb') as stream:
            raw = stream.read(info.st_size)
        self.remaining -= len(raw)
        self.read_bytes += len(raw)
        if len(raw) != info.st_size:
            raise ValueError('file changed during verification')
        return raw

    def read(self, path):
        key = str(path.absolute())
        if _regular(path, float('inf')).st_size > MAX_CACHE_BYTES:
            raise _Capacity('metadata file exceeds capacity')
        identity = _identity(path)
        saved = self.cache['files'].get(key)
        if saved and saved[0] == identity:
            return saved[1]
        if identity[4] > self.remaining:
            self.minimum_bytes = self.read_bytes + identity[4]
            raise _Paused('read byte budget')
        if self.metadata_bytes + identity[4] > MAX_CACHE_BYTES:
            raise _Capacity('cumulative metadata capacity exceeded')
        value = json.loads(self.raw(path))
        self.metadata_bytes += identity[4]
        if _identity(path) != identity:
            raise ValueError('metadata changed during verification')
        self.cache['files'][key] = [identity, value]
        self.progress += 1
        return value

    def load_checkpoint(self, root):
        path = root.parent / ('.' + root.name + CHECKPOINT)
        if _exists(path):
            if _regular(path, float('inf')).st_size > MAX_CACHE_BYTES + 1024:
                raise _Capacity('checkpoint file exceeds capacity')
            raw = self.raw(path)
            try:
                import zlib
                from django.utils.crypto import constant_time_compare
                compressed, signature = raw.rsplit(b'\n', 1)
                signer = signing.Signer(salt=CHECKPOINT_SALT)
                if not constant_time_compare(signer.signature(compressed), signature.decode()):
                    raise signing.BadSignature('invalid validation checkpoint')
                inflater = zlib.decompressobj()
                decoded = inflater.decompress(compressed, MAX_CACHE_BYTES + 1)
                if len(decoded) > MAX_CACHE_BYTES or not inflater.eof or inflater.unused_data:
                    raise _Capacity('checkpoint decompression capacity exceeded')
                saved = json.loads(decoded)
                if (not isinstance(saved, dict) or set(saved) != {'root', 'schema', 'cache'}
                        or saved['root'] != str(root.absolute()) or type(saved['schema']) is not int
                        or saved['schema'] != 1):
                    raise _Capacity('invalid checkpoint schema')
                self.cache = saved['cache']
                self.validate_cache(root)
                self.metadata_bytes = len(decoded)
            except _Capacity:
                raise
            except (signing.BadSignature, ValueError, KeyError, TypeError, zlib.error, RecursionError):
                # Unsigned or tampered data is never validation evidence.
                self.cache = {'files': {}, 'directories': {}}

    def save_checkpoint(self, root):
        # Compact, authenticated metadata only; payload bodies are never cached.
        import zlib
        self.validate_cache(root)
        value = dict(schema=1, root=str(root.absolute()), cache=self.cache)
        raw = json.dumps(value, separators=(',', ':')).encode()
        if len(raw) > MAX_CACHE_BYTES:
            raise _Capacity('checkpoint serialization capacity exceeded')
        compressed = zlib.compress(raw)
        signature = signing.Signer(salt=CHECKPOINT_SALT).signature(compressed).encode()
        atomic_bytes(root.parent / ('.' + root.name + CHECKPOINT), compressed + b'\n' + signature)


def _candidate(directory, current, now, budget):
    files = budget.listing(directory)
    infos = {p.name: _regular(p, float('inf')) for p in files}
    pending = budget.read(directory / 'pending-delete.json') if 'pending-delete.json' in infos else None
    proof = budget.read(directory / 'complete.json') if 'complete.json' in infos else None
    authorized = set()
    if pending is not None:
        if (not isinstance(pending, dict) or set(pending) != {'schema', 'proof', 'authorized'}
                or pending['schema'] != 1 or not isinstance(pending['authorized'], list)
                or not all(isinstance(n, str) for n in pending['authorized'])):
            raise ValueError('invalid pending deletion')
        if proof is not None and proof != pending['proof']:
            raise ValueError('pending proof changed')
        proof = pending['proof']
        authorized = set(pending['authorized'])
    if not isinstance(proof, dict) or set(proof) != {'schema', 'manifest', 'completed_at', 'files'} or proof['schema'] != 1:
        raise ValueError('missing or unknown completion proof')
    saved = proof['manifest']
    if (not isinstance(saved, dict) or set(saved) != BASE | {'attempt', 'state', 'csv'}
            or any(saved.get(k) != current[k] for k in BASE)
            or saved.get('attempt') != directory.name or saved.get('state') != 'complete'):
        raise ValueError('completion identity mismatch')
    _validate_csv(saved['csv'], current)
    total, chunk = current['total'], current['chunk_size']
    # Bound computed expected membership before allocating it.
    if not isinstance(proof['files'], dict) or (total + chunk - 1) // chunk != len(proof['files']):
        raise ValueError('part membership mismatch')
    expected = {f'{offset}-{total}.json' for offset in range(0, total, chunk)}
    if set(proof['files']) != expected or not authorized <= expected:
        raise ValueError('unknown proof membership')
    present = set(infos) - {'complete.json', 'pending-delete.json'}
    if not present <= expected or not expected - present <= authorized:
        raise ValueError('unknown attempt closure')
    if 'complete.json' not in infos and (pending is None or present):
        raise ValueError('missing completion proof')
    for evidence in proof['files'].values():
        if (not isinstance(evidence, dict) or set(evidence) != {'size', 'sha256'}
                or type(evidence['size']) is not int or evidence['size'] <= 0
                or not isinstance(evidence['sha256'], str) or not HEX64.fullmatch(evidence['sha256'])):
            raise ValueError('invalid raw file evidence')
    completed = proof['completed_at']
    if type(completed) not in (float, int) or not 0 < completed <= now:
        raise ValueError('invalid completion time')
    if pending is None and now - max(completed, *(s.st_mtime for s in infos.values())) < GRACE_SECONDS:
        return None
    return dict(directory=directory, proof=proof, present=present, authorized=authorized, selected=[])


def _verify_part(plan, name, budget):
    raw = budget.raw(plan['directory'] / name)
    proof = plan['proof']
    if proof['files'][name] != {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}:
        raise ValueError('part file hash mismatch')
    part = json.loads(raw)
    saved = proof['manifest']
    offset = int(name.split('-')[0])
    if (not isinstance(part, dict) or set(part) != {'identity', 'offset', 'total', 'headers', 'sha256', 'rows'}
            or part['identity'] != saved['identity'] or part['offset'] != offset or part['total'] != saved['total']
            or not valid_rows(part['rows'], min(saved['chunk_size'], saved['total'] - offset))
            or part['headers'] != sorted(saved['csv']['headers'])
            or part['headers'] != sorted(part['rows'][0])
            or part['sha256'] != hashlib.sha256(encoded(part['rows'])).hexdigest()):
        raise ValueError('invalid part contents')
    return int(part['rows'][0]['ID']), int(part['rows'][-1]['ID'])


def _unlink_control(path, item, report):
    try:
        size = _regular(path, float('inf')).st_size
    except FileNotFoundError:
        return
    path.unlink()
    item.update(status='partial', durability_pending=True, cleanup_pending=True)
    item['deleted_files'] = item.get('deleted_files', 0) + 1
    item['deleted_bytes'] = item.get('deleted_bytes', 0) + size
    report['deleted_files'] += 1
    report['deleted_bytes'] += size


def _cleanup_marker(marker, raw):
    marker.unlink()
    try:
        fsync_directory(marker.parent)
    except OSError:
        # Restore the exact authenticated intent if cleanup durability is unknown.
        atomic_bytes(marker, raw)
        raise


def maintain_download_parts(root, *, apply=False, now=None, max_entries=1024, max_bytes=128 * 1024 * 1024):
    """Plan the entire bounded batch before unlinking; resume durable exact intents."""
    root = Path(root)
    if not root.name.endswith('.parts') or max_entries < 2 or max_bytes < 1:
        raise ValueError('explicit .parts root and positive budgets required')
    now = time.time() if now is None else now
    report = {'apply': apply, 'root': str(root), 'entries': [], 'deleted_attempts': 0,
              'recovered_attempts': 0, 'deleted_files': 0, 'deleted_bytes': 0}
    budget = _Budget(max_entries, max_bytes)
    plans = []
    with _identity_limit(budget), download_lock(root):
        try:
            budget.load_checkpoint(root)
            budget.closure(root)
            tails = []
            # All current manifests and closure metadata are checked before any deletion.
            for scope in budget.listing(root):
                if scope.name in {'.download.lock', CHECKPOINT}:
                    continue
                if not HEX64.fullmatch(scope.name) or not safe_path(scope) or not _is_dir(scope):
                    report['entries'].append({'path': str(scope), 'status': 'legacy_or_unknown'})
                    continue
                manifest = _manifest(scope / 'manifest.json', max_bytes, budget)
                if root.name != f"{manifest['identity']['table']}.{manifest['identity']['locale']}.parts":
                    raise ValueError('table scope mismatch')
                for attempt in budget.listing(scope):
                    if attempt.name == 'manifest.json':
                        continue
                    if re.fullmatch(r'\.delete-[0-9a-f]{32}\.json', attempt.name):
                        if _regular(attempt, float('inf')).st_size > MAX_CACHE_BYTES:
                            raise _Capacity('external intent exceeds metadata capacity')
                        marker_raw = budget.raw(attempt)
                        if marker_raw.startswith(b'.'):
                            raise _Capacity('compressed external intent unsupported')
                        intent = signing.loads(marker_raw.decode(), salt=CHECKPOINT_SALT + '.tail')
                        name = attempt.name[8:-5]
                        if (not isinstance(intent, dict) or set(intent) != {'attempt', 'scope', 'inode', 'base'}
                                or not isinstance(intent['inode'], list) or len(intent['inode']) != 2
                                or not all(type(n) is int for n in intent['inode'])
                                or intent.get('attempt') != name or intent.get('scope') != str(scope.absolute())
                                or name == manifest['attempt']
                                or intent.get('base') != {k: manifest[k] for k in BASE}):
                            raise ValueError('invalid external deletion intent')
                        target = scope / name
                        if _exists(target):
                            if not safe_path(target) or not _is_dir(target) or _identity(target)[:2] != intent['inode']:
                                raise ValueError('deletion directory identity changed')
                            if budget.listing(target):
                                continue
                        tails.append((attempt, target, marker_raw))
                        continue
                    item = {'path': str(attempt), 'status': 'preserved'}
                    report['entries'].append(item)
                    if attempt.name == manifest['attempt']:
                        item['status'] = 'current'
                        continue
                    if not HEX32.fullmatch(attempt.name) or not safe_path(attempt) or not _is_dir(attempt):
                        item['status'] = 'legacy_or_unknown'
                        continue
                    # Unproven legacy is report-only, never inferred complete.
                    if not _exists(attempt / 'complete.json') and not _exists(attempt / 'pending-delete.json'):
                        item['reason'] = 'missing completion proof'
                        continue
                    try:
                        plan = _candidate(attempt, manifest, now, budget)
                    except _Paused:
                        raise
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        item['reason'] = str(exc)
                        report['blocked'] = str(exc)
                        continue
                    item['status'] = 'grace' if plan is None else 'eligible'
                    if plan is not None:
                        plan['item'] = item
                        plans.append(plan)
            if report.get('blocked'):
                raise ValueError(report['blocked'])
            budget.validate_cache(root)
            for plan in plans:
                previous = None
                for name in sorted(plan['present'], key=lambda n: int(n.split('-')[0])):
                    if plan['proof']['files'][name]['size'] > budget.remaining:
                        report['paused'] = 'read byte budget'
                        budget.minimum_bytes = budget.read_bytes + plan['proof']['files'][name]['size']
                        break
                    try:
                        first, last = _verify_part(plan, name, budget)
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        plan['item'].update(status='preserved', reason=str(exc))
                        raise
                    if previous is not None and first <= previous:
                        raise ValueError('overlapping parts')
                    previous = last
                    plan['selected'].append(name)
            # No mutations above: a bad tail member cancels all planned deletions.
            if apply:
                for marker, target, marker_raw in tails:
                    item = dict(path=str(target), status='preserved', directory_removed=False,
                                durability_pending=False, cleanup_pending=True)
                    report['entries'].append(item)
                    if _exists(target):
                        target.rmdir()
                        report['deleted_attempts'] += 1
                    else:
                        report['recovered_attempts'] += 1
                    item.update(status='removed', directory_removed=True, durability_pending=True)
                    fsync_directory(target.parent)
                    item['durability_pending'] = False
                    _cleanup_marker(marker, marker_raw)
                    item.update(status='deleted', cleanup_pending=False)
                for plan in plans:
                    directory = plan['directory']
                    if plan['present'] and not plan['selected']:
                        plan['item']['status'] = 'paused'
                        continue
                    atomic_json(directory / 'pending-delete.json', {
                        'schema': 1, 'proof': plan['proof'],
                        'authorized': sorted(plan['authorized'] | set(plan['selected'])),
                    })
                    for name in plan['selected']:
                        (directory / name).unlink()
                        item = plan['item']
                        size = plan['proof']['files'][name]['size']
                        item.update(status='partial', durability_pending=True, cleanup_pending=True)
                        item['deleted_files'] = item.get('deleted_files', 0) + 1
                        item['deleted_bytes'] = item.get('deleted_bytes', 0) + size
                        report['deleted_files'] += 1
                        report['deleted_bytes'] += size
                    fsync_directory(directory)
                    plan['item']['durability_pending'] = False
                    remaining = plan['present'] - set(plan['selected'])
                    if remaining:
                        plan['item'].update(status='paused', remaining=len(remaining))
                        continue
                    marker = directory.parent / f'.delete-{directory.name}.json'
                    intent = dict(attempt=directory.name, scope=str(directory.parent.absolute()),
                                  inode=_identity(directory)[:2],
                                  base={k: plan['proof']['manifest'][k] for k in BASE})
                    marker_raw = signing.dumps(intent, salt=CHECKPOINT_SALT + '.tail').encode()
                    atomic_bytes(marker, marker_raw)
                    _unlink_control(directory / 'complete.json', plan['item'], report)
                    fsync_directory(directory)
                    plan['item']['durability_pending'] = False
                    _unlink_control(directory / 'pending-delete.json', plan['item'], report)
                    fsync_directory(directory)
                    plan['item']['durability_pending'] = False
                    directory.rmdir()
                    report['deleted_attempts'] += 1
                    plan['item'].update(status='removed', directory_removed=True,
                                        durability_pending=True, cleanup_pending=True)
                    fsync_directory(directory.parent)
                    plan['item']['durability_pending'] = False
                    _cleanup_marker(marker, marker_raw)
                    plan['item'].update(status='deleted', cleanup_pending=False)
        except _Capacity as exc:
            report.update(capacity_blocked=str(exc), blocked=str(exc))
        except _Unsupported as exc:
            report.update(unsupported_budget=str(exc), blocked=str(exc))
        except _Paused as exc:
            report['paused'] = str(exc)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, signing.BadSignature) as exc:
            report['blocked'] = str(exc)
            for item in report['entries']:
                if item['status'] == 'eligible':
                    item.update(status='preserved', reason='batch blocked: ' + str(exc))
        if report.get('paused') and budget.progress == 0 and not report['deleted_files']:
            report['unsupported_budget'] = report.pop('paused')
            report['blocked'] = 'no validation or deletion progress with configured budget'
        if budget.minimum_bytes:
            report['required_bytes_for_blocked_unit'] = budget.minimum_bytes
        if budget.minimum_entries:
            report['minimum_entries'] = budget.minimum_entries
        if apply and not report.get('blocked'):
            # Prune stale names so repeated refreshes cannot grow this forever.
            try:
                for section in ('files', 'directories'):
                    budget.cache[section] = {k: v for k, v in budget.cache[section].items()
                                             if _exists(Path(k))}
                budget.save_checkpoint(root)
            except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
                report['maintenance_warning'] = 'checkpoint maintenance: ' + str(exc)
    report['read_bytes'] = budget.read_bytes
    report['scanned_entries'] = max_entries - budget.entries
    report['identity_checks'] = budget.identity_checks
    return report
