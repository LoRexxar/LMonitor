"""Conservative, bounded GC of disposable spec-stat projections only.

Call maintain_scope or maintain_scopes from an independent maintenance worker.
Unknown history is never inferred from directory times. A disabled marker is
sticky: repair by a publisher does not authorize deletion of uncertain history.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

if os.name != 'nt':
    import fcntl

from .simc_benchmark_result_snapshot import _write

MANIFEST = '.stats-retention.json'
DISABLED = '.stats-retention-disabled.json'
LOCK = '.stats-publish.lock'
CHECKPOINT = '.stats-validation.json'
MAX_ENTRIES = 512
MAX_BYTES = 64 * 1024 * 1024
GRACE = 48 * 3600
MODULES = ('dungeon', 'raid')
GEN = re.compile(r'[0-9a-f]{32}')
KEY = re.compile(r'(dungeon-(all|\d+)|raid-[45]-\d+)')


class Unsafe(ValueError):
    pass


class ObservationChanged(Unsafe):
    """A read could not establish a stable current, not proof of corruption."""


class PublicationDeferred(RuntimeError):
    """Retry publication after an unresolved current observation."""


class AdoptionRequired(PublicationDeferred):
    """A valid hashless current requires explicit current-only migration."""


def _safe_path(path):
    for parent in (path, *path.parents):
        if parent.is_symlink() or (parent.exists() and getattr(parent.lstat(), 'st_file_attributes', 0)
                                  & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise Unsafe('symlink')


def _entries(path):
    _safe_path(path)
    with os.scandir(path) as entries:
        result = []
        for entry in entries:
            if len(result) >= MAX_ENTRIES:
                raise Paused('directory entry budget exceeded')
            if entry.is_symlink():
                raise Unsafe('symlink')
            result.append(Path(entry.path))
    return sorted(result)


class Paused(Unsafe):
    pass


def _identity(path):
    _safe_path(path)
    st = path.lstat()
    if not (stat.S_ISDIR(st.st_mode) or (stat.S_ISREG(st.st_mode) and st.st_nlink == 1)):
        raise Unsafe('unsafe file identity')
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


@contextmanager
def _scope_lock(path, blocking=True, create=True):
    _safe_path(path)
    if not path.exists() and not create:
        yield True
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        # 复用已有真实跨进程字节锁；不通过空实现绕过发布互斥。
        from .simc_benchmark_result_snapshot import _lock
        before = _identity(path)[:2] if path.exists() else None
        with _lock(path, blocking=blocking) as acquired:
            if acquired:
                _safe_path(path)
                if before is not None and _identity(path)[:2] != before:
                    raise Unsafe('lock replaced')
            yield acquired
        return
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise Unsafe('unsafe lock identity')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        if _identity(path)[:2] != [st.st_dev, st.st_ino]:
            raise Unsafe('lock replaced')
        yield True
    finally:
        os.close(fd)


def _inventory(scope):
    result = {}
    for name in (LOCK, MANIFEST, 'dungeon.json', 'raid.json', DISABLED):
        path = scope / name
        _safe_path(path)
        result[name] = _identity(path) if path.exists() else None
    root = scope / 'stats-details'
    _safe_path(root)
    if root.exists():
        result['stats-details'] = _identity(root)
        count = 0
        for directory in _entries(root):
            if not GEN.fullmatch(directory.name) or not directory.is_dir():
                raise Unsafe('unknown generation')
            result[str(directory.relative_to(scope))] = _identity(directory)
            for path in _entries(directory):
                count += 1
                if count > MAX_ENTRIES:
                    raise Unsafe('scope entry budget')
                result[str(path.relative_to(scope))] = _identity(path)
    return result


class Budget:
    def __init__(self, *, reuse=False):
        self.reuse = reuse
        self.reads = {}
        self.identities = {}
        self.remaining = MAX_BYTES
        self.hashes = {}
        self.cache = {}
        self.scope = None
        self.inventory = None
        self.digests = {}

    def checkpoint(self, scope):
        self.scope = scope
        self.inventory = _inventory(scope)
        self.controls = {str(p.relative_to(scope)): digest for p, digest in self.hashes.items()}
        path = scope / CHECKPOINT
        _safe_path(path)
        if path.exists():
            from django.core.signing import BadSignature
            try:
                saved = self.read(path)
                if not isinstance(saved, dict) or set(saved) != {'proof'} or not isinstance(saved['proof'], str):
                    return
                saved = self._signer().unsign_object(saved['proof'])
                if self._valid_checkpoint(saved):
                    self.cache = saved['files']
            except Paused:
                raise
            except (BadSignature, ValueError, TypeError, KeyError):
                # Legacy, damaged, or rotated-key caches are hints, not proof.
                # Revalidate actual files; never trust a self-reported checksum.
                return

    @staticmethod
    def _signer():
        from django.core.signing import Signer
        # Trust boundary: checkpoint may be edited, application SECRET_KEY may not.
        # Do not accept rotated keys through SECRET_KEY_FALLBACKS.
        return Signer(salt='spec-stats-retention.checkpoint.v2', fallback_keys=[])

    def _valid_checkpoint(self, saved):
        if (not isinstance(saved, dict) or set(saved) != {'schema', 'scope', 'inventory', 'controls', 'files'}
                or type(saved['schema']) is not int or saved['schema'] != 2
                or saved['scope'] != str(self.scope.absolute())
                or saved['inventory'] != self.inventory or saved['controls'] != self.controls
                or not isinstance(saved['files'], dict) or len(saved['files']) > MAX_ENTRIES):
            return False
        for name, item in saved['files'].items():
            if (not isinstance(name, str) or not re.fullmatch(
                    r'stats-details/[0-9a-f]{32}/(dungeon-(all|\d+)|raid-[45]-\d+)\.json', name)
                    or name not in self.inventory or not isinstance(item, dict)
                    or set(item) != {'raw_hash', 'digest', 'body'}
                    or any(not isinstance(item[k], str) or not re.fullmatch('[0-9a-f]{64}', item[k])
                           for k in ('raw_hash', 'digest'))):
                return False
            body = item['body']
            if (not isinstance(body, dict) or set(body) != {'schema', 'scope', 'generation', 'key', 'data'}
                    or type(body['schema']) is not int or body['schema'] != 1
                    or body['scope'] != list(self.scope.parts[-3:])
                    or body['generation'] != name.split('/')[1] or body['key'] != Path(name).stem
                    or not isinstance(body['data'], dict) or set(body['data']) - {'dungeon_id', 'boss_id'}
                    or any(type(v) not in (int, str) for v in body['data'].values())):
                return False
        return True

    def save(self):
        if self.scope is not None and _inventory(self.scope) == self.inventory:
            saved = {'schema': 2, 'scope': str(self.scope.absolute()), 'inventory': self.inventory,
                     'controls': self.controls, 'files': self.cache}
            if self._valid_checkpoint(saved):
                _write(self.scope / CHECKPOINT, {'proof': self._signer().sign_object(saved)})

    def digest(self, path, body):
        from .spec_stats_snapshot import content_digest
        return self.digests[path] if path in self.digests else content_digest(body['data'])

    def read(self, path):
        identity = _identity(path)
        if self.reuse and path in self.reads:
            if identity != self.identities[path]:
                raise ObservationChanged('file changed between observations')
            return self.reads[path]
        relative = str(path.relative_to(self.scope)) if self.scope is not None else ''
        detail = relative.startswith('stats-details/')
        if detail and relative in self.cache:
            if identity != self.inventory.get(relative):
                raise ObservationChanged('checkpoint file changed')
            item = self.cache[relative]
            self.hashes[path] = item['raw_hash']
            self.digests[path] = item['digest']
            return item['body']
        size = identity[2]
        if size > self.remaining:
            raise Paused('file or byte budget')
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError as exc:
            raise ObservationChanged('file disappeared during read') from exc
        with os.fdopen(fd, 'rb') as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or [st.st_dev, st.st_ino] != identity[:2]:
                raise ObservationChanged('file changed')
            raw = stream.read(size)
        self.remaining -= len(raw)
        try:
            final_identity = _identity(path)
        except FileNotFoundError as exc:
            raise ObservationChanged('file disappeared during read') from exc
        if len(raw) != size or final_identity != identity:
            raise ObservationChanged('file changed')
        self.hashes[path] = hashlib.sha256(raw).hexdigest()
        body = json.loads(raw)
        if self.reuse:
            self.identities[path] = identity
            compact = body
            if (path.parent.parent.name == 'stats-details' and isinstance(body, dict)
                    and isinstance(body.get('data'), dict)):
                from .spec_stats_snapshot import content_digest
                self.digests[path] = content_digest(body['data'])
                compact = {**body, 'data': {k: body['data'][k] for k in ('dungeon_id', 'boss_id') if k in body['data']}}
            self.reads[path] = compact
        if detail and isinstance(body, dict) and isinstance(body.get('data'), dict):
            from .spec_stats_snapshot import content_digest
            digest = content_digest(body['data'])
            compact = {**body, 'data': {k: body['data'][k] for k in ('dungeon_id', 'boss_id') if k in body['data']}}
            self.cache[relative] = {'raw_hash': self.hashes[path], 'digest': digest, 'body': compact}
            self.digests[path] = digest
        return body


def _meta(meta, scope, module):
    if (not isinstance(meta, dict) or meta.get('schema') != 1
            or meta.get('module') != module or meta.get('scope') != list(scope.parts[-3:])
            or not GEN.fullmatch(str(meta.get('generation', '')))):
        raise Unsafe('invalid publication identity')
    keys = meta.get('details')
    hashes = meta.get('detail_hashes')
    if (not isinstance(keys, list) or not keys or len(keys) > MAX_ENTRIES
            or any(not isinstance(k, str) or not KEY.fullmatch(k) or not k.startswith(module + '-') for k in keys)
            or len(set(keys)) != len(keys) or not isinstance(hashes, dict) or set(hashes) != set(keys)
            or any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v) for v in hashes.values())):
        raise Unsafe('missing or invalid detail hashes')
    return {key: meta[key] for key in ('schema', 'module', 'scope', 'generation', 'details', 'detail_hashes')}


def _currents(scope, budget):
    from .spec_stats_snapshot import _valid_index_containers, content_digest
    result = {}
    for module in MODULES:
        path = scope / f'{module}.json'
        _safe_path(path)
        if not path.exists():
            continue
        index = budget.read(path)
        if not isinstance(index, dict) or not _valid_index_containers(index, module):
            raise Unsafe('invalid current index')
        meta = _meta(index.get('_snapshot'), scope, module)
        source_meta = index['_snapshot']
        if set(source_meta) - {'schema', 'module', 'generation', 'scope', 'details',
                               'detail_hashes', 'index_hash', 'payload_hash', 'input_fingerprint'}:
            raise Unsafe('unknown snapshot metadata')
        allowed = {'generated_at', 'updated_at', '_snapshot'} | (
            {'dungeons'} if module == 'dungeon' else {'difficulties', 'zone_groups'})
        if set(index) - allowed:
            raise Unsafe('unknown index fields')
        if ('index_hash' in source_meta and source_meta['index_hash'] !=
                content_digest({k: v for k, v in index.items() if k != '_snapshot'})):
            raise Unsafe('invalid index hash')
        result[module] = meta
    return result


def _manifest(scope, budget):
    data = budget.read(scope / MANIFEST)
    if (not isinstance(data, dict)
            or set(data) not in ({'schema', 'scope', 'records'},
                                {'schema', 'scope', 'records', 'pending_deletes'})
            or data['schema'] != 1 or data['scope'] != list(scope.parts[-3:])
            or not isinstance(data['records'], list) or len(data['records']) > MAX_ENTRIES):
        raise Unsafe('invalid manifest')
    seen = set()
    for row in data['records']:
        if not isinstance(row, dict) or set(row) != {'meta', 'retired_at'}:
            raise Unsafe('invalid record')
        if not isinstance(row['meta'], dict) or set(row['meta']) != {
                'schema', 'module', 'scope', 'generation', 'details', 'detail_hashes'}:
            raise Unsafe('unknown record metadata')
        meta = _meta(row['meta'], scope, row['meta'].get('module'))
        if meta['module'] not in MODULES or meta['generation'] in seen:
            raise Unsafe('duplicate or unknown record')
        seen.add(meta['generation'])
        if row['retired_at'] is not None and (type(row['retired_at']) not in (float, int) or not 0 <= row['retired_at'] < float('inf')):
            raise Unsafe('invalid retirement clock')
    pending = data.get('pending_deletes', [])
    if not isinstance(pending, list) or len(pending) > MAX_ENTRIES:
        raise Unsafe('invalid pending delete budget')
    for item in pending:
        if not isinstance(item, dict) or set(item) != {'meta', 'files'}:
            raise Unsafe('invalid pending delete')
        meta = item['meta']
        if not isinstance(meta, dict) or _meta(meta, scope, meta.get('module')) != meta:
            raise Unsafe('invalid pending metadata')
        if meta['module'] not in MODULES or meta['generation'] in seen:
            raise Unsafe('pending generation still recorded or duplicate')
        seen.add(meta['generation'])
        files = item['files']
        if (not isinstance(files, dict) or set(files) != {k + '.json' for k in meta['details']}
                or any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v)
                       for v in files.values())):
            raise Unsafe('invalid pending file hashes')
    return data


def _validate_generation(scope, meta, budget):
    from .spec_stats_snapshot import content_digest
    directory = scope / 'stats-details' / meta['generation']
    paths = _entries(directory)
    if {p.name for p in paths} != {k + '.json' for k in meta['details']}:
        raise Unsafe('unknown generation member')
    latest = directory.stat().st_mtime
    for path in paths:
        body = budget.read(path)
        key = path.stem
        if (not isinstance(body, dict) or set(body) != {'schema', 'scope', 'generation', 'key', 'data'}
                or any(body.get(k) != meta[k] for k in ('schema', 'scope', 'generation'))
                or body.get('key') != key or not isinstance(body.get('data'), dict)
                or budget.digest(path, body) != meta['detail_hashes'][key]):
            raise Unsafe('invalid detail envelope or hash')
        latest = max(latest, path.stat().st_mtime)
    return paths, latest


def _known_orphan(scope, directory, budget):
    """Recognize producer envelopes, not evidence of historical publication.

    Partial failed publishes are allowed: every surviving member must be a
    known detail. No absent index or hash is reconstructed as rollback history.
    """
    from .spec_stats_snapshot import content_digest
    paths = _entries(directory)
    if not paths:
        raise Unsafe('empty unknown generation')
    hashes = {}
    module = None
    latest = directory.stat().st_mtime
    for path in paths:
        key = path.stem
        if path.suffix != '.json' or not KEY.fullmatch(key):
            raise Unsafe('unknown orphan member')
        member_module = key.split('-')[0]
        if module is not None and member_module != module:
            raise Unsafe('mixed orphan modules')
        module = member_module
        body = budget.read(path)
        if (not isinstance(body, dict) or set(body) != {'schema', 'scope', 'generation', 'key', 'data'}
                or type(body['schema']) is not int or body['schema'] != 1
                or body['scope'] != list(scope.parts[-3:]) or body['generation'] != directory.name
                or body['key'] != key or not isinstance(body['data'], dict)):
            raise Unsafe('invalid orphan envelope')
        if key != 'dungeon-all':
            identity = 'dungeon_id' if module == 'dungeon' else 'boss_id'
            if str(body['data'].get(identity)) != key.split('-')[-1]:
                raise Unsafe('orphan detail key identity mismatch')
        hashes[key] = budget.digest(path, body)
        latest = max(latest, path.stat().st_mtime)
    meta = {'schema': 1, 'scope': list(scope.parts[-3:]), 'module': module,
            'generation': directory.name, 'details': list(hashes), 'detail_hashes': hashes}
    return meta, paths, latest


def _pending_paths(scope, item, budget):
    """Missing members are expected only for durable deletion intents."""
    directory = scope / 'stats-details' / item['meta']['generation']
    _safe_path(directory)
    if not directory.exists():
        return []
    paths = _entries(directory)
    for path in paths:
        if path.name not in item['files']:
            raise Unsafe('unknown pending member')
        budget.read(path)
        if budget.hashes[path] != item['files'][path.name]:
            raise Unsafe('pending member changed')
    return paths


def _sync_directory(path):
    if os.name == 'nt':
        # Windows不支持目录fsync；文件内容仍由原子写入器刷新后替换。
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _disable(scope, reason):
    path = scope / DISABLED
    _safe_path(path)
    _write(path, {'schema': 1, 'reason': str(reason)[:200]})


def _bootstrap(scope, currents):
    data = {'schema': 1, 'scope': list(scope.parts[-3:]),
            'records': [{'meta': meta, 'retired_at': None} for meta in currents.values()]}
    _write(scope / MANIFEST, data)
    return data


@contextmanager
def publication(scope):
    """Lock from before the first shard through durable publication accounting."""
    scope = Path(scope)
    _safe_path(scope / LOCK)
    with _scope_lock(scope / LOCK):
        budget = Budget(reuse=True)
        corrupt = _publication_gate(scope, budget)
        try:
            if corrupt is not None:
                raise corrupt
            currents = _currents(scope, budget)
            _safe_path(scope / MANIFEST)
            if not (scope / MANIFEST).exists():
                for meta in currents.values():
                    _validate_generation(scope, meta, budget)
                _bootstrap(scope, currents)
            else:
                data = _manifest(scope, budget)
                active = {row['meta']['module']: row['meta'] for row in data['records']
                          if row['retired_at'] is None}
                if active != currents:
                    raise Unsafe('manifest/current disagreement before publication')
        except (Paused, ObservationChanged) as exc:
            raise PublicationDeferred('publication observation unresolved') from exc
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError):
                raise PublicationDeferred('publication IO unresolved') from exc
            _disable(scope, exc)
        except (ValueError, TypeError, KeyError) as exc:
            _disable(scope, exc)
        yield


def record_publication(scope):
    """Called only after index publication, while the publication lock is held."""
    scope = Path(scope)
    if (scope / DISABLED).exists():
        return
    budget = Budget()
    data = _manifest(scope, budget)
    original = json.dumps(data, sort_keys=True)
    currents = _currents(scope, budget)
    now = time.time()
    by_gen = {row['meta']['generation']: row for row in data['records']}
    for row in data['records']:
        current = currents.get(row['meta']['module'])
        if current != row['meta'] and row['retired_at'] is None:
            row['retired_at'] = now
    for meta in currents.values():
        if meta['generation'] not in by_gen:
            data['records'].append({'meta': meta, 'retired_at': None})
    if len(data['records']) > MAX_ENTRIES:
        _disable(scope, 'manifest record budget exceeded')
        return
    if json.dumps(data, sort_keys=True) != original:
        _write(scope / MANIFEST, data)


def maintain_scope(scope, *, delete_limit=8, now=None, dry_run=False):
    """Validate every recorded generation before deleting a bounded batch.

    Returns explicit fail-closed status; no database or source files are touched.
    First maintenance of a legacy scope bootstraps current pointers, deleting zero.
    """
    scope = Path(scope)
    now = time.time() if now is None else now
    result = {'scope': str(scope), 'deleted': 0, 'status': 'refused'}
    if not 1 <= delete_limit <= MAX_ENTRIES:
        raise ValueError('delete_limit outside budget')
    try:
        _safe_path(scope / LOCK)
        with _scope_lock(scope / LOCK, blocking=False, create=not dry_run) as acquired:
            if not acquired:
                return {**result, 'status': 'locked'}
            _safe_path(scope / DISABLED)
            if (scope / DISABLED).exists():
                return {**result, 'status': 'disabled'}
            budget = Budget()
            currents = _currents(scope, budget)
            _safe_path(scope / MANIFEST)
            if not (scope / MANIFEST).exists():
                for meta in currents.values():
                    _validate_generation(scope, meta, budget)
                if not dry_run:
                    _bootstrap(scope, currents)
                return {**result, 'status': 'bootstrapped'}
            data = _manifest(scope, budget)
            budget.checkpoint(scope)
            rows = data['records']
            by_gen = {r['meta']['generation']: r for r in rows}
            for meta in currents.values():
                row = by_gen.get(meta['generation'])
                if not row or row['meta'] != meta or row['retired_at'] is not None:
                    raise Unsafe('current not durably recorded')
            for row in rows:
                if row['retired_at'] is None and currents.get(row['meta']['module']) != row['meta']:
                    raise Unsafe('active publication pointer missing or changed')
            protected = {m['generation'] for m in currents.values()}
            for module in MODULES:
                protected.update(r['meta']['generation'] for r in [r for r in rows if r['meta']['module'] == module and r['meta']['generation'] not in protected][-2:])
            root = scope / 'stats-details'
            pending = data.get('pending_deletes', [])
            pending_gens = {item['meta']['generation'] for item in pending}
            if pending_gens & protected:
                raise Unsafe('pending generation protected')
            candidates = []
            for row in rows:
                meta = row['meta']
                paths, modified = _validate_generation(scope, meta, budget)
                retired = row['retired_at']
                if meta['generation'] not in protected and retired is not None and max(retired, modified) <= now - GRACE:
                    candidates.append((meta, paths))
            _safe_path(root)
            if root.exists():
                for directory in _entries(root):
                    if not GEN.fullmatch(directory.name) or not directory.is_dir():
                        raise Unsafe('unknown generation')
                    if directory.name not in by_gen and directory.name not in pending_gens:
                        meta, paths, modified = _known_orphan(scope, directory, budget)
                        if modified <= now - GRACE:
                            candidates.append((meta, paths))
            # Recovery takes priority and consumes the same deletion budget.
            # Validate every surviving pending file before deleting any member.
            if pending:
                validated = [(item, _pending_paths(scope, item, budget)) for item in pending]
                work = []
                actual = 0
                for item, paths in validated:
                    exists = (root / item['meta']['generation']).exists()
                    if not exists or actual < delete_limit:
                        work.append((item, paths))
                        actual += int(exists)
            else:
                work = [({'meta': meta, 'files': {p.name: budget.hashes[p] for p in paths}}, paths)
                        for meta, paths in candidates[:delete_limit]]
            # Final exact pointer/manifest comparison before any destructive work.
            if _inventory(scope) != budget.inventory:
                raise Unsafe('validation snapshot changed')
            prior_hashes = dict(budget.hashes)
            check = budget
            if (_currents(scope, check) != currents or _manifest(scope, check) != data
                    or any(prior_hashes.get(path) != digest for path, digest in check.hashes.items() if path in prior_hashes)):
                raise Unsafe('publication changed')
            if not dry_run and work:
                if not pending:
                    deleting = {item['meta']['generation'] for item, _ in work}
                    # Atomically transfer records into durable deletion intents:
                    # no crash can leave missing files referenced as history.
                    data['records'] = [r for r in rows if r['meta']['generation'] not in deleting]
                    data['pending_deletes'] = [item for item, _ in work]
                    _write(scope / MANIFEST, data)
                for item, paths in work:
                    directory = root / item['meta']['generation']
                    existed = directory.exists()
                    for path in paths:
                        path.unlink()
                    if existed:
                        directory.rmdir()
                        result['deleted'] += 1
                # Flush directory removal before acknowledging deletion intents.
                if root.exists():
                    _sync_directory(root)
                completed = {item['meta']['generation'] for item, _ in work}
                data['pending_deletes'] = [item for item in data['pending_deletes']
                                           if item['meta']['generation'] not in completed]
                _write(scope / MANIFEST, data)
            return {**result, 'status': 'ok', 'eligible': len(work),
                    'pending': len(data.get('pending_deletes', [])),
                    'bytes_read': MAX_BYTES - budget.remaining}
    except Paused as exc:
        if not dry_run and 'budget' in locals():
            try:
                with _scope_lock(scope / LOCK, blocking=False) as acquired:
                    if acquired:
                        budget.save()
            except (OSError, ValueError, TypeError, KeyError) as save_exc:
                return {**result, 'reason': str(save_exc)}
        return {**result, 'status': 'paused', 'reason': str(exc),
                'validated_files': len(budget.cache), 'bytes_read': MAX_BYTES - budget.remaining}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {**result, 'reason': str(exc)}


def maintain_scopes(root, *, after='', scope_limit=8, delete_limit=8, dry_run=False):
    """Bounded hierarchy traversal; caller persists next_cursor and refused scopes.

    Each directory is capped at MAX_ENTRIES, as are total visited directory
    entries. Oversized trees refuse rather than silently scanning the whole site.
    """
    if not 1 <= scope_limit <= MAX_ENTRIES:
        raise ValueError('scope_limit outside budget')
    root = Path(root)
    results = []
    remaining = MAX_ENTRIES
    cursor = after
    def walk(path, depth):
        nonlocal remaining
        entries = _entries(path)
        remaining -= len(entries)
        if remaining < 0:
            raise Unsafe('global traversal budget exceeded')
        for child in entries:
            if not child.is_dir():
                continue
            relative = child.relative_to(root).as_posix()
            if depth == 1:
                if relative > after:
                    yield child
            elif not after or relative >= '/'.join(after.split('/')[:4-depth]):
                yield from walk(child, depth - 1)
    try:
        for scope in walk(root, 3):
            if len(results) == scope_limit:
                return {'results': results, 'next_cursor': cursor, 'has_more': True}
            results.append(maintain_scope(scope, delete_limit=delete_limit, dry_run=dry_run))
            cursor = scope.relative_to(root).as_posix()
        return {'results': results, 'next_cursor': cursor, 'has_more': False}
    except (OSError, ValueError) as exc:
        return {'results': results, 'next_cursor': cursor, 'has_more': True, 'error': str(exc)}


def _observe_current(scope, module, budget):
    """Lock-held, read-only migration validation; return proposed index + missing flag."""
    from .spec_stats_snapshot import _valid_index_containers, content_digest
    path = scope / (module + '.json')
    _safe_path(path)
    try:
        path.lstat()
    except FileNotFoundError:
        budget.identities[path] = None
        return None, False
    try:
        index = budget.read(path)
    except FileNotFoundError as exc:
        raise ObservationChanged('current disappeared during observation') from exc
    if not isinstance(index, dict) or not _valid_index_containers(index, module):
        raise Unsafe('invalid current index')
    meta = index.get('_snapshot')
    if not isinstance(meta, dict):
        raise Unsafe('invalid current metadata')
    allowed = {'generated_at', 'updated_at', '_snapshot'} | (
        {'dungeons'} if module == 'dungeon' else {'difficulties', 'zone_groups'})
    if set(index) - allowed or set(meta) - {'schema', 'module', 'generation', 'scope',
            'details', 'detail_hashes', 'index_hash', 'payload_hash', 'input_fingerprint'}:
        raise Unsafe('unknown current fields')
    if 'index_hash' in meta and meta['index_hash'] != content_digest({
            k: v for k, v in index.items() if k != '_snapshot'}):
        raise Unsafe('invalid current index hash')
    missing = 'detail_hashes' not in meta
    keys = meta.get('details')
    if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys):
        raise Unsafe('invalid current details')
    provisional = {**meta, 'detail_hashes': {k: '0' * 64 for k in keys}} if missing else meta
    checked = _meta(provisional, scope, module)
    directory = scope / 'stats-details' / checked['generation']
    identity = _identity(directory)
    try:
        if missing:
            observed, _, _ = _known_orphan(scope, directory, budget)
            if set(observed['details']) != set(checked['details']) or observed['module'] != module:
                raise Unsafe('current member mismatch')
            index = {**index, '_snapshot': {**meta, 'detail_hashes': observed['detail_hashes']}}
        else:
            _validate_generation(scope, checked, budget)
        if _identity(directory) != identity:
            raise ObservationChanged('current directory changed')
    except FileNotFoundError as exc:
        raise ObservationChanged('current member disappeared during observation') from exc
    budget.identities[directory] = identity
    return index, missing


def _publication_gate(scope, budget):
    """Classify BOTH currents before any application write, even after corruption."""
    adoption, unresolved, corrupt = [], [], []
    for module in MODULES:
        try:
            _, missing = _observe_current(scope, module, budget)
            if missing:
                adoption.append(module)
        except (Paused, ObservationChanged) as exc:
            unresolved.append(exc)
        except FileNotFoundError as exc:
            # A missing referenced member is deterministic corruption.
            corrupt.append(exc)
        except OSError as exc:
            unresolved.append(exc)
        except (ValueError, TypeError, KeyError) as exc:
            corrupt.append(exc)
    try:
        for path, identity in budget.identities.items():
            _safe_path(path)
            try:
                current_identity = _identity(path)
            except FileNotFoundError:
                current_identity = None
            if current_identity != identity:
                unresolved.append(ObservationChanged('current observation changed'))
    except (OSError, Unsafe) as exc:
        unresolved.append(exc)
    if unresolved:
        raise PublicationDeferred('current observation unresolved') from unresolved[0]
    if adoption:
        raise AdoptionRequired('explicit current hash migration required: ' + ', '.join(adoption))
    return corrupt[0] if corrupt else None


def migrate_current_hashes(scope, *, apply=False):
    """Explicit current-only observation, never invent history or clear DISABLED."""
    scope = Path(scope)
    result = {'scope': str(scope), 'status': 'refused', 'changed': []}
    try:
        with _scope_lock(scope / LOCK, blocking=False, create=apply) as acquired:
            if not acquired:
                return {**result, 'status': 'locked'}
            budget = Budget()
            inventory = _inventory(scope)
            updates = []
            for module in MODULES:
                index, missing = _observe_current(scope, module, budget)
                if missing:
                    updates.append((scope / (module + '.json'), index))
            if inventory != _inventory(scope):
                raise Unsafe('migration snapshot changed')
            if apply:
                for path, index in updates:
                    _write(path, index)
            return {**result, 'status': 'migrated' if apply else 'dry-run',
                    'changed': [p.name for p, _ in updates], 'bytes_read': MAX_BYTES - budget.remaining}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {**result, 'reason': str(exc)}
