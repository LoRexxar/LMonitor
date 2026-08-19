import json
from pathlib import Path

from django.apps import apps
from django.contrib.auth.models import User
from django.test import TestCase

from botend.models import GeWechatAuth, MonitorTask, SimcResourceVersion, TargetAuth


ROOT = Path(__file__).resolve().parents[2]


class DashboardDatabaseTableContractTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="dashboard-admin",
            password="test-pass",
            is_staff=True,
            is_superuser=True,
        )
        self.staff_without_database_access = User.objects.create_user(
            username="dashboard-staff",
            password="test-pass",
            is_staff=True,
        )
        self.user = User.objects.create_user(
            username="database-user",
            password="test-password",
        )

    def post_action(self, payload, *, user=None):
        self.client.force_login(user or self.admin)
        return self.client.post(
            "/dashboard/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_staff_sidebar_lists_every_botend_model_with_chinese_and_original_table_name(self):
        self.client.force_login(self.admin)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)

        expected = {
            model.__name__: model._meta.db_table
            for model in apps.get_app_config("botend").get_models()
            if model._meta.managed and not model._meta.proxy
        }
        actual = {row["name"]: row for row in response.context["tables_info"]}
        self.assertEqual(set(actual), set(expected))
        for model_name, db_table in expected.items():
            with self.subTest(model_name=model_name):
                row = actual[model_name]
                self.assertEqual(row["original_name"], db_table)
                self.assertEqual(row["display_name"], f'{row["description"]} - {db_table}')
                self.assertRegex(row["description"], r"[\u4e00-\u9fff]")

    def test_non_staff_does_not_receive_database_sidebar_or_database_api(self):
        self.client.force_login(self.user)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tables_info"], [])

        response = self.post_action(
            {"action": "get_table_data", "table_name": "MonitorTask"},
            user=self.user,
        )
        self.assertEqual(response.status_code, 403)

    def test_staff_can_read_and_search_dedicated_and_generic_tables(self):
        MonitorTask.objects.create(name="仅搜索这个任务", target="https://example.com/a")
        MonitorTask.objects.create(name="另一个任务", target="https://example.com/b")

        response = self.post_action({
            "action": "get_table_data",
            "table_name": "MonitorTask",
            "search": "仅搜索",
            "page": 1,
            "page_size": 50,
        })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual(payload["data"][0]["name"], "仅搜索这个任务")
        self.assertEqual(payload["table_original_name"], MonitorTask._meta.db_table)
        self.assertEqual(payload["table_display_name"], f'{payload["table_description"]} - {MonitorTask._meta.db_table}')
        self.assertIn("name", payload["search_fields"])

        response = self.post_action({
            "action": "get_table_data",
            "table_name": "SimcResourceVersion",
            "search": "不存在也应正常搜索",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")

    def test_schema_marks_safe_edit_fields_and_masks_sensitive_fields(self):
        auth = TargetAuth.objects.create(
            domain="example.com",
            cookie="session=top-secret",
            is_login=True,
        )
        response = self.post_action({
            "action": "get_table_data",
            "table_name": "TargetAuth",
        })
        payload = response.json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["data"][0]["cookie"], "••••••")
        self.assertFalse(payload["field_types"]["cookie"]["editable"])
        self.assertTrue(payload["field_types"]["cookie"]["sensitive"])
        self.assertTrue(payload["field_types"]["domain"]["editable"])

        response = self.post_action({
            "action": "update_table_row",
            "table_name": "TargetAuth",
            "row_id": auth.pk,
            "update_data": {
                "domain": "changed.example.com",
                "cookie": "session=overwritten",
                "id": 99999,
            },
        })
        self.assertEqual(response.status_code, 400)
        auth.refresh_from_db()
        self.assertEqual(auth.domain, "example.com")
        self.assertEqual(auth.cookie, "session=top-secret")

    def test_staff_can_edit_valid_field_with_model_validation(self):
        task = MonitorTask.objects.create(name="旧名称", target="https://example.com")
        response = self.post_action({
            "action": "update_table_row",
            "table_name": "MonitorTask",
            "row_id": task.pk,
            "update_data": {"name": "新名称", "is_active": False},
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "success")
        task.refresh_from_db()
        self.assertEqual(task.name, "新名称")
        self.assertFalse(task.is_active)

    def test_server_side_pagination_is_bounded_and_reports_totals(self):
        for index in range(12):
            MonitorTask.objects.create(name=f"分页任务{index:02d}", target=f"https://example.com/{index}")
        response = self.post_action({
            "action": "get_table_data",
            "table_name": "MonitorTask",
            "page": 2,
            "page_size": 10,
        })
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["total_count"], 12)
        self.assertEqual(payload["page"], 2)
        self.assertEqual(payload["page_size"], 10)
        self.assertEqual(payload["total_pages"], 2)
        self.assertEqual(len(payload["data"]), 2)

    def test_create_update_and_delete_support_nullable_omitted_fields(self):
        response = self.post_action({
            'action': 'create_table_row',
            'table_name': 'MonitorTask',
            'create_data': {
                'name': '通用 CRUD 验收任务',
                'target': 'https://example.invalid/dashboard-crud',
                'is_active': False,
            },
        })
        self.assertEqual(response.status_code, 200, response.content)
        row_id = response.json()['data']['id']
        task = MonitorTask.objects.get(pk=row_id)
        self.assertIsNone(task.flag)
        self.assertFalse(task.is_active)

        response = self.post_action({
            'action': 'update_table_row',
            'table_name': 'MonitorTask',
            'row_id': row_id,
            'update_data': {'name': '通用 CRUD 验收任务（已更新）'},
        })
        self.assertEqual(response.status_code, 200, response.content)
        task.refresh_from_db()
        self.assertEqual(task.name, '通用 CRUD 验收任务（已更新）')

        response = self.post_action({
            'action': 'delete_table_row',
            'table_name': 'MonitorTask',
            'row_id': row_id,
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(MonitorTask.objects.filter(pk=row_id).exists())

    def test_create_rejects_unknown_or_sensitive_fields_without_partial_write(self):
        before = MonitorTask.objects.count()
        response = self.post_action({
            "action": "create_table_row",
            "table_name": "MonitorTask",
            "create_data": {
                "name": "不应创建",
                "target": "https://example.com/new",
                "unknown_field": "value",
            },
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(MonitorTask.objects.count(), before)

        response = self.post_action({
            "action": "create_table_row",
            "table_name": "TargetAuth",
            "create_data": {
                "domain": "secret.example.com",
                "cookie": "session=secret",
                "is_login": True,
            },
        })
        self.assertIn(response.status_code, {400, 403})
        self.assertFalse(TargetAuth.objects.filter(domain="secret.example.com").exists())

    def test_dedicated_models_are_read_only_in_generic_database_api(self):
        response = self.post_action({
            "action": "get_table_data",
            "table_name": "SimcResourceVersion",
        })
        self.assertEqual(response.status_code, 200)
        capabilities = response.json()["capabilities"]
        self.assertFalse(capabilities["can_create"])
        self.assertFalse(capabilities["can_update"])
        self.assertFalse(capabilities["can_delete"])

        response = self.post_action({
            "action": "update_table_row",
            "table_name": "SimcResourceVersion",
            "row_id": 1,
            "update_data": {"name": "绕过专用接口"},
        })
        self.assertEqual(response.status_code, 403)

    def test_staff_without_database_admin_rights_cannot_read_registry_or_api(self):
        self.client.force_login(self.staff_without_database_access)
        page = self.client.get('/dashboard/')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['tables_info'], [])
        response = self.post_action(
            {'action': 'get_table_data', 'table_name': 'MonitorTask'},
            user=self.staff_without_database_access,
        )
        self.assertEqual(response.status_code, 403)

    def test_wechat_login_flow_values_are_masked_and_read_only(self):
        auth = GeWechatAuth.objects.create(
            appId='app-id',
            uuid='login-flow-uuid',
            qrImgBase64='data:image/png;base64,secret',
        )
        response = self.post_action({
            'action': 'get_table_data',
            'table_name': 'GeWechatAuth',
        })
        payload = response.json()
        row = next(item for item in payload['data'] if item['id'] == auth.pk)
        self.assertEqual(row['uuid'], '••••••')
        self.assertEqual(row['qrImgBase64'], '••••••')
        self.assertFalse(payload['capabilities']['can_update'])
        self.assertTrue(payload['field_types']['uuid']['sensitive'])
        self.assertTrue(payload['field_types']['qrImgBase64']['sensitive'])

    def test_page_beyond_last_page_is_clamped_to_last_page(self):
        for index in range(12):
            MonitorTask.objects.create(name=f'越界任务{index}', target=f'https://example.com/{index}')
        response = self.post_action({
            'action': 'get_table_data',
            'table_name': 'MonitorTask',
            'page': 999,
            'page_size': 10,
        })
        payload = response.json()
        self.assertEqual(payload['page'], 2)
        self.assertEqual(payload['total_pages'], 2)
        self.assertEqual(len(payload['data']), 2)


class DashboardDatabaseFrontendContractTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.javascript = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')

    def test_database_links_only_use_validated_http_urls(self):
        self.assertIn("['http:', 'https:'].includes(parsed.protocol)", self.javascript)
        self.assertIn('const safeUrl = getSafeHttpUrl(cellText);', self.javascript)
        self.assertIn("link.rel = 'noopener noreferrer';", self.javascript)
        self.assertNotIn('link.href = cellText;', self.javascript)

    def test_row_actions_follow_each_server_capability_independently(self):
        self.assertIn('if (currentTableCapabilities.can_update)', self.javascript)
        self.assertIn('if (currentTableCapabilities.can_delete)', self.javascript)
        self.assertNotIn('disableAdd:', self.javascript)

    def test_monitor_task_has_a_dedicated_sidebar_entry_that_opens_its_editable_table(self):
        template = (ROOT / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        self.assertIn('data-dashboard-table="MonitorTask"', template)
        self.assertIn('监控任务', template)
        self.assertIn("const dashboardTable = this.getAttribute('data-dashboard-table');", self.javascript)
        self.assertIn('openDashboardTable(dashboardTable, this.querySelector(\'a\')?.textContent);', self.javascript)
