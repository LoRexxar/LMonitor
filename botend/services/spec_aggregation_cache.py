"""大块中间结果写私有文件；人物重试状态留表，兼容旧版数据库缓存。"""
import json
import gzip
import math
import zlib
from datetime import date, datetime
from decimal import Decimal
import hashlib
import logging
import os
import re
from pathlib import Path

from django.conf import settings
from botend.models import SpecAggregationWorkCache
from django.db import connection
from botend.services.spec_stats_retention import _scope_lock, _safe_path
from botend.services.spec_stats_snapshot import content_digest, _bounded_canonical_chunks

logger = logging.getLogger(__name__)
SCHEMA = 1
MAX_WORK_BYTES = 64 * 1024 * 1024
NEW_PREFIX = b'{"data":'
NEW_SUFFIX = b',"digest":"'


class WorkCacheTooLarge(OSError):
    """可重建缓存超出预算；调用方仍可使用已经计算的结果。"""


def byte_limit():
    return max(1024, int(getattr(settings, 'SPEC_AGGREGATION_WORK_MAX_BYTES', MAX_WORK_BYTES)))
PREFIX = b'{"digest":"'
SEPARATOR = b'","data":'
HEADER_BYTES = 4096
_FINGERPRINT_MISMATCH = object()
# Only the known scalar prefix is recognized. Unknown order/long/old headers
# fall through to the existing complete reader, never a guessed cache hit.
_JSON_STRING = rb'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"'
_WORK_HEADER = re.compile(
    rb'\{\s*"schema"\s*:\s*([0-9]+)\s*,\s*"season_id"\s*:\s*([0-9]+)'
    rb'\s*,\s*"scope"\s*:\s*(' + _JSON_STRING + rb')'
    rb'\s*,\s*"fingerprint"\s*:\s*(' + _JSON_STRING + rb')\s*,\s*"payload"\s*:')


def cache_root():
    """采集器与聚合器共享的私有目录，不与公开发布目录混放。"""
    root = Path(getattr(settings, 'SPEC_AGGREGATION_WORK_ROOT',
                        Path(settings.BASE_DIR) / 'var' / 'spec-aggregation-work'))
    _safe_path(root)
    root = root.resolve()
    public = [getattr(settings, 'MEDIA_ROOT', None), getattr(settings, 'STATIC_ROOT', None)]
    public.extend(getattr(settings, 'STATICFILES_DIRS', []))
    for directory in public:
        if isinstance(directory, (tuple, list)):
            directory = directory[-1]
        if directory and root.is_relative_to(Path(directory).resolve()):
            raise ValueError('聚合中间缓存必须位于私有目录')
    return root


def work_path(season_id, scope):
    # 业务范围不直接拼入路径，避免角色名、区域或首领名影响目录边界。
    key = content_digest(scope)
    path = cache_root() / str(int(season_id)) / key[:2] / (key + '.json.gz')
    _safe_path(path)
    return path


