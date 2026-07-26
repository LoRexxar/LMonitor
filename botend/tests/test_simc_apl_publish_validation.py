import hashlib
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings

from botend.models import (SimcApl, SimcAplSymbol, SimcBackendBinary,
                           SimcContentTemplate, SimcProfile,
                           SimcResourceVersion, SimcTask)
from botend.services.simc_apl.publish import (current_validation_identity,
                                              validate_apl_for_profile)
from botend.services.simc_task_service import TaskCreationError, create_task


CONTENT = "actions=/auto_attack\nactions+=/bloodthirst"
REVISION = "a" * 40
BUILD = "12.0.1.70000"


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@override_settings(SIMC_APL_CURRENT_IDENTITY=None)
class SimcAplCurrentIdentityTests(TestCase):
    def test_resolves_unique_full_revision_from_binary_version_suffix(self):
        full_revision = "62ababb127bef2a35f96357968d455dde7de7616"
        backend, _ = SimcBackendBinary.objects.update_or_create(
            identifier='production',
            defaults={
                'name': '正式服', 'platform': 'linux64',
                'current_version': '1205-01-62ababb', 'is_active': True,
            },
        )
        SimcAplSymbol.objects.create(
            simc_revision=full_revision, wow_build="12.0.7.68453",
            token="bloodthirst", symbol_kind="action", is_active=True,
        )

        self.assertEqual(
            current_validation_identity(),
            (full_revision, "12.0.7.68453"),
        )

    @patch("botend.services.simc_apl.publish.validate_payload")
    @patch("botend.services.simc_apl.publish.SimcComposer.compose_validation_input",
           return_value='warrior="Player"\nspec=fury\nactions=/auto_attack')
    def test_authoritative_validation_uses_resolved_full_binary_revision(
            self, compose_validation_input, validate):
        full_revision = "62ababb127bef2a35f96357968d455dde7de7616"
        backend, _ = SimcBackendBinary.objects.update_or_create(
            identifier='production',
            defaults={
                'name': '正式服', 'platform': 'linux64',
                'current_version': '1205-01-62ababb',
                'simc_path': '/tmp/simc', 'is_active': True,
            },
        )
        SimcAplSymbol.objects.create(
            simc_revision=full_revision, wow_build="12.0.7.68453",
            token="bloodthirst", symbol_kind="action", is_active=True,
        )
        profile = SimcProfile.objects.create(
            user_id=1, name="Player", spec="warrior_fury",
            player_config_mode="manual_equipment",
            player_equipment='warrior="Player"\nspec=fury\nmain_hand=,id=1',
            is_active=True,
        )
        apl = SimcApl(name="APL", spec="warrior_fury", content=CONTENT)

        def validation_result(content, **kwargs):
            self.assertEqual(
                kwargs["authoritative_validator"].binary_revision,
                full_revision,
            )
            self.assertEqual(
                kwargs["validation_context"]["binary_revision"],
                full_revision,
            )
            return {
                "structural_valid": True,
                "authoritative_valid": True,
                "diagnostics": [],
            }

        validate.side_effect = validation_result

        self.assertTrue(validate_apl_for_profile(profile, apl)["valid"])


