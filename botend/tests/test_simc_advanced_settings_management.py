import json
from django.contrib.auth.models import User
from django.test import Client
from django.test import TestCase
from botend.models import SimcContentTemplate
from botend.models import SimcProfile
from botend.models import SimcApl

class _GlobalTemplateFixture:
    """Shared global-template setup; deliberately not a discoverable TestCase."""
    BASE_CONTENT = 'iterations=100\n{player_config}'

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(username='template-owner', password='pwd')
        self.staff = User.objects.create_user(username='template-staff', password='pwd', is_staff=True)
        self.system = SimcContentTemplate.objects.create(
            owner_user_id=None, name='System Base', spec='default',
            source=SimcContentTemplate.SOURCE_USER,
            content=self.BASE_CONTENT, is_active=True,
        )
        self.client.force_login(self.owner)


class SimcAdvancedSettingsManagementTests(_GlobalTemplateFixture, TestCase):
    """高级设置中的基础模板是全局系统能力，不承载玩家配置或个人资源。"""

    def setUp(self):
        super().setUp()
        self.user = self.owner
        self.private = SimcContentTemplate.objects.create(
            name='Legacy Private', spec='warrior_fury', owner_user_id=self.user.id,
            source=SimcContentTemplate.SOURCE_USER,
            content='iterations=100\n{player_config}', is_active=True,
        )

    def _json(self, method, path, payload=None):
        return getattr(self.client, method)(
            path,
            data=json.dumps(payload or {}),
            content_type='application/json',
        )





    def test_private_legacy_template_is_hidden_and_not_writable(self):
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(f'/api/simc-workbench/templates/{self.private.id}/').status_code,
            404,
        )
        self.assertEqual(
            self._json('put', f'/api/simc-workbench/templates/{self.private.id}/', {
                'content': 'iterations=200\n{player_config}',
            }).status_code,
            404,
        )

    def test_upstream_template_is_read_only(self):
        upstream = SimcContentTemplate.objects.create(
            name='Upstream Base', spec='warrior_fury',
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            content='iterations=100\n{player_config}', is_active=True,
        )
        self.client.force_login(self.staff)
        response = self._json('put', f'/api/simc-workbench/templates/{upstream.id}/', {
            'content': 'iterations=200\n{player_config}',
        })
        self.assertEqual(response.status_code, 403)

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

class SimcWorkbenchTemplateResourceTests(_GlobalTemplateFixture, TestCase):
    BASE_CONTENT = 'iterations=100\n{player_config}\n'


    def _post(self, path, payload):
        return self.client.post(path, json.dumps(payload), content_type='application/json')

    def _put(self, path, payload):
        return self.client.put(path, json.dumps(payload), content_type='application/json')

    def test_list_exposes_only_global_base_template(self):
        private = SimcContentTemplate.objects.create(
            owner_user_id=self.owner.id, name='Legacy Private',
            source='user', spec='legacy', content='warrior="Private"', is_active=True,
        )
        default_player = SimcProfile.objects.create(
            user_id=None, name='Default Player', source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury', class_name='warrior',
            spec='warrior_fury', player_config_mode='manual_equipment',
            player_equipment='warrior="Base"', is_active=True,
        )

        payload = self.client.get('/api/simc-workbench/templates/').json()
        self.assertEqual([row['id'] for row in payload['data']], [self.system.id])
        self.assertEqual(payload['data'][0]['template_type'], 'base_template')
        self.assertTrue(payload['data'][0]['read_only'])
        self.assertFalse(payload['can_write'])
        self.assertNotIn(private.id, [row['id'] for row in payload['data']])

        self.client.force_login(self.staff)
        payload = self.client.get('/api/simc-workbench/templates/').json()
        self.assertEqual([row['id'] for row in payload['data']], [self.system.id])
        self.assertFalse(payload['data'][0]['read_only'])
        self.assertTrue(payload['can_write'])

    def test_create_archive_restore_and_delete_are_disabled(self):
        self.client.force_login(self.staff)
        self.assertEqual(self._post('/api/simc-workbench/templates/', {
            'name': 'Extra', 'content': self.BASE_CONTENT,
        }).status_code, 405)
        for action in ('archive', 'restore'):
            self.assertEqual(self._post(
                f'/api/simc-workbench/templates/{self.system.id}/', {'action': action},
            ).status_code, 405)
        self.assertEqual(self.client.delete(
            f'/api/simc-workbench/templates/{self.system.id}/',
        ).status_code, 405)
        self.system.refresh_from_db()
        self.assertTrue(self.system.is_active)

    def test_regular_user_cannot_edit_system_template(self):
        response = self._put(
            f'/api/simc-workbench/templates/{self.system.id}/',
            {'content': 'iterations=200\n{player_config}\n'},
        )
        self.assertEqual(response.status_code, 403)
        self.system.refresh_from_db()
        self.assertEqual(self.system.content, self.BASE_CONTENT)

    def test_staff_can_edit_only_system_template_content(self):
        self.client.force_login(self.staff)
        updated_content = 'iterations=200\n{player_config}\n'
        response = self._put(
            f'/api/simc-workbench/templates/{self.system.id}/',
            {'content': updated_content},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.system.refresh_from_db()
        self.assertEqual(self.system.content, updated_content)

        for field, value in (
            ('name', 'Renamed'),
            ('spec', 'warrior_fury'),
            ('class_name', 'warrior'),
            ('source', 'simc_upstream'),
        ):
            response = self._put(
                f'/api/simc-workbench/templates/{self.system.id}/',
                {'content': updated_content, field: value},
            )
            self.assertEqual(response.status_code, 400, field)

    def test_internal_or_private_templates_are_not_workbench_resources(self):
        private = SimcContentTemplate.objects.create(
            owner_user_id=self.owner.id, name='Legacy Private',
            source='user', spec='legacy', content='warrior="Private"', is_active=True,
        )
        default_player = SimcProfile.objects.create(
            user_id=None, name='Default Player', source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury', class_name='warrior',
            spec='warrior_fury', player_config_mode='manual_equipment',
            player_equipment='warrior="Base"', is_active=True,
        )
        self.client.force_login(self.staff)
        path = f'/api/simc-workbench/templates/{private.id}/'
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self._put(path, {'content': self.BASE_CONTENT}).status_code, 404)
        self.assertTrue(SimcProfile.objects.filter(pk=default_player.id).exists())

    def test_split_resource_tables_keep_system_base_and_internal_players(self):
        duplicate = SimcContentTemplate.objects.create(
            owner_user_id=None, name='Old Base',
            source='user', spec='legacy', content=self.BASE_CONTENT, is_active=False,
        )
        default_player = SimcProfile.objects.create(
            user_id=None, name='Default Player', source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury', class_name='warrior',
            spec='warrior_fury', player_config_mode='manual_equipment',
            player_equipment='warrior="Base"', is_active=True,
        )

        self.assertTrue(SimcContentTemplate.objects.filter(pk=self.system.pk).exists())
        self.assertTrue(SimcContentTemplate.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(SimcProfile.objects.filter(pk=default_player.pk).exists())
