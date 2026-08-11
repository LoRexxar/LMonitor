"""校验并幂等导入无版本 SimC APL 中英文本地化元数据。"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from botend.services.simc_apl.metadata_package import (
    find_default_metadata_package,
    import_metadata_package,
    load_metadata_package,
)


class Command(BaseCommand):
    help = '校验并幂等导入 SimC APL 英文 token、中文名称及职业/专精作用域'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file', dest='file_path', default='',
            help='数据包 JSON 路径；默认使用仓库内唯一的内置数据包',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='在事务中执行完整导入后回滚，不修改数据库',
        )
        parser.add_argument(
            '--deactivate-missing', action='store_true',
            help='显式停用数据包未包含的非人工同类归属',
        )
        parser.add_argument(
            '--refresh-all', action='store_true',
            help='全量刷新 active 目录：修正字段归属并停用包外自动归属；保留人工记录',
        )

    def handle(self, *args, **options):
        try:
            package_path = (
                Path(options['file_path']).expanduser().resolve()
                if options['file_path'] else find_default_metadata_package()
            )
            payload = load_metadata_package(package_path)
            summary = import_metadata_package(
                payload,
                dry_run=bool(options['dry_run']),
                deactivate_missing=bool(options['deactivate_missing']),
                refresh_all=bool(options['refresh_all']),
            )
        except (OSError, TypeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        prefix = '[DRY-RUN] ' if options['dry_run'] else ''
        self.stdout.write(
            f"数据包={package_path} source_revision={payload['simc_revision']} "
            f"source_build={payload['game_build']} facts={summary.package_facts} "
            f"symbols={summary.symbols_total} symbols_created={summary.symbols_created}"
        )
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}created={summary.created} updated={summary.updated} '
            f'unchanged={summary.unchanged} manual_preserved={summary.manual_preserved} '
            f'deactivated={summary.deactivated}'
        ))
