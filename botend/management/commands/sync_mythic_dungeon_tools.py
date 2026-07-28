import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.models import (
    MythicDungeonDataVersion,
    MythicDungeonSpell,
    WowSpellSnapshot,
)
from botend.mythic_planner.importer import import_mythic_dungeon_payload
from botend.mythic_planner.mdt_converter import (
    SOURCE_TAG,
    build_payload,
    compose_maps,
    compose_ui_assets,
    write_payload,
)


class Command(BaseCommand):
    help = '将固定版本 MythicDungeonTools Lua 与地图切片转换并同步到路线规划器。'

    def add_arguments(self, parser):
        parser.add_argument(
            '--source-dir',
            default='',
            help='MythicDungeonTools 源码目录；默认使用项目内随附的固定版本快照。',
        )
        parser.add_argument(
            '--output',
            default='',
            help='生成的 JSON 数据包路径；默认写入 botend/data/mythic_planner。',
        )
        parser.add_argument(
            '--static-map-root',
            default='',
            help='WebP 地图输出目录；默认写入 static/portal/mythic_planner/vendor。',
        )
        parser.add_argument(
            '--version-key',
            default='',
            help='覆盖生成数据包的版本键，适合后续上游快照更新。',
        )
        parser.add_argument(
            '--activate',
            action='store_true',
            help='导入后将该数据版本设为当前生效版本。',
        )
        parser.add_argument(
            '--replace',
            action='store_true',
            help='将同版本中已从新快照消失的实体标记为停用。',
        )
        parser.add_argument(
            '--no-import',
            action='store_true',
            help='只生成数据包和地图，不写入数据库。',
        )
        parser.add_argument(
            '--no-compose-maps',
            action='store_true',
            help='不重新合成地图；适合仅更新 Lua 数据。',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='完整转换并校验数据库导入，但回滚数据库写入。',
        )

    def handle(self, *args, **options):
        base_dir = Path(settings.BASE_DIR).resolve()
        source_dir = self._resolve_path(
            options['source_dir'],
            base_dir
            / 'botend'
            / 'data'
            / 'mythic_planner'
            / 'vendor'
            / f'mythic-dungeon-tools-{SOURCE_TAG}',
        )
        output_path = self._resolve_path(
            options['output'],
            base_dir / 'botend' / 'data' / 'mythic_planner' / 'mdt_6_2_0_alpha3.json',
        )
        static_map_root = self._resolve_path(
            options['static_map_root'],
            base_dir
            / 'static'
            / 'portal'
            / 'mythic_planner'
            / 'vendor'
            / f'mdt-{SOURCE_TAG}'
            / 'maps',
        )
        self._validate_source(source_dir)

        try:
            payload = build_payload(
                source_dir,
                version_key=str(options['version_key'] or '').strip() or None,
            )
            spell_ids = {
                ability['spell_id']
                for dungeon in payload['dungeons']
                for enemy in dungeon['enemies']
                for ability in enemy['abilities']
            }
            existing_version = MythicDungeonDataVersion.objects.filter(
                key=payload['data_version']['key'],
            ).first()
            existing_metadata = (
                existing_version.metadata
                if existing_version and isinstance(existing_version.metadata, dict)
                else {}
            )
            snapshot_metadata = (
                existing_metadata.get('spell_snapshot')
                if isinstance(existing_metadata.get('spell_snapshot'), dict)
                else {}
            )
            supplement_metadata = payload['data_version']['metadata'].get(
                'ability_supplement',
            )
            supplement_target = (
                supplement_metadata.get('target')
                if isinstance(supplement_metadata, dict)
                and isinstance(supplement_metadata.get('target'), dict)
                else {}
            )
            snapshot_branch = str(
                snapshot_metadata.get('source_branch')
                or supplement_target.get('branch')
                or 'wow'
            ).strip()
            snapshot_build = str(
                snapshot_metadata.get('snapshot_build')
                or supplement_target.get('game_build')
                or ''
            ).strip()
            snapshot_queryset = WowSpellSnapshot.objects.filter(
                branch=snapshot_branch,
                locale='zhCN',
                spell_id__in=spell_ids,
            )
            if snapshot_build:
                snapshot_queryset = snapshot_queryset.filter(
                    snapshot_build=snapshot_build,
                )
            snapshots = {
                int(row['spell_id']): row
                for row in snapshot_queryset.values(
                    'spell_id',
                    'name',
                    'name_zh',
                    'description',
                )
            }
            version_spell_hits = 0
            if existing_version:
                for row in MythicDungeonSpell.objects.filter(
                    data_version=existing_version,
                    spell_id__in=spell_ids,
                    is_active=True,
                ).values(
                    'spell_id',
                    'name',
                    'name_zh',
                    'description_zh',
                ):
                    spell_id = int(row['spell_id'])
                    current = dict(snapshots.get(spell_id) or {})
                    preferred = {
                        'spell_id': spell_id,
                        'name': str(row.get('name') or current.get('name') or ''),
                        'name_zh': str(
                            row.get('name_zh')
                            or current.get('name_zh')
                            or ''
                        ),
                        'description': str(
                            row.get('description_zh')
                            or current.get('description')
                            or ''
                        ),
                    }
                    if any(
                        preferred[field]
                        for field in ('name', 'name_zh', 'description')
                    ):
                        snapshots[spell_id] = preferred
                        version_spell_hits += 1
            if snapshots:
                payload = build_payload(
                    source_dir,
                    version_key=str(options['version_key'] or '').strip() or None,
                    spell_snapshots=snapshots,
                )
            write_payload(payload, output_path)

            map_manifest = []
            ui_asset_manifest = []
            if not options['no_compose_maps']:
                map_manifest = compose_maps(source_dir, static_map_root)
                ui_asset_manifest = compose_ui_assets(
                    source_dir,
                    static_map_root.parent / 'assets',
                )
                manifest_path = static_map_root.parent / 'maps-manifest.json'
                manifest_path.write_text(
                    json.dumps(
                        {
                            'maps': map_manifest,
                            'ui_assets': ui_asset_manifest,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ) + '\n',
                    encoding='utf-8',
                )
        except (OSError, RuntimeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        imported = None
        if not options['no_import']:
            raw = output_path.read_bytes()
            activate = bool(options['activate']) or not MythicDungeonDataVersion.objects.filter(
                is_active=True
            ).exists()
            try:
                with transaction.atomic():
                    imported = import_mythic_dungeon_payload(
                        payload,
                        activate=activate,
                        replace=bool(options['replace']),
                        source_bytes=raw,
                    )
                    if options['dry_run']:
                        transaction.set_rollback(True)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc

        dungeon_count = len(payload['dungeons'])
        enemy_count = sum(len(dungeon['enemies']) for dungeon in payload['dungeons'])
        spawn_count = sum(
            len(enemy['spawns'])
            for dungeon in payload['dungeons']
            for enemy in dungeon['enemies']
        )
        ability_count = sum(
            len(enemy['abilities'])
            for dungeon in payload['dungeons']
            for enemy in dungeon['enemies']
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'MDT 转换完成：地下城 {dungeon_count}，怪物 {enemy_count}，'
                f'刷新点 {spawn_count}，技能 {ability_count}，'
                f'本地法术快照 {snapshot_branch}/{snapshot_build or "未限定"} '
                f'命中 {len(snapshots)}（同版本最终资料 {version_spell_hits}），'
                f'地图 {len(map_manifest)}，'
                f'界面贴图 {len(ui_asset_manifest)}。'
            )
        )
        self.stdout.write(f'数据包：{output_path}')
        if map_manifest:
            self.stdout.write(f'地图目录：{static_map_root}')
        if imported:
            mode = '数据库校验已回滚' if options['dry_run'] else '数据库同步完成'
            self.stdout.write(
                f"{mode}：版本 {imported['version_key']}，"
                f"新增 {imported['created']}，更新 {imported['updated']}，"
                f"当前生效={'是' if imported['active'] else '否'}。"
            )

    @staticmethod
    def _resolve_path(configured, default):
        path = Path(configured).expanduser() if configured else Path(default)
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / path
        return path.resolve()

    @staticmethod
    def _validate_source(source_dir):
        required = (
            source_dir / 'LICENSE',
            source_dir / 'Locales' / 'enUS.lua',
            source_dir / 'Locales' / 'zhCN.lua',
            source_dir / 'Midnight' / 'load_midnight.xml',
            source_dir / 'Midnight' / 'Textures',
            source_dir / 'Textures' / 'MDTFull.tga',
        )
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise CommandError('MDT 源快照不完整：' + '；'.join(missing))
