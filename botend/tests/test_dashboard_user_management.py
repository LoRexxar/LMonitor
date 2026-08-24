import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

from botend.models import DashboardUserGroup, DashboardUserGroupMembership


User = get_user_model()
ROOT = Path(__file__).resolve().parents[2]


class DashboardPageAccessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='page-user', password='PageUser-pass-123!')
        self.staff = User.objects.create_user(
            username='page-staff', password='PageStaff-pass-123!', is_staff=True
        )
        self.admin = User.objects.create_superuser(
            username='page-admin', password='PageAdmin-pass-123!', email='page-admin@example.com'
        )

    def test_user_without_group_does_not_default_to_database_section(self):
        self.client.force_login(self.user)
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'window.DASHBOARD_DEFAULT_SECTION = "";')
        self.assertContains(response, 'dashboard-permissions-data')

    def test_staff_without_group_has_no_implicit_dashboard_page_access(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get('/dashboard/?section=database-tables').status_code, 403)
        self.assertEqual(self.client.get('/dashboard/?section=news').status_code, 403)

    def test_enabled_group_permission_controls_default_section_and_union(self):
        group = DashboardUserGroup.objects.create(
            name='页面组', permission_codes=['news.index'], is_active=True
        )
        DashboardUserGroupMembership.objects.create(user=self.user, group=group)
        self.client.force_login(self.user)
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'window.DASHBOARD_DEFAULT_SECTION = "news";')
        self.assertEqual(self.client.get('/dashboard/?section=news').status_code, 200)
        self.assertEqual(self.client.get('/dashboard/?section=database-tables').status_code, 403)

    def test_superuser_can_open_skill_damage_section_directly(self):
        self.client.force_login(self.admin)

        response = self.client.get('/dashboard/?section=simc-skill-damage')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['dashboard_default_section'], 'simc-skill-damage')

    def test_superuser_can_access_database_section_without_business_group(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/dashboard/?section=database-tables').status_code, 200)

    def test_wcl_page_and_api_require_wcl_dashboard_permission(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/wcl-analysis/').status_code, 403)
        self.assertEqual(self.client.get('/api/wcl-analysis-task/').status_code, 403)

        group = DashboardUserGroup.objects.create(
            name='WCL 组', permission_codes=['tools.wcl-analysis'], is_active=True,
        )
        DashboardUserGroupMembership.objects.create(user=self.user, group=group)
        self.assertEqual(self.client.get('/wcl-analysis/').status_code, 200)
        self.assertEqual(self.client.get('/api/wcl-analysis-task/').status_code, 200)

    def test_dashboard_post_actions_require_their_page_permissions(self):
        self.client.force_login(self.staff)
        for action in ('list_log_files', 'read_log_file', 'force_run_task'):
            with self.subTest(action=action):
                response = self.client.post(
                    '/dashboard/',
                    data=json.dumps({'action': action}),
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 403)

        group = DashboardUserGroup.objects.create(
            name='日志和首页组', permission_codes=['system.logs', 'dashboard.home']
        )
        DashboardUserGroupMembership.objects.create(user=self.staff, group=group)
        self.assertEqual(
            self.client.post(
                '/dashboard/', data=json.dumps({'action': 'list_log_files'}),
                content_type='application/json',
            ).status_code,
            200,
        )

    def test_system_alert_api_requires_system_alert_permission(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/api/system-alert/').status_code, 403)

        group = DashboardUserGroup.objects.create(
            name='系统报警组', permission_codes=['system.alerts'], is_active=True,
        )
        DashboardUserGroupMembership.objects.create(user=self.user, group=group)
        self.assertEqual(self.client.get('/api/system-alert/').status_code, 200)

    def test_wago_apis_require_their_dashboard_permissions(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/api/wago-hotfix-reports/').status_code, 403)
        response = self.client.post(
            '/api/wago-skill-diff/rerun/',
            data=json.dumps({'event_id': 1}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

        group = DashboardUserGroup.objects.create(
            name='Hotfix 组', permission_codes=['reports.hotfix'], is_active=True,
        )
        DashboardUserGroupMembership.objects.create(user=self.user, group=group)
        self.assertEqual(self.client.get('/api/wago-hotfix-reports/').status_code, 200)

    def test_legacy_simc_pages_require_simc_history_permission(self):
        self.client.force_login(self.user)
        for url in (
            '/simc-result/',
            '/simc-attribute-analysis/',
            '/simc-attribute-analysis-ssr/',
            '/simc-compare/',
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

        group = DashboardUserGroup.objects.create(
            name='SimC 历史组', permission_codes=['simc.history'], is_active=True,
        )
        DashboardUserGroupMembership.objects.create(user=self.user, group=group)
        self.assertEqual(self.client.get('/simc-result/').status_code, 200)
        self.assertEqual(self.client.get('/simc-attribute-analysis/').status_code, 200)
        self.assertEqual(self.client.get('/simc-compare/').status_code, 200)

    def test_user_management_script_is_available_for_business_group_users(self):
        template = (ROOT / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        script = "{% static 'dashboard/js/user_management.js' %}"
        self.assertIn(script, template)
        script_start = template.index(script)
        self.assertNotIn('{% if user.is_superuser %}', template[script_start - 80:script_start])


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

    def test_superuser_can_create_edit_and_assign_independent_user_group(self):
        self.login()
        response = self.client.post(
            '/api/dashboard/user-groups/',
            data=json.dumps({'name': '内容审核组', 'description': '负责内容审核', 'is_active': True}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        group = DashboardUserGroup.objects.get(name='内容审核组')
        self.assertEqual(group.description, '负责内容审核')

        response = self.client.post(
            '/api/dashboard/users/',
            data=json.dumps({
                'username': 'group-member',
                'password': 'GroupMember-pass-123!',
                'user_group_ids': [group.pk],
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        member = User.objects.get(username='group-member')
        self.assertFalse(member.is_staff)
        self.assertFalse(member.is_superuser)
        self.assertFalse(member.has_perm('botend.change_monitortask'))
        self.assertEqual(response.json()['data']['user_groups'], [{'id': group.pk, 'name': group.name}])

        response = self.client.patch(
            f'/api/dashboard/user-groups/{group.pk}/',
            data=json.dumps({'name': '内容审核二组', 'description': '已调整', 'is_active': False}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        group.refresh_from_db()
        self.assertEqual(group.name, '内容审核二组')
        self.assertEqual(group.description, '已调整')
        self.assertFalse(group.is_active)

    def test_group_rejects_permission_fields_and_unknown_membership_ids(self):
        self.login()
        response = self.client.post(
            '/api/dashboard/user-groups/',
            data=json.dumps({'name': '越权组', 'permission_ids': [1]}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(DashboardUserGroup.objects.filter(name='越权组').exists())

        response = self.client.post(
            '/api/dashboard/users/',
            data=json.dumps({
                'username': 'invalid-group-member',
                'password': 'InvalidGroup-pass-123!',
                'user_group_ids': [999999],
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username='invalid-group-member').exists())

    def test_user_can_belong_to_multiple_business_groups(self):
        first = DashboardUserGroup.objects.create(name='一组')
        second = DashboardUserGroup.objects.create(name='二组')
        member = User.objects.create_user(username='multi-group-user', password='MultiGroup-pass-123!')
        DashboardUserGroupMembership.objects.create(user=member, group=first)
        DashboardUserGroupMembership.objects.create(user=member, group=second)
        self.assertEqual(member.dashboard_user_groups.count(), 2)

    def test_inactive_group_keeps_existing_members_but_rejects_new_assignment(self):
        group = DashboardUserGroup.objects.create(name='停用组', is_active=False)
        existing = User.objects.create_user(username='existing-group-user', password='Existing-pass-123!')
        DashboardUserGroupMembership.objects.create(user=existing, group=group)
        self.login()

        response = self.client.post(
            '/api/dashboard/users/',
            data=json.dumps({
                'username': 'blocked-inactive-group-user',
                'password': 'BlockedInactive-pass-123!',
                'user_group_ids': [group.pk],
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(User.objects.filter(username='blocked-inactive-group-user').exists())
        self.assertTrue(group.users.filter(pk=existing.pk).exists())

    def test_group_permissions_are_union_and_inactive_groups_are_ignored(self):
        from botend.dashboard.permissions import effective_dashboard_permissions
        first = DashboardUserGroup.objects.create(name='权限一组', permission_codes=['news.index'])
        second = DashboardUserGroup.objects.create(name='权限二组', permission_codes=['reports.hotfix'])
        member = User.objects.create_user(username='permission-union-user', password='PermissionUnion-pass-123!')
        DashboardUserGroupMembership.objects.create(user=member, group=first)
        DashboardUserGroupMembership.objects.create(user=member, group=second)
        self.assertEqual(effective_dashboard_permissions(member), {'news.index', 'reports.hotfix'})
        second.is_active = False
        second.save(update_fields=['is_active'])
        self.assertEqual(effective_dashboard_permissions(member), {'news.index'})

    def test_group_api_updates_permissions_and_members_atomically(self):
        self.login()
        first = User.objects.create_user(username='group-api-first', password='GroupApiFirst-pass-123!')
        second = User.objects.create_user(username='group-api-second', password='GroupApiSecond-pass-123!')
        response = self.client.post('/api/dashboard/user-groups/', data=json.dumps({
            'name': 'API 组', 'permission_codes': ['news.index'], 'user_ids': [first.pk],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        group = DashboardUserGroup.objects.get(name='API 组')
        self.assertEqual(response.json()['data']['users'], [{'id': first.pk, 'username': first.username}])
        response = self.client.patch(f'/api/dashboard/user-groups/{group.pk}/', data=json.dumps({
            'permission_codes': ['reports.hotfix'], 'user_ids': [second.pk],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        group.refresh_from_db()
        self.assertEqual(group.permission_codes, ['reports.hotfix'])
        self.assertEqual(list(group.users.values_list('pk', flat=True)), [second.pk])

    def test_unknown_page_permission_code_is_rejected(self):
        self.login()
        response = self.client.post('/api/dashboard/user-groups/', data=json.dumps({
            'name': '未知权限组', 'permission_codes': ['not.registered'],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(DashboardUserGroup.objects.filter(name='未知权限组').exists())
    def test_group_user_cannot_create_or_modify_groups(self):
        group = DashboardUserGroup.objects.create(name='普通组')
        member = User.objects.create_user(username='group-only', password='GroupOnly-pass-123!')
        group.users.add(member)
        self.login(member)
        response = self.client.get('/api/dashboard/user-groups/')
        self.assertEqual(response.status_code, 403)

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

    def test_superuser_can_generate_one_time_password_for_existing_user(self):
        target = User.objects.create_user(
            username='reset-target',
            password='Old-pass-123!',
        )
        old_hash = target.password
        self.login()

        response = self.client.patch(
            f'/api/dashboard/users/{target.pk}/',
            data=json.dumps({'reset_password': True}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response['Pragma'], 'no-cache')
        payload = response.json()['data']
        password = payload['generated_password']
        self.assertEqual(len(password), 16)
        self.assertTrue(password.isalnum())
        target.refresh_from_db()
        self.assertNotEqual(target.password, old_hash)
        self.assertTrue(target.check_password(password))

        list_payload = self.client.get('/api/dashboard/users/?search=reset-target').json()['data'][0]
        self.assertNotIn('generated_password', list_payload)
        self.assertNotIn('password', list_payload)

    def test_non_superusers_cannot_reset_password(self):
        target = User.objects.create_user(username='protected-reset-target', password='Old-pass-123!')
        old_hash = target.password
        for caller in (self.staff, User.objects.create_user(username='regular-reset-caller', password='Regular-pass-123!')):
            with self.subTest(caller=caller.username):
                self.login(caller)
                response = self.client.patch(
                    f'/api/dashboard/users/{target.pk}/',
                    data=json.dumps({'reset_password': True}),
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 403)
                target.refresh_from_db()
                self.assertEqual(target.password, old_hash)
        self.client.logout()
        response = self.client.patch(
            f'/api/dashboard/users/{target.pk}/',
            data=json.dumps({'reset_password': True}),
            content_type='application/json',
        )
        self.assertIn(response.status_code, (302, 403))
        target.refresh_from_db()
        self.assertEqual(target.password, old_hash)

    def test_generated_password_reset_rejects_extra_fields_and_false_mode(self):
        target = User.objects.create_user(username='safe-reset-target', password='Old-pass-123!')
        old_hash = target.password
        self.login()

        for payload in (
            {'reset_password': False},
            {'reset_password': True, 'is_staff': True},
            {'reset_password': True, 'password': 'Injected-pass-123!'},
        ):
            with self.subTest(payload=payload):
                response = self.client.patch(
                    f'/api/dashboard/users/{target.pk}/',
                    data=json.dumps(payload),
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 400)
                target.refresh_from_db()
                self.assertEqual(target.password, old_hash)

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

    def test_user_management_page_container_is_permission_filtered_but_api_remains_superuser_only(self):
        group = DashboardUserGroup.objects.create(
            name='用户管理页面组', permission_codes=['dashboard.user-management']
        )
        DashboardUserGroupMembership.objects.create(user=self.staff, group=group)
        self.client.force_login(self.admin)
        response = self.client.get('/dashboard/')
        self.assertContains(response, 'data-section="user-management"')
        self.assertContains(response, 'id="user-management"')
        self.assertContains(response, '用户管理')

        self.client.force_login(self.staff)
        response = self.client.get('/dashboard/')
        self.assertContains(response, 'data-section="user-management"')
        self.assertContains(response, 'id="user-management"')
        self.assertEqual(self.client.get('/api/dashboard/users/').status_code, 403)

    def test_frontend_uses_dedicated_api_and_never_reads_or_prefills_password_hashes(self):
        javascript = (ROOT / 'static/dashboard/js/user_management.js').read_text(encoding='utf-8')
        self.assertIn("'/api/dashboard/users/'", javascript)
        self.assertIn("'PATCH'", javascript)
        self.assertIn("'POST'", javascript)
        self.assertNotIn('password_hash', javascript)
        self.assertNotIn("user.password", javascript)
        self.assertIn("if (!id || password) payload.password = password", javascript)

    def test_frontend_uses_independent_editable_user_groups(self):
        javascript = (ROOT / 'static/dashboard/js/user_management.js').read_text(encoding='utf-8')
        template = (ROOT / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        self.assertIn("method: id ? 'PATCH' : 'POST'", javascript)
        self.assertIn("user_group_ids:", javascript)
        self.assertIn('user-management-group-description', template)
        self.assertIn('user-management-group-active', template)
        self.assertIn('user-management-group-permissions', template)
        self.assertIn('user-management-group-members', template)
        self.assertIn('user_ids:', javascript)
        self.assertIn('permission_codes:', javascript)

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

    def test_frontend_exposes_one_time_password_reset_and_copy_flow(self):
        self.client.force_login(self.admin)
        response = self.client.get('/dashboard/')
        self.assertContains(response, 'id="user-management-reset-modal"')
        self.assertContains(response, '重置并复制')

        javascript = (ROOT / 'static/dashboard/js/user_management.js').read_text(encoding='utf-8')
        self.assertIn("resetPasswordButton.textContent = '重置密码'", javascript)
        self.assertIn('reset_password: true', javascript)
        self.assertIn('data.generated_password', javascript)
        self.assertIn('clearPasswordResetResult()', javascript)
