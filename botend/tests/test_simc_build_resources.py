"""验证宽松启动预算、资源限制、兼容回退与避免重复编译。"""
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest
from unittest import mock

from botend.services import simc_build_resources as resources


class SimcBuildResourceTests(unittest.TestCase):
    def test_shared_2c8g_host_reserves_resources_for_business(self):
        budget = resources.choose_budget(8 * resources.GIB, 6 * resources.GIB, 2)
        self.assertEqual(budget.memory, 4 * resources.GIB)
        self.assertEqual(budget.cpu_percent, 100)
        self.assertLess(budget.high, budget.memory)

    def test_busy_host_warns_without_blocking_or_shrinking_budget(self):
        for available in (4 * resources.GIB, 3 * resources.GIB, 128 * resources.MIB, 0):
            budget = resources.choose_budget(8 * resources.GIB, available, 2)
            self.assertEqual(budget.memory, 4 * resources.GIB)
            self.assertEqual(bool(budget.warnings), available < 4 * resources.GIB)

    def test_small_cpu_host_keeps_half_cpu_for_business(self):
        self.assertEqual(resources.choose_budget(8 * resources.GIB, 6 * resources.GIB, 1).cpu_percent, 50)

    def test_larger_machine_does_not_expand_shared_production_budget(self):
        budget = resources.choose_budget(64 * resources.GIB, 60 * resources.GIB, 32)
        self.assertEqual(budget.memory, 4 * resources.GIB)
        self.assertEqual(budget.cpu_percent, 100)

    def test_only_cli_target_is_built_without_tests_or_lto(self):
        cmake, ninja = resources.build_commands('/source', '/build')
        self.assertIn('-DBUILD_TESTING=OFF', cmake)
        self.assertIn('-DCMAKE_INTERPROCEDURAL_OPTIMIZATION=OFF', cmake)
        self.assertEqual(ninja, ['ninja', '-C', '/build', '-j1', 'simc'])

    def test_parent_slice_headroom_limits_budget_even_when_host_has_free_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / 'user.slice'
            child = parent / 'session.scope'
            child.mkdir(parents=True)
            (child / 'memory.max').write_text('max')
            (parent / 'memory.max').write_text(str(4 * resources.GIB))
            (parent / 'memory.current').write_text(str(2 * resources.GIB))
            total, available = resources.constrain_memory(8 * resources.GIB, 6 * resources.GIB, child, root)
            self.assertEqual((total, available), (4 * resources.GIB, 2 * resources.GIB))
            self.assertEqual(resources.choose_budget(total, available, 2).memory, 2 * resources.GIB)

    def test_scope_inherits_user_environment_without_requesting_privilege(self):
        budget = resources.BuildBudget(3 * resources.GIB, 100)
        with mock.patch.object(resources.os, 'geteuid', return_value=1000, create=True):
            command = resources.scope_command(budget, '/source with spaces', '/build', 'test.scope', '/bin/systemd-run', '/entry')
        self.assertIn('--user', command)
        self.assertIn('--scope', command)
        self.assertIn('--no-ask-password', command)
        self.assertIn('--property=MemorySwapMax=0', command)
        self.assertIn('--property=KillMode=control-group', command)
        self.assertIn('--property=CPUQuota=100%', command)
        self.assertIn('/source with spaces', command)

    def _scope(self, tmp):
        root = Path(tmp) / 'cgroup'
        group = root / 'user.slice' / 'test.scope'
        group.mkdir(parents=True)
        membership = Path(tmp) / 'membership'
        membership.write_text('0::/user.slice/test.scope\n')
        for name, value in {
            'memory.max': '1000', 'memory.high': '750', 'memory.swap.max': '0',
            'cpu.max': '100000 100000', 'pids.max': '64',
        }.items():
            (group / name).write_text(value)
        return root, group, membership

    def test_real_kernel_values_must_match_before_compiler_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, group, membership = self._scope(tmp)
            self.assertEqual(resources.verify_scope(resources.BuildBudget(1000, 100), 'test.scope', root, membership), group)
            for filename, invalid in (
                ('memory.max', 'max'), ('memory.max', '1001'), ('memory.high', 'max'),
                ('memory.swap.max', '1'), ('cpu.max', 'max 100000'),
                ('cpu.max', '200000 100000'), ('pids.max', 'max'),
            ):
                with self.subTest(filename=filename, invalid=invalid):
                    path = group / filename
                    previous = path.read_text()
                    path.write_text(invalid)
                    with self.assertRaises(resources.BuildResourceError):
                        resources.verify_scope(resources.BuildBudget(1000, 100), 'test.scope', root, membership)
                    path.write_text(previous)

    def test_wrong_scope_or_missing_controller_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, group, membership = self._scope(tmp)
            with self.assertRaises(resources.BuildResourceError):
                resources.verify_scope(resources.BuildBudget(1000, 100), 'other.scope', root, membership)
            membership.write_text('1:memory:/legacy\n')
            with self.assertRaises(resources.BuildResourceError):
                resources.verify_scope(resources.BuildBudget(1000, 100), 'test.scope', root, membership)

    def test_failed_scope_verification_uses_compatible_limits_before_compiling(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {'fcntl': mock.Mock(LOCK_EX=2, LOCK_NB=4)}), mock.patch.object(
            resources.os, 'nice', create=True,
        ), mock.patch.object(resources, 'verify_scope', side_effect=resources.BuildResourceError('未隔离')), mock.patch.object(
            resources, 'apply_compatible_limits',
        ) as limits, mock.patch.object(resources.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            resources.execute_inside_scope(resources.BuildBudget(1000, 100), 'test.scope', tmp, tmp)
            limits.assert_called_once()
            self.assertEqual(run.call_count, 2)
            self.assertIn('pass_fds', run.call_args.kwargs)

    def test_missing_systemd_starts_compatible_build(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(resources.os, 'geteuid', return_value=1000, create=True), mock.patch.object(
            resources.shutil, 'which', return_value=None,
        ), mock.patch.object(resources, 'run_compatible_build') as run:
            resources.run_build(tmp, tmp, tmp, resources.BuildBudget(1000, 100))
            run.assert_called_once()

    def test_timeout_stops_whole_scope_and_keeps_log(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(resources.shutil, 'which', side_effect=lambda name: '/bin/' + name), mock.patch.object(
            resources.os, 'geteuid', return_value=0, create=True,
        ), mock.patch.object(resources.subprocess, 'run') as run, mock.patch.object(resources, 'run_compatible_build') as compatible:
            run.side_effect = [subprocess.TimeoutExpired('systemd-run', 1), subprocess.CompletedProcess([], 0)]
            with self.assertRaisesRegex(resources.BuildResourceError, '超过 3 小时'):
                resources.run_build(tmp, tmp, tmp, resources.BuildBudget(1000, 100))
            self.assertEqual(run.call_count, 2)
            self.assertIn('stop', run.call_args.args[0])
            self.assertTrue(list(Path(tmp).glob('*.log')))
            self.assertNotIn('capture_output', run.call_args_list[0].kwargs)
            compatible.assert_not_called()

    def test_unavailable_user_manager_falls_back_once_even_when_systemctl_cannot_connect(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(resources.shutil, 'which', side_effect=lambda name: '/bin/' + name), mock.patch.object(
            resources.os, 'geteuid', return_value=1000, create=True,
        ), mock.patch.object(resources.subprocess, 'run') as run, mock.patch.object(resources, 'run_compatible_build') as compatible:
            def fail(command, **kwargs):
                if command[0].endswith('systemd-run'):
                    kwargs['stdout'].write('控制器未委派'.encode())
                    return subprocess.CompletedProcess(command, 1)
                return subprocess.CompletedProcess(command, 1, stdout='')
            run.side_effect = fail
            path = resources.run_build(tmp, tmp, tmp, resources.BuildBudget(1000, 100))
            compatible.assert_called_once()
            self.assertIn('控制器未委派', path.read_text(encoding='utf-8'))

    def test_failure_after_helper_entered_does_not_retry_compilation(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(resources.shutil, 'which', side_effect=lambda name: '/bin/' + name), mock.patch.object(
            resources.os, 'geteuid', return_value=1000, create=True,
        ), mock.patch.object(resources.subprocess, 'run') as run, mock.patch.object(resources, 'run_compatible_build') as compatible:
            def fail(command, **kwargs):
                if command[0].endswith('systemd-run'):
                    Path(command[command.index('--entry-file') + 1]).write_text('已启动')
                    return subprocess.CompletedProcess(command, 1)
                return subprocess.CompletedProcess(command, 0)
            run.side_effect = fail
            with self.assertRaises(resources.BuildResourceError):
                resources.run_build(tmp, tmp, tmp, resources.BuildBudget(1000, 100))
            compatible.assert_not_called()

    def test_compatible_memory_limit_respects_existing_stricter_limit(self):
        module = mock.Mock(RLIMIT_AS=9, RLIM_INFINITY=-1)
        module.getrlimit.return_value = (2 * resources.GIB, -1)
        with mock.patch.dict(sys.modules, {'resource': module}), mock.patch.object(
            resources.os, 'sched_getaffinity', return_value={2, 3}, create=True,
        ), mock.patch.object(resources.os, 'sched_setaffinity', create=True) as affinity:
            resources.apply_compatible_limits(resources.BuildBudget(4 * resources.GIB, 100))
        module.setrlimit.assert_called_once_with(9, (2 * resources.GIB, 2 * resources.GIB))
        affinity.assert_called_once_with(0, {2})

    def test_compatible_timeout_kills_compiler_process_group(self):
        with tempfile.TemporaryFile() as log, mock.patch.object(resources.signal, 'SIGKILL', 9, create=True), mock.patch.object(resources.subprocess, 'Popen') as popen, mock.patch.object(
            resources.os, 'killpg', create=True,
        ) as kill:
            process = popen.return_value
            process.pid = 123
            process.wait.side_effect = [subprocess.TimeoutExpired('compile', 1), 0]
            with self.assertRaisesRegex(resources.BuildResourceError, '超过 3 小时'):
                resources.run_compatible_build(resources.BuildBudget(1000, 100), '/source', '/build', '/entry', log)
        kill.assert_called_once_with(123, 9)
        self.assertTrue(popen.call_args.kwargs['start_new_session'])

    def test_missing_cgroup_metadata_does_not_block_budget(self):
        def read(path, *args, **kwargs):
            if str(path).replace('\\', '/') == '/proc/meminfo':
                return 'MemTotal: 8388608 kB\nMemAvailable: 262144 kB\n'
            raise FileNotFoundError(str(path))
        with mock.patch.object(resources.sys, 'platform', 'linux'), mock.patch.object(Path, 'read_text', read):
            budget = resources.read_budget()
        self.assertEqual(budget.memory, 4 * resources.GIB)
        self.assertTrue(budget.warnings)


if __name__ == '__main__':
    unittest.main()
