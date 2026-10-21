import os
import stat
import subprocess
import tempfile
from pathlib import Path
from django.conf import settings
from django.test import SimpleTestCase
from LMonitor.config import DedicatedSimcWorkerSlot
from LMonitor.config import Monitor_Type_BaseObject_List
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from unittest.mock import MagicMock
from unittest.mock import patch


class SimcWorkerIntegrationTests(SimpleTestCase):
    def test_public_monitor_keeps_type_indexes_but_does_not_register_simc_consumer(self):
        self.assertNotIn(SimcMonitor, Monitor_Type_BaseObject_List)
        self.assertIs(Monitor_Type_BaseObject_List[15], DedicatedSimcWorkerSlot)
        self.assertTrue(DedicatedSimcWorkerSlot(None, None).scan())

    def test_frozen_canonical_spec_is_converted_at_worker_composer_boundary(self):
        from botend.controller.plugins.simc.SimcMonitor import _composer_identity

        self.assertEqual(_composer_identity('warrior_arms'), ('arms', 'warrior'))
        self.assertEqual(_composer_identity('fury'), ('fury', 'warrior'))
        self.assertEqual(_composer_identity('paladin_protection'), ('protection', 'paladin'))

    def test_deploy_only_runs_repeatable_release_steps(self):
        with open('deploy.sh', 'r', encoding='utf-8') as handle:
            script = handle.read()
        self.assertIn("git pull origin master", script)
        self.assertIn("manage.py migrate --no-input", script)
        self.assertIn("collectstatic --no-input --ignore='simc_results/*'", script)
        self.assertIn("manage.py runserver 0.0.0.0:18000 --noreload", script)
        self.assertIn("for session in lmweb lmback lmsimc", script)
        self.assertIn('curl -fsS http://127.0.0.1:18000/', script)
        self.assertIn("screen -S lmsimc -X quit", script)
        self.assertIn("screen -dmS lmsimc", script)
        self.assertIn("manage.py simc_worker", script)
        self.assertIn("lmweb|lmback|lmsimc", script)
        self.assertIn("flock -n 9", script)
        self.assertNotIn("manage.py update_simc_binary", script)
        self.assertNotIn("repair_gear_builder_tooltips", script)
        self.assertNotIn("repair_ptr_talent_metadata", script)
        self.assertNotIn("import_mythic_dungeon_data", script)
        self.assertGreater(
            script.index("screen -S lmsimc -X quit"),
            script.index("=== 6. 重启 lmsimc ==="),
        )

    def test_deploy_reexecutes_updated_script_before_release_steps(self):
        deploy_script = (Path(settings.BASE_DIR) / 'deploy.sh').read_text(encoding='utf-8')
        migrate_command = '"$PYTHON_BIN" manage.py migrate --no-input'
        self.assertEqual(deploy_script.count(migrate_command), 1)
        updated_script = deploy_script.replace(
            migrate_command,
            'printf "updated-script\\n" >> "$DEPLOY_TEST_LOG"\n\n' + migrate_command,
            1,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            current_deploy = root / 'deploy.sh'
            updated_deploy = root / 'updated-deploy.sh'
            current_deploy.write_text(deploy_script, encoding='utf-8')
            updated_deploy.write_text(updated_script, encoding='utf-8')
            current_deploy.chmod(current_deploy.stat().st_mode | stat.S_IXUSR)
            updated_deploy.chmod(updated_deploy.stat().st_mode | stat.S_IXUSR)

            (bin_dir / 'git').write_text(
                '#!/bin/bash\n'
                'count_file="$PWD/git-pull-count"\n'
                'count=0\n'
                '[ ! -f "$count_file" ] || count="$(<"$count_file")"\n'
                'count=$((count + 1))\n'
                'printf "%s" "$count" > "$count_file"\n'
                'if [ "$count" = 1 ]; then\n'
                '  cp "$UPDATED_DEPLOY" "$PWD/deploy.sh.next"\n'
                '  chmod +x "$PWD/deploy.sh.next"\n'
                '  mv "$PWD/deploy.sh.next" "$PWD/deploy.sh"\n'
                'fi\n',
                encoding='utf-8',
            )
            (bin_dir / 'python3').write_text(
                '#!/bin/bash\nprintf "python:%s\\n" "$*" >> "$DEPLOY_TEST_LOG"\n',
                encoding='utf-8',
            )
            (bin_dir / 'pgrep').write_text('#!/bin/bash\nexit 1\n', encoding='utf-8')
            (bin_dir / 'sleep').write_text('#!/bin/bash\nexit 0\n', encoding='utf-8')
            (bin_dir / 'curl').write_text('#!/bin/bash\nexit 0\n', encoding='utf-8')
            (bin_dir / 'screen').write_text(
                '#!/bin/bash\n'
                'if [ "$1" = "-list" ]; then\n'
                '  printf "1.lmweb\\n2.lmback\\n3.lmsimc\\n"\n'
                'fi\n',
                encoding='utf-8',
            )
            for executable in bin_dir.iterdir():
                executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

            log_path = root / 'deploy.log'
            env = os.environ.copy()
            env.update({
                'DEPLOY_TEST_LOG': str(log_path),
                'UPDATED_DEPLOY': str(updated_deploy),
                'PATH': f'{bin_dir}:{env["PATH"]}',
            })
            result = subprocess.run(
                [str(current_deploy)],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / 'git-pull-count').read_text(), '2')
            log_lines = log_path.read_text(encoding='utf-8').splitlines()
            self.assertEqual(log_lines.count('updated-script'), 1)
            self.assertEqual(log_lines.count('python:manage.py migrate --no-input'), 1)


class StandaloneSimcConsumerTests(SimpleTestCase):
    def test_once_uses_worker_recovery_and_single_consume_lifecycle(self):
        from botend.simc_worker_entry import run_worker

        worker = MagicMock()
        factory = MagicMock(return_value=worker)
        with patch('botend.simc_worker_entry.signal.signal') as register_signal:
            run_worker(once=True, poll_interval=1.5, worker_factory=factory)

        factory.assert_called_once_with(poll_interval=1.5)
        self.assertEqual(register_signal.call_count, 2)
        worker.recover_stale_tasks.assert_called_once_with()
        worker.consume_once.assert_called_once_with()
        worker.run.assert_not_called()

    def test_continuous_mode_runs_worker_loop(self):
        from botend.simc_worker_entry import run_worker

        worker = MagicMock()
        with patch('botend.simc_worker_entry.signal.signal'):
            run_worker(worker_factory=MagicMock(return_value=worker))

        worker.run.assert_called_once_with()
        worker.recover_stale_tasks.assert_not_called()
        worker.consume_once.assert_not_called()

    def test_entry_and_start_script_are_location_independent(self):
        with open('simc_worker.py', 'r', encoding='utf-8') as handle:
            entry = handle.read()
        with open('start_simc_worker.sh', 'r', encoding='utf-8') as handle:
            start_script = handle.read()

        self.assertIn('DJANGO_SETTINGS_MODULE', entry)
        self.assertIn('django.setup()', entry)
        self.assertIn('botend.simc_worker_entry', entry)
        self.assertIn('dirname -- "$0"', start_script)
        self.assertIn('.venv/bin/python', start_script)
        self.assertIn('exec "$PYTHON_BIN" "$PROJECT_ROOT/simc_worker.py" "$@"', start_script)
        self.assertNotIn('manage.py', start_script)
        self.assertNotIn('/home/lighthouse', start_script)
