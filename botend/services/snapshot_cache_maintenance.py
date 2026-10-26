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
    if getattr(settings, 'VARIANT_TEXT_MAINTENANCE_ENABLED', True):
        try:
            from .variant_text import maintain_variant_text
            results.append(maintain_variant_text())
        except Exception as exc:
            results.append(dict(kind='variant_text', apply=True, status='error',
                                deleted=0, deleted_bytes=0, error=str(exc)))
    for result in results:
        logger.info('[Snapshot Cache Maintenance] kind=%s apply=%s status=%s deleted=%s bytes=%s error=%s',
                    result['kind'], result['apply'], result['status'], result['deleted'],
                    result['deleted_bytes'], result.get('error', ''))
        if result.get('reasons'):
            logger.info('[Snapshot Cache Maintenance] kind=%s paused=%s read_bytes=%s reasons=%s',
                        result['kind'], result.get('paused_scopes', 0),
                        result.get('bytes_read', 0), result['reasons'])
        if result['kind'] == 'variant_text' and 'items' in result:
            logger.info('装备共享文本维护：检查物品=%s，修改变体=%s，下次起点=%s，错误数=%s',
                        result['items'], result['variants'], result.get('next_cursor', ''), len(result['errors']))
    return results


def _loop(lifecycle=None):
    from botend.backend_lifecycle import worker_cycles
    interval = lambda: max(60, int(getattr(settings, 'SNAPSHOT_CACHE_GC_INTERVAL_SECONDS', 3600)))
    for _ in worker_cycles(lifecycle, interval=interval, label='snapshot-cache-maintenance'):
        try:
            run_maintenance_once()
        except Exception:
            logger.exception('[Snapshot Cache Maintenance] cycle failed')



def start_snapshot_cache_maintenance_worker(*, lifecycle=None):
    global _thread
    if not getattr(settings, 'SNAPSHOT_CACHE_GC_WORKER_ENABLED', True):
        return None
    with _guard:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, kwargs={'lifecycle': lifecycle},
                                       name='snapshot-cache-maintenance', daemon=True)
            _thread.start()
        return _thread
