import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings

from botend.dashboard.api import (
    SimcBackendBinaryAPIView,
    SimcRegularCompareAPIView,
    SimcWorkbenchAPIView,
)
from botend.models import (
    SimcApl,
    SimcContentTemplate,
    SimcProfile,
    SimcTask,
    SimcTaskArtifact,
    SimulationRun,
)
from botend.services.simc_artifacts import upsert_task_html_artifact
from botend.services.simc_task_service import create_task


@override_settings(SIMC_APL_CURRENT_IDENTITY=('test-simc-revision', 'test-game-build'))
class SimcReviewFixTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="simc_review_owner")
        self.other = User.objects.create_user(username="simc_review_other")
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name="review",
            spec="fury",
            player_config_mode="manual_equipment",
            player_equipment='warrior="Review"\nspec=fury',
        )
        self.template = SimcContentTemplate.objects.create(
            name="review template",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="{player_config}\n{action_list}",
            is_active=True,
            is_selectable=True,
        )
        self.apl = SimcApl.objects.create(
            name="review apl",
            spec="fury",
            content="actions=/auto_attack",
            owner_user_id=self.user.id,
            is_active=True,
            is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=hashlib.sha256(b"actions=/auto_attack").hexdigest(),
            validation_revision='test-simc-revision',
            validation_game_build='test-game-build',
        )
        self.factory = RequestFactory()

    def _task(self, **values):
        validation = {
            'valid': True,
            'content_hash': hashlib.sha256(self.apl.content.encode()).hexdigest(),
            'revision': 'test-simc-revision',
            'game_build': 'test-game-build',
        }
        with patch('botend.services.simc_task_service.validate_apl_for_profile', return_value=validation):
            task = create_task(
                user_id=self.user.id,
                name=values.pop("name", "review task"),
                profile_id=self.profile.id,
                template_id=self.template.id,
                apl_id=self.apl.id,
                mode=values.pop("mode", "normal"),
                candidates=values.pop("candidates", None),
            )
        for field, value in {"current_status": 2, **values}.items():
            setattr(task, field, value)
        task.save()
        return task

    def test_worker_artifact_upsert_accepts_only_task_bound_result(self):
        with tempfile.TemporaryDirectory() as base_dir:
            result_dir = Path(base_dir) / "static" / "simc_results"
            result_dir.mkdir(parents=True)
            task = self._task()
            filename = f"simc_task_{task.id}.html"
            report = result_dir / filename
            report.write_text("<html>first</html>", encoding="utf-8")
            with override_settings(BASE_DIR=base_dir):
                first = upsert_task_html_artifact(task, filename)
                self.assertIsNotNone(first)
                self.assertEqual(first.task.user_id, self.user.id)
                self.assertEqual(first.file_path, f"simc_results/{filename}")
                report.write_text("<html>updated report</html>", encoding="utf-8")
                second = upsert_task_html_artifact(task, filename)
                self.assertEqual(first.id, second.id)
                self.assertEqual(SimcTaskArtifact.objects.filter(task=task).count(), 1)
                self.assertEqual(second.file_size, report.stat().st_size)

                other_task = self._task(name="other task")
                self.assertIsNone(upsert_task_html_artifact(other_task, filename))
                self.assertIsNone(upsert_task_html_artifact(task, "../settings.py"))
                self.assertIsNone(upsert_task_html_artifact(task, "/tmp/report.html"))

    def test_worker_artifact_upsert_accepts_canonical_attribute_result(self):
        with tempfile.TemporaryDirectory() as base_dir:
            result_dir = Path(base_dir) / "static" / "simc_results"
            result_dir.mkdir(parents=True)
            task = self._task(task_type=2)
            filename = f"{task.id}_gear_crit_900_gear_haste_929.html"
            (result_dir / filename).write_text("<html>attribute report</html>", encoding="utf-8")

            with override_settings(BASE_DIR=base_dir):
                artifact = upsert_task_html_artifact(task, filename)

            self.assertIsNotNone(artifact)
            self.assertEqual(artifact.file_path, f"simc_results/{filename}")

    def test_member_rerun_is_detached_from_immutable_batch(self):
        original = self._task(
            mode="comparison", result_file="simc_task_1.html",
            candidates=[{
                "candidate_key": "base", "candidate_label": "base",
                "candidate_params": {"is_base": True},
            }],
        )
        rerun = SimcWorkbenchAPIView().post
        request = self.factory.post(
            f"/api/simc-workbench/tasks/{original.id}/",
            data=json.dumps({"action": "rerun"}), content_type="application/json",
        )
        request.user = self.user
        validation = {
            'valid': True,
            'content_hash': hashlib.sha256(self.apl.content.encode()).hexdigest(),
            'revision': 'test-simc-revision',
            'game_build': 'test-game-build',
        }
        with patch('botend.services.simc_task_service.validate_apl_for_profile', return_value=validation):
            response = rerun(request, resource="tasks", object_id=original.id)
        self.assertEqual(response.status_code, 200, response.content.decode())
        new_id = json.loads(response.content)["data"]["id"]
        new_task = SimcTask.objects.get(id=new_id)
        self.assertEqual(new_task.source_task_id, original.id)
        self.assertEqual(new_task.mode, "normal")
        self.assertEqual(new_task.simulation_runs.count(), 1)
        self.assertEqual(SimcTask.objects.filter(source_task=original).count(), 1)

    def test_compare_is_safe_without_summary_flag_too(self):
        task = self._task(
            mode="comparison",
            candidates=[{
                "candidate_key": "base", "candidate_label": "safe label",
                "candidate_params": {
                    "is_base": True, "apl_override": "actions=secret",
                    "talent_candidate": {"name": "secret", "talent": "/srv/secret.simc"},
                },
            }],
            result_file="private/server/result.html",
        )
        run = task.simulation_runs.get()
        run.status = "completed"
        run.result_summary = {
            "dps": 123, "abilities": [{"raw": "secret body"}], "talents": {"apl": "secret"},
        }
        run.save(update_fields=["status", "result_summary"])
        request = self.factory.get(f"/api/simc-regular-compare/?task_id={task.id}")
        request.user = self.user
        with patch.object(SimcRegularCompareAPIView, "_get_result_file_content", return_value="<html/>"), patch.object(
            SimcRegularCompareAPIView, "_parse_regular_result", return_value={
                "dps": 123, "abilities": [{"raw": "secret body"}], "talents": {"apl": "secret"},
            },
        ):
            response = SimcRegularCompareAPIView().get(request)
        payload = json.loads(response.content)
        self.assertTrue(payload["success"])
        self.assertEqual(set(payload["data"]["runs"][0]), {
            "id", "name", "label", "rank", "dps", "delta_dps", "delta_percent", "candidate",
        })
        self.assertEqual(payload["data"]["runs"][0]["candidate"], {
            "type": "talent", "name": "secret",
        })
        self.assertEqual(payload["data"]["runs"][0]["dps"], 123)
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("result_file", "actions=secret", "internal_file", "/srv/", "abilities", "talents", "candidate_params"):
            self.assertNotIn(forbidden, serialized)

    def test_backend_exception_is_logged_but_not_returned(self):
        request = self.factory.get("/api/simc-backend-binary/")
        request.user = self.user
        secret = "/srv/private/simc path failed"
        with patch.object(SimcBackendBinaryAPIView, "_resolve_local_build_paths", side_effect=RuntimeError(secret)):
            response = SimcBackendBinaryAPIView().get(request)
        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(payload["error"], "获取 SimC 后端状态失败，请稍后重试")
        self.assertNotIn(secret, json.dumps(payload, ensure_ascii=False))
