"""兼容旧 DB2 迁移入口，使用当前分支中央节点导入，不再创建独立英雄树表。"""
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '从 DB2 合并当前分支中央天赋结构与技能展示'

    def add_arguments(self, parser):
        parser.add_argument('--dump-dir', default='')
        parser.add_argument('--branch', choices=('retail', 'ptr'), default='retail')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        call_command('backfill_db2_talent_nodes', dump_dir=options['dump_dir'],
                     version_key=options['branch'], dry_run=options['dry_run'], stdout=self.stdout)
