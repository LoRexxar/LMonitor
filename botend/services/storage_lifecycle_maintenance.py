"""独立回收聚合历史；下载和其他快照继续使用各自的显式开关。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat

from django.conf import settings
from .simc_benchmark_result_snapshot import _write as atomic_json
from .spec_stats_retention import _scope_lock, _safe_path


_FLAGS = ('SPEC_STATS_GC_ENABLED', 'MYTHICSTATS_GC_ENABLED', 'DOWNLOAD_PARTS_GC_ENABLED')


def _safe(path):
    _safe_path(path)


@contextmanager
def _coordinator(root):
    _safe(root)
    with _scope_lock(root / 'worker.lock', blocking=False) as acquired:
        yield acquired


def _state(root):
    path = root / 'state.json'
    _safe(path)
    if not path.exists():
        return {'schema': 1, 'aggregate_after': '', 'download_after': '', 'refused': {}}
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
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


def _part_roots(base, *, prune_tooltips=False):
    """Bound discovery; prune .parts contents and default-producer tooltips only.

    UpdateSource uses <branch>/<a|b>/source for legacy/current and versioned
    Wago downloads, and <branch>/<a|b>/loot-specializations for its flat source.
    BranchCatalogSource owns <branch>/<a|b>/current/wowhead: tooltip JSONs, not
    download parts. Do not spend the discovery budget traversing that tree.
    Explicit DOWNLOAD_PARTS_GC_BASE overrides remain arbitrary recursive trees.
    """
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
                relative = path.relative_to(base).parts
                if (prune_tooltips and len(relative) == 4 and relative[1] in ('a', 'b')
                        and relative[2:] == ('current', 'wowhead')):
                    continue
                if entry.name.endswith('.parts'):
                    roots.append(path)
                else:
                    pending.append(path)
    return sorted(roots, key=lambda p: p.relative_to(base).as_posix())


def _result(kind, apply=False, status='ok', deleted=0, error='', **extra):
    return dict(kind=kind, apply=apply, status=status, deleted=deleted,
                deleted_bytes=extra.pop('deleted_bytes', None), error=error, **extra)


def run_extended_maintenance():
    if not any(getattr(settings, flag, flag == 'SPEC_STATS_GC_ENABLED') is True for flag in _FLAGS):
        return []
    root = Path(getattr(settings, 'STORAGE_MAINTENANCE_ROOT', Path(settings.BASE_DIR) / 'var/storage-maintenance'))
    results = []
    try:
        with _coordinator(root) as acquired:
            if not acquired:
                return [_result('lifecycle', status='busy')]
            state = _state(root)
            if getattr(settings, 'SPEC_STATS_GC_ENABLED', True) is True:
                apply = getattr(settings, 'SPEC_STATS_GC_AUTO_APPLY', True) is True
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
                        deleted_bytes=sum(i.get('deleted_bytes', 0) for i in response['results']),
                        error=response.get('error', ''), scopes=len(response['results']),
                        paused_scopes=sum(i['status'] == 'paused' for i in response['results']),
                        bytes_read=sum(i.get('bytes_read', 0) for i in response['results']),
                        reasons=[{'scope': i['scope'], 'status': i['status'], 'reason': i.get('reason', '')}
                                 for i in response['results'] if i['status'] not in ('ok', 'bootstrapped')],
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
                    roots = _part_roots(base, prune_tooltips=not hasattr(settings, 'DOWNLOAD_PARTS_GC_BASE'))
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
