"""私有中间缓存的有界回收；只处理可识别的缓存，保留锁文件。"""
import os
from pathlib import Path
import re
import time

from django.conf import settings
from .spec_aggregation_cache import cache_root, _read_file, _valid, work_path
from .spec_stats_retention import _safe_path, _scope_lock


NAME = re.compile(r'([0-9a-f]{64})\.(json(?:\.gz)?|pending)')


def maintain_work_cache(*, apply=False, limit=128, scan_limit=4096, after='', now=None):
    """按稳定游标遍历，过期正文逐项拿写锁后复查；未知文件只报告。"""
    root = cache_root()
    now = time.time() if now is None else now
    ttl = max(2 * 86400, int(getattr(settings, 'SPEC_AGGREGATION_WORK_TTL_SECONDS', 7 * 86400)))
    result = dict(kind='aggregation_work', apply=apply, status='ok', deleted=0,
                  deleted_bytes=0, eligible=0, scanned=0, unknown=0,
                  next_cursor='', has_more=False, error='')
    if not root.exists():
        return result
    if not 1 <= limit <= 1000 or not 1 <= scan_limit <= 100000:
        raise ValueError('回收批次超出范围')
    # 控制目录不会进入业务缓存的三层布局。
    with _scope_lock(root / '.maintenance.lock', blocking=False, create=apply) as acquired:
        if not acquired:
            return {**result, 'status': 'busy'}
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs.sort()
            dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
            for name in sorted(files):
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                if relative <= after or name.endswith('.lock') or name.startswith('.'):
                    continue
                if result['scanned'] >= scan_limit or result['deleted'] >= limit:
                    return {**result, 'has_more': True}
                result['scanned'] += 1
                result['next_cursor'] = relative
                match = NAME.fullmatch(name)
                parts = path.relative_to(root).parts
                if (not match or len(parts) != 3 or not parts[0].isdigit()
                        or parts[1] != match[1][:2]):
                    result['unknown'] += 1
                    continue
                try:
                    _safe_path(path)
                    before = path.stat()
                    if before.st_nlink != 1 or now - before.st_mtime < ttl:
                        continue
                    lock = path.parent / (match[1] + '.lock')
                    known_lock = lock.exists()
                    if match[2] == 'pending' and not known_lock:
                        result['unknown'] += 1
                        continue
                    with _scope_lock(lock, blocking=False, create=apply) as owned:
                        if not owned or path.stat() != before:
                            continue
                        # 暂存文件可能中断，只有既存生产者锁能证明它归属该缓存。
                        if match[2] != 'pending':
                            data = _read_file(path)
                            if (not isinstance(data, dict) or not _valid(data, int(parts[0]), data.get('scope'))
                                    or work_path(int(parts[0]), data['scope']).name != match[1] + '.json.gz'):
                                result['unknown'] += 1
                                continue
                        result['eligible'] += 1
                        if apply:
                            path.unlink()
                            result['deleted'] += 1
                            result['deleted_bytes'] += before.st_size
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, TypeError):
                    result['unknown'] += 1
        result['next_cursor'] = ''
    return result
