import json

from django.contrib.auth.models import User
from django.middleware.csrf import _get_new_csrf_string
from django.test import Client, TestCase

from botend.models import UserAplStorage, SimcContentTemplate


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

    def test_copy_default_apl_validates_template_type_and_status(self):
        """Copy must only work for active+selectable+default_apl templates."""
        valid_template = SimcContentTemplate.objects.create(
            name="Fury Default APL",
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec="warrior_fury",
            class_name="warrior",
            content="actions=/charge",
            is_active=True,
            is_selectable=True,
        )

        response = self.client.post(
            "/api/apl-storage/",
            data=json.dumps({"copy_template_id": valid_template.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        copy = UserAplStorage.objects.get(user_id=self.user.id, apl_code="actions=/charge")
        self.assertIn("Fury Default APL", copy.title)
        self.assertTrue(copy.is_active)

    def test_copy_rejects_inactive_or_non_selectable_templates(self):
        """Copy must reject templates that are inactive or not selectable."""
        inactive = SimcContentTemplate.objects.create(
            name="Inactive APL",
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            content="actions=/auto_attack",
            is_active=False,
            is_selectable=True,
        )
        non_selectable = SimcContentTemplate.objects.create(
            name="Non-selectable APL",
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            content="actions=/charge",
            is_active=True,
            is_selectable=False,
        )

        for template_id in (inactive.id, non_selectable.id):
            response = self.client.post(
                "/api/apl-storage/",
                data=json.dumps({"copy_template_id": template_id}),
                content_type="application/json",
            )
            self.assertFalse(response.json()["success"])
            self.assertIn("不可复制", response.json()["error"])

    def test_copy_rejects_non_default_apl_template_types(self):
        """Copy must reject template types other than default_apl."""
        base_template = SimcContentTemplate.objects.create(
            name="Base Template",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            content="some config",
            is_active=True,
            is_selectable=True,
        )
        custom_apl = SimcContentTemplate.objects.create(
            name="Custom APL",
            template_type=SimcContentTemplate.TYPE_CUSTOM_APL,
            content="actions=/custom",
            is_active=True,
            is_selectable=True,
        )

        for template_id in (base_template.id, custom_apl.id):
            response = self.client.post(
                "/api/apl-storage/",
                data=json.dumps({"copy_template_id": template_id}),
                content_type="application/json",
            )
            self.assertFalse(response.json()["success"])
            self.assertIn("不可复制", response.json()["error"])

    def test_copy_handles_title_collision_safely(self):
        """Copy must auto-append safe suffix on title collision."""
        template = SimcContentTemplate.objects.create(
            name="Fury APL",
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            content="actions=/charge",
            is_active=True,
            is_selectable=True,
        )
        UserAplStorage.objects.create(
            user_id=self.user.id, title="Fury APL", apl_code="existing"
        )
        UserAplStorage.objects.create(
            user_id=self.user.id, title="Fury APL 副本 1", apl_code="existing2"
        )

        response = self.client.post(
            "/api/apl-storage/",
            data=json.dumps({"copy_template_id": template.id}),
            content_type="application/json",
        )
        self.assertTrue(response.json()["success"])
        copies = UserAplStorage.objects.filter(user_id=self.user.id, apl_code="actions=/charge")
        self.assertEqual(copies.count(), 1)
        self.assertRegex(copies.first().title, r"Fury APL 副本 \d+")

    def test_copy_rejects_invalid_template_id_without_leaking_content(self):
        """Copy must reject non-existent template IDs without leaking any content."""
        response = self.client.post(
            "/api/apl-storage/",
            data=json.dumps({"copy_template_id": 99999}),
            content_type="application/json",
        )
        self.assertFalse(response.json()["success"])
        self.assertIn("不存在", response.json()["error"])
        self.assertNotIn("content", response.json())
        self.assertNotIn("apl_code", response.json())

    def test_copy_rejects_other_users_private_default_apl(self):
        private = SimcContentTemplate.objects.create(
            owner_user_id=self.other.id,
            name="Private Default APL",
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            content="actions=/private_secret",
            is_active=True,
            is_selectable=True,
        )
        response = self.client.post(
            "/api/apl-storage/",
            data=json.dumps({"copy_template_id": private.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])
        self.assertNotIn("private_secret", response.content.decode())
        self.assertFalse(UserAplStorage.objects.filter(user_id=self.user.id).exists())

    def test_default_apl_library_filters_server_side_and_hides_list_content(self):
        visible = SimcContentTemplate.objects.create(
            name="Visible Default",
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            content="actions=/visible",
            is_active=True,
            is_selectable=True,
        )
        SimcContentTemplate.objects.create(
            name="Wrong Type",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            content="secret-base",
            is_active=True,
            is_selectable=True,
        )
        SimcContentTemplate.objects.create(
            name="Inactive Default",
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            content="secret-inactive",
            is_active=False,
            is_selectable=True,
        )
        response = self.client.get("/api/simc-workbench/templates/?library=default_apl")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]
        self.assertEqual([row["id"] for row in rows], [visible.id])
        self.assertNotIn("content", rows[0])

        detail = self.client.get(
            f"/api/simc-workbench/templates/{visible.id}/?library=default_apl"
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["content"], "actions=/visible")
