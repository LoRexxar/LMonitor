"""Real POSIX subprocess tests for the supervisor + scheduler retirement protocol."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]

# No signal mocks: this process imports the production admission/retirement code.
WORKER = r'''
import json, os, signal, sys, threading, time
from botend.backend_lifecycle import BackendLifecycle, worker_cycles
path, mode = sys.argv[1:]
def record(event):
    with open(path, 'a') as f:
        f.write(json.dumps({'event': event, 'pid': os.getpid(), 'at': time.monotonic()}) + '\n')
if mode == 'helper':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    record('helper-started')
    while True:
        time.sleep(1)
if mode == 'with-helper':
    import subprocess
    subprocess.Popen([sys.executable, __file__, path, 'helper'])
if mode.startswith('scheduler'):
    os.environ['DJANGO_SETTINGS_MODULE'] = 'LMonitor.settings_test_sqlite'
    import django
    django.setup()
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import patch, Mock
    from django.conf import settings
    from botend import views
    from botend.management.commands.LMonitorCoreBackend import Command
    settings.MONITOR_TASK_AUTO_SYNC_PLUGINS = False
    settings.MONITOR_TASK_MAX_RUNTIME_SECONDS = 5
    settings.MONITOR_RETIRE_CLEANUP_SECONDS = .2
    settings.SNAPSHOT_CACHE_GC_WORKER_ENABLED = True
    def claim(*, lease_owner, task_names=None):
        label = 'isolated' if task_names else 'ordinary'
        record('claim-' + label)
        if mode == 'scheduler-claim':
            time.sleep(.5)  # Retirement while the claim is in progress.
        return SimpleNamespace(name=label, pk=label)
    def execute(self, task, owner):
        record('started-' + task.name)
        time.sleep(1.6)
        record('finished-' + task.name)
    def refresh(label):
        def call(*args, **kwargs):
            record('started-' + label)
            time.sleep(1.6)
            record('finished-' + label)
        return call
    with ExitStack() as stack:
        stack.enter_context(patch.object(views, 'filter_runnable_tasks', return_value=Mock(count=lambda: 2)))
        stack.enter_context(patch.object(views, 'claim_next_monitor_task', side_effect=claim))
        stack.enter_context(patch.object(views.LMonitorCore, '_execute_claimed_task', execute))
        for module, fn, label in [
            ('gear_catalog_snapshot', 'refresh_catalog_snapshots', 'gear'),
            ('journal_loot_snapshot', 'refresh_journal_loot_snapshots', 'loot'),
            ('news_snapshot', 'refresh_news_snapshot', 'news'),
            ('class_guide_snapshot', 'refresh_guide_snapshot', 'guides'),
            ('journal_snapshot', 'refresh_journal_snapshot', 'journal'),
            ('snapshot_cache_maintenance', 'run_maintenance_once', 'gc'),
        ]:
            stack.enter_context(patch('botend.services.' + module + '.' + fn, side_effect=refresh(label)))
        if mode == 'scheduler-claim':
            from botend.services import gear_catalog_snapshot
            original_start = gear_catalog_snapshot.start_catalog_snapshot_worker
            def wait_for_retirement(*, lifecycle):
                # Deterministic handshake: do not race main-thread startup
                # against the parent's poll interval before SIGUSR1 arrives.
                until = time.monotonic() + 4
                while not lifecycle.retiring and time.monotonic() < until:
                    time.sleep(.01)
                return original_start(lifecycle=lifecycle)
            stack.enter_context(patch.object(gear_catalog_snapshot, 'start_catalog_snapshot_worker', side_effect=wait_for_retirement))
            stack.enter_context(patch.object(views, '_release_monitor_task_lease_safely',
                                            side_effect=lambda pk, owner: record('released-' + pk)))
        if mode == 'scheduler-error':
            def fail_start(*args, **kwargs):
                time.sleep(.1)  # Allow the real isolated scan thread to enter.
                raise RuntimeError('injected scheduler startup failure')
            stack.enter_context(patch('botend.services.gear_catalog_snapshot.start_catalog_snapshot_worker', side_effect=fail_start))
        record('boot')
        Command().handle()
        record('drained')
    sys.exit(0)
if mode == 'mixed-deadlines':
    life = BackendLifecycle(max_runtime=1, cleanup_seconds=.2)
    life.install_signal_handlers()
    def work(label, duration):
        with life.admission(label) as admitted:
            assert admitted
            record('started-' + label)
            time.sleep(duration)
            record('finished-' + label)
    old = threading.Thread(target=work, args=('expired', 60), daemon=True)
    old.start()
    time.sleep(.6)
    healthy = threading.Thread(target=work, args=('healthy', .8), daemon=True)
    healthy.start()
    time.sleep(.1)
    life.drain_threads([old, healthy])
    sys.exit(0)
life = BackendLifecycle(max_runtime=4 if mode != 'hung' else .5, cleanup_seconds=.2)
life.install_signal_handlers()
if mode == 'resist-stop':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
record('boot')
if mode == 'crash':
    time.sleep(.3)
    os._exit(19)
def run():
    for _ in worker_cycles(life, interval=.01, label='slow-task'):
        record('started')
        if mode == 'hung':
            time.sleep(60)
        else:
            time.sleep(1.6)
        record('finished')
t = threading.Thread(target=run, daemon=True)
t.start()
while not life.retiring:
    time.sleep(.02)
record('retiring')
life.drain_threads([t])
record('drained')
'''


class BackendRetirementProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'events.jsonl'
        self.worker = Path(self.tmp.name) / 'worker.py'
        self.worker.write_text(WORKER)
        self.output = open(Path(self.tmp.name) / 'output.log', 'w+')
        self.addCleanup(self.output.close)
        self.processes = []
        self.addCleanup(self._stop_all)

    def _stop_all(self):
        for proc in self.processes:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=3)
        # A failed assertion must not leave fixture children behind.
        for event in self.events():
            try:
                cmdline = Path(f"/proc/{event['pid']}/cmdline").read_bytes().split(b'\0')
                if os.fsencode(self.worker) in cmdline:
                    os.kill(event['pid'], signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def wait_for(self, predicate, timeout=8):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            events = self.events()
            if predicate(events):
                return events
            time.sleep(.025)
        self.output.flush()
        self.output.seek(0)
        self.fail(f'timed out: {self.events()}\n{self.output.read()}')

    def launch(self, mode='slow', supervisor=True):
        env = dict(os.environ, PYTHONPATH=str(ROOT), RESTART_INTERVAL='1',
                   LMONITOR_BACKEND_RETIRE_AFTER='0',
                   CHECK_INTERVAL='.05', STOP_GRACE_SECONDS='.2',
                   BACKEND_LOCK_FILE=str(Path(self.tmp.name) / 'supervisor.lock'))
        child = [sys.executable, str(self.worker), str(self.log), mode]
        command = [str(ROOT / 'start.sh'), *child] if supervisor else child
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=self.output,
                                stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(proc)
        return proc

    def test_rotation_drains_slow_task_before_restart_and_stops_dispatch(self):
        proc = self.launch()
        events = self.wait_for(lambda es: len([e for e in es if e['event'] == 'boot']) >= 2)
        pid = next(e['pid'] for e in events if e['event'] == 'boot')
        first = [e for e in events if e['pid'] == pid]
        self.assertEqual([e['event'] for e in first],
                         ['boot', 'started', 'retiring', 'finished', 'drained'])
        second_boot = next(e for e in events if e['event'] == 'boot' and e['pid'] != pid)
        self.assertGreater(second_boot['at'], first[-1]['at'])
        self.assertIsNone(proc.poll())

    def test_crashed_child_is_restarted(self):
        self.launch('crash')
        events = self.wait_for(lambda es: len([e for e in es if e['event'] == 'boot']) >= 2)
        self.assertEqual(len({e['pid'] for e in events if e['event'] == 'boot'}), 2)

    def test_expired_work_is_bounded_and_restarted(self):
        self.launch('hung')
        events = self.wait_for(lambda es: len([e for e in es if e['event'] == 'boot']) >= 2)
        first_pid = events[0]['pid']
        self.assertNotIn('finished', [e['event'] for e in events if e['pid'] == first_pid])
        self.output.flush()
        self.output.seek(0)
        self.assertIn('retirement deadline exceeded', self.output.read())

    def test_operator_stop_during_drain_does_not_restart(self):
        proc = self.launch()
        self.wait_for(lambda es: any(e['event'] == 'retiring' for e in es))
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=3), 0)
        time.sleep(.25)
        self.assertEqual(len([e for e in self.events() if e['event'] == 'boot']), 1)
        self.assertNotIn('finished', [e['event'] for e in self.events()])

    def test_sigint_is_explicit_stop_not_retirement(self):
        proc = self.launch(supervisor=False)
        self.wait_for(lambda es: any(e['event'] == 'started' for e in es))
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=3), 130)
        self.assertNotIn('finished', [e['event'] for e in self.events()])

    def test_usr1_retires_without_interrupting_the_task(self):
        proc = self.launch(supervisor=False)
        self.wait_for(lambda es: any(e['event'] == 'started' for e in es))
        proc.send_signal(signal.SIGUSR1)
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertEqual([e['event'] for e in self.events()],
                         ['boot', 'started', 'retiring', 'finished', 'drained'])

    def test_actual_command_drains_ordinary_isolated_and_all_maintenance_workers(self):
        proc = self.launch('scheduler')
        events = self.wait_for(lambda es: len([e for e in es if e['event'] == 'boot']) >= 2, timeout=20)
        first_pid = events[0]['pid']
        first = [e['event'] for e in events if e['pid'] == first_pid]
        self.assertEqual(first.count('claim-ordinary'), 1)
        self.assertEqual(first.count('claim-isolated'), 1)
        for label in ['ordinary', 'isolated', 'gear', 'loot', 'news', 'guides', 'journal', 'gc']:
            self.assertEqual(first.count('started-' + label), 1, first)
            self.assertEqual(first.count('finished-' + label), 1, first)
            self.assertLess(first.index('finished-' + label), first.index('drained'))
        self.assertIsNone(proc.poll())

    def test_scheduler_startup_error_drains_already_started_worker_then_fails(self):
        proc = self.launch('scheduler-error', supervisor=False)
        self.assertEqual(proc.wait(timeout=15), 1)
        self.assertEqual([e['event'] for e in self.events()],
                         ['boot', 'claim-isolated', 'started-isolated', 'finished-isolated'])
        self.output.flush()
        self.output.seek(0)
        self.assertIn('injected scheduler startup failure', self.output.read())

    def test_second_supervisor_refuses_to_kill_the_first_child(self):
        first = self.launch()
        self.wait_for(lambda es: any(e['event'] == 'started' for e in es))
        duplicate = self.launch()
        self.assertEqual(duplicate.wait(timeout=3), 1)
        self.assertIsNone(first.poll())
        self.assertEqual(len([e for e in self.events() if e['event'] == 'boot']), 1)

    def test_claim_in_progress_is_drained_without_a_second_claim(self):
        proc = self.launch('scheduler-claim', supervisor=False)
        self.wait_for(lambda es: any(e['event'] == 'claim-isolated' for e in es))
        proc.send_signal(signal.SIGUSR1)
        self.assertEqual(proc.wait(timeout=5), 0)
        events = [e['event'] for e in self.events()]
        self.assertEqual(events.count('claim-isolated'), 1)
        self.assertNotIn('claim-ordinary', events)
        self.assertEqual(events.count('started-isolated'), 1)
        self.assertEqual(events.count('finished-isolated'), 1)
        self.assertLess(events.index('finished-isolated'), events.index('drained'))

    def test_an_expired_worker_cannot_kill_another_still_legal_worker(self):
        proc = self.launch('mixed-deadlines', supervisor=False)
        self.assertEqual(proc.wait(timeout=4), 75)
        self.assertEqual([e['event'] for e in self.events()],
                         ['started-expired', 'started-healthy', 'finished-healthy'])

    def test_hup_from_screen_shutdown_stops_the_separate_session(self):
        proc = self.launch('resist-stop')
        events = self.wait_for(lambda es: any(e['event'] == 'started' for e in es))
        child = events[0]['pid']
        self.assertEqual(os.getpgid(child), child)
        self.assertEqual(os.getsid(child), child)
        proc.send_signal(signal.SIGHUP)
        self.assertEqual(proc.wait(timeout=3), 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        self.assertEqual(len([e for e in self.events() if e['event'] == 'boot']), 1)

    def test_operator_stop_terminates_same_group_grandchild(self):
        proc = self.launch('with-helper')
        events = self.wait_for(lambda es: any(e['event'] == 'helper-started' for e in es)
                               and any(e['event'] == 'boot' for e in es))
        backend = next(e['pid'] for e in events if e['event'] == 'boot')
        helper = next(e['pid'] for e in events if e['event'] == 'helper-started')
        self.assertEqual(os.getpgid(helper), backend)
        proc.send_signal(signal.SIGHUP)
        self.assertEqual(proc.wait(timeout=3), 0)
        def terminated(pid):
            try:
                return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()[0] == 'Z'
            except (FileNotFoundError, ProcessLookupError):
                return True
        deadline = time.monotonic() + 2
        while not terminated(helper) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(terminated(helper), 'live helper escaped process-group cleanup')
        self.assertEqual(len([e for e in self.events() if e['event'] == 'boot']), 1)

    def test_real_screen_quit_does_not_orphan_setsid_child(self):
        name = 'lmonitor-retirement-test-' + str(os.getpid())
        env = dict(os.environ, PYTHONPATH=str(ROOT), RESTART_INTERVAL='0',
                   CHECK_INTERVAL='.05', STOP_GRACE_SECONDS='.2',
                   BACKEND_LOCK_FILE=str(Path(self.tmp.name) / 'screen.lock'))
        proc = subprocess.Popen(['screen', '-DmS', name, 'bash', str(ROOT / 'start.sh'),
                                 sys.executable, str(self.worker), str(self.log), 'resist-stop'],
                                cwd=ROOT, env=env, stdout=self.output,
                                stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(proc)
        self.addCleanup(lambda: subprocess.run(['screen', '-S', name, '-X', 'quit'],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        events = self.wait_for(lambda es: any(e['event'] == 'started' for e in es))
        child = events[0]['pid']
        subprocess.run(['screen', '-S', name, '-X', 'quit'], check=True)
        proc.wait(timeout=3)
        until = time.monotonic() + 3
        while time.monotonic() < until:
            try:
                state = Path(f'/proc/{child}/stat').read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                return
            if state == 'Z':
                return  # Already exited; init will reap the orphan's zombie.
            time.sleep(.025)
        self.fail('screen quit left a live detached backend')

    def test_child_keeps_lock_after_supervisor_crash(self):
        proc = self.launch()
        self.wait_for(lambda es: any(e['event'] == 'started' for e in es))
        proc.kill()
        proc.wait(timeout=3)
        duplicate = self.launch()
        self.assertEqual(duplicate.wait(timeout=3), 1)
        self.assertEqual(len([e for e in self.events() if e['event'] == 'boot']), 1)

    def test_lifecycle_rejects_nonfinite_and_invalid_limits(self):
        from botend.backend_lifecycle import BackendLifecycle
        for field in ('max_runtime', 'cleanup_seconds', 'retire_after'):
            for value in (-1, float('inf'), float('nan')):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    BackendLifecycle(**{field: value})
        for field in ('max_runtime', 'cleanup_seconds'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                BackendLifecycle(**{field: 0})

    def test_explicit_stop_has_a_kill_fallback_for_an_unresponsive_child(self):
        proc = self.launch('resist-stop')
        self.wait_for(lambda es: any(e['event'] == 'started' for e in es))
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertNotIn('finished', [e['event'] for e in self.events()])
