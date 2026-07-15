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
from botend.models import SimcProfile, SimcTask, SimcTaskArtifact, SimcTaskBatch
from botend.services.simc_artifacts import upsert_task_html_artifact


class SimcReviewFixTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="simc_review_owner")
        self.other = User.objects.create_user(username="simc_review_other")
        self.profile = SimcProfile.objects.create(user_id=self.user.id, name="review", spec="fury")
        self.factory = RequestFactory()

    def _task(self, **values):
        defaults = {
            "user_id": self.user.id,
            "simc_profile_id": self.profile.id,
            "name": "review task",
            "task_type": 1,
            "current_status": 2,
            "is_active": True,
        }
        defaults.update(values)
        return SimcTask.objects.create(**defaults)

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
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name="finished comparison", batch_type="comparison", status=2,
        )
        original = self._task(batch=batch, candidate_label="base", result_file=f"simc_task_1.html")
        rerun = SimcWorkbenchAPIView().post
        request = self.factory.post(
            f"/api/simc-workbench/tasks/{original.id}/",
            data=json.dumps({"action": "rerun"}), content_type="application/json",
        )
        request.user = self.user
        response = rerun(request, resource="tasks", object_id=original.id)
        self.assertEqual(response.status_code, 200)
        new_id = json.loads(response.content)["data"]["id"]
        self.assertIsNone(SimcTask.objects.get(id=new_id).batch_id)
        self.assertEqual(SimcTask.objects.filter(batch=batch).count(), 1)

    def test_compare_is_safe_without_summary_flag_too(self):
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name="safe", batch_type="comparison", status=2,
        )
        task = self._task(
            batch=batch,
            result_file="private/server/result.html",
            ext=json.dumps({"batch_compare": {
                "label": "safe label", "index": 0, "is_base": True,
                "candidate": {"apl": "actions=secret", "internal_file": "/srv/secret.simc"},
            }}),
        )
        request = self.factory.get(f"/api/simc-regular-compare/?batch_id={batch.id}")
        request.user = self.user
        with patch.object(SimcRegularCompareAPIView, "_get_result_file_content", return_value="<html/>"), patch.object(
            SimcRegularCompareAPIView, "_parse_regular_result", return_value={
                "dps": 123, "abilities": [{"raw": "secret body"}], "talents": {"apl": "secret"},
            },
        ):
            response = SimcRegularCompareAPIView().get(request)
        payload = json.loads(response.content)
        self.assertTrue(payload["success"])
        self.assertEqual(
            set(payload["data"]["tasks"][0]),
            {"id", "name", "label", "rank", "dps", "delta_dps", "delta_percent"},
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("result_file", "actions=secret", "internal_file", "/srv/", "abilities", "talents", "candidate"):
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
