"""冒险手册掉落文件投影：按发布版本、副本、难度和资料分支隔离。"""
import hashlib
import json
import logging
from pathlib import Path
import threading
import time

from django.conf import settings
from django.db import close_old_connections
from botend.services.simc_benchmark_result_snapshot import _load, _lock, _write

logger = logging.getLogger(__name__)
_thread = None
_thread_lock = threading.Lock()
SCHEMA = 1


def snapshot_root():
    return Path(getattr(settings, 'JOURNAL_LOOT_SNAPSHOT_ROOT', Path(settings.BASE_DIR) / 'var' / 'journal-loot'))


def _directory(key):
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return snapshot_root() / digest


def read_loot_projection(release_id, instance_id, difficulty, source, *, journal_version=None):
    """冷启动只登记任务；失败保留本坐标的旧版，不回退到在线聚合。"""
    key = {'schema': SCHEMA, 'release_id': release_id, 'instance_id': instance_id,
           'difficulty': difficulty, 'source': source}
    if journal_version is not None:
        key['journal_version'] = journal_version
    directory = _directory(key)
    try:
        if _load(directory / 'request.json') != key:
            _write(directory / 'request.json', key)
        data = _load(directory / 'data.json')
        if (isinstance(data, dict) and data.get('coordinate') == key and isinstance(data.get('loot'), list)
                and data.get('snapshot', {}).get('state') == 'ready'):
            return data
        if time.time() - (directory / 'request.json').stat().st_mtime > 60:
            (directory / 'request.json').touch()
    except OSError:
        logger.exception('冒险手册掉落快照读取失败')
        return {'snapshot': {'state': 'unavailable'}}
    return {'snapshot': {'state': 'building'}}


def refresh_journal_loot_snapshots(*, batch_size=4, force=False, poll=False):
    from botend.journal_models import JournalInstance
    from botend.portal.adventure_journal import current_release, instance_source, build_loot_projection
    from botend.services.gear_catalog_snapshot import _source, snapshot_root as gear_root
    from botend.services.journal_snapshot import loot_version
    built = []
    root = snapshot_root()
    with _lock(root / 'worker.lock', blocking=False) as acquired:
        if not acquired:
            return built
        requests = list(root.glob('*/request.json'))
        if not requests:
            return built
        checked = _load(root / 'source-check.json', {})
        revision = _load(gear_root() / 'revision.json', {})
        if poll and not force and checked.get('revision') == revision and time.time() - checked.get('at', 0) < 300:
            if not checked.get('pending') and not any(path.stat().st_mtime > checked.get('at', 0) for path in requests):
                return built
        started_at = time.time()
        season, _, version = _source()
        release = current_release()
        if not release:
            return built
        pending = []
        for path in requests:
            key = _load(path, {})
            if key.get('schema') != SCHEMA or key.get('release_id') != release.id or _directory(key) != path.parent:
                continue
            previous = _load(path.parent / 'data.json', {})
            if not force and previous.get('source_version') == version and time.time() - previous.get('built_at', 0) < 3600:
                continue
            attempt = _load(path.parent / 'attempt.json', {}).get('at', 0)
            if poll and attempt > previous.get('built_at', 0) and time.time() - attempt < 60:
                continue
            pending.append((attempt, path, key))
        for _, path, key in sorted(pending, key=lambda entry: entry[0])[:max(1, batch_size)]:
            _write(path.parent / 'attempt.json', {'at': time.time()})
            try:
                instance = JournalInstance.objects.get(release=release, journal_id=key['instance_id'])
                if key['difficulty'] not in instance.payload['difficulty_ids']:
                    continue
                source = instance_source(release, instance.journal_id, season=season)
                if source != key['source']:
                    continue
                bosses = [boss for boss in instance.encounters.all() if key['difficulty'] in boss.payload['difficulty_ids']]
                if key.get('journal_version') and loot_version(bosses, key['difficulty']) != key['journal_version']:
                    continue
                payload = build_loot_projection(release, instance.journal_id, bosses, key['difficulty'], source)
                if _source()[2] != version:
                    break
                if key.get('journal_version'):
                    active = current_release()
                    if (not active or active.id != release.id or
                            instance_source(active, instance.journal_id, season=season) != source or
                            loot_version(list(instance.encounters.all()), key['difficulty']) != key['journal_version']):
                        continue
                _write(path.parent / 'data.json', {**payload, 'coordinate': key, 'source_version': version,
                    'built_at': time.time(), 'snapshot': {'state': 'ready', 'generated_at': time.time()}})
                built.append(key)
            except Exception:
                logger.exception('冒险手册掉落快照构建失败：%s', key)
        _write(root / 'source-check.json', {'at': started_at, 'revision': revision, 'version': version,
                                          'pending': len(pending) > max(1, batch_size)})
    return built


def start_journal_loot_snapshot_worker():
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        def run():
            while True:
                close_old_connections()
                try:
                    refresh_journal_loot_snapshots(poll=True)
                except Exception:
                    logger.exception('冒险手册掉落维护失败')
                finally:
                    close_old_connections()
                time.sleep(15)
        _thread = threading.Thread(target=run, name='journal-loot-snapshot', daemon=True)
        _thread.start()
