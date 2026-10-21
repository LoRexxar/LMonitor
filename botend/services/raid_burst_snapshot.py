"""团本战斗统计的私有中间文件；页面只能读取已经发布的版本。"""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from django.conf import settings


SCHEMA = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def private_root():
    root = Path(getattr(settings, 'RAID_BURST_SNAPSHOT_ROOT',
                        Path(settings.BASE_DIR) / 'var' / 'raid-burst')).resolve()
    # 登录数据不可落入公开媒体或静态目录。
    public = [getattr(settings, 'MEDIA_ROOT', None), getattr(settings, 'STATIC_ROOT', None)]
    public.extend(getattr(settings, 'STATICFILES_DIRS', []))
    for directory in public:
        if isinstance(directory, (tuple, list)):
            directory = directory[-1]
        if directory and root.is_relative_to(Path(directory).resolve()):
            raise ValueError('战斗统计缓存必须位于私有目录')
    return root


def write_json(path, payload):
    """同目录写入后原子替换；异常时保留旧版本及临时文件供排查。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.pending-', suffix='.json', dir=path.parent)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path):
    def invalid_constant(value):
        raise ValueError('JSON 含非有限数值')
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'), parse_constant=invalid_constant)
    except (OSError, ValueError):
        return None


def read_blob(directory, signature):
    if not isinstance(signature, str) or not re.fullmatch(r'[a-f0-9]{64}', signature):
        return None
    data = read_json(Path(directory) / 'snapshots' / f'{signature}.json')
    if isinstance(data, dict) and data.get('schema') == SCHEMA and digest(data) == signature:
        return data
    return None


def read_manifest(directory, name='index.json', *, verify_blobs=True):
    envelope = read_json(Path(directory) / name)
    if not isinstance(envelope, dict):
        return None
    manifest = envelope.get('data')
    if not isinstance(manifest, dict) or envelope.get('digest') != digest(manifest):
        return None
    if manifest.get('schema') != SCHEMA or not isinstance(manifest.get('scopes'), list):
        return None
    if verify_blobs and any(read_blob(directory, item.get('blob')) is None for item in manifest['scopes']):
        return None
    return manifest


def load_published(selection=None):
    """仅文件读取，缺失或损坏时不联网、不查询排名、不现场聚合。"""
    if not selection:
        return None, []
    options = []
    for path in private_root().glob('*/*/*/index.json'):
        manifest = (read_manifest(path.parent, verify_blobs=False)
                    or read_manifest(path.parent, 'previous.json', verify_blobs=False))
        if manifest:
            for scope in manifest['scopes']:
                options.append((scope, path.parent))
    options.sort(key=lambda pair: (-pair[0]['season_id'], not pair[0].get('sample_count', 0),
                                   -pair[0].get('difficulty', 5), pair[0]['key']))
    selected = next((pair for pair in options if pair[0]['key'] == selection), None)
    data = read_blob(selected[1], selected[0]['blob']) if selected else None
    if selected and data is None:
        previous = read_manifest(selected[1], 'previous.json', verify_blobs=False)
        old = next((s for s in previous['scopes'] if s['key'] == selected[0]['key']), None) if previous else None
        data = read_blob(selected[1], old['blob']) if old else None
    if data and data.get('scope', {}).get('key') != selection:
        data = None
    return data, [dict(key=item['key'], label=item['label']) for item, _ in options]


def needs_raid_burst_refresh(cycle):
    """已发布团本周期缺少本版战斗统计时，交给原聚合任务补建。

    只查索引，不联网，不扫描原始事件；未完成的采集周期由调用者优先处理。
    """
    raid_scopes = [scope for scope in cycle.completed_scopes if scope.startswith('raid:')]
    if not raid_scopes or not cycle.collected_at:
        return False
    from botend.services.raid_burst_aggregation import ANALYSIS_VERSION, RAW_VERSION, SUPPORTED
    cutoff = cycle.collected_at.isoformat()
    root = private_root() / str(cycle.season_id)
    for (class_name, spec_name), config in SUPPORTED.items():
        manifest = read_manifest(root / class_name / spec_name, verify_blobs=False)
        revision = digest({'analysis': ANALYSIS_VERSION, 'raw': RAW_VERSION, 'config': config})
        if not manifest or manifest.get('profile_digest') != revision or manifest.get('source_cutoff') != cutoff:
            return True
        keys = {scope['key'] for scope in manifest['scopes']}
        expected = {f'{cycle.season_id}/{class_name}/{spec_name}/{scope.split(":")[1]}/{scope.split(":")[2]}'
                    for scope in raid_scopes}
        if not expected.issubset(keys):
            return True
        for scope in manifest['scopes']:
            signature = scope.get('blob', '')
            if (not re.fullmatch(r'[a-f0-9]{64}', signature)
                    or not (root / class_name / spec_name / 'snapshots' / f'{signature}.json').is_file()):
                return True
    return False
