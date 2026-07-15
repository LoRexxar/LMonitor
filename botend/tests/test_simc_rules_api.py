import json

from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.models import SimcSecondaryStatRule, SimcMasteryCoefficient


class SimcRulesApiTests(TestCase):
    def setUp(self):
        self.regular_user = User.objects.create_user(username="regular", password="test-password")
        self.staff_user = User.objects.create_user(username="staff", password="test-password", is_staff=True)
        SimcSecondaryStatRule.objects.all().delete()
        SimcMasteryCoefficient.objects.all().delete()

    def test_secondary_rules_write_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff_user)
        response = client.post(
            "/api/simc-workbench/secondary-rules/",
            data=json.dumps({"class_name": "warrior", "crit_per_percent": 46}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SimcSecondaryStatRule.objects.filter(class_name="warrior").exists())

    def test_mastery_rules_write_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff_user)
        response = client.post(
            "/api/simc-workbench/mastery-rules/",
            data=json.dumps({"spec": "fury", "mastery_coefficient": 1.4}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SimcMasteryCoefficient.objects.filter(spec="fury").exists())

    def test_regular_user_can_read_secondary_rules(self):
        SimcSecondaryStatRule.objects.create(
            class_name="warrior",
            crit_per_percent=46,
            haste_per_percent=44,
            mastery_per_percent=46,
            versatility_per_percent=54,
        )
        self.client.force_login(self.regular_user)
        response = self.client.get("/api/simc-workbench/secondary-rules/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(len(response.json()["data"]), 1)

    def test_regular_user_cannot_create_secondary_rules(self):
        self.client.force_login(self.regular_user)
        response = self.client.post(
            "/api/simc-workbench/secondary-rules/",
            data=json.dumps({"class_name": "mage", "crit_per_percent": 46}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SimcSecondaryStatRule.objects.filter(class_name="mage").exists())

    def test_regular_user_cannot_edit_secondary_rules(self):
        rule = SimcSecondaryStatRule.objects.create(class_name="warrior", crit_per_percent=46)
        self.client.force_login(self.regular_user)
        response = self.client.put(
            f"/api/simc-workbench/secondary-rules/{rule.id}/",
            data=json.dumps({"crit_per_percent": 99}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        rule.refresh_from_db()
        self.assertEqual(rule.crit_per_percent, 46)

    def test_regular_user_cannot_delete_secondary_rules(self):
        rule = SimcSecondaryStatRule.objects.create(class_name="warrior", crit_per_percent=46)
        self.client.force_login(self.regular_user)
        response = self.client.delete(f"/api/simc-workbench/secondary-rules/{rule.id}/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(SimcSecondaryStatRule.objects.filter(id=rule.id).exists())

    def test_staff_can_create_secondary_rules(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            "/api/simc-workbench/secondary-rules/",
            data=json.dumps({
                "class_name": "mage",
                "crit_per_percent": 48,
                "haste_per_percent": 45,
                "mastery_per_percent": 47,
                "versatility_per_percent": 55,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        rule = SimcSecondaryStatRule.objects.get(class_name="mage")
        self.assertEqual(rule.crit_per_percent, 48)

    def test_staff_can_edit_secondary_rules(self):
        rule = SimcSecondaryStatRule.objects.create(class_name="warrior", crit_per_percent=46)
        self.client.force_login(self.staff_user)
        response = self.client.put(
            f"/api/simc-workbench/secondary-rules/{rule.id}/",
            data=json.dumps({"crit_per_percent": 50}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        rule.refresh_from_db()
        self.assertEqual(rule.crit_per_percent, 50)

    def test_staff_can_delete_secondary_rules(self):
        rule = SimcSecondaryStatRule.objects.create(class_name="warrior", crit_per_percent=46)
        self.client.force_login(self.staff_user)
        response = self.client.delete(f"/api/simc-workbench/secondary-rules/{rule.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertFalse(SimcSecondaryStatRule.objects.filter(id=rule.id).exists())

    def test_regular_user_can_read_mastery_rules(self):
        SimcMasteryCoefficient.objects.create(spec="fury", mastery_coefficient=1.4)
        self.client.force_login(self.regular_user)
        response = self.client.get("/api/simc-workbench/mastery-rules/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(len(response.json()["data"]), 1)

    def test_regular_user_cannot_create_mastery_rules(self):
        self.client.force_login(self.regular_user)
        response = self.client.post(
            "/api/simc-workbench/mastery-rules/",
            data=json.dumps({"spec": "arms", "mastery_coefficient": 1.5}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SimcMasteryCoefficient.objects.filter(spec="arms").exists())

    def test_regular_user_cannot_edit_mastery_rules(self):
        rule = SimcMasteryCoefficient.objects.create(spec="fury", mastery_coefficient=1.4)
        self.client.force_login(self.regular_user)
        response = self.client.put(
            f"/api/simc-workbench/mastery-rules/{rule.id}/",
            data=json.dumps({"mastery_coefficient": 9.9}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        rule.refresh_from_db()
        self.assertEqual(rule.mastery_coefficient, 1.4)

    def test_regular_user_cannot_delete_mastery_rules(self):
        rule = SimcMasteryCoefficient.objects.create(spec="fury", mastery_coefficient=1.4)
        self.client.force_login(self.regular_user)
        response = self.client.delete(f"/api/simc-workbench/mastery-rules/{rule.id}/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(SimcMasteryCoefficient.objects.filter(id=rule.id).exists())

    def test_staff_can_create_mastery_rules(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            "/api/simc-workbench/mastery-rules/",
            data=json.dumps({"spec": "arms", "mastery_coefficient": 1.5}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        rule = SimcMasteryCoefficient.objects.get(spec="arms")
        self.assertEqual(rule.mastery_coefficient, 1.5)

    def test_staff_can_edit_mastery_rules(self):
        rule = SimcMasteryCoefficient.objects.create(spec="fury", mastery_coefficient=1.4)
        self.client.force_login(self.staff_user)
        response = self.client.put(
            f"/api/simc-workbench/mastery-rules/{rule.id}/",
            data=json.dumps({"mastery_coefficient": 1.6}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        rule.refresh_from_db()
        self.assertEqual(rule.mastery_coefficient, 1.6)

    def test_staff_can_delete_mastery_rules(self):
        rule = SimcMasteryCoefficient.objects.create(spec="fury", mastery_coefficient=1.4)
        self.client.force_login(self.staff_user)
        response = self.client.delete(f"/api/simc-workbench/mastery-rules/{rule.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertFalse(SimcMasteryCoefficient.objects.filter(id=rule.id).exists())

    def test_get_single_secondary_rule_detail(self):
        rule = SimcSecondaryStatRule.objects.create(
            class_name="warrior",
            crit_per_percent=46,
            haste_per_percent=44,
            mastery_per_percent=46,
            versatility_per_percent=54,
        )
        self.client.force_login(self.regular_user)
        response = self.client.get(f"/api/simc-workbench/secondary-rules/{rule.id}/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["class_name"], "warrior")
        self.assertEqual(data["crit_per_percent"], 46)

    def test_get_single_mastery_rule_detail(self):
        rule = SimcMasteryCoefficient.objects.create(spec="fury", mastery_coefficient=1.4)
        self.client.force_login(self.regular_user)
        response = self.client.get(f"/api/simc-workbench/mastery-rules/{rule.id}/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["spec"], "fury")
        self.assertEqual(data["mastery_coefficient"], 1.4)

    def test_secondary_rules_reject_non_numeric_crit(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            "/api/simc-workbench/secondary-rules/",
            data=json.dumps({"class_name": "warrior", "crit_per_percent": "not_a_number"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_secondary_rules_reject_duplicate_class_name(self):
        SimcSecondaryStatRule.objects.create(class_name="warrior", crit_per_percent=46)
        self.client.force_login(self.staff_user)
        response = self.client.post(
            "/api/simc-workbench/secondary-rules/",
            data=json.dumps({"class_name": "warrior", "crit_per_percent": 48}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("error", response.json())

    def test_mastery_rules_reject_non_numeric_coefficient(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            "/api/simc-workbench/mastery-rules/",
            data=json.dumps({"spec": "fury", "mastery_coefficient": "invalid"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_mastery_rules_reject_duplicate_spec(self):
        SimcMasteryCoefficient.objects.create(spec="fury", mastery_coefficient=1.4)
        self.client.force_login(self.staff_user)
        response = self.client.post(
            "/api/simc-workbench/mastery-rules/",
            data=json.dumps({"spec": "fury", "mastery_coefficient": 1.6}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("error", response.json())
