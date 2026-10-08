"""Rebuildable, private JSON read model; never an execution/result authority.

Web requests read immutable coordinate files via one atomically published index.
Only the background maintenance worker (or explicit management command) projects
DB results. A failed build leaves the previous index and pending events intact.
All serving/refresh workers must share SIMC_BENCHMARK_RESULT_SNAPSHOT_ROOT.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import close_old_connections, transaction
from django.db.models.signals import post_save
from django.utils import timezone

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1
_COORDINATE_KEYS = ('spec_key', 'profile_key', 'scenario_key')
_thread = None
_thread_lock = threading.Lock()


def snapshot_root():
    return Path(getattr(settings, 'SIMC_BENCHMARK_RESULT_SNAPSHOT_ROOT',
                        Path(settings.BASE_DIR) / 'var' / 'benchmark-results'))


def _panel_dir(panel_id):
    return snapshot_root() / str(int(panel_id))


def _load(path, default=None):
    try:
        with path.open(encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, cls=DjangoJSONEncoder, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Windows 不支持目录描述符；文件刷盘和原子替换仍必须完成。
        if os.name != 'nt':
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _lock(path, *, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a+b') as stream:
        if os.name == 'nt':
            # 固定锁住首字节，包含空文件；不同进程必须竞争同一区间。
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not blocking:
                        yield False
                        return
                    time.sleep(0.05)
            try:
                yield True
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            return
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _coordinate_key(coordinate):
    values = [coordinate.get(key, '') for key in _COORDINATE_KEYS]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def request_snapshot_refresh(panel_id, coordinates=None, *, only_if_missing=False):
    """Merge durable events without overwriting a concurrently queued coordinate."""
    directory = _panel_dir(panel_id)
    with _lock(directory / 'events.lock'):
        pending = _load(directory / 'pending.json', {})
        if only_if_missing and pending:
            return
        revision = uuid.uuid4().hex
        if coordinates is None:
            pending['*'] = {'revision': revision}
        else:
            for coordinate in coordinates:
                pending[_coordinate_key(coordinate)] = {
                    'revision': revision,
                    'coordinate': {key: coordinate[key] for key in _COORDINATE_KEYS},
                }
        _write(directory / 'pending.json', pending)


def invalidate_result_snapshot(panel_id, coordinates=None):
    """Commit first: a derived-file IO failure must never roll back simulation data."""
    def enqueue():
        try:
            request_snapshot_refresh(panel_id, coordinates)
        except Exception:
            logger.exception('Benchmark snapshot invalidation failed for panel %s', panel_id)
    transaction.on_commit(enqueue)


def _panel_saved(sender, instance, **kwargs):
    if not kwargs.get('raw'):
        invalidate_result_snapshot(instance.pk)


def _execution_saved(sender, instance, **kwargs):
    if instance.completed_at is not None and not kwargs.get('raw'):
        invalidate_result_snapshot(instance.panel_id)


def _configuration_saved(sender, instance, **kwargs):
    if kwargs.get('raw'):
        return
    from botend.models import SimcBenchmarkSpec
    if hasattr(instance, 'panel_id'):
        invalidate_result_snapshot(instance.panel_id)
    else:
        panel_id = SimcBenchmarkSpec.objects.values_list('panel_id', flat=True).get(
            pk=instance.panel_spec_id,
        )
        invalidate_result_snapshot(panel_id)


def _resource_saved(sender, instance, **kwargs):
    if kwargs.get('raw'):
        return
    from django.db.models import Q
    from botend.models import SimcBenchmarkSpec
    selectors = {
        'SimcProfile': Q(profiles__profile_id=instance.pk),
        'SimcApl': (Q(apl_id=instance.pk) | Q(profiles__apl_id=instance.pk)
                    | Q(profiles__talent_string__default_apl_id=instance.pk)),
        'SimcContentTemplate': Q(template_id=instance.pk),
        'SimcTalentString': Q(profiles__talent_string_id=instance.pk),
    }
    for panel_id in SimcBenchmarkSpec.objects.filter(
            selectors[sender.__name__]).values_list('panel_id', flat=True).distinct():
        invalidate_result_snapshot(panel_id)


def register_snapshot_signals():
    from botend.models import (
        SimcApl, SimcBenchmarkCandidate, SimcBenchmarkExecution, SimcBenchmarkPanel,
        SimcBenchmarkProfile, SimcBenchmarkScenario, SimcBenchmarkSpec,
        SimcContentTemplate, SimcProfile, SimcTalentString,
    )
    handlers = [(SimcBenchmarkPanel, _panel_saved),
                (SimcBenchmarkExecution, _execution_saved)]
    handlers.extend((model, _configuration_saved) for model in (
        SimcBenchmarkCandidate, SimcBenchmarkProfile, SimcBenchmarkScenario, SimcBenchmarkSpec,
    ))
    handlers.extend((model, _resource_saved) for model in (
        SimcApl, SimcContentTemplate, SimcProfile, SimcTalentString,
    ))
    for model, handler in handlers:
        post_save.connect(handler, sender=model,
                          dispatch_uid=f'benchmark-result-snapshot-{model.__name__}', weak=False)


def snapshot_refresh_pending(panel_id):
    return bool(_load(_panel_dir(panel_id) / 'pending.json', {}))


def _index(panel_id):
    index = _load(_panel_dir(panel_id) / 'index.json')
    if not isinstance(index, dict) or index.get('schema_version') != SCHEMA_VERSION:
        return None
    return index


def read_panel_result_snapshot(panel, *, coordinate_filter=None, scenario_filter=None,
                               include_coordinate_options=True):
    """Zero SQL, zero planning, zero historical scanning, including on cache miss."""
    from botend.services.simc_benchmark_execution import _selected_plan_coordinate
    directory = _panel_dir(panel.pk)
    # Retry the pointer once if an old reader races cleanup/publication.
    for _ in range(2):
        index = _index(panel.pk)
        if index is None:
            break
        options = index['coordinate_options']
        if coordinate_filter is not None:
            selected = _selected_plan_coordinate(options, coordinate_filter)
            selected_options = [selected] if selected else []
        elif scenario_filter is not None and options:
            scenario = next((row['scenario_key'] for row in options
                             if row['scenario_key'] == scenario_filter),
                            options[0]['scenario_key'])
            selected_options = [row for row in options if row['scenario_key'] == scenario]
        else:
            selected_options = options
        result = {'panel_id': panel.pk, 'coordinates': [], **index.get('comparison', {})}
        valid = True
        for option in selected_options:
            filename = index['files'].get(_coordinate_key(option), '')
            if not re.fullmatch(r'[a-f0-9]{32}\.json', filename):
                valid = False
                break
            payload = _load(directory / 'coordinates' / filename)
            if not isinstance(payload, dict):
                valid = False
                break
            result['coordinates'].extend(payload['coordinates'])
            for key in ('comparison_rows', 'option_gain_rows'):
                if key in payload:
                    result.setdefault(key, []).extend(payload[key])
        if not valid:
            continue
        for key in ('comparison_rows', 'option_gain_rows'):
            if key in result:
                result[key].sort(key=lambda row: (-row['gain_percent'], row['spec_key'],
                                                  row['profile_key'], row['scenario_key']))
        if include_coordinate_options:
            result['coordinate_options'] = options
        result['snapshot'] = {
            'state': 'updating' if snapshot_refresh_pending(panel.pk) else 'ready',
            'generation': index['generation'], 'generated_at': index['generated_at'],
        }
        return result
    request_snapshot_refresh(panel.pk, only_if_missing=True)
    return {'panel_id': panel.pk, 'coordinates': [], 'coordinate_options': [],
            'snapshot': {'state': 'building', 'generation': None, 'generated_at': None}}


def _publication_revision(panel_id):
    """Bounded scalar watermark, including the commit-before-notification window.

    Terminal executions are immutable; count also notices history removal. This
    check is background-only and intentionally ignores independent partial runs.
    """
    from django.db.models import Count, Max
    from botend.models import SimcBenchmarkExecution
    revision = SimcBenchmarkExecution.objects.filter(
        panel_id=panel_id, completed_at__isnull=False,
    ).aggregate(count=Count('id'), last_id=Max('id'), completed_at=Max('completed_at'))
    revision['completed_at'] = (revision['completed_at'].isoformat()
                                if revision['completed_at'] else None)
    return revision


def rebuild_panel_result_snapshot(panel_id):
    """Publish all requested coordinates together; acknowledge only captured events."""
    from botend.models import SimcBenchmarkPanel
    from botend.services.simc_benchmark_execution import (
        _coordinate_option, build_execution_plan, serialize_incremental_panel_results,
    )
    directory = _panel_dir(panel_id)
    with _lock(directory / 'build.lock', blocking=False) as acquired:
        if not acquired:
            return False
        pending = _load(directory / 'pending.json', {})
        previous = _index(panel_id)
        revision = _publication_revision(panel_id)
        publication_changed = previous is None or previous.get('publication_revision') != revision
        if previous is not None and not pending and not publication_changed:
            return False
        panel = SimcBenchmarkPanel.objects.filter(pk=panel_id, is_active=True).first()
        if panel is None:
            return False
        plan = build_execution_plan(panel, lock=False)
        options = [_coordinate_option(row) for row in plan['cases']]
        keys = {_coordinate_key(option) for option in options}
        full = publication_changed or '*' in pending or keys != set(previous['files'])
        files = {} if full else dict(previous['files'])
        comparison = {} if full else dict(previous.get('comparison', {}))
        generation = uuid.uuid4().hex
        for option in options:
            key = _coordinate_key(option)
            if not full and key not in pending:
                continue
            payload = serialize_incremental_panel_results(
                panel, coordinate_filter=option, include_details=False, _prepared_plan=plan,
            )
            rows = payload.get('coordinates', [])
            if len(rows) != 1 or _coordinate_key(rows[0]) != key:
                raise ValueError('Incomplete Benchmark coordinate projection')
            for label in ('comparison_option_label', 'comparison_label'):
                if label in payload:
                    comparison[label] = payload[label]
            filename = f'{uuid.uuid4().hex}.json'
            _write(directory / 'coordinates' / filename, payload)
            files[key] = filename
        index = {
            'schema_version': SCHEMA_VERSION, 'generation': generation,
            'generated_at': timezone.now().isoformat(), 'coordinate_options': options,
            'files': files, 'comparison': comparison, 'publication_revision': revision,
        }
        # Even if on_commit has not written its event yet, a terminal transaction
        # cannot cause old/new coordinates of an atomic-targeted batch to mix.
        if _publication_revision(panel_id) != revision:
            request_snapshot_refresh(panel_id)
            return False
        # Don't publish a superseded configuration or resurrect a purged panel.
        if not SimcBenchmarkPanel.objects.filter(pk=panel_id, is_active=True).exists():
            return False
        from botend.services.simc_benchmark_purge import panel_has_active_purge
        if panel_has_active_purge(panel_id):
            return False
        with _lock(directory / 'events.lock'):
            current = _load(directory / 'pending.json', {})
            if current.get('*') != pending.get('*'):
                return False
            # Readers see either the previous complete mapping or the new one.
            _write(directory / 'index.json', index)
            remaining = {key: value for key, value in current.items()
                         if pending.get(key) != value}
            _write(directory / 'pending.json', remaining)
        # Keep current and immediately previous files. A grace period protects slow
        # readers of still older indexes, and removes interrupted-build orphans.
        retained = set(files.values()) | set((previous or {}).get('files', {}).values())
        for path in (directory / 'coordinates').glob('*.json'):
            if path.name not in retained and path.stat().st_mtime < time.time() - 300:
                path.unlink(missing_ok=True)
        return True


def refresh_pending_result_snapshots(*, batch_size=1):
    """Single global builder, outside simulation heartbeats and web threads."""
    from botend.models import SimcBenchmarkPanel
    built = []
    with _lock(snapshot_root() / 'worker.lock', blocking=False) as acquired:
        if not acquired:
            return built
        for panel_id in SimcBenchmarkPanel.objects.filter(is_active=True).order_by('id').values_list('id', flat=True):
            index = _index(panel_id)
            if index is None or index.get('publication_revision') != _publication_revision(panel_id):
                # Recover terminal commits even after a process dies before enqueue.
                request_snapshot_refresh(panel_id, only_if_missing=True)
        pending = sorted(snapshot_root().glob('*/pending.json'), key=lambda p: p.stat().st_mtime)
        attempted = 0
        for path in pending:
            if not path.parent.name.isdecimal() or not _load(path, {}):
                continue
            panel_id = int(path.parent.name)
            if not SimcBenchmarkPanel.objects.filter(pk=panel_id, is_active=True).exists():
                continue
            attempted += 1
            try:
                if rebuild_panel_result_snapshot(panel_id):
                    built.append(panel_id)
            except Exception:
                logger.exception('Benchmark snapshot rebuild failed for panel %s', panel_id)
                # A broken legacy configuration must not starve other panels.
                os.utime(path, None)
            if attempted >= batch_size:
                break
    return built


def start_result_snapshot_refresh():
    """Maintenance only kicks a separate thread; it must not block lease renewal."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        def refresh():
            close_old_connections()
            try:
                refresh_pending_result_snapshots()
            except Exception:
                logger.exception('Benchmark snapshot maintenance failed')
            finally:
                close_old_connections()
        _thread = threading.Thread(target=refresh, name='benchmark-result-snapshot', daemon=True)
        _thread.start()
