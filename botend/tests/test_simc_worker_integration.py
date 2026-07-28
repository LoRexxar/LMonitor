from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from LMonitor.config import DedicatedSimcWorkerSlot, Monitor_Type_BaseObject_List
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor


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

    def test_standalone_entry_once_uses_the_same_worker_lifecycle(self):
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

    def test_standalone_entry_continuous_mode_runs_worker_loop(self):
        from botend.simc_worker_entry import run_worker

        worker = MagicMock()
        with patch('botend.simc_worker_entry.signal.signal'):
            run_worker(worker_factory=MagicMock(return_value=worker))

        worker.run.assert_called_once_with()
        worker.recover_stale_tasks.assert_not_called()
        worker.consume_once.assert_not_called()

    def test_worker_has_standalone_entry_and_location_independent_start_script(self):
        with open('simc_worker.py', 'r', encoding='utf-8') as handle:
            entry = handle.read()
        with open('start_simc_worker.sh', 'r', encoding='utf-8') as handle:
            start_script = handle.read()

        self.assertIn("DJANGO_SETTINGS_MODULE", entry)
        self.assertIn("django.setup()", entry)
        self.assertIn("botend.simc_worker_entry", entry)
        self.assertIn('dirname -- "$0"', start_script)
        self.assertIn('.venv/bin/python', start_script)
        self.assertIn('exec "$PYTHON_BIN" "$PROJECT_ROOT/simc_worker.py" "$@"', start_script)
        self.assertNotIn('manage.py', start_script)
        self.assertNotIn('/home/lighthouse', start_script)

    def test_deploy_uses_standalone_worker_start_script(self):
        with open('deploy.sh', 'r', encoding='utf-8') as handle:
            script = handle.read()
        self.assertIn("screen -S lmsimc -X quit", script)
        self.assertIn("screen -dmS lmsimc", script)
        self.assertIn("./start_simc_worker.sh", script)
        self.assertNotIn("$PYTHON_BIN manage.py simc_worker", script)
        self.assertIn('MANAGE_SIMC_WORKER="${MANAGE_SIMC_WORKER:-1}"', script)
        self.assertIn('if [ "$MANAGE_SIMC_WORKER" = "1" ]; then', script)
        self.assertIn("lmweb|lmback|lmsimc", script)
        self.assertIn("flock -n 9", script)
