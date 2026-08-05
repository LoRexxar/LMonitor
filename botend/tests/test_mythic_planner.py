import copy
import json
import re
import tempfile
from pathlib import Path
from unittest import mock

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from botend.models import (
    MythicDungeon,
    MythicDungeonAbility,
    MythicDungeonDataVersion,
    MythicDungeonEnemy,
    MythicDungeonFloor,
    MythicDungeonPoi,
    MythicDungeonRoute,
    MythicDungeonRouteShare,
    MythicDungeonSelectionGroup,
    MythicDungeonSelectionMembership,
    MythicDungeonSpell,
    MythicDungeonSpawn,
    MythicPlannerConfig,
)
from botend.mythic_planner.importer import import_mythic_dungeon_payload
from botend.mythic_planner.icon_assets import (
    build_wowhead_icon_url,
    normalize_wowhead_icon_slug,
)
from botend.mythic_planner.mdt_converter import (
    LuaParseError,
    LuaValueParser,
    build_payload,
)
from botend.mythic_planner.services import (
    decode_share_code,
    encode_share_code,
    get_active_dungeon,
    serialize_ability,
    serialize_catalog,
    validate_route_payload,
)
from botend.mythic_planner.spell_tooltips import (
    QUALITY_EXACT_RENDERED,
    QUALITY_MANUAL_OVERRIDE,
    QUALITY_MECHANIC_ONLY,
    QUALITY_RENDERED_EXTERNAL,
    SOURCE_MANUAL,
    SOURCE_WAGO_DB2,
    SOURCE_WOW_CLIENT,
    SOURCE_WOWHEAD_TOOLTIP,
    SOURCE_WOWHEAD_TOOLTIP_REFERENCE,
    build_manifest_core,
    manifest_hash,
)
from botend.management.commands.sync_mythic_dungeon_spells import (
    Command as SyncMythicDungeonSpellsCommand,
)
from botend.management.commands.sync_mythic_dungeon_assets import (
    AssetUnavailableError,
    Command as SyncMythicDungeonAssetsCommand,
)
from botend.management.commands.sync_mythic_dungeon_tools import (
    load_payload_seed,
)
from botend.wow.spell_text import SpellTextResolver


def demo_payload():
    path = (
        Path(settings.BASE_DIR)
        / 'botend'
        / 'mythic_planner'
        / 'fixtures'
        / 'demo_v1.json'
    )
    return json.loads(path.read_text(encoding='utf-8'))


class MythicPlannerImportTests(TestCase):
    def test_builtin_alpha5_package_imports_complete_dataset(self):
        call_command(
            'import_mythic_dungeon_data',
            activate=True,
            replace=True,
            verbosity=0,
        )

        version = MythicDungeonDataVersion.objects.get(
            key='mdt-6-2-0-alpha5',
            is_active=True,
        )
        self.assertEqual(version.metadata['source_tag'], '6.2.0-alpha5')
        self.assertEqual(version.dungeons.filter(is_active=True).count(), 16)
        self.assertEqual(
            MythicDungeonEnemy.objects.filter(
                dungeon__data_version=version,
                is_active=True,
            ).count(),
            467,
        )
        self.assertEqual(
            MythicDungeonSpawn.objects.filter(
                enemy__dungeon__data_version=version,
                is_active=True,
            ).count(),
            3012,
        )
        self.assertEqual(
            MythicDungeonAbility.objects.filter(
                enemy__dungeon__data_version=version,
                is_active=True,
            ).count(),
            1648,
        )
        self.assertFalse(
            MythicDungeonAbility.objects.filter(
                enemy__dungeon__data_version=version,
                name__regex=r'^Spell #[0-9]+$',
            ).exists()
        )

    def test_builtin_command_initializes_and_is_idempotent(self):
        call_command('import_mythic_dungeon_data', demo=True, verbosity=0)
        counts = {
            'versions': MythicDungeonDataVersion.objects.count(),
            'dungeons': MythicDungeon.objects.count(),
            'floors': MythicDungeonFloor.objects.count(),
            'enemies': MythicDungeonEnemy.objects.count(),
            'spells': MythicDungeonSpell.objects.count(),
            'abilities': MythicDungeonAbility.objects.count(),
            'spawns': MythicDungeonSpawn.objects.count(),
            'pois': MythicDungeonPoi.objects.count(),
            'configs': MythicPlannerConfig.objects.count(),
        }

        call_command('import_mythic_dungeon_data', demo=True, verbosity=0)

        self.assertEqual(MythicDungeonDataVersion.objects.filter(is_active=True).count(), 1)
        self.assertEqual(MythicDungeonDataVersion.objects.get().key, 'lmonitor-demo-1')
        self.assertEqual(MythicDungeon.objects.count(), counts['dungeons'])
        self.assertEqual(MythicDungeonFloor.objects.count(), counts['floors'])
        self.assertEqual(MythicDungeonEnemy.objects.count(), counts['enemies'])
        self.assertEqual(MythicDungeonSpell.objects.count(), counts['spells'])
        self.assertEqual(MythicDungeonAbility.objects.count(), counts['abilities'])
        self.assertEqual(MythicDungeonSpawn.objects.count(), counts['spawns'])
        self.assertEqual(MythicDungeonPoi.objects.count(), counts['pois'])
        self.assertEqual(MythicPlannerConfig.objects.count(), counts['configs'])
        self.assertEqual(
            MythicDungeonSpell.objects.count(),
            MythicDungeonAbility.objects.values('spell_id').distinct().count(),
        )
        self.assertFalse(
            MythicDungeonAbility.objects.filter(spell_record__isnull=True).exists()
        )

    def test_shared_spell_snapshot_replaces_import_placeholders(self):
        import_mythic_dungeon_payload(demo_payload(), activate=True)
        ability = MythicDungeonAbility.objects.select_related('spell_record').first()
        ability.name = f'Spell #{ability.spell_id}'
        ability.name_zh = f'技能 #{ability.spell_id}'
        ability.description = 'Deals x damage for a while.'
        ability.description_zh = '在一段时间内造成x点伤害。'
        ability.icon_url = ''
        ability.save()
        spell = ability.spell_record
        spell.name = 'Resolved name'
        spell.name_zh = '已解析技能'
        spell.description_zh = '来自公共技能资料表的完整说明。'
        spell.icon_url = 'https://example.com/icon.jpg'
        spell.metadata = {
            'description_source': SOURCE_WOW_CLIENT,
            'description_quality': QUALITY_EXACT_RENDERED,
            'client_version': '12.1.0',
            'client_build': '68914',
            'client_locale': 'zhCN',
            'difficulty_id': 8,
        }
        spell.save()

        payload = serialize_ability(
            MythicDungeonAbility.objects.select_related('spell_record').get(pk=ability.pk)
        )

        self.assertEqual(payload['display_name'], '已解析技能')
        self.assertEqual(payload['description_zh'], '来自公共技能资料表的完整说明。')
        self.assertEqual(payload['icon_url'], 'https://example.com/icon.jpg')
        self.assertEqual(
            payload['metadata']['spell_snapshot']['description_quality'],
            QUALITY_EXACT_RENDERED,
        )
        self.assertEqual(
            payload['metadata']['spell_snapshot']['difficulty_id'],
            8,
        )

    def test_explicit_relation_override_beats_shared_spell_snapshot(self):
        import_mythic_dungeon_payload(demo_payload(), activate=True)
        ability = MythicDungeonAbility.objects.select_related('spell_record').first()
        ability.description_zh = '关系级人工说明。'
        ability.metadata = {'manual_override_fields': ['description_zh']}
        ability.save()
        ability.spell_record.description_zh = '公共技能说明。'
        ability.spell_record.save()

        payload = serialize_ability(
            MythicDungeonAbility.objects.select_related('spell_record').get(pk=ability.pk)
        )

        self.assertEqual(payload['description_zh'], '关系级人工说明。')

    def test_same_version_updates_content_and_replace_soft_disables_missing_rows(self):
        payload = demo_payload()
        import_mythic_dungeon_payload(payload, activate=True)
        updated = copy.deepcopy(payload)
        updated['dungeons'][0]['name_zh'] = '暮影宝库（更新）'
        updated['dungeons'][0]['enemies'][0]['enemy_forces'] = 6
        removed_spawn_key = updated['dungeons'][0]['enemies'][0]['spawns'].pop()['key']

        result = import_mythic_dungeon_payload(updated, activate=True, replace=True)

        dungeon = MythicDungeon.objects.get(
            data_version__key='lmonitor-demo-1',
            key='gloamvault',
        )
        enemy = dungeon.enemies.get(key='vault-guardian')
        removed = enemy.spawns.get(key=removed_spawn_key)
        self.assertEqual(result['version_key'], 'lmonitor-demo-1')
        self.assertEqual(dungeon.name_zh, '暮影宝库（更新）')
        self.assertEqual(enemy.enemy_forces, 6)
        self.assertFalse(removed.is_active)

    def test_upgrade_from_version_reuses_relations_and_preserves_manual_edits(self):
        payload = demo_payload()
        import_mythic_dungeon_payload(payload, activate=True)
        version = MythicDungeonDataVersion.objects.get(key='lmonitor-demo-1')
        source_dungeon = payload['dungeons'][0]
        source_enemy = source_dungeon['enemies'][0]
        source_spawn = source_enemy['spawns'][0]
        dungeon = version.dungeons.get(key=source_dungeon['key'])
        spawn = MythicDungeonSpawn.objects.select_related('floor').get(
            enemy__dungeon=dungeon,
            enemy__key=source_enemy['key'],
            key=source_spawn['key'],
        )
        imported_position = {
            'floor_key': spawn.floor.key,
            'x': spawn.x,
            'y': spawn.y,
        }
        imported_group_key = spawn.group_key
        spawn.x += 1
        spawn.y += 1
        spawn.group_key = 'manual-group-1'
        spawn.metadata = {
            **(spawn.metadata or {}),
            'manual_position_override': True,
            'manual_position_updated_by_user_id': 1,
            'imported_position': imported_position,
            'manual_group_override': True,
            'manual_group_updated_by_user_id': 1,
            'imported_group_key': imported_group_key,
        }
        spawn.save()
        manual_group = MythicDungeonSelectionGroup.objects.create(
            data_version=version,
            key='manual-favorites',
            name='Manual Favorites',
            name_zh='人工收藏',
            order=99,
            metadata={'source': 'manual'},
        )
        route = MythicDungeonRoute.objects.create(
            dungeon=dungeon,
            name='升级保留路线',
            route_data={'version': 1, 'dungeon_key': dungeon.key},
        )
        share = MythicDungeonRouteShare.objects.create(
            dungeon=dungeon,
            name='升级保留短链',
            route_data={'version': 1, 'dungeon_key': dungeon.key},
            content_hash='a' * 64,
        )

        upgraded = copy.deepcopy(payload)
        upgraded['data_version']['key'] = 'lmonitor-demo-2'
        upgraded['data_version']['label'] = 'LMonitor Demo 2'
        upgraded_spawn = upgraded['dungeons'][0]['enemies'][0]['spawns'][0]
        upgraded_spawn['x'] += 5
        upgraded_spawn['y'] += 5
        upgraded_spawn['group_key'] = 'group-upstream-2'
        with tempfile.TemporaryDirectory() as temporary_directory:
            package_path = Path(temporary_directory) / 'upgrade.json'
            package_path.write_text(
                json.dumps(upgraded, ensure_ascii=False),
                encoding='utf-8',
            )
            call_command(
                'import_mythic_dungeon_data',
                file_path=str(package_path),
                upgrade_from_version='lmonitor-demo-1',
                activate=True,
                replace=True,
                verbosity=0,
            )
            call_command(
                'import_mythic_dungeon_data',
                file_path=str(package_path),
                upgrade_from_version='lmonitor-demo-1',
                activate=True,
                replace=True,
                verbosity=0,
            )

        version.refresh_from_db()
        dungeon.refresh_from_db()
        spawn.refresh_from_db()
        route.refresh_from_db()
        share.refresh_from_db()
        manual_group.refresh_from_db()
        self.assertEqual(version.key, 'lmonitor-demo-2')
        self.assertFalse(
            MythicDungeonDataVersion.objects.filter(
                key='lmonitor-demo-1',
            ).exists()
        )
        self.assertEqual(dungeon.data_version_id, version.id)
        self.assertEqual(route.dungeon_id, dungeon.id)
        self.assertEqual(share.dungeon_id, dungeon.id)
        self.assertEqual(manual_group.data_version_id, version.id)
        self.assertEqual(spawn.group_key, 'manual-group-1')
        self.assertEqual(spawn.x, imported_position['x'] + 1)
        self.assertEqual(spawn.y, imported_position['y'] + 1)
        self.assertTrue(spawn.metadata['manual_position_override'])
        self.assertTrue(spawn.metadata['manual_group_override'])
        self.assertEqual(
            spawn.metadata['imported_group_key'],
            'group-upstream-2',
        )

    def test_upgrade_from_version_dry_run_rolls_back_version_key(self):
        payload = demo_payload()
        import_mythic_dungeon_payload(payload, activate=True)
        upgraded = copy.deepcopy(payload)
        upgraded['data_version']['key'] = 'lmonitor-demo-2'
        with tempfile.TemporaryDirectory() as temporary_directory:
            package_path = Path(temporary_directory) / 'upgrade.json'
            package_path.write_text(
                json.dumps(upgraded),
                encoding='utf-8',
            )
            call_command(
                'import_mythic_dungeon_data',
                file_path=str(package_path),
                upgrade_from_version='lmonitor-demo-1',
                activate=True,
                replace=True,
                dry_run=True,
                verbosity=0,
            )

        self.assertTrue(
            MythicDungeonDataVersion.objects.filter(
                key='lmonitor-demo-1',
                is_active=True,
            ).exists()
        )
        self.assertFalse(
            MythicDungeonDataVersion.objects.filter(
                key='lmonitor-demo-2',
            ).exists()
        )

    def test_reimport_preserves_manual_spell_and_ability_descriptions(self):
        payload = demo_payload()
        import_mythic_dungeon_payload(payload, activate=True)
        ability = MythicDungeonAbility.objects.select_related(
            'spell_record',
        ).first()
        ability.description_zh = '关系级人工说明。'
        ability.metadata = {
            **(ability.metadata or {}),
            'manual_override_fields': ['description_zh'],
            'manual_updated_by_user_id': 1,
        }
        ability.save()
        spell = ability.spell_record
        spell.description_zh = '技能库人工说明。'
        spell.metadata = {
            **(spell.metadata or {}),
            'description_source': SOURCE_MANUAL,
            'description_quality': QUALITY_MANUAL_OVERRIDE,
        }
        spell.save()

        updated = copy.deepcopy(payload)
        updated_ability = updated['dungeons'][0]['enemies'][0]['abilities'][0]
        updated_ability['description_zh'] = '上游新说明。'
        import_mythic_dungeon_payload(updated, activate=True, replace=True)

        ability.refresh_from_db()
        spell.refresh_from_db()
        self.assertEqual(ability.description_zh, '关系级人工说明。')
        self.assertEqual(spell.description_zh, '技能库人工说明。')

    def test_selection_groups_are_independent_idempotent_resources(self):
        payload = demo_payload()
        payload['selection_groups'] = [{
            'key': 'demo-season-3',
            'name': 'Demo Season 3',
            'name_zh': '演示第三赛季',
            'order': 3,
            'dungeon_keys': ['emberworks', 'gloamvault'],
        }]
        import_mythic_dungeon_payload(payload, activate=True, replace=True)
        version = MythicDungeonDataVersion.objects.get(key='lmonitor-demo-1')
        manual_group = MythicDungeonSelectionGroup.objects.create(
            data_version=version,
            key='manual-favorites',
            name='Manual Favorites',
            name_zh='人工收藏',
            order=9,
        )

        result = import_mythic_dungeon_payload(
            payload,
            activate=True,
            replace=True,
        )

        group = MythicDungeonSelectionGroup.objects.get(
            data_version=version,
            key='demo-season-3',
        )
        self.assertEqual(result['selection_groups'], 1)
        self.assertEqual(
            list(
                group.memberships.filter(is_active=True).values_list(
                    'dungeon__key',
                    flat=True,
                )
            ),
            ['emberworks', 'gloamvault'],
        )
        self.assertEqual(
            MythicDungeonSelectionMembership.objects.filter(
                selection_group=group,
            ).count(),
            2,
        )
        manual_group.refresh_from_db()
        self.assertTrue(manual_group.is_active)
        catalog = serialize_catalog()
        self.assertIn(
            'demo-season-3',
            [row['key'] for row in catalog['selection_groups']],
        )
        emberworks = next(
            row for row in catalog['dungeons'] if row['key'] == 'emberworks'
        )
        self.assertEqual(
            emberworks['selection_groups'][0]['key'],
            'demo-season-3',
        )

    def test_dry_run_command_rolls_back_all_writes(self):
        call_command('import_mythic_dungeon_data', demo=True, dry_run=True, verbosity=0)

        self.assertFalse(MythicDungeonDataVersion.objects.exists())
        self.assertFalse(MythicDungeon.objects.exists())

    def test_spell_sync_rejects_unknown_explicit_data_version(self):
        import_mythic_dungeon_payload(demo_payload(), activate=True)

        with self.assertRaisesMessage(CommandError, '找不到 MDT 数据版本'):
            SyncMythicDungeonSpellsCommand._resolve_version('not-exists')

    def test_demo_enemy_forces_match_each_dungeon_target(self):
        import_mythic_dungeon_payload(demo_payload(), activate=True)

        for dungeon in MythicDungeon.objects.filter(is_active=True):
            imported_forces = sum(
                spawn.enemy.enemy_forces
                for spawn in MythicDungeonSpawn.objects.filter(
                    enemy__dungeon=dungeon,
                    enemy__is_active=True,
                    floor__is_active=True,
                    is_active=True,
                ).select_related('enemy')
            )
            self.assertEqual(imported_forces, dungeon.total_enemy_forces, dungeon.key)


