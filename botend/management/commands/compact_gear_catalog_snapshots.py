"""Convert only published on-disk catalogs; never enumerate ORM or fetch sources."""
import json
import math
import time

from django.core.management.base import BaseCommand, CommandError
from botend.services import gear_builder as gb
from botend.services import gear_catalog_snapshot as snapshots
from botend.services import gear_catalog_storage as storage


class Command(BaseCommand):
    help = '安全压缩现存装备目录；默认 dry-run，--apply 才写入；JSON 摘要和逐坐标进度'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--limit', type=int, default=640)
        parser.add_argument('--after', default='', help='按坐标目录名排序，从此名称之后续跑')
        parser.add_argument('--scope', help='仅处理此精确坐标目录名')
        parser.add_argument('--kind', choices=('all', 'equipment', 'enhancements'), default='all')
        parser.add_argument('--lock-timeout', type=float, default=0, help='每个 scope 等待共同 worker.lock 的秒数')
        parser.add_argument('--max-seconds', type=float, default=1800, help='软期限：已开始的单个 scope 校验不会被中断')

    def handle(self, *args, **options):
        if options['limit'] < 1:
            raise CommandError('--limit 必须为正整数')
        if not math.isfinite(options['lock_timeout']) or not 0 <= options['lock_timeout'] <= 300:
            raise CommandError('--lock-timeout 必须在 0..300 秒')
        if not math.isfinite(options['max_seconds']) or options['max_seconds'] <= 0:
            raise CommandError('--max-seconds 必须为有限正数')
        if options['scope'] and ('/' in options['scope'] or options['scope'] in ('.', '..')):
            raise CommandError('非法 --scope')
        root = snapshots.snapshot_root()
        storage.safe_path(root, exists=False)
        summary = {'apply': options['apply'], 'attempted': 0, 'converted': 0, 'compliant': 0,
                   'would_convert': 0, 'skipped': 0, 'failed': 0, 'busy': 0,
                   'removed_bytes': 0, 'removed_files': 0, 'remaining': 0,
                   'results': [], 'errors': [], 'last_scope': None}
        paths = sorted((p for p in root.iterdir() if p.is_dir() or p.is_symlink()), key=lambda p: p.name) if root.exists() else []
        paths = [p for p in paths if p.name > options['after'] and (not options['scope'] or p.name == options['scope'])]
        if options['scope'] and not paths:
            raise CommandError('目标 scope 不存在或被 --after 排除')
        deadline = time.monotonic() + options['max_seconds']
        for position, directory in enumerate(paths):
            if summary['attempted'] >= options['limit'] or time.monotonic() >= deadline:
                summary['remaining'] = len(paths) - position
                break
            summary['attempted'] += 1
            summary['last_scope'] = directory.name
            result = {'scope': directory.name}
            try:
                storage.safe_path(directory, exists=False)
                with storage.worker_lock(root, options['lock_timeout']) as acquired:
                    if not acquired:
                        result.update(status='busy', error='worker.lock busy; no mutation')
                        summary['busy'] += 1
                        summary['failed'] += 1
                        summary['errors'].append(result.copy())
                    elif not (directory / 'index.json').exists():
                        result.update(status='skipped', reason='no published index; building/orphan scope untouched')
                        summary['skipped'] += 1
                    else:
                        index = storage.load_small(directory / 'index.json')
                        key = snapshots.coordinate(**index['coordinate'])
                        if snapshots._directory(key) != directory or index['coordinate'] != key:
                            raise ValueError('coordinate/directory mismatch')
                        if options['kind'] != 'all' and key.get('kind', 'equipment') != options['kind']:
                            result.update(status='skipped', reason='kind filter')
                            summary['skipped'] += 1
                        else:
                            result.update(storage.compact_scope(directory, key, apply=options['apply']))
                            summary[result['status']] += 1
                            if options['apply']:
                                summary['removed_bytes'] += result.get('removed_bytes', 0)
                                summary['removed_files'] += result.get('removed_files', 0)
            except (gb.GearBuilderError, *storage.ERRORS) as exc:
                result.update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:500]}')
                summary['failed'] += 1
                summary['errors'].append(result.copy())
            summary['results'].append(result)
            self.stderr.write(json.dumps(result, ensure_ascii=False))
            self.stderr.flush()
        summary['complete'] = summary['failed'] == 0 and summary['remaining'] == 0
        self.stdout.write(json.dumps(summary, ensure_ascii=False))
        if summary['failed']:
            raise CommandError(f"{summary['failed']} 个坐标失败/锁忙；详见 JSON 摘要，未宣称全量成功")
