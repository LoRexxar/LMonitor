from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase


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
