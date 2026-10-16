"""旧描述补采入口统一使用已发布分支的 DB2 来源，不再抓取硬编码旧构建。"""
from pathlib import Path
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from botend.wow.talents.versioning import TalentVersionResolver


class Command(BaseCommand):
    help = '从当前分支来源目录补齐天赋原文并发布中央技能描述'

    def add_arguments(self, parser):
        parser.add_argument('--branch', choices=('retail', 'ptr'), default='retail')
        parser.add_argument('--dump-dir', default='')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--batch-size', type=int, default=200)
        parser.add_argument('--sleep', type=float, default=0)
        parser.add_argument('--skip-crawl', action='store_true', help='兼容旧参数；本命令只使用当前已采集来源')

    def handle(self, *args, **options):
        version = TalentVersionResolver.resolve(version_key=options['branch'])
        directory = options['dump_dir'] or version.source_dir
        if not directory or not Path(directory).is_dir():
            raise CommandError('当前分支尚无完整 DB2 来源，请先执行统一数据采集')
        call_command('repair_talent_metadata_from_db2', dump_dir=directory, version_key=version.key,
                     dry_run=options['dry_run'], skip_snapshot=True, stdout=self.stdout)
