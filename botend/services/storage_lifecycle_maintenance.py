"""Idle, opt-in maintenance of cold projection/download scopes.

Scheduling metadata is separate from cache contents. Each domain has its own
explicit apply switch; enabling news GC does not authorize these newer domains.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat

from django.conf import settings
from .download_checkpoint import atomic_json


_FLAGS = ('SPEC_STATS_GC_ENABLED', 'MYTHICSTATS_GC_ENABLED', 'DOWNLOAD_PARTS_GC_ENABLED')


def _safe(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('linked maintenance path')


@contextmanager
def _coordinator(root):
    _safe(root)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / 'worker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('unsafe maintenance lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def _state(root):
    path = root / 'state.json'
    _safe(path)
    if not path.exists():
        return {'schema': 1, 'aggregate_after': '', 'download_after': '', 'refused': {}}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
            raise ValueError('invalid maintenance state file')
        value = json.load(stream)
    if (not isinstance(value, dict) or set(value) != {'schema', 'aggregate_after', 'download_after', 'refused'}
            or value['schema'] != 1 or not isinstance(value['aggregate_after'], str)
            or not isinstance(value['download_after'], str) or not isinstance(value['refused'], dict)
            or len(value['refused']) > 512):
        raise ValueError('invalid maintenance state')
    return value


def _part_roots(base):
    """Bound discovery and prune .parts trees before inspecting their contents."""
    if not base.exists():
        _safe(base)
        return []
    pending = [base]
    roots = []
    remaining = 2048
    while pending:
        directory = pending.pop()
        _safe(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                remaining -= 1
                if remaining < 0:
                    raise ValueError('download discovery entry budget exceeded')
                if entry.is_symlink():
                    raise ValueError('linked download discovery entry')
                if not entry.is_dir(follow_symlinks=False):
                    continue
                path = Path(entry.path)
                if entry.name.endswith('.parts'):
                    roots.append(path)
                else:
                    pending.append(path)
    return sorted(roots, key=lambda p: p.relative_to(base).as_posix())


def _result(kind, apply=False, status='ok', deleted=0, error='', **extra):
    return dict(kind=kind, apply=apply, status=status, deleted=deleted,
                deleted_bytes=None, error=error, **extra)


def run_extended_maintenance():
    if not any(getattr(settings, flag, False) is True for flag in _FLAGS):
        return []
    root = Path(getattr(settings, 'STORAGE_MAINTENANCE_ROOT', Path(settings.BASE_DIR) / 'var/storage-maintenance'))
    results = []
    try:
        with _coordinator(root) as acquired:
            if not acquired:
                return [_result('lifecycle', status='busy')]
            state = _state(root)
            if getattr(settings, 'SPEC_STATS_GC_ENABLED', False) is True:
                apply = getattr(settings, 'SPEC_STATS_GC_AUTO_APPLY', False) is True
                try:
                    from .spec_stats_retention import maintain_scopes
                    response = maintain_scopes(Path(getattr(settings, 'MEDIA_ROOT', '') or 'media') / 'aggregated',
                        after=state['aggregate_after'],
                        scope_limit=max(1, min(8, int(getattr(settings, 'SPEC_STATS_GC_SCOPE_LIMIT', 8)))),
                        delete_limit=8, dry_run=not apply)
                    for item in response['results']:
                        key = 'aggregate:' + item['scope']
                        if item['status'] in ('refused', 'disabled', 'error'):
                            state['refused'][key] = str(item.get('reason', item['status']))[:200]
                        else:
                            state['refused'].pop(key, None)
                    if not response.get('error'):
                        state['aggregate_after'] = response['next_cursor'] if response['has_more'] else ''
                    paused = any(i['status'] == 'paused' for i in response['results'])
                    results.append(_result('aggregate', apply,
                        status='refused' if response.get('error') else 'partial' if any(
                            i['status'] in ('refused', 'disabled', 'error') for i in response['results']) else 'paused' if paused else 'ok',
                        deleted=sum(i.get('deleted', 0) for i in response['results']),
                        error=response.get('error', ''), scopes=len(response['results']),
                        has_more=response['has_more'] or paused))
                except Exception as exc:
                    results.append(_result('aggregate', apply, status='refused', error=str(exc)[:200]))
            if getattr(settings, 'MYTHICSTATS_GC_ENABLED', False) is True:
                apply = getattr(settings, 'MYTHICSTATS_GC_AUTO_APPLY', False) is True
                try:
                    from .mythicstats_retention import collect_garbage
                    response = collect_garbage(apply=apply, limit=32)
                    status = 'busy' if response['busy'] else 'refused' if response['blocked'] else 'ok'
                    if response['blocked']:
                        state['refused']['mythicstats'] = str(response.get('reason', 'blocked'))[:200]
                    elif not response['busy']:
                        state['refused'].pop('mythicstats', None)
                    results.append(_result('mythicstats', apply, status,
                        len(response['deleted']), response.get('reason', ''),
                        candidates=len(response['candidates'])))
                except Exception as exc:
                    results.append(_result('mythicstats', apply, status='refused', error=str(exc)[:200]))
            if getattr(settings, 'DOWNLOAD_PARTS_GC_ENABLED', False) is True:
                apply = getattr(settings, 'DOWNLOAD_PARTS_GC_AUTO_APPLY', False) is True
                try:
                    from .download_retention import maintain_download_parts
                    base = Path(getattr(settings, 'DOWNLOAD_PARTS_GC_BASE', Path(settings.BASE_DIR) / '.cache/wow_data_updates'))
                    roots = _part_roots(base)
                    eligible = [p for p in roots if p.relative_to(base).as_posix() > state['download_after']]
                    limit = max(1, min(8, int(getattr(settings, 'DOWNLOAD_PARTS_GC_ROOT_LIMIT', 8))))
                    chosen = eligible[:limit]
                    for path in chosen:
                        key = path.relative_to(base).as_posix()
                        response = maintain_download_parts(path, apply=apply)
                        blocked = response.get('blocked')
                        if blocked:
                            state['refused']['download:' + key] = str(blocked)[:200]
                        else:
                            state['refused'].pop('download:' + key, None)
                        results.append(_result('download_parts', apply,
                            status='refused' if blocked else 'paused' if response.get('paused') else 'ok',
                            deleted=response['deleted_attempts'], error=str(blocked or ''),
                            root=key, read_bytes=response.get('read_bytes', 0)))
                    state['download_after'] = chosen[-1].relative_to(base).as_posix() if chosen and len(eligible) > len(chosen) else ''
                except Exception as exc:
                    results.append(_result('download_parts', apply, status='refused', error=str(exc)[:200]))
            if len(state['refused']) > 512 or len(json.dumps(state).encode()) > 1024 * 1024:
                raise ValueError('maintenance status budget exceeded')
            atomic_json(root / 'state.json', state)
    except Exception as exc:
        results.append(_result('lifecycle', status='refused', error=str(exc)[:200]))
    return results
