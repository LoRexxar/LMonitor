import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.models import (
    MythicDungeonEnemy,
    MythicDungeonFloor,
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


def load_payload_seed(path):
    """从已有数据包继承已解析技能资料和已归档资源地址。"""

    path = Path(path)
    if not path.is_file():
        return {}, {}, {}, {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'无法读取已有 MDT 数据包 {path}：{exc}') from exc
    if not isinstance(payload, dict):
        raise ValueError(f'已有 MDT 数据包根节点必须是对象：{path}')

    snapshots = {}
    floor_background_urls = {}
    enemy_icon_urls = {}
    for dungeon in payload.get('dungeons') or []:
        if not isinstance(dungeon, dict):
            continue
        dungeon_key = str(dungeon.get('key') or '')
        if not dungeon_key:
            continue
        for floor in dungeon.get('floors') or []:
            if not isinstance(floor, dict):
                continue
            floor_key = str(floor.get('key') or '')
            background_url = str(floor.get('background_url') or '')
            if floor_key and background_url:
                floor_background_urls[(dungeon_key, floor_key)] = background_url
        for enemy in dungeon.get('enemies') or []:
            if not isinstance(enemy, dict):
                continue
            enemy_key = str(enemy.get('key') or '')
            enemy_icon_url = str(enemy.get('icon_url') or '')
            if enemy_key and enemy_icon_url:
                enemy_icon_urls[(dungeon_key, enemy_key)] = enemy_icon_url
            for ability in enemy.get('abilities') or []:
                if not isinstance(ability, dict):
                    continue
                try:
                    spell_id = int(ability.get('spell_id'))
                except (TypeError, ValueError):
                    continue
                current = dict(snapshots.get(spell_id) or {})
                for target_field, source_field in (
                    ('name', 'name'),
                    ('name_zh', 'name_zh'),
                    ('description', 'description_zh'),
                    ('icon_url', 'icon_url'),
                ):
                    candidate = str(ability.get(source_field) or '')
                    if candidate and not current.get(target_field):
                        current[target_field] = candidate
                snapshots[spell_id] = current

    version_data = payload.get('data_version')
    metadata = (
        dict(version_data.get('metadata') or {})
        if isinstance(version_data, dict)
        and isinstance(version_data.get('metadata'), dict)
        else {}
    )
    return snapshots, floor_background_urls, enemy_icon_urls, metadata


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
            '--no-database-metadata',
            action='store_true',
            help=(
                '仅使用已有数据包继承技能和资源资料，不读取数据库；'
                '适合离线生成发布包，必须与 --no-import 一起使用。'
            ),
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
        if options['no_database_metadata'] and not options['no_import']:
            raise CommandError('--no-database-metadata 必须与 --no-import 一起使用。')
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
            base_dir
            / 'botend'
            / 'data'
            / 'mythic_planner'
            / f'mdt_{SOURCE_TAG.replace(".", "_").replace("-", "_")}.json',
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
            (
                snapshots,
                floor_background_urls,
                enemy_icon_urls,
                seed_metadata,
            ) = load_payload_seed(output_path)
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
            existing_version = None
            if not options['no_database_metadata']:
                existing_version = MythicDungeonDataVersion.objects.filter(
                    key=payload['data_version']['key'],
                ).first()
            existing_metadata = (
                existing_version.metadata
                if existing_version and isinstance(existing_version.metadata, dict)
                else {}
            )
            if existing_version:
                floor_background_urls.update({
                    (str(row['dungeon__key']), str(row['key'])): str(
                        row['background_url'] or ''
                    )
                    for row in MythicDungeonFloor.objects.filter(
                        dungeon__data_version=existing_version,
                        is_active=True,
                    ).exclude(background_url='').values(
                        'dungeon__key',
                        'key',
                        'background_url',
                    )
                })
                enemy_icon_urls.update({
                    (str(row['dungeon__key']), str(row['key'])): str(
                        row['icon_url'] or ''
                    )
                    for row in MythicDungeonEnemy.objects.filter(
                        dungeon__data_version=existing_version,
                        is_active=True,
                    ).exclude(icon_url='').values(
                        'dungeon__key',
                        'key',
                        'icon_url',
                    )
                })
            metadata_seed = dict(seed_metadata)
            metadata_seed.update(existing_metadata)
            snapshot_metadata = (
                metadata_seed.get('spell_snapshot')
                if isinstance(metadata_seed.get('spell_snapshot'), dict)
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
            if not options['no_database_metadata']:
                snapshot_queryset = WowSpellSnapshot.objects.filter(
                    branch=snapshot_branch,
                    locale='zhCN',
                    spell_id__in=spell_ids,
                )
                if snapshot_build:
                    snapshot_queryset = snapshot_queryset.filter(
                        snapshot_build=snapshot_build,
                    )
                for row in snapshot_queryset.values(
                    'spell_id',
                    'name',
                    'name_zh',
                    'description',
                ):
                    spell_id = int(row['spell_id'])
                    current = dict(snapshots.get(spell_id) or {})
                    for field in ('name', 'name_zh', 'description'):
                        if row.get(field) and not current.get(field):
                            current[field] = row[field]
                    snapshots[spell_id] = current
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
                    'icon_url',
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
                        'icon_url': str(
                            row.get('icon_url')
                            or current.get('icon_url')
                            or ''
                        ),
                    }
                    if any(
                        preferred[field]
                        for field in ('name', 'name_zh', 'description', 'icon_url')
                    ):
                        snapshots[spell_id] = preferred
                        version_spell_hits += 1
            if snapshots or floor_background_urls or enemy_icon_urls:
                payload = build_payload(
                    source_dir,
                    version_key=str(options['version_key'] or '').strip() or None,
                    spell_snapshots=snapshots,
                    floor_background_urls=floor_background_urls,
                    enemy_icon_urls=enemy_icon_urls,
                )
            payload_metadata = dict(seed_metadata)
            payload_metadata.update(existing_metadata)
            payload_metadata.update(payload['data_version'].get('metadata') or {})
            payload['data_version']['metadata'] = payload_metadata
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
