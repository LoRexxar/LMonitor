"""配装目录文件投影：请求只读文件，后台按需构建，失败保留已发布版本。"""
import hashlib
import gzip
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid
import zlib

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import close_old_connections, transaction
from django.db.models import Count, Max
from django.http import FileResponse, HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.http import parse_etags

from botend.models import WowItemSnapshot, WowItemVariantSnapshot
from botend.services import gear_builder as gb
from botend.services.simc_benchmark_result_snapshot import _load, _lock, _write

SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)
_thread = None
_thread_lock = threading.Lock()


def snapshot_root():
    return Path(getattr(settings, 'GEAR_CATALOG_SNAPSHOT_ROOT',
                        Path(settings.BASE_DIR) / 'var' / 'gear-catalog'))


def coordinate(class_name, spec_name, slot, kind='equipment'):
    class_name, spec_name = gb.canonical_spec(class_name, spec_name)
    if slot not in gb.SLOT_LABELS:
        raise gb.GearBuilderError('未知装备槽位')
    if kind not in ('equipment', 'enhancements'):
        raise gb.GearBuilderError('未知目录类型')
    key = {'class_name': class_name, 'spec_name': spec_name, 'slot': slot}
    return {**key, 'kind': kind} if kind == 'enhancements' else key


def _directory(key):
    prefix = 'enhancements-' if key.get('kind') == 'enhancements' else ''
    return snapshot_root() / f"{prefix}{key['class_name']}-{key['spec_name']}-{key['slot']}"


def request_refresh(key):
    """坐标经过白名单验证，持久登记后不会因进程重启丢失。"""
    key = coordinate(**key)
    path = _directory(key) / 'request.json'
    if _load(path) != key:
        _write(path, key)
    elif time.time() - path.stat().st_mtime > 60:
        # 每分钟最多记录一次热度，不随筛选次数频繁写盘。
        path.touch()
    return key


def invalidate_catalog_snapshots():
    """批量更新方在事务提交后通知；周期指纹检查补偿错失的通知。"""
    def enqueue():
        try:
            _write(snapshot_root() / 'revision.json', {'revision': uuid.uuid4().hex})
        except OSError:
            logger.exception('装备目录快照更新通知写入失败')
    transaction.on_commit(enqueue)


def _source():
    season = gb.active_season()
    catalog = gb.catalog_context(season)
    variants = WowItemVariantSnapshot.objects.filter(season=season, batch_key=season.gear_batch_key) if season else WowItemVariantSnapshot.objects.none()
    facts = {
        'schema': SCHEMA_VERSION, 'catalog': catalog,
        'variants': variants.aggregate(count=Count('pk'), updated=Max('updated_at')),
        'items': WowItemSnapshot.objects.aggregate(count=Count('pk'), updated=Max('updated_at')),
        'revision': _load(snapshot_root() / 'revision.json', {}),
    }
    version = hashlib.sha256(json.dumps(facts, cls=DjangoJSONEncoder, sort_keys=True).encode()).hexdigest()
    return season, catalog, version


def _index(key):
    data = _load(_directory(key) / 'index.json')
    if (not isinstance(data, dict) or data.get('schema_version') != SCHEMA_VERSION
            or data.get('coordinate') != key or not re.fullmatch(r'[0-9a-f]{32}\.json', str(data.get('file', '')))):
        return None
    try:
        if (_directory(key) / data['file']).stat().st_size != data.get('bytes'):
            return None
        if data.get('gzip_bytes') and (_directory(key) / (data['file'] + '.gz')).stat().st_size != data['gzip_bytes']:
            return None
    except OSError:
        return None
    return data


