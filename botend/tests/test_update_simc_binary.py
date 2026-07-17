import os
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from botend.models import SimcBackendBinary, SimcContentTemplate


class UpdateSimcBinaryCommandTests(TestCase):
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
                        if cmd == ['git', 'rev-parse', '--short', 'HEAD']:
                            return mock.Mock(returncode=0, stdout='abc1234\n', stderr='')
                        if cmd[0] == 'cmake':
                            return mock.Mock(returncode=0, stdout='', stderr='')
                        if cmd[0] == 'ninja':
                            return mock.Mock(returncode=0, stdout='', stderr='')
                        if cmd == [str(fake_binary)]:
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

                        probe_calls = [call for call in mock_run.call_args_list if call[0][0] == [str(fake_binary)]]
                        self.assertGreater(len(probe_calls), 0,
                                          "Update verification must call _probe_binary() with no arguments")
                        for call in probe_calls:
                            self.assertNotIn('--help', call[0][0])
                            self.assertNotIn('--version', call[0][0])
