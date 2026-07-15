"""
回归测试：SimC 工作台快照冻结契约

测试新任务以三部分冻结快照执行：
1. update_simc_binary 的 _sync_generated_inputs 依次同步 base_template、default_player、default_apl
2. 新任务冻结 base_template_id、selected_apl_id、default_player 快照
3. 缺少显式 ID 时按 spec 使用唯一启用默认项，重复项 fail closed
4. 前端发起模拟页应有基础模板选择、默认玩家配置可编辑区、APL 可编辑覆盖区
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.management.commands.update_simc_binary import Command as UpdateSimcBinaryCommand
from botend.models import SimcContentTemplate, SimcTask


class UpdateSimcBinarySyncContractTests(TestCase):
    """测试 update_simc_binary 的 _sync_generated_inputs 同步契约。"""

    def test_sync_generated_inputs_calls_base_template_then_player_then_apl(self):
        """_sync_generated_inputs 依次同步 base_template、default_player、default_apl，并传递 git hash。"""
        command = UpdateSimcBinaryCommand()
        command.simc_source_dir = '/srv/simc'
        command.stdout = SimpleNamespace(write=lambda x: None)
        command.row = SimpleNamespace(save=lambda **kwargs: None)

        git_hash = 'abc123def'

        with patch.object(command, '_get_git_hash', return_value=git_hash), \
             patch.object(command, '_set_status'), \
             patch.object(command, '_sync_default_template') as sync_template, \
             patch('botend.management.commands.update_simc_binary.call_command') as call_cmd:
            command._sync_generated_inputs()

        # 验证先同步基础模板
        sync_template.assert_called_once()

        # 验证调用 import_simc_player_templates，传递 git hash 作为 sync_version
        player_calls = [call for call in call_cmd.call_args_list if call[0][0] == 'import_simc_player_templates']
        self.assertEqual(len(player_calls), 1, "应当调用 import_simc_player_templates 一次")
        self.assertIn('sync_version', player_calls[0][1])
        self.assertEqual(player_calls[0][1]['sync_version'], git_hash)
        self.assertEqual(player_calls[0][1]['source_dir'], '/srv/simc/profiles/MID1')

        # 验证调用 import_simc_apl
        apl_calls = [call for call in call_cmd.call_args_list if call[0][0] == 'import_simc_apl']
        self.assertEqual(len(apl_calls), 1, "应当调用 import_simc_apl 一次")


class SimcTaskBaseTemplateSnapshotTests(TestCase):
    """测试任务冻结基础模板快照的契约。"""

    def setUp(self):
        self.user = User.objects.create_user(username='snapshot_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_explicit_base_template_id_freezes_template_content(self):
        """明确选择 base_template_id 时必须冻结该基础模板内容到 ext.base_template_content。"""
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury', name='用户基础模板',
            content='{simulation_options}\n{player_identity}\n{equipment}\n{talents}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
        )

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Frozen base template task',
            'task_type': 1,
            'base_template_id': base_template.id,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222',
            'spec': 'fury',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])
        ext = json.loads(task.ext)
        self.assertIn('base_template_content', ext, "任务 ext 应冻结 base_template_content")
        self.assertIn('{player_identity}', ext['base_template_content'])
        self.assertIn('{equipment}', ext['base_template_content'])

    def test_request_base_template_content_overrides_selected_template_snapshot(self):
        """用户编辑后的基础模板正文优先于所选模板当前数据库正文。"""
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury', name='可编辑基础模板',
            content='{simulation_options}\n{player_identity}\n{equipment}\n{talents}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
        )
        edited = '{simulation_options}\niterations=12345\n{player_identity}\n{equipment}\n{talents}\n{stat_overrides}\n{action_list}\n{output_options}'
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Edited base template snapshot', 'task_type': 1,
            'base_template_id': base_template.id,
            'base_template_content': edited,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222',
            'spec': 'fury',
        }), content_type='application/json')
        self.assertTrue(response.json()['success'], response.json())
        ext = json.loads(SimcTask.objects.get(id=response.json()['data']['id']).ext)
        self.assertEqual(ext['base_template_content'], edited)
        self.assertIn('iterations=12345', ext['base_template_content'])

    def test_frozen_base_template_immune_to_upstream_changes(self):
        """冻结的基础模板快照不受模板后续更新影响。"""
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury', name='可变基础模板',
            content='{simulation_options}\n{player_identity}\n{equipment}\n{talents}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
        )

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Immutable snapshot',
            'task_type': 1,
            'base_template_id': base_template.id,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222',
            'spec': 'fury',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'])
        task = SimcTask.objects.get(id=response.json()['data']['id'])
        original_frozen = json.loads(task.ext)['base_template_content']
        self.assertIn('{player_identity}', original_frozen)

        # 更新模板内容
        base_template.content = '{simulation_options}\nfight_style=HecticAddCleave\n{player_identity}\n{equipment}'
        base_template.save()

        # 任务快照不应改变
        task.refresh_from_db()
        frozen = json.loads(task.ext)['base_template_content']
        self.assertEqual(frozen, original_frozen)
        self.assertIn('{player_identity}', frozen)
        self.assertNotIn('HecticAddCleave', frozen)


class SimcTaskAplSnapshotTests(TestCase):
    """测试任务冻结 APL 快照的契约。"""

    def setUp(self):
        self.user = User.objects.create_user(username='apl_snapshot_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_explicit_selected_apl_id_freezes_apl_content(self):
        """明确 selected_apl_id 时沿用既有 override_action_list 字段冻结 APL。"""
        # Create base template first
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury', name='基础模板',
            content='{simulation_options}\n{player_identity}\n{equipment}\n{talents}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
        )

        apl = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='默认 APL',
            content='actions+=/bloodthirst\nactions+=/rampage\nactions+=/execute',
            is_active=True,
        )

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Frozen APL task',
            'task_type': 1,
            'base_template_id': base_template.id,
            'selected_apl_id': apl.id,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222',
            'spec': 'fury',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])
        ext = json.loads(task.ext)
        self.assertIn('override_action_list', ext, "任务 ext 应冻结 override_action_list")
        self.assertIn('bloodthirst', ext['override_action_list'])
        self.assertIn('rampage', ext['override_action_list'])

    def test_frozen_apl_immune_to_upstream_changes(self):
        """冻结的 APL 快照不受 APL 后续更新影响。"""
        # Create base template first
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury', name='基础模板',
            content='{simulation_options}\n{player_identity}\n{equipment}\n{talents}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
        )

        apl = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='可变 APL',
            content='actions+=/bloodthirst\nactions+=/rampage',
            is_active=True,
        )

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Immutable APL snapshot',
            'task_type': 1,
            'base_template_id': base_template.id,
            'selected_apl_id': apl.id,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222',
            'spec': 'fury',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'])
        task = SimcTask.objects.get(id=response.json()['data']['id'])
        original_frozen = json.loads(task.ext)['override_action_list']

        # 更新 APL
        apl.content = 'actions+=/whirlwind\nactions+=/bladestorm'
        apl.save()

        # 任务快照不应改变
        task.refresh_from_db()
        frozen = json.loads(task.ext)['override_action_list']
        self.assertEqual(frozen, original_frozen)
        self.assertIn('bloodthirst', frozen)
        self.assertIn('rampage', frozen)
        self.assertNotIn('whirlwind', frozen)
        self.assertNotIn('bladestorm', frozen)

    def test_explicit_empty_apl_override_remains_empty(self):
        """显式清空 APL 表示冻结空内容，不能重新填回所选或默认 APL。"""
        # Create base template first
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury', name='基础模板',
            content='{simulation_options}\n{player_identity}\n{equipment}\n{talents}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
        )

        apl = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='默认 APL', content='actions+=/bloodthirst', is_active=True,
        )
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Explicit empty APL', 'task_type': 1,
            'base_template_id': base_template.id,
            'selected_apl_id': apl.id, 'override_action_list': '',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222',
            'spec': 'fury',
        }), content_type='application/json')
        self.assertTrue(response.json()['success'], response.json())
        ext = json.loads(SimcTask.objects.get(id=response.json()['data']['id']).ext)
        self.assertIn('override_action_list', ext)
        self.assertEqual(ext['override_action_list'], '')


class SimcTaskDefaultPlayerBaselineTests(TestCase):
    """测试任务默认玩家基线的契约。"""

    def setUp(self):
        self.user = User.objects.create_user(username='player_baseline_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_attribute_only_mode_uses_default_player_baseline(self):
        """attribute_only 模式下，玩家基线默认从 default_player 获取并冻结。"""
        # Create base template first
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury', name='基础模板',
            content='{simulation_options}\n{player_identity}\n{equipment}\n{talents}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
        )

        equipment = '\n'.join([
            'warrior="DefaultPlayer"', 'level=90', 'spec=fury',
            'head=,id=212048', 'neck=,id=224433', 'shoulder=,id=212050',
            'back=,id=224435', 'chest=,id=212046', 'wrist=,id=224436',
            'hands=,id=212047', 'waist=,id=224437', 'legs=,id=212049',
            'feet=,id=224438', 'finger1=,id=224439', 'finger2=,id=224440',
            'trinket1=,id=224441', 'trinket2=,id=224442', 'main_hand=,id=224443',
        ])
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', class_name='warrior',
            content=equipment,
            is_active=True, is_selectable=False,
        )

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Default player baseline',
            'task_type': 1,
            'base_template_id': base_template.id,
            'player_config_mode': 'attribute_only',
            'spec': 'fury',
            'talent': 'BUILD',
            'gear_crit': 1000, 'gear_haste': 2000,
            'gear_mastery': 3000, 'gear_versatility': 4000,
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])
        ext = json.loads(task.ext)
        self.assertIn('player_equipment', ext)
        self.assertIn('DefaultPlayer', ext['player_equipment'])
        self.assertIn('head=,id=212048', ext['player_equipment'])


class SimcTaskAutoSelectionTests(TestCase):
    """测试缺少显式 ID 时的自动选择契约。"""

    def setUp(self):
        self.user = User.objects.create_user(username='auto_select_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_unique_enabled_apl_auto_selected(self):
        """缺少显式 selected_apl_id 时，按 spec 使用唯一启用默认 APL。"""
        # Create base template first
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury', name='基础模板',
            content='{simulation_options}\n{player_identity}\n{equipment}\n{talents}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
        )

        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='唯一 APL',
            content='actions+=/execute', is_active=True,
        )

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Auto select unique APL',
            'task_type': 1,
            'base_template_id': base_template.id,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222',
            'spec': 'fury',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])
        ext = json.loads(task.ext)
        # 应该自动选择并冻结唯一的 APL
        self.assertIn('override_action_list', ext)
        self.assertIn('execute', ext['override_action_list'])

    def test_user_default_apl_overrides_global_default(self):
        """全局与当前用户默认 APL 并存时，自动选择当前用户版本。"""
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury', name='基础模板',
            content='{simulation_options}\n{player_identity}\n{equipment}\n{talents}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
        )
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='Global APL',
            content='actions+=/execute', is_active=True,
        )
        owned = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            source=SimcContentTemplate.SOURCE_USER,
            owner_user_id=self.user.id,
            spec='warrior_fury', name='User APL',
            content='actions+=/whirlwind', is_active=True,
        )

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'User APL wins',
            'task_type': 1,
            'base_template_id': base_template.id,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222',
            'spec': 'fury',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        ext = json.loads(SimcTask.objects.get(id=response.json()['data']['id']).ext)
        self.assertEqual(ext['selected_apl_id'], owned.id)
        self.assertEqual(ext['override_action_list'], 'actions+=/whirlwind')


class SimcWorkbenchFrontendContractTests(TestCase):
    """测试前端发起模拟页的契约。"""

    def test_frontend_has_base_template_selector(self):
        """前端应有基础模板选择器。"""
        template_path = Path(__file__).resolve().parents[2] / 'templates/dashboard/index.html'
        template = template_path.read_text(encoding='utf-8')
        self.assertIn('base-template-select', template, "模板应包含基础模板选择器 ID")
        self.assertIn('base-template-content', template, "模板应包含基础模板正文编辑区")

    def test_frontend_has_player_baseline_config_area(self):
        """前端应有默认玩家配置可编辑区。"""
        template_path = Path(__file__).resolve().parents[2] / 'templates/dashboard/index.html'
        template = template_path.read_text(encoding='utf-8')
        self.assertIn('player-baseline-config', template, "模板应包含玩家基线配置区")

    def test_frontend_has_apl_override_editor(self):
        """前端应有 APL 可编辑覆盖区。"""
        template_path = Path(__file__).resolve().parents[2] / 'templates/dashboard/index.html'
        template = template_path.read_text(encoding='utf-8')
        self.assertIn('apl-override', template, "模板应包含 APL 覆盖编辑区")

    def test_frontend_js_submits_required_snapshot_fields(self):
        """前端 JS 把 template id/content、player baseline、APL id/override 传入请求。"""
        main_js_path = Path(__file__).resolve().parents[2] / 'static/dashboard/js/main.js'
        main_js = main_js_path.read_text(encoding='utf-8')

        # 验证任务创建请求包含必要字段
        self.assertIn('base_template_id', main_js, "JS 应提交 base_template_id")
        self.assertIn('base_template_content', main_js, "JS 应提交编辑后的 base_template_content")
        self.assertIn('selected_apl_id', main_js, "JS 应提交 selected_apl_id")
        self.assertIn('player_equipment', main_js, "JS 应提交冻结的 player_equipment")
        self.assertIn('override_action_list', main_js, "JS 应提交用户可编辑的 APL 覆盖")
