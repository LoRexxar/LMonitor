import json

from django.contrib.auth.models import User
from django.middleware.csrf import _get_new_csrf_string
from django.test import Client, TestCase

from botend.models import UserAplStorage


class SimcAplWorkbenchApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="apl-owner", password="test-password")
        self.other = User.objects.create_user(username="apl-other", password="test-password")
        self.client.force_login(self.user)

    def test_apl_write_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(
            "/api/apl-storage/",
            data=json.dumps({"title": "No CSRF", "apl_code": "actions=/auto_attack"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(UserAplStorage.objects.filter(title="No CSRF").exists())

    def test_owner_can_create_edit_archive_restore_and_read_apl(self):
        create = self.client.post(
            "/api/apl-storage/",
            data=json.dumps({"title": "Raid APL", "apl_code": "actions=/auto_attack"}),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 200)
        self.assertTrue(create.json()["success"])
        apl_id = create.json()["data"]["id"]

        update = self.client.put(
            "/api/apl-storage/",
            data=json.dumps({"id": apl_id, "title": "Raid APL v2", "apl_code": "actions=/charge"}),
            content_type="application/json",
        )
        self.assertEqual(update.status_code, 200)
        self.assertTrue(update.json()["success"])

        detail = self.client.get(f"/api/apl-storage/{apl_id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["apl_code"], "actions=/charge")

        archive = self.client.post(
            f"/api/simc-workbench/apl-storage/{apl_id}/",
            data=json.dumps({"action": "archive"}),
            content_type="application/json",
        )
        self.assertEqual(archive.status_code, 200)
        self.assertFalse(UserAplStorage.objects.get(id=apl_id).is_active)

        workbench = self.client.get("/api/simc-workbench/apl-storage/")
        row = next(row for row in workbench.json()["data"] if row["id"] == apl_id)
        self.assertFalse(row["is_active"])

        restore = self.client.post(
            f"/api/simc-workbench/apl-storage/{apl_id}/",
            data=json.dumps({"action": "restore"}),
            content_type="application/json",
        )
        self.assertEqual(restore.status_code, 200)
        self.assertTrue(UserAplStorage.objects.get(id=apl_id).is_active)

    def test_other_user_cannot_read_edit_or_change_lifecycle(self):
        foreign = UserAplStorage.objects.create(
            user_id=self.other.id,
            title="Private APL",
            apl_code="actions=/private_action",
        )

        detail = self.client.get(f"/api/apl-storage/{foreign.id}/")
        self.assertFalse(detail.json()["success"])

        update = self.client.put(
            "/api/apl-storage/",
            data=json.dumps({"id": foreign.id, "title": "Stolen", "apl_code": "actions=/stolen"}),
            content_type="application/json",
        )
        self.assertFalse(update.json()["success"])

        lifecycle = self.client.post(
            f"/api/simc-workbench/apl-storage/{foreign.id}/",
            data=json.dumps({"action": "archive"}),
            content_type="application/json",
        )
        self.assertEqual(lifecycle.status_code, 404)
        foreign.refresh_from_db()
        self.assertTrue(foreign.is_active)

        rows = self.client.get("/api/simc-workbench/apl-storage/").json()["data"]
        self.assertNotIn(foreign.id, [row["id"] for row in rows])
