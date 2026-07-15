import json
from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.models import SimcContentTemplate


class SimcTemplateDashboardFixTests(TestCase):
    """TDD tests for SimC template management dashboard fix."""

    def setUp(self):
        self.user = User.objects.create_user(username='dashboard_user', password='pwd', is_staff=True)
        self.client = Client()
        self.client.force_login(self.user)

    def test_template_management_can_filter_by_all_four_types(self):
        """模板管理必须能按四种类型筛选：base_template、default_apl、custom_apl、default_player。"""
        base = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='default', name='基础运行框架', content='fight_style={fight_style}\n{player_config}\n{action_list}',
            is_active=True,
        )
        default_apl = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='默认 APL', content='actions+=/bloodthirst', is_active=True,
        )
        custom_apl = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_CUSTOM_APL,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury', name='个人 APL', content='actions+=/custom', is_active=True,
        )
        default_player = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='Fury 默认玩家', content='warrior="Default"\nspec=fury\nhead=,id=212048',
            is_active=True, is_selectable=False,
        )

        for template_type, expected_id in [
            ('base_template', base.id),
            ('default_apl', default_apl.id),
            ('custom_apl', custom_apl.id),
            ('default_player', default_player.id),
        ]:
            response = self.client.get(f'/api/simc-template/?template_type={template_type}')
            self.assertEqual(response.status_code, 200, f'{template_type} 筛选应返回 200')
            payload = response.json()
            self.assertTrue(payload['success'], f'{template_type} 筛选应成功: {payload}')
            self.assertEqual(len(payload['templates']), 1, f'{template_type} 应返回恰好 1 个模板')
            self.assertEqual(payload['templates'][0]['id'], expected_id)
            self.assertEqual(payload['templates'][0]['template_type'], template_type)

    def test_authenticated_dashboard_user_can_list_and_view_default_player_detail(self):
        """已登录 Dashboard 用户可列表和查看 default_player 详情，但不能通过通用 API 创建/改身份/删除。"""
        default_player = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='Fury 默认玩家', content='warrior="Default"\nspec=fury\nhead=,id=212048',
            is_active=True, is_selectable=False,
        )

        # 列表应返回 default_player
        list_response = self.client.get('/api/simc-template/?template_type=default_player')
        self.assertEqual(list_response.status_code, 200)
        list_payload = list_response.json()
        self.assertTrue(list_payload['success'], list_payload)
        self.assertEqual(len(list_payload['templates']), 1)
        self.assertEqual(list_payload['templates'][0]['id'], default_player.id)

        # 详情应返回完整内容
        detail_response = self.client.get(f'/api/simc-template/?id={default_player.id}')
        self.assertEqual(detail_response.status_code, 200)
        detail_payload = detail_response.json()
        self.assertTrue(detail_payload['success'], detail_payload)
        self.assertEqual(detail_payload['content'], 'warrior="Default"\nspec=fury\nhead=,id=212048')
        self.assertEqual(detail_payload['template_type'], 'default_player')

    def test_authenticated_dashboard_user_cannot_update_upstream_default_player(self):
        """upstream default_player 只能由受控同步链路维护，staff API 也只读。"""
        default_player = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='旧名称', content='旧内容',
            is_active=True, is_selectable=False,
        )

        update_response = self.client.put(
            f'/api/simc-template/?id={default_player.id}',
            data=json.dumps({
                'content': 'warrior="Updated"\nspec=fury\nhead=,id=999999',
                'name': '新名称',
                'is_selectable': True,
                'is_active': False,
            }),
            content_type='application/json',
        )
        self.assertEqual(update_response.status_code, 403, update_response.content)

        default_player.refresh_from_db()
        self.assertEqual(default_player.content, '旧内容')
        self.assertEqual(default_player.name, '旧名称')
        self.assertFalse(default_player.is_selectable)
        self.assertTrue(default_player.is_active)

    def test_default_player_identity_fields_are_immutable_through_api(self):
        """default_player 的 template_type/source/spec 身份字段通过 API 不可变更。"""
        default_player = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='原始', content='原始内容',
            is_active=True, is_selectable=False,
        )

        # 尝试改 template_type 应被拒绝
        type_change = self.client.put(
            f'/api/simc-template/?id={default_player.id}',
            data=json.dumps({'content': '改动', 'template_type': 'base_template'}),
            content_type='application/json',
        )
        self.assertEqual(type_change.status_code, 403)
        self.assertFalse(type_change.json()['success'])

        # 尝试改 source 应被拒绝
        source_change = self.client.put(
            f'/api/simc-template/?id={default_player.id}',
            data=json.dumps({'content': '改动', 'source': 'user'}),
            content_type='application/json',
        )
        self.assertEqual(source_change.status_code, 403)

        # 尝试改 spec 应被拒绝
        spec_change = self.client.put(
            f'/api/simc-template/?id={default_player.id}',
            data=json.dumps({'content': '改动', 'spec': 'warrior_arms'}),
            content_type='application/json',
        )
        self.assertEqual(spec_change.status_code, 403)

        default_player.refresh_from_db()
        self.assertEqual(default_player.content, '原始内容')
        self.assertEqual(default_player.template_type, SimcContentTemplate.TYPE_DEFAULT_PLAYER)
        self.assertEqual(default_player.source, SimcContentTemplate.SOURCE_SIMC_UPSTREAM)
        self.assertEqual(default_player.spec, 'warrior_fury')

    def test_cannot_create_default_player_via_api(self):
        """不允许通过通用 API 创建 default_player。"""
        response = self.client.post(
            '/api/simc-template/',
            data=json.dumps({
                'content': 'warrior="Forged"\nspec=fury',
                'template_type': 'default_player',
                'source': 'simc_upstream',
                'spec': 'warrior_fury',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()['success'])
        self.assertFalse(SimcContentTemplate.objects.filter(template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER).exists())

    def test_cannot_delete_default_player_via_api(self):
        """不允许通过 API 删除 default_player（DELETE 方法）。"""
        default_player = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='保护', content='内容',
            is_active=True, is_selectable=False,
        )

        response = self.client.delete(f'/api/simc-template/?id={default_player.id}')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()['success'])
        self.assertTrue(SimcContentTemplate.objects.filter(id=default_player.id).exists())