class MythicDungeonToolsConverterTests(SimpleTestCase):
    @staticmethod
    def source_root():
        return (
            Path(settings.BASE_DIR)
            / 'botend'
            / 'data'
            / 'mythic_planner'
            / 'vendor'
            / 'mythic-dungeon-tools-6.2.0-alpha5'
        )

    def test_fixed_upstream_snapshot_converts_real_dungeons_and_assets(self):
        payload = build_payload(self.source_root())

        self.assertEqual(payload['data_version']['key'], 'mdt-6-2-0-alpha5')
        self.assertEqual(
            payload['data_version']['metadata']['source_commit'],
            '94473b391b6fb7563f8466c4a596a11ef6218a12',
        )
        self.assertEqual(payload['data_version']['metadata']['license'], 'GPL-2.0-only')
        self.assertEqual(len(payload['dungeons']), 16)
        selection_groups = payload['data_version']['metadata']['dungeon_selection_groups']
        self.assertEqual(
            [group['name_zh'] for group in selection_groups],
            ['至暗之夜 第二赛季', '至暗之夜 第一赛季'],
        )
        self.assertEqual(
            selection_groups[0]['dungeon_indexes'],
            [160, 161, 162, 163, 164, 42, 20, 17],
        )
        self.assertEqual(
            selection_groups[1]['dungeon_indexes'],
            [45, 11, 150, 151, 152, 153, 154, 155],
        )
        self.assertEqual(
            sum(len(dungeon['enemies']) for dungeon in payload['dungeons']),
            467,
        )
        self.assertEqual(
            sum(
                len(enemy['spawns'])
                for dungeon in payload['dungeons']
                for enemy in dungeon['enemies']
            ),
            3012,
        )
        murder_row = next(
            dungeon for dungeon in payload['dungeons'] if dungeon['key'] == 'murder-row'
        )
        self.assertEqual(murder_row['name_zh'], '密谋小径')
        self.assertEqual(murder_row['total_enemy_forces'], 690)
        self.assertEqual(
            murder_row['metadata']['selection_groups'][0]['key'],
            'midnight-season-2',
        )
        self.assertIn(
            '/static/portal/mythic_planner/vendor/mdt-6.2.0-alpha5/maps/',
            murder_row['floors'][0]['background_url'],
        )
        self.assertTrue(all(
            0 <= spawn['x'] <= 100 and 0 <= spawn['y'] <= 100
            for enemy in murder_row['enemies']
            for spawn in enemy['spawns']
        ))
        ability_supplement = payload['data_version']['metadata']['ability_supplement']
        self.assertEqual(ability_supplement['target']['branch'], 'wowt')
        self.assertEqual(ability_supplement['target']['game_build'], '12.1.0.68914')
        self.assertEqual(
            ability_supplement['relative_path'],
            'LMonitor/ability_overrides.json',
        )
        expected_ability_counts = {
            'ruby-life-pools': 93,
            'temple-of-sethraliss': 108,
            'kings-rest': 77,
        }
        excluded_non_dungeon_spells = {
            181089,
            205276,
            209859,
            224729,
            228318,
            240443,
            260792,
            277242,
            277485,
            277564,
            288865,
            317898,
            346202,
            454782,
        }
        for dungeon_key, expected_count in expected_ability_counts.items():
            dungeon = next(
                row for row in payload['dungeons'] if row['key'] == dungeon_key
            )
            abilities = [
                ability
                for enemy in dungeon['enemies']
                for ability in enemy['abilities']
            ]
            self.assertEqual(len(abilities), expected_count, dungeon_key)
            self.assertTrue(all(
                ability['metadata']['source'] == 'LMonitorAbilitySupplement'
                for ability in abilities
            ))
            self.assertFalse(
                excluded_non_dungeon_spells
                & {ability['spell_id'] for ability in abilities},
                dungeon_key,
            )

        temple = next(
            row
            for row in payload['dungeons']
            if row['key'] == 'temple-of-sethraliss'
        )
        tormentor = next(
            enemy for enemy in temple['enemies'] if enemy['npc_id'] == 268317
        )
        self.assertEqual(
            [ability['spell_id'] for ability in tormentor['abilities']],
            [1300714],
        )
        kings_rest = next(
            row for row in payload['dungeons'] if row['key'] == 'kings-rest'
        )
        shadow_barrage = next(
            ability
            for enemy in kings_rest['enemies']
            for ability in enemy['abilities']
            if ability['spell_id'] == 272388
        )
        self.assertEqual(
            shadow_barrage['description_zh'],
            '',
        )
        self.assertEqual(
            sum(
                len(enemy['abilities'])
                for dungeon in payload['dungeons']
                for enemy in dungeon['enemies']
            ),
            1648,
        )

    def test_existing_payload_seeds_spell_and_asset_metadata(self):
        seed_payload = {
            'data_version': {
                'metadata': {'spell_snapshot': {'source_branch': 'wowt'}},
            },
            'dungeons': [{
                'key': 'demo-dungeon',
                'floors': [{
                    'key': 'floor-1',
                    'background_url': 'https://oss.example/maps/floor.webp',
                }],
                'enemies': [{
                    'key': 'npc-1',
                    'icon_url': 'https://oss.example/enemies/1.jpg',
                    'abilities': [{
                        'spell_id': 123,
                        'name': 'Seed Spell',
                        'name_zh': '种子技能',
                        'description_zh': '保留已有完整说明。',
                        'icon_url': 'https://oss.example/spells/123.jpg',
                    }],
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            package_path = Path(temporary_directory) / 'seed.json'
            package_path.write_text(
                json.dumps(seed_payload, ensure_ascii=False),
                encoding='utf-8',
            )
            snapshots, floors, enemies, metadata = load_payload_seed(
                package_path,
            )

        self.assertEqual(snapshots[123]['name_zh'], '种子技能')
        self.assertEqual(
            snapshots[123]['description'],
            '保留已有完整说明。',
        )
        self.assertEqual(
            floors[('demo-dungeon', 'floor-1')],
            'https://oss.example/maps/floor.webp',
        )
        self.assertEqual(
            enemies[('demo-dungeon', 'npc-1')],
            'https://oss.example/enemies/1.jpg',
        )
        self.assertEqual(metadata['spell_snapshot']['source_branch'], 'wowt')

    def test_lua_parser_does_not_execute_identifiers_or_function_calls(self):
        with self.assertRaises(LuaParseError):
            LuaValueParser('os.execute("not allowed")').parse()

    def test_converter_preserves_oss_asset_snapshots(self):
        payload = build_payload(
            self.source_root(),
            spell_snapshots={
                272388: {
                    'name': 'Shadow Barrage',
                    'name_zh': '暗影弹幕',
                    'description': 'PTR 技能说明',
                    'icon_url': 'http://oss.example/spells/shadow-barrage.jpg',
                },
            },
            floor_background_urls={
                (
                    'kings-rest',
                    'floor-1',
                ): 'http://oss.example/maps/kings-rest/floor-1.webp',
            },
            enemy_icon_urls={
                (
                    'kings-rest',
                    'npc-135204',
                ): 'http://oss.example/enemies/kings-rest/npc-135204.jpg',
            },
        )
        kings_rest = next(
            row for row in payload['dungeons'] if row['key'] == 'kings-rest'
        )
        self.assertEqual(
            kings_rest['floors'][0]['background_url'],
            'http://oss.example/maps/kings-rest/floor-1.webp',
        )
        shadow_barrage = next(
            ability
            for enemy in kings_rest['enemies']
            for ability in enemy['abilities']
            if ability['spell_id'] == 272388
        )
        self.assertEqual(
            shadow_barrage['icon_url'],
            'http://oss.example/spells/shadow-barrage.jpg',
        )
        phantom_hex_priest = next(
            enemy for enemy in kings_rest['enemies'] if enemy['npc_id'] == 135204
        )
        self.assertEqual(
            phantom_hex_priest['icon_url'],
            'http://oss.example/enemies/kings-rest/npc-135204.jpg',
        )

    def test_asset_sync_deduplicates_shared_spell_icon_uploads(self):
        first = object()
        second = object()
        jobs = SyncMythicDungeonAssetsCommand._deduplicate_jobs([
            {
                'kind': 'spell',
                'instance': first,
                'source': 'https://example.com/icon.jpg',
                'source_url': 'https://example.com/icon.jpg',
                'object_key': 'mythic-planner/version/spells/icon.jpg',
            },
            {
                'kind': 'spell',
                'instance': second,
                'source': 'https://example.com/icon.jpg',
                'source_url': 'https://example.com/icon.jpg',
                'object_key': 'mythic-planner/version/spells/icon.jpg',
            },
        ])

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['instances'], [first, second])

    def test_asset_sync_only_recognizes_configured_oss_host(self):
        base_url = 'http://oss.example/assets/'
        self.assertTrue(
            SyncMythicDungeonAssetsCommand._is_oss_url(
                'https://oss.example/mythic-planner/map.webp',
                base_url,
            )
        )
        self.assertFalse(
            SyncMythicDungeonAssetsCommand._is_oss_url(
                'https://wow.zamimg.com/images/icon.jpg',
                base_url,
            )
        )

    def test_asset_sync_maps_remote_path_to_stable_oss_key(self):
        object_key = SyncMythicDungeonAssetsCommand._remote_object_key(
            'mythic-planner',
            (
                'https://wow.zamimg.com/images/wow/icons/large/'
                'spell_shadow_shadowbolt.jpg?build=68914'
            ),
            fallback='spells/fallback.jpg',
        )

        self.assertEqual(
            object_key,
            (
                'wowhead/images/wow/icons/large/'
                'spell_shadow_shadowbolt.jpg'
            ),
        )

    def test_wowhead_icon_slug_normalizes_client_filename_spaces(self):
        cases = {
            'inv_10_specialreagentfoozles_tuskclaw black': (
                'inv_10_specialreagentfoozles_tuskclaw-black'
            ),
            ' Spell_Frost_Ring Of Frost ': 'spell_frost_ring-of-frost',
            'inv_ misc_herb_marrowroot_leaf': 'inv_-misc_herb_marrowroot_leaf',
            'spell_priest_void blast': 'spell_priest_void-blast',
            'spell_frost_piercing chill': 'spell_frost_piercing-chill',
            'trade_archaeology_bones of transformation': (
                'trade_archaeology_bones-of-transformation'
            ),
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    normalize_wowhead_icon_slug(source),
                    expected,
                )
                self.assertEqual(
                    build_wowhead_icon_url(source),
                    (
                        'https://wow.zamimg.com/images/wow/icons/large/'
                        f'{expected}.jpg'
                    ),
                )

    def test_asset_sync_keeps_host_for_non_wowhead_remote_path(self):
        object_key = SyncMythicDungeonAssetsCommand._remote_object_key(
            'mythic-planner',
            'https://assets.example.com/images/icon.jpg?revision=2',
            fallback='spells/fallback.jpg',
        )

        self.assertEqual(
            object_key,
            'mythic-planner/sources/assets.example.com/images/icon.jpg',
        )

    def test_asset_sync_distinguishes_legacy_and_current_oss_paths(self):
        base_url = 'http://oss.example/'
        object_key = 'wowhead/images/wow/icons/large/icon.jpg'

        self.assertTrue(
            SyncMythicDungeonAssetsCommand._is_oss_object_url(
                'https://oss.example/wowhead/images/wow/icons/large/icon.jpg',
                base_url,
                object_key,
            )
        )
        self.assertFalse(
            SyncMythicDungeonAssetsCommand._is_oss_object_url(
                (
                    'https://oss.example/mythic-planner/sources/'
                    'wow.zamimg.com/images/wow/icons/large/icon.jpg'
                ),
                base_url,
                object_key,
            )
        )

    @override_settings(
        PROXY_CONFIG={
            'http': 'socks5://127.0.0.1:10809',
            'https': 'socks5://127.0.0.1:10809',
        },
    )
    @mock.patch(
        'botend.management.commands.sync_mythic_dungeon_assets.requests.get',
    )
    def test_asset_sync_force_refreshes_download_cache(self, request_get):
        response = mock.Mock()
        response.headers = {'Content-Type': 'image/jpeg'}
        response.content = b'new-image'
        response.raise_for_status.return_value = None
        request_get.return_value = response

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / 'icon.jpg'
            target.write_bytes(b'old-image')
            SyncMythicDungeonAssetsCommand._download_image(
                'https://wow.zamimg.com/images/wow/icons/large/icon.jpg',
                target,
                refresh=True,
            )

            self.assertEqual(target.read_bytes(), b'new-image')
            request_get.assert_called_once()
            self.assertEqual(
                request_get.call_args.kwargs['proxies'],
                {
                    'http': 'socks5://127.0.0.1:10809',
                    'https': 'socks5://127.0.0.1:10809',
                },
            )

    @override_settings(PROXY_CONFIG={}, REQUEST_CONFIG={})
    @mock.patch(
        'botend.management.commands.sync_mythic_dungeon_assets.requests.get',
    )
    def test_asset_sync_download_allows_empty_proxy_config(self, request_get):
        response = mock.Mock()
        response.headers = {'Content-Type': 'image/jpeg'}
        response.content = b'image'
        response.raise_for_status.return_value = None
        request_get.return_value = response

        with tempfile.TemporaryDirectory() as temp_dir:
            SyncMythicDungeonAssetsCommand._download_image(
                'https://wow.zamimg.com/images/wow/icons/large/icon.jpg',
                Path(temp_dir) / 'icon.jpg',
            )

        self.assertIsNone(request_get.call_args.kwargs['proxies'])

    @override_settings(
        PROXY_CONFIG={},
        REQUEST_CONFIG={
            'proxies': {
                'https': 'http://127.0.0.1:7890',
            },
        },
    )
    @mock.patch(
        'botend.management.commands.sync_mythic_dungeon_assets.requests.get',
    )
    def test_asset_sync_uses_request_config_proxy_fallback(self, request_get):
        response = mock.Mock()
        response.headers = {'Content-Type': 'image/jpeg'}
        response.content = b'image'
        response.raise_for_status.return_value = None
        request_get.return_value = response

        with tempfile.TemporaryDirectory() as temp_dir:
            SyncMythicDungeonAssetsCommand._download_image(
                'https://wow.zamimg.com/images/wow/icons/large/icon.jpg',
                Path(temp_dir) / 'icon.jpg',
            )

        self.assertEqual(
            request_get.call_args.kwargs['proxies'],
            {'https': 'http://127.0.0.1:7890'},
        )

    @override_settings(
        PROXY_CONFIG={'https': 'socks5://127.0.0.1:10809'},
    )
    @mock.patch(
        'botend.management.commands.sync_mythic_dungeon_assets.requests.get',
    )
    def test_asset_sync_explains_missing_socks_dependency(self, request_get):
        request_get.side_effect = requests.exceptions.InvalidSchema(
            'Missing dependencies for SOCKS support.',
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(
                RuntimeError,
                'python -m pip install PySocks==1.7.1',
            ):
                SyncMythicDungeonAssetsCommand._download_image(
                    'https://wow.zamimg.com/images/wow/icons/large/icon.jpg',
                    Path(temp_dir) / 'icon.jpg',
                )

        request_get.assert_called_once()

    @mock.patch(
        'botend.management.commands.sync_mythic_dungeon_assets.requests.get',
    )
    def test_asset_sync_treats_upstream_404_as_unavailable(self, request_get):
        response = mock.Mock()
        response.status_code = 404
        request_get.return_value = response

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(AssetUnavailableError):
                SyncMythicDungeonAssetsCommand._download_image(
                    'https://wow.zamimg.com/missing.jpg',
                    Path(temp_dir) / 'missing.jpg',
                )

        request_get.assert_called_once()

    def test_wowhead_tooltip_html_parser_returns_plain_chinese_description(self):
        tooltip = (
            '<table><tr><td><div class="q">造成101690点自然伤害，'
            '<br>并使最大生命值降低5%。</div></td></tr></table>'
        )

        self.assertEqual(
            SyncMythicDungeonSpellsCommand._description_from_tooltip_html(tooltip),
            '造成101690点自然伤害，\n并使最大生命值降低5%。',
        )
        self.assertEqual(
            SyncMythicDungeonSpellsCommand._description_from_tooltip_html(
                '<div class="q">造成的伤害提高20$</div>',
            ),
            '造成的伤害提高20%',
        )

    def test_wowhead_tooltip_parser_discovers_linked_effect_spell(self):
        tooltip = (
            '<div class="q"><a href="/cn/spell=1236709/唤棘者咆哮" '
            'target="_blank"></a></div>'
        )

        self.assertEqual(
            SyncMythicDungeonSpellsCommand._referenced_spell_ids_from_tooltip(
                tooltip,
                1236731,
            ),
            [1236709],
        )

    def test_wowhead_data_environment_tracks_the_requested_game_branch(self):
        self.assertEqual(
            SyncMythicDungeonSpellsCommand._resolve_wowhead_data_env('wow', 0),
            1,
        )
        self.assertEqual(
            SyncMythicDungeonSpellsCommand._resolve_wowhead_data_env('wowt', 0),
            2,
        )
        self.assertEqual(
            SyncMythicDungeonSpellsCommand._resolve_wowhead_data_env('wowxptr', 0),
            10,
        )
        self.assertEqual(
            SyncMythicDungeonSpellsCommand._resolve_wowhead_data_env('wowt', 3),
            3,
        )
        with self.assertRaisesMessage(CommandError, '不支持的 Wowhead dataEnv'):
            SyncMythicDungeonSpellsCommand._resolve_wowhead_data_env('wowt', 99)

    def test_spell_sync_resolves_latest_processed_wago_branch_build(self):
        payload = {
            'props': {
                'builds': {
                    'data': [
                        {
                            'product': 'wowt',
                            'version': '12.1.0.69112',
                            'processed': False,
                        },
                        {
                            'product': 'wow',
                            'version': '12.0.7.68974',
                            'processed': True,
                        },
                        {
                            'product': 'wowt',
                            'version': '12.1.0.69111',
                            'processed': True,
                        },
                    ],
                    'next_page_url': None,
                },
            },
        }
        response = mock.Mock()
        response.text = f"<main data-page='{json.dumps(payload)}'></main>"
        session = mock.Mock()
        session.get.return_value = response

        resolved = SyncMythicDungeonSpellsCommand()._resolve_wago_build(
            session,
            branch='wowt',
            configured_build='latest',
        )

        self.assertEqual(resolved, '12.1.0.69111')
        session.get.assert_called_once_with(
            'https://wago.tools/builds',
            timeout=45,
        )
        response.raise_for_status.assert_called_once_with()

    def test_spell_sync_keeps_explicit_build_without_catalog_request(self):
        session = mock.Mock()

        resolved = SyncMythicDungeonSpellsCommand()._resolve_wago_build(
            session,
            branch='wowt',
            configured_build='12.1.0.68914',
        )

        self.assertEqual(resolved, '12.1.0.68914')
        session.get.assert_not_called()

    @override_settings(
        PROXY_CONFIG={
            'http': 'socks5://127.0.0.1:10809',
            'https': 'socks5://127.0.0.1:10809',
        },
    )
    @mock.patch(
        'botend.management.commands.sync_mythic_dungeon_spells.requests.get',
    )
    def test_wowhead_tooltip_request_uses_ptr_data_environment(self, request_get):
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            'tooltip': '<div class="q">造成123点暗影伤害。</div>',
        }
        request_get.return_value = response

        description = SyncMythicDungeonSpellsCommand._fetch_wowhead_tooltip(
            12345,
            0,
            4,
            data_env=2,
        )

        self.assertEqual(description, '造成123点暗影伤害。')
        self.assertEqual(request_get.call_args.kwargs['params']['dataEnv'], 2)
        self.assertEqual(request_get.call_args.kwargs['params']['locale'], 4)
        self.assertEqual(request_get.call_args.kwargs['params']['dd'], 8)
        self.assertEqual(
            request_get.call_args.kwargs['proxies'],
            {
                'http': 'socks5://127.0.0.1:10809',
                'https': 'socks5://127.0.0.1:10809',
            },
        )

    @override_settings(PROXY_CONFIG={}, REQUEST_CONFIG={})
    @mock.patch(
        'botend.management.commands.sync_mythic_dungeon_spells.requests.get',
    )
    def test_wowhead_tooltip_request_allows_empty_proxy_config(
        self,
        request_get,
    ):
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            'tooltip': '<div class="q">造成123点暗影伤害。</div>',
        }
        request_get.return_value = response

        SyncMythicDungeonSpellsCommand._fetch_wowhead_tooltip(
            12345,
            0,
            4,
            data_env=1,
        )

        self.assertIsNone(request_get.call_args.kwargs['proxies'])

    def test_wowhead_description_references_do_not_include_effect_values(self):
        rows = {
            1253520: {
                'description_zh': (
                    '$@spelldesc1253519 每$1253520t2秒受到$s1点火焰伤害。'
                ),
                'aura_description_zh': '',
            },
            1254336: {
                'description_zh': '造成$1254338s1点火焰伤害。',
                'aura_description_zh': '',
            },
        }

        self.assertEqual(
            SyncMythicDungeonSpellsCommand._collect_description_references(rows),
            {1253519},
        )

    def test_wowhead_reference_is_stitched_into_wago_template(self):
        resolved = SyncMythicDungeonSpellsCommand._composite_description_zh(
            spell_id=1253520,
            raw_description_zh=(
                '$@spelldesc1253519 每$1253520t2秒受到$s1点火焰伤害。'
            ),
            raw_aura_description_zh='',
            wowhead_tooltips={
                1253519: (
                    '鲁克兰用燃烧利爪撕裂目标，造成203380点物理伤害，'
                    '并使目标燃烧。'
                ),
            },
            resolver=SpellTextResolver(locale='zhCN', branch='wowt'),
        )

        self.assertEqual(
            resolved['source'],
            SOURCE_WOWHEAD_TOOLTIP_REFERENCE,
        )
        self.assertEqual(resolved['quality'], QUALITY_RENDERED_EXTERNAL)
        self.assertEqual(resolved['reference_spell_ids'], [1253519])
        self.assertIn('203380点物理伤害', resolved['description'])
        self.assertNotIn('$', resolved['description'])
        self.assertNotRegex(resolved['description'], r'(?<![A-Za-z])x(?![A-Za-z])')

    def test_spell_sync_prefers_chinese_db2_text_over_english_tooltip(self):
        self.assertEqual(
            SyncMythicDungeonSpellsCommand._localized_description(
                'Inflicts 3789 Shadow damage to an enemy.',
                '对一名敌人造成3789点暗影伤害。',
            ),
            '对一名敌人造成3789点暗影伤害。',
        )
        self.assertEqual(
            SyncMythicDungeonSpellsCommand._localized_description(
                '召唤一个原始雷云协助施法者战斗。',
                'DB2 说明',
            ),
            '召唤一个原始雷云协助施法者战斗。',
        )
        self.assertEqual(
            SyncMythicDungeonSpellsCommand._localized_description(
                'English fallback',
                '',
            ),
            'English fallback',
        )

    def test_empty_ptr_tooltip_is_cached_but_not_counted_as_usable(self):
        self.assertFalse(
            SyncMythicDungeonSpellsCommand._tooltip_needs_retry(''),
        )
        self.assertEqual(
            SyncMythicDungeonSpellsCommand._usable_tooltip_count(
                {270493, 272388},
                {270493: '', 272388: ''},
            ),
            0,
        )

    def test_spell_sync_preserves_only_archived_icon_urls(self):
        self.assertTrue(
            SyncMythicDungeonSpellsCommand._is_wowhead_asset_url(
                'https://wow.zamimg.com/images/wow/icons/large/icon.jpg',
            )
        )
        self.assertFalse(
            SyncMythicDungeonSpellsCommand._is_wowhead_asset_url(
                'http://oss.example/mythic-planner/sources/icon.jpg',
            )
        )


class MythicPlannerSpellDescriptionSourceTests(TestCase):
    def _version_and_spell(self, *, client_build='68914', difficulty_id=8):
        version = MythicDungeonDataVersion.objects.create(
            key='tooltip-source-test',
            label='Tooltip 来源测试',
            game_version='12.1.0',
            is_active=True,
        )
        spell = MythicDungeonSpell.objects.create(
            data_version=version,
            spell_id=154132,
            source_branch='wowt',
            source_locale='zhCN',
            snapshot_build='12.1.0.68914',
            name_zh='灼热重击',
            description_zh='客户端精确说明。',
            metadata={
                'description_source': SOURCE_WOW_CLIENT,
                'description_quality': QUALITY_EXACT_RENDERED,
                'client_version': '12.1.0',
                'client_build': client_build,
                'client_locale': 'zhCN',
                'difficulty_id': difficulty_id,
            },
        )
        return version, spell

    @staticmethod
    def _coverage():
        return {
            'total': 1,
            'name': 1,
            'name_zh': 1,
            'raw_text': 1,
            'icon_id': 0,
            'icon_name': 0,
            'rendered_tooltip_zh': 0,
        }

    def _write_db2_snapshot(self, version, build):
        command = SyncMythicDungeonSpellsCommand()
        with tempfile.TemporaryDirectory() as temp_dir:
            command._write_snapshots(
                version=version,
                branch='wowt',
                build=build,
                dump_dir=Path(temp_dir),
                spell_ids={154132},
                rows={
                    154132: {
                        'name': 'Searing Slam',
                        'name_zh': '灼热重击',
                        'description': 'Deals $s1 Fire damage.',
                        'description_zh': '造成$s1点火焰伤害。',
                        'aura_description': '',
                        'aura_description_zh': '',
                    },
                },
                misc={},
                effects={},
                icon_names={},
                coverage=self._coverage(),
                listfile_url='',
                wowhead_tooltips={},
                tooltip_data_env=2,
            )

    def test_same_build_db2_sync_does_not_downgrade_exact_client_tooltip(self):
        version, spell = self._version_and_spell()

        self._write_db2_snapshot(version, '12.1.0.68914')

        spell.refresh_from_db()
        version.refresh_from_db()
        self.assertEqual(spell.description_zh, '客户端精确说明。')
        self.assertEqual(
            spell.metadata['description_quality'],
            QUALITY_EXACT_RENDERED,
        )
        self.assertEqual(
            version.metadata['spell_snapshot']['description_coverage'][
                QUALITY_EXACT_RENDERED
            ],
            1,
        )

    def test_new_build_db2_sync_replaces_stale_exact_client_tooltip_safely(self):
        version, spell = self._version_and_spell(client_build='68914')

        self._write_db2_snapshot(version, '12.1.0.69000')

        spell.refresh_from_db()
        self.assertEqual(spell.description_zh, '造成火焰伤害。')
        self.assertEqual(
            spell.metadata['description_quality'],
            QUALITY_MECHANIC_ONLY,
        )
        self.assertNotIn('x', spell.description_zh.lower())
        self.assertNotIn('$', spell.description_zh)

    def test_wowhead_tooltip_only_does_not_override_exact_client_tooltip(self):
        version, spell = self._version_and_spell()

        result = SyncMythicDungeonSpellsCommand()._write_tooltips_only(
            version=version,
            branch='wowt',
            build='12.1.0.68914',
            spell_ids={spell.spell_id},
            wowhead_tooltips={spell.spell_id: '外部渲染说明。'},
            locale=4,
            data_env=2,
        )

        spell.refresh_from_db()
        self.assertEqual(spell.description_zh, '客户端精确说明。')
        self.assertEqual(result['updated'], 0)

    def test_wowhead_tooltip_only_replaces_stale_build_client_tooltip(self):
        version, spell = self._version_and_spell(client_build='68914')

        result = SyncMythicDungeonSpellsCommand()._write_tooltips_only(
            version=version,
            branch='wowt',
            build='12.1.0.69000',
            spell_ids={spell.spell_id},
            wowhead_tooltips={spell.spell_id: '目标 PTR 环境渲染说明。'},
            locale=4,
            data_env=2,
        )

        spell.refresh_from_db()
        self.assertEqual(spell.description_zh, '目标 PTR 环境渲染说明。')
        self.assertEqual(result['updated'], 1)
        self.assertEqual(spell.snapshot_build, '12.1.0.68914')
        self.assertEqual(
            spell.metadata['wowhead_requested_build'],
            '12.1.0.69000',
        )
        self.assertFalse(spell.metadata['wowhead_build_exact'])

    def test_wowhead_tooltip_only_replaces_wrong_difficulty_client_tooltip(self):
        version, spell = self._version_and_spell(difficulty_id=23)

        result = SyncMythicDungeonSpellsCommand()._write_tooltips_only(
            version=version,
            branch='wowt',
            build='12.1.0.68914',
            spell_ids={spell.spell_id},
            wowhead_tooltips={spell.spell_id: '钥石难度渲染说明。'},
            locale=4,
            data_env=2,
            difficulty_id=8,
        )

        spell.refresh_from_db()
        self.assertEqual(spell.description_zh, '钥石难度渲染说明。')
        self.assertEqual(result['updated'], 1)
        self.assertEqual(spell.metadata['wowhead_difficulty_id'], 8)

    def test_tooltip_only_requires_matching_db2_snapshot_context(self):
        version, spell = self._version_and_spell()

        with self.assertRaisesMessage(
            CommandError,
            '请先去掉 --tooltip-only 执行完整 DB2 同步',
        ):
            SyncMythicDungeonSpellsCommand._validate_tooltip_snapshot_context(
                version,
                {spell.spell_id},
                branch='wowt',
                build='12.1.0.69000',
            )

        SyncMythicDungeonSpellsCommand._validate_tooltip_snapshot_context(
            version,
            {spell.spell_id},
            branch='wowt',
            build='12.1.0.68914',
        )

    def test_spell_sync_dungeon_filter_validates_requested_keys(self):
        version, _spell = self._version_and_spell()
        MythicDungeon.objects.create(
            data_version=version,
            key='ruby-life-pools',
            name='Ruby Life Pools',
            name_zh='红玉新生法池',
        )
        MythicDungeon.objects.create(
            data_version=version,
            key='kings-rest',
            name="King's Rest",
            name_zh='诸王之眠',
        )

        self.assertEqual(
            SyncMythicDungeonSpellsCommand._resolve_dungeon_keys(
                version,
                ['ruby-life-pools', 'kings-rest', 'ruby-life-pools'],
            ),
            ['kings-rest', 'ruby-life-pools'],
        )
        with self.assertRaisesMessage(CommandError, 'missing-dungeon'):
            SyncMythicDungeonSpellsCommand._resolve_dungeon_keys(
                version,
                ['missing-dungeon'],
            )

    def test_tooltip_only_builds_direct_reference_and_mechanic_descriptions(self):
        version = MythicDungeonDataVersion.objects.create(
            key='tooltip-composite-test',
            label='Tooltip 组合来源测试',
            game_version='12.1.0',
            is_active=True,
        )
        direct = MythicDungeonSpell.objects.create(
            data_version=version,
            spell_id=154132,
            source_branch='wowt',
            source_locale='zhCN',
            snapshot_build='12.1.0.68914',
            description_zh='造成x点火焰伤害。',
            metadata={'raw_description_zh': '造成$s1点火焰伤害。'},
        )
        referenced = MythicDungeonSpell.objects.create(
            data_version=version,
            spell_id=1253520,
            source_branch='wowt',
            source_locale='zhCN',
            snapshot_build='12.1.0.68914',
            description_zh='造成x点火焰伤害。',
            metadata={
                'raw_description_zh': (
                    '$@spelldesc1253519 每$1253520t2秒受到$s1点火焰伤害。'
                ),
            },
        )
        mechanic = MythicDungeonSpell.objects.create(
            data_version=version,
            spell_id=245742,
            source_branch='wowt',
            source_locale='zhCN',
            snapshot_build='12.1.0.68914',
            description_zh='造成x点物理伤害。',
            metadata={'raw_description_zh': '造成$s1点物理伤害。'},
        )
        blank = MythicDungeonSpell.objects.create(
            data_version=version,
            spell_id=1239871,
            source_branch='wowt',
            source_locale='zhCN',
            snapshot_build='12.1.0.68914',
            description_zh='x',
            metadata={},
        )

        result = SyncMythicDungeonSpellsCommand()._write_tooltips_only(
            version=version,
            branch='wowt',
            build='12.1.0.68914',
            spell_ids={
                direct.spell_id,
                referenced.spell_id,
                mechanic.spell_id,
                blank.spell_id,
            },
            wowhead_tooltips={
                direct.spell_id: '造成127113点火焰伤害。',
                1253519: '撕裂目标，造成203380点物理伤害。',
            },
            locale=4,
            data_env=1,
            difficulty_id=8,
        )

        direct.refresh_from_db()
        referenced.refresh_from_db()
        mechanic.refresh_from_db()
        blank.refresh_from_db()
        self.assertEqual(direct.description_zh, '造成127113点火焰伤害。')
        self.assertEqual(
            direct.metadata['description_source'],
            SOURCE_WOWHEAD_TOOLTIP,
        )
        self.assertEqual(direct.metadata['wowhead_difficulty_id'], 8)
        self.assertFalse(direct.metadata['wowhead_build_exact'])
        self.assertEqual(
            direct.metadata['wowhead_version_scope'],
            'environment_current',
        )
        self.assertIn('dd=8', direct.metadata['wowhead_tooltip_source'])
        self.assertIn('203380点物理伤害', referenced.description_zh)
        self.assertEqual(
            referenced.metadata['description_source'],
            SOURCE_WOWHEAD_TOOLTIP_REFERENCE,
        )
        self.assertEqual(
            referenced.metadata['wowhead_reference_spell_ids'],
            [1253519],
        )
        self.assertEqual(
            mechanic.metadata['description_source'],
            SOURCE_WAGO_DB2,
        )
        self.assertEqual(
            mechanic.metadata['description_quality'],
            QUALITY_MECHANIC_ONLY,
        )
        self.assertEqual(mechanic.description_zh, '造成物理伤害。')
        self.assertEqual(blank.description_zh, '')
        self.assertEqual(result['fetched'], 1)
        self.assertEqual(result['referenced'], 1)
        self.assertEqual(result['mechanic_only'], 1)
        self.assertEqual(result['blank'], 1)
        self.assertEqual(result['updated'], 4)

    def test_tooltip_only_dry_run_reports_without_writing(self):
        version = MythicDungeonDataVersion.objects.create(
            key='tooltip-dry-run-test',
            label='Tooltip dry-run 测试',
            game_version='12.1.0',
            is_active=True,
            metadata={'marker': 'keep'},
        )
        spell = MythicDungeonSpell.objects.create(
            data_version=version,
            spell_id=154132,
            source_branch='wowt',
            source_locale='zhCN',
            snapshot_build='12.1.0.68914',
            description_zh='旧说明x。',
            metadata={'raw_description_zh': '造成$s1点火焰伤害。'},
        )

        result = SyncMythicDungeonSpellsCommand()._write_tooltips_only(
            version=version,
            branch='wowt',
            build='12.1.0.68914',
            spell_ids={spell.spell_id},
            wowhead_tooltips={spell.spell_id: '造成127113点火焰伤害。'},
            locale=4,
            data_env=1,
            difficulty_id=8,
            dry_run=True,
        )

        spell.refresh_from_db()
        version.refresh_from_db()
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['updated'], 1)
        self.assertEqual(spell.description_zh, '旧说明x。')
        self.assertEqual(version.metadata, {'marker': 'keep'})


class MythicPlannerNormalizeSpellDescriptionCommandTests(TestCase):
    full_build = '12.1.0.68914'

    def setUp(self):
        self.version = MythicDungeonDataVersion.objects.create(
            key='normalize-tooltip-test',
            label='说明重建测试',
            game_version='12.1.0',
            is_active=True,
            metadata={
                'spell_snapshot': {
                    'snapshot_build': self.full_build,
                    'source_branch': 'wowt',
                },
            },
        )

    def _create_spell(self, spell_id, **values):
        defaults = {
            'data_version': self.version,
            'spell_id': spell_id,
            'source_branch': 'wowt',
            'source_locale': 'zhCN',
            'snapshot_build': self.full_build,
            'description_zh': '造成x点火焰伤害。',
            'metadata': {
                'raw_description_zh': '造成$s1点火焰伤害。',
            },
        }
        defaults.update(values)
        return MythicDungeonSpell.objects.create(**defaults)

    def test_normalize_rebuilds_legacy_description_without_redownloading_db2(self):
        spell = self._create_spell(154132)

        call_command(
            'normalize_mythic_spell_descriptions',
            version_key=self.version.key,
            expected_build=self.full_build,
            verbosity=0,
        )

        spell.refresh_from_db()
        self.assertEqual(spell.description_zh, '造成火焰伤害。')
        self.assertEqual(
            spell.metadata['description_quality'],
            QUALITY_MECHANIC_ONLY,
        )
        self.assertNotIn('x', spell.description_zh.lower())
        self.assertNotIn('$', spell.description_zh)

    def test_normalize_dry_run_does_not_write_and_preserves_current_exact(self):
        legacy = self._create_spell(154132)
        exact = self._create_spell(
            154133,
            description_zh='客户端精确说明。',
            metadata={
                'raw_description_zh': '造成$s1点冰霜伤害。',
                'description_source': SOURCE_WOW_CLIENT,
                'description_quality': QUALITY_EXACT_RENDERED,
                'client_version': '12.1.0',
                'client_build': '68914',
            },
        )

        call_command(
            'normalize_mythic_spell_descriptions',
            version_key=self.version.key,
            expected_build=self.full_build,
            dry_run=True,
            verbosity=0,
        )

        legacy.refresh_from_db()
        exact.refresh_from_db()
        self.assertEqual(legacy.description_zh, '造成x点火焰伤害。')
        self.assertEqual(exact.description_zh, '客户端精确说明。')
        self.assertEqual(
            exact.metadata['description_quality'],
            QUALITY_EXACT_RENDERED,
        )

    def test_normalize_replaces_only_stale_exact_client_description(self):
        stale = self._create_spell(
            154132,
            description_zh='旧客户端说明。',
            metadata={
                'raw_description_zh': '造成$s1点火焰伤害。',
                'description_source': SOURCE_WOW_CLIENT,
                'description_quality': QUALITY_EXACT_RENDERED,
                'client_version': '12.0.0',
                'client_build': '65000',
            },
        )

        call_command(
            'normalize_mythic_spell_descriptions',
            version_key=self.version.key,
            expected_build=self.full_build,
            verbosity=0,
        )

        stale.refresh_from_db()
        self.assertEqual(stale.description_zh, '造成火焰伤害。')
        self.assertEqual(
            stale.metadata['description_quality'],
            QUALITY_MECHANIC_ONLY,
        )

    def test_normalize_rejects_a_different_expected_build(self):
        with self.assertRaisesMessage(CommandError, '数据版本 build 不匹配'):
            call_command(
                'normalize_mythic_spell_descriptions',
                version_key=self.version.key,
                expected_build='12.1.0.69000',
                verbosity=0,
            )


class MythicPlannerClientTooltipCommandTests(TestCase):
    full_build = '12.1.0.68914'

    def setUp(self):
        import_mythic_dungeon_payload(demo_payload(), activate=True)
        self.version = MythicDungeonDataVersion.objects.get(
            key='lmonitor-demo-1',
        )
        self.version.game_version = '12.1.0'
        self.version.metadata = {
            'spell_snapshot': {
                'snapshot_build': self.full_build,
                'source_branch': 'wowt',
            },
        }
        self.version.save()
        self.spell_ids = sorted({
            int(spell_id)
            for spell_id in MythicDungeonAbility.objects.filter(
                enemy__dungeon__data_version=self.version,
                is_active=True,
            ).values_list('spell_id', flat=True)
        })

    def _manifest_hash(self):
        return manifest_hash(build_manifest_core(
            data_version_key=self.version.key,
            full_build=self.full_build,
            locale='zhCN',
            difficulty_id=8,
            spell_ids=self.spell_ids,
        ))

    def _snapshot_text(
        self,
        *,
        client_build='68914',
        manifest_digest=None,
        invalid_spell_id=None,
        missing_ids=None,
    ):
        missing_ids = set(missing_ids or ())
        lines = [
            'LMonitorMythicTooltipExport = {',
            '    schema_version = 1,',
            '    collector_version = "1.0.0",',
            f'    data_version_key = {json.dumps(self.version.key)},',
            f'    expected_full_build = {json.dumps(self.full_build)},',
            '    client_version = "12.1.0",',
            f'    client_build = {json.dumps(client_build)},',
            '    client_interface_version = 120100,',
            '    client_locale = "zhCN",',
            '    difficulty_id = 8,',
            f'    manifest_hash = {json.dumps(manifest_digest or self._manifest_hash())},',
            '    completed_at = 1785254400,',
            f'    total = {len(self.spell_ids)},',
            '    spells = {',
        ]
        for spell_id in self.spell_ids:
            if spell_id in missing_ids:
                continue
            description = (
                '造成$x点火焰伤害。'
                if spell_id == invalid_spell_id
                else '对玩家造成火焰伤害。'
            )
            lines.extend([
                f'        [{spell_id}] = {{',
                f'            name = "技能 {spell_id}",',
                f'            description = {json.dumps(description, ensure_ascii=False)},',
                '            capture_source = "tooltip_info",',
                '            line_type = 34,',
                '        },',
            ])
        lines.extend([
            '    },',
            '    missing = {',
        ])
        for spell_id in sorted(missing_ids):
            lines.extend([
                f'        [{spell_id}] = {{',
                '            reason = "description_not_loaded",',
                '            attempts = 6,',
                '        },',
            ])
        lines.extend([
            '    },',
            '}',
            '',
        ])
        return '\n'.join(lines)

    def test_export_manifest_writes_versioned_ignored_lua_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / 'manifest.lua'
            call_command(
                'export_mythic_tooltip_manifest',
                version_key=self.version.key,
                build=self.full_build,
                output=str(output),
                verbosity=0,
            )

            text = output.read_text(encoding='utf-8')
            match = re.search(
                r'LMonitorMythicTooltipManifest\s*=\s*',
                text,
            )
            manifest = LuaValueParser(text, match.end()).parse()

        self.assertEqual(manifest['data_version_key'], self.version.key)
        self.assertEqual(manifest['expected_full_build'], self.full_build)
        self.assertEqual(manifest['difficulty_id'], 8)
        self.assertEqual(
            [manifest['spell_ids'][index] for index in sorted(manifest['spell_ids'])],
            self.spell_ids,
        )
        self.assertEqual(manifest['manifest_hash'], self._manifest_hash())

    def test_import_client_tooltip_snapshot_updates_public_spell_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / 'LMonitor.lua'
            snapshot_path.write_text(
                self._snapshot_text(),
                encoding='utf-8',
            )
            call_command(
                'import_mythic_tooltip_snapshot',
                input=str(snapshot_path),
                version_key=self.version.key,
                expected_build=self.full_build,
                min_coverage=1.0,
                verbosity=0,
            )

        spells = list(MythicDungeonSpell.objects.filter(
            data_version=self.version,
            spell_id__in=self.spell_ids,
        ))
        self.assertEqual(len(spells), len(self.spell_ids))
        self.assertTrue(all(
            spell.metadata.get('description_quality') == QUALITY_EXACT_RENDERED
            for spell in spells
        ))
        self.assertTrue(all(
            spell.metadata.get('description_source') == SOURCE_WOW_CLIENT
            for spell in spells
        ))
        self.assertTrue(all(spell.description_zh == '对玩家造成火焰伤害。' for spell in spells))
        self.version.refresh_from_db()
        client_tooltips = self.version.metadata['spell_snapshot']['client_tooltips']
        self.assertEqual(client_tooltips['captured'], len(self.spell_ids))
        self.assertEqual(client_tooltips['coverage'], 1.0)

    def test_import_rejects_wrong_build_stale_manifest_and_unresolved_text(self):
        cases = (
            (
                self._snapshot_text(client_build='68915'),
                '客户端 build 不匹配',
            ),
            (
                self._snapshot_text(manifest_digest='0' * 64),
                '采集清单哈希与当前数据库不一致',
            ),
            (
                self._snapshot_text(invalid_spell_id=self.spell_ids[0]),
                '快照包含无效技能说明',
            ),
        )
        for index, (content, message) in enumerate(cases):
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory() as temp_dir:
                    snapshot_path = Path(temp_dir) / f'LMonitor-{index}.lua'
                    snapshot_path.write_text(content, encoding='utf-8')
                    with self.assertRaisesMessage(CommandError, message):
                        call_command(
                            'import_mythic_tooltip_snapshot',
                            input=str(snapshot_path),
                            version_key=self.version.key,
                            expected_build=self.full_build,
                            verbosity=0,
                        )

    def test_import_can_gate_partial_snapshot_by_exact_coverage(self):
        missing_id = self.spell_ids[-1]
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / 'LMonitor-partial.lua'
            snapshot_path.write_text(
                self._snapshot_text(missing_ids={missing_id}),
                encoding='utf-8',
            )
            with self.assertRaisesMessage(CommandError, '低于最低要求'):
                call_command(
                    'import_mythic_tooltip_snapshot',
                    input=str(snapshot_path),
                    version_key=self.version.key,
                    expected_build=self.full_build,
                    min_coverage=1.0,
                    verbosity=0,
                )

    def test_import_does_not_overwrite_public_manual_description(self):
        manual_spell = MythicDungeonSpell.objects.filter(
            data_version=self.version,
            spell_id=self.spell_ids[0],
        ).first()
        manual_spell.description_zh = '管理员手工说明。'
        manual_spell.metadata = {
            **(manual_spell.metadata or {}),
            'description_source': SOURCE_MANUAL,
            'description_quality': QUALITY_MANUAL_OVERRIDE,
        }
        manual_spell.save()

        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / 'LMonitor.lua'
            snapshot_path.write_text(
                self._snapshot_text(),
                encoding='utf-8',
            )
            call_command(
                'import_mythic_tooltip_snapshot',
                input=str(snapshot_path),
                version_key=self.version.key,
                expected_build=self.full_build,
                verbosity=0,
            )

        manual_spell.refresh_from_db()
        self.assertEqual(manual_spell.description_zh, '管理员手工说明。')
        self.assertEqual(
            manual_spell.metadata['description_quality'],
            QUALITY_MANUAL_OVERRIDE,
        )


class MythicPlannerAssetPersistenceTests(TestCase):
    def test_recovered_icon_uses_normalized_slug_and_clears_failure_marker(self):
        version = MythicDungeonDataVersion.objects.create(
            key='asset-icon-test',
            label='图标恢复测试',
            is_active=True,
        )
        spell = MythicDungeonSpell.objects.create(
            data_version=version,
            spell_id=1235656,
            icon_file_data_id=464484,
            icon_name='spell_frost_ring of frost',
            metadata={
                'asset_unavailable': True,
                'asset_unavailable_reason': 'HTTP 404',
            },
        )
        MythicDungeonSpell.objects.create(
            data_version=version,
            spell_id=1237330,
            icon_file_data_id=3480676,
            icon_name='inv_ misc_herb_marrowroot_leaf',
        )
        command = SyncMythicDungeonAssetsCommand()
        jobs, stats = command._build_jobs(
            version=version,
            base_prefix='mythic-planner',
            version_prefix='mythic-planner/versions/asset-icon-test',
            oss_base_url='https://oss.example/',
            force=True,
            spell_ids={1235656},
        )

        self.assertEqual(stats['spells'], 1)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0]['source_url'],
            (
                'https://wow.zamimg.com/images/wow/icons/large/'
                'spell_frost_ring-of-frost.jpg'
            ),
        )
        SyncMythicDungeonAssetsCommand._write_results(
            version,
            [(jobs[0], 'https://oss.example/wowhead/icons/frozen-tempest.jpg')],
            [],
            'mythic-planner/versions/asset-icon-test',
            'https://oss.example/',
        )

        spell.refresh_from_db()
        self.assertEqual(
            spell.icon_url,
            'https://oss.example/wowhead/icons/frozen-tempest.jpg',
        )
        self.assertNotIn('asset_unavailable', spell.metadata)
        self.assertNotIn('asset_unavailable_reason', spell.metadata)


class MythicPlannerPublicApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        import_mythic_dungeon_payload(demo_payload(), activate=True)

    @staticmethod
    def share_payload(*, name='测试路线', level=12):
        return {
            'version': 1,
            'dungeon_key': 'gloamvault',
            'data_version_key': 'lmonitor-demo-1',
            'name': name,
            'dungeon_level': level,
            'pulls': [{
                'id': 'pull-share',
                'name': '第一波',
                'color': '#e879f9',
                'spawn_uids': ['vault-guardian:guardian-01'],
            }],
            'annotations': [],
        }

    def test_catalog_and_dungeon_payload_include_planning_data(self):
        catalog_response = self.client.get('/portal/api/mythic-planner/catalog/')
        self.assertEqual(catalog_response.status_code, 200)
        catalog = catalog_response.json()['data']
        self.assertEqual(catalog['version']['key'], 'lmonitor-demo-1')
        self.assertEqual([row['key'] for row in catalog['dungeons']], ['gloamvault', 'emberworks'])

        dungeon_response = self.client.get('/portal/api/mythic-planner/dungeons/gloamvault/')
        self.assertEqual(dungeon_response.status_code, 200)
        dungeon = dungeon_response.json()['data']
        self.assertEqual(dungeon['total_enemy_forces'], 100)
        self.assertEqual(len(dungeon['floors']), 2)
        guardian = next(row for row in dungeon['enemies'] if row['key'] == 'vault-guardian')
        self.assertEqual(guardian['enemy_forces'], 5)
        self.assertTrue(guardian['abilities'])
        self.assertTrue(guardian['spawns'][0]['uid'].startswith('vault-guardian:'))

    def test_share_code_api_round_trip_and_rejects_unknown_spawn(self):
        dungeon = get_active_dungeon('gloamvault')
        payload = {
            'version': 1,
            'dungeon_key': 'gloamvault',
            'pulls': [{
                'id': 'pull-1',
                'name': '第一波',
                'color': '#ff00ff',
                'spawn_uids': ['vault-guardian:guardian-01'],
            }],
            'annotations': [],
        }
        validated = validate_route_payload(payload, dungeon)
        code = encode_share_code(validated.payload)
        self.assertEqual(decode_share_code(code)['dungeon_key'], 'gloamvault')

        response = self.client.post(
            '/portal/api/mythic-planner/share-code/',
            data=json.dumps({'action': 'decode', 'share_code': code}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)

        invalid = copy.deepcopy(payload)
        invalid['pulls'][0]['spawn_uids'] = ['vault-guardian:not-found']
        invalid_code = encode_share_code(invalid)
        response = self.client.post(
            '/portal/api/mythic-planner/share-code/',
            data=json.dumps({'action': 'decode', 'share_code': invalid_code}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('不存在的怪物刷新点', response.json()['message'])

    def test_anonymous_short_link_can_be_created_opened_and_loaded(self):
        cache.clear()
        created = self.client.post(
            '/portal/api/mythic-planner/share-links/',
            data=json.dumps({'route_data': self.share_payload()}),
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 200, created.content)
        data = created.json()['data']
        self.assertRegex(data['token'], r'^[A-Za-z0-9_-]{10,16}$')
        self.assertEqual(data['short_path'], f"/m/{data['token']}")
        self.assertTrue(data['share_code'].startswith('!LMDT1!'))
        self.assertEqual(MythicDungeonRouteShare.objects.count(), 1)

        page = self.client.get(data['short_path'])
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'data-share-token="{data["token"]}"')
        loaded = self.client.get(
            f"/portal/api/mythic-planner/share-links/{data['token']}/",
        )
        self.assertEqual(loaded.status_code, 200, loaded.content)
        self.assertEqual(loaded.json()['data']['route_data']['name'], '测试路线')
        share = MythicDungeonRouteShare.objects.get(token=data['token'])
        self.assertEqual(share.view_count, 1)
        self.assertIsNotNone(share.last_accessed_at)

    def test_short_link_deduplicates_validates_and_rate_limits(self):
        cache.clear()
        body = json.dumps({'route_data': self.share_payload()})
        first = self.client.post(
            '/portal/api/mythic-planner/share-links/',
            data=body,
            content_type='application/json',
        )
        second = self.client.post(
            '/portal/api/mythic-planner/share-links/',
            data=body,
            content_type='application/json',
        )
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(
            first.json()['data']['token'],
            second.json()['data']['token'],
        )
        self.assertEqual(MythicDungeonRouteShare.objects.count(), 1)

        invalid = self.share_payload(level=100)
        rejected = self.client.post(
            '/portal/api/mythic-planner/share-links/',
            data=json.dumps({'route_data': invalid}),
            content_type='application/json',
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn('2–99', rejected.json()['message'])

        cache.clear()
        with mock.patch(
            'botend.mythic_planner.api.SHORT_LINK_RATE_LIMIT',
            1,
        ):
            allowed = self.client.post(
                '/portal/api/mythic-planner/share-links/',
                data=body,
                content_type='application/json',
            )
            limited = self.client.post(
                '/portal/api/mythic-planner/share-links/',
                data=body,
                content_type='application/json',
            )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(limited.status_code, 429)

    def test_account_save_endpoints_are_disabled_but_old_public_links_remain(self):
        user = User.objects.create_user(username='legacy-route-user', password='pwd')
        dungeon = get_active_dungeon('gloamvault')
        payload = self.share_payload(name='旧公开路线')
        route = MythicDungeonRoute.objects.create(
            owner_user_id=user.id,
            dungeon=dungeon,
            name='旧公开路线',
            dungeon_level=12,
            route_data=payload,
            share_code=encode_share_code(payload),
            is_public=True,
        )
        self.client.force_login(user)
        self.assertEqual(
            self.client.get('/portal/api/mythic-planner/routes/').status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                '/portal/api/mythic-planner/routes/',
                data=json.dumps({'route_data': payload}),
                content_type='application/json',
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f'/portal/api/mythic-planner/routes/{route.id}/',
            ).status_code,
            404,
        )
        self.client.logout()
        legacy = self.client.get(
            f'/portal/api/mythic-planner/shared/{route.share_id}/',
        )
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.json()['data']['name'], '旧公开路线')


class MythicPlannerDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        import_mythic_dungeon_payload(demo_payload(), activate=True)
        cls.user = User.objects.create_user(username='normal-user', password='pwd')
        cls.staff = User.objects.create_user(username='staff-user', password='pwd', is_staff=True)

    def test_management_page_and_api_require_staff(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/dashboard/mythic-planner/').status_code, 403)
        self.assertEqual(
            self.client.get('/dashboard/mythic-planner/routes/').status_code,
            403,
        )
        self.assertEqual(
            self.client.get('/dashboard/mythic-planner/positions/').status_code,
            403,
        )
        self.assertEqual(self.client.get('/api/mythic-planner/manage/').status_code, 403)
        self.assertEqual(
            self.client.get('/api/mythic-planner/manage/1/?resource=routes').status_code,
            403,
        )

        self.client.force_login(self.staff)
        page = self.client.get('/dashboard/mythic-planner/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'MDT 数据与配置')
        self.assertContains(page, 'id="sidebar"')
        self.assertContains(page, 'id="user-menu-button"')
        self.assertContains(page, 'class="dashboard-shell')
        self.assertEqual(page.content.count(b'<!DOCTYPE html>'), 1)
        self.assertNotContains(page, 'class="mp-admin-header"')
        self.assertNotContains(page, 'data-resource="routes"')
        self.assertNotContains(page, 'id="import-builtin-mdt"')
        self.assertNotContains(page, 'id="spawn-map-editor"')
        self.assertContains(page, 'dashboard/js/dashboard_shell.js')
        self.assertNotContains(page, 'dashboard/js/main.js')
        self.assertNotContains(page, 'dashboard/js/simc-workbench.js')
        config_html = page.content.decode('utf-8')
        config_link = re.search(
            r'href="/dashboard/mythic-planner/" class="(?P<classes>[^"]+)"',
            config_html,
        )
        self.assertIsNotNone(config_link)
        self.assertIn('bg-blue-50', config_link.group('classes'))
        position_page = self.client.get('/dashboard/mythic-planner/positions/')
        self.assertEqual(position_page.status_code, 200)
        self.assertContains(position_page, '地图点位编辑')
        self.assertContains(position_page, 'id="spawn-map-editor"')
        self.assertContains(position_page, 'id="spawn-map-canvas"')
        self.assertContains(position_page, 'id="spawn-map-create"')
        self.assertContains(position_page, 'id="spawn-map-enemy"')
        self.assertNotContains(position_page, 'id="spawn-map-group-key"')
        self.assertContains(position_page, 'id="spawn-map-group-manage"')
        self.assertContains(position_page, 'id="spawn-map-group-inspector"')
        self.assertContains(position_page, 'id="spawn-group-list"')
        self.assertContains(position_page, 'id="spawn-group-create"')
        self.assertContains(position_page, 'id="spawn-group-assign"')
        self.assertContains(position_page, 'id="spawn-group-remove"')
        self.assertContains(position_page, 'id="spawn-group-restore"')
        self.assertContains(position_page, 'id="spawn-map-coordinates"')
        self.assertContains(position_page, 'id="spawn-map-save"')
        self.assertContains(position_page, '＋ 开始添加怪物')
        self.assertContains(position_page, '精确坐标（高级微调）')
        self.assertContains(position_page, 'id="sidebar"')
        self.assertContains(position_page, 'class="dashboard-shell')
        self.assertEqual(position_page.content.count(b'<!DOCTYPE html>'), 1)
        self.assertNotContains(position_page, 'class="mp-admin-header"')
        position_html = position_page.content.decode('utf-8')
        position_link = re.search(
            r'href="/dashboard/mythic-planner/positions/" class="(?P<classes>[^"]+)"',
            position_html,
        )
        self.assertIsNotNone(position_link)
        self.assertIn('bg-blue-50', position_link.group('classes'))
        route_page = self.client.get('/dashboard/mythic-planner/routes/')
        self.assertEqual(route_page.status_code, 200)
        self.assertContains(route_page, '账号路线 / MDT 字符串')
        self.assertContains(route_page, 'id="route-table-body"')
        self.assertContains(route_page, 'id="sidebar"')
        self.assertContains(route_page, 'id="user-menu-button"')
        self.assertContains(route_page, 'class="dashboard-shell')
        self.assertEqual(route_page.content.count(b'<!DOCTYPE html>'), 1)
        self.assertNotContains(route_page, 'class="mp-admin-header"')
        route_html = route_page.content.decode('utf-8')
        route_link = re.search(
            r'href="/dashboard/mythic-planner/routes/" class="(?P<classes>[^"]+)"',
            route_html,
        )
        self.assertIsNotNone(route_link)
        self.assertIn('bg-blue-50', route_link.group('classes'))
        legacy_route_page = self.client.get('/dashboard/mythic-planner/?resource=routes')
        self.assertEqual(legacy_route_page.status_code, 200)
        self.assertNotContains(legacy_route_page, 'id="route-table-body"')
        source_spell = MythicDungeonSpell.objects.first()
        source_spell.metadata = {
            **(source_spell.metadata or {}),
            'description_source': SOURCE_WOW_CLIENT,
            'description_quality': QUALITY_EXACT_RENDERED,
        }
        source_spell.save(update_fields=['metadata', 'updated_at'])
        snapshot = self.client.get('/api/mythic-planner/manage/')
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()['data']['counts']['dungeons'], 2)
        self.assertGreater(snapshot.json()['data']['counts']['spells'], 0)
        self.assertTrue(snapshot.json()['data']['abilities'][0]['display_name'])
        serialized_spell = next(
            row
            for row in snapshot.json()['data']['spells']
            if row['id'] == source_spell.id
        )
        self.assertEqual(
            serialized_spell['description_source'],
            SOURCE_WOW_CLIENT,
        )
        self.assertEqual(
            serialized_spell['description_quality'],
            QUALITY_EXACT_RENDERED,
        )

        scoped_snapshot = self.client.get(
            '/api/mythic-planner/manage/'
            '?resources=versions,dungeons,floors,enemies,spawns',
        )
        self.assertEqual(scoped_snapshot.status_code, 200)
        scoped_data = scoped_snapshot.json()['data']
        self.assertEqual(scoped_data['spells'], [])
        self.assertEqual(scoped_data['abilities'], [])
        self.assertEqual(scoped_data['routes'], [])
        self.assertGreater(len(scoped_data['spawns']), 0)
        self.assertLess(len(scoped_snapshot.content), len(snapshot.content))

        dungeon_id = scoped_data['dungeons'][0]['id']
        dungeon_snapshot = self.client.get(
            '/api/mythic-planner/manage/'
            f'?resources=floors,enemies,spawns&dungeon_id={dungeon_id}',
        )
        self.assertEqual(dungeon_snapshot.status_code, 200)
        dungeon_data = dungeon_snapshot.json()['data']
        self.assertTrue(dungeon_data['floors'])
        self.assertTrue(dungeon_data['enemies'])
        enemy_ids = {row['id'] for row in dungeon_data['enemies']}
        self.assertTrue(all(
            row['dungeon_id'] == dungeon_id
            for row in dungeon_data['floors']
        ))
        self.assertTrue(all(
            row['dungeon_id'] == dungeon_id
            for row in dungeon_data['enemies']
        ))
        self.assertTrue(all(
            row['enemy_id'] in enemy_ids
            for row in dungeon_data['spawns']
        ))
        self.assertLess(
            len(dungeon_snapshot.content),
            len(scoped_snapshot.content),
        )

        dashboard_home = self.client.get('/dashboard/')
        self.assertContains(dashboard_home, 'dashboard/js/main.js')
        self.assertNotContains(dashboard_home, 'dashboard/js/dashboard_shell.js')

    def test_planner_frontend_does_not_expose_management_entry(self):
        anonymous = self.client.get('/portal/mythic-planner/')
        self.assertNotContains(anonymous, 'id="open-planner-admin"')

        self.client.force_login(self.user)
        normal_user = self.client.get('/portal/mythic-planner/')
        self.assertNotContains(normal_user, 'id="open-planner-admin"')

        self.client.force_login(self.staff)
        staff_user = self.client.get('/portal/mythic-planner/')
        self.assertNotContains(staff_user, 'id="open-planner-admin"')

    def test_staff_can_manage_account_routes_and_read_payload_on_demand(self):
        owner = User.objects.create_user(
            username='managed-route-owner',
            email='route-owner@example.com',
            password='pwd',
        )
        dungeon = MythicDungeon.objects.get(key='gloamvault')
        route_data = {
            'version': 1,
            'dungeon_key': dungeon.key,
            'pulls': [
                {
                    'id': 'pull-1',
                    'name': '第一波',
                    'spawn_uids': ['vault-guardian:guardian-01'],
                },
                {
                    'id': 'pull-2',
                    'name': '第二波',
                    'spawn_uids': [],
                },
            ],
            'annotations': [{'id': 'note-1', 'text': '跳怪'}],
        }
        route = MythicDungeonRoute.objects.create(
            owner_user_id=owner.id,
            dungeon=dungeon,
            name='后台管理测试路线',
            dungeon_level=17,
            route_data=route_data,
            share_code=encode_share_code(route_data),
            is_public=True,
        )

        self.client.force_login(self.staff)
        snapshot_response = self.client.get('/api/mythic-planner/manage/')
        self.assertEqual(snapshot_response.status_code, 200)
        snapshot = snapshot_response.json()['data']
        managed = next(row for row in snapshot['routes'] if row['id'] == route.id)
        self.assertEqual(snapshot['counts']['routes'], 1)
        self.assertEqual(managed['owner_username'], owner.username)
        self.assertEqual(managed['owner_email'], owner.email)
        self.assertEqual(managed['pull_count'], 2)
        self.assertEqual(managed['spawn_count'], 1)
        self.assertEqual(managed['annotation_count'], 1)
        self.assertNotIn('route_data', managed)
        self.assertNotIn('share_code', managed)

        detail = self.client.get(
            f'/api/mythic-planner/manage/{route.id}/?resource=routes',
        )
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()['data']['route_data'], route_data)
        self.assertTrue(detail.json()['data']['share_code'])

        private = self.client.patch(
            f'/api/mythic-planner/manage/{route.id}/',
            data=json.dumps({
                'resource': 'routes',
                'data': {'is_public': False},
            }),
            content_type='application/json',
        )
        self.assertEqual(private.status_code, 200, private.content)
        route.refresh_from_db()
        self.assertFalse(route.is_public)
        self.assertEqual(route.revision, 2)

        archived = self.client.delete(
            f'/api/mythic-planner/manage/{route.id}/',
            data=json.dumps({'resource': 'routes'}),
            content_type='application/json',
        )
        self.assertEqual(archived.status_code, 200, archived.content)
        route.refresh_from_db()
        self.assertFalse(route.is_active)

        restored = self.client.patch(
            f'/api/mythic-planner/manage/{route.id}/',
            data=json.dumps({
                'resource': 'routes',
                'data': {'is_active': True},
            }),
            content_type='application/json',
        )
        self.assertEqual(restored.status_code, 200, restored.content)
        route.refresh_from_db()
        self.assertTrue(route.is_active)

        config = MythicPlannerConfig.objects.get(key='default')
        config.allow_public_route_share = False
        config.save(update_fields=['allow_public_route_share', 'updated_at'])
        blocked_public = self.client.patch(
            f'/api/mythic-planner/manage/{route.id}/',
            data=json.dumps({
                'resource': 'routes',
                'data': {'is_public': True},
            }),
            content_type='application/json',
        )
        self.assertEqual(blocked_public.status_code, 400)
        self.assertIn('关闭公开路线分享', blocked_public.json()['message'])

    def test_staff_can_update_config_and_import_new_version(self):
        self.client.force_login(self.staff)
        config = MythicPlannerConfig.objects.get(key='default')
        patched = self.client.patch(
            f'/api/mythic-planner/manage/{config.id}/',
            data=json.dumps({
                'resource': 'configs',
                'data': {
                    'default_dungeon_key': 'emberworks',
                    'default_dungeon_level': 15,
                    'allow_public_route_share': False,
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(patched.status_code, 200, patched.content)
        config.refresh_from_db()
        self.assertEqual(config.default_dungeon_key, 'emberworks')
        self.assertEqual(config.default_dungeon_level, 15)
        self.assertFalse(config.allow_public_route_share)

        payload = demo_payload()
        payload['data_version']['key'] = 'lmonitor-demo-2'
        payload['data_version']['label'] = '第二数据版本'
        imported = self.client.post(
            '/api/mythic-planner/manage/',
            data=json.dumps({
                'resource': 'import',
                'data': {'payload': payload, 'activate': True, 'replace': False},
            }),
            content_type='application/json',
        )
        self.assertEqual(imported.status_code, 200, imported.content)
        self.assertTrue(MythicDungeonDataVersion.objects.get(key='lmonitor-demo-2').is_active)
        self.assertFalse(MythicDungeonDataVersion.objects.get(key='lmonitor-demo-1').is_active)

    def test_staff_can_lock_restore_and_reimport_spawn_position(self):
        self.client.force_login(self.staff)
        spawn = MythicDungeonSpawn.objects.select_related(
            'enemy__dungeon',
            'floor',
        ).get(
            enemy__dungeon__key='gloamvault',
            enemy__key='vault-guardian',
            key='guardian-01',
        )
        original_position = {
            'floor_key': spawn.floor.key,
            'x': spawn.x,
            'y': spawn.y,
        }

        patched = self.client.patch(
            f'/api/mythic-planner/manage/{spawn.id}/',
            data=json.dumps({
                'resource': 'spawns',
                'data': {
                    'floor_id': spawn.floor_id,
                    'x': 42.25,
                    'y': 63.75,
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(patched.status_code, 200, patched.content)
        spawn.refresh_from_db()
        self.assertEqual(spawn.x, 42.25)
        self.assertEqual(spawn.y, 63.75)
        self.assertTrue(spawn.metadata['manual_position_override'])
        self.assertEqual(spawn.metadata['imported_position'], original_position)
        returned = next(
            row
            for row in patched.json()['snapshot']['spawns']
            if row['id'] == spawn.id
        )
        self.assertTrue(returned['is_position_manual'])
        self.assertEqual(returned['imported_position'], original_position)

        updated_payload = demo_payload()
        source_spawn = next(
            spawn_data
            for dungeon_data in updated_payload['dungeons']
            if dungeon_data['key'] == 'gloamvault'
            for enemy_data in dungeon_data['enemies']
            if enemy_data['key'] == 'vault-guardian'
            for spawn_data in enemy_data['spawns']
            if spawn_data['key'] == 'guardian-01'
        )
        source_spawn['x'] = 81.5
        source_spawn['y'] = 27.25
        import_mythic_dungeon_payload(updated_payload, activate=True)
        spawn.refresh_from_db()
        self.assertEqual(spawn.x, 42.25)
        self.assertEqual(spawn.y, 63.75)
        self.assertEqual(
            spawn.metadata['imported_position'],
            {
                'floor_key': original_position['floor_key'],
                'x': 81.5,
                'y': 27.25,
            },
        )

        restored = self.client.patch(
            f'/api/mythic-planner/manage/{spawn.id}/',
            data=json.dumps({'resource': 'spawn_position_reset'}),
            content_type='application/json',
        )
        self.assertEqual(restored.status_code, 200, restored.content)
        spawn.refresh_from_db()
        self.assertEqual(spawn.x, 81.5)
        self.assertEqual(spawn.y, 27.25)
        self.assertNotIn('manual_position_override', spawn.metadata)
        reset_row = next(
            row
            for row in restored.json()['snapshot']['spawns']
            if row['id'] == spawn.id
        )
        self.assertFalse(reset_row['is_position_manual'])

    def test_spawn_position_rejects_coordinates_outside_map(self):
        self.client.force_login(self.staff)
        spawn = MythicDungeonSpawn.objects.first()
        rejected = self.client.patch(
            f'/api/mythic-planner/manage/{spawn.id}/',
            data=json.dumps({
                'resource': 'spawns',
                'data': {'x': 100.1, 'y': -0.1},
            }),
            content_type='application/json',
        )
        self.assertEqual(rejected.status_code, 400, rejected.content)
        self.assertIn('必须在 0 到 100 之间', rejected.json()['message'])

    def test_staff_can_create_spawn_and_change_enemy(self):
        self.client.force_login(self.staff)
        dungeon = MythicDungeon.objects.get(key='gloamvault')
        floor = MythicDungeonFloor.objects.filter(dungeon=dungeon).first()
        enemies = list(
            MythicDungeonEnemy.objects.filter(dungeon=dungeon).order_by('id')[:2],
        )
        self.assertGreaterEqual(len(enemies), 2)

        created = self.client.post(
            '/api/mythic-planner/manage/',
            data=json.dumps({
                'resource': 'spawns',
                'data': {
                    'enemy_id': enemies[0].id,
                    'floor_id': floor.id,
                    'key': 'manual-linked-spawn',
                    'x': 32.5,
                    'y': 47.25,
                    'scale': 1.15,
                    'patrol': [],
                    'is_active': True,
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 200, created.content)
        spawn = MythicDungeonSpawn.objects.get(
            enemy=enemies[0],
            key='manual-linked-spawn',
        )
        self.assertTrue(spawn.metadata['manual_position_override'])
        self.assertEqual(spawn.group_key, '')

        relinked = self.client.patch(
            f'/api/mythic-planner/manage/{spawn.id}/',
            data=json.dumps({
                'resource': 'spawns',
                'data': {
                    'enemy_id': enemies[1].id,
                    'floor_id': floor.id,
                    'key': spawn.key,
                    'x': spawn.x,
                    'y': spawn.y,
                    'scale': 0.9,
                    'patrol': [],
                    'is_active': True,
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(relinked.status_code, 200, relinked.content)
        spawn.refresh_from_db()
        self.assertEqual(spawn.enemy_id, enemies[1].id)
        self.assertEqual(spawn.group_key, '')
        self.assertEqual(spawn.scale, 0.9)

        other_enemy = MythicDungeonEnemy.objects.exclude(
            dungeon=dungeon,
        ).first()
        rejected = self.client.patch(
            f'/api/mythic-planner/manage/{spawn.id}/',
            data=json.dumps({
                'resource': 'spawns',
                'data': {'enemy_id': other_enemy.id},
            }),
            content_type='application/json',
        )
        self.assertEqual(rejected.status_code, 400, rejected.content)
        self.assertIn('必须属于同一个地下城', rejected.json()['message'])

    def test_staff_can_manage_spawn_groups_and_restore_imported_group(self):
        self.client.force_login(self.staff)
        dungeon = MythicDungeon.objects.get(key='gloamvault')
        outer_floor = MythicDungeonFloor.objects.get(
            dungeon=dungeon,
            key='outer-halls',
        )
        guardians = {
            spawn.key: spawn
            for spawn in MythicDungeonSpawn.objects.filter(
                enemy__dungeon=dungeon,
                enemy__key='vault-guardian',
                floor=outer_floor,
            )
        }
        selected = [
            guardians['guardian-01'],
            guardians['guardian-03'],
        ]
        created = self.client.post(
            '/api/mythic-planner/manage/',
            data=json.dumps({
                'resource': 'spawn_groups',
                'snapshot_resources': ['floors', 'enemies', 'spawns'],
                'snapshot_dungeon_id': dungeon.id,
                'data': {
                    'action': 'create',
                    'spawn_ids': [spawn.id for spawn in selected],
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 200, created.content)
        created_data = created.json()['data']
        self.assertEqual(created_data['group_key'], 'manual-group-1')
        self.assertEqual(created_data['updated'], 2)
        for spawn, imported_group_key in zip(
            selected,
            ['hall-a', 'hall-b'],
        ):
            spawn.refresh_from_db()
            self.assertEqual(spawn.group_key, 'manual-group-1')
            self.assertTrue(spawn.metadata['manual_group_override'])
            self.assertEqual(
                spawn.metadata['imported_group_key'],
                imported_group_key,
            )
        returned = {
            row['id']: row
            for row in created.json()['snapshot']['spawns']
        }
        self.assertTrue(returned[selected[0].id]['is_group_manual'])
        self.assertEqual(
            returned[selected[0].id]['imported_group_key'],
            'hall-a',
        )

        joined_spawn = guardians['guardian-05']
        joined = self.client.patch(
            '/api/mythic-planner/manage/',
            data=json.dumps({
                'resource': 'spawn_groups',
                'data': {
                    'action': 'assign',
                    'spawn_ids': [joined_spawn.id],
                    'group_key': 'manual-group-1',
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(joined.status_code, 200, joined.content)
        joined_spawn.refresh_from_db()
        self.assertEqual(joined_spawn.group_key, 'manual-group-1')
        self.assertEqual(
            joined_spawn.metadata['imported_group_key'],
            'hall-c',
        )

        removed = self.client.patch(
            '/api/mythic-planner/manage/',
            data=json.dumps({
                'resource': 'spawn_groups',
                'data': {
                    'action': 'remove',
                    'spawn_ids': [selected[0].id],
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(removed.status_code, 200, removed.content)
        selected[0].refresh_from_db()
        self.assertEqual(selected[0].group_key, '')
        self.assertTrue(selected[0].metadata['manual_group_override'])

        updated_payload = demo_payload()
        source_spawn = next(
            spawn_data
            for dungeon_data in updated_payload['dungeons']
            if dungeon_data['key'] == 'gloamvault'
            for enemy_data in dungeon_data['enemies']
            if enemy_data['key'] == 'vault-guardian'
            for spawn_data in enemy_data['spawns']
            if spawn_data['key'] == 'guardian-01'
        )
        source_spawn['group_key'] = 'hall-updated'
        import_mythic_dungeon_payload(updated_payload, activate=True)
        selected[0].refresh_from_db()
        self.assertEqual(selected[0].group_key, '')
        self.assertEqual(
            selected[0].metadata['imported_group_key'],
            'hall-updated',
        )

        restored = self.client.patch(
            '/api/mythic-planner/manage/',
            data=json.dumps({
                'resource': 'spawn_groups',
                'data': {
                    'action': 'restore',
                    'spawn_ids': [selected[0].id],
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(restored.status_code, 200, restored.content)
        selected[0].refresh_from_db()
        self.assertEqual(selected[0].group_key, 'hall-updated')
        self.assertNotIn('manual_group_override', selected[0].metadata)
        self.assertNotIn('imported_group_key', selected[0].metadata)

        other_floor_spawn = MythicDungeonSpawn.objects.get(
            enemy__dungeon=dungeon,
            enemy__key='umbral-hound',
            key='hound-03',
        )
        rejected = self.client.patch(
            '/api/mythic-planner/manage/',
            data=json.dumps({
                'resource': 'spawn_groups',
                'data': {
                    'action': 'create',
                    'spawn_ids': [selected[1].id, other_floor_spawn.id],
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(rejected.status_code, 400, rejected.content)
        self.assertIn('同一个楼层', rejected.json()['message'])

    def test_staff_can_edit_shared_spell_library(self):
        self.client.force_login(self.staff)
        spell = MythicDungeonSpell.objects.first()

        patched = self.client.patch(
            f'/api/mythic-planner/manage/{spell.id}/',
            data=json.dumps({
                'resource': 'spells',
                'data': {
                    'name_zh': '后台修订技能',
                    'description_zh': '后台维护的技能说明。',
                    'snapshot_build': 'manual',
                },
            }),
            content_type='application/json',
        )

        self.assertEqual(patched.status_code, 200, patched.content)
        spell.refresh_from_db()
        self.assertEqual(spell.name_zh, '后台修订技能')
        self.assertEqual(spell.description_zh, '后台维护的技能说明。')
        self.assertEqual(
            spell.metadata['description_source'],
            SOURCE_MANUAL,
        )
        self.assertEqual(
            spell.metadata['description_quality'],
            QUALITY_MANUAL_OVERRIDE,
        )
        returned = next(
            row
            for row in patched.json()['snapshot']['spells']
            if row['id'] == spell.id
        )
        self.assertEqual(returned['display_name'], '后台修订技能')

    def test_staff_can_create_season_and_assign_dungeon(self):
        self.client.force_login(self.staff)
        version = MythicDungeonDataVersion.objects.get(key='lmonitor-demo-1')
        dungeon = MythicDungeon.objects.get(
            data_version=version,
            key='gloamvault',
        )
        created_group = self.client.post(
            '/api/mythic-planner/manage/',
            data=json.dumps({
                'resource': 'selection_groups',
                'data': {
                    'data_version_id': version.id,
                    'key': 'midnight-season-3',
                    'name': 'Midnight Season 3',
                    'name_zh': '至暗之夜 第三赛季',
                    'order': 3,
                    'is_active': True,
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(created_group.status_code, 200, created_group.content)
        group_id = created_group.json()['data']['id']

        assigned = self.client.post(
            '/api/mythic-planner/manage/',
            data=json.dumps({
                'resource': 'selection_memberships',
                'data': {
                    'selection_group_id': group_id,
                    'dungeon_id': dungeon.id,
                    'order': 1,
                    'is_active': True,
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(assigned.status_code, 200, assigned.content)
        membership = MythicDungeonSelectionMembership.objects.get(
            selection_group_id=group_id,
            dungeon=dungeon,
        )
        self.assertEqual(membership.order, 1)
        snapshot = assigned.json()['snapshot']
        self.assertEqual(snapshot['counts']['selection_groups'], 1)
        self.assertEqual(snapshot['counts']['selection_memberships'], 1)

        other_version = MythicDungeonDataVersion.objects.create(
            key='other-version',
            label='其他版本',
        )
        other_group = MythicDungeonSelectionGroup.objects.create(
            data_version=other_version,
            key='other-season',
            name='Other Season',
        )
        rejected = self.client.post(
            '/api/mythic-planner/manage/',
            data=json.dumps({
                'resource': 'selection_memberships',
                'data': {
                    'selection_group_id': other_group.id,
                    'dungeon_id': dungeon.id,
                    'order': 1,
                    'is_active': True,
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(rejected.status_code, 400, rejected.content)
        self.assertIn('同一个数据版本', rejected.json()['message'])

class MythicPlannerPageContractTests(SimpleTestCase):
    def test_direct_route_exists_and_portal_navigation_exposes_mdt(self):
        planner = self.client.get('/portal/mythic-planner/')
        self.assertEqual(planner.status_code, 200)
        self.assertContains(planner, 'id="planner-app"')
        self.assertContains(planner, 'id="map-viewport"')
        self.assertContains(planner, 'id="pull-list"')
        self.assertContains(planner, 'id="season-select"')
        self.assertContains(planner, 'id="enemy-detail-modal"')
        self.assertContains(planner, 'data-close-enemy-detail')
        self.assertContains(planner, 'id="share-route"')
        self.assertContains(planner, 'data-share-token=""')
        self.assertContains(
            planner,
            '<a href="/" class="mdt-icon-button mdt-home-button" title="返回首页">返回首页</a>',
            html=True,
        )
        self.assertNotContains(planner, 'id="save-server-route"')
        self.assertNotContains(planner, 'data-authenticated=')
        self.assertNotContains(planner, '登录后云端保存')
        self.assertNotContains(planner, 'id="enemy-tooltip"')
        self.assertNotContains(planner, '将鼠标移到怪物上')

        portal = self.client.get('/')
        self.assertEqual(portal.status_code, 200)
        self.assertContains(portal, 'href="/portal/mythic-planner/"')
        self.assertContains(portal, '<span>MDT</span>', html=True)

        dashboard_template = (
            Path(settings.BASE_DIR)
            / 'templates'
            / 'dashboard'
            / 'index.html'
        ).read_text(encoding='utf-8')
        self.assertIn(
            'href="/dashboard/mythic-planner/routes/"',
            dashboard_template,
        )
        self.assertIn(
            'href="/dashboard/mythic-planner/positions/"',
            dashboard_template,
        )
        self.assertIn('地图点位编辑', dashboard_template)
        self.assertIn('账号路线 / MDT 字符串', dashboard_template)

        planner_dashboard_template = (
            Path(settings.BASE_DIR)
            / 'templates'
            / 'dashboard'
            / 'mythic_planner.html'
        ).read_text(encoding='utf-8')
        self.assertIn('data-resource="selection_groups"', planner_dashboard_template)
        self.assertIn(
            'data-resource="selection_memberships"',
            planner_dashboard_template,
        )
        self.assertNotIn('data-resource="routes"', planner_dashboard_template)

        position_dashboard_template = (
            Path(settings.BASE_DIR)
            / 'templates'
            / 'dashboard'
            / 'mythic_planner_positions.html'
        ).read_text(encoding='utf-8')
        self.assertIn('dashboard/js/mythic_planner_positions.js', position_dashboard_template)

        route_dashboard_template = (
            Path(settings.BASE_DIR)
            / 'templates'
            / 'dashboard'
            / 'mythic_planner_routes.html'
        ).read_text(encoding='utf-8')
        self.assertIn('账号路线 / MDT 字符串', route_dashboard_template)
        self.assertIn('id="route-detail-modal"', route_dashboard_template)
        self.assertIn('id="route-owner-filter"', route_dashboard_template)
        self.assertIn('id="route-share-filter"', route_dashboard_template)
        self.assertIn('dashboard/js/mythic_planner_routes.js', route_dashboard_template)

    def test_frontend_sources_expose_core_interactions(self):
        portal_js = (
            Path(settings.BASE_DIR) / 'static' / 'portal' / 'js' / 'mythic_planner.js'
        ).read_text(encoding='utf-8')
        planner_template = (
            Path(settings.BASE_DIR) / 'templates' / 'portal' / 'mythic_planner.html'
        ).read_text(encoding='utf-8')
        planner_css = (
            Path(settings.BASE_DIR) / 'static' / 'portal' / 'css' / 'mythic_planner.css'
        ).read_text(encoding='utf-8')
        dashboard_js = (
            Path(settings.BASE_DIR) / 'static' / 'dashboard' / 'js' / 'mythic_planner.js'
        ).read_text(encoding='utf-8')
        self.assertNotIn('importBuiltinMdt', dashboard_js)
        self.assertNotIn('import-builtin-mdt', dashboard_js)
        route_dashboard_js = (
            Path(settings.BASE_DIR)
            / 'static'
            / 'dashboard'
            / 'js'
            / 'mythic_planner_routes.js'
        ).read_text(encoding='utf-8')
        position_dashboard_js = (
            Path(settings.BASE_DIR)
            / 'static'
            / 'dashboard'
            / 'js'
            / 'mythic_planner_positions.js'
        ).read_text(encoding='utf-8')
        dashboard_shell_js = (
            Path(settings.BASE_DIR)
            / 'static'
            / 'dashboard'
            / 'js'
            / 'dashboard_shell.js'
        ).read_text(encoding='utf-8')
        for token in (
            'toggleSpawn',
            'selectBox',
            'renderProgress',
            'encodeShareLocal',
            'BroadcastChannel',
            'shareRoute',
            '/portal/api/mythic-planner/share-links/',
            'stripAccountMetadata',
            '复制短链接',
            'sharedRouteRequest',
            'source_share_key',
            'replaceSharedRouteUrl',
            'syncDungeonUrl',
            'window.history.replaceState',
            '导入当前浏览器成为可编辑副本',
            '已打开当前浏览器中的路线',
            'mdt-spawn-initial',
            'dungeonsForSelectionGroup',
            'shouldStartMapPan',
            'mdt-ability-icon',
            'openEnemyDetail',
            'closeEnemyDetail',
            'nextPullColor',
            "addEventListener('contextmenu'",
        ):
            self.assertIn(token, portal_js)
        sync_url = portal_js[
            portal_js.index('function syncDungeonUrl'):
            portal_js.index('function normalizeRoute')
        ]
        self.assertIn('sharedRouteRequest()', sync_url)
        self.assertIn("url.searchParams.set('dungeon', dungeonKey)", sync_url)
        self.assertIn('${url.pathname}${url.search}${url.hash}', sync_url)
        load_dungeon = portal_js[
            portal_js.index('async function loadDungeon'):
            portal_js.index('function renderAll')
        ]
        self.assertIn('syncDungeonUrl(dungeon.key)', load_dungeon)
        self.assertIn('进度 · ${forcesPercent.toFixed(2)}%', portal_js)
        self.assertNotIn('${formatNumber(stats.health)} HP', portal_js)
        self.assertNotIn('打开这份只读路线快照', portal_js)
        self.assertIn('reorderPull', portal_js)
        self.assertIn('onPullPointerMove', portal_js)
        self.assertIn('function selectPull', portal_js)
        self.assertIn('function suppressNextPullClick', portal_js)
        self.assertIn(
            'if (!cancelled && !drag.moved) {',
            portal_js,
        )
        self.assertIn('selectPull(drag.pullId);', portal_js)
        self.assertIn('selectPull(article.dataset.pullId);', portal_js)
        self.assertIn(
            "els.pullList.addEventListener('keydown'",
            portal_js,
        )
        for token in (
            'function spawnMarkerSize',
            'function spawnOutlineRadius',
            'function circleBoundaryPoints',
            'function convexHull',
            'function roundedPolygonPath',
            'function pullAreaMarkup',
            'function renderPullArea',
            "window.addEventListener('resize', renderPullArea)",
        ):
            self.assertIn(token, portal_js)
        self.assertIn('const PULL_AREA_PADDING_PX = 1', portal_js)
        self.assertIn('const PULL_AREA_SELECTED_RING_PX = 2', portal_js)
        self.assertIn(
            'circleBoundaryPoints(points[index], outlineRadii[index])',
            portal_js,
        )
        self.assertNotIn('PULL_AREA_PADDING_PX + Math.max', portal_js)
        self.assertNotIn('renderRouteLines', portal_js)
        self.assertNotIn('route-lines-layer', planner_template)
        self.assertNotIn('mdt-route-line', planner_css)
        select_pull = portal_js[
            portal_js.index('function selectPull'):
            portal_js.index('function suppressNextPullClick')
        ]
        self.assertIn('renderPulls();', select_pull)
        self.assertIn('renderPullArea();', select_pull)
        self.assertIn('id="pull-area-layer"', planner_template)
        self.assertIn(
            'const pullAreas = (state.route.pulls || []).map(',
            portal_js,
        )
        self.assertIn(
            'class="mdt-pull-area${isCurrent ? \' is-current\' : \'\'}"',
            portal_js,
        )
        render_pull_area = portal_js[
            portal_js.index('function renderPullArea'):
            portal_js.index('function renderPois')
        ]
        self.assertNotIn('const pull = currentPull();', render_pull_area)
        self.assertIn('.mdt-pull-area.is-current', planner_css)
        self.assertIn(
            '.mdt-pull-area:not(.is-current) .mdt-pull-area-shape',
            planner_css,
        )
        self.assertIn('mdt-pull-area-shape', planner_css)
        self.assertIn('mdt-pull-area-label', planner_css)
        toggle_spawn = portal_js[
            portal_js.index('function toggleSpawn'):
            portal_js.index('function selectBox')
        ]
        self.assertIn(
            'const selectedInCurrentPull = uids.some(',
            toggle_spawn,
        )
        self.assertIn('if (!selectedInCurrentPull)', toggle_spawn)
        self.assertNotIn('const existingPull = pullForUid(uid)', toggle_spawn)
        self.assertIn(
            'aria-current="${isCurrent ? \'true\' : \'false\'}"',
            portal_js,
        )
        self.assertIn(
            '.filter(([key]) => Boolean(enemy.traits?.[key]))',
            portal_js,
        )
        self.assertIn('const markerSize = spawnMarkerSize(spawn)', portal_js)
        self.assertIn('clamp(baseMarkerSize * 0.55, 4, 13) + 1', portal_js)
        self.assertNotIn('data-pull-action="up"', portal_js)
        self.assertNotIn('data-pull-action="down"', portal_js)
        self.assertNotIn('data-pull-action="rename"', portal_js)
        self.assertNotIn(
            "els.spawnLayer.addEventListener('pointerover'",
            portal_js,
        )
        self.assertIn(
            'defaultPull(route.pulls.length, nextPullColor(route.pulls))',
            portal_js,
        )
        for token in (
            'RESOURCE_CONFIG',
            'submitImport',
            'archiveRow',
            'saveEditor',
            'selection_groups',
            'selection_memberships',
            "'selection-group'",
            "dungeon_keys: ['dungeon-key']",
        ):
            self.assertIn(token, dashboard_js)
        for token in (
            'beginCreate',
            'beginDrag',
            'createPositionAt',
            'nextManualKey',
            'savePosition',
            'resetPosition',
            'beginGroupManage',
            'toggleGroupSpawn',
            'selectSpawnGroup',
            'updateSpawnGroups',
            "resource: 'spawn_groups'",
            "resource: 'spawn_position_reset'",
            "resource: 'spawns'",
            'spawn-map-enemy',
            'spawn-map-group-manage',
            'spawn-map-group-inspector',
            'spawn-group-list',
            'els.coordinates.hidden = creating',
            'els.save.hidden = creating',
            'createPositionAt(positionOnMap(event))',
        ):
            self.assertIn(token, position_dashboard_js)
        self.assertNotIn('spawn-map-group-key', position_dashboard_js)
        self.assertNotIn('group_key: els.groupKey', position_dashboard_js)
        self.assertIn(
            "if (state.mode === 'create') {\n"
            "                createPositionAt(positionOnMap(event));\n"
            "            }",
            position_dashboard_js,
        )
        for token in (
            'bindDashboardLinks',
            'bindSubmenus',
            'bindMobileSidebar',
            'bindUserMenu',
            '/dashboard/?',
        ):
            self.assertIn(token, dashboard_shell_js)
        self.assertIn('POSITION_DIRECTORY_RESOURCES', position_dashboard_js)
        self.assertIn('POSITION_DUNGEON_RESOURCES', position_dashboard_js)
        self.assertIn('ROUTE_SNAPSHOT_RESOURCES', route_dashboard_js)
        self.assertIn('CONFIG_RESOURCE_DEPENDENCIES', dashboard_js)
        self.assertIn('snapshot_resources', dashboard_js)
        for token in (
            'openRouteDetail',
            'toggleRoutePublic',
            'toggleRouteActive',
            'route-owner-filter',
            'data-copy-route-code',
            "resource: 'routes'",
        ):
            self.assertIn(token, route_dashboard_js)
        for token in (
            'refreshServerRoutes',
            'loadServerRoute',
            'toggleServerRouteVisibility',
            'copyServerRouteLink',
            'deleteServerRoute',
            'syncLocalServerMetadata',
            'error.status === 404',
            "cookieValue('csrftoken')",
            "'X-CSRFToken'",
            'data-load-server-route',
            'data-toggle-server-public',
            'data-copy-server-link',
            'data-delete-server-route',
            'pendingServerDeleteId',
            '再次确认删除',
            '我的账号',
            '/portal/api/mythic-planner/routes/',
            'saveServerRoute',
            'serverRoutes',
            '登录后云端保存',
        ):
            self.assertNotIn(token, portal_js)
        self.assertIn('当前浏览器', portal_js)
        self.assertIn('delete cleaned.server_share_id', portal_js)

    def test_spawn_markers_do_not_render_a_black_crescent(self):
        portal_css = (
            Path(settings.BASE_DIR) / 'static' / 'portal' / 'css' / 'mythic_planner.css'
        ).read_text(encoding='utf-8')

        self.assertIn('--spawn-background:', portal_css)
        self.assertIn('.mdt-spawn:focus-visible', portal_css)
        self.assertIn('-webkit-text-stroke-width: var(--spawn-outline-size)', portal_css)
        self.assertIn('paint-order: stroke fill', portal_css)
        self.assertIn('color-scheme: dark', portal_css)
        self.assertIn('select option:checked', portal_css)
        self.assertIn('.mdt-enemy-dialog', portal_css)
        self.assertIn('.mdt-enemy-detail-layout', portal_css)
        self.assertNotIn('#101820 58%', portal_css)
        self.assertNotIn('0 0 0 1px #101010', portal_css)