@override_settings(SIMC_APL_CURRENT_IDENTITY=(REVISION, BUILD))
class SimcAplPublishValidationTests(TestCase):
    def setUp(self):
        self.backend, _ = SimcBackendBinary.objects.update_or_create(
            identifier='production',
            defaults={
                'name': '正式服', 'platform': 'linux64',
                'current_version': REVISION, 'simc_path': '/tmp/simc',
                'is_active': True,
            },
        )
        SimcAplSymbol.objects.get_or_create(
            simc_revision=REVISION, wow_build=BUILD,
            token='auto_attack', symbol_kind='action',
            defaults={'is_active': True},
        )
        self.user = User.objects.create_user(username="publisher", password="pwd")
        self.client.force_login(self.user)
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id, name="Player", spec="warrior_fury",
            player_config_mode="manual_equipment",
            player_equipment='warrior="Player"\nspec=fury\nmain_hand=,id=1', is_active=True,
        )
        self.template = SimcContentTemplate.objects.create(
            name="Base", spec="warrior_fury",
            content="{simulation_options}\n{player_config}\n{action_list}\n{output_options}",
            owner_user_id=self.user.id, is_active=True, is_selectable=True,
        )

    def create_draft(self, content=CONTENT):
        response = self.client.post(
            "/api/simc-workbench/apls/",
            data=json.dumps({"name": "Draft", "spec": "warrior_fury", "content": content,
                             "is_selectable": True, "validation_status": "valid"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        return SimcApl.objects.get(pk=response.json()["data"]["id"])

    def test_model_default_draft_is_not_selectable(self):
        apl = SimcApl.objects.create(
            name="Default draft", spec="warrior_fury", content=CONTENT,
            owner_user_id=self.user.id,
        )
        self.assertEqual(apl.validation_status, SimcApl.VALIDATION_DRAFT)
        self.assertFalse(apl.is_selectable)

    def test_invalid_draft_is_saved_but_never_selectable_and_client_cannot_forge_valid(self):
        apl = self.create_draft("actions+=/bloodthirst\nactions=/auto_attack")
        self.assertFalse(apl.is_selectable)
        self.assertEqual(apl.validation_status, SimcApl.VALIDATION_DRAFT)
        self.assertEqual(apl.validated_content_hash, "")

    def test_changing_one_character_makes_old_validation_stale(self):
        apl = SimcApl.objects.create(
            name="Published", spec="warrior_fury", content=CONTENT,
            owner_user_id=self.user.id, is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=digest(CONTENT), validation_revision=REVISION,
            validation_game_build=BUILD,
        )
        apl.content += " "
        apl.save(update_fields=["content"])
        apl.refresh_from_db()
        self.assertFalse(apl.is_selectable)
        self.assertEqual(apl.validation_status, SimcApl.VALIDATION_STALE)
        self.assertEqual(apl.validation_stale_reason, "content_changed")

    def test_changing_spec_makes_old_validation_stale(self):
        apl = SimcApl.objects.create(
            name="Published", spec="warrior_fury", content=CONTENT,
            owner_user_id=self.user.id, is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=digest(CONTENT), validation_revision=REVISION,
            validation_game_build=BUILD,
        )
        apl.spec = "warrior_arms"
        apl.save(update_fields=["spec"])
        apl.refresh_from_db()
        self.assertFalse(apl.is_selectable)
        self.assertEqual(apl.validation_status, SimcApl.VALIDATION_STALE)
        self.assertEqual(apl.validation_stale_reason, "spec_changed")

    def test_revision_change_makes_validation_stale(self):
        apl = SimcApl(
            name="Published", spec="warrior_fury", content=CONTENT,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=digest(CONTENT), validation_revision=REVISION,
            validation_game_build=BUILD, is_selectable=True,
        )
        self.assertTrue(apl.has_current_validation((REVISION, BUILD)))
        self.assertFalse(apl.has_current_validation(("b" * 40, BUILD)))
        self.assertEqual(apl.validation_staleness(("b" * 40, BUILD)), "revision_changed")

    @patch("botend.services.simc_apl.publish.validate_apl_for_profile")
    def test_publish_only_accepts_validation_of_exact_same_content_hash(self, validate):
        apl = self.create_draft()
        validate.return_value = {
            "valid": True, "content_hash": digest(CONTENT + "changed"),
            "revision": REVISION, "game_build": BUILD, "diagnostics": [],
        }
        response = self.client.post(
            f"/api/simc-workbench/apls/{apl.id}/",
            data=json.dumps({"action": "publish", "profile_id": self.profile.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        apl.refresh_from_db()
        self.assertFalse(apl.is_selectable)

    @patch("botend.services.simc_apl.publish.validate_apl_for_profile")
    def test_publish_marks_exact_valid_hash_selectable(self, validate):
        apl = self.create_draft()
        validate.return_value = {
            "valid": True, "content_hash": digest(CONTENT), "revision": REVISION,
            "game_build": BUILD, "diagnostics": [],
        }
        response = self.client.post(
            f"/api/simc-workbench/apls/{apl.id}/",
            data=json.dumps({"action": "publish", "profile_id": self.profile.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        apl.refresh_from_db()
        self.assertTrue(apl.is_selectable)
        self.assertTrue(apl.has_current_validation((REVISION, BUILD)))

    @patch("botend.services.simc_apl.publish.validate_apl_for_profile")
    def test_publish_rejects_profile_with_different_canonical_spec(self, validate):
        apl = self.create_draft()
        self.profile.spec = "warrior_arms"
        self.profile.save(update_fields=["spec"])
        response = self.client.post(
            f"/api/simc-workbench/apls/{apl.id}/",
            data=json.dumps({"action": "publish", "profile_id": self.profile.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        validate.assert_not_called()

    @patch("botend.services.simc_apl.publish.validate_apl_for_profile")
    def test_publish_rechecks_locked_content_after_validation(self, validate):
        apl = self.create_draft()

        def mutate(_profile, locked_apl):
            SimcApl.objects.filter(pk=locked_apl.pk).update(content=CONTENT + " changed")
            return {"valid": True, "content_hash": digest(CONTENT), "revision": REVISION,
                    "game_build": BUILD, "diagnostics": []}

        validate.side_effect = mutate
        response = self.client.post(
            f"/api/simc-workbench/apls/{apl.id}/",
            data=json.dumps({"action": "publish", "profile_id": self.profile.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)

    @patch("botend.services.simc_task_service.validate_apl_for_profile")
    def test_task_creation_revalidates_with_authoritative_persisted_profile(self, validate):
        apl = SimcApl.objects.create(
            name="Published", spec="warrior_fury", content=CONTENT,
            owner_user_id=self.user.id, is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=digest(CONTENT), validation_revision=REVISION,
            validation_game_build=BUILD,
        )
        validate.return_value = {
            "valid": False, "content_hash": digest(CONTENT), "revision": REVISION,
            "game_build": BUILD, "diagnostics": [{"message": "profile-specific failure"}],
        }
        with self.assertRaises(TaskCreationError):
            create_task(self.user.id, "Rejected", self.profile.id, self.template.id, apl.id)
        validate.assert_called_once()
        self.assertEqual(validate.call_args.args[0].id, self.profile.id)
        self.assertEqual(validate.call_args.args[1].id, apl.id)

    @patch("botend.services.simc_task_service.validate_apl_for_profile")
    def test_existing_task_keeps_immutable_apl_version_after_later_draft_save(self, validate):
        apl = SimcApl.objects.create(
            name="Published", spec="warrior_fury", content=CONTENT,
            owner_user_id=self.user.id, is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=digest(CONTENT), validation_revision=REVISION,
            validation_game_build=BUILD,
        )
        validate.return_value = {
            "valid": True, "content_hash": digest(CONTENT), "revision": REVISION,
            "game_build": BUILD, "diagnostics": [],
        }
        task = create_task(self.user.id, "Accepted", self.profile.id, self.template.id, apl.id)
        version_id = task.apl_version_id
        frozen = task.apl_version.payload["content"]
        apl.content = "actions=/changed"
        apl.save(update_fields=["content"])
        task.refresh_from_db()
        self.assertEqual(task.apl_version_id, version_id)
        self.assertEqual(task.apl_version.payload["content"], frozen)
        self.assertEqual(SimcResourceVersion.objects.get(pk=version_id).payload["content"], CONTENT)

        with self.assertRaises(ProtectedError):
            task.apl_version.delete()

    @patch("botend.services.simc_task_service.validate_apl_for_profile")
    def test_task_creation_rechecks_apl_after_authoritative_validation(self, validate):
        apl = SimcApl.objects.create(
            name="Published", spec="warrior_fury", content=CONTENT,
            owner_user_id=self.user.id, is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=digest(CONTENT), validation_revision=REVISION,
            validation_game_build=BUILD,
        )

        def mutate(_profile, locked_apl, **_kwargs):
            SimcApl.objects.filter(pk=locked_apl.pk).update(content=CONTENT + " changed")
            return {"valid": True, "content_hash": digest(CONTENT), "revision": REVISION,
                    "game_build": BUILD, "diagnostics": []}

        validate.side_effect = mutate
        with self.assertRaises(TaskCreationError):
            create_task(self.user.id, "Race", self.profile.id, self.template.id, apl.id)

    @patch("botend.services.simc_task_service.validate_apl_for_profile")
    def test_rerun_copies_frozen_versions_without_live_revalidation(self, validate):
        from botend.services.task_rerun import create_rerun

        apl = SimcApl.objects.create(
            name="Published", spec="warrior_fury", content=CONTENT,
            owner_user_id=self.user.id, is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=digest(CONTENT), validation_revision=REVISION,
            validation_game_build=BUILD,
        )
        validate.return_value = {
            "valid": True, "content_hash": digest(CONTENT), "revision": REVISION,
            "game_build": BUILD, "diagnostics": [],
        }
        source = create_task(self.user.id, "Source", self.profile.id, self.template.id, apl.id)
        source.current_status = 2
        source.save(update_fields=["current_status"])
        validate.reset_mock()
        validate.return_value = {
            "valid": False, "content_hash": digest(CONTENT), "revision": REVISION,
            "game_build": BUILD, "diagnostics": [{"message": "profile changed"}],
        }
        rerun = create_rerun(source.id, self.user.id)
        validate.assert_not_called()
        self.assertEqual(SimcTask.objects.count(), 2)
        self.assertEqual(rerun.apl_version_id, source.apl_version_id)
        self.assertEqual(rerun.simulation_runs.count(), 0)

    @patch("botend.services.simc_apl.publish.validate_apl_for_profile")
    def test_generated_candidate_must_publish_before_execution(self, validate):
        from botend.services.simc_apl.publish import publish_apl

        candidate = SimcApl.objects.create(
            name="Candidate", spec="warrior_fury", content=CONTENT,
            owner_user_id=self.user.id, is_selectable=False,
            validation_status=SimcApl.VALIDATION_DRAFT,
        )
        validate.return_value = {
            "valid": False, "content_hash": digest(CONTENT), "revision": REVISION,
            "game_build": BUILD, "diagnostics": [{"message": "invalid candidate"}],
        }
        result = publish_apl(candidate.id, self.user.id, self.profile.id)
        candidate.refresh_from_db()
        self.assertFalse(result["valid"])
        self.assertFalse(candidate.is_selectable)
        self.assertEqual(candidate.validation_status, SimcApl.VALIDATION_INVALID)
