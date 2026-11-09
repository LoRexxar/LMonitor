"""后台展示投影与有界只读缓存；访客不枚举统计库、不解压正文、不查中文资料。"""
from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
from threading import RLock, Event

from django.db import DatabaseError
from django.http import HttpResponse, HttpResponseNotModified
from django.utils import timezone

from botend.services.rio_statistics_scope import ScopeUnavailable, public_scope
from botend.services.rio_statistics_storage import encoded, StorageError
from botend.services.rio_statistics_dimensions import project_periods
from botend.services.simc_benchmark_result_snapshot import _lock

SCHEMA = 3
MAX_FILE_BYTES = 40 * 1024 ** 2
MAX_CACHE_BYTES = 64 * 1024 ** 2
_cache = OrderedDict()
_cache_bytes = 0
_mutex = RLock()
_pending = {}


def _object_bytes(value):
    """按对象实际占用估算缓存权重，共享引用只计一次。"""
    seen = set()
    def size(item):
        identity = id(item)
        if identity in seen:
            return 0
        seen.add(identity)
        count = sys.getsizeof(item)
        if isinstance(item, dict):
            count += sum(size(k) + size(v) for k, v in item.items())
        elif isinstance(item, (list, tuple, set)):
            count += sum(size(v) for v in item)
        return count
    return size(value)


def clear_cache():
    global _cache_bytes
    with _mutex:
        _cache.clear()
        _cache_bytes = 0


def _remember(key, build, weight):
    global _cache_bytes
    while True:
        with _mutex:
            if key in _cache:
                value, _ = _cache[key]
                _cache.move_to_end(key)
                return value
            waiter = _pending.get(key)
            if waiter is None:
                waiter = _pending[key] = Event()
                break
        waiter.wait()
    try:
        value = build()
        size = weight(value) + 1024
        with _mutex:
            if size <= MAX_CACHE_BYTES:
                while _cache and (_cache_bytes + size > MAX_CACHE_BYTES or len(_cache) >= 256):
                    _, (_, old_size) = _cache.popitem(last=False)
                    _cache_bytes -= old_size
                _cache[key] = (value, size)
                _cache_bytes += size
        return value
    finally:
        with _mutex:
            _pending.pop(key, None)
            waiter.set()


