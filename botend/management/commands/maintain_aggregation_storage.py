"""显式检查与分批回收聚合存储，不依赖后台开关。"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = '检查聚合中间缓存和历史发布，默认只预演；--apply 才回收。'

    def add_arguments(self, parser):
        parser.add_argument('--kind', choices=['work', 'published'], required=True)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--after', default='')
        parser.add_argument('--scope-limit', type=int, default=8)
        parser.add_argument('--passes', type=int, default=1)
        parser.add_argument('--repair-disabled-scope', default='',
                            help='聚合根目录内的赛季/职业/专精；完整校验后归档禁用标记')

    def handle(self, *args, **options):
        if not 1 <= options['scope_limit'] <= 40 or not 1 <= options['passes'] <= 32:
            raise CommandError('范围数应为1至40，轮数应为1至32')
        after = options['after']
        for _ in range(options['passes']):
            if options['repair_disabled_scope']:
                from botend.services.spec_stats_retention import repair_disabled_scope, _safe_path
                root = (Path(getattr(settings, 'MEDIA_ROOT', '') or 'media') / 'aggregated').resolve()
                target = root / options['repair_disabled_scope']
                _safe_path(target)
                if not target.resolve().is_relative_to(root) or len(target.relative_to(root).parts) != 3:
                    raise CommandError('修复范围必须是聚合根目录下的赛季/职业/专精')
                result = repair_disabled_scope(target, dry_run=not options['apply'])
                self.stdout.write(json.dumps(result, ensure_ascii=False))
                if result['status'] == 'refused':
                    raise CommandError('完整校验失败，禁用标记保留')
                if result['status'] != 'paused':
                    break
                continue
            if options['kind'] == 'work':
                from botend.services.spec_work_retention import maintain_work_cache
                result = maintain_work_cache(apply=options['apply'], after=after)
            else:
                from botend.services.spec_stats_retention import maintain_scopes
                root = Path(getattr(settings, 'MEDIA_ROOT', '') or 'media') / 'aggregated'
                result = maintain_scopes(root, after=after,
                    scope_limit=options['scope_limit'], dry_run=not options['apply'])
            self.stdout.write(json.dumps(result, ensure_ascii=False))
            after = result['next_cursor'] if result['has_more'] else ''
            if result.get('error'):
                raise CommandError('回收检查失败，详情见输出')
            if options['kind'] == 'published' and any(
                    row['status'] in ('disabled', 'refused', 'error') for row in result['results']):
                raise CommandError('存在被禁用或拒绝的范围；未跳过校验或自动清除禁用标记')
            if not options['apply'] and not result['has_more']:
                break
