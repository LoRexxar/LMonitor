"""Conservative GC for flat, rebuildable news/RIO/journal projections only.

Public worker API: maintain_snapshot_cache / maintain_snapshot_caches (dry-run
by default). Publishers call publish_snapshot_index *inside publish.lock*.
No source, database, backup, temporary or unknown filename is ever deleted.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from django.conf import settings
from botend.services.simc_benchmark_result_snapshot import _lock, _write

HISTORY = 'gc-history.manifest'
BLOCKED = 'gc-blocked.manifest'
GRACE_SECONDS = 48 * 3600
NEWS_SECTIONS = ('blueposts', 'exwind', 'wowhead', 'nga_preview', 'nga_hot', 'nga_fallback')
ROOTS = {'news': ('NEWS_SNAPSHOT_ROOT', 'var/news'),
         'rio': ('RIO_RANKINGS_SNAPSHOT_ROOT', 'var/rio-rankings'),
         'journal': ('JOURNAL_SNAPSHOT_ROOT', 'var/journal')}


class UnsafeCache(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise UnsafeCache(message)


def digest(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _name(kind, value):
    require(isinstance(value, str) and re.fullmatch(
        r'[0-9a-f]{32}\.json' if kind == 'rio' else r'[0-9a-f]{64}\.json', value),
        'unknown content filename')
    return value


def _safe_root(root):
    root = Path(os.path.abspath(root))
    for path in (*reversed(root.parents), root):
        require(not path.is_symlink(), f'symlink directory: {path}')
    require(root.is_dir(), 'cache root missing')
    for name in ('index.json', HISTORY, BLOCKED):
        require(not (root / name).is_symlink(), f'symlink control file: {name}')
    lock = root / 'publish.lock'
    if lock.exists() or lock.is_symlink():
        require(stat.S_ISREG(lock.lstat().st_mode), 'unsafe publisher lock')
    return root


def _identity(path):
    s = path.lstat()
    require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1, f'non-regular/shared file: {path.name}')
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


class Reader:
    def __init__(self, root, kind):
        self.root, self.kind = root, kind
        self.fingerprints = {}
        self._metadata = {}
        self._closures = {}
        self.bytes = 0
        self.maximum = int(getattr(settings, 'SNAPSHOT_CACHE_GC_MAX_BYTES', 256 * 1024 * 1024))

    def read(self, name):
        path = self.root / name
        before = _identity(path)
        if name in self.fingerprints:
            require(before == self.fingerprints[name], 'file changed while validating')
        require(before[2] <= 32 * 1024 * 1024, 'file validation budget exceeded')
        self.bytes += before[2]
        require(self.bytes <= self.maximum, 'closure validation byte budget exceeded')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            body = json.load(stream, object_pairs_hook=_pairs,
                             parse_constant=lambda _: require(False, 'non-finite JSON'))
        require(_identity(path) == before, 'file changed while validating')
        self.fingerprints[name] = before
        require(isinstance(body, dict), 'JSON object required')
        return body

    def content(self, name):
        body = self.read(_name(self.kind, name))
        require(type(body.get('schema')) is int and body['schema'] == 1, 'unknown content schema')
        if self.kind != 'rio':
            require(digest(body) + '.json' == name, 'content digest mismatch')
        else:
            coordinate = body.get('coordinate')
            require(isinstance(coordinate, list) and len(coordinate) == 3
                    and coordinate[0] in ('mplus', 'peak')
                    and all(isinstance(x, str) and x for x in coordinate)
                    and body.get('season') == coordinate[1] and body.get('region') == coordinate[2]
                    and isinstance(body.get('items'), list), 'invalid RIO content')
        # A valid digest/schema alone does not identify an owned projection.
        # Unknown shapes may carry references we cannot traverse: fail closed.
        if self.kind == 'news':
            if set(body) == {'schema', 'items'}:
                require(isinstance(body['items'], list)
                        and all(isinstance(row, dict) for row in body['items']), 'invalid news items')
            else:
                require(set(body) == {'schema', 'sections'}
                        and isinstance(body.get('sections'), dict)
                        and set(body['sections']) == set(NEWS_SECTIONS)
                        and all(isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
                                for rows in body['sections'].values()), 'unknown news content shape')
        elif self.kind == 'rio':
            fields = {'schema': int, 'coordinate': list, 'season': str, 'region': str,
                      'items': list, 'generated_at': str}
            fields.update({'dungeons': list, 'runs_by_dungeon': dict} if body['coordinate'][0] == 'mplus'
                          else {'role': str})
            require(set(body) == set(fields)
                    and all(isinstance(body[key], value_type) for key, value_type in fields.items()),
                    'unknown RIO content shape')
        else:
            if 'coordinate' not in body:
                fields = {'schema': int, 'release': dict, 'instances': list,
                          'tiers': list, 'season_label': str}
            elif 'instance' in body:
                fields = {'schema': int, 'coordinate': dict, 'instance': dict,
                          'bosses': list, 'references': dict, 'loot_versions': dict}
            else:
                fields = {'schema': int, 'coordinate': dict, 'boss': dict,
                          'sections': list, 'roles': list, 'tooltips': dict}
            require(set(body) == set(fields)
                    and all(isinstance(body[key], value_type) for key, value_type in fields.items()),
                    'unknown journal content shape')
        return body

    def metadata(self, name):
        """Cache only validated traversal facts, never whole projection bodies."""
        _name(self.kind, name)
        if name in self._metadata:
            require(_identity(self.root / name) == self.fingerprints[name],
                    'file changed while validating')
            return self._metadata[name]
        body = self.content(name)
        if self.kind == 'news':
            if 'sections' in body:
                facts = {'sections': {key: [] for key in body['sections']}}
            else:
                facts = {'items': [{'source': row.get('source')} for row in body['items']]}
        elif self.kind == 'rio':
            logical = {k: v for k, v in body.items() if k != 'generated_at'}
            facts = {key: body[key] for key in ('coordinate', 'generated_at')}
            facts['content_hash'] = hashlib.sha256(json.dumps(
                logical, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
        elif 'coordinate' not in body:
            facts = {'release': body['release'],
                     'instances': [{'id': row['id']} for row in body['instances']]}
        elif 'instance' in body:
            facts = {'coordinate': body['coordinate'],
                     'bosses': [{key: row[key] for key in ('id', 'difficulty_ids', 'skills')}
                                for row in body['bosses']]}
        else:
            facts = {'coordinate': body['coordinate'], 'sections': [],
                     'roles': [], 'tooltips': {}, 'boss': {}}
        self._metadata[name] = facts
        return facts

    def closure(self, index):
        require(isinstance(index, dict) and type(index.get('schema')) is int
                and index['schema'] == 1, 'unknown index schema')
        cache_key = digest(index)
        if cache_key in self._closures:
            refs = self._closures[cache_key]
            _unchanged(self.root, {name: self.fingerprints[name] for name in refs})
            return set(refs)
        refs = set()
        def load(name):
            refs.add(_name(self.kind, name))
            return self.metadata(name)
        if self.kind == 'news':
            keys = ('schema', 'shards', 'sources', 'home')
            require(set(index) == {*keys, 'generation', 'generated_at'}, 'unknown news index fields')
            require(digest({k: index[k] for k in keys}) == index['generation'], 'news generation mismatch')
            require(isinstance(index['shards'], list) and isinstance(index['sources'], dict), 'invalid news index')
            sections = load(index['home']).get('sections')
            require(isinstance(sections, dict) and set(sections) == set(NEWS_SECTIONS)
                    and all(isinstance(v, list) for v in sections.values()), 'invalid news home')
            totals = Counter()
            for shard in index['shards']:
                require(isinstance(shard, dict) and set(shard) == {'file', 'count', 'sources', 'month'}, 'invalid shard')
                items = load(shard['file']).get('items')
                require(isinstance(items, list) and all(isinstance(r, dict) and isinstance(r.get('source'), str)
                        for r in items), 'invalid news items')
                counts = Counter(r['source'] for r in items)
                require(type(shard['count']) is int and len(items) == shard['count']
                        and dict(counts) == shard['sources'], 'news shard count mismatch')
                totals.update(counts)
            require(dict(totals) == index['sources'], 'news total mismatch')
        elif self.kind == 'rio':
            require(set(index) == {'schema', 'current_season', 'entries'}
                    and isinstance(index['current_season'], str) and isinstance(index['entries'], dict), 'invalid RIO index')
            for key, entry in index['entries'].items():
                require(isinstance(entry, dict) and set(entry) <= {'file', 'content_hash', 'generated_at', 'checked_at', 'error'}
                        and {'file', 'content_hash', 'generated_at'} <= set(entry), 'invalid RIO entry')
                body = load(entry['file'])
                expected = hashlib.sha256(json.dumps(body['coordinate']).encode()).hexdigest()
                require(key == expected and body['content_hash'] == entry['content_hash']
                        and body.get('generated_at') == entry['generated_at'], 'RIO scope/hash mismatch')
        else:
            keys = ('schema', 'source_version', 'release', 'art_ids', 'difficulties', 'instances', 'catalog')
            require(set(index) == {*keys, 'generation', 'generated_at'}, 'unknown journal index fields')
            require(digest({k: index[k] for k in keys}) == index['generation'], 'journal generation mismatch')
            require(isinstance(index['instances'], dict) and isinstance(index['release'], dict), 'invalid journal index')
            catalog = load(index['catalog'])
            require(catalog.get('release') == index['release'] and isinstance(catalog.get('instances'), list), 'journal catalog mismatch')
            require({str(row['id']) for row in catalog['instances']} == set(index['instances']), 'journal catalog scope mismatch')
            for instance_id, filename in index['instances'].items():
                require(re.fullmatch(r'[1-9][0-9]*', instance_id), 'invalid journal instance id')
                instance = load(filename)
                coordinate = instance['coordinate']
                require(set(coordinate) == {'release_id', 'instance_id', 'source'}
                        and coordinate['release_id'] == index['release']['id']
                        and str(coordinate['instance_id']) == instance_id
                        and isinstance(instance['bosses'], list), 'journal instance mismatch')
                for boss in instance['bosses']:
                    require(isinstance(boss['skills'], dict) and isinstance(boss['difficulty_ids'], list), 'invalid journal skills')
                    for difficulty, skill_file in boss['skills'].items():
                        require(re.fullmatch(r'[1-9][0-9]*', difficulty) and int(difficulty) in boss['difficulty_ids'], 'invalid skill difficulty')
                        skills = load(skill_file)
                        require(skills.get('coordinate') == {**coordinate, 'boss_id': boss['id'], 'difficulty': int(difficulty)}
                                and isinstance(skills.get('sections'), list) and isinstance(skills.get('roles'), list)
                                and isinstance(skills.get('tooltips'), dict) and isinstance(skills.get('boss'), dict), 'journal skills mismatch')
        self._closures[cache_key] = frozenset(refs)
        return refs


def _history(reader, current):
    history = reader.read(HISTORY)
    require(set(history) == {'schema', 'kind', 'current', 'previous', 'pending', 'retired'}
            and history['schema'] == 1 and history['kind'] == reader.kind
            and isinstance(history['previous'], list) and len(history['previous']) <= 2
            and isinstance(history['retired'], dict), 'invalid GC history')
    if history['pending'] is not None and current == history['pending']:
        same = history['current'] is not None and reader.closure(history['current']) == reader.closure(current)
        previous = history['previous'] if same else (([history['current']] if history['current'] is not None else []) + history['previous'])[:2]
        history = {**history, 'current': current, 'previous': previous, 'pending': None}
    require(history['current'] == current, 'index not tracked by publication history')
    refs = reader.closure(current)
    for name, retired_at in history['retired'].items():
        _name(reader.kind, name)
        require(type(retired_at) in (int, float) and retired_at >= 0, 'invalid retirement timestamp')
        if retired_at >= time.time() - GRACE_SECONDS:
            reader.metadata(name)
            refs.add(name)
    for index in history['previous'] + ([history['pending']] if history['pending'] is not None else []):
        refs.update(reader.closure(index))
    return history, refs


def publish_snapshot_index(kind, root, index, *, write_index=None):
    """Durably retain two actual rollback publications; caller MUST hold publish.lock.

    Pending is written before index so interruption is recoverable without guessing
    generations from mtime. A failed publication cannot evict actual rollback state.
    """
    require(kind in ROOTS, 'unknown cache kind')
    root = _safe_root(root)
    reader = Reader(root, kind)
    reader.closure(index)
    write_index = write_index or _write
    try:
        require(not (root / BLOCKED).exists(), 'GC history requires operator recovery')
        old = reader.read('index.json') if (root / 'index.json').exists() else None
        if old is not None:
            reader.closure(old)
        if (root / HISTORY).exists():
            history, _ = _history(reader, old)
        else:
            history = {'schema': 1, 'kind': kind, 'current': old, 'previous': [], 'pending': None, 'retired': {}}
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # Bad old data must not permanently prevent last-good repair. Never infer
        # a replacement rollback history: durably disable GC before repairing.
        _write(root / BLOCKED, {'schema': 1, 'reason': str(exc)})
        write_index(root / 'index.json', index)
        return
    # Metadata-only check/failure updates do not consume a rollback generation.
    same_refs = old is not None and reader.closure(old) == reader.closure(index)
    previous = history['previous'] if same_refs else (([old] if old is not None else []) + history['previous'])[:2]
    now = time.time()
    retired = {name: at for name, at in history['retired'].items() if at >= now - GRACE_SECONDS}
    for prior in ([old] if old is not None else []) + history['previous']:
        for name in reader.closure(prior):
            retired[name] = now
    history = {**history, 'retired': retired}
    _unchanged(root, reader.fingerprints)
    _write(root / HISTORY, {**history, 'pending': index})
    write_index(root / 'index.json', index)
    _write(root / HISTORY, {**history, 'current': index, 'previous': previous, 'pending': None})


def _unchanged(root, fingerprints):
    for name, expected in fingerprints.items():
        require(_identity(root / name) == expected, f'file changed before delete: {name}')


def maintain_snapshot_cache(kind, *, apply=False, limit=None, root=None):
    """One bounded maintenance pass, nonblocking shared publisher lock.

    First apply on a legacy root records current only, and deletes nothing.
    Limits bound directory enumeration, validation bytes and per-pass deletion.
    Any unknown/broken index or closure aborts the whole batch before unlink.
    """
    require(kind in ROOTS, 'unknown cache kind')
    limit = int(limit if limit is not None else getattr(settings, 'SNAPSHOT_CACHE_GC_BATCH_LIMIT', 32))
    require(1 <= limit <= 1000, 'batch limit must be 1..1000')
    setting, default = ROOTS[kind]
    root = Path(root if root is not None else getattr(settings, setting, Path(settings.BASE_DIR) / default))
    result = {'kind': kind, 'apply': bool(apply), 'status': 'ok', 'deleted': 0,
              'deleted_bytes': 0, 'candidates': [], 'protected': 0, 'ignored': 0}
    try:
        root = _safe_root(root)
        with _lock(root / 'publish.lock', blocking=False) as acquired:
            if not acquired:
                return {**result, 'status': 'busy'}
            require(not (root / BLOCKED).exists() and not (root / BLOCKED).is_symlink(),
                    'GC disabled after invalid publication history; operator recovery required')
            reader = Reader(root, kind)
            index = reader.read('index.json')
            refs = reader.closure(index)
            if not (root / HISTORY).exists():
                _unchanged(root, reader.fingerprints)
                if apply:
                    _write(root / HISTORY, {'schema': 1, 'kind': kind, 'current': index, 'previous': [], 'pending': None, 'retired': {}})
                return {**result, 'status': 'initialized' if apply else 'bootstrap_required', 'protected': len(refs)}
            _, refs = _history(reader, index)
            result['protected'] = len(refs)
            cutoff = time.time() - GRACE_SECONDS
            scan_limit = int(getattr(settings, 'SNAPSHOT_CACHE_GC_SCAN_LIMIT', 20000))
            candidates = []
            with os.scandir(root) as entries:
                for count, entry in enumerate(entries, 1):
                    require(count <= scan_limit, 'directory scan budget exceeded')
                    require(not entry.is_symlink(), f'symlink in cache: {entry.name}')
                    try:
                        _name(kind, entry.name)
                    except UnsafeCache:
                        result['ignored'] += 1
                        continue
                    identity = _identity(root / entry.name)
                    if entry.name in refs or identity[3] / 1e9 > cutoff:
                        continue
                    candidates.append((entry.name, identity))
            # Complete discovery precedes validation; only a bounded batch is read.
            candidates.sort(key=lambda row: (row[1][3], row[0]))
            result['eligible'] = len(candidates)
            for name, identity in candidates[:limit]:
                reader.content(name)
                require(reader.fingerprints[name] == identity, 'candidate changed during scan')
                result['candidates'].append({'file': name, 'bytes': identity[2], 'mtime_ns': identity[3]})
            _unchanged(root, reader.fingerprints)
            if apply:
                for candidate in result['candidates']:
                    # Recheck pointers for each unlink as well as the complete batch.
                    _unchanged(root, {k: v for k, v in reader.fingerprints.items()
                                      if k in ('index.json', HISTORY, candidate['file'])})
                    (root / candidate['file']).unlink()
                    result['deleted'] += 1
                    result['deleted_bytes'] += candidate['bytes']
            return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        return {**result, 'status': 'blocked', 'error': str(exc)}


def maintain_snapshot_caches(*, apply=False, limit=None):
    """Public idle-worker hook; independent of rebuild eligibility; no DB access."""
    return [maintain_snapshot_cache(kind, apply=apply, limit=limit) for kind in ROOTS]