def _file(path):
    try:
        stat = path.stat()
        if stat.st_size > MAX_FILE_BYTES:
            return None, None
        stamp = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, stat.st_ino)
        def load():
            data = json.loads(path.read_bytes())
            if not isinstance(data, dict) or data.get('schema') not in (1, 2, SCHEMA):
                return None
            checksum = data.pop('checksum', None)
            return data if checksum == hashlib.sha256(encoded(data)).hexdigest() else None
        # 大清单只供后台使用，避免遍历巨大对象来估算一个注定无法保留的缓存项。
        return _remember(('file', stamp), load, lambda value: MAX_CACHE_BYTES + 1
                         if stat.st_size > MAX_CACHE_BYTES // 8 else _object_bytes(value)), stamp
    except (OSError, ValueError):
        return None, None


@dataclass(frozen=True)
class Body:
    raw: bytes
    packed: bytes
    etag: str


def _body(data):
    raw = encoded({'success': True, 'data': data})
    return Body(raw, gzip.compress(raw, compresslevel=5, mtime=0), hashlib.sha256(raw).hexdigest())


def _response(key, make):
    return _remember(('response', key), lambda: _body(make()), lambda b: _object_bytes((b.raw, b.packed, b.etag)))


def directory():
    from botend.services.rio_statistics import root
    return root() / 'views'


def _write_view(path, data):
    from botend.services import rio_statistics as rio
    payload = {**data, 'checksum': hashlib.sha256(encoded(data)).hexdigest()}
    rio.store()._capacity(len(encoded(payload)) + 65536)
    rio._write(path, payload)


def _publication(*, status_only=False, reader=False):
    """只认一次原子提交；候选目录和候选正文不会因落盘而自动可见。"""
    folder = directory()
    pointer_path = folder / 'current.json'
    for _ in range(2):
        pointer, pointer_stamp = _file(pointer_path)
        if not pointer:
            # 首次升级前兼容既有展示；已有指针损坏时不能误回退到另一历史发布。
            if pointer_path.exists():
                return None, None
            return _file(folder / ('status.json' if status_only else 'catalog.json'))
        if pointer.get('schema') not in (2, SCHEMA) or pointer.get('slot') not in ('a', 'b'):
            return None, None
        if status_only:
            return pointer, pointer_stamp
        use_reader = reader and pointer.get('layout', pointer['schema']) == SCHEMA
        prefix = 'reader' if use_reader else 'catalog'
        catalog, stamp = _file(folder / f"{prefix}-{pointer['slot']}.json")
        expected_schema = SCHEMA if use_reader else pointer['schema']
        if (catalog and catalog.get('schema') == expected_schema and catalog.get('revision') == pointer.get('revision')
                and catalog.get('scope') == pointer.get('scope')):
            return catalog, stamp
        # 极慢读者可能跨过两次提交而遇到槽复用，重新取得当前指针再读一次。
    return None, None


def _entry_path(catalog, key):
    if not re.fullmatch(r'[0-9a-f]{64}', key):
        return None
    if catalog.get('schema') == 1:
        return directory() / 'entries' / (key + '.json')
    slot = catalog.get('files', {}).get(key)
    return directory() / 'entries' / f'{key}-{slot}.json' if slot in ('a', 'b') else None


def _entry_location(catalog, key):
    """公开请求只读取对应的哈希分片，不读取数万行工作清单。"""
    if 'entries' in catalog:
        return _entry_path(catalog, key), catalog.get('entries', {}).get(key)
    prefix = key[:2]
    reference = catalog.get('indexes', {}).get(prefix) or {}
    if reference.get('slot') not in ('a', 'b'):
        return None, None
    index, _ = _file(directory() / 'indexes' / f"{prefix}-{reference['slot']}.json")
    if not index or index.get('signature') != reference.get('signature') or index.get('scope') != catalog.get('scope'):
        return None, None
    row = index.get('entries', {}).get(key) or {}
    slot = row.get('slot')
    path = directory() / 'entries' / f'{key}-{slot}.json' if slot in ('a', 'b') else None
    return path, row.get('marker')


def _context(meta):
    # 元数据每日更新时间不改变正文呈现，不能据此重建全部历史热力图。
    return hashlib.sha256(encoded({k: meta.get(k) for k in ('scope', 'seasons', 'maps')})).hexdigest()


def _compact_available(available, meta):
    """只提供选择初始副本所需的代表范围；组合总量单独统计。"""
    selected = {}
    counts = {}
    for row in available:
        c = row['coordinate']
        module = c['module']
        counts[module] = counts.get(module, 0) + 1
        group = tuple(c.get(k) for k in ('module', 'season', 'region', 'level', 'period'))
        if module == 'heatmaps':
            if c.get('filters'):
                continue
            if c.get('period') == 'last-reset':
                target = 'us' if c['region'] == 'world' else c['region']
                current = next((p for p in meta.get('periods', []) if p.get('region') == target), {})
                if c.get('period_id') != (current.get('previous') or {}).get('period'):
                    continue
        if group not in selected:
            selected[group] = row
    return list(selected.values()), counts


def _publish_indexes(entries, files, previous, scope, *, verify=False):
    groups = {}
    for key, marker in entries.items():
        groups.setdefault(key[:2], {})[key] = {'marker': marker, 'slot': files[key]}
    references = {}
    for prefix, rows in groups.items():
        signature = hashlib.sha256(encoded(rows)).hexdigest()
        old = previous.get('indexes', {}).get(prefix) or {}
        path = directory() / 'indexes' / f"{prefix}-{old.get('slot')}.json"
        old_index, _ = _file(path) if verify and path.is_file() else (None, None)
        if (old.get('signature') == signature and old.get('slot') in ('a', 'b') and path.is_file()
                and (not verify or old_index and old_index.get('signature') == signature)):
            references[prefix] = old
            continue
        slot = 'b' if old.get('slot') == 'a' else 'a'
        path = directory() / 'indexes' / f'{prefix}-{slot}.json'
        _write_view(path, {'schema': SCHEMA, 'scope': scope, 'signature': signature, 'entries': rows})
        checked, _ = _file(path)
        if not checked or checked.get('signature') != signature:
            raise StorageError('候选索引分片校验失败，未切换发布')
        references[prefix] = {'slot': slot, 'signature': signature}
    return references


def entry_path(coordinate):
    """供后台维护和验收定位已提交的正文，公开读仍执行范围门禁。"""
    from botend.services.rio_statistics import key_for
    catalog, _ = _publication()
    return _entry_path(catalog, key_for(coordinate)) if catalog else None


def refresh_views(*, keys=None, verify=None):
    """由后台调用；写入非活动槽后以单一指针提交，失败保留上一完整发布。"""
    from botend.services import rio_statistics as rio
    folder = directory()
    with _lock(folder / 'refresh.lock'):
        # 等待其他发布者后重新读取，避免迟到的旧元数据覆盖较新的目录。
        scope = rio.active_scope()
        source = rio.catalog()
        if (source.get('scope') or {}).get('token') != scope['token']:
            raise ScopeUnavailable('当前范围元数据尚未就绪，未覆盖展示文件')
        meta = rio.localize_catalog(json.loads(encoded(source)), scope)
        display_meta = project_periods(meta)
        previous, _ = _publication()
        previous = previous or {}
        same_scope = previous.get('scope') == scope['token']
        allowed_keys = {rio.key_for(row['coordinate']) for row in previous.get('available', [])
                        if rio.supported_coordinate(row['coordinate'])}
        old_entries = {k: v for k, v in previous.get('entries', {}).items() if k in allowed_keys} if same_scope else {}
        old_files = {k: v for k, v in previous.get('files', {}).items() if k in old_entries} if same_scope else {}
        context = _context(meta)
        same_context = same_scope and _context(previous.get('meta', {})) == context
        previous_layout = previous.get('layout', previous.get('schema'))
        limited = keys is not None and previous_layout == SCHEMA and same_context
        verify = keys is None if verify is None else verify
        selected = set(keys or [])
        if limited and previous.get('meta', {}).get('periods') != meta.get('periods'):
            selected.update(rio.key_for(v['coordinate']) for v in previous.get('available', [])
                            if v['coordinate']['module'] == 'runs')
        rows = rio.store().projection_entries(scope['token'], keys=selected if limited else None)
        rows = {k: v for k, v in rows.items() if rio.supported_coordinate(v['coordinate'])}
        if not rows and old_entries and not limited:
            raise StorageError('当前范围统计意外为空，保留已有展示发布')
        entries = dict(old_entries) if limited else {}
        files = dict(old_files) if limited else {}
        available = [v for v in previous.get('available', []) if rio.key_for(v['coordinate']) not in rows and rio.supported_coordinate(v['coordinate'])] if limited else []
        retained_keys = (set(previous.get('retained_keys', [])) & old_entries.keys()) - rows.keys() if limited else set()
        legacy_context = previous.get('legacy_context') if same_scope else None
        if same_scope and previous_layout in (1, 2):
            legacy_context = hashlib.sha256(encoded(previous['meta'])).hexdigest()
        written = 0
        for key, entry in rows.items():
            c = entry['coordinate']
            marker = hashlib.sha256(encoded([context, entry, meta.get('periods') if c['module'] == 'runs' else None])).hexdigest()
            entries[key] = marker
            old_path = _entry_path(previous, key) if same_scope else None
            legacy_marker = hashlib.sha256(encoded([legacy_context, entry])).hexdigest() if legacy_context else None
            unchanged = (same_context and old_files.get(key) in ('a', 'b') and
                         (old_entries.get(key) == marker or (legacy_marker and old_entries.get(key) == legacy_marker
                          and (c['module'] != 'runs' or previous['meta'].get('periods') == meta.get('periods')))))
            if not verify and unchanged and old_path.is_file():
                entries[key] = old_entries[key]
                files[key] = old_files[key]
                kept = key in previous.get('retained_keys', [])
                if kept:
                    retained_keys.add(key)
                if entry['has_payload'] or kept:
                    available.append({'coordinate': c, 'state': 'stale' if kept or entry.get('error') else 'ready'})
                continue
            existing, _ = _file(old_path) if old_path else (None, None)
            valid_old = bool(existing and existing.get('coordinate') == c
                             and existing.get('marker') == old_entries.get(key))
            kept = False
            if valid_old and unchanged:
                entries[key] = old_entries[key]
                files[key] = old_files[key]
                kept = bool(existing.get('retained'))
            else:
                slot = 'b' if old_files.get(key) == 'a' else 'a'
                files[key] = slot
                path = folder / 'entries' / f'{key}-{slot}.json'
                envelope = rio.read_entry(c, meta=display_meta)
                if (not isinstance(envelope.get('data'), dict) and valid_old
                        and isinstance(existing.get('response', {}).get('data'), dict)):
                    # 缓存损坏或响应不可用不能覆盖已经验证的同范围正文。
                    old_response = existing['response']
                    envelope = {**old_response, 'snapshot': {**old_response['snapshot'], 'state': 'stale',
                        'last_attempt_at': entry.get('attempted_at'), 'message': '本次更新未完成，保留上次数据。'}}
                    kept = True
                envelope['scope'] = public_scope(scope)
                # 保持旧 Web 进程能读取已提交正文，轻量目录通过附加布局版本切换。
                payload = {'schema': 2, 'coordinate': c, 'response': envelope, 'marker': marker, 'retained': kept}
                _write_view(path, payload)
                checked, _ = _file(path)
                if not checked or checked.get('marker') != marker or checked.get('coordinate') != c:
                    raise StorageError('候选展示正文校验失败，未切换发布')
                written += 1
            if kept:
                retained_keys.add(key)
            if entry['has_payload'] or (valid_old and isinstance(existing.get('response', {}).get('data'), dict)):
                available.append({'coordinate': c, 'state': 'stale' if kept or entry.get('error') or not entry['has_payload'] else 'ready'})
        if rio.active_scope()['token'] != scope['token']:
            raise ScopeUnavailable('展示发布期间数据库版本发生变化，未切换目录')
        source_revision = hashlib.sha256(encoded([meta, entries, list(rio.LEVELS.items())])).hexdigest()
        bad_index = False
        if verify:
            for prefix, reference in previous.get('indexes', {}).items():
                index, _ = _file(folder / 'indexes' / f"{prefix}-{reference['slot']}.json")
                if not index or index.get('signature') != reference.get('signature'):
                    bad_index = True
                    break
        changed = previous_layout != SCHEMA or previous.get('source_revision') != source_revision or written or bad_index
        revision = hashlib.sha256(encoded([source_revision, rio._now()])).hexdigest() if changed else previous['revision']
        if changed:
            pointer, _ = _file(folder / 'current.json')
            slot = 'b' if pointer and pointer.get('slot') == 'a' else 'a'
            indexes = _publish_indexes(entries, files, previous if same_scope else {}, scope['token'], verify=verify)
            payload = {'schema': 2, 'layout': SCHEMA, 'revision': revision, 'generated_at': rio._now(),
                       'source_revision': source_revision, 'scope': scope['token'], 'meta': meta,
                       'level_policy': list(rio.LEVELS),
                       'available': available, 'entries': entries, 'files': files, 'indexes': indexes,
                       'retained_keys': sorted(retained_keys), 'legacy_context': legacy_context}
            path = folder / f'catalog-{slot}.json'
            _write_view(path, payload)
            checked, _ = _file(path)
            if not checked or checked.get('revision') != revision:
                raise StorageError('候选展示目录校验失败，未切换发布')
            compact, counts = _compact_available(available, display_meta)
            reader = {'schema': SCHEMA, 'revision': revision, 'scope': scope['token'], 'meta': meta,
                      'available': compact, 'indexes': indexes, 'level_policy': list(rio.LEVELS),
                      'availability': {'total': len(available), 'modules': counts, 'complete_list': False}}
            _write_view(folder / f'reader-{slot}.json', reader)
            checked, _ = _file(folder / f'reader-{slot}.json')
            if not checked or checked.get('revision') != revision:
                raise StorageError('候选轻量目录校验失败，未切换发布')
            if rio.active_scope()['token'] != scope['token']:
                raise ScopeUnavailable('展示提交前数据库版本发生变化，未切换发布')
            # 这是最后一个写入步骤。此前所有文件均在非活动槽，任一异常不影响现有发布。
            _write_view(folder / 'current.json', {'schema': 2, 'layout': SCHEMA, 'scope': scope['token'], 'slot': slot,
                'revision': revision, 'updated_at': meta.get('updated_at'), 'period_boundaries': _boundaries(meta)})
    return {'state': 'partial' if retained_keys else 'complete', 'entries': len(entries),
            'written': written, 'retained': len(retained_keys), 'revision': revision, 'incremental': limited}


def _empty(scope, message):
    return {'schema': SCHEMA, 'scope': public_scope(scope) if scope else None,
            'scope_state': 'updating' if scope else 'unconfigured', 'scope_message': message,
            'current_season': scope['rio_season'] if scope else '', 'seasons': [], 'raids': [], 'periods': []}


def _period_key(meta, now):
    from botend.services.rio_statistics import _stamp
    return tuple(bool(stamp and stamp <= now) for row in meta.get('periods', [])
                 for part in ('previous', 'current', 'next') for field in ('start', 'end')
                 for stamp in [_stamp((row.get(part) or {}).get(field))])


def _boundaries(meta):
    from botend.services.rio_statistics import _stamp
    return sorted({stamp.timestamp() for row in meta.get('periods', [])
                   for part in ('previous', 'current', 'next') for field in ('start', 'end')
                   for stamp in [_stamp((row.get(part) or {}).get(field))] if stamp})


def read_public(module, params, *, status_only=False, _retry=False):
    from botend.services import rio_statistics as rio
    if module in ('heatmaps', 'runs'):
        rio._add_level({}, params)
    try:
        scope = rio.active_scope()
    except (DatabaseError, ScopeUnavailable) as exc:
        message = str(exc) if isinstance(exc, ScopeUnavailable) else '当前赛季资料暂不可用，请稍后查看。'
        meta = _empty(None, message)
        return _gate_response(module, meta, status_only=status_only)
    if module != 'catalog' and (params.get('region', 'cn') not in rio.REGIONS
                               or params.get('season') not in (None, '', scope['rio_season'])):
        raise ValueError('赛季或地区不存在')
    published, stamp = _publication(status_only=status_only, reader=True)
    if not published or published.get('scope') != scope['token']:
        return _gate_response(module, _empty(scope, '当前版本的展示数据尚未准备好，后台将在夜间更新。'), status_only=status_only)
    if status_only:
        epoch = timezone.now().timestamp()
        phase = sum(boundary <= epoch for boundary in published.get('period_boundaries', []))
        return _response(('status', stamp, phase), lambda: {'scope': public_scope(scope), 'scope_state': 'ready',
            'updated_at': published.get('updated_at'), 'revision': f"{published['revision']}:{phase}"})
    meta = published.get('meta')
    if not isinstance(meta, dict) or (meta.get('scope') or {}).get('token') != scope['token']:
        raise StorageError('展示目录身份不一致')
    now = timezone.now()
    period_key = _period_key(meta, now)
    view_meta = _remember(('periods', stamp, period_key), lambda: {
        **meta, 'periods': project_periods({'periods': meta.get('periods', [])}, now)['periods'],
        'scope': public_scope(scope), 'scope_state': 'ready', 'scope_message': ''}, _object_bytes)
    if module == 'catalog':
        phase = sum(boundary <= now.timestamp() for boundary in _boundaries(meta))
        return _response(('catalog', stamp, period_key), lambda: {
            **rio.describe_catalog(view_meta, published.get('available', [])),
            'availability': published.get('availability') if published.get('level_policy') == list(rio.LEVELS) else None,
            'revision': f"{published['revision']}:{phase}"})
    c = rio.coordinate(module, params, meta=view_meta)
    key = rio.key_for(c)
    path, expected_marker = _entry_location(published, key)
    record, record_stamp = _file(path) if path else (None, None)
    if (not record or record.get('coordinate') != c or not isinstance(record.get('response'), dict)
            or record.get('marker') != expected_marker):
        if not _retry:
            return read_public(module, params, status_only=status_only, _retry=True)
        return _body({'snapshot': {'state': 'pending', 'message': '该筛选范围的展示数据尚未准备好，请稍后查看。'},
                      'data': None, 'scope': public_scope(scope)})
    envelope = record['response']
    if (envelope.get('scope') or {}).get('token') != scope['token']:
        raise StorageError('展示快照身份不一致')
    snapshot = envelope.get('snapshot') or {}
    fetched = rio._stamp(snapshot.get('fetched_at'))
    expired = bool(fetched and now - fetched > rio.freshness_window(c) and snapshot.get('state') == 'ready')
    def result():
        if not expired:
            return envelope
        return {**envelope, 'snapshot': {**snapshot, 'state': 'stale', 'message': '本次更新未完成，保留上次数据。'}}
    return _response(('entry', record_stamp, expired), result)


def _gate_response(module, meta, *, status_only):
    from botend.services.rio_statistics import describe_catalog
    if status_only:
        data = {key: meta[key] for key in ('scope', 'scope_state', 'scope_message')}
        data['revision'] = None
    elif module == 'catalog':
        data = describe_catalog(meta, [])
        data['revision'] = None
    else:
        data = {'snapshot': {'state': 'pending', 'message': meta['scope_message']}, 'data': None, 'scope': meta['scope']}
    return _body(data)


def http_response(request, body):
    accept = request.headers.get('Accept-Encoding', '')
    use_gzip = False
    for part in accept.split(','):
        fields = [value.strip().lower() for value in part.split(';')]
        if fields[0] == 'gzip':
            try:
                use_gzip = float(next((v[2:] for v in fields[1:] if v.startswith('q=')), '1')) > 0
            except ValueError:
                pass
    etag = '"' + body.etag + ('-gzip' if use_gzip else '') + '"'
    validators = {value.strip().removeprefix('W/') for value in request.headers.get('If-None-Match', '').split(',')}
    if etag in validators or '*' in validators:
        response = HttpResponseNotModified()
    else:
        response = HttpResponse(body.packed if use_gzip else body.raw, content_type='application/json; charset=utf-8')
        if use_gzip:
            response['Content-Encoding'] = 'gzip'
    response['ETag'] = etag
    response['Vary'] = 'Accept-Encoding'
    # 版本门禁必须到达服务端；保留条件缓存，但不允许浏览器或代理直接复用六十秒旧响应。
    response['Cache-Control'] = 'public, no-cache, max-age=0, must-revalidate'
    return response


def audit_views(scope, *, deep=False, _retry=False):
    """运维检查展示文件；不会在缺失时重建，也不请求上游。"""
    catalog, _ = _publication()
    status, _ = _publication(status_only=True)
    if not catalog:
        exists = (directory() / 'current.json').exists() or (directory() / 'catalog.json').exists()
        return {'state': 'error' if exists else 'pending', 'message': '展示目录缺失或校验失败'}
    if catalog.get('scope') != scope:
        return {'state': 'pending', 'message': '当前版本展示目录缺失或校验失败'}
    if not status or status.get('revision') != catalog.get('revision') or status.get('scope') != scope:
        if not _retry:
            return audit_views(scope, deep=deep, _retry=True)
        return {'state': 'error', 'message': '展示状态文件与目录不一致'}
    if catalog.get('layout') == SCHEMA:
        reader, _ = _publication(reader=True)
        if not reader or reader.get('revision') != catalog.get('revision'):
            return {'state': 'error', 'message': '轻量读取目录缺失或校验失败'}
    invalid = []
    invalid_indexes = []
    if deep:
        for prefix, reference in catalog.get('indexes', {}).items():
            index, _ = _file(directory() / 'indexes' / f"{prefix}-{reference['slot']}.json")
            if not index or index.get('signature') != reference.get('signature') or index.get('scope') != scope:
                invalid_indexes.append(prefix)
        for key, marker in catalog.get('entries', {}).items():
            path = _entry_path(catalog, key)
            entry, _ = _file(path) if path else (None, None)
            if not entry or entry.get('marker') != marker:
                invalid.append(key)
        latest, _ = _publication(status_only=True)
        if latest and latest.get('revision') != status.get('revision') and not _retry:
            return audit_views(scope, deep=deep, _retry=True)
    return {'state': 'error' if invalid or invalid_indexes else 'ok', 'entries': len(catalog.get('entries', {})),
            'revision': catalog.get('revision'), 'invalid_entries': invalid, 'invalid_indexes': invalid_indexes}
