"""离线更新基希克斯装备、手册与职业聚合展示使用的中文资料。"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from botend.services.kithix_localization import import_localization


class Command(BaseCommand):
    help = '更新基希克斯中文并归入本赛季，默认只预览；--apply 写入并备份'

    def add_arguments(self, parser):
        parser.add_argument('--artifact', default=str(Path(settings.BASE_DIR) / 'botend/data/kithix_localization_12_1_5.json'), help='离线中文制品路径')
        parser.add_argument('--apply', action='store_true', help='实际更新数据库')
        parser.add_argument('--backup', help='新备份文件路径，不允许覆盖已有备份')

    def handle(self, *args, **options):
        backup = options['backup'] or str(Path(settings.BASE_DIR) / 'media/import-backups' /
                                         f'kithix-zh-{timezone.now():%Y%m%d-%H%M%S-%f}.json')
        try:
            report = import_localization(options['artifact'], apply=options['apply'], backup_path=backup)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        if not options['apply']:
            self.stdout.write('仅预览，未写入数据库；执行更新请添加 --apply。')
