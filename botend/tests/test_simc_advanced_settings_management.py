"""
测试 SimC 高级设置（templates/apl-keywords）的完整管理闭环。
包括权限、CSRF、owner 隔离、唯一键校验、只读模板保护。
"""
import json

from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.models import SimcContentTemplate, SimcAplKeywordPair


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
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            spec="default",
            content="global apl",
            owner_user_id=None,
        )
        foreign = SimcContentTemplate.objects.create(
            name="Foreign Template",
            template_type=SimcContentTemplate.TYPE_CUSTOM_APL,
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
        self.assertFalse(response.json()['can_write'])

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

    def test_templates_create_requires_staff(self):
        self.client.force_login(self.user)
        response = self.client.post(
            '/api/simc-workbench/templates/',
            data=json.dumps({
                'name': 'New Template',
                'template_type': SimcContentTemplate.TYPE_BASE_TEMPLATE,
                'spec': 'fury',
                'content': 'new content',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn('仅管理员可创建模板', response.json()['error'])

    def test_templates_create_staff_succeeds(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            '/api/simc-workbench/templates/',
            data=json.dumps({
                'name': 'Staff Template',
                'template_type': SimcContentTemplate.TYPE_BASE_TEMPLATE,
                'spec': 'fury',
                'content': 'staff content',
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
            content="first",
            owner_user_id=self.staff.id,
            is_active=True,
        )
        response = self.client.post(
            '/api/simc-workbench/templates/',
            data=json.dumps({
                'name': 'Duplicate',
                'template_type': SimcContentTemplate.TYPE_BASE_TEMPLATE,
                'spec': 'fury',
                'content': 'second',
                'owner_user_id': self.staff.id,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn('已存在', response.json()['error'])

    def test_templates_edit_requires_staff(self):
        tpl = SimcContentTemplate.objects.create(
            name="Edit Test",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="original",
            owner_user_id=self.user.id,
        )
        self.client.force_login(self.user)
        response = self.client.put(
            f'/api/simc-workbench/templates/{tpl.id}/',
            data=json.dumps({'content': 'updated'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn('仅管理员可编辑模板', response.json()['error'])

    def test_templates_edit_staff_succeeds(self):
        tpl = SimcContentTemplate.objects.create(
            name="Edit Staff",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="original",
            owner_user_id=self.staff.id,
        )
        self.client.force_login(self.staff)
        response = self.client.put(
            f'/api/simc-workbench/templates/{tpl.id}/',
            data=json.dumps({'content': 'updated content', 'name': 'New Name'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        tpl.refresh_from_db()
        self.assertEqual(tpl.content, 'updated content')
        self.assertEqual(tpl.name, 'New Name')

    def test_templates_staff_can_edit_system_but_not_upstream_templates(self):
        system_tpl = SimcContentTemplate.objects.create(
            name="System",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="default",
            content="system content",
            owner_user_id=None,
        )
        upstream_tpl = SimcContentTemplate.objects.create(
            name="Upstream",
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            spec="fury",
            content="upstream apl",
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            owner_user_id=self.staff.id,
        )
        self.client.force_login(self.staff)
        response = self.client.put(
            f'/api/simc-workbench/templates/{system_tpl.id}/',
            data=json.dumps({'content': 'managed system content'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        system_tpl.refresh_from_db()
        self.assertEqual(system_tpl.content, 'managed system content')

        response = self.client.put(
            f'/api/simc-workbench/templates/{upstream_tpl.id}/',
            data=json.dumps({'content': 'hacked'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn('只读', response.json()['error'])
        upstream_tpl.refresh_from_db()
        self.assertNotEqual(upstream_tpl.content, 'hacked')

    def test_templates_archive_restore_requires_staff(self):
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
        self.assertEqual(response.status_code, 403)
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

    def test_apl_keywords_get_returns_list_and_can_write_flag(self):
        SimcAplKeywordPair.objects.create(
            apl_keyword="actions=/test",
            cn_keyword="测试",
            description="test keyword",
        )
        self.client.force_login(self.user)
        response = self.client.get('/api/simc-workbench/apl-keywords/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('data', data)
        self.assertIn('can_write', data)
        self.assertFalse(data['can_write'])

        self.client.force_login(self.staff)
        response = self.client.get('/api/simc-workbench/apl-keywords/')
        self.assertTrue(response.json()['can_write'])

    def test_apl_keywords_create_requires_staff(self):
        self.client.force_login(self.user)
        response = self.client.post(
            '/api/simc-workbench/apl-keywords/',
            data=json.dumps({
                'apl_keyword': 'actions=/new',
                'cn_keyword': '新建',
                'description': 'new keyword',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn('仅管理员可修改', response.json()['error'])

    def test_apl_keywords_create_staff_succeeds(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            '/api/simc-workbench/apl-keywords/',
            data=json.dumps({
                'apl_keyword': 'actions=/staff',
                'cn_keyword': '管理员',
                'description': 'staff keyword',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        kw_id = response.json()['data']['id']
        kw = SimcAplKeywordPair.objects.get(id=kw_id)
        self.assertEqual(kw.apl_keyword, 'actions=/staff')
        self.assertEqual(kw.cn_keyword, '管理员')
        self.assertTrue(kw.is_active)

    def test_apl_keywords_enforces_unique_apl_keyword_when_active(self):
        self.client.force_login(self.staff)
        SimcAplKeywordPair.objects.create(
            apl_keyword="actions=/duplicate",
            cn_keyword="重复",
            is_active=True,
        )
        response = self.client.post(
            '/api/simc-workbench/apl-keywords/',
            data=json.dumps({
                'apl_keyword': 'actions=/duplicate',
                'cn_keyword': '再次',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn('已存在', response.json()['error'])

    def test_apl_keywords_edit_requires_staff(self):
        kw = SimcAplKeywordPair.objects.create(
            apl_keyword="actions=/edit",
            cn_keyword="编辑",
        )
        self.client.force_login(self.user)
        response = self.client.put(
            f'/api/simc-workbench/apl-keywords/{kw.id}/',
            data=json.dumps({'cn_keyword': '修改'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn('仅管理员可修改', response.json()['error'])

    def test_apl_keywords_edit_staff_succeeds(self):
        kw = SimcAplKeywordPair.objects.create(
            apl_keyword="actions=/edit",
            cn_keyword="编辑",
            description="old",
        )
        self.client.force_login(self.staff)
        response = self.client.put(
            f'/api/simc-workbench/apl-keywords/{kw.id}/',
            data=json.dumps({'cn_keyword': '修改后', 'description': 'new'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        kw.refresh_from_db()
        self.assertEqual(kw.cn_keyword, '修改后')
        self.assertEqual(kw.description, 'new')

    def test_apl_keywords_archive_restore_requires_staff(self):
        kw = SimcAplKeywordPair.objects.create(
            apl_keyword="actions=/archive",
            cn_keyword="归档",
            is_active=True,
        )
        self.client.force_login(self.user)
        response = self.client.post(
            f'/api/simc-workbench/apl-keywords/{kw.id}/',
            data=json.dumps({'action': 'archive'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        kw.refresh_from_db()
        self.assertTrue(kw.is_active)

    def test_apl_keywords_archive_restore_staff_succeeds(self):
        kw = SimcAplKeywordPair.objects.create(
            apl_keyword="actions=/archive_staff",
            cn_keyword="归档",
            is_active=True,
        )
        self.client.force_login(self.staff)
        response = self.client.post(
            f'/api/simc-workbench/apl-keywords/{kw.id}/',
            data=json.dumps({'action': 'archive'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        kw.refresh_from_db()
        self.assertFalse(kw.is_active)

        response = self.client.post(
            f'/api/simc-workbench/apl-keywords/{kw.id}/',
            data=json.dumps({'action': 'restore'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        kw.refresh_from_db()
        self.assertTrue(kw.is_active)

    def test_apl_keywords_write_requires_csrf(self):
        self.csrf_client.force_login(self.staff)
        kw = SimcAplKeywordPair.objects.create(
            apl_keyword="actions=/csrf",
            cn_keyword="测试",
        )
        for method, path, payload in [
            ('post', '/api/simc-workbench/apl-keywords/', {'apl_keyword': 'actions=/new', 'cn_keyword': 'x'}),
            ('put', f'/api/simc-workbench/apl-keywords/{kw.id}/', {'cn_keyword': 'updated'}),
            ('post', f'/api/simc-workbench/apl-keywords/{kw.id}/', {'action': 'archive'}),
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
                'content': 'test',
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
