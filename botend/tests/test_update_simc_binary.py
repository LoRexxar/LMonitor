import os
import hashlib
import json
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from botend.models import (SimcApl, SimcAplSymbol, SimcBackendBinary, SimcContentTemplate,
                           WowSpellSnapshotState)


class UpdateSimcBinaryCommandTests(TestCase):
    @override_settings(SIMC_CONFIG={'wow_build': '12.0.1.70000'})
    def test_symbol_sync_failure_rolls_back_same_revision_apl_import(self):
        from django.core.management import call_command as real_call_command
        from botend.management.commands.update_simc_binary import Command

        old = SimcApl.objects.create(
            name='old', class_name='warrior', spec='warrior_fury', content='actions=/old',
            source='simc_upstream', is_system=True, sync_version='old-sha',
        )
        missing = SimcApl.objects.create(
            name='arms', class_name='warrior', spec='warrior_arms', content='actions=/arms',
            source='simc_upstream', is_system=True, sync_version='old-sha',
            is_active=True, is_selectable=True,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir)
            apl_dir = source / 'ActionPriorityLists' / 'default'
            apl_dir.mkdir(parents=True)
            (apl_dir / 'warrior_fury.simc').write_text('actions=/new\n', encoding='utf-8')
            command = Command()
            command.simc_source_dir = str(source)
            command.stdout = StringIO()

            def dispatch(name, **kwargs):
                if name == 'import_simc_apl':
                    return real_call_command(name, **kwargs)
                if name == 'sync_simc_apl_symbols':
                    raise CommandError('invalid symbol catalog')

            with mock.patch.object(command, '_get_git_hash', return_value='a' * 40), \
                    mock.patch.object(command, '_sync_default_template'), \
                    mock.patch.object(command, '_export_runtime_manifest', return_value='/tmp/test-runtime-manifest.json'), \
                    mock.patch('botend.management.commands.update_simc_binary.call_command',
                               side_effect=dispatch):
                with self.assertRaisesRegex(CommandError, 'invalid symbol catalog'):
                    command._sync_generated_inputs()
        old.refresh_from_db()
        self.assertEqual(old.content, 'actions=/old')
        self.assertEqual(old.sync_version, 'old-sha')
        missing.refresh_from_db()
        self.assertTrue(missing.is_active and missing.is_selectable)

    @override_settings(SIMC_CONFIG={'wow_build': '12.0.1.70000'})
    def test_generated_inputs_pass_same_git_sha_to_apl_and_symbol_sync(self):
        from botend.management.commands.update_simc_binary import Command

        command = Command()
        command.simc_source_dir = '/srv/simc'
        command.stdout = StringIO()
        with mock.patch.object(command, '_get_git_hash', return_value='b' * 40), \
                mock.patch.object(command, '_sync_default_template'), \
                mock.patch.object(command, '_export_runtime_manifest', return_value='/tmp/test-runtime-manifest.json'), \
                mock.patch.object(command, '_publish_system_apl_corpus'), \
                mock.patch('botend.management.commands.update_simc_binary.call_command') as calls:
            command._sync_generated_inputs()
        apl = next(call for call in calls.call_args_list if call.args[0] == 'import_simc_apl')
        symbols = next(call for call in calls.call_args_list
                       if call.args[0] == 'sync_simc_apl_symbols')
        self.assertEqual(apl.kwargs['sync_version'], 'b' * 40)
        self.assertTrue(apl.kwargs['strict'])
        self.assertEqual(symbols.kwargs['simc_revision'], 'b' * 40)
        self.assertEqual(symbols.kwargs['wow_build'], '12.0.1.70000')
        self.assertEqual(symbols.kwargs['runtime_manifest'], '/tmp/test-runtime-manifest.json')

    @override_settings(SIMC_CONFIG={})
    def test_missing_authoritative_wow_build_fails_before_writes(self):
        from botend.management.commands.update_simc_binary import Command
        command = Command()
        command.simc_source_dir = '/srv/simc'
        command.stdout = StringIO()
        with mock.patch.object(command, '_get_git_hash', return_value='c' * 40), \
                mock.patch.object(command, '_sync_default_template') as template, \
                mock.patch('botend.management.commands.update_simc_binary.call_command') as calls:
            with self.assertRaisesRegex(CommandError, 'wow_build'):
                command._sync_generated_inputs()
        template.assert_not_called()
        calls.assert_not_called()

    @override_settings(SIMC_CONFIG={})
    def test_unique_current_wago_snapshot_is_authoritative_build_fallback(self):
        from botend.management.commands.update_simc_binary import Command
        WowSpellSnapshotState.objects.create(branch='wow', locale='enUS',
                                             snapshot_build='12.0.1.70001')
        command = Command()
        command.simc_source_dir = '/srv/simc'
        command.stdout = StringIO()
        with mock.patch.object(command, '_get_git_hash', return_value='d' * 40), \
                mock.patch.object(command, '_sync_default_template'), \
                mock.patch.object(command, '_export_runtime_manifest', return_value='/tmp/test-runtime-manifest.json'), \
                mock.patch.object(command, '_publish_system_apl_corpus'), \
                mock.patch('botend.management.commands.update_simc_binary.call_command') as calls:
            command._sync_generated_inputs()
        symbols = next(c for c in calls.call_args_list if c.args[0] == 'sync_simc_apl_symbols')
        self.assertEqual(symbols.kwargs['wow_build'], '12.0.1.70001')

    @override_settings(SIMC_CONFIG={})
    def test_explicit_wow_build_override_wins(self):
        from botend.management.commands.update_simc_binary import Command
        command = Command()
        command.simc_source_dir = '/srv/simc'
        command.stdout = StringIO()
        with mock.patch.object(command, '_get_git_hash', return_value='e' * 40), \
                mock.patch.object(command, '_sync_default_template'), \
                mock.patch.object(command, '_export_runtime_manifest', return_value='/tmp/test-runtime-manifest.json'), \
                mock.patch.object(command, '_publish_system_apl_corpus'), \
                mock.patch('botend.management.commands.update_simc_binary.call_command') as calls:
            command._sync_generated_inputs(wow_build_override='12.0.1.70002')
        symbols = next(c for c in calls.call_args_list if c.args[0] == 'sync_simc_apl_symbols')
        self.assertEqual(symbols.kwargs['wow_build'], '12.0.1.70002')

    @override_settings(SIMC_CONFIG={'wow_build': '12.0.1.70000'})
    def test_invalid_git_sha_fails_before_any_database_write(self):
        from botend.management.commands.update_simc_binary import Command
        for sha in ('', 'abc123', 'z' * 40):
            with self.subTest(sha=sha):
                command = Command()
                command.simc_source_dir = '/srv/simc'
                command.stdout = StringIO()
                with mock.patch.object(command, '_get_git_hash', return_value=sha), \
                        mock.patch.object(command, '_sync_default_template') as template, \
                        mock.patch('botend.management.commands.update_simc_binary.call_command') as calls:
                    with self.assertRaisesRegex(CommandError, 'SHA'):
                        command._sync_generated_inputs()
                template.assert_not_called()
                calls.assert_not_called()
                self.assertEqual(SimcApl.objects.count(), 0)
                self.assertEqual(SimcAplSymbol.objects.count(), 0)

    @override_settings(SIMC_CONFIG={'wow_build': '12.0.1.70000'})
    def test_strict_apl_error_rolls_back_partial_import(self):
        from botend.management.commands.update_simc_binary import Command
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir)
            apl_dir = source / 'ActionPriorityLists' / 'default'
            apl_dir.mkdir(parents=True)
            (apl_dir / 'warrior_fury.simc').write_text('actions=/new\n', encoding='utf-8')
            (apl_dir / 'broken.simc').write_text('actions=/bad\n', encoding='utf-8')
            command = Command()
            command.simc_source_dir = str(source)
            command.stdout = StringIO()
            with mock.patch.object(command, '_get_git_hash', return_value='f' * 40), \
                    mock.patch.object(command, '_sync_default_template'), \
                    mock.patch.object(command, '_export_runtime_manifest', return_value='/tmp/test-runtime-manifest.json'), \
                    mock.patch('botend.management.commands.update_simc_binary.call_command',
                               wraps=call_command):
                with self.assertRaises(CommandError):
                    command._sync_generated_inputs()
        self.assertEqual(SimcApl.objects.count(), 0)

    def test_apply_patches_mode_builds_only_when_patch_changes_source(self):
        from botend.management.commands.update_simc_binary import Command

        command = Command()
        command.stdout = StringIO()
        command.platform = 'linux64'
        command.simc_source_dir = '/tmp/simc'
        command.simc_build_dir = '/tmp/simc/build-cli'
        command.simc_binary_path = '/tmp/simc/build-cli/simc'
        command.row = mock.Mock()

        with mock.patch.object(command, '_apply_local_patches', side_effect=[False, True]), \
                mock.patch.object(command, '_binary_needs_patch_rebuild', return_value=False), \
                mock.patch.object(command, '_update_binary') as update_binary:
            command._apply_patches_only(threads=4)
            update_binary.assert_not_called()

            command._apply_patches_only(threads=4)
            update_binary.assert_called_once_with(do_pull=False, threads=4, apply_patches=False)

    def test_apply_patches_mode_rebuilds_when_patch_is_present_but_binary_is_stale(self):
        from botend.management.commands.update_simc_binary import Command

        command = Command()
        command.stdout = StringIO()
        with mock.patch.object(command, '_apply_local_patches', return_value=False), \
                mock.patch.object(command, '_binary_needs_patch_rebuild', return_value=True), \
                mock.patch.object(command, '_update_binary') as update_binary:
            self.assertTrue(command._apply_patches_only(threads=2))
            update_binary.assert_called_once_with(do_pull=False, threads=2, apply_patches=False)

    def test_binary_health_requires_simulationcraft_identity(self):
        from botend.management.commands.update_simc_binary import Command

        with tempfile.TemporaryDirectory() as tmpdir:
            binary = Path(tmpdir) / 'simc'
            binary.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
            binary.chmod(0o755)
            command = Command()
            command.simc_binary_path = str(binary)
            with override_settings(SIMC_CONFIG={'simc_patch_dir': str(Path(tmpdir) / 'none')}):
                self.assertTrue(command._binary_needs_patch_rebuild())

    def test_local_simc_patch_is_applied_once_and_then_detected_as_present(self):
        from botend.management.commands.update_simc_binary import Command

        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / 'simc'
            patch_dir = Path(tmpdir) / 'patches'
            source_dir.mkdir()
            patch_dir.mkdir()
            target = source_dir / 'warrior.cpp'
            target.write_text('before\nbroken\nafter\n', encoding='utf-8')
            subprocess.run(['git', 'init', '-q'], cwd=source_dir, check=True)
            subprocess.run(['git', 'add', 'warrior.cpp'], cwd=source_dir, check=True)
            subprocess.run(
                ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                 'commit', '-qm', 'base'],
                cwd=source_dir,
                check=True,
            )
            (patch_dir / '0001-fix.patch').write_text(
                'diff --git a/warrior.cpp b/warrior.cpp\n'
                '--- a/warrior.cpp\n'
                '+++ b/warrior.cpp\n'
                '@@ -1,3 +1,3 @@\n'
                ' before\n'
                '-broken\n'
                '+fixed\n'
                ' after\n',
                encoding='utf-8',
            )
            command = Command()
            command.stdout = StringIO()
            command.simc_source_dir = str(source_dir)

            with override_settings(SIMC_CONFIG={'simc_patch_dir': str(patch_dir)}):
                self.assertTrue(command._apply_local_patches())
                self.assertEqual(target.read_text(encoding='utf-8'), 'before\nfixed\nafter\n')
                self.assertFalse(command._apply_local_patches())

    def test_patch_ledger_uses_unquoted_non_ascii_paths(self):
        from botend.management.commands.update_simc_binary import Command

        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / 'simc'
            patch_dir = Path(tmpdir) / 'patches'
            source_dir.mkdir()
            patch_dir.mkdir()
            target = source_dir / '技能.cpp'
            target.write_text('before\n', encoding='utf-8')
            subprocess.run(['git', 'init', '-q'], cwd=source_dir, check=True)
            subprocess.run(['git', 'add', '技能.cpp'], cwd=source_dir, check=True)
            subprocess.run(
                ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                 'commit', '-qm', 'base'],
                cwd=source_dir,
                check=True,
            )
            (patch_dir / '0001-unicode.patch').write_text(
                'diff --git a/技能.cpp b/技能.cpp\n'
                '--- a/技能.cpp\n'
                '+++ b/技能.cpp\n'
                '@@ -1 +1 @@\n'
                '-before\n'
                '+after\n',
                encoding='utf-8',
            )
            command = Command()
            command.stdout = StringIO()
            command.simc_source_dir = str(source_dir)

            with override_settings(SIMC_CONFIG={'simc_patch_dir': str(patch_dir)}):
                self.assertTrue(command._apply_local_patches())
                self.assertFalse(command._apply_local_patches())
            ledger = json.loads(
                (source_dir / '.git' / 'lmonitor-applied-patches.json').read_text(encoding='utf-8')
            )
            self.assertEqual(set(ledger['files']), {'技能.cpp'})

    def test_deletion_patch_is_rejected_before_source_mutation(self):
        from botend.management.commands.update_simc_binary import Command

        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / 'simc'
            patch_dir = Path(tmpdir) / 'patches'
            source_dir.mkdir()
            patch_dir.mkdir()
            target = source_dir / 'runtime.cpp'
            target.write_text('keep\n', encoding='utf-8')
            subprocess.run(['git', 'init', '-q'], cwd=source_dir, check=True)
            subprocess.run(['git', 'add', 'runtime.cpp'], cwd=source_dir, check=True)
            subprocess.run(
                ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                 'commit', '-qm', 'base'], cwd=source_dir, check=True,
            )
            (patch_dir / '0001-delete.patch').write_bytes(
                b'diff --git a/runtime.cpp b/runtime.cpp\r\n'
                b'deleted file mode 100644\r\n'
                b'--- a/runtime.cpp\t2026-07-23 00:00:00 +0000\r\n'
                b'+++ /dev/null\t2026-07-23 00:00:00 +0000\r\n'
                b'@@ -1 +0,0 @@\r\n'
                b'-keep\r\n'
            )
            command = Command()
            command.stdout = StringIO()
            command.row = mock.Mock()
            command.simc_source_dir = str(source_dir)
            with override_settings(SIMC_CONFIG={'simc_patch_dir': str(patch_dir)}):
                with self.assertRaises(CommandError):
                    command._apply_local_patches()
            self.assertEqual(target.read_text(encoding='utf-8'), 'keep\n')

    def test_incremental_local_patches_remain_idempotent_when_later_patch_changes_earlier_lines(self):
        from botend.management.commands.update_simc_binary import Command

        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / 'simc'
            patch_dir = Path(tmpdir) / 'patches'
            source_dir.mkdir()
            patch_dir.mkdir()
            target = source_dir / 'runtime.cpp'
            target.write_text('before\nbroken\nafter\n', encoding='utf-8')
            subprocess.run(['git', 'init', '-q'], cwd=source_dir, check=True)
            subprocess.run(['git', 'add', 'runtime.cpp'], cwd=source_dir, check=True)
            subprocess.run(
                ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                 'commit', '-qm', 'base'],
                cwd=source_dir,
                check=True,
            )
            (patch_dir / '0001-base.patch').write_text(
                'diff --git a/runtime.cpp b/runtime.cpp\n'
                '--- a/runtime.cpp\n'
                '+++ b/runtime.cpp\n'
                '@@ -1,3 +1,3 @@\n'
                ' before\n'
                '-broken\n'
                '+fixed\n'
                ' after\n',
                encoding='utf-8',
            )
            (patch_dir / '0002-incremental.patch').write_text(
                'diff --git a/runtime.cpp b/runtime.cpp\n'
                '--- a/runtime.cpp\n'
                '+++ b/runtime.cpp\n'
                '@@ -1,3 +1,3 @@\n'
                ' before\n'
                '-fixed\n'
                '+better\n'
                ' after\n',
                encoding='utf-8',
            )
            command = Command()
            command.stdout = StringIO()
            command.simc_source_dir = str(source_dir)
            command.row = mock.Mock()

            with override_settings(SIMC_CONFIG={'simc_patch_dir': str(patch_dir)}):
                ledger_path = source_dir / '.git' / 'lmonitor-applied-patches.json'
                ledger_path.write_text('{', encoding='utf-8')
                with self.assertRaises(CommandError):
                    command._apply_local_patches()
                self.assertEqual(target.read_text(encoding='utf-8'), 'before\nbroken\nafter\n')
                ledger_path.unlink()

                self.assertTrue(command._apply_local_patches())
                self.assertEqual(target.read_text(encoding='utf-8'), 'before\nbetter\nafter\n')
                self.assertFalse(command._apply_local_patches())

                ledger_path = source_dir / '.git' / 'lmonitor-applied-patches.json'
                ledger = json.loads(ledger_path.read_text(encoding='utf-8'))
                ledger['files'] = {}
                ledger_path.write_text(json.dumps(ledger), encoding='utf-8')
                subprocess.run(['git', 'checkout', '--', 'runtime.cpp'], cwd=source_dir, check=True)
                self.assertTrue(command._apply_local_patches())
                self.assertEqual(target.read_text(encoding='utf-8'), 'before\nbetter\nafter\n')
                self.assertFalse(command._apply_local_patches())

                target.write_text('unrelated source rewrite\n', encoding='utf-8')
                command.row = mock.Mock()
                with self.assertRaises(CommandError):
                    command._apply_local_patches()

                target.write_text('before\nbetter\nafter\n', encoding='utf-8')
                incremental_patch = patch_dir / '0002-incremental.patch'
                incremental_patch.write_text(
                    incremental_patch.read_text(encoding='utf-8').replace('+better', '+alternate'),
                    encoding='utf-8',
                )
                with self.assertRaises(CommandError):
                    command._apply_local_patches()

    def test_known_pre_ledger_patch_state_is_replayed_into_current_chain(self):
        from botend.management.commands.update_simc_binary import Command

        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / 'simc'
            patch_dir = Path(tmpdir) / 'patches'
            legacy_dir = Path(tmpdir) / 'legacy'
            source_dir.mkdir()
            patch_dir.mkdir()
            legacy_dir.mkdir()
            target = source_dir / 'runtime.cpp'
            target.write_text('base\n', encoding='utf-8')
            subprocess.run(['git', 'init', '-q'], cwd=source_dir, check=True)
            subprocess.run(['git', 'add', 'runtime.cpp'], cwd=source_dir, check=True)
            subprocess.run(
                ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                 'commit', '-qm', 'base'], cwd=source_dir, check=True,
            )
            revision = subprocess.run(
                ['git', 'rev-parse', 'HEAD'], cwd=source_dir, check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            current_patch = (
                'diff --git a/runtime.cpp b/runtime.cpp\n'
                '--- a/runtime.cpp\n+++ b/runtime.cpp\n'
                '@@ -1 +1 @@\n-base\n+current\n'
            )
            legacy_patch = current_patch.replace('+current', '+legacy')
            (patch_dir / '0001-current.patch').write_text(current_patch, encoding='utf-8')
            (legacy_dir / 'pre-ledger.patch').write_text(legacy_patch, encoding='utf-8')
            target.write_text('legacy\n', encoding='utf-8')
            (legacy_dir / 'pre-ledger.json').write_text(json.dumps({
                'base_revision': revision,
                'patch': 'pre-ledger.patch',
                'files': {'runtime.cpp': hashlib.sha256(b'legacy\n').hexdigest()},
            }), encoding='utf-8')

            command = Command()
            command.stdout = StringIO()
            command.row = mock.Mock()
            command.simc_source_dir = str(source_dir)
            with override_settings(SIMC_CONFIG={
                'simc_patch_dir': str(patch_dir),
                'simc_legacy_patch_dir': str(legacy_dir),
            }):
                self.assertTrue(command._apply_local_patches())
                self.assertEqual(target.read_text(encoding='utf-8'), 'current\n')
                self.assertFalse(command._apply_local_patches())

    def test_known_pre_ledger_patch_state_rejects_source_fingerprint_mismatch(self):
        from botend.management.commands.update_simc_binary import Command

        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / 'simc'
            patch_dir = Path(tmpdir) / 'patches'
            legacy_dir = Path(tmpdir) / 'legacy'
            source_dir.mkdir()
            patch_dir.mkdir()
            legacy_dir.mkdir()
            target = source_dir / 'runtime.cpp'
            target.write_text('top\nbase\nkeep-1\nkeep-2\nkeep-3\n', encoding='utf-8')
            subprocess.run(['git', 'init', '-q'], cwd=source_dir, check=True)
            subprocess.run(['git', 'add', 'runtime.cpp'], cwd=source_dir, check=True)
            subprocess.run(
                ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                 'commit', '-qm', 'base'], cwd=source_dir, check=True,
            )
            revision = subprocess.run(
                ['git', 'rev-parse', 'HEAD'], cwd=source_dir, check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            legacy_patch = (
                'diff --git a/runtime.cpp b/runtime.cpp\n'
                '--- a/runtime.cpp\n+++ b/runtime.cpp\n'
                '@@ -1,2 +1,2 @@\n top\n-base\n+legacy\n'
            )
            (patch_dir / '0001-current.patch').write_text(
                legacy_patch.replace('+legacy', '+current'), encoding='utf-8',
            )
            (legacy_dir / 'pre-ledger.patch').write_text(legacy_patch, encoding='utf-8')
            (legacy_dir / 'pre-ledger.json').write_text(json.dumps({
                'base_revision': revision,
                'patch': 'pre-ledger.patch',
                'files': {
                    'runtime.cpp': hashlib.sha256(
                        b'top\nlegacy\nkeep-1\nkeep-2\nkeep-3\n'
                    ).hexdigest(),
                },
            }), encoding='utf-8')
            target.write_text(
                'top\nlegacy\nkeep-1\nkeep-2\nunknown-outside-hunk\n',
                encoding='utf-8',
            )

            command = Command()
            command.stdout = StringIO()
            command.row = mock.Mock()
            command.simc_source_dir = str(source_dir)
            with override_settings(SIMC_CONFIG={
                'simc_patch_dir': str(patch_dir),
                'simc_legacy_patch_dir': str(legacy_dir),
            }):
                with self.assertRaises(CommandError):
                    command._apply_local_patches()
            self.assertEqual(
                target.read_text(encoding='utf-8'),
                'top\nlegacy\nkeep-1\nkeep-2\nunknown-outside-hunk\n',
            )

    def test_known_pre_ledger_patch_state_rolls_back_when_ledger_temp_create_fails(self):
        from botend.management.commands.update_simc_binary import Command

        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / 'simc'
            patch_dir = Path(tmpdir) / 'patches'
            legacy_dir = Path(tmpdir) / 'legacy'
            source_dir.mkdir()
            patch_dir.mkdir()
            legacy_dir.mkdir()
            target = source_dir / 'runtime.cpp'
            target.write_text('base\n', encoding='utf-8')
            subprocess.run(['git', 'init', '-q'], cwd=source_dir, check=True)
            subprocess.run(['git', 'add', 'runtime.cpp'], cwd=source_dir, check=True)
            subprocess.run(
                ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                 'commit', '-qm', 'base'], cwd=source_dir, check=True,
            )
            revision = subprocess.run(
                ['git', 'rev-parse', 'HEAD'], cwd=source_dir, check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            current_patch = (
                'diff --git a/runtime.cpp b/runtime.cpp\n'
                '--- a/runtime.cpp\n+++ b/runtime.cpp\n'
                '@@ -1 +1 @@\n-base\n+current\n'
            )
            legacy_patch = current_patch.replace('+current', '+legacy')
            (patch_dir / '0001-current.patch').write_text(current_patch, encoding='utf-8')
            (legacy_dir / 'pre-ledger.patch').write_text(legacy_patch, encoding='utf-8')
            target.write_text('legacy\n', encoding='utf-8')
            (legacy_dir / 'pre-ledger.json').write_text(json.dumps({
                'base_revision': revision,
                'patch': 'pre-ledger.patch',
                'files': {'runtime.cpp': hashlib.sha256(b'legacy\n').hexdigest()},
            }), encoding='utf-8')

            command = Command()
            command.stdout = StringIO()
            command.row = mock.Mock()
            command.simc_source_dir = str(source_dir)
            real_mkstemp = tempfile.mkstemp

            def fail_ledger_mkstemp(*args, **kwargs):
                if str(kwargs.get('prefix', '')).startswith('.lmonitor-applied-patches.'):
                    raise OSError('simulated ledger temp create failure')
                return real_mkstemp(*args, **kwargs)

            with override_settings(SIMC_CONFIG={
                'simc_patch_dir': str(patch_dir),
                'simc_legacy_patch_dir': str(legacy_dir),
            }), mock.patch(
                'botend.management.commands.update_simc_binary.tempfile.mkstemp',
                side_effect=fail_ledger_mkstemp,
            ):
                with self.assertRaises(OSError):
                    command._apply_local_patches()
            self.assertEqual(target.read_text(encoding='utf-8'), 'legacy\n')
            self.assertFalse((source_dir / '.git' / 'lmonitor-applied-patches.json').exists())

    def test_binary_probe_uses_no_arguments_not_help_or_version(self):
        """Binary probe must invoke [simc_binary_path] with no arguments, not --help/--version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_binary = Path(tmpdir) / "simc"
            fake_binary.write_text("#!/bin/sh\necho 'SimulationCraft 11.0.0'\n", encoding="utf-8")
            fake_binary.chmod(0o755)

            with override_settings(SIMC_CONFIG={'simc_path': str(fake_binary)}):
                with mock.patch('subprocess.run') as mock_run:
                    mock_run.return_value = mock.Mock(returncode=0, stdout='SimulationCraft 11.0.0\n', stderr='')
                    from botend.management.commands.update_simc_binary import Command
                    cmd = Command()
                    cmd._probe_binary(str(fake_binary))

                    mock_run.assert_called_once()
                    call_args = mock_run.call_args
                    self.assertEqual(call_args[0][0], [str(fake_binary)],
                                     "Binary probe must call [simc_binary_path] with no arguments")
                    self.assertNotIn('--help', call_args[0][0])
                    self.assertNotIn('--version', call_args[0][0])

    def test_binary_probe_returns_failed_process_for_contextual_status_handling(self):
        """Probe reports process failure; callers retain responsibility for persisting status."""
        from botend.management.commands.update_simc_binary import Command

        command = Command()
        with mock.patch('botend.management.commands.update_simc_binary.subprocess.run') as run:
            run.return_value = mock.Mock(returncode=60, stdout='SimulationCraft 12.0\n', stderr='bad option')
            result, output = command._probe_binary('/srv/simc')

        self.assertEqual(result.returncode, 60)
        self.assertIn('SimulationCraft 12.0', output)
        self.assertIn('bad option', output)

    def test_check_truncates_missing_binary_error_to_database_field_limit(self):
        long_missing_path = '/tmp/' + ('missing-' * 70) + 'simc'
        with override_settings(SIMC_CONFIG={'simc_path': long_missing_path}):
            call_command('update_simc_binary', '--check', stdout=StringIO())

        row = SimcBackendBinary.objects.get(platform='linux64')
        self.assertEqual(row.update_status, '二进制不存在')
        self.assertLessEqual(len(row.last_error), 500)
        self.assertLessEqual(len(row.simc_path), 500)

    def test_check_persists_probe_timeout_instead_of_leaving_stale_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_binary = Path(tmpdir) / 'simc'
            fake_binary.touch()
            with override_settings(SIMC_CONFIG={'simc_path': str(fake_binary)}), \
                 mock.patch(
                     'botend.management.commands.update_simc_binary.Command._probe_binary',
                     side_effect=subprocess.TimeoutExpired([str(fake_binary)], 10),
                 ):
                call_command('update_simc_binary', '--check', stdout=StringIO())

        row = SimcBackendBinary.objects.get(platform='linux64')
        self.assertFalse(row.is_updating)
        self.assertEqual(row.update_status, '二进制验证超时')
        self.assertIn('超时', row.last_error)
        self.assertIsNotNone(row.last_checked_at)

    def test_check_truncates_long_probe_error_to_database_field_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_binary = Path(tmpdir) / 'simc'
            fake_binary.touch()
            failed = mock.Mock(returncode=60, stdout='x' * 800, stderr='')
            with override_settings(SIMC_CONFIG={'simc_path': str(fake_binary)}), \
                 mock.patch(
                     'botend.management.commands.update_simc_binary.Command._probe_binary',
                     return_value=(failed, failed.stdout),
                 ):
                call_command('update_simc_binary', '--check', stdout=StringIO())

        row = SimcBackendBinary.objects.get(platform='linux64')
        self.assertEqual(row.update_status, '二进制验证失败')
        self.assertLessEqual(len(row.last_error), 500)

    def test_sync_default_template_creates_and_updates_selectable_template(self):
        from botend.management.commands.update_simc_binary import Command

        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / 'simc_template.txt'
            template_path.write_text('iterations=1000\n', encoding='utf-8')
            command = Command()
            command.stdout = StringIO()

            with override_settings(SIMC_CONFIG={'simc_template': str(template_path)}):
                command._sync_default_template()
                template = SimcContentTemplate.objects.get(
                    template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
                    source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
                    spec='default',
                    name='基础模板 default',
                )
                self.assertTrue(template.is_selectable)

                template.is_selectable = False
                template.save(update_fields=['is_selectable'])
                template_path.write_text('iterations=2000\n', encoding='utf-8')
                command._sync_default_template()

            template.refresh_from_db()
            self.assertTrue(template.is_selectable)
            self.assertEqual(template.content, 'iterations=2000\n')

    def test_sync_default_template_deactivates_legacy_upstream_spec_templates(self):
        from botend.management.commands.update_simc_binary import Command

        legacy = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='fury',
            name='基础模板 fury',
            content='warrior="Legacy"\nlevel=80\n{player_config}\n{action_list}',
            is_active=True,
            is_selectable=True,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / 'simc_template.txt'
            template_path.write_text(
                '{simulation_options}\n{player_identity}\n{action_list}\n',
                encoding='utf-8',
            )
            command = Command()
            command.stdout = StringIO()
            with override_settings(SIMC_CONFIG={'simc_template': str(template_path)}):
                command._sync_default_template()

        legacy.refresh_from_db()
        canonical = SimcContentTemplate.objects.get(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='default',
            name='基础模板 default',
        )
        self.assertFalse(legacy.is_active)
        self.assertFalse(legacy.is_selectable)
        self.assertTrue(canonical.is_active)
        self.assertTrue(canonical.is_selectable)

    def test_update_success_reuses_safe_probe_not_help(self):
        """After successful update, verification must reuse _probe_binary() not --help."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_source = Path(tmpdir) / "simc_source"
            fake_source.mkdir()
            (fake_source / ".git").mkdir()
            fake_build = fake_source / "build-cli"
            fake_build.mkdir()
            fake_binary = fake_build / "simc"
            fake_binary.write_text("#!/bin/sh\necho 'SimulationCraft 11.0.0'\n", encoding="utf-8")
            fake_binary.chmod(0o755)

            cmake_content = "project(SimulationCraft)\nset(SC_MAJOR_VERSION 11)\nset(SC_MINOR_VERSION 0)\n"
            (fake_source / "CMakeLists.txt").write_text(cmake_content, encoding="utf-8")

            with override_settings(SIMC_CONFIG={
                'simc_source_dir': str(fake_source),
                'simc_build_dir': str(fake_build),
                'simc_path': str(fake_binary),
            }):
                with mock.patch('subprocess.run') as mock_run:
                    def run_side_effect(cmd, **kwargs):
                        if cmd == ['git', 'status', '--porcelain', '--untracked-files=no']:
                            return mock.Mock(returncode=0, stdout='', stderr='')
                        if cmd == ['git', 'pull', '--rebase']:
                            return mock.Mock(returncode=0, stdout='Already up to date.', stderr='')
                        if cmd == ['git', 'rev-parse', 'HEAD']:
                            return mock.Mock(returncode=0, stdout=('a' * 40) + '\n', stderr='')
                        if cmd[0] == 'cmake':
                            return mock.Mock(returncode=0, stdout='', stderr='')
                        if cmd[0] == 'ninja':
                            return mock.Mock(returncode=0, stdout='', stderr='')
                        if len(cmd) == 1 and Path(cmd[0]).name == 'simc':
                            return mock.Mock(returncode=0, stdout='SimulationCraft 11.0.0\n', stderr='')
                        return mock.Mock(returncode=0, stdout='', stderr='')

                    mock_run.side_effect = run_side_effect

                    with mock.patch('botend.management.commands.update_simc_binary.Command._sync_generated_inputs'):
                        out = StringIO()
                        try:
                            call_command('update_simc_binary', stdout=out)
                        except AttributeError as e:
                            if '_probe_binary' in str(e):
                                self.fail("Command._probe_binary() does not exist yet (expected RED phase failure)")

                        probe_calls = [call for call in mock_run.call_args_list
                                       if len(call[0][0]) == 1 and Path(call[0][0][0]).name == 'simc']
                        self.assertGreater(len(probe_calls), 0,
                                          "Update verification must call _probe_binary() with no arguments")
                        for call in probe_calls:
                            self.assertNotIn('--help', call[0][0])
                            self.assertNotIn('--version', call[0][0])