def catalog_snapshot_response(request, *, kind='equipment'):
    """此读取路径不查询数据库，也不触发同步聚合。"""
    key = coordinate(request.GET.get('class') or 'Warrior',
                     request.GET.get('spec') or 'Fury', request.GET.get('slot') or 'head', kind)
    try:
        request_refresh(key)
    except OSError:
        logger.exception('装备目录请求登记失败')
        response = JsonResponse({'success': False, 'error': '装备目录暂不可用，请稍后重试。'}, status=503)
        response['Cache-Control'] = 'no-store'
        return response
    index = _index(key)
    if index is None:
        response = JsonResponse({'success': True, 'items': [], 'snapshot': {'state': 'building'}}, status=202)
        response['Retry-After'] = '3'
        response['Cache-Control'] = 'no-store'
        return response
    if kind == 'enhancements' and request.GET.get('snapshot') != '1':
        payload = _load(_directory(key) / index['file'])
        if not isinstance(payload, dict) or payload.get('snapshot', {}).get('coordinate') != key:
            return JsonResponse({'success': True, 'groups': {}, 'snapshot': {'state': 'building'}}, status=202)
        response = JsonResponse({'success': True, 'catalog': payload['catalog'],
                                 'groups': gb.filter_enhancement_snapshot(payload, request.GET.get('variant_id')),
                                 'snapshot': payload['snapshot']})
        response['Cache-Control'] = 'public, max-age=60'
        return response
    encodings = request.headers.get('Accept-Encoding', '').lower()
    compressed = any(part.split(';')[0].strip() == 'gzip'
                     and not re.search(r';\s*q=0(?:\.0*)?\s*$', part) for part in encodings.split(','))
    compressed = compressed and bool(index.get('gzip_bytes'))
    filename = index['file'] + ('.gz' if compressed else '')
    etag = f'"{index["generation"]}{"-gzip" if compressed else ""}"'
    client_tags = [tag.removeprefix('W/') for tag in parse_etags(request.headers.get('If-None-Match', ''))]
    if etag in client_tags or '*' in client_tags:
        response = HttpResponse(status=304)
    else:
        try:
            response = FileResponse((_directory(key) / filename).open('rb'), content_type='application/json')
        except OSError:
            response = JsonResponse({'success': False, 'error': '装备目录暂不可用，请稍后重试。'}, status=503)
            response['Cache-Control'] = 'no-store'
            return response
    response['ETag'] = etag
    response['Cache-Control'] = 'public, max-age=60'
    response['Vary'] = 'Accept-Encoding'
    if compressed:
        response['Content-Encoding'] = 'gzip'
    return response


def _verified_index(key):
    """后台/命令完整回读；Web 热路径仍只检查小索引和文件长度。"""
    index = _index(key)
    if index is None:
        return None
    try:
        raw = (_directory(key) / index['file']).read_bytes()
        payload = json.loads(raw)
        snapshot = payload['snapshot']
        content = {name: value for name, value in payload.items() if name not in ('success', 'total', 'snapshot')}
        digest = hashlib.sha256(json.dumps(content, cls=DjangoJSONEncoder, sort_keys=True,
                                            ensure_ascii=False).encode()).hexdigest()
        if (payload.get('success') is not True or snapshot.get('state') != 'ready'
                or snapshot.get('coordinate') != key or snapshot.get('generation') != index['generation']
                or digest != index.get('content_hash') or not index.get('gzip_bytes')
                or gzip.decompress((_directory(key) / (index['file'] + '.gz')).read_bytes()) != raw):
            return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError, zlib.error):
        return None
    return index


