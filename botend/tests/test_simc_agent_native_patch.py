"""Managed Agent patch delivery: real Git apply and tiny shell/CMake builds."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import time
from unittest.mock import patch

from django.test import SimpleTestCase

import simc_agent_consumer as agent


class SimcAgentNativePatchTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.app = self.root / 'application'
        self.patch_path = self.app / 'simc_patches/0063-luminescent-phoenixblade.patch'
        self.patch_path.parent.mkdir(parents=True)
        module = patch.object(agent, '__file__', str(self.app / 'simc_agent_consumer.py'))
        module.start()
        self.addCleanup(module.stop)
        self.source = self.root / 'upstream'
        self.source.mkdir()
        self.git('init', '-b', 'midnight')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Agent fixture')
        self.git('remote', 'add', 'origin', 'https://github.com/simulationcraft/simc.git')
        self.original = (
            '#!/bin/sh\n'
            "printf '%s\\n' 'SimulationCraft 1210-01 for World of Warcraft 12.1.0.69933 Live'\n"
            '# native: upstream\n'
        )
        (self.source / 'simc.in').write_text(self.original)
        (self.source / 'CMakeLists.txt').write_text(
            'cmake_minimum_required(VERSION 3.19)\n'
            'project(agent_fixture NONE)\n'
            'configure_file(simc.in ${CMAKE_BINARY_DIR}/simc COPYONLY)\n'
            'file(CHMOD ${CMAKE_BINARY_DIR}/simc PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE)\n'
            'add_custom_target(simc)\n'
        )
        self.git('add', '.')
        self.git('commit', '-m', 'fixture')
        self.revision = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('update-ref', 'refs/remotes/origin/midnight', self.revision)
        (self.source / 'simc.in').write_text(self.original.replace('native: upstream', 'native: phoenix-one'))
        self.patch_path.write_text(self.git('diff', '--', 'simc.in').stdout)
        (self.source / 'simc.in').write_text(self.original)
        # Unselected application patches must never be read or applied.
        (self.patch_path.parent / '0001-exporter.patch').write_text('not a patch\n')
        self.binary = self.root / 'active-simc'
        self.binary.write_text(self.original)
        self.binary.chmod(0o755)
        self.marker = Path(str(self.binary) + '.lmonitor-build.json')
        self.marker.write_text(json.dumps({'revision': self.revision, 'html_locale_patch_version': 1}))
        self.consumer = agent.SimcAgentConsumer(agent.AgentConfig.from_dict({
            'simc_path': str(self.binary), 'simc_source_path': str(self.source),
            'token_path': str(self.root / 'token'), 'auto_update_simc': False,
        }))
        command = self.consumer._command
        self.commands = []

        def local_command(argv, **kwargs):
            self.commands.append(argv)
            if argv[:3] == ['git', '-C', str(self.source)] and argv[3:4] == ['fetch']:
                return subprocess.CompletedProcess(argv, 0, '', '')
            return command(argv, **kwargs)

        boundary = patch.object(self.consumer, '_command', side_effect=local_command)
        self.command_mock = boundary.start()
        self.addCleanup(boundary.stop)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.source), *args],
                              check=True, capture_output=True, text=True)

    def assert_upstream_unchanged(self):
        self.assertEqual((self.source / 'simc.in').read_text(), self.original)
        self.assertEqual(self.git('status', '--porcelain').stdout, '')
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), self.revision)

    def test_copy_without_html_still_applies_only_managed_patch_and_reverse_checks(self):
        copy = self.root / 'candidate-source'
        digest = agent.prepare_simc_html_build_source(self.source, copy)
        self.assertIn('native: phoenix-one', (copy / 'simc.in').read_text())
        self.assertEqual(digest, hashlib.sha256(self.patch_path.read_bytes()).hexdigest())
        self.assert_upstream_unchanged()
        # The exact post-image is accepted as already applied, without replay.
        second = self.root / 'second-copy'
        self.assertEqual(agent.prepare_simc_html_build_source(copy, second), digest)
        self.assertEqual((second / 'simc.in').read_bytes(), (copy / 'simc.in').read_bytes())

    def test_same_sha_patch_change_rebuilds_and_reports_actual_binary_bound_digest(self):
        self.consumer._last_simc_check = time.monotonic()
        self.assertTrue(self.consumer._maintain_simc())
        first = self.consumer._report()
        first_digest = hashlib.sha256(self.patch_path.read_bytes()).hexdigest()
        self.assertEqual(first['capabilities']['native_patchset_sha256'], first_digest)
        self.assertEqual(json.loads(self.marker.read_text())['binary_sha256'],
                         hashlib.sha256(self.binary.read_bytes()).hexdigest())
        # Exercise both the periodic same-SHA skip and the disabled-upstream
        # update path: application patch changes still require rebuilding.
        self.consumer.config = replace(self.consumer.config, auto_update_simc=True)
        self.assertFalse(self.consumer._maintain_simc(force=True))
        self.consumer.config = replace(self.consumer.config, auto_update_simc=False)
        self.patch_path.write_text(self.patch_path.read_text().replace('phoenix-one', 'phoenix-two'))
        self.assertTrue(self.consumer._maintain_simc())
        second = self.consumer._report()
        self.assertEqual(first['current_version'], second['current_version'])
        self.assertNotEqual(first['capabilities']['binary_sha256'], second['capabilities']['binary_sha256'])
        self.assertEqual(second['capabilities']['native_patchset_sha256'],
                         hashlib.sha256(self.patch_path.read_bytes()).hexdigest())
        self.assertNotEqual(first_digest, second['capabilities']['native_patchset_sha256'])
        restarted = agent.SimcAgentConsumer(self.consumer.config)
        self.assertEqual(restarted._report()['capabilities']['native_patchset_sha256'],
                         second['capabilities']['native_patchset_sha256'])
        self.assert_upstream_unchanged()
        self.assertEqual(sum(argv[:2] == ['cmake', '--build'] for argv in self.commands), 2)
        # A stale marker cannot attribute its patch to a different installed file.
        self.binary.write_text(self.original)
        self.assertNotIn('native_patchset_sha256', self.consumer._report()['capabilities'])

    def test_missing_or_conflicting_patch_preserves_active_binary_and_marker(self):
        before = self.binary.read_bytes(), self.marker.read_bytes()
        contents = self.patch_path.read_text()
        for reason in ('missing', 'conflict'):
            with self.subTest(reason=reason):
                if reason == 'missing':
                    self.patch_path.unlink()
                else:
                    self.patch_path.write_text(contents.replace('-# native: upstream', '-# native: absent'))
                with self.assertRaisesRegex(agent.APIError, 'native patch'):
                    self.consumer._maintain_simc(force=True)
                self.assertEqual((self.binary.read_bytes(), self.marker.read_bytes()), before)
                self.assert_upstream_unchanged()
        self.assertFalse(any(argv[0] == 'cmake' for argv in self.commands))

    def test_patch_change_during_build_does_not_activate_or_mark(self):
        before = self.binary.read_bytes(), self.marker.read_bytes()
        command = self.command_mock.side_effect

        def change_patch(argv, **kwargs):
            result = command(argv, **kwargs)
            if argv[:2] == ['cmake', '--build']:
                self.patch_path.write_text(self.patch_path.read_text().replace('phoenix-one', 'phoenix-two'))
            return result

        self.command_mock.side_effect = change_patch
        with self.assertRaisesRegex(agent.APIError, 'native patch.*changed'):
            self.consumer._maintain_simc(force=True)
        self.assertEqual((self.binary.read_bytes(), self.marker.read_bytes()), before)
        self.assert_upstream_unchanged()

    def test_failed_compile_or_probe_never_records_patch_success(self):
        before = self.binary.read_bytes(), self.marker.read_bytes()
        command = self.command_mock.side_effect
        for stage in ('compile', 'probe'):
            with self.subTest(stage=stage):
                def fail(argv, **kwargs):
                    if stage == 'compile' and argv[:2] == ['cmake', '--build']:
                        raise agent.APIError('fixture compiler failure')
                    if stage == 'probe' and len(argv) == 1 and Path(argv[0]).name == 'simc':
                        return subprocess.CompletedProcess(argv, 0, 'not a simulator', '')
                    return command(argv, **kwargs)
                self.command_mock.side_effect = fail
                with self.assertRaises(agent.APIError):
                    self.consumer._maintain_simc(force=True)
                self.assertEqual((self.binary.read_bytes(), self.marker.read_bytes()), before)
                self.assertNotIn('native_patchset_sha256', self.consumer._report()['capabilities'])
        self.assert_upstream_unchanged()

    def test_live_lease_does_not_read_patch_or_build(self):
        self.patch_path.unlink()
        self.consumer._lease_block_until = float('inf')
        self.assertFalse(self.consumer._maintain_simc(force=True))
        self.assertEqual(self.commands, [])

    def test_copy_overrides_upstream_absolute_worktree_config(self):
        self.git('config', 'core.worktree', str(self.source))
        candidate = self.root / 'copy-with-absolute-worktree'
        agent.prepare_simc_html_build_source(self.source, candidate)
        self.assertIn('native: phoenix-one', (candidate / 'simc.in').read_text())
        self.assert_upstream_unchanged()

    def test_report_rejects_unbound_or_malformed_marker_but_keeps_legacy_fields(self):
        for native, binary_hash in [('a' * 64, 'b' * 64), ('bad', hashlib.sha256(self.binary.read_bytes()).hexdigest())]:
            with self.subTest(native=native):
                self.marker.write_text(json.dumps({
                    'revision': self.revision, 'html_locale_patch_version': 1,
                    'native_patchset_sha256': native, 'binary_sha256': binary_hash,
                }))
                report = self.consumer._report()
                self.assertEqual(report['current_version'], self.revision)
                self.assertEqual(report['html_locale_patch_version'], 1)
                self.assertNotIn('native_patchset_sha256', report['capabilities'])
