import json

from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.models import SimcContentTemplate, SimcSecondaryStatRule


class SimcWorkbenchSecurityContractTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="simc-user", password="test-password")
        self.other = User.objects.create_user(username="simc-other", password="test-password")

    def test_simc_cookie_authenticated_write_endpoints_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        requests = (
            ("post", "/api/keyword-manager/", {"apl_keyword": "actions=/x", "cn_keyword": "x"}),
            ("post", "/api/simc-task/", {"name": "csrf-task"}),
            ("post", "/api/simc-task/batch/", {"name": "csrf-batch"}),
            ("post", "/api/simc-profile/", {"name": "csrf-profile"}),
            ("post", "/api/simc-profile/inspect-raw/", {"raw_simc_code": "warrior=csrf"}),
            ("post", "/api/simc-player-config-detail/", {"player_config_mode": "manual_equipment"}),
            ("post", "/api/simc-battlenet-preflight/", {"region": "us", "realm": "x", "character": "y"}),
            ("post", "/api/simc-apl-candidates/", {"spec": "fury"}),
            ("post", "/api/simc-template/", {"name": "csrf-template"}),
        )
        for method, path, payload in requests:
            with self.subTest(path=path):
                response = getattr(client, method)(
                    path,
                    data=json.dumps(payload),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 403)

    def test_old_template_api_never_mutates_upstream_rows_even_for_staff(self):
        staff = User.objects.create_user(username="simc-staff", password="test-password", is_staff=True)
        upstream = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec="warrior_fury", name="Upstream", content="iterations=10000",
            is_active=True,
        )
        self.client.force_login(staff)
        requests = (
            ("put", {"name": "Changed", "content": "iterations=5000"}),
            ("patch", {"is_active": False}),
            ("delete", {}),
        )
        for method, payload in requests:
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    f"/api/simc-template/?id={upstream.id}",
                    data=json.dumps(payload), content_type="application/json",
                )
                self.assertEqual(response.status_code, 403)
                upstream.refresh_from_db()
                self.assertEqual(upstream.name, "Upstream")
                self.assertEqual(upstream.content, "iterations=10000")
                self.assertTrue(upstream.is_active)

    def test_generic_dashboard_cannot_mutate_simc_resources(self):
        self.client.force_login(self.user)
        rule, _ = SimcSecondaryStatRule.objects.update_or_create(
            class_name="warrior",
            defaults={
                "crit_per_percent": 46,
                "haste_per_percent": 44,
                "mastery_per_percent": 0,
                "versatility_per_percent": 54,
            },
        )
        for action in ("create_table_row", "update_table_row", "delete_table_row"):
            with self.subTest(action=action):
                response = self.client.post(
                    "/dashboard/",
                    data=json.dumps({
                        "action": action,
                        "table_name": "SimcSecondaryStatRule",
                        "id": rule.id,
                        "data": {"class_name": "mage", "crit_per_percent": 1},
                    }),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 403)
        rule.refresh_from_db()
        self.assertEqual(rule.class_name, "warrior")
        self.assertEqual(rule.crit_per_percent, 46)

    def test_generic_dashboard_cannot_read_any_simc_resource(self):
        self.client.force_login(self.user)
        simc_models = (
            "SimcTask", "SimcTaskBatch", "SimcTaskArtifact", "SimcProfile",
            "SimcContentTemplate", "SimcSecondaryStatRule", "SimcMasteryCoefficient",
            "SimcAplKeywordPair", "SimcApl", "SimcBackendBinary",
        )
        for model_name in simc_models:
            with self.subTest(model_name=model_name):
                response = self.client.post(
                    "/dashboard/",
                    data=json.dumps({
                        "action": "get_table_data", "table_name": model_name,
                        "page": 1, "page_size": 50,
                    }),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 403)

    def test_dashboard_hides_simc_models_and_legacy_apl_tool_entry(self):
        self.client.force_login(self.user)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        visible_tables = {row["name"] for row in response.context["tables_info"]}
        self.assertTrue(visible_tables.isdisjoint({
            "SimcTask", "SimcTaskBatch", "SimcTaskArtifact", "SimcProfile",
            "SimcContentTemplate", "SimcSecondaryStatRule", "SimcMasteryCoefficient",
            "SimcAplKeywordPair", "SimcBackendBinary",
        }))
        self.assertNotContains(response, 'data-table="SimcAplKeywordPair"')
