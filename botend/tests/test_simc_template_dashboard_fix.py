import json
from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.models import SimcApl, SimcContentTemplate, SimcProfile


class SimcTemplateDashboardFixTests(TestCase):
    """TDD tests for SimC template management dashboard fix."""

    def setUp(self):
        self.user = User.objects.create_user(username='dashboard_user', password='pwd', is_staff=True)
        self.client = Client()
        self.client.force_login(self.user)

    def test_legacy_template_api_lists_only_global_base_template(self):
        base = SimcContentTemplate.objects.create(
            source=SimcContentTemplate.SOURCE_USER,
            spec='default', name='基础运行框架', content='fight_style={fight_style}\n{player_config}\n{action_list}',
            is_active=True,
        )
        default_player = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury',
            spec='warrior_fury', class_name='warrior', name='Fury 默认玩家',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Default"\nspec=fury\nhead=,id=212048',
            is_active=True,
        )

        response = self.client.get('/api/simc-template/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.json()['templates']], [base.id])

        self.assertTrue(SimcProfile.objects.filter(pk=default_player.id).exists())

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
        default_player = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury',
            spec='warrior_fury', class_name='warrior', name='Fury 默认玩家',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Default"\nspec=fury\nhead=,id=212048',
            is_active=True,
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
        self.assertEqual(default_player.player_equipment, 'warrior="Default"\nspec=fury\nhead=,id=212048')

    def test_staff_can_update_only_base_template_content(self):
        base = SimcContentTemplate.objects.create(
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
                'source': 'simc_upstream',
                'spec': 'warrior_fury',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 405)
        self.assertFalse(response.json()['success'])
        self.assertFalse(SimcProfile.objects.filter(
            user_id__isnull=True, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
        ).exists())
