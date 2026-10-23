"""Idle cache maintenance; deliberately independent of snapshot rebuild eligibility."""
import threading

from django.conf import settings
from utils.log import logger
from botend.services.snapshot_cache_gc import maintain_snapshot_caches
from .spec_work_retention import maintain_work_cache

_guard = threading.Lock()
_thread = None
_work_cursor = ''


def run_maintenance_once():
    global _work_cursor
    # Opt in after an operator reviews a production dry-run. Publishers retain
    # history even while automatic deletion is disabled.
    try:
        results = maintain_snapshot_caches(
            apply=getattr(settings, 'SNAPSHOT_CACHE_GC_AUTO_APPLY', False) is True,
        )
    except Exception as exc:
        results = [dict(kind='snapshot', apply=False, status='error',
                        deleted=0, deleted_bytes=0, error=str(exc))]
    if getattr(settings, 'SPEC_AGGREGATION_WORK_GC_ENABLED', True):
        try:
            result = maintain_work_cache(
                apply=getattr(settings, 'SPEC_AGGREGATION_WORK_GC_AUTO_APPLY', True) is True,
                after=_work_cursor)
            if result['status'] != 'busy':
                _work_cursor = result['next_cursor'] if result['has_more'] else ''
            results.append(result)
        except Exception as exc:
            results.append(dict(kind='aggregation_work', apply=False, status='error',
                                deleted=0, deleted_bytes=0, error=str(exc)))
    try:
        from .storage_lifecycle_maintenance import run_extended_maintenance
        results.extend(run_extended_maintenance())
    except Exception as exc:
        # 一个领域的导入或检查失败，不能掩盖其他领域的回收结果。
        results.append(dict(kind='lifecycle', apply=False, status='error',
                            deleted=0, deleted_bytes=0, error=str(exc)))
    for result in results:
        logger.info('[Snapshot Cache Maintenance] kind=%s apply=%s status=%s deleted=%s bytes=%s error=%s',
                    result['kind'], result['apply'], result['status'], result['deleted'],
                    result['deleted_bytes'], result.get('error', ''))
        if result.get('reasons'):
            logger.info('[Snapshot Cache Maintenance] kind=%s paused=%s read_bytes=%s reasons=%s',
                        result['kind'], result.get('paused_scopes', 0),
                        result.get('bytes_read', 0), result['reasons'])
    return results


def _loop():
    stop = threading.Event()
    while True:
        try:
            run_maintenance_once()
        except Exception:
            logger.exception('[Snapshot Cache Maintenance] cycle failed')
        interval = max(60, int(getattr(settings, 'SNAPSHOT_CACHE_GC_INTERVAL_SECONDS', 3600)))
        stop.wait(interval)


def start_snapshot_cache_maintenance_worker():
    global _thread
    if not getattr(settings, 'SNAPSHOT_CACHE_GC_WORKER_ENABLED', True):
        return None
    with _guard:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name='snapshot-cache-maintenance', daemon=True)
            _thread.start()
        return _thread
