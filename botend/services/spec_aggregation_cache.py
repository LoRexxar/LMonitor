"""大块中间结果写私有文件；人物重试状态留表，兼容旧版数据库缓存。"""
import json
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
    path = cache_root() / str(int(season_id)) / key[:2] / (key + '.json')
    _safe_path(path)
    return path


def _normalized(payload):
    from botend.controller.plugins.portal.SpecDetailAggregationMonitor import DecimalEncoder
    return json.loads(json.dumps(payload, cls=DecimalEncoder, ensure_ascii=False, allow_nan=False))


def _read_file(path):
    """读取标准 JSON 信封，以原始数据字节校验，避免命中时重新序列化整块结果。"""
    try:
        raw = path.read_bytes()
        start = len(PREFIX) + 64 + len(SEPARATOR)
        if (not raw.startswith(PREFIX) or raw[len(PREFIX) + 64:start] != SEPARATOR
                or not raw.endswith(b'}')):
            return None
        body = raw[start:-1]
        if hashlib.sha256(body).hexdigest().encode('ascii') != raw[len(PREFIX):len(PREFIX) + 64]:
            return None
        return json.loads(body)
    except (OSError, ValueError):
        return None


def _valid(data, season_id, scope):
    if not isinstance(data, dict):
        return False
    return (data.get('schema') == SCHEMA and data.get('season_id') == int(season_id)
            and data.get('scope') == scope and isinstance(data.get('fingerprint'), str)
            and 'payload' in data)


def load_work(season_id, scope, fingerprint):
    """文件命中不查询数据库；旧表只作为首次升级或文件损坏时的兼容来源。"""
    try:
        data = _read_file(work_path(season_id, scope))
    except OSError:
        logger.warning('读取聚合中间缓存失败：赛季=%s 范围=%s', season_id, scope, exc_info=True)
        data = None
    if _valid(data, season_id, scope):
        return data['payload'] if data['fingerprint'] == fingerprint else None
    payload = _load_database(season_id, scope, fingerprint)
    if payload is not None:
        # 不删除旧记录，已应用 0252 的环境可直接升级；之后只写文件。
        save_work(season_id, scope, fingerprint, payload)
    return payload


def save_work(season_id, scope, fingerprint, payload):
    from botend.controller.plugins.portal.SpecDetailAggregationMonitor import DecimalEncoder
    data = {'schema': SCHEMA, 'season_id': int(season_id), 'scope': scope,
            'fingerprint': fingerprint, 'payload': payload}
    body = json.dumps(data, cls=DecimalEncoder, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':')).encode('utf-8')
    payload = json.loads(body)['payload']
    header = PREFIX + hashlib.sha256(body).hexdigest().encode('ascii') + SEPARATOR
    try:
        path = work_path(season_id, scope)
        # 同范围串行提交；固定暂存文件使反复失败不累计临时文件。
        with _scope_lock(path.with_suffix('.lock')):
            pending = path.with_suffix('.pending')
            _safe_path(pending)
            with pending.open('wb') as stream:
                stream.write(header)
                stream.write(body)
                stream.write(b'}')
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
        # 缓存故障不能把已经完成的计算变成全链路失败；旧文件仍可核验复用。
        logger.warning('保存聚合中间缓存失败：赛季=%s 范围=%s', season_id, scope, exc_info=True)
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
