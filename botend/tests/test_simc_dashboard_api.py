import json
from unittest.mock import patch as mock_patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.dashboard.api import inspect_raw_simc_code
from botend.services.simc_player_config import parse_manual_player_config
from botend.models import SimcContentTemplate, SimcTask, WowItemSnapshot


class SimcRawInspectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='simc_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_inspect_raw_simc_code_detects_profile_and_default_apl(self):
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='hunter_beast_mastery',
            class_name='hunter',
            name='默认APL hunter_beast_mastery',
            content='actions+=/kill_command',
            is_active=True,
            is_selectable=True,
        )
        payload = inspect_raw_simc_code('''
hunter="Bloodmastêr"
level=80
race=orc
role=attack
spec=beast_mastery
''')

        self.assertEqual(payload['character_name'], 'Bloodmastêr')
        self.assertEqual(payload['class'], 'hunter')
        self.assertEqual(payload['spec'], 'beast_mastery')
        self.assertEqual(payload['spec_key'], 'hunter_beast_mastery')
        self.assertTrue(payload['default_apl_available'])
        self.assertEqual(payload['plans'][0]['id'], 'regular')
        self.assertTrue(payload['plans'][0]['enabled'])
        self.assertFalse(payload['plans'][1]['enabled'])

    def test_inspect_raw_endpoint_returns_plans(self):
        response = self.client.post(
            '/api/simc-profile/inspect-raw/',
            data=json.dumps({'raw_simc_code': 'warrior="Foo"\nspec=fury\n'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['data']['class'], 'warrior')
        self.assertEqual(payload['data']['spec'], 'fury')
        self.assertEqual(payload['data']['plans'][0]['task_type'], 1)

    def test_raw_simc_task_create_persists_raw_code_in_ext(self):
        raw_code = 'mage="Arcaneone"\nspec=arcane\n'
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Arcaneone arcane 常规模拟',
                'task_type': 1,
                'simc_profile_id': 0,
                'raw_simc_code': raw_code,
                'regular_time': 300,
                'regular_target_count': 1,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        self.assertEqual(task.simc_profile_id, 0)
        self.assertEqual(task.task_type, 1)
        ext = json.loads(task.ext)
        self.assertEqual(ext['raw_simc_code'], raw_code)
        self.assertEqual(ext['regular_time'], 300)
        self.assertEqual(ext['regular_target_count'], 1)

    def test_raw_simc_attribute_task_is_rejected(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'bad attribute raw',
                'task_type': 2,
                'simc_profile_id': 0,
                'raw_simc_code': 'paladin="Foo"\nspec=retribution\n',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('不支持属性模拟', payload['error'])
        self.assertFalse(SimcTask.objects.exists())


class SimcNewConfigModeTests(TestCase):
    """测试新版工作台任务配置：只输入玩家信息，战斗/APL 由选项控制。"""

    def setUp(self):
        self.user = User.objects.create_user(username='newmode_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_create_task_with_manual_equipment_mode(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Fury Manual Equipment',
                'task_type': 1,
                'player_import_mode': 'manual_equipment',
                'player_equipment': 'talents=TEST\nhead=,id=212048',
                'fight_style': 'Patchwerk',
                'time': 300,
                'target_count': 1,
                'spec': 'fury',
                'talent': 'TEST',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        self.assertEqual(task.result_file, '')
        ext = json.loads(task.ext)
        self.assertEqual(ext['player_config_mode'], 'manual_equipment')
        self.assertEqual(ext['player_import_mode'], 'manual_equipment')
        self.assertEqual(ext['player_equipment'], 'talents=TEST\nhead=,id=212048')
        self.assertEqual(ext['fight_style'], 'Patchwerk')
        self.assertEqual(ext['time'], 300)
        self.assertEqual(ext['target_count'], 1)

    def test_create_task_with_legacy_equipment_alias_maps_to_manual_equipment(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Legacy Equipment Alias',
                'task_type': 1,
                'player_config_mode': 'equipment',
                'player_equipment': 'talents=TEST\nneck=,id=224433',
                'spec': 'fury',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        ext = json.loads(task.ext)
        self.assertEqual(ext['player_config_mode'], 'manual_equipment')
        self.assertEqual(ext['player_import_mode'], 'manual_equipment')

    def test_create_task_with_battlenet_mode(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Fury Battle.net Import',
                'task_type': 1,
                'player_import_mode': 'battlenet',
                'battlenet_region': 'EU',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
                'fight_style': 'Patchwerk',
                'time': 300,
                'target_count': 1,
                'spec': 'fury',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        self.assertEqual(task.result_file, '')
        ext = json.loads(task.ext)
        self.assertEqual(ext['player_config_mode'], 'battlenet')
        self.assertEqual(ext['player_import_mode'], 'battlenet')
        self.assertEqual(ext['battlenet_region'], 'eu')
        self.assertEqual(ext['battlenet_realm'], 'Kazzak')
        self.assertEqual(ext['battlenet_character'], 'Bloodmastêr')

    def test_manual_equipment_requires_player_block(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'No Equipment',
                'task_type': 1,
                'player_import_mode': 'manual_equipment',
                'player_equipment': '',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('玩家装备配置不能为空', payload['error'])

    def test_battlenet_requires_region_realm_character(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Bad Battlenet',
                'task_type': 1,
                'player_import_mode': 'battlenet',
                'battlenet_region': 'eu',
                'battlenet_realm': '',
                'battlenet_character': 'Bloodmastêr',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('Battle.net 导入需要提供', payload['error'])

    def test_stats_mode_is_rejected_in_new_workbench(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Stats Not Allowed',
                'task_type': 1,
                'player_config_mode': 'stats',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('玩家信息导入方式必须是', payload['error'])

    def test_apply_template_builds_battlenet_armory_player_block(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'fight_style={fight_style}\n{player_config}\n{action_list}',
            {
                'fight_style': 'Patchwerk',
                'player_import_mode': 'battlenet',
                'battlenet_region': 'eu',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
                'spec': 'fury',
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertNotIn('Bloodmast_r', rendered)
        self.assertIn('armory=eu,Kazzak,Bloodmastêr', rendered)
        self.assertIn('spec=fury', rendered)
        self.assertIn('actions=auto_attack', rendered)

    def test_apply_template_inserts_manual_equipment_player_block(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'fight_style={fight_style}\n{player_config}\n{action_list}',
            {
                'fight_style': 'Patchwerk',
                'player_import_mode': 'manual_equipment',
                'player_equipment': 'talents=TEST\nhead=,id=212048',
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertIn('talents=TEST', rendered)
        self.assertIn('head=,id=212048', rendered)
        self.assertIn('actions=auto_attack', rendered)


class SimcPreviewTests(TestCase):
    """预览必须复用实际任务的模板拼装，不创建任务也不触发 SimC。"""

    def setUp(self):
        self.user = User.objects.create_user(username='preview_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        self.template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='fury',
            class_name='warrior',
            name='Fury base template',
            content='spec={spec}\nfight_style={fight_style}\nmax_time={time}\ntargets={target_count}\n{player_config}\n{action_list}',
            is_active=True,
            is_selectable=True,
        )
        self.template_lookup = mock_patch(
            'botend.controller.plugins.simc.SimcMonitor.SimcMonitor.select_template_by_spec',
            return_value=self.template,
        )
        self.mock_template_lookup = self.template_lookup.start()
        self.addCleanup(self.template_lookup.stop)
        self.apl = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury',
            class_name='warrior',
            name='Fury APL',
            content='actions=auto_attack',
            is_active=True,
            is_selectable=True,
        )

    def test_preview_battlenet_returns_full_rendered_configuration(self):
        response = self.client.post(
            '/api/simc-preview/',
            data=json.dumps({
                'spec': 'fury',
                'fight_style': 'Patchwerk',
                'time': 300,
                'target_count': 1,
                'player_import_mode': 'battlenet',
                'battlenet_region': 'EU',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
                'selected_apl_id': self.apl.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertIn('armory=eu,Kazzak,Bloodmastêr', payload['data']['simc_code'])
        self.assertIn('max_time=300', payload['data']['simc_code'])
        self.assertIn('actions=auto_attack', payload['data']['simc_code'])
        self.assertEqual(payload['data']['player_preview'], 'armory=eu,Kazzak,Bloodmastêr')
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_preview_manual_equipment_returns_full_rendered_configuration(self):
        response = self.client.post(
            '/api/simc-preview/',
            data=json.dumps({
                'spec': 'fury',
                'fight_style': 'Cleave',
                'time': 420,
                'target_count': 2,
                'player_config_mode': 'manual_equipment',
                'player_equipment': 'talents=TEST\nhead=,id=212048',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertIn('fight_style=Cleave', payload['data']['simc_code'])
        self.assertIn('max_time=420', payload['data']['simc_code'])
        self.assertIn('targets=2', payload['data']['simc_code'])
        self.assertIn('talents=TEST', payload['data']['simc_code'])
        self.assertIn('head=,id=212048', payload['data']['simc_code'])
        self.assertEqual(payload['data']['player_preview'], 'talents=TEST\nhead=,id=212048')
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_preview_returns_structured_manual_player_detail_with_items_and_stats(self):
        WowItemSnapshot.objects.create(item_id=212048, name='Helm of Tests', name_zh='测试头盔', icon='inv_helmet_01')
        WowItemSnapshot.objects.create(item_id=71543, name='Swift Enchant', name_zh='迅捷附魔')
        WowItemSnapshot.objects.create(item_id=213479, name='Test Gem', name_zh='测试宝石')
        from botend.models import SimcSecondaryStatRule
        SimcSecondaryStatRule.objects.update_or_create(
            class_name='warrior',
            defaults={
                'crit_per_percent': 46, 'haste_per_percent': 44,
                'mastery_per_percent': 46, 'versatility_per_percent': 54,
            },
        )
        response = self.client.post(
            '/api/simc-preview/',
            data=json.dumps({
                'spec': 'fury',
                'player_config_mode': 'manual_equipment',
                'player_equipment': '\n'.join([
                    'warrior="Previewer"',
                    'level=80',
                    'race=orc',
                    'region=cn',
                    'server=死亡之翼',
                    'spec=fury',
                    'talents=BUILDCODE',
                    'head=,id=212048,ilevel=639,enchant_id=71543,gems=213479/213480',
                    'main_hand=,id=224638,ilevel=646',
                    'crit_rating=10730',
                    'haste_rating=18641',
                    'mastery_rating=21785',
                    'versatility_rating=6757',
                ]),
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        detail = payload['data']['player_detail']
        self.assertEqual(detail['source']['type'], 'manual_equipment')
        self.assertEqual(detail['identity']['name'], 'Previewer')
        self.assertEqual(detail['identity']['race'], 'orc')
        self.assertEqual(detail['identity']['region'], 'cn')
        self.assertEqual(detail['identity']['realm'], '死亡之翼')
        self.assertEqual(detail['talents']['build_code'], 'BUILDCODE')
        self.assertEqual(detail['equipment'][0]['slot'], 'head')
        self.assertEqual(detail['equipment'][0]['display_name'], '测试头盔')
        self.assertEqual(detail['equipment'][0]['item_level'], 639)
        self.assertEqual(detail['equipment'][0]['enchant']['display_name'], '迅捷附魔')
        self.assertEqual(detail['equipment'][0]['gems'][0]['display_name'], '测试宝石')
        self.assertEqual(detail['stats']['secondary']['crit']['rating'], 10730)
        self.assertAlmostEqual(detail['stats']['secondary']['crit']['percent'], 233.26, places=2)
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_real_simc_export_keeps_main_gear_names_and_excludes_bag_choices(self):
        config = '''# 炎色雷灬 - Fury - 2026-07-10 02:37 - CN/死亡之翼
warrior="炎色雷灬"
level=90
race=orc
region=cn
server=死亡之翼
role=attack
professions=enchanting=100/jewelcrafting=100
spec=fury
talents=ACTIVE_BUILD
# Saved Loadout: 团本屠戮
# talents=SAVED_BUILD
omnium_talents=136817:1/136819:1
# 终夜者的獠牙头盔 (289)
head=,id=249952,enchant_id=8017,gem_id=240892,bonus_id=6652/13534
# 腐沼的孢子之心 (298)
neck=,id=268291,gem_id=240983,bonus_id=6652/13668
# 信徒的流丝罩袍 (285)
back=,id=239656,bonus_id=12214/13667,content_tuning=3615,crafted_stats=32/36,crafting_quality=5
# 旋风虚空裂斧 (298)
main_hand=,id=251117,enchant_id=8041,bonus_id=13440/6652
### Gear from Bags
# 盘绕恶意丝带 (285)
# neck=,id=249337,bonus_id=6652/13668
'''
        detail = parse_manual_player_config(config, 'fury')

        self.assertEqual(detail['identity']['name'], '炎色雷灬')
        self.assertEqual(detail['identity']['region'], 'cn')
        self.assertEqual(detail['identity']['realm'], '死亡之翼')
        self.assertEqual(detail['identity']['role'], 'attack')
        self.assertEqual(detail['identity']['professions'], {'enchanting': 100, 'jewelcrafting': 100})
        self.assertEqual(detail['talents']['build_code'], 'ACTIVE_BUILD')
        self.assertEqual(detail['talents']['saved_loadouts'], [{'name': '团本屠戮', 'build_code': 'SAVED_BUILD'}])
        self.assertEqual(len(detail['equipment']), 4)
        self.assertEqual(detail['equipment'][0]['display_name'], '终夜者的獠牙头盔')
        self.assertEqual(detail['equipment'][0]['item_level'], 289)
        self.assertEqual(detail['equipment'][0]['gems'][0]['id'], 240892)
        self.assertEqual(detail['equipment'][2]['crafted_stats'], ['精通', '全能'])
        self.assertEqual(detail['equipment'][2]['crafting_quality'], 5)
        self.assertEqual(detail['omnium_talents'], [{'id': 136817, 'rank': 1}, {'id': 136819, 'rank': 1}])

    def test_preview_returns_battlenet_identity_and_explicit_missing_detail(self):
        response = self.client.post(
            '/api/simc-preview/',
            data=json.dumps({
                'spec': 'fury',
                'player_import_mode': 'battlenet',
                'battlenet_region': 'EU',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        detail = payload['data']['player_detail']
        self.assertEqual(detail['source']['type'], 'battlenet')
        self.assertEqual(detail['identity']['region'], 'eu')
        self.assertEqual(detail['identity']['realm'], 'Kazzak')
        self.assertEqual(detail['identity']['name'], 'Bloodmastêr')
        self.assertEqual(detail['equipment'], [])
        self.assertTrue(detail['missing_fields'])
        self.assertIn('未保存角色装备快照', detail['missing_fields'][0])

    def test_preview_rejects_incomplete_battlenet_configuration(self):
        response = self.client.post(
            '/api/simc-preview/',
            data=json.dumps({
                'spec': 'fury',
                'player_import_mode': 'battlenet',
                'battlenet_region': 'eu',
                'battlenet_character': 'Bloodmastêr',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('Battle.net 导入需要提供', payload['error'])
