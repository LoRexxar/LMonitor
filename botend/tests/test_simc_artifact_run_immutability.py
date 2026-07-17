import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.models import SimcApl, SimcContentTemplate, SimcProfile, SimcTaskArtifact, SimulationRun
from botend.services.simc_artifacts import upsert_task_html_artifact
from botend.services.simc_task_service import create_task


class SimcArtifactRunImmutabilityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='artifact-owner', password='x')
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id, name='P', spec='fury',
            player_config_mode='manual_equipment',
            player_equipment='warrior="x"\nspec=fury', is_active=True,
        )
        self.template = SimcContentTemplate.objects.create(
            name='T', template_type='base_template', spec='fury',
            content='{simulation_options}\n{player_config}\n{action_list}\n{output_options}',
            is_active=True, is_selectable=True,
        )
        self.apl = SimcApl.objects.create(
            name='A', spec='fury', content='actions=/bloodthirst',
            is_system=True, is_active=True, is_selectable=True,
        )

    def make_task(self, name='artifact-task'):
        return create_task(
            user_id=self.user.id, name=name, profile_id=self.profile.id,
            template_id=self.template.id, apl_id=self.apl.id,
        )

    def test_create_task_uuid_result_file_can_be_registered(self):
        task = self.make_task()
        self.assertRegex(task.result_file, r'^[0-9a-f]{32}\.html$')
        with TemporaryDirectory() as base_dir, override_settings(BASE_DIR=base_dir):
            result_root = Path(base_dir) / 'static' / 'simc_results'
            result_root.mkdir(parents=True)
            report = result_root / task.result_file
            report.write_text('uuid report', encoding='utf-8')

            artifact = upsert_task_html_artifact(task, task.result_file)

        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.file_path, f'simc_results/{task.result_file}')

    @patch('botend.services.simc_artifacts._validated_result')
    def test_register_rejects_run_owned_by_another_task(self, validated_result):
        task = self.make_task('owner')
        other_task = self.make_task('other')
        foreign_run = SimulationRun.objects.create(task=other_task, sequence=1)
        validated_result.return_value = (Path('/tmp/unused'), f'simc_results/{task.result_file}')

        artifact = upsert_task_html_artifact(task, task.result_file, run=foreign_run)

        self.assertIsNone(artifact)
        self.assertFalse(SimcTaskArtifact.objects.exists())
        validated_result.assert_not_called()

    def test_each_run_gets_distinct_physical_report_and_old_artifact_stays_stable(self):
        task = self.make_task()
        run1 = SimulationRun.objects.create(task=task, sequence=1)
        run2 = SimulationRun.objects.create(task=task, sequence=2)

        with TemporaryDirectory() as base_dir, override_settings(BASE_DIR=base_dir):
            result_root = Path(base_dir) / 'static' / 'simc_results'
            result_root.mkdir(parents=True)
            monitor = SimcMonitor(None, task)
            monitor.result_path = str(result_root)
            monitor.simc_path = '/fake/simc'
            generated = []

            def fake_run(cmd, **kwargs):
                output_path = Path(next(arg.split('=', 1)[1] for arg in cmd if arg.startswith('html=')))
                content = f'report-{len(generated) + 1}'
                output_path.write_text(content, encoding='utf-8')
                generated.append((output_path, content))
                return SimpleNamespace(
                    returncode=0, stderr='',
                    stdout='DPS=12345\n  bloodthirst Count=10 pDPS= 12345\n',
                )

            with patch('botend.controller.plugins.simc.SimcMonitor.subprocess.run', side_effect=fake_run), \
                    patch('botend.interface.ossupload.ossUpload', return_value=True):
                self.assertTrue(monitor.execute_simc_command('/tmp/one.simc', task, run=run1))
                old_artifact = SimcTaskArtifact.objects.get(run=run1)
                old_path = old_artifact.file_path
                old_content = (Path(base_dir) / 'static' / old_path).read_text(encoding='utf-8')

                self.assertTrue(monitor.execute_simc_command('/tmp/two.simc', task, run=run2))

            old_artifact.refresh_from_db()
            new_artifact = SimcTaskArtifact.objects.get(run=run2)
            self.assertNotEqual(old_artifact.file_path, new_artifact.file_path)
            self.assertEqual(old_artifact.file_path, old_path)
            self.assertEqual((Path(base_dir) / 'static' / old_path).read_text(encoding='utf-8'), old_content)
            self.assertEqual(len({path for path, _ in generated}), 2)
            self.assertTrue(all(re.search(r'_run_\d+\.html$', path.name) for path, _ in generated))
