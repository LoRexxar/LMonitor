"""Bounded, fail-closed file-only retention. No database/source access.

Public collect_garbage acquires worker.lock. Publishers already holding that lock
must use _publish_index_locked, never recursively acquire it. History stores only
index descriptors, not body copies. Interrupted publication fails closed.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from botend.services.simc_benchmark_result_snapshot import _write
from botend.services.spec_stats_snapshot import content_digest

STATE = 'retention.manifest'
BLOCKED = 'retention.blocked'
GRACE = 48 * 3600
MAX_ENTRIES = 20000
MAX_BYTES = 128 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_DELETE = 100
ROLES = {'damage', 'tank', 'healer'}
BODY_KEYS = {'season', 'dungeon_id', 'active_period', 'roles', 'source_note', 'key_min', 'key_max', 'generated_at'}
ROW_KEYS = {'rank', 'diff_raw', 'diff_value', 'tier', 'avg', 'avg_value', 'top', 'top_value', 'runs', 'spec_name', 'spec_slug', 'spec_url', 'week', 'updated_at', 'class_name', 'class_name_cn', 'spec_name_cn', 'class_color', 'icon_url'}
ROW_REQUIRED = ROW_KEYS - {'class_name', 'class_name_cn', 'spec_name_cn', 'class_color', 'icon_url'}
UUID = re.compile(r'[0-9a-f]{32}\.json\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')


class Unsafe(ValueError):
    pass


class Uncertain(Unsafe):
    """Read budget or identity cannot establish a stable current."""


class PublicationDeferred(RuntimeError):
    """Retry without modifying publication or retention state."""


class AdoptionRequired(PublicationDeferred):
    """Valid legacy current needs explicit current-only adoption."""


def _certain(ok, message):
    if not ok:
        raise Uncertain(message)


def _require(ok, message):
    if not ok:
        raise Unsafe(message)


def _stamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink, st.st_mode)


@contextmanager
def _root_directory(root, *, initialize=False):
    """Walk without following *any* ancestor link, including before mkdir."""
    root = Path(root).absolute()
    _require('..' not in root.parts, 'parent traversal in snapshot root')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root.anchor, flags)
    try:
        for part in root.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not initialize:
                    yield None
                    return
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _worker_lock(root, *, initialize=False):
    """Same flock inode as the collector/legacy worker, never follow links.

    Read-only callers neither mkdir nor create worker.lock. None means absent
    root, False means busy. Existing roots without locks require explicit setup
    by an apply maintenance run or publisher; adoption never initializes them.
    """
    with _root_directory(root, initialize=initialize) as directory:
        if directory is None:
            yield None
            return
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        if initialize:
            flags |= os.O_CREAT
        try:
            fd = os.open('worker.lock', flags, 0o600, dir_fd=directory)
        except FileNotFoundError as exc:
            raise Unsafe('missing worker.lock; explicit apply/publisher initialization required') from exc
        try:
            st = os.fstat(fd)
            _require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1, 'worker.lock is not a private regular file')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                _require(_identity(os.stat('worker.lock', dir_fd=directory, follow_symlinks=False)) == _identity(st),
                         'worker.lock identity changed')
                with _root_directory(root) as check:
                    _require(check is not None and
                             (os.fstat(check).st_dev, os.fstat(check).st_ino) ==
                             (os.fstat(directory).st_dev, os.fstat(directory).st_ino), 'root identity changed')
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class Reader:
    def __init__(self, root):
        self.root = Path(root)
        self.used = 0
        self.cache = {}
        self.current_missing = False

    def unchanged(self, name, st):
        try:
            actual = (self.root / name).lstat()
        except OSError as exc:
            raise Uncertain('observed file unavailable') from exc
        _certain(_identity(actual) == _identity(st), 'observed file changed')

    def read(self, name):
        _require(isinstance(name, str) and Path(name).name == name, 'unsafe filename')
        if name in self.cache:
            self.unchanged(name, self.cache[name][2])
            return self.cache[name]
        path = self.root / name
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            st = os.fstat(stream.fileno())
            _require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1, 'not a private regular file')
            _certain(st.st_size <= MAX_FILE and self.used + st.st_size <= MAX_BYTES, 'read budget exceeded')
            raw = stream.read(min(MAX_FILE, MAX_BYTES - self.used) + 1)
            self.used += len(raw)
            _certain(len(raw) == st.st_size and self.used <= MAX_BYTES and len(raw) <= MAX_FILE, 'file changed/read budget')
        self.unchanged(name, st)
        data = json.loads(raw)
        digest = hashlib.sha256(raw).hexdigest()
        if UUID.fullmatch(name):
            # Validate the complete body once, retain only its proven identity.
            data = {'scope': _body(data), 'sha256': digest, 'content_hash': content_digest(data)}
        self.unchanged(name, st)
        self.cache[name] = (data, digest, st)
        return self.cache[name]


def _body(data):
    _require(isinstance(data, dict) and set(data) == BODY_KEYS, 'unknown body schema')
    _require(isinstance(data['season'], str) and bool(data['season']) and
             type(data['dungeon_id']) is int and data['dungeon_id'] >= 0 and
             type(data['active_period']) is int and data['active_period'] > 0, 'invalid body scope')
    _require(isinstance(data['roles'], dict) and set(data['roles']) == ROLES, 'incomplete roles')
    for rows in data['roles'].values():
        _require(isinstance(rows, list) and len(rows) <= 80, 'invalid role rows')
        for row in rows:
            _require(isinstance(row, dict) and ROW_REQUIRED <= set(row) <= ROW_KEYS and
                     isinstance(row.get('spec_slug'), str) and bool(row['spec_slug']) and
                     all(not isinstance(v, (dict, list)) for v in row.values()), 'unknown row structure')
    _require(isinstance(data['source_note'], str) and isinstance(data['generated_at'], str) and
             all(data[k] is None or type(data[k]) in (int, float) for k in ('key_min', 'key_max')), 'invalid metadata')
    return [data['season'], data['dungeon_id'], data['active_period']]


def _closure(index, reader, *, legacy=False):
    _require(isinstance(index, dict) and set(index) <= {'schema', 'current_season', 'seasons', 'updated_at'} and
             index.get('schema') == 1 and isinstance(index.get('seasons'), dict) and
             isinstance(index.get('current_season'), str), 'invalid index')
    _require(not index['current_season'] or index['current_season'] in index['seasons'], 'missing current season')
    refs = {}
    for season, meta in index['seasons'].items():
        _require(isinstance(season, str) and bool(season) and isinstance(meta, dict) and
                 set(meta) <= {'periods', 'dungeons', 'shards'} and isinstance(meta.get('shards'), dict) and
                 isinstance(meta.get('periods'), list) and isinstance(meta.get('dungeons'), list), 'unknown season schema')
        for row in meta['periods']:
            _require(isinstance(row, dict) and set(row) == {'id', 'label'} and type(row['id']) is int and
                     row['id'] > 0 and isinstance(row['label'], str), 'unknown period metadata')
        for row in meta['dungeons']:
            _require(isinstance(row, dict) and set(row) == {'id', 'name'} and type(row['id']) is int and
                     row['id'] >= 0 and isinstance(row['name'], str), 'unknown dungeon metadata')
        for key, entry in meta['shards'].items():
            _require(isinstance(key, str) and re.fullmatch(r'\d+:\d+', key) and isinstance(entry, dict) and
                     set(entry) <= {'file', 'origin', 'updated_at', 'facts_hash', 'content_hash', 'checked_at', 'error', 'attempted_at'}, 'unknown shard entry')
            _require(all(isinstance(value, str) for value in entry.values()), 'invalid entry metadata')
            if 'file' not in entry:
                _require(set(entry) <= {'error', 'attempted_at'} and 'error' in entry, 'invalid pending shard')
                continue
            _require(entry.get('origin') in ('collector', 'database'), 'unknown producer')
            name = entry['file']
            _require(isinstance(name, str) and UUID.fullmatch(name), 'invalid shard filename')
            descriptor, _, _ = reader.read(name)
            scope = descriptor['scope']
            _require(scope == [season, *map(int, key.split(':'))], 'scope mismatch')
            expected = entry.get('content_hash')
            _require((legacy and 'content_hash' not in entry) or expected == descriptor['content_hash'], 'missing/mismatched content hash')
            _require(name not in refs or refs[name] == descriptor, 'conflicting identity')
            refs[name] = descriptor
    return refs


def _verify_refs(refs, reader, *, missing_ok=False):
    _require(isinstance(refs, dict), 'invalid reference map')
    for name, desc in refs.items():
        _require(isinstance(name, str) and UUID.fullmatch(name) and isinstance(desc, dict) and
                 set(desc) == {'scope', 'sha256', 'content_hash'} and
                 isinstance(desc['sha256'], str) and HASH.fullmatch(desc['sha256']) and
                 isinstance(desc['content_hash'], str) and HASH.fullmatch(desc['content_hash']) and
                 isinstance(desc['scope'], list) and len(desc['scope']) == 3 and
                 isinstance(desc['scope'][0], str) and bool(desc['scope'][0]) and
                 type(desc['scope'][1]) is int and desc['scope'][1] >= 0 and
                 type(desc['scope'][2]) is int and desc['scope'][2] > 0, 'missing history hash')
        if missing_ok and not os.path.lexists(reader.root / name):
            continue
        actual, _, _ = reader.read(name)
        _require(actual == desc, 'history body mismatch')


def _current(root, reader):
    if 'index.json' in reader.cache:
        return reader.read('index.json')[0]
    try:
        (root / 'index.json').lstat()
    except FileNotFoundError:
        reader.current_missing = True
        return {'schema': 1, 'current_season': '', 'seasons': {}}
    _certain(not reader.current_missing, 'current appeared during publication')
    return reader.read('index.json')[0]


def _publication_guard(root, reader=None):
    """Caller holds worker.lock. Observe current only, never adopt or repair.

    Return the bounded descriptor cache for the remainder of this locked publish.
    Missing bodies and invalid schemas are determinate damage, not migration.
    """
    root = Path(root)
    reader = reader if reader is not None else Reader(root)
    try:
        index = _current(root, reader)
        _closure(index, reader, legacy=True)
        for name in reader.cache:
            reader.read(name)
    except Uncertain as exc:
        raise PublicationDeferred(str(exc)) from exc
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
            raise PublicationDeferred('current read unavailable') from exc
    except (ValueError, TypeError, KeyError):
        pass  # Determinate bad current retains the original repair + sticky path.
    else:
        if any('file' in entry and 'content_hash' not in entry
               for meta in index['seasons'].values() for entry in meta['shards'].values()):
            raise AdoptionRequired('mythicstats current requires explicit hash adoption')
    return reader


def _state(root, reader, current):
    state = reader.read(STATE)[0]
    _require(isinstance(state, dict) and set(state) in ({'schema', 'current', 'history', 'retired'},
             {'schema', 'current', 'history', 'retired', 'pending_deletes'}) and
             state['schema'] == 1 and state['current'] == current and isinstance(state['history'], list) and
             len(state['history']) <= 2 and isinstance(state['retired'], dict), 'invalid/stale retention manifest')
    _verify_refs(state['current'], reader)
    for history in state['history']:
        _require(isinstance(history, dict) and set(history) == {'index', 'index_hash', 'refs', 'retired_at'} and _stamp(history['retired_at']), 'bad publication history')
        _require(history['index_hash'] == content_digest(history['index']), 'history index hash mismatch')
        _require(_closure(history['index'], reader) == history['refs'], 'history index closure mismatch')
        _verify_refs(history['refs'], reader)
    for name, retired in state['retired'].items():
        _require(isinstance(retired, dict) and set(retired) == {'ref', 'retired_at'} and _stamp(retired['retired_at']), 'bad retirement')
        _verify_refs({name: retired['ref']}, reader)
    pending = state.get('pending_deletes', {})
    _require(isinstance(pending, dict) and len(pending) <= MAX_DELETE, 'invalid pending deletes')
    protected = set(current) | set(state['retired'])
    for history in state['history']:
        protected.update(history['refs'])
    _require(not protected.intersection(pending), 'pending delete still referenced')
    _verify_refs(pending, reader, missing_ok=True)
    return state


def _block(root, error):
    # Sticky across successful repair; explicit audited operator recovery required.
    _write(root / BLOCKED, {'schema': 1, 'reason': str(error)[:500]})


def _publish_index_locked(root, index, *, writer=_write, adoption_reader=None, publication_reader=None):
    """Caller MUST hold worker.lock; retain old closure before pointer replacement.

    Bad old pointers/manifests block GC durably but do not prevent repair by a
    valid new publication. A failed pointer write cannot record a real publish.
    """
    root = Path(root)
    if adoption_reader is not None:
        # Explicit current-only migration: never bootstrap/rotate/repair history,
        # and never route validation errors through the repair publisher below.
        reader = adoption_reader
        old_index = reader.read('index.json')[0]
        old = _closure(old_index, reader, legacy=True)
        _require(_closure(index, reader) == old, 'adoption changed current closure')
        if os.path.lexists(root / STATE):
            _state(root, reader, old)
        for name in reader.cache:
            reader.read(name)
        # Raw pointer identity fence, not semantic/content_digest equivalence.
        fresh = Reader(root)
        fresh.used = reader.used
        _, digest, st = fresh.read('index.json')
        reader.used = fresh.used
        _require(digest == reader.cache['index.json'][1] and
                 _identity(st) == _identity(reader.cache['index.json'][2]), 'adoption pointer changed')
        writer(root / 'index.json', index)
        return
    reader = _publication_guard(root, publication_reader)
    new = _closure(index, reader)
    prepared = None
    old_state = None
    try:
        old_index = _current(root, reader)
        old = _closure(old_index, reader, legacy=True)
        old_state = _state(root, reader, old) if os.path.lexists(root / STATE) else {
            'schema': 1, 'current': old, 'history': [], 'retired': {}}
        if new != old or not os.path.lexists(root / STATE):
            prepared = deepcopy(old_state)
            if new != old:
                now = time.time()
                if old:
                    # Do not manufacture hashes for an unhashed historical index.
                    _closure(old_index, reader)
                    candidates = [{'index': old_index, 'index_hash': content_digest(old_index),
                                   'refs': old, 'retired_at': now}, *prepared['history']]
                    # Whole-index generations, not independent per-shard history.
                    # Filter current and deduplicate real closures BEFORE taking two.
                    history = []
                    for candidate in candidates:
                        if candidate['refs'] == new or any(candidate['refs'] == h['refs'] for h in history):
                            continue
                        history.append(candidate)
                    prepared['history'] = history[:2]
                for name, ref in old.items():
                    if name not in new:
                        prepared['retired'][name] = {'ref': ref, 'retired_at': now}
                for name in new:
                    prepared['retired'].pop(name, None)
                prepared['current'] = new
            for name in reader.cache:
                reader.read(name)
            if reader.current_missing:
                _current(root, reader)
            _write(root / STATE, prepared)
    except Uncertain as exc:
        raise PublicationDeferred(str(exc)) from exc
    except (OSError, ValueError, TypeError, KeyError) as exc:
        _block(root, exc)
    try:
        for name in reader.cache:
            if name != STATE:
                reader.read(name)
        writer(root / 'index.json', index)
    except Exception:
        if prepared is not None and old_state is not None:
            # Atomic writer failures before replace leave the old pointer. If the
            # pointer actually moved (e.g. directory fsync failure), keep prepared.
            try:
                actual = _closure(_current(root, Reader(root)), Reader(root), legacy=True)
                if actual == old_state['current']:
                    _write(root / STATE, old_state)
                elif actual != new:
                    _block(root, 'pointer changed during failed publication')
            except (OSError, ValueError, TypeError, KeyError) as exc:
                _block(root, exc)
        raise


def adopt_current_hashes(*, apply=False):
    """Opt-in observed hash adoption for CURRENT entries only; default zero-write.

    Existing worker.lock is required even for apply (initialize explicitly via
    GC apply/publisher first). Complete current/history/pending validation and a
    raw pointer identity fence precede the sole write. Sticky markers survive.
    """
    from botend.services.mythicstats_snapshot import snapshot_root
    root = snapshot_root()
    result = {'apply': apply, 'busy': False, 'blocked': False, 'entries_updated': 0}
    try:
        with _worker_lock(root) as acquired:
            _require(acquired is not None, 'missing snapshot root')
            if not acquired:
                return {**result, 'busy': True}
            reader = Reader(root)
            index = reader.read('index.json')[0]
            current = _closure(index, reader, legacy=True)
            controls = {}
            for name in (STATE, BLOCKED):
                controls[name] = reader.read(name)[1] if os.path.lexists(root / name) else None
            if controls[STATE] is not None:
                _state(root, reader, current)
            migrated = deepcopy(index)
            for meta in migrated['seasons'].values():
                for entry in meta['shards'].values():
                    if 'file' in entry and 'content_hash' not in entry:
                        entry['content_hash'] = reader.read(entry['file'])[0]['content_hash']
                        result['entries_updated'] += 1
            _require(_closure(migrated, reader) == current, 'adoption changed current closure')
            for name in reader.cache:
                reader.read(name)
            if not apply or not result['entries_updated']:
                return result
            # Reserve raw fence + complete readback before the only side effect.
            encoded_size = len(json.dumps(migrated, ensure_ascii=False, separators=(',', ':'),
                                          allow_nan=False).encode('utf-8'))
            _require(encoded_size <= MAX_FILE and reader.used * 2 + encoded_size <= MAX_BYTES,
                     'adoption verification read budget exceeded')
            for name, digest in controls.items():
                _require(os.path.lexists(root / name) == (digest is not None), 'adoption control changed')
            _publish_index_locked(root, migrated, adoption_reader=reader)
            # Verify the exact target and all current body bytes after publication.
            verified = Reader(root)
            verified.used = reader.used
            _require(verified.read('index.json')[0] == migrated and
                     _closure(migrated, verified) == current, 'adoption readback mismatch')
            for name, digest in controls.items():
                actual = verified.read(name)[1] if os.path.lexists(root / name) else None
                _require(actual == digest, 'adoption changed control metadata')
            if controls[STATE] is not None:
                _state(root, verified, current)
            return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {**result, 'blocked': True, 'reason': str(exc)}


def publish_index(index):
    """Offline public publisher for callers not already holding worker.lock."""
    from botend.services.mythicstats_snapshot import snapshot_root
    root = snapshot_root()
    with _worker_lock(root, initialize=True) as acquired:
        if not acquired:
            raise BlockingIOError('mythicstats worker busy')
        _publish_index_locked(root, index)


def collect_garbage(*, apply=False, limit=MAX_DELETE, now=None):
    """Public maintenance API; dry-run creates neither root nor lock."""
    _require(type(limit) is int and 1 <= limit <= MAX_DELETE, 'invalid deletion batch limit')
    try:
        return _collect_garbage(apply=apply, limit=limit, now=now)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {'busy': False, 'blocked': True, 'bootstrap': False, 'deleted': [],
                'candidates': [], 'apply': apply, 'reason': str(exc)}


def _collect_garbage(*, apply, limit, now):
    """Bootstrap always deletes zero; errors before lock acquisition fail closed."""
    from botend.services.mythicstats_snapshot import snapshot_root
    _require(type(limit) is int and 1 <= limit <= MAX_DELETE, 'invalid deletion batch limit')
    now = time.time() if now is None else now
    _require(_stamp(now), 'invalid clock')
    root = snapshot_root()
    result = {'busy': False, 'blocked': False, 'bootstrap': False, 'deleted': [], 'candidates': [], 'apply': apply}
    with _worker_lock(root, initialize=apply) as acquired:
        if acquired is None:
            return {**result, 'bootstrap': True}
        if not acquired:
            return {**result, 'busy': True}
        try:
            _require(not root.is_symlink(), 'symlink root')
            _require(not os.path.lexists(root / BLOCKED), 'persistent GC block; audited recovery required')
            names = []
            with os.scandir(root) as entries:
                for entry in entries:
                    _require(len(names) < MAX_ENTRIES, 'directory entry budget exceeded')
                    _require(entry.is_file(follow_symlinks=False) and not entry.is_symlink(), 'unknown/symlink directory entry')
                    # Control files (notably worker.lock) are not all read by Reader.
                    _require(entry.stat(follow_symlinks=False).st_nlink == 1, 'hardlinked directory entry')
                    _require(entry.name in {'index.json', STATE, 'worker.lock'} or UUID.fullmatch(entry.name), 'unknown directory entry')
                    names.append(entry.name)
            _require('index.json' in names or not any(UUID.fullmatch(name) for name in names),
                     'missing pointer with existing bodies')
            reader = Reader(root)
            current = _closure(_current(root, reader), reader, legacy=True)
            if not os.path.lexists(root / STATE):
                if apply:
                    _write(root / STATE, {'schema': 1, 'current': current, 'history': [], 'retired': {}})
                return {**result, 'bootstrap': True}
            state = _state(root, reader, current)
            protected = set(current)
            for history in state['history']:
                protected.update(history['refs'])
            protected.update(name for name, retired in state['retired'].items() if now - retired['retired_at'] < GRACE)
            candidates = []
            # Validate ALL bounded files, including retained bodies, before unlink.
            for name in sorted(names):
                if not UUID.fullmatch(name):
                    continue
                _, digest, st = reader.read(name)
                if name not in protected and now - st.st_mtime >= GRACE:
                    candidates.append((name, digest, st))
            pending = state.get('pending_deletes', {})
            work = list(pending)[:limit] if pending else [name for name, _, _ in candidates[:limit]]
            if pending:
                candidates = [(name, reader.cache[name][1], reader.cache[name][2]) for name in work if name in reader.cache]
            else:
                candidates = candidates[:limit]
            result['candidates'] = [name for name, _, _ in candidates]
            if not apply:
                return result
            # Recheck all cached identities before committing a deletion intent.
            for name in reader.cache:
                reader.read(name)
            if work and not pending:
                state['pending_deletes'] = {name: reader.cache[name][0] for name in work}
                for name in work:
                    state['retired'].pop(name, None)
                _write(root / STATE, state)
                del reader.cache[STATE]
            pointer = reader.read('index.json')[1] if 'index.json' in names else None
            state_hash = reader.read(STATE)[1]
            control_size = reader.read(STATE)[2].st_size + (reader.read('index.json')[2].st_size if pointer else 0)
            _require(reader.used + sum(control_size + st.st_size for _, _, st in candidates) <= MAX_BYTES,
                     'recheck read budget exceeded')
            for name, digest, st in candidates:
                # Re-read exact pointer/control bytes and candidate for each unlink.
                fresh = Reader(root)
                fresh.used = reader.used
                actual = fresh.read('index.json')[1] if os.path.lexists(root / 'index.json') else None
                _require(actual == pointer and fresh.read(STATE)[1] == state_hash and
                         not os.path.lexists(root / BLOCKED), 'pointer/control changed')
                _, actual_hash, actual_st = fresh.read(name)
                _require(actual_hash == digest and _identity(actual_st) == _identity(st), 'candidate changed')
                reader.used = fresh.used
                (root / name).unlink()
                result['deleted'].append(name)
            if work:
                # Make missing files durable before acknowledging the intent.
                fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                for name in work:
                    state['pending_deletes'].pop(name, None)
                _write(root / STATE, state)
            return result
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return {**result, 'blocked': True, 'reason': str(exc)}
