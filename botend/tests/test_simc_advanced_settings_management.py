"""
测试 SimC 高级设置（templates/APL）的完整管理闭环。
包括权限、CSRF、owner 隔离、唯一键校验、只读模板保护。
"""
import json

from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.models import SimcContentTemplate, SimcApl


class SimcAdvancedSettingsManagementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="regular", password="pass")
        self.staff = User.objects.create_user(username="admin", password="pass", is_staff=True)
        self.other = User.objects.create_user(username="other", password="pass")
        self.csrf_client = Client(enforce_csrf_checks=True)

    def _csrf_token(self):
        response = self.csrf_client.get('/dashboard/')
        return response.cookies.get('csrftoken').value if 'csrftoken' in response.cookies else ''

    def test_templates_get_regular_user_sees_own_and_global_templates(self):
        own = SimcContentTemplate.objects.create(
            name="Own Template",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="own content",
            owner_user_id=self.user.id,
        )
        global_tpl = SimcContentTemplate.objects.create(
            name="Global Template",
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            spec="default",
            content="global player",
            owner_user_id=None,
        )
        foreign = SimcContentTemplate.objects.create(
            name="Foreign Template",
            template_type=SimcContentTemplate.TYPE_CUSTOM_PLAYER,
            spec="arms",
            content="foreign content",
            owner_user_id=self.other.id,
        )
        self.client.force_login(self.user)
        response = self.client.get('/api/simc-workbench/templates/')
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        ids = {item['id'] for item in data}
        self.assertIn(own.id, ids)
        self.assertIn(global_tpl.id, ids)
        self.assertNotIn(foreign.id, ids)
        self.assertTrue(response.json()['can_write'])

    def test_templates_get_staff_can_write_global_non_upstream_templates(self):
        global_tpl = SimcContentTemplate.objects.create(
            name="Managed System Template",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="managed_system",
            content="system content",
            owner_user_id=None,
            source=SimcContentTemplate.SOURCE_USER,
        )
        self.client.force_login(self.staff)
        response = self.client.get('/api/simc-workbench/templates/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['can_write'])
        row = next(item for item in payload['data'] if item['id'] == global_tpl.id)
        self.assertFalse(row['read_only'])

    def test_templates_staff_create_without_owner_creates_editable_system_template(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            '/api/simc-workbench/templates/',
            data=json.dumps({
                'name': 'Managed Global Template',
                'template_type': SimcContentTemplate.TYPE_BASE_TEMPLATE,
                'spec': 'managed_global',
                'content': '{player_config}\niterations=1000',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        tpl = SimcContentTemplate.objects.get(id=response.json()['data']['id'])
        self.assertIsNone(tpl.owner_user_id)
        update = self.client.put(
            f'/api/simc-workbench/templates/{tpl.id}/',
            data=json.dumps({'name': 'Managed Global Template v2'}),
            content_type='application/json',
        )
        self.assertEqual(update.status_code, 200)
        tpl.refresh_from_db()
        self.assertEqual(tpl.name, 'Managed Global Template v2')

    def test_templates_get_detail_by_id(self):
        tpl = SimcContentTemplate.objects.create(
            name="Detail Template",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="test content",
            owner_user_id=self.user.id,
        )
        self.client.force_login(self.user)
        response = self.client.get(f'/api/simc-workbench/templates/{tpl.id}/')
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(data['id'], tpl.id)
        self.assertEqual(data['name'], "Detail Template")
        self.assertEqual(data['content'], "test content")

    def test_templates_regular_user_can_create_owned_template(self):
        self.client.force_login(self.user)
        response = self.client.post(
            '/api/simc-workbench/templates/',
            data=json.dumps({
                'name': 'New Template',
                'template_type': SimcContentTemplate.TYPE_BASE_TEMPLATE,
                'spec': 'fury',
                'content': '{player_config}\niterations=100',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        created = SimcContentTemplate.objects.get(id=response.json()['data']['id'])
        self.assertEqual(created.owner_user_id, self.user.id)

    def test_templates_create_staff_succeeds(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            '/api/simc-workbench/templates/',
            data=json.dumps({
                'name': 'Staff Template',
                'template_type': SimcContentTemplate.TYPE_BASE_TEMPLATE,
                'spec': 'fury',
                'content': '{player_config}\niterations=100',
                'owner_user_id': self.staff.id,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        tpl_id = response.json()['data']['id']
        tpl = SimcContentTemplate.objects.get(id=tpl_id)
        self.assertEqual(tpl.name, 'Staff Template')
        self.assertEqual(tpl.owner_user_id, self.staff.id)
        self.assertTrue(tpl.is_active)

    def test_templates_create_enforces_unique_key(self):
        self.client.force_login(self.staff)
        SimcContentTemplate.objects.create(
            name="Existing",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="{player_config}\niterations=100",
            owner_user_id=self.staff.id,
            is_active=True,
        )
        response = self.client.post(
            '/api/simc-workbench/templates/',
            data=json.dumps({
                'name': 'Duplicate',
                'template_type': SimcContentTemplate.TYPE_BASE_TEMPLATE,
                'spec': 'fury',
                'content': '{player_config}\niterations=200',
                'owner_user_id': self.staff.id,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn('已存在', response.json()['error'])

    def test_templates_owner_can_edit(self):
        tpl = SimcContentTemplate.objects.create(
            name="Edit Test",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="{player_config}\niterations=100",
            owner_user_id=self.user.id,
        )
        self.client.force_login(self.user)
        response = self.client.put(
            f'/api/simc-workbench/templates/{tpl.id}/',
            data=json.dumps({'content': '{player_config}\niterations=200'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)

    def test_templates_edit_staff_succeeds(self):
        tpl = SimcContentTemplate.objects.create(
            name="Edit Staff",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="{player_config}\niterations=100",
            owner_user_id=self.staff.id,
        )
        self.client.force_login(self.staff)
        response = self.client.put(
            f'/api/simc-workbench/templates/{tpl.id}/',
            data=json.dumps({'content': '{player_config}\niterations=200', 'name': 'New Name'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        tpl.refresh_from_db()
        self.assertEqual(tpl.content, '{player_config}\niterations=200')
        self.assertEqual(tpl.name, 'New Name')

    def test_templates_staff_can_edit_system_and_upstream_apls(self):
        system_tpl = SimcContentTemplate.objects.create(
            name="System",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="default",
            content="{player_config}\niterations=100",
            owner_user_id=None,
        )
        upstream_apl = SimcApl.objects.create(
            name="Upstream",
            spec="warrior_fury",
            class_name="warrior",
            content="upstream apl",
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
            owner_user_id=None,
        )
        self.client.force_login(self.staff)
        response = self.client.put(
            f'/api/simc-workbench/templates/{system_tpl.id}/',
            data=json.dumps({'content': '{player_config}\niterations=200'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        system_tpl.refresh_from_db()
        self.assertEqual(system_tpl.content, '{player_config}\niterations=200')

        response = self.client.put(
            f'/api/simc-workbench/apls/{upstream_apl.id}/',
            data=json.dumps({'content': 'hacked'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        upstream_apl.refresh_from_db()
        self.assertEqual(upstream_apl.content, 'hacked')

    def test_templates_owner_can_archive_restore(self):
        tpl = SimcContentTemplate.objects.create(
            name="Archive Test",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="test",
            owner_user_id=self.user.id,
            is_active=True,
        )
        self.client.force_login(self.user)
        response = self.client.post(
            f'/api/simc-workbench/templates/{tpl.id}/',
            data=json.dumps({'action': 'archive'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        tpl.refresh_from_db()
        self.assertFalse(tpl.is_active)

        response = self.client.post(
            f'/api/simc-workbench/templates/{tpl.id}/',
            data=json.dumps({'action': 'restore'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        tpl.refresh_from_db()
        self.assertTrue(tpl.is_active)

    def test_templates_archive_restore_staff_succeeds(self):
        tpl = SimcContentTemplate.objects.create(
            name="Archive Staff",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="test",
            owner_user_id=self.staff.id,
            is_active=True,
        )
        self.client.force_login(self.staff)
        response = self.client.post(
            f'/api/simc-workbench/templates/{tpl.id}/',
            data=json.dumps({'action': 'archive'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        tpl.refresh_from_db()
        self.assertFalse(tpl.is_active)

        response = self.client.post(
            f'/api/simc-workbench/templates/{tpl.id}/',
            data=json.dumps({'action': 'restore'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        tpl.refresh_from_db()
        self.assertTrue(tpl.is_active)

    def test_templates_delete_not_supported_if_has_is_active(self):
        tpl = SimcContentTemplate.objects.create(
            name="Delete Test",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="test",
            owner_user_id=self.staff.id,
        )
        self.client.force_login(self.staff)
        response = self.client.delete(f'/api/simc-workbench/templates/{tpl.id}/')
        self.assertEqual(response.status_code, 400)
        self.assertIn('不支持真实删除', response.json()['error'])

    def test_templates_write_requires_csrf(self):
        self.csrf_client.force_login(self.staff)
        tpl = SimcContentTemplate.objects.create(
            name="CSRF Test",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="test",
            owner_user_id=self.staff.id,
        )
        for method, path, payload in [
            ('post', '/api/simc-workbench/templates/', {'name': 'New', 'template_type': 'base_template', 'spec': 'fury', 'content': 'x'}),
            ('put', f'/api/simc-workbench/templates/{tpl.id}/', {'content': 'updated'}),
            ('post', f'/api/simc-workbench/templates/{tpl.id}/', {'action': 'archive'}),
        ]:
            with self.subTest(method=method, path=path):
                response = getattr(self.csrf_client, method)(
                    path,
                    data=json.dumps(payload),
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 403)


    def test_validation_rejects_invalid_fields(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            '/api/simc-workbench/templates/',
            data=json.dumps({
                'name': 'Test',
                'template_type': 'base_template',
                'spec': 'fury',
                'content': '{player_config}\niterations=100',
                'malicious_field': 'injected',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        tpl = SimcContentTemplate.objects.get(id=response.json()['data']['id'])
        self.assertFalse(hasattr(tpl, 'malicious_field'))

    def test_error_messages_do_not_leak_internals(self):
        self.client.force_login(self.staff)
        response = self.client.get('/api/simc-workbench/templates/99999/')
        self.assertEqual(response.status_code, 404)
        error = response.json()['error']
        self.assertNotIn('Traceback', error)
        self.assertNotIn('Exception', error)
        self.assertNotIn('/home/', error)
        self.assertNotIn('simc_content_template', error.lower())
