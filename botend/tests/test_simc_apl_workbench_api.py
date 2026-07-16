import json

from django.contrib.auth.models import User
from django.middleware.csrf import _get_new_csrf_string
from django.test import Client, TestCase

from botend.models import SimcApl, SimcContentTemplate


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
        self.assertFalse(SimcApl.objects.filter(name="No CSRF", owner_user_id=self.user.id).exists())

    def test_owner_can_create_edit_archive_restore_and_read_apl(self):
        create = self.client.post(
            "/api/apl-storage/",
            data=json.dumps({"title": "Raid APL", "spec": "warrior_fury", "apl_code": "actions=/auto_attack"}),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 200)
        self.assertTrue(create.json()["success"])
        apl_id = create.json()["data"]["id"]
        self.assertEqual(SimcApl.objects.get(id=apl_id).spec, "warrior_fury")

        update = self.client.put(
            "/api/apl-storage/",
            data=json.dumps({"id": apl_id, "title": "Raid APL v2", "spec": "warrior_arms", "apl_code": "actions=/charge"}),
            content_type="application/json",
        )
        self.assertEqual(update.status_code, 200)
        self.assertTrue(update.json()["success"])

        detail = self.client.get(f"/api/apl-storage/{apl_id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["apl_code"], "actions=/charge")
        self.assertEqual(detail.json()["data"]["spec"], "warrior_arms")

        archive = self.client.post(
            f"/api/simc-workbench/apl-storage/{apl_id}/",
            data=json.dumps({"action": "archive"}),
            content_type="application/json",
        )
        self.assertEqual(archive.status_code, 200)
        self.assertFalse(SimcApl.objects.get(id=apl_id).is_active)

        workbench = self.client.get("/api/simc-workbench/apl-storage/")
        row = next(row for row in workbench.json()["data"] if row["id"] == apl_id)
        self.assertFalse(row["is_active"])

        restore = self.client.post(
            f"/api/simc-workbench/apl-storage/{apl_id}/",
            data=json.dumps({"action": "restore"}),
            content_type="application/json",
        )
        self.assertEqual(restore.status_code, 200)
        self.assertTrue(SimcApl.objects.get(id=apl_id).is_active)

    def test_other_user_cannot_read_edit_or_change_lifecycle(self):
        foreign = SimcApl.objects.create(
            owner_user_id=self.other.id,
            name="Private APL",
            spec="warrior_fury",
            content="actions=/private_action",
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
        """Copy must only work for active+selectable system APL."""
        valid_template = SimcApl.objects.create(
            name="Fury Default APL",
            spec="warrior_fury",
            class_name="warrior",
            content="actions=/charge",
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
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
        copy = SimcApl.objects.get(owner_user_id=self.user.id, content="actions=/charge")
        self.assertIn("Fury Default APL", copy.name)
        self.assertEqual(copy.spec, "warrior_fury")
        self.assertTrue(copy.is_active)

    def test_copy_rejects_inactive_or_non_selectable_templates(self):
        """Copy must reject templates that are inactive or not selectable."""
        inactive = SimcApl.objects.create(
            name="Inactive APL",
            spec="warrior_fury",
            content="actions=/auto_attack",
            is_system=True,
            is_active=False,
            is_selectable=True,
        )
        non_selectable = SimcApl.objects.create(
            name="Non-selectable APL",
            spec="warrior_fury",
            content="actions=/charge",
            is_system=True,
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

    def test_copy_rejects_non_system_apl(self):
        """Copy must reject non-system APL (only system APL can be copied)."""
        user_apl = SimcApl.objects.create(
            name="User APL",
            spec="warrior_fury",
            content="actions=/user_custom",
            owner_user_id=self.other.id,
            is_system=False,
            is_active=True,
            is_selectable=True,
        )

        response = self.client.post(
            "/api/apl-storage/",
            data=json.dumps({"copy_template_id": user_apl.id}),
            content_type="application/json",
        )
        self.assertFalse(response.json()["success"])
        self.assertIn("不可复制", response.json()["error"])

    def test_copy_handles_title_collision_safely(self):
        """Copy must auto-append safe suffix on title collision."""
        template = SimcApl.objects.create(
            name="Fury APL",
            spec="warrior_fury",
            content="actions=/charge",
            is_system=True,
            is_active=True,
            is_selectable=True,
        )
        SimcApl.objects.create(
            owner_user_id=self.user.id, name="Fury APL", spec="warrior_fury", content="existing"
        )
        SimcApl.objects.create(
            owner_user_id=self.user.id, name="Fury APL 副本 1", spec="warrior_fury", content="existing2"
        )

        response = self.client.post(
            "/api/apl-storage/",
            data=json.dumps({"copy_template_id": template.id}),
            content_type="application/json",
        )
        self.assertTrue(response.json()["success"])
        copies = SimcApl.objects.filter(owner_user_id=self.user.id, content="actions=/charge")
        self.assertEqual(copies.count(), 1)
        self.assertRegex(copies.first().name, r"Fury APL 副本 \d+")

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
        private = SimcApl.objects.create(
            owner_user_id=self.other.id,
            name="Private Default APL",
            spec="warrior_fury",
            content="actions=/private_secret",
            is_system=True,
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
        self.assertFalse(SimcApl.objects.filter(owner_user_id=self.user.id).exists())

    def test_default_apl_library_filters_server_side_and_hides_list_content(self):
        visible = SimcApl.objects.create(
            name="Visible Default",
            spec="warrior_fury",
            content="actions=/visible",
            is_system=True,
            is_active=True,
            is_selectable=True,
        )
        SimcApl.objects.create(
            name="Inactive Default",
            spec="warrior_fury",
            content="secret-inactive",
            is_system=True,
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
