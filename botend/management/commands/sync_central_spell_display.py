"""预览或合并当前分支的技能最终展示资料。"""
import json

from django.core.management.base import BaseCommand, CommandError

from botend.services.wow_spell_display import sync_spell_display


class Command(BaseCommand):
    help = '中央技能描述合并：默认仅预览，--apply 写入，重复执行不新增相同资料'

    def add_arguments(self, parser):
        parser.add_argument('--branch', choices=('retail', 'ptr', 'all'), default='all')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--batch-size', type=int, default=200)

    def handle(self, *args, **options):
        if options['batch_size'] < 1:
            raise CommandError('批次大小必须大于零')
        branches = ('retail', 'ptr') if options['branch'] == 'all' else (options['branch'],)
        for branch in branches:
            report = sync_spell_display(branch=branch, apply=options['apply'], batch_size=options['batch_size'])
            self.stdout.write(('已写入：' if options['apply'] else '仅预览：') + json.dumps(report, ensure_ascii=False))
