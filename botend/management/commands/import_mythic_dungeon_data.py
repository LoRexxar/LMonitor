import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.models import MythicDungeonDataVersion
from botend.mythic_planner.importer import import_mythic_dungeon_payload


class Command(BaseCommand):
    help = '初始化或更新大秘境路线规划器数据。'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            dest='file_path',
            default='',
            help='JSON 数据包路径；不传时使用项目内置 MDT 6.2.0-alpha3 数据。',
        )
        parser.add_argument(
            '--demo',
            action='store_true',
            help='显式使用原创演示数据，仅用于开发测试。',
        )
        parser.add_argument(
            '--activate',
            action='store_true',
            help='导入后将该数据版本设为当前生效版本。',
        )
        parser.add_argument(
            '--replace',
            action='store_true',
            help='将数据包中缺失的同版本实体标记为停用，不做物理删除。',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='完整校验和执行导入逻辑，但回滚数据库写入。',
        )

    def handle(self, *args, **options):
        if options['file_path'] and options['demo']:
            raise CommandError('--file 与 --demo 不能同时使用。')
        if options['file_path']:
            file_path = Path(options['file_path']).expanduser()
            if not file_path.is_absolute():
                file_path = Path(settings.BASE_DIR) / file_path
        elif options['demo']:
            file_path = (
                Path(settings.BASE_DIR)
                / 'botend'
                / 'data'
                / 'mythic_planner'
                / 'demo_v1.json'
            )
        else:
            file_path = (
                Path(settings.BASE_DIR)
                / 'botend'
                / 'data'
                / 'mythic_planner'
                / 'mdt_6_2_0_alpha3.json'
            )
        file_path = file_path.resolve()
        if not file_path.is_file():
            raise CommandError(f'找不到数据包：{file_path}')

        try:
            raw = file_path.read_bytes()
            payload = json.loads(raw.decode('utf-8-sig'))
        except UnicodeDecodeError as exc:
            raise CommandError('数据包必须使用 UTF-8 编码。') from exc
        except json.JSONDecodeError as exc:
            raise CommandError(f'数据包 JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列。') from exc

        activate = bool(options['activate']) or not MythicDungeonDataVersion.objects.filter(is_active=True).exists()
        try:
            with transaction.atomic():
                result = import_mythic_dungeon_payload(
                    payload,
                    activate=activate,
                    replace=bool(options['replace']),
                    source_bytes=raw,
                )
                if options['dry_run']:
                    transaction.set_rollback(True)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        mode = '校验完成并已回滚' if options['dry_run'] else '导入完成'
        self.stdout.write(
            self.style.SUCCESS(
                f"{mode}：版本 {result['version_key']}，"
                f"地下城 {result['dungeons']} 个，"
                f"新增 {result['created']} 项，更新 {result['updated']} 项，"
                f"当前生效={'是' if result['active'] else '否'}。"
            )
        )
        if options['demo']:
            self.stdout.write('已显式使用项目内置原创演示数据。')
        elif not options['file_path']:
            self.stdout.write(
                '已使用项目内置 MythicDungeonTools 6.2.0-alpha3 转换数据；'
                '可在 Dashboard 中修改，或运行 sync_mythic_dungeon_tools 更新。'
            )
