import json
from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError
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
            "/api/simc-workbench/apls/",
            data=json.dumps({"name": "No CSRF", "spec": "warrior_fury", "content": "actions=/auto_attack"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SimcApl.objects.filter(name="No CSRF", owner_user_id=self.user.id).exists())

    def test_owner_can_create_edit_archive_restore_and_read_apl(self):
        original_content = "\n\nactions=/auto_attack\n"
        create = self.client.post(
            "/api/simc-workbench/apls/",
            data=json.dumps({
                "name": "  Raid APL  ",
                "spec": "warrior_fury",
                "class_name": "mage",
                "content": original_content,
            }),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 200)
        self.assertTrue(create.json()["success"])
        apl_id = create.json()["data"]["id"]
        apl = SimcApl.objects.get(id=apl_id)
        self.assertEqual(apl.name, "Raid APL")
        self.assertEqual(apl.spec, "warrior_fury")
        self.assertEqual(apl.class_name, "warrior")
        self.assertEqual(apl.content, original_content)

        update = self.client.put(
            f"/api/simc-workbench/apls/{apl_id}/",
            data=json.dumps({
                "name": "Raid APL v2",
                "spec": "warrior_arms",
                "class_name": "priest",
                "content": "\nactions=/charge\n\n",
            }),
            content_type="application/json",
        )
        self.assertEqual(update.status_code, 200)
        self.assertTrue(update.json()["success"])

        detail = self.client.get(f"/api/simc-workbench/apls/{apl_id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["name"], "Raid APL v2")
        self.assertEqual(detail.json()["data"]["content"], "\nactions=/charge\n\n")
        self.assertEqual(detail.json()["data"]["spec"], "warrior_arms")
        self.assertEqual(detail.json()["data"]["class_name"], "warrior")

        archive = self.client.post(
            f"/api/simc-workbench/apls/{apl_id}/",
            data=json.dumps({"action": "archive"}),
            content_type="application/json",
        )
        self.assertEqual(archive.status_code, 200)
        self.assertFalse(SimcApl.objects.get(id=apl_id).is_active)

        workbench = self.client.get("/api/simc-workbench/apls/")
        row = next(row for row in workbench.json()["data"] if row["id"] == apl_id)
        self.assertFalse(row["is_active"])

        restore = self.client.post(
            f"/api/simc-workbench/apls/{apl_id}/",
            data=json.dumps({"action": "restore"}),
            content_type="application/json",
        )
        self.assertEqual(restore.status_code, 200)
        self.assertTrue(SimcApl.objects.get(id=apl_id).is_active)

    def test_restore_name_conflict_returns_json_409_and_keeps_archived(self):
        archived = SimcApl.objects.create(
            owner_user_id=self.user.id, name="Raid APL", spec="warrior_fury",
            content="actions=/old", is_active=False,
        )
        SimcApl.objects.create(
            owner_user_id=self.user.id, name="Raid APL", spec="warrior_fury",
            content="actions=/new", is_active=True,
        )

        response = self.client.post(
            f"/api/simc-workbench/apls/{archived.id}/",
            data=json.dumps({"action": "restore"}), content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertFalse(response.json()["success"])
        archived.refresh_from_db()
        self.assertFalse(archived.is_active)

    def test_other_user_cannot_read_edit_or_change_lifecycle(self):
        foreign = SimcApl.objects.create(
            owner_user_id=self.other.id,
            name="Private APL",
            spec="warrior_fury",
            content="actions=/private_action",
        )

        detail = self.client.get(f"/api/simc-workbench/apls/{foreign.id}/")
        self.assertFalse(detail.json()["success"])

        update = self.client.put(
            f"/api/simc-workbench/apls/{foreign.id}/",
            data=json.dumps({"name": "Stolen", "content": "actions=/stolen"}),
            content_type="application/json",
        )
        self.assertEqual(update.status_code, 404)
        self.assertFalse(update.json()["success"])

        lifecycle = self.client.post(
            f"/api/simc-workbench/apls/{foreign.id}/",
            data=json.dumps({"action": "archive"}),
            content_type="application/json",
        )
        self.assertEqual(lifecycle.status_code, 404)
        foreign.refresh_from_db()
        self.assertTrue(foreign.is_active)

        rows = self.client.get("/api/simc-workbench/apls/").json()["data"]
        self.assertNotIn(foreign.id, [row["id"] for row in rows])

    def test_create_and_update_reject_blank_content_and_non_authoritative_spec(self):
        for payload in (
            {"name": "Blank", "spec": "warrior_fury", "content": " \n\t "},
            {"name": "Bad spec", "spec": "demon_hunter_devourer", "content": "actions=/auto_attack"},
        ):
            response = self.client.post(
                "/api/simc-workbench/apls/",
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 400)

        apl = SimcApl.objects.create(
            owner_user_id=self.user.id,
            name="Valid",
            spec="demonhunter_devourer",
            class_name="demonhunter",
            content="actions=/auto_attack",
        )
        response = self.client.put(
            f"/api/simc-workbench/apls/{apl.id}/",
            data=json.dumps({"content": "\n\t"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        apl.refresh_from_db()
        self.assertEqual(apl.content, "actions=/auto_attack")

    def test_create_derives_class_from_authoritative_multiword_spec(self):
        response = self.client.post(
            "/api/simc-workbench/apls/",
            data=json.dumps({
                "name": "Beast Mastery",
                "spec": "hunter_beast_mastery",
                "class_name": "hunter_beast",
                "content": "actions=/auto_shot",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        apl = SimcApl.objects.get(id=response.json()["data"]["id"])
        self.assertEqual(apl.spec, "hunter_beast_mastery")
        self.assertEqual(apl.class_name, "hunter")

    def test_update_without_spec_rejects_invalid_stored_spec(self):
        apl = SimcApl.objects.create(
            owner_user_id=self.user.id,
            name="Legacy invalid",
            spec="hunter_beast",
            class_name="hunter",
            content="actions=/auto_shot",
        )
        response = self.client.put(
            f"/api/simc-workbench/apls/{apl.id}/",
            data=json.dumps({"name": "Still invalid"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        apl.refresh_from_db()
        self.assertEqual(apl.name, "Legacy invalid")

    def test_copy_rejects_template_with_invalid_stored_spec(self):
        template = SimcApl.objects.create(
            name="Invalid system template",
            spec="hunter_beast",
            class_name="hunter",
            content="actions=/auto_shot",
            is_system=True,
            is_active=True,
            is_selectable=True,
        )
        response = self.client.post(
            "/api/simc-workbench/apls/",
            data=json.dumps({"copy_template_id": template.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])
        self.assertFalse(SimcApl.objects.filter(owner_user_id=self.user.id).exists())

    def test_system_apl_is_read_only_for_regular_user(self):
        system_apl = SimcApl.objects.create(
            name="System",
            spec="warrior_fury",
            class_name="warrior",
            content="actions=/charge",
            is_system=True,
        )
        update = self.client.put(
            f"/api/simc-workbench/apls/{system_apl.id}/",
            data=json.dumps({"name": "Changed"}),
            content_type="application/json",
        )
        delete = self.client.delete(f"/api/simc-workbench/apls/{system_apl.id}/")
        archive = self.client.post(
            f"/api/simc-workbench/apls/{system_apl.id}/",
            data=json.dumps({"action": "archive"}),
            content_type="application/json",
        )
        self.assertEqual(update.status_code, 403)
        self.assertEqual(delete.status_code, 403)
        self.assertEqual(archive.status_code, 403)

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
            "/api/simc-workbench/apls/",
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
                "/api/simc-workbench/apls/",
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
            owner_user_id=self.user.id,
            is_system=False,
            is_active=True,
            is_selectable=True,
        )

        response = self.client.post(
            "/api/simc-workbench/apls/",
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
            "/api/simc-workbench/apls/",
            data=json.dumps({"copy_template_id": template.id}),
            content_type="application/json",
        )
        self.assertTrue(response.json()["success"])
        copies = SimcApl.objects.filter(owner_user_id=self.user.id, content="actions=/charge")
        self.assertEqual(copies.count(), 1)
        self.assertRegex(copies.first().name, r"Fury APL 副本 \d+")

    def test_copy_retries_a_unique_create_race_with_next_suffix(self):
        template = SimcApl.objects.create(
            name="Race APL", spec="warrior_fury", content="actions=/charge",
            is_system=True, is_active=True, is_selectable=True,
        )
        original_create = SimcApl.objects.create
        attempts = []

        def racing_create(**kwargs):
            attempts.append(kwargs["name"])
            if len(attempts) == 1:
                raise IntegrityError("UNIQUE constraint failed: botend_simcapl.active_unique_key")
            return original_create(**kwargs)

        with mock.patch.object(SimcApl.objects, "create", side_effect=racing_create):
            response = self.client.post(
                "/api/simc-workbench/apls/",
                data=json.dumps({"copy_template_id": template.id}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, ["Race APL", "Race APL 副本 1"])
        self.assertTrue(SimcApl.objects.filter(
            owner_user_id=self.user.id, name="Race APL 副本 1"
        ).exists())

    def test_copy_does_not_swallow_non_unique_database_errors(self):
        template = SimcApl.objects.create(
            name="Broken APL", spec="warrior_fury", content="actions=/charge",
            is_system=True, is_active=True, is_selectable=True,
        )
        with mock.patch.object(
            SimcApl.objects, "create", side_effect=IntegrityError("database is unavailable")
        ):
            with self.assertRaisesMessage(IntegrityError, "database is unavailable"):
                self.client.post(
                    "/api/simc-workbench/apls/",
                    data=json.dumps({"copy_template_id": template.id}),
                    content_type="application/json",
                )

    def test_apl_list_exposes_can_copy_for_every_system_status(self):
        copyable = SimcApl.objects.create(
            name="Copyable", spec="warrior_fury", content="actions=/one",
            is_system=True, is_active=True, is_selectable=True,
        )
        inactive = SimcApl.objects.create(
            name="Inactive", spec="warrior_arms", content="actions=/two",
            is_system=True, is_active=False, is_selectable=True,
        )
        hidden = SimcApl.objects.create(
            name="Non-selectable", spec="warrior_protection", content="actions=/three",
            is_system=True, is_active=True, is_selectable=False,
        )

        response = self.client.get("/api/simc-workbench/apls/")

        self.assertEqual(response.status_code, 200)
        rows = {row["id"]: row for row in response.json()["data"]}
        self.assertTrue(rows[copyable.id]["can_copy"])
        self.assertFalse(rows[inactive.id]["can_copy"])
        self.assertFalse(rows[hidden.id]["can_copy"])

    def test_copy_rejects_invalid_template_id_without_leaking_content(self):
        """Copy must reject non-existent template IDs without leaking any content."""
        response = self.client.post(
            "/api/simc-workbench/apls/",
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
            "/api/simc-workbench/apls/",
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
