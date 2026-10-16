"""兼容旧命令名称，最终描述统一写入中央技能资料，不覆盖采集原文。"""
import json
from django.core.management.base import BaseCommand, CommandError
from botend.services.wow_spell_display import sync_spell_display


class Command(BaseCommand):
    help = '后台合成中央技能描述；默认预览，--write 写入'

    def add_arguments(self, parser):
        parser.add_argument('--branch', choices=('retail', 'ptr', 'all'), default='all')
        parser.add_argument('--write', action='store_true')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--batch-size', type=int, default=200)
        parser.add_argument('--locale', default='zhCN', help='兼容旧参数；统一生成中英文字段')
        parser.add_argument('--limit', type=int, default=0, help='兼容旧参数；完整合成必须使用 0')

    def handle(self, *args, **options):
        if options['limit'] or options['batch_size'] < 1:
            raise CommandError('完整合成要求 limit=0 且 batch-size 大于零')
        branches = ('retail', 'ptr') if options['branch'] == 'all' else (options['branch'],)
        for branch in branches:
            result = sync_spell_display(branch=branch,
                apply=options['write'] and not options['dry_run'], batch_size=options['batch_size'])
            self.stdout.write(json.dumps(result, ensure_ascii=False))
