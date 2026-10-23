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
from pathlib import Path

from django.conf import settings
from botend.models import SpecAggregationWorkCache
from django.db import connection
from botend.services.spec_stats_retention import _scope_lock, _safe_path
from botend.services.spec_stats_snapshot import content_digest

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


def _read_file(path):
    """有界解压；直接校验正文视图，不额外复制大块字节。"""
    try:
        if path.stat().st_size > byte_limit():
            return None
        opener = gzip.open if path.name.endswith('.gz') else open
        with opener(path, 'rb') as stream:
            raw = stream.read(byte_limit() + 1)
        if len(raw) > byte_limit():
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
    try:
        path = work_path(season_id, scope)
        data = _read_file(path)
        if data is None:
            data = _read_file(path.with_suffix(''))
    except OSError:
        logger.warning('读取聚合中间缓存失败：赛季=%s 范围=%s', season_id, scope, exc_info=True)
        data = None
    if _valid(data, season_id, scope):
        if data['fingerprint'] != fingerprint:
            return None
        payload = data['payload']
        try:
            return unpack_detail(payload) if isinstance(payload, dict) else payload
        except (ValueError, KeyError, TypeError):
            return None
    if not getattr(settings, 'SPEC_AGGREGATION_IMPORT_LEGACY_DATABASE', False):
        return None
    payload = _load_database(season_id, scope, fingerprint)
    if payload is not None:
        save_work(season_id, scope, fingerprint, payload)
    return payload


def save_work(season_id, scope, fingerprint, payload, *, strict=False):
    from .stats_storage_codec import pack_detail
    payload = _normalized(payload)
    stored = pack_detail(payload) if isinstance(payload, dict) else payload
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
                    for fragment in encoder.iterencode(data):
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
                os.fsync(stream.fileno())
            os.replace(pending, path)
            if os.name != 'nt':
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
    if not scope.startswith('battlenet:'):
        raise ValueError('仅允许保存 Battle.net 刷新状态')
    return _load_database(season_id, scope, fingerprint)


def save_state(season_id, scope, fingerprint, payload):
    if not scope.startswith('battlenet:'):
        raise ValueError('仅允许保存 Battle.net 刷新状态')
    payload = _normalized(payload)
    SpecAggregationWorkCache.objects.bulk_create([SpecAggregationWorkCache(
        season_id=season_id, scope=scope, fingerprint=fingerprint,
        payload=payload, payload_hash=content_digest(payload))], update_conflicts=True,
        update_fields=['fingerprint', 'payload', 'payload_hash', 'updated_at'],
        unique_fields=['season', 'scope'] if connection.features.supports_update_conflicts_with_target else None)
    return payload
