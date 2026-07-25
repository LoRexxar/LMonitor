import json
from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.models import SimcApl, SimcContentTemplate


class SimcTemplateDashboardFixTests(TestCase):
    """TDD tests for SimC template management dashboard fix."""

    def setUp(self):
        self.user = User.objects.create_user(username='dashboard_user', password='pwd', is_staff=True)
        self.client = Client()
        self.client.force_login(self.user)

    def test_legacy_template_api_lists_only_global_base_template(self):
        base = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='default', name='基础运行框架', content='fight_style={fight_style}\n{player_config}\n{action_list}',
            is_active=True,
        )
        default_player = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='Fury 默认玩家', content='warrior="Default"\nspec=fury\nhead=,id=212048',
            is_active=True, is_selectable=False,
        )

        response = self.client.get('/api/simc-template/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.json()['templates']], [base.id])

        hidden_list = self.client.get('/api/simc-template/?template_type=default_player')
        self.assertEqual(hidden_list.status_code, 400)
        hidden_detail = self.client.get(f'/api/simc-template/?id={default_player.id}')
        self.assertEqual(hidden_detail.status_code, 404)

    def test_apl_management_uses_separate_api(self):
        """APL 管理使用独立的 /api/simc-workbench/apls/ 端点。"""
        default_apl = SimcApl.objects.create(
            name='默认 APL',
            spec='warrior_fury',
            content='actions+=/bloodthirst',
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
            is_active=True,
        )
        custom_apl = SimcApl.objects.create(
            name='个人 APL',
            spec='warrior_fury',
            content='actions+=/custom',
            source=SimcApl.SOURCE_USER,
            owner_user_id=self.user.id,
            is_active=True,
        )

        # APL 列表使用新端点
        response = self.client.get('/api/simc-workbench/apls/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        ids = {item['id'] for item in payload['data']}
        self.assertIn(default_apl.id, ids)
        self.assertIn(custom_apl.id, ids)

    def test_internal_default_player_is_hidden_and_immutable(self):
        default_player = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='Fury 默认玩家', content='warrior="Default"\nspec=fury\nhead=,id=212048',
            is_active=True, is_selectable=False,
        )

        self.assertEqual(self.client.get(
            f'/api/simc-template/?id={default_player.id}',
        ).status_code, 404)
        self.assertEqual(self.client.put(
            f'/api/simc-template/?id={default_player.id}',
            data=json.dumps({'content': 'warrior="Updated"'}),
            content_type='application/json',
        ).status_code, 404)
        self.assertEqual(self.client.patch(
            f'/api/simc-template/?id={default_player.id}',
            data=json.dumps({'is_active': False}),
            content_type='application/json',
        ).status_code, 405)
        self.assertEqual(self.client.delete(
            f'/api/simc-template/?id={default_player.id}',
        ).status_code, 405)
        default_player.refresh_from_db()
        self.assertEqual(default_player.content, 'warrior="Default"\nspec=fury\nhead=,id=212048')

    def test_staff_can_update_only_base_template_content(self):
        base = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='default', name='基础运行框架',
            content='iterations=100\n{player_config}\n', is_active=True,
        )
        content = 'iterations=200\n{player_config}\n'
        response = self.client.put(
            f'/api/simc-template/?id={base.id}',
            data=json.dumps({'content': content}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        base.refresh_from_db()
        self.assertEqual(base.content, content)

        response = self.client.put(
            f'/api/simc-template/?id={base.id}',
            data=json.dumps({'content': content, 'name': '新名称'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

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
        self.assertEqual(response.status_code, 405)
        self.assertFalse(response.json()['success'])
        self.assertFalse(SimcContentTemplate.objects.filter(template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER).exists())
