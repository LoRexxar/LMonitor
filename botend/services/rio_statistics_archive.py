"""Read-only previous-build display for an identical coordinate in one season."""
from contextlib import nullcontext
from hashlib import sha256
import re

from botend.services.rio_statistics_scope import public_scope
from botend.services.rio_statistics_storage import encoded, key_for, StorageError
from botend.services.simc_benchmark_result_snapshot import _lock

_TOKEN = re.compile(r'[0-9a-f]{64}')
_BUILD = re.compile(r'\d+\.\d+\.\d+\.\d+')


def compatible(source, current):
    return (isinstance(source, dict) and type(current.get('id')) is int
            and source.get('id') == current['id'] and source.get('rio_season') == current['rio_season']
            and source.get('token') != current['token'] and _TOKEN.fullmatch(str(source.get('token', '')))
            and _BUILD.fullmatch(str(source.get('game_build', '')))
            and source['game_build'].split('.')[0] == str(current.get('game_build', '')).split('.')[0])


def _registry(scope):
    from botend.services.rio_statistics_views import directory
    return directory() / 'archives' / f"season-{int(scope['id'])}.json"


def _sources(scope):
    from botend.services.rio_statistics_views import _file
    if type(scope.get('id')) is not int:
        return []
    registry, _ = _file(_registry(scope))
    return [source for source in (registry or {}).get('sources', [])[:2]
            if compatible(source, scope) and _TOKEN.fullmatch(str(source.get('revision', '')))]


def archive_revision(scope):
    return ':'.join(source['revision'] for source in _sources(scope))


def _register(source, current, rows):
    """Caller holds refresh.lock. Write manifest shards before the registry."""
    from botend.services.rio_statistics_views import _file, _write_view, SCHEMA
    if not compatible(source, current) or not rows:
        return 0
    path = _registry(current)
    registry, _ = _file(path)
    previous = (registry or {}).get('sources', [])
    if any(ref.get('token') == source['token'] for ref in previous):
        return 0
    shards = {}
    for key, proof in rows.items():
        shards.setdefault(key[:2], {})[key] = proof
    revision = sha256(encoded([source['token'], rows])).hexdigest()
    for prefix, entries in shards.items():
        _write_view(path.parent / revision / f'{prefix}.json', {
            'schema': SCHEMA, 'scope': source['token'], 'revision': revision, 'entries': entries})
    _write_view(path, {'schema': SCHEMA, 'sources': [dict(public_scope(source), revision=revision), *previous[:1]]})
    return len(rows)


def capture_previous(published, current):
    """Capture references from the last committed directory before reusing slots."""
    from botend.services import rio_statistics as rio
    from botend.services.rio_statistics_views import SCHEMA
    source = published.get('meta', {}).get('scope')
    if published.get('layout') != SCHEMA or not compatible(source, current):
        return 0
    entries, files = published.get('entries', {}), published.get('files', {})
    rows = {}
    for record in published.get('available', []):
        c = record['coordinate']
        if c.get('scope') == source['token'] and rio.supported_coordinate(c):
            key = rio.key_for(c)
            if files.get(key) in ('a', 'b') and _TOKEN.fullmatch(str(entries.get(key, ''))):
                rows[key] = {'slot': files[key], 'marker': entries[key]}
    return _register(source, current, rows)


def recover_existing(source, *, apply=False):
    """Register old scope files whose coordinate and timestamp match existing stored facts.

    No acquisition or writes to the source cache. Exact archive lookup requires
    both this source receipt and a checksummed old display record.
    """
    from botend.services import rio_statistics as rio
    from botend.services.rio_statistics_views import _file, directory
    parent = directory()
    with (_lock(parent / 'refresh.lock') if apply else nullcontext()):
        current = rio.active_scope()
        if not compatible(source, current):
            raise ValueError('历史构建必须属于同一赛季、同一资料片且不是当前范围')
        rows = {}
        counts = {}
        samples = set()
        for key, row in rio.store().projection_entries(source['token'], readonly=True).items():
            c = row['coordinate']
            if (c.get('scope') != source['token'] or c.get('season') != source['rio_season']
                    or not rio.supported_coordinate(c) or not row['has_payload'] or not rio._stamp(row['fetched_at'])):
                continue
            rows[key] = {'fetched_at': row['fetched_at']}
            counts[c['module']] = counts.get(c['module'], 0) + 1
            if c['module'] not in samples and _record(c, source, rows[key], _file, parent)[0]:
                samples.add(c['module'])
        if not rows or not samples:
            raise StorageError('没有可验证的历史展示文件')
        if rio.active_scope()['token'] != current['token']:
            raise ValueError('当前版本已变化，未登记历史资料')
        return {'candidate_source_rows': counts, 'sampled_modules': sorted(samples),
                'registered': _register(source, current, rows) if apply else 0}


def _record(c, source, proof, read_file, folder):
    key = key_for(c)
    slots = (proof['slot'],) if proof.get('slot') in ('a', 'b') else ('a', 'b')
    for slot in slots:
        record, stamp = read_file(folder / 'entries' / f'{key}-{slot}.json')
        if not record or record.get('coordinate') != c:
            continue
        envelope = record.get('response') or {}
        origin = envelope.get('scope') or {}
        snapshot = envelope.get('snapshot') or {}
        if (envelope.get('coordinate') != c or not isinstance(envelope.get('data'), dict)
                or not all(origin.get(k) == source.get(k) for k in ('token', 'id', 'rio_season', 'game_build'))
                or not snapshot.get('fetched_at') or snapshot['fetched_at'] != envelope.get('fetched_at')):
            continue
        if ('marker' in proof and record.get('marker') == proof['marker']
                or 'fetched_at' in proof and snapshot['fetched_at'] == proof['fetched_at']):
            return envelope, stamp
    return None, None


def read_archive(coordinate, current):
    from botend.services.rio_statistics_views import _file, _response, directory
    for source in _sources(current):
        path = _registry(current)
        archived = dict(coordinate, scope=source['token'])
        key = key_for(archived)
        shard, _ = _file(path.parent / source['revision'] / f'{key[:2]}.json')
        if not shard or shard.get('scope') != source['token'] or shard.get('revision') != source['revision']:
            continue
        proof = shard.get('entries', {}).get(key)
        if not proof:
            continue
        envelope, stamp = _record(archived, source, proof, _file, directory())
        if envelope:
            message = f"当前版本暂未采集到此范围，以下为同赛季历史快照（版本 {source['game_build']}）。"
            return _response(('archive', stamp, current['token']), lambda: {
                **envelope, 'scope': public_scope(current), 'source_scope': public_scope(source),
                'snapshot': {**envelope['snapshot'], 'state': 'stale', 'message': message}})
    return None
