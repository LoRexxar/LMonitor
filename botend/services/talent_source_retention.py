"""受管天赋来源保留当前和上一份；数据库引用与运行中读取阻止回收。"""
import json
import hashlib
import errno
import os
from pathlib import Path
import re
import stat
import threading
import time
from contextlib import contextmanager
from functools import wraps

from django.conf import settings

_condition = threading.Condition(threading.RLock())
_readers = 0
_reader_stream = None
_cleaning = False
_local = threading.local()
_digest = re.compile(r'[0-9a-f]{64}')


def source_root():
    return Path(settings.BASE_DIR).absolute() / '.cache' / 'wow_data_updates'


def _safe(path):
    """拒绝符号链接、Windows 联接及其他重解析点，包含所有上级目录。"""
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            return False
    return True


def _open_lock(root, cleanup):
    path = root / '.talent-source.lock'
    if not _safe(path):
        raise ValueError('天赋来源锁文件包含链接')
    stream = path.open('a+b')
    try:
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError('天赋来源锁文件包含硬链接')
        if os.name == 'nt':
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b'0')
                stream.flush()
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if cleanup:
                        stream.close()
                        return None
                    time.sleep(.05)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if cleanup else fcntl.LOCK_SH)
            except BlockingIOError:
                stream.close()
                return None
        return stream
    except BaseException:
        stream.close()
        raise


def _close_lock(stream):
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        stream.close()


@contextmanager
def source_access(*, cleanup=False):
    """来源读写持共享锁；回收非阻塞抢独占锁，线程和进程均参与引用保护。"""
    global _readers, _reader_stream, _cleaning
    if not settings.configured:
        yield not cleanup
        return
    if getattr(_local, 'held', False):
        yield not cleanup
        return
    root = source_root()
    if not root.exists():
        yield not cleanup
        return
    if not _safe(root):
        raise ValueError('天赋来源根目录包含链接，拒绝访问')
    stream = None
    with _condition:
        if cleanup:
            if not _readers and not _cleaning:
                stream = _open_lock(root, True)
                _cleaning = bool(stream)
            acquired = bool(stream)
        else:
            while _cleaning:
                _condition.wait()
            if not _readers:
                _reader_stream = _open_lock(root, False)
            _readers += 1
            acquired = True
    if not acquired:
        yield False
        return
    _local.held = True
    try:
        yield True
    finally:
        _local.held = False
        with _condition:
            if cleanup:
                _close_lock(stream)
                _cleaning = False
            else:
                _readers -= 1
                if not _readers:
                    _close_lock(_reader_stream)
                    _reader_stream = None
            _condition.notify_all()


def protect_sources(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with source_access():
            return function(*args, **kwargs)
    return guarded


def _absolute(value):
    path = Path(value)
    if not path.is_absolute():
        path = Path(settings.BASE_DIR) / path
    return Path(os.path.abspath(path))


def retained_sources(current, previous, old_report):
    """仅成功发布才能推进上一份引用；重复发布同目录不能丢失回退版本。"""
    saved = (old_report or {}).get('talent_sources') or {}
    return {'current': current, 'previous': previous if previous and previous != current else saved.get('previous', '')}


def managed_source_matches(path, branch, build):
    """赠送节点导入兼容摘要目录，仍以清单证明精确分支和来源构建。"""
    path = _absolute(path)
    if (not _safe(path) or not _digest.fullmatch(path.name)
            or path.parent not in [source_root() / branch / slot / 'talents' for slot in ('a', 'b')]):
        return False
    try:
        from botend.services.download_checkpoint import encoded
        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        return bool(manifest) and hashlib.sha256(encoded(manifest)).hexdigest() == path.name and all(
            row['identity']['branch'] == branch and row['identity']['build'] == build
            for row in manifest.values())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _remove_source(path, entries):
    # 保留归属证明到最后，某个文件被占用时下轮仍能验证并继续回收。
    proof = 'manifest.json' if _digest.fullmatch(path.name) else 'retention_owner.json'
    for child in sorted(entries, key=lambda entry: entry.name == proof):
        child.unlink()
    path.rmdir()


def prune_talent_sources(branch):
    """仅回收指定分支 a/b/talents 下已识别的无引用目录，不碰其他缓存。"""
    from botend.models import WowTalentVersion, WowDataUpdateState
    if branch not in ('retail', 'ptr', 'beta'):
        raise ValueError('无法识别的天赋来源分支')
    report = {'removed': [], 'retained': [], 'blocked': [], 'busy': False}
    with source_access(cleanup=True) as locked:
        if not locked:
            report['busy'] = True
            return report
        state = WowDataUpdateState.objects.filter(branch=branch).first()
        # 首次接入不能猜测历史目录哪份发布成功；等下次发布记录真实前驱后再清理。
        if not state or not (state.report or {}).get('talent_sources', {}).get('current'):
            report['blocked'].append('尚无可核验的当前及上一份发布记录')
            return report
        # 所有绑定均算引用，包括非活动历史版本，不能按 is_active 忽略它们。
        values = list(WowTalentVersion.objects.exclude(source_dir='').values_list('source_dir', flat=True))
        states = list(WowDataUpdateState.objects.values_list('report', flat=True))
        for state in states:
            values.extend(value for value in (state or {}).get('talent_sources', {}).values() if value)
        refs = [_absolute(value) for value in values]
        root = source_root()
        for slot in ('a', 'b'):
            parent = root / branch / slot / 'talents'
            if not parent.exists() or not _safe(parent):
                continue
            for path in parent.iterdir():
                if not (_digest.fullmatch(path.name) or re.fullmatch(r'\.preparing-[A-Za-z0-9_-]+', path.name)):
                    continue
                # 删除前验证最终绝对路径仍属于唯一受管父目录，拒绝链接与未知内容。
                if not _safe(path) or not path.is_dir() or path.resolve().parent != parent.resolve():
                    report['blocked'].append(str(path))
                    continue
                if any(ref == path or path in ref.parents or ref in path.parents for ref in refs):
                    report['retained'].append(str(path))
                    continue
                entries = list(path.iterdir())
                if not entries and _digest.fullmatch(path.name):
                    path.rmdir()
                    report['removed'].append(str(path))
                    continue
                if any(not _safe(child) or not child.is_file() or child.stat().st_nlink != 1
                       or not (re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*\.csv', child.name)
                               or child.name in ('manifest.json', 'display_fallback.json', 'retention_owner.json')) for child in entries):
                    report['blocked'].append(str(path))
                    continue
                if _digest.fullmatch(path.name):
                    try:
                        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
                        if not manifest or any(row['identity']['branch'] != branch for row in manifest.values()):
                            raise ValueError('来源分支不符')
                        from botend.services.download_checkpoint import encoded
                        if hashlib.sha256(encoded(manifest)).hexdigest() != path.name:
                            raise ValueError('来源摘要不符')
                    except (OSError, ValueError, KeyError, TypeError):
                        report['blocked'].append(str(path))
                        continue
                else:
                    try:
                        owner = json.loads((path / 'retention_owner.json').read_text(encoding='utf-8'))
                        if owner != {'branch': branch, 'purpose': 'talent-source-staging'}:
                            raise ValueError('暂存目录归属不明')
                    except (OSError, ValueError, TypeError):
                        report['blocked'].append(str(path))
                        continue
                _remove_source(path, entries)
                report['removed'].append(str(path))
    return report
