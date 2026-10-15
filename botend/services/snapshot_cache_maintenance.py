"""Idle cache maintenance; deliberately independent of snapshot rebuild eligibility."""
import threading

from django.conf import settings
from utils.log import logger
from botend.services.snapshot_cache_gc import maintain_snapshot_caches

_guard = threading.Lock()
_thread = None


def run_maintenance_once():
    # Opt in after an operator reviews a production dry-run. Publishers retain
    # history even while automatic deletion is disabled.
    results = maintain_snapshot_caches(
        apply=getattr(settings, 'SNAPSHOT_CACHE_GC_AUTO_APPLY', False) is True,
    )
    from .storage_lifecycle_maintenance import run_extended_maintenance
    results.extend(run_extended_maintenance())
    for result in results:
        logger.info('[Snapshot Cache Maintenance] kind=%s apply=%s status=%s deleted=%s bytes=%s error=%s',
                    result['kind'], result['apply'], result['status'], result['deleted'],
                    result['deleted_bytes'], result.get('error', ''))
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
