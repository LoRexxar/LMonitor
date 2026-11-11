"""有容量限制的私有恢复暂存，不替代正式事实。

序列化与压缩在锁外执行；文件操作使用跨进程锁。保留完整事件，
路径及诊断不含人物标识，过期时间从原始采集时刻起算，读取不续期。
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import time
import uuid
import zlib

from django.conf import settings

log = logging.getLogger(__name__)
_MARKER = '.wcl-raw-staging-v1'
_OWNED = re.compile(r'(?:[0-9a-f]{64}\.gz|\.tmp-[0-9a-f]{32})\Z')
_PREPARED_BYTES = 16 * 1024**2
_PREPARED_FILES = 20


class UnsafePath(Exception):
    pass


class _StagingRejected(Exception):
    def __init__(self, status):
        self.status = status


@contextmanager
def record_duration(timings, key):
    """累计阶段耗时，异常路径也计入；不记录报告、人物或文件名。"""
    started = time.monotonic()
    try:
        yield
    finally:
        if timings is not None:
            timings[key] = timings.get(key, 0.0) + time.monotonic() - started


# Cache-only failures; deliberately exclude database exceptions.
_CACHE_ERRORS = (OSError, ValueError, TypeError, KeyError, EOFError,
                 zlib.error, AttributeError, OverflowError, UnsafePath)


def _limits():
    return (max(0, int(getattr(settings, 'WCL_RAW_STAGING_TTL_SECONDS', 48 * 3600))),
            max(0, int(getattr(settings, 'WCL_RAW_STAGING_MAX_BYTES', 512 * 1024**2))),
            max(0, int(getattr(settings, 'WCL_RAW_STAGING_MAX_FILE_BYTES', 16 * 1024**2))))


@contextmanager
def _locked(*, timings=None):
    path = Path(getattr(settings, 'WCL_RAW_STAGING_DIR',
                        Path(settings.BASE_DIR) / 'var' / 'wcl-raw-staging')).absolute()
    media = getattr(settings, 'MEDIA_ROOT', None) or Path(settings.BASE_DIR) / 'media'
    if ('..' in path.parts or len(path.parts) < 3 or path == Path(settings.BASE_DIR).absolute()
            or (media and os.path.commonpath([str(path), os.path.abspath(media)]) == os.path.abspath(media))):
        raise UnsafePath()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    lock = None
    acquired = None
    try:
        for part in path.parts[1:]:
            if part in ('.', '..'):
                raise UnsafePath()
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        # Validate ownership before changing permissions or deleting anything.
        # A mistaken cache setting must never adopt an unrelated directory.
        names = os.listdir(fd)
        if any(name not in ('.lock', _MARKER) and not _OWNED.fullmatch(name) for name in names):
            raise UnsafePath()
        if _MARKER not in names and any(name != '.lock' for name in names):
            raise UnsafePath()
        lock = os.open('.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_nlink != 1:
            raise UnsafePath()
        with record_duration(timings, 'staging_lock_wait_seconds'):
            fcntl.flock(lock, fcntl.LOCK_EX)
        acquired = time.monotonic()
        try:
            marker = os.open(_MARKER, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        except FileExistsError:
            marker = os.open(_MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                info = os.fstat(marker)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.read(marker, 32) != b'wcl-raw-staging-v1\n':
                    raise UnsafePath()
            finally:
                os.close(marker)
        else:
            try:
                os.write(marker, b'wcl-raw-staging-v1\n')
                os.fsync(marker)
            finally:
                os.close(marker)
            os.fsync(fd)
        os.fchmod(fd, 0o700)
        yield fd
    finally:
        if acquired is not None and timings is not None:
            key = 'staging_lock_hold_seconds'
            timings[key] = timings.get(key, 0.0) + time.monotonic() - acquired
        if lock is not None:
            os.close(lock)
        os.close(fd)


def _identity(code, fight):
    if not isinstance(code, str) or not code or type(fight) is not int or fight <= 0:
        raise ValueError('Invalid staging identity')
    return hashlib.sha256(json.dumps([code, fight], separators=(',', ':')).encode()).hexdigest()


def _read(fd, name, limit):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    with os.fdopen(handle, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError('Invalid staging file')
        with gzip.GzipFile(fileobj=stream) as compressed:
            raw = compressed.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Staging payload exceeds limit')
    value = json.loads(raw)
    payload = value['events']
    digest = hashlib.sha256(_json(payload)).hexdigest()
    if value['sha256'] != digest or not isinstance(payload, list):
        raise ValueError('Staging checksum mismatch')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _prune(fd, now, ttl, limit, file_limit):
    kept, removed, freed = [], 0, 0
    for name in os.listdir(fd):
        if name in ('.lock', _MARKER):
            continue
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not _OWNED.fullmatch(name):
            raise UnsafePath()
        # Admission/GC uses small filesystem metadata, not a full decompression
        # of the entire cache on each RPC. Requested reads validate the envelope.
        stamp = info.st_mtime
        valid = name.endswith('.gz') and 0 <= now - stamp < ttl and info.st_size <= file_limit
        if valid:
            kept.append((stamp, name, info.st_size))
        else:
            os.unlink(name, dir_fd=fd)
            removed += 1
            freed += info.st_size
    total = sum(row[2] for row in kept)
    kept.sort()
    while total > limit and kept:
        _, name, size = kept.pop(0)
        os.unlink(name, dir_fd=fd)
        total -= size
        removed += 1
        freed += size
    return kept, {'removed': removed, 'bytes_removed': freed, 'bytes': total}


def _failure(exc):
    status = 'unsafe_path' if isinstance(exc, UnsafePath) or getattr(exc, 'errno', None) in (40, 20) else 'io_error'
    log.warning('WCL raw staging unavailable status=%s', status)
    return {'status': status, 'stored': 0}


def _prepare_event(code, fight, events, stamp, ttl, budget, file_limit):
    """锁外编码一次事件列表，超出原文限制时不再压缩。"""
    if not 0 <= time.time() - stamp < ttl:
        raise _StagingRejected('expired')
    identity = _identity(code, fight)
    if not isinstance(events, list) or any(not isinstance(e, dict) or e.get('fight') != fight for e in events):
        raise ValueError('Invalid staging events')
    payload = _json(events)
    digest = hashlib.sha256(payload).hexdigest()
    header = _json({'identity': identity, 'fetched_at': stamp, 'sha256': digest})
    # 复用已编码的列表，避免为摘要和外层信封分别编码整个事件树。
    if len(header) + len(b',"events":') + len(payload) > file_limit:
        raise _StagingRejected('quota')
    raw = header[:-1] + b',"events":' + payload + b'}'
    packed = gzip.compress(raw, mtime=0)
    if len(packed) > file_limit or len(packed) > budget:
        raise _StagingRejected('quota')
    return identity, stamp, digest, packed


def _prepared_batches(code, by_fight, default_stamp, clocks, ttl, budget, file_limit, timings):
    """按压缩字节数及文件数分组，避免把整批原文复制到无界内存。"""
    pending, size = [], 0
    for fight, events in by_fight.items():
        with record_duration(timings, 'staging_prepare_seconds'):
            source_time = clocks.get(fight)
            stamp = source_time.timestamp() if source_time is not None else default_stamp
            prepared = _prepare_event(code, fight, events, stamp, ttl, budget, file_limit)
        if pending and (size + len(prepared[3]) > _PREPARED_BYTES or len(pending) >= _PREPARED_FILES):
            yield pending
            pending, size = [], 0
        pending.append(prepared)
        size += len(prepared[3])
    if pending:
        yield pending


def _write_payload(fd, identity, stamp, packed):
    """持锁原子替换；文件和目录同步均保留，失败时只清理当前临时文件。"""
    temp = '.tmp-' + uuid.uuid4().hex
    try:
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=fd)
        with os.fdopen(handle, 'wb') as stream:
            stream.write(packed)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, identity + '.gz', src_dir_fd=fd, dst_dir_fd=fd)
        os.utime(identity + '.gz', (stamp, stamp), dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass


def stage_events(code, by_fight, *, fetched_at=None, fetched_at_by_fight=None, timings=None):
    """锁外准备有界压缩批次，锁内复核期限、容量并原子落盘。"""
    stored, total = 0, 0
    try:
        ttl, budget, file_limit = _limits()
        now = time.time()
        default_stamp = fetched_at.timestamp() if fetched_at is not None else now
        for prepared in _prepared_batches(code, by_fight, default_stamp, fetched_at_by_fight or {},
                                          ttl, budget, file_limit, timings):
            with _locked(timings=timings) as fd:
                kept, summary = _prune(fd, time.time(), ttl, budget, file_limit)
                for identity, stamp, digest, packed in prepared:
                    now = time.time()
                    if not 0 <= now - stamp < ttl:
                        raise _StagingRejected('expired')
                    # 相同内容复用原文件，不延长原始暂存期限。
                    if any(row[1] == identity + '.gz' for row in kept):
                        try:
                            previous = _read(fd, identity + '.gz', file_limit)
                            if (previous['identity'] == identity and previous['sha256'] == digest
                                    and type(previous['fetched_at']) in (int, float)
                                    and 0 <= now - previous['fetched_at'] < ttl):
                                stored += 1
                                continue
                        except _CACHE_ERRORS:
                            pass
                    # 写入中的临时文件也占预算，保留原有容量及文件数量保护。
                    while sum(row[2] for row in kept) + len(packed) > budget and kept:
                        _, name, _ = kept.pop(0)
                        os.unlink(name, dir_fd=fd)
                    while len(kept) >= 4096:
                        _, name, _ = kept.pop(0)
                        os.unlink(name, dir_fd=fd)
                    with record_duration(timings, 'staging_write_seconds'):
                        _write_payload(fd, identity, stamp, packed)
                    kept = [row for row in kept if row[1] != identity + '.gz']
                    kept.append((stamp, identity + '.gz', len(packed)))
                    kept.sort()
                    stored += 1
                total = sum(row[2] for row in kept)
            del prepared, packed
        return {'status': 'stored', 'stored': stored, 'bytes': total}
    except _StagingRejected as exc:
        return {'status': exc.status, 'stored': stored}
    except _CACHE_ERRORS as exc:
        return _failure(exc)


def load_staged_events(code, fights, *, with_fetched_at=False):
    """仅完整命中时返回事件；恢复调用可同时取得逐场原始采集时间。"""
    try:
        ttl, budget, file_limit = _limits()
        with _locked() as fd:
            _prune(fd, time.time(), ttl, budget, file_limit)
            result = []
            fetched_at_by_fight = {}
            for fight in sorted(set(fights)):
                identity = _identity(code, fight)
                value = _read(fd, identity + '.gz', file_limit)
                stamp = value.get('fetched_at')
                if (type(stamp) not in (int, float) or not 0 <= time.time() - stamp < ttl
                        or value['identity'] != identity or any(not isinstance(e, dict) or e.get('fight') != fight for e in value['events'])):
                    raise ValueError('Staging identity mismatch')
                result.extend(value['events'])
                fetched_at_by_fight[fight] = datetime.fromtimestamp(stamp, tz=timezone.utc)
            return (result, fetched_at_by_fight) if with_fetched_at else result
    except FileNotFoundError:
        return None
    except _CACHE_ERRORS as exc:
        _failure(exc)
        return None


def prune_staging():
    """Called automatically on every read/write; also available to operators."""
    try:
        ttl, budget, file_limit = _limits()
        with _locked() as fd:
            _, summary = _prune(fd, time.time(), ttl, budget, file_limit)
            os.fsync(fd)
            return summary
    except _CACHE_ERRORS as exc:
        return _failure(exc)


def discard_staged_events(code, fights):
    """Release only after facts and negative-state readback processing succeeds."""
    try:
        with _locked() as fd:
            for fight in fights:
                try:
                    os.unlink(_identity(code, fight) + '.gz', dir_fd=fd)
                except FileNotFoundError:
                    pass
            os.fsync(fd)
    except _CACHE_ERRORS as exc:
        _failure(exc)
