import json

from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.models import SimcContentTemplate, SimcProfile


class SimcAdvancedSettingsManagementTests(TestCase):
    """高级设置中的基础模板是全局系统能力，不承载玩家配置或个人资源。"""

    def setUp(self):
        self.user = User.objects.create_user(username='simc_user', password='pwd')
        self.staff = User.objects.create_user(username='simc_staff', password='pwd', is_staff=True)
        self.client = Client()
        self.system = SimcContentTemplate.objects.create(
            name='System Base', spec='default', source=SimcContentTemplate.SOURCE_USER,
            content='iterations=100\n{player_config}', is_active=True,
        )
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

    def test_list_exposes_only_global_base_templates(self):
        self.client.force_login(self.user)
        response = self.client.get('/api/simc-workbench/templates/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.json()['data']], [self.system.id])
        self.assertEqual(response.json()['data'][0]['template_type'], 'base_template')

    def test_default_player_profile_is_not_a_template_resource(self):
        profile = SimcProfile.objects.create(
            user_id=None,
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury',
            name='Fury', spec='warrior_fury', class_name='warrior',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Fury"\nspec=fury',
            is_active=True,
        )
        self.client.force_login(self.staff)
        response = self.client.get('/api/simc-workbench/templates/')
        self.assertEqual([row['name'] for row in response.json()['data']], ['System Base'])

    def test_template_creation_is_disabled(self):
        self.client.force_login(self.staff)
        response = self._json('post', '/api/simc-workbench/templates/', {
            'name': 'New Base', 'content': '{player_config}',
        })
        self.assertEqual(response.status_code, 405)

    def test_staff_can_update_only_global_template_content(self):
        self.client.force_login(self.staff)
        response = self._json('put', f'/api/simc-workbench/templates/{self.system.id}/', {
            'content': 'iterations=200\n{player_config}',
        })
        self.assertEqual(response.status_code, 200)
        self.system.refresh_from_db()
        self.assertEqual(self.system.content, 'iterations=200\n{player_config}')

        response = self._json('put', f'/api/simc-workbench/templates/{self.system.id}/', {
            'content': 'iterations=200\n{player_config}', 'name': 'Renamed',
        })
        self.assertEqual(response.status_code, 400)

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

    def test_archive_restore_and_real_delete_are_disabled(self):
        self.client.force_login(self.staff)
        for action in ('archive', 'restore'):
            response = self._json(
                'post', f'/api/simc-workbench/templates/{self.system.id}/', {'action': action},
            )
            self.assertEqual(response.status_code, 405)
        response = self._json('delete', f'/api/simc-workbench/templates/{self.system.id}/')
        self.assertEqual(response.status_code, 405)
        self.assertTrue(SimcContentTemplate.objects.filter(pk=self.system.id).exists())
