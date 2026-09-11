"""同步中文冒险手册。"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from botend.services.journal_service import sync_journal


class Command(BaseCommand):
    help = '完整同步正式服冒险手册，并在校验通过后原子发布'

    def add_arguments(self, parser):
        parser.add_argument('--build', default='', help='完整版本号；默认自动选择正式服')
        parser.add_argument('--directory', default=None, help='Wago 表缓存根目录')
        parser.add_argument('--offline', action='store_true', help='仅重放已下载的表和补充数据')
        parser.add_argument('--refresh', action='store_true', help='重新下载本版本表以纳入热修')
        parser.add_argument('--no-fallback', action='store_true', help='仅使用当前版本 Wago 中文表')
        parser.add_argument('--report', help='将完整核对报告写入指定 JSON 文件')

    def handle(self, *args, **options):
        def progress(message):
            self.stdout.write(message)
            self.stdout.flush()
        try:
            run = sync_journal(build=options['build'], directory=options['directory'],
                               offline=options['offline'], refresh=options['refresh'],
                               fallback=not options['no_fallback'], progress=progress)
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        if options['report']:
            path = Path(options['report'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'build': run.build, **run.report}, ensure_ascii=False, indent=2), encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'冒险手册发布成功：版本 {run.build}，'
                          f'{run.report["instances"]} 个副本，{run.report["encounters"]} 个首领，'
                          f'{run.report["sections"]} 个技能条目，{run.report["loot"]} 条掉落'))
