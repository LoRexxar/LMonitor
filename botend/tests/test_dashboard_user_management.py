import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase


User = get_user_model()
ROOT = Path(__file__).resolve().parents[2]


class DashboardUserManagementApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='user-admin',
            email='admin@example.com',
            password='Admin-pass-123!',
        )
        self.staff = User.objects.create_user(
            username='staff-user',
            password='Staff-pass-123!',
            is_staff=True,
        )

    def login(self, user=None):
        self.client.force_login(user or self.admin)

    def test_only_superusers_can_access_user_management_api(self):
        self.login(self.staff)
        response = self.client.get('/api/dashboard/users/')
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            '/api/dashboard/users/',
            data=json.dumps({'username': 'blocked-user', 'password': 'Blocked-pass-123!'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username='blocked-user').exists())

    def test_superuser_can_list_search_and_page_users_without_password_data(self):
        User.objects.create_user(username='alpha-user', email='alpha@example.com', password='Alpha-pass-123!')
        User.objects.create_user(username='beta-user', email='beta@example.com', password='Beta-pass-123!')
        self.login()

        response = self.client.get('/api/dashboard/users/?search=alpha&page=1&page_size=10')
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload['status'], 'success')
        self.assertEqual(payload['total_count'], 1)
        self.assertEqual(payload['page'], 1)
        self.assertEqual(payload['page_size'], 10)
        self.assertEqual(payload['total_pages'], 1)
        row = payload['data'][0]
        self.assertEqual(row['username'], 'alpha-user')
        self.assertEqual(row['email'], 'alpha@example.com')
        self.assertNotIn('password', row)
        self.assertIn('date_joined', row)
        self.assertIn('last_login', row)

    def test_superuser_can_create_user_with_hashed_password_and_flags(self):
        self.login()
        response = self.client.post(
            '/api/dashboard/users/',
            data=json.dumps({
                'username': 'created-user',
                'email': 'created@example.com',
                'first_name': '测试',
                'last_name': '用户',
                'password': 'Created-pass-123!',
                'is_active': True,
                'is_staff': True,
                'is_superuser': False,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        created = User.objects.get(username='created-user')
        self.assertTrue(created.check_password('Created-pass-123!'))
        self.assertNotEqual(created.password, 'Created-pass-123!')
        self.assertTrue(created.is_staff)
        self.assertFalse(created.is_superuser)
        self.assertNotIn('password', response.json()['data'])

    def test_create_defaults_to_regular_member_when_role_is_omitted(self):
        self.login()
        response = self.client.post(
            '/api/dashboard/users/',
            data=json.dumps({'username': 'default-member', 'password': 'ValidDefault123!'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 201, response.content)
        created = User.objects.get(username='default-member')
        self.assertTrue(created.is_active)
        self.assertFalse(created.is_staff)
        self.assertFalse(created.is_superuser)

    def test_quick_create_generates_one_time_password_for_regular_member(self):
        self.login()
        response = self.client.post(
            '/api/dashboard/users/',
            data=json.dumps({'username': 'quick-member', 'quick_create': True}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()['data']
        password = payload['generated_password']
        self.assertEqual(len(password), 16)
        self.assertTrue(password.isalnum())
        created = User.objects.get(username='quick-member')
        self.assertTrue(created.check_password(password))
        self.assertTrue(created.is_active)
        self.assertFalse(created.is_staff)
        self.assertFalse(created.is_superuser)

        list_payload = self.client.get('/api/dashboard/users/?search=quick-member').json()['data'][0]
        self.assertNotIn('generated_password', list_payload)
        self.assertNotIn('password', list_payload)

    def test_quick_create_accepts_only_username_and_mode(self):
        self.login()
        response = self.client.post(
            '/api/dashboard/users/',
            data=json.dumps({
                'username': 'unsafe-quick-member',
                'quick_create': True,
                'is_staff': True,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username='unsafe-quick-member').exists())

    def test_superuser_can_edit_profile_flags_and_optionally_reset_password(self):
        target = User.objects.create_user(
            username='editable-user',
            email='old@example.com',
            password='Old-pass-123!',
        )
        self.login()
        response = self.client.patch(
            f'/api/dashboard/users/{target.pk}/',
            data=json.dumps({
                'email': 'new@example.com',
                'first_name': '新',
                'is_staff': True,
                'password': 'New-pass-123!',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        target.refresh_from_db()
        self.assertEqual(target.email, 'new@example.com')
        self.assertEqual(target.first_name, '新')
        self.assertTrue(target.is_staff)
        self.assertTrue(target.check_password('New-pass-123!'))

    def test_edit_without_password_preserves_existing_password_hash(self):
        self.client.force_login(self.admin)
        target = User.objects.create_user(username='preserve-me', password='OldPass!234')
        old_hash = target.password
        response = self.client.patch(
            f'/api/dashboard/users/{target.pk}/',
            data=json.dumps({'email': 'preserved@example.com'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        target.refresh_from_db()
        self.assertEqual(target.password, old_hash)
        self.assertTrue(target.check_password('OldPass!234'))

    def test_resetting_own_password_keeps_current_admin_session_authenticated(self):
        self.client.force_login(self.admin)
        response = self.client.patch(
            f'/api/dashboard/users/{self.admin.pk}/',
            data=json.dumps({'password': 'ChangedAdminPass!234'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password('ChangedAdminPass!234'))
        follow_up = self.client.get('/api/dashboard/users/')
        self.assertEqual(follow_up.status_code, 200, follow_up.content)

    def test_admin_cannot_deactivate_or_remove_own_superuser_access(self):
        self.login()
        for payload in (
            {'is_active': False},
            {'is_staff': False},
            {'is_superuser': False},
        ):
            with self.subTest(payload=payload):
                response = self.client.patch(
                    f'/api/dashboard/users/{self.admin.pk}/',
                    data=json.dumps(payload),
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 400)
                self.admin.refresh_from_db()
                self.assertTrue(self.admin.is_active)
                self.assertTrue(self.admin.is_staff)
                self.assertTrue(self.admin.is_superuser)

    def test_unknown_fields_and_invalid_json_are_rejected_without_partial_write(self):
        self.login()
        before = User.objects.count()
        response = self.client.post(
            '/api/dashboard/users/',
            data=json.dumps({
                'username': 'unsafe-user',
                'password': 'Unsafe-pass-123!',
                'date_joined': '2000-01-01',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.count(), before)

        response = self.client.post(
            '/api/dashboard/users/',
            data='{invalid',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.count(), before)


class DashboardUserManagementFrontendContractTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username='frontend-admin', password='Admin-pass-123!')
        self.staff = User.objects.create_user(username='frontend-staff', password='Staff-pass-123!', is_staff=True)

    def test_user_management_navigation_and_panel_are_superuser_only(self):
        self.client.force_login(self.admin)
        response = self.client.get('/dashboard/')
        self.assertContains(response, 'data-section="user-management"')
        self.assertContains(response, 'id="user-management"')
        self.assertContains(response, '用户管理')

        self.client.force_login(self.staff)
        response = self.client.get('/dashboard/')
        self.assertNotContains(response, 'data-section="user-management"')
        self.assertNotContains(response, 'id="user-management"')

    def test_frontend_uses_dedicated_api_and_never_reads_or_prefills_password_hashes(self):
        javascript = (ROOT / 'static/dashboard/js/user_management.js').read_text(encoding='utf-8')
        self.assertIn("'/api/dashboard/users/'", javascript)
        self.assertIn("'PATCH'", javascript)
        self.assertIn("'POST'", javascript)
        self.assertNotIn('password_hash', javascript)
        self.assertNotIn("user.password", javascript)
        self.assertIn("if (!id || password) payload.password = password", javascript)

    def test_frontend_exposes_quick_regular_member_creation_and_copy_result(self):
        self.client.force_login(self.admin)
        response = self.client.get('/dashboard/')
        self.assertContains(response, 'id="user-management-quick-add"')
        self.assertContains(response, '快速创建普通会员')

        javascript = (ROOT / 'static/dashboard/js/user_management.js').read_text(encoding='utf-8')
        self.assertIn('quick_create: true', javascript)
        self.assertIn('generated_password', javascript)
        self.assertIn('navigator.clipboard', javascript)
        self.assertIn('权限：普通会员', javascript)
