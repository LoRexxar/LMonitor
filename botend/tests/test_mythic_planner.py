import copy
import json
import re
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase

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
from botend.management.commands.sync_mythic_dungeon_spells import (
    Command as SyncMythicDungeonSpellsCommand,
)


def demo_payload():
    path = Path(settings.BASE_DIR) / 'botend' / 'data' / 'mythic_planner' / 'demo_v1.json'
    return json.loads(path.read_text(encoding='utf-8'))


class MythicPlannerImportTests(TestCase):
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
        spell.save()

        payload = serialize_ability(
            MythicDungeonAbility.objects.select_related('spell_record').get(pk=ability.pk)
        )

        self.assertEqual(payload['display_name'], '已解析技能')
        self.assertEqual(payload['description_zh'], '来自公共技能资料表的完整说明。')
        self.assertEqual(payload['icon_url'], 'https://example.com/icon.jpg')

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
            / 'mythic-dungeon-tools-6.2.0-alpha3'
        )

    def test_fixed_upstream_snapshot_converts_real_dungeons_and_assets(self):
        payload = build_payload(self.source_root())

        self.assertEqual(payload['data_version']['key'], 'mdt-6-2-0-alpha3')
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
            '/static/portal/mythic_planner/vendor/mdt-6.2.0-alpha3/maps/',
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
            '对一名敌人造成3495点暗影伤害。\n'
            '每2秒对一名敌人造成2330点暗影伤害，持续8秒。',
        )
        self.assertEqual(
            sum(
                len(enemy['abilities'])
                for dungeon in payload['dungeons']
                for enemy in dungeon['enemies']
            ),
            1648,
        )

    def test_lua_parser_does_not_execute_identifiers_or_function_calls(self):
        with self.assertRaises(LuaParseError):
            LuaValueParser('os.execute("not allowed")').parse()

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
        self.assertContains(position_page, 'id="spawn-map-group-key"')
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
        snapshot = self.client.get('/api/mythic-planner/manage/')
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()['data']['counts']['dungeons'], 2)
        self.assertGreater(snapshot.json()['data']['counts']['spells'], 0)
        self.assertTrue(snapshot.json()['data']['abilities'][0]['display_name'])

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

    def test_staff_can_create_spawn_and_change_linked_enemy(self):
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
                    'group_key': 'manual-pack-a',
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
        self.assertEqual(spawn.group_key, 'manual-pack-a')

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
                    'group_key': 'manual-pack-b',
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
        self.assertEqual(spawn.group_key, 'manual-pack-b')
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
    def test_direct_route_exists_and_portal_navigation_stays_unchanged(self):
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
        self.assertNotContains(planner, 'id="save-server-route"')
        self.assertNotContains(planner, 'data-authenticated=')
        self.assertNotContains(planner, '登录后云端保存')
        self.assertNotContains(planner, 'id="enemy-tooltip"')
        self.assertNotContains(planner, '将鼠标移到怪物上')

        portal = self.client.get('/')
        self.assertEqual(portal.status_code, 200)
        self.assertNotContains(portal, 'href="/portal/mythic-planner/"')

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
            'mdt-spawn-initial',
            'dungeonsForSelectionGroup',
            'shouldStartMapPan',
            'mdt-ability-icon',
            'openEnemyDetail',
            'closeEnemyDetail',
            "addEventListener('contextmenu'",
        ):
            self.assertIn(token, portal_js)
        self.assertIn('进度 · ${forcesPercent.toFixed(2)}%', portal_js)
        self.assertNotIn('${formatNumber(stats.health)} HP', portal_js)
        self.assertIn('reorderPull', portal_js)
        self.assertIn('onPullPointerMove', portal_js)
        self.assertIn(
            '.filter(([key]) => Boolean(enemy.traits?.[key]))',
            portal_js,
        )
        self.assertIn('const markerSize = baseMarkerSize + 1', portal_js)
        self.assertIn('clamp(baseMarkerSize * 0.55, 4, 13) + 1', portal_js)
        self.assertNotIn('data-pull-action="up"', portal_js)
        self.assertNotIn('data-pull-action="down"', portal_js)
        self.assertNotIn('data-pull-action="rename"', portal_js)
        self.assertNotIn(
            "els.spawnLayer.addEventListener('pointerover'",
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
            "resource: 'spawn_position_reset'",
            "resource: 'spawns'",
            'spawn-map-enemy',
            'spawn-map-group-key',
            'els.coordinates.hidden = creating',
            'els.save.hidden = creating',
            'createPositionAt(positionOnMap(event))',
        ):
            self.assertIn(token, position_dashboard_js)
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