def _normalized(value):
    """只复制需要标量转换的分支，不再整块编码后解码。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _normalized(float(value))
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('缓存不允许非有限数值')
    if isinstance(value, dict):
        def key(k):
            if isinstance(k, str):
                return k
            if k is None:
                return 'null'
            if type(k) is bool:
                return 'true' if k else 'false'
            if isinstance(k, (int, float)):
                _normalized(k)
                return str(k)
            raise TypeError('缓存对象键必须是JSON支持的标量')
        result = {key(k): _normalized(v) for k, v in value.items()}
        return value if len(value) == len(result) and all(
            k in value and value[k] is v for k, v in result.items()) else result
    if isinstance(value, (list, tuple)):
        result = [_normalized(v) for v in value]
        return value if isinstance(value, list) and all(a is b for a, b in zip(value, result)) else result
    return value


def _header_fingerprint(raw, season_id, scope):
    """Unauthenticated hint: a match must still pass full body validation."""
    if raw.startswith(NEW_PREFIX):
        start = len(NEW_PREFIX)
    elif raw.startswith(PREFIX):
        start = len(PREFIX) + 64 + len(SEPARATOR)
        if raw[len(PREFIX) + 64:start] != SEPARATOR:
            return None
    else:
        return None
    match = _WORK_HEADER.match(raw, start)
    if match is None:
        return None
    try:
        schema, season, stored_scope, fingerprint = json.loads(b'[' + b','.join(match.groups()) + b']')
        if schema == SCHEMA and season == int(season_id) and stored_scope == scope:
            return fingerprint
    except (ValueError, TypeError):
        pass
    return None


def work_fingerprint_hint(season_id, scope):
    """Only prioritize request-local inputs; never evidence of a cache hit.

    Request at most HEADER_BYTES from gzip (which may prefetch internally);
    no sidecar or index.
    An unrecognized existing gzip header does not consult a possibly stale
    plain predecessor. False hints only cost a safe recomputation later.
    """
    try:
        path = work_path(season_id, scope)
    except OSError:
        return None
    limit = byte_limit()
    for candidate in (path, path.with_suffix('')):
        try:
            if candidate.stat().st_size > limit:
                return None
            opener = gzip.open if candidate.name.endswith('.gz') else open
            with opener(candidate, 'rb') as stream:
                raw = stream.read(min(HEADER_BYTES, limit + 1))
            return _header_fingerprint(raw, season_id, scope) if len(raw) <= limit else None
        except FileNotFoundError:
            continue
        except (OSError, EOFError, ValueError, zlib.error):
            return None
    return None


def _read_file(path, *, expected=None):
    """有界解压；头部只作快速否定，完整正文仍以视图核验摘要。"""
    try:
        limit = byte_limit()
        if path.stat().st_size > limit:
            return None
        opener = gzip.open if path.name.endswith('.gz') else open
        with opener(path, 'rb') as stream:
            raw = b''
            if expected is not None:
                raw = stream.read(min(HEADER_BYTES, limit + 1))
                hint = _header_fingerprint(raw, expected[0], expected[1])
                if hint is not None and hint != expected[2]:
                    return _FINGERPRINT_MISMATCH
            # Continue the SAME descriptor: atomic replacement cannot splice
            # a header from one generation onto another generation's body.
            raw += stream.read(limit + 1 - len(raw))
        if len(raw) > limit:
            return None
        if raw.startswith(NEW_PREFIX):
            end = len(raw) - len(NEW_SUFFIX) - 64 - 2
            if raw[end:end + len(NEW_SUFFIX)] != NEW_SUFFIX or not raw.endswith(b'"}'):
                return None
            digest = raw[-66:-2]
            body = memoryview(raw)[len(NEW_PREFIX):end]
        else:
            start = len(PREFIX) + 64 + len(SEPARATOR)
            if (not raw.startswith(PREFIX) or raw[len(PREFIX) + 64:start] != SEPARATOR
                    or not raw.endswith(b'}')):
                return None
            body = memoryview(raw)[start:-1]
            digest = raw[len(PREFIX):len(PREFIX) + 64]
        if hashlib.sha256(body).hexdigest().encode('ascii') != digest:
            return None
        return json.loads(raw)['data']
    except (OSError, EOFError, ValueError, KeyError, TypeError, zlib.error):
        return None


def _valid(data, season_id, scope):
    if not isinstance(data, dict):
        return False
    return (data.get('schema') == SCHEMA and data.get('season_id') == int(season_id)
            and data.get('scope') == scope and isinstance(data.get('fingerprint'), str)
            and 'payload' in data)


def load_work(season_id, scope, fingerprint):
    """新缓存不回查旧大表；旧表兼容导入由显式开关控制。"""
    from .stats_storage_codec import unpack_detail
    import_legacy = getattr(settings, 'SPEC_AGGREGATION_IMPORT_LEGACY_DATABASE', False)
    # Explicit DB migration retains the original corruption/fallback semantics.
    expected = None if import_legacy else (season_id, scope, fingerprint)
    try:
        path = work_path(season_id, scope)
        data = _read_file(path, expected=expected)
        if data is None:
            data = _read_file(path.with_suffix(''), expected=expected)
    except OSError:
        logger.warning('读取聚合中间缓存失败：赛季=%s 范围=%s', season_id, scope, exc_info=True)
        data = None
    if data is _FINGERPRINT_MISMATCH:
        return None
    if _valid(data, season_id, scope):
        if data['fingerprint'] != fingerprint:
            return None
        payload = data['payload']
        try:
            return unpack_detail(payload) if isinstance(payload, dict) else payload
        except (ValueError, KeyError, TypeError):
            return None
    if not import_legacy:
        return None
    payload = _load_database(season_id, scope, fingerprint)
    if payload is not None:
        save_work(season_id, scope, fingerprint, payload)
    return payload


def save_work(season_id, scope, fingerprint, payload, *, strict=False, raw_json=False, durable=True):
    """日志检查点可直接编码JSON；可重建派生数据允许省略刷盘屏障。

    durable=False仍保留原子替换和摘要，断电后允许丢失缓存并从中央事实重建。
    排名采集进度、范围统计等既有调用默认仍强制刷盘。
    """
    from .stats_storage_codec import pack_detail
    if not raw_json:
        payload = _normalized(payload)
    stored = pack_detail(payload) if isinstance(payload, dict) and not raw_json else payload
    data = {'schema': SCHEMA, 'season_id': int(season_id), 'scope': scope,
            'fingerprint': fingerprint, 'payload': stored}
    try:
        path = work_path(season_id, scope)
        # 与旧版共用同一把锁，升级过程也不允许两个写入者同时提交。
        with _scope_lock(path.with_suffix('').with_suffix('.lock')):
            pending = path.with_suffix('').with_suffix('.pending')
            _safe_path(pending)
            digest, size = hashlib.sha256(), len(NEW_PREFIX) + len(NEW_SUFFIX) + 66
            encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            with pending.open('wb') as stream:
                with gzip.GzipFile(fileobj=stream, mode='wb', compresslevel=3, mtime=0) as compressed:
                    compressed.write(NEW_PREFIX)
                    buffer = bytearray()
                    fragments = (_bounded_canonical_chunks(data, encoder) if raw_json
                                 else encoder.iterencode(data))
                    for fragment in fragments:
                        chunk = fragment.encode('utf-8')
                        size += len(chunk)
                        if size > byte_limit():
                            raise WorkCacheTooLarge('中间缓存单项超过字节预算')
                        digest.update(chunk)
                        buffer.extend(chunk)
                        if len(buffer) >= 65536:
                            compressed.write(buffer)
                            buffer.clear()
                    compressed.write(buffer)
                    compressed.write(NEW_SUFFIX + digest.hexdigest().encode('ascii') + b'"}')
                stream.flush()
                if durable:
                    os.fsync(stream.fileno())
            os.replace(pending, path)
            if durable and os.name != 'nt':
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
    except OSError:
        logger.warning('保存聚合中间缓存失败：赛季=%s 范围=%s', season_id, scope, exc_info=True)
        if strict:
            raise
    return payload


def _load_database(season_id, scope, fingerprint):
    row = SpecAggregationWorkCache.objects.filter(
        season_id=season_id, scope=scope, fingerprint=fingerprint).first()
    if row is not None and content_digest(row.payload) == row.payload_hash:
        return row.payload
    return None


def load_state(season_id, scope, fingerprint):
    """仅人物刷新状态使用关系表，避免大块结果继续回流数据库。"""
    if not scope.startswith(('battlenet:', 'player-ranking:')):
        raise ValueError('仅允许保存人物刷新状态和本地榜单引用')
    return _load_database(season_id, scope, fingerprint)


def save_state(season_id, scope, fingerprint, payload):
    if not scope.startswith(('battlenet:', 'player-ranking:')):
        raise ValueError('仅允许保存人物刷新状态和本地榜单引用')
    payload = _normalized(payload)
    SpecAggregationWorkCache.objects.bulk_create([SpecAggregationWorkCache(
        season_id=season_id, scope=scope, fingerprint=fingerprint,
        payload=payload, payload_hash=content_digest(payload))], update_conflicts=True,
        update_fields=['fingerprint', 'payload', 'payload_hash', 'updated_at'],
        unique_fields=['season', 'scope'] if connection.features.supports_update_conflicts_with_target else None)
    return payload