def refresh_catalog_snapshots(*, batch_size=1, force=False, poll=False, targets=None, kind='all', strict=False):
    """后台有界轮询；手工预热只构建目标坐标并严格回读验收。"""
    root = snapshot_root()
    built = []
    if targets is not None:
        targets = list({json.dumps(coordinate(**key), sort_keys=True): coordinate(**key) for key in targets}.values())
    with _lock(root / 'worker.lock', blocking=False) as acquired:
        if not acquired:
            if strict:
                raise RuntimeError('装备目录快照锁忙，未完成预热')
            return built
        requests = ([_directory(key) / 'request.json' for key in targets]
                    if targets is not None else list(root.glob('*/request.json')))
        if not requests:
            return built
        checked = _load(root / 'source-check.json', {})
        revision = _load(root / 'revision.json', {})
        if poll and not force and checked.get('revision') == revision and time.time() - checked.get('at', 0) < 300:
            # 空闲轮次只检查小索引；事件或新分片立即唤醒，漏通知每五分钟补偿。
            def needs_build(path):
                index = _load(path.parent / 'index.json', {})
                return (index.get('source_version') != checked.get('version')
                        or time.time() - index.get('built_at', 0) > 43200)
            if not any(needs_build(path) for path in requests):
                return built
        season, catalog, version = _source()
        _write(root / 'source-check.json', {'at': time.time(), 'revision': revision, 'version': version})
        pending = []
        current = []
        for path in requests:
            try:
                key = coordinate(**(_load(path) or {}))
                if _directory(key) != path.parent:
                    continue
                if kind != 'all' and key.get('kind', 'equipment') != kind:
                    continue
            except (TypeError, gb.GearBuilderError):
                continue
            index = _index(key)
            if force or index is None or index.get('source_version') != version or time.time() - index.get('built_at', 0) > 43200:
                # 失败坐标退到后面，避免长期阻塞其他请求。
                attempt = _load(path.parent / 'attempt.json', {})
                retry = attempt.get('at', 0) > (index or {}).get('built_at', 0)
                if poll and retry and time.time() - attempt.get('at', 0) < 60:
                    continue
                pending.append(((retry, index is not None, -path.stat().st_mtime), key))
            else:
                current.append(key)
        selected = [key for _, key in sorted(pending, key=lambda row: row[0])[:max(1, batch_size)]]
        for key in selected:
            directory = _directory(key)
            _write(directory / 'attempt.json', {'at': time.time()})
            try:
                if key.get('kind') == 'enhancements':
                    identity = {name: key[name] for name in ('class_name', 'spec_name', 'slot')}
                    content = {**gb.enhancement_snapshot_payload(**identity, season=season), 'catalog': catalog}
                    total = sum(len(rows) for rows in content['groups'].values()) + len(content['embellishment_options'])
                else:
                    rows = gb.catalog_snapshot_items(**key, season=season)
                    content = {'items': rows, 'catalog': catalog,
                               'source_groups': {k: sorted(v) for k, v in gb.QUICK_SOURCE_FILTERS.items()}}
                    total = len(rows)
                content_hash = hashlib.sha256(json.dumps(content, cls=DjangoJSONEncoder, sort_keys=True,
                                                         ensure_ascii=False).encode()).hexdigest()
                previous = _index(key)
                if previous and previous.get('content_hash') == content_hash and _verified_index(key):
                    if _source()[2] != version:
                        break
                    _write(directory / 'index.json', {**previous, 'source_version': version, 'built_at': time.time()})
                    built.append(key)
                    continue
                generation = uuid.uuid4().hex
                payload = {'success': True, **content, 'total': total,
                           'snapshot': {'state': 'ready', 'generation': generation,
                                        'generated_at': timezone.now().isoformat(), 'coordinate': key}}
                filename = f'{generation}.json'
                _write(directory / filename, payload)
                # 压缩只在后台做一次，普通 Web 响应直接传输已压缩文件。
                compressed = gzip.compress((directory / filename).read_bytes(), compresslevel=6, mtime=0)
                with (directory / (filename + '.gz')).open('xb') as stream:
                    stream.write(compressed)
                    stream.flush()
                    os.fsync(stream.fileno())
                # 构建期间有源更新时放弃发布，下一轮重新生成，旧文件继续可读。
                if _source()[2] != version:
                    break
                _write(directory / 'index.json', {
                    'schema_version': SCHEMA_VERSION, 'coordinate': key, 'generation': generation,
                    'content_hash': content_hash,
                    'source_version': version, 'file': filename, 'built_at': time.time(),
                    'bytes': (directory / filename).stat().st_size,
                    'gzip_bytes': len(compressed),
                })
                built.append(key)
            except Exception:
                logger.exception('装备目录分片构建失败：%s', key)
        if strict:
            expected = targets if targets is not None else (selected + current)[:max(1, batch_size)]
            required = [key for _, key in pending] if targets is not None else selected
            failed = [key for key in required if key not in built]
            for key in expected:
                index = _verified_index(key)
                if not index or index.get('source_version') != version:
                    if key not in failed:
                        failed.append(key)
            if failed:
                raise RuntimeError(f'装备目录预热未完成：{len(failed)} 个目标分片生成或回读失败')
            if expected and _source()[2] != version:
                raise RuntimeError('装备目录来源在预热期间变化，请重试')
    return built


def start_catalog_snapshot_worker():
    """由监控后端启动独立维护线程，不占 Web 请求和普通采集线程。"""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        def run():
            while True:
                close_old_connections()
                try:
                    refresh_catalog_snapshots(batch_size=4, poll=True)
                except Exception:
                    logger.exception('装备目录快照维护失败')
                finally:
                    close_old_connections()
                time.sleep(15)
        _thread = threading.Thread(target=run, name='gear-catalog-snapshot', daemon=True)
        _thread.start()
