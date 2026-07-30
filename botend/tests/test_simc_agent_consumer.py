import hashlib
import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase


class SimcAgentConsumerTests(SimpleTestCase):
    def write_token(self, values, token):
        token_path = Path(values['token_path'])
        token_path.write_text(token, encoding='ascii')
        token_path.chmod(0o600)

    def config(self, root):
        simc = Path(root) / 'simc'
        simc.write_text('#!/bin/sh\n', encoding='utf-8')
        simc.chmod(0o755)
        return {
            'server_url': 'https://control.example/',
            'backend_identifier': 'midnight-linux64',
            'simc_path': str(simc),
            'token_path': str(Path(root) / 'agent.token'),
            'enrollment_token': 'enroll-secret',
            'name': 'worker-a',
            'platform': 'linux64',
            'poll_interval_seconds': 2,
            'request_timeout_seconds': 10,
        }

    def test_config_is_independent_and_rejects_unknown_or_insecure_values(self):
        from simc_agent_consumer import AgentConfig, ConfigError

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            config = AgentConfig.from_dict(values)
            self.assertEqual(config.server_url, 'https://control.example')
            self.assertEqual(config.backend_identifier, 'midnight-linux64')
            self.assertTrue(config.host_identifier)
            with self.assertRaises(ConfigError):
                AgentConfig.from_dict({**values, 'unknown': True})
            with self.assertRaises(ConfigError):
                AgentConfig.from_dict({**values, 'server_url': 'http://control.example'})

    def test_minimal_config_only_requires_enrollment_token_and_simc_path(self):
        from simc_agent_consumer import AgentConfig

        with tempfile.TemporaryDirectory() as root:
            simc = Path(root) / 'simc'
            simc.write_text('#!/bin/sh\n', encoding='utf-8')
            simc.chmod(0o755)
            config_path = Path(root) / 'agent.json'
            config_path.write_text(json.dumps({
                'enrollment_token': 'enroll-secret',
                'simc_path': str(simc),
            }), encoding='utf-8')

            config = AgentConfig.load(str(config_path))

            self.assertEqual(config.server_url, 'https://wowdaily.cn')
            self.assertEqual(config.simc_path, str(simc))
            self.assertEqual(config.token_path, str(Path(root) / 'agent.token'))
            self.assertEqual(config.simc_source_path, str(Path(root) / 'simc-source'))
            self.assertEqual(config.enrollment_token, 'enroll-secret')
            self.assertEqual(config.backend_identifier, '')

    def test_loading_existing_minimal_config_persists_all_example_defaults(self):
        from simc_agent_consumer import AgentConfig

        with tempfile.TemporaryDirectory() as root:
            simc = Path(root) / 'simc'
            simc.write_text('#!/bin/sh\n', encoding='utf-8')
            simc.chmod(0o755)
            config_path = Path(root) / 'simc_agent.json'
            config_path.write_text(json.dumps({
                'enrollment_token': 'enroll-secret',
                'simc_path': str(simc),
            }), encoding='utf-8')

            config = AgentConfig.load(str(config_path))
            persisted = json.loads(config_path.read_text(encoding='utf-8'))

            self.assertEqual(set(persisted), set(AgentConfig.__dataclass_fields__))
            self.assertEqual(persisted['server_url'], 'https://wowdaily.cn')
            self.assertEqual(persisted['max_concurrent_runs'], 1)
            self.assertEqual(persisted['token_path'], config.token_path)
            self.assertEqual(persisted['log_path'], config.log_path)
            self.assertEqual(persisted['simc_source_path'], config.simc_source_path)

    def test_agent_runtime_state_paths_are_ignored_when_config_lives_in_checkout(self):
        repository_ignore = (Path(__file__).resolve().parents[2] / '.gitignore').read_text(encoding='utf-8')

        for pattern in (
            '/agent.json', '/agent.token', '/simc-agent.log', '/completion-outbox/',
            '/simc-source/', '*.lmonitor-build.json',
        ):
            self.assertIn(pattern, repository_ignore)

    def test_minimal_config_derives_a_nonblank_bounded_agent_name(self):
        from simc_agent_consumer import AgentConfig

        with tempfile.TemporaryDirectory() as root:
            simc = Path(root) / 'simc'
            simc.write_text('#!/bin/sh\n', encoding='utf-8')
            simc.chmod(0o755)

            config = AgentConfig.from_dict({'simc_path': str(simc)})

            self.assertRegex(config.name, r'^simc-agent-[0-9a-f]{12}$')

    def test_config_load_discovers_git_source_above_build_output_before_managed_fallback(self):
        from simc_agent_consumer import AgentConfig

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source = root_path / 'simulationcraft'
            (source / '.git').mkdir(parents=True)
            binary = source / 'build' / 'simc'
            binary.parent.mkdir()
            binary.write_text('#!/bin/sh\n', encoding='utf-8')
            binary.chmod(0o755)
            config_path = root_path / 'agent-state' / 'agent.json'
            config_path.parent.mkdir()
            config_path.write_text(json.dumps({
                'enrollment_token': 'enroll-secret',
                'simc_path': str(binary),
            }), encoding='utf-8')

            config = AgentConfig.load(str(config_path))

            self.assertEqual(config.simc_source_path, str(source))

    def test_simc_path_must_be_an_explicit_executable_file_not_a_directory(self):
        from simc_agent_consumer import AgentConfig, ConfigError

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            values['simc_path'] = root
            with self.assertRaisesRegex(ConfigError, 'executable SimC binary file'):
                AgentConfig.from_dict(values)

    def test_windows_config_accepts_regular_simc_exe_without_posix_execute_mode(self):
        from simc_agent_consumer import AgentConfig

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            binary = Path(root) / 'simc.exe'
            binary.write_bytes(b'MZ')
            binary.chmod(0o644)
            values['simc_path'] = str(binary)

            with patch('simc_agent_consumer._is_windows', return_value=True):
                config = AgentConfig.from_dict(values)

            self.assertEqual(config.simc_path, str(binary))

    def test_windows_default_token_path_uses_local_app_data(self):
        from simc_agent_consumer import AgentConfig

        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'LOCALAPPDATA': root}):
            simc = Path(root) / 'simc.exe'
            simc.write_bytes(b'MZ')
            with patch('simc_agent_consumer._is_windows', return_value=True):
                config = AgentConfig.from_dict({'simc_path': str(simc)})

            self.assertEqual(config.token_path, str(Path(root) / 'LMonitorSimCAgent' / 'agent.token'))

    def test_missing_binary_is_allowed_when_source_path_can_build_it(self):
        from simc_agent_consumer import AgentConfig

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            values.update({
                'simc_path': str(Path(root) / 'build' / 'simc'),
                'simc_source_path': str(Path(root) / 'source'),
            })
            config = AgentConfig.from_dict(values)
            self.assertEqual(config.simc_path, values['simc_path'])
            self.assertEqual(config.simc_source_path, values['simc_source_path'])

    def test_config_allows_explicit_agent_run_capacity_and_reports_it(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            values['max_concurrent_runs'] = 2

            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())

            self.assertEqual(consumer._report()['capabilities']['max_concurrent_runs'], 2)

    def test_maintenance_failure_keeps_claiming_when_the_existing_binary_is_usable(self):
        from simc_agent_consumer import APIError, AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())
            consumer.register = MagicMock()
            consumer.flush_completion_outbox = MagicMock(return_value=True)
            consumer._maintain_simc_with_heartbeats = MagicMock(
                side_effect=APIError('compiler failed'),
            )
            consumer.heartbeat = MagicMock()
            consumer.claim = MagicMock(return_value=None)

            consumer.run(once=True)

            consumer.claim.assert_called_once_with()
            consumer.heartbeat.assert_any_call('degraded')

    def test_example_configuration_documents_every_supported_field(self):
        from simc_agent_consumer import AgentConfig

        example_path = Path(__file__).resolve().parents[2] / 'simc_agent.example.json'
        values = json.loads(example_path.read_text(encoding='utf-8'))

        self.assertEqual(set(values), set(AgentConfig.__dataclass_fields__))

    def test_first_start_interactively_creates_minimal_configuration(self):
        from simc_agent_consumer import ensure_configuration

        with tempfile.TemporaryDirectory() as root:
            config_path = Path(root) / 'simc_agent.json'
            with patch('builtins.input', side_effect=[
                '  enroll-secret  ',
                '  /opt/simc/bin/simc  ',
            ]):
                created = ensure_configuration(str(config_path), interactive=True)

            self.assertTrue(created)
            self.assertEqual(json.loads(config_path.read_text(encoding='utf-8')), {
                'enrollment_token': 'enroll-secret',
                'simc_path': '/opt/simc/bin/simc',
            })
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)

    def test_first_start_requires_both_interactive_values_without_writing_partial_config(self):
        from simc_agent_consumer import ConfigError, ensure_configuration

        with tempfile.TemporaryDirectory() as root:
            config_path = Path(root) / 'simc_agent.json'
            with patch('builtins.input', side_effect=['', '/opt/simc/bin/simc']):
                with self.assertRaisesRegex(ConfigError, 'enrollment token'):
                    ensure_configuration(str(config_path), interactive=True)

            self.assertFalse(config_path.exists())

    def test_main_uses_a_local_default_configuration_path(self):
        import simc_agent_consumer

        with patch.object(simc_agent_consumer, 'ensure_configuration') as ensure, \
                patch.object(simc_agent_consumer, 'AgentConfig') as config_class, \
                patch.object(simc_agent_consumer, 'SimcAgentConsumer') as consumer_class, \
                patch.object(simc_agent_consumer.signal, 'signal'), \
                patch('sys.argv', ['simc_agent_consumer.py', '--once']):
            config_class.load.return_value = MagicMock(log_path='', token_path='/tmp/agent.token')
            consumer_class.return_value.run = MagicMock()

            result = simc_agent_consumer.main()

        self.assertEqual(result, 0)
        config_class.load.assert_called_once_with(
            str(Path(simc_agent_consumer.__file__).resolve().with_name('simc_agent.json')),
        )
        ensure.assert_called_once_with(
            str(Path(simc_agent_consumer.__file__).resolve().with_name('simc_agent.json')),
            interactive=False,
        )

    def test_run_claims_and_executes_to_explicit_capacity_in_parallel(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            values['max_concurrent_runs'] = 2
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())
            consumer.register = MagicMock()
            consumer.flush_completion_outbox = MagicMock(return_value=True)
            consumer._maintain_simc_with_heartbeats = MagicMock()
            consumer.heartbeat = MagicMock()
            first = {'task_id': 1, 'run_id': 1}
            second = {'task_id': 1, 'run_id': 2}
            consumer.claim = MagicMock(side_effect=[first, second])

            def execute(job):
                if job['run_id'] == 2:
                    consumer.stop_event.set()

            consumer.execute_job = MagicMock(side_effect=execute)
            consumer.run()

            self.assertEqual(consumer.claim.call_count, 2)
            self.assertCountEqual(
                [entry.args[0] for entry in consumer.execute_job.call_args_list], [first, second],
            )

    def test_once_claims_only_one_run_even_when_capacity_is_higher(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            values['max_concurrent_runs'] = 2
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())
            consumer.register = MagicMock()
            consumer.flush_completion_outbox = MagicMock(return_value=True)
            consumer._maintain_simc_with_heartbeats = MagicMock()
            consumer.heartbeat = MagicMock()
            first = {'task_id': 1, 'run_id': 1}
            second = {'task_id': 1, 'run_id': 2}
            consumer.claim = MagicMock(side_effect=[first, second])
            consumer.execute_job = MagicMock()

            consumer.run(once=True)

            self.assertEqual(consumer.claim.call_count, 1)
            self.assertEqual(
                [entry.args[0] for entry in consumer.execute_job.call_args_list], [first],
            )

    def test_report_contains_real_simc_revision_and_binary_availability(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            marker = Path(values['simc_path'] + '.lmonitor-build.json')
            marker.write_text(json.dumps({'revision': 'abc123def456'}), encoding='utf-8')
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())

            report = consumer._report()

            self.assertTrue(report['binary_available'])
            self.assertEqual(report['current_version'], 'abc123def456')
            Path(values['simc_path']).unlink()
            self.assertFalse(consumer._report()['binary_available'])

    def test_idle_maintenance_rebuilds_when_existing_binary_lacks_html_locale_patch(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'simc-source'
            source.mkdir()
            (source / '.git').mkdir()
            report = source / 'engine' / 'report' / 'report_html_sim.cpp'
            report.parent.mkdir(parents=True)
            report.write_text(
                '  catch ( const std::runtime_error& )\n'
                '  {\n'
                '    // backup spelling for CI\n'
                '    std::locale::global( std::locale( "en_US.utf8" ) );\n'
                '  }\n', encoding='utf-8',
            )
            values = self.config(root)
            values.update({
                'simc_source_path': str(source),
                'auto_update_simc': False,
            })
            revision = 'a' * 40
            Path(values['simc_path'] + '.lmonitor-build.json').write_text(
                json.dumps({'revision': revision}), encoding='utf-8',
            )
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())

            def command_result(command, **_kwargs):
                stdout = ''
                if command[-3:] == ['config', '--get', 'remote.origin.url']:
                    stdout = 'https://github.com/simulationcraft/simc.git\n'
                elif command[-2:] == ['branch', '--show-current']:
                    stdout = 'midnight\n'
                elif command[-2:] == ['rev-parse', 'HEAD']:
                    stdout = revision + '\n'
                elif command[-2:] == ['rev-parse', 'origin/midnight']:
                    stdout = revision + '\n'
                elif command[:2] == ['cmake', '--build']:
                    candidate = Path(command[2]) / 'simc'
                    candidate.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
                    candidate.chmod(0o755)
                elif command[0].endswith('/simc') and len(command) == 1:
                    stdout = 'SimulationCraft 1200-01\n'
                return MagicMock(returncode=0, stdout=stdout, stderr='')

            with patch('simc_agent_consumer.subprocess.run', side_effect=command_result) as run_command:
                changed = consumer._maintain_simc(force=True)

            self.assertTrue(changed)
            self.assertTrue(any(
                call.args[0][:2] == ['cmake', '--build']
                for call in run_command.call_args_list
            ))

    def test_idle_maintenance_pulls_compiles_verifies_and_atomically_replaces_simc(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'simc-source'
            source.mkdir()
            (source / '.git').mkdir()
            values = self.config(root)
            values.update({
                'simc_source_path': str(source),
                'simc_update_interval_seconds': 60,
            })
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())

            def command_result(command, **_kwargs):
                stdout = ''
                if command[-3:] == ['config', '--get', 'remote.origin.url']:
                    stdout = 'https://github.com/simulationcraft/simc.git\n'
                elif command[-2:] == ['branch', '--show-current']:
                    stdout = 'midnight\n'
                elif command[-2:] == ['rev-parse', 'HEAD']:
                    stdout = ('a' * 40) + '\n' if not any(
                        call.args[0][-5:] == ['pull', '--ff-only', '--force', 'origin', 'midnight']
                        for call in run_command.call_args_list
                    ) else ('b' * 40) + '\n'
                elif command[-2:] == ['rev-parse', 'origin/midnight']:
                    stdout = ('b' * 40) + '\n'
                elif command[:2] == ['cmake', '--build']:
                    build_dir = Path(command[2])
                    candidate = build_dir / 'simc'
                    candidate.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
                    candidate.chmod(0o755)
                elif command[0].endswith('/simc') and len(command) == 1:
                    stdout = 'SimulationCraft 1200-01\n'
                return MagicMock(returncode=0, stdout=stdout, stderr='')

            with patch('simc_agent_consumer.subprocess.run', side_effect=command_result) as run_command:
                changed = consumer._maintain_simc(force=True)

            self.assertTrue(changed)
            self.assertEqual(
                json.loads(Path(values['simc_path'] + '.lmonitor-build.json').read_text(encoding='utf-8')),
                {
                    'revision': 'b' * 40,
                    'html_locale_patch_version': 1,
                },
            )
            self.assertTrue(os.access(values['simc_path'], os.X_OK))
            commands = [call.args[0] for call in run_command.call_args_list]
            self.assertIn(
                ['git', '-C', str(source), 'pull', '--ff-only', '--force', 'origin', 'midnight'], commands,
            )
            self.assertFalse(any('reset' in command or 'clean' in command for command in commands))
            self.assertTrue(any(command[:2] == ['cmake', '--build'] for command in commands))
            self.assertTrue(any(command[0].endswith('/simc') and len(command) == 1 for command in commands))
            self.assertFalse(any('--version' in command for command in commands))

    def test_html_build_source_uses_classic_locale_when_en_us_is_unavailable(self):
        from simc_agent_consumer import prepare_simc_html_build_source

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'simc-source'
            report = source / 'engine' / 'report' / 'report_html_sim.cpp'
            report.parent.mkdir(parents=True)
            report.write_text(
                'try { std::locale::global( std::locale( "en_US.UTF-8" ) ); }\n'
                '  catch ( const std::runtime_error& )\n'
                '  {\n'
                '    // backup spelling for CI\n'
                '    std::locale::global( std::locale( "en_US.utf8" ) );\n'
                '  }\n', encoding='utf-8',
            )
            build_source = Path(root) / 'build-source'

            prepare_simc_html_build_source(source, build_source)

            rendered = (build_source / 'engine' / 'report' / 'report_html_sim.cpp').read_text(encoding='utf-8')
            self.assertIn('std::locale::classic()', rendered)
            self.assertEqual(
                rendered.count('catch ( const std::runtime_error& )'), 2,
            )
            self.assertNotIn('std::locale::classic()', report.read_text(encoding='utf-8'))

    def test_windows_maintenance_uses_simc_exe_and_skips_posix_mode_changes(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'simc-source'
            source.mkdir()
            (source / '.git').mkdir()
            values = self.config(root)
            values['simc_source_path'] = str(source)
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())

            def command_result(command, **_kwargs):
                stdout = ''
                if command[-3:] == ['config', '--get', 'remote.origin.url']:
                    stdout = 'https://github.com/simulationcraft/simc.git\n'
                elif command[-2:] == ['branch', '--show-current']:
                    stdout = 'midnight\n'
                elif command[-2:] == ['rev-parse', 'HEAD']:
                    stdout = ('a' * 40) + '\n' if not any(
                        call.args[0][-5:] == ['pull', '--ff-only', '--force', 'origin', 'midnight']
                        for call in run_command.call_args_list
                    ) else ('b' * 40) + '\n'
                elif command[-2:] == ['rev-parse', 'origin/midnight']:
                    stdout = ('b' * 40) + '\n'
                elif command[:2] == ['cmake', '--build']:
                    candidate = Path(command[2]) / 'simc.exe'
                    candidate.write_bytes(b'MZ')
                elif command[0].endswith('simc.exe') and len(command) == 1:
                    stdout = 'SimulationCraft 1200-01\n'
                return MagicMock(returncode=0, stdout=stdout, stderr='')

            with patch('simc_agent_consumer._is_windows', return_value=True), \
                    patch('simc_agent_consumer.os.chmod') as chmod, \
                    patch('simc_agent_consumer.subprocess.run', side_effect=command_result) as run_command:
                changed = consumer._maintain_simc(force=True)

            self.assertTrue(changed)
            self.assertTrue(Path(values['simc_path']).is_file())
            # ``copytree`` may preserve source-directory metadata through the
            # platform module, but Agent activation itself must not chmod the
            # Windows binary's temporary replacement file.
            self.assertFalse(any(
                Path(call.args[0]).name.startswith('.simc.')
                for call in chmod.call_args_list
            ))
            commands = [call.args[0] for call in run_command.call_args_list]
            self.assertTrue(any(command[:2] == ['cmake', '--build'] for command in commands))
            self.assertTrue(any(command[0].endswith('simc.exe') and len(command) == 1 for command in commands))
            self.assertFalse(any('--version' in command for command in commands))

    def test_required_simc_revision_clones_managed_source_and_builds_exact_commit(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'simc-source'
            values = self.config(root)
            Path(values['simc_path']).unlink()
            values['simc_source_path'] = str(source)
            values['auto_update_simc'] = False
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())
            required_revision = 'b' * 40
            upstream_revision = 'c' * 40
            local_revision = 'a' * 40

            def command_result(command, **_kwargs):
                stdout = ''
                if command[:2] == ['git', 'clone']:
                    (source / '.git').mkdir(parents=True)
                elif command[-3:] == ['config', '--get', 'remote.origin.url']:
                    stdout = 'https://github.com/simulationcraft/simc.git\n'
                elif command[-2:] == ['branch', '--show-current']:
                    stdout = 'midnight\n'
                elif command[-2:] == ['rev-parse', 'HEAD']:
                    pull_done = any(
                        call.args[0][-5:] == ['pull', '--ff-only', '--force', 'origin', 'midnight']
                        for call in run_command.call_args_list
                    )
                    stdout = (required_revision if pull_done else local_revision) + '\n'
                elif command[-2:] == ['rev-parse', 'origin/midnight']:
                    stdout = upstream_revision + '\n'
                elif command[-2:] == ['status', '--porcelain']:
                    stdout = ' M engine/generated.cpp\n'
                elif command[:2] == ['cmake', '--build']:
                    candidate = Path(command[2]) / 'simc'
                    candidate.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
                    candidate.chmod(0o755)
                elif command[0].endswith('/simc') and len(command) == 1:
                    stdout = 'SimulationCraft 1200-01\n'
                return MagicMock(returncode=0, stdout=stdout, stderr='')

            with patch('simc_agent_consumer.subprocess.run', side_effect=command_result) as run_command:
                changed = consumer._maintain_simc(
                    force=True, required_revision=required_revision,
                )

            self.assertTrue(changed)
            commands = [call.args[0] for call in run_command.call_args_list]
            self.assertIn([
                'git', 'clone', '--branch', 'midnight', '--single-branch',
                'https://github.com/simulationcraft/simc.git', str(source),
            ], commands)
            self.assertIn(
                ['git', '-C', str(source), 'merge-base', '--is-ancestor',
                 required_revision, 'origin/midnight'],
                commands,
            )
            self.assertIn(
                ['git', '-C', str(source), 'pull', '--ff-only', '--force', 'origin', 'midnight'], commands,
            )
            self.assertFalse(any('reset' in command or 'clean' in command for command in commands))
            self.assertEqual(
                json.loads(Path(values['simc_path'] + '.lmonitor-build.json').read_text()),
                {
                    'revision': required_revision,
                    'html_locale_patch_version': 1,
                },
            )

    def test_simc_maintenance_is_skipped_while_run_lease_may_be_live(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            values['simc_source_path'] = str(Path(root) / 'missing-source')
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values))
            consumer._lease_block_until = 10**20

            self.assertFalse(consumer._maintain_simc(force=True))

    def test_simc_maintenance_refuses_symlink_binary_entry(self):
        from simc_agent_consumer import AgentConfig, ConfigError

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'simc-source'
            (source / '.git').mkdir(parents=True)
            real_binary = Path(root) / 'real-simc'
            real_binary.write_text('#!/bin/sh\n', encoding='utf-8')
            real_binary.chmod(0o755)
            link = Path(root) / 'simc'
            link.symlink_to(real_binary)
            values = self.config(root)
            values.update({'simc_path': str(link), 'simc_source_path': str(source)})

            with self.assertRaisesRegex(ConfigError, 'executable SimC binary file'):
                AgentConfig.from_dict(values)

    def test_agent_writes_rotating_local_log_with_runtime_errors(self):
        from simc_agent_consumer import configure_logging

        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / 'logs' / 'agent.log'
            logger = configure_logging(str(log_path), max_bytes=4096, backup_count=2)
            logger.error('simc compile failed for test')
            for handler in logger.handlers:
                handler.flush()

            content = log_path.read_text(encoding='utf-8')
            self.assertIn('ERROR', content)
            self.assertIn('simc compile failed for test', content)
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)

    def test_minimal_config_registration_does_not_send_redundant_backend_identifier(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            simc = Path(root) / 'simc'
            simc.write_text('#!/bin/sh\n', encoding='utf-8')
            simc.chmod(0o755)
            config = AgentConfig.from_dict({
                'enrollment_token': 'enroll-secret',
                'simc_path': str(simc),
                'token_path': str(Path(root) / 'agent.token'),
            })
            transport = MagicMock()
            transport.json.return_value = {
                'success': True, 'agent_token': 'token-id.' + ('x' * 43),
                'heartbeat_interval_seconds': 30, 'lease_seconds': 90,
            }

            SimcAgentConsumer(config, transport=transport).register()

            payload = transport.json.call_args.kwargs['payload']
            self.assertNotIn('backend_identifier', payload)
            self.assertRegex(payload['name'], r'^simc-agent-[0-9a-f]{12}$')

    def test_first_registration_persists_separate_agent_token_with_0600_mode(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            config = AgentConfig.from_dict(self.config(root))
            transport = MagicMock()
            transport.json.return_value = {
                'success': True, 'agent_token': 'token-id.' + ('x' * 43),
                'heartbeat_interval_seconds': 30, 'lease_seconds': 90,
            }
            consumer = SimcAgentConsumer(config, transport=transport)
            consumer.register()

            token_path = Path(config.token_path)
            self.assertEqual(token_path.read_text(encoding='utf-8'), 'token-id.' + ('x' * 43))
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
            request = transport.json.call_args
            self.assertEqual(request.kwargs['authorization'], 'Enrollment enroll-secret')
            self.assertNotIn('enroll-secret', request.kwargs['payload'])

    def test_existing_token_registration_and_claim_use_bearer_identity(self):
        from simc_agent_consumer import AgentConfig, PROTOCOL_VERSION, SimcAgentConsumer, VERSION, agent_revision

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            token = 'token-id.' + ('y' * 43)
            token_path = Path(values['token_path'])
            token_path.write_text(token, encoding='utf-8')
            token_path.chmod(0o600)
            config = AgentConfig.from_dict(values)
            transport = MagicMock()
            transport.json.side_effect = [
                {'success': True, 'heartbeat_interval_seconds': 20, 'lease_seconds': 60},
                None,
            ]
            consumer = SimcAgentConsumer(config, transport=transport)
            consumer.register()
            self.assertIsNone(consumer.claim())
            self.assertEqual(transport.json.call_args_list[0].kwargs['authorization'], 'Bearer ' + token)
            self.assertEqual(transport.json.call_args_list[1].kwargs['authorization'], 'Bearer ' + token)
            self.assertEqual(transport.json.call_args_list[1].kwargs['payload'], {
                'instance_id': consumer.instance_id,
                'agent_version': VERSION,
                'agent_revision': agent_revision(Path(__file__).resolve().parents[2]),
                'protocol_version': PROTOCOL_VERSION,
            })

    def test_update_required_claim_forces_fast_forward_pull_without_reset_or_clean(self):
        from simc_agent_consumer import APIError, AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            values['repository_path'] = root
            self.write_token(values, 'token-id.' + ('u' * 43))
            (Path(root) / '.git').mkdir()
            (Path(root) / 'simc_agent_consumer.py').write_text(
                "VERSION = '1.3.3'\n", encoding='utf-8',
            )
            transport = MagicMock()
            transport.json.side_effect = [
                {'success': True, 'heartbeat_interval_seconds': 20, 'lease_seconds': 60},
                None,
                APIError(
                    'Agent update required', 426,
                    {'code': 'agent_update_required', 'required_version': '1.3.3'},
                ),
            ]
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=transport)

            def git_result(command, **kwargs):
                stdout = ''
                if command[-3:] == ['config', '--get', 'remote.origin.url']:
                    stdout = 'https://github.com/LoRexxar/LMonitor.git\n'
                elif command[-2:] == ['branch', '--show-current']:
                    stdout = 'master\n'
                return MagicMock(returncode=0, stdout=stdout, stderr='')

            with patch('simc_agent_consumer.__file__', str(Path(root) / 'simc_agent_consumer.py')), patch(
                'simc_agent_consumer.subprocess.run', side_effect=git_result,
            ) as run_git, patch(
                'simc_agent_consumer.os.execv', side_effect=RuntimeError('reexec'),
            ) as execv:
                with self.assertRaisesRegex(RuntimeError, 'reexec'):
                    consumer.run(once=True)

            commands = [call.args[0] for call in run_git.call_args_list]
            self.assertIn(
                ['git', '-C', root, 'pull', '--ff-only', '--force', 'origin', 'master'], commands,
            )
            self.assertFalse(any('reset' in command or 'clean' in command for command in commands))
            execv.assert_called_once()

    def test_legacy_simc_revision_error_does_not_drive_agent_maintenance(self):
        from simc_agent_consumer import APIError, AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            values['simc_source_path'] = str(Path(root) / 'simc-source')
            self.write_token(values, 'token-id.' + ('s' * 43))
            transport = MagicMock()
            transport.json.side_effect = [
                {'success': True, 'heartbeat_interval_seconds': 20, 'lease_seconds': 60},
                None,
                APIError(
                    'Agent SimC update required', 409,
                    {'code': 'simc_update_required', 'required_version': 'b' * 40},
                ),
            ]
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=transport)

            with patch.object(
                consumer, '_maintain_simc_with_heartbeats', return_value=False,
            ) as maintain:
                with self.assertRaisesRegex(APIError, 'SimC update required'):
                    consumer.run(once=True)

            self.assertEqual(maintain.call_count, 1)
            maintain.assert_any_call()

    def test_self_update_still_refuses_an_untrusted_repository_before_forcing_reset(self):
        from simc_agent_consumer import APIError, AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            values['repository_path'] = root
            (Path(root) / '.git').mkdir()
            (Path(root) / 'simc_agent_consumer.py').write_text(
                "VERSION = '1.0.0'\n", encoding='utf-8',
            )
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())

            def git_result(command, **_kwargs):
                stdout = 'https://example.invalid/untrusted.git\n' if command[-3:] == [
                    'config', '--get', 'remote.origin.url',
                ] else ''
                return MagicMock(returncode=0, stdout=stdout, stderr='')

            with patch('simc_agent_consumer.__file__', str(Path(root) / 'simc_agent_consumer.py')), patch(
                'simc_agent_consumer.subprocess.run', side_effect=git_result,
            ) as run_git:
                with self.assertRaisesRegex(APIError, 'origin is not the trusted repository'):
                    consumer._self_update('1.1.0')

            commands = [call.args[0] for call in run_git.call_args_list]
            self.assertFalse(any(command[-2:] == ['reset', '--hard'] for command in commands))

    def test_existing_token_with_group_or_other_permissions_is_rejected(self):
        from simc_agent_consumer import AgentConfig, ConfigError, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            token_path = Path(values['token_path'])
            token_path.write_text('token-id.' + ('z' * 43), encoding='ascii')
            token_path.chmod(0o644)

            with self.assertRaisesRegex(ConfigError, 'permissions'):
                SimcAgentConsumer(AgentConfig.from_dict(values), MagicMock())

    def test_windows_token_save_does_not_require_posix_directory_or_fchmod(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())

            with patch('simc_agent_consumer._is_windows', return_value=True), \
                    patch('simc_agent_consumer.os.fchmod') as fchmod:
                consumer._save_token('token-id.' + ('w' * 43))

            self.assertEqual(Path(values['token_path']).read_text(encoding='ascii'), 'token-id.' + ('w' * 43))
            fchmod.assert_not_called()

    def test_windows_token_mode_bits_are_not_enforced(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            token_path = Path(values['token_path'])
            token_path.write_text('token-id.' + ('w' * 43), encoding='ascii')
            token_path.chmod(0o644)

            with patch('simc_agent_consumer._is_windows', return_value=True):
                consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())

            self.assertTrue(consumer.agent_token)

    def test_execute_job_writes_isolated_input_runs_simc_and_uploads_report(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            token = 'token-id.' + ('z' * 43)
            self.write_token(values, token)
            config = AgentConfig.from_dict(values)
            transport = MagicMock()
            report = b'<html>ok</html>'
            transport.json.side_effect = [
                {
                    'object_key': 'simc_agent_results/simc_task_1_run_17.html',
                    'url': 'https://bucket.oss.example/signed', 'method': 'PUT',
                    'headers': {'Content-Type': 'text/html; charset=utf-8'},
                },
                {'run_id': 17, 'status': 'completed'},
            ]
            process = MagicMock()
            process.communicate.return_value = ('Player: A\nDPS=1234', '')
            process.returncode = 0
            job = {
                'run_id': 17, 'task_id': 9, 'sequence': 2,
                'lease_token': 'lease-secret',
                'input': 'warrior="A"\nhtml=simc_task_1_run_17.html',
                'input_hash': hashlib.sha256(
                    b'warrior="A"\nhtml=simc_task_1_run_17.html').hexdigest(),
                'output_filename': 'simc_task_1_run_17.html', 'timeout_seconds': 600, 'lease_expires_at': '2999-01-01T00:00:00+00:00',
            }

            def create_report(*args, **kwargs):
                cwd = Path(kwargs['cwd'])
                (cwd / 'simc_task_1_run_17.html').write_text('<html>ok</html>', encoding='utf-8')
                return process

            with patch.dict(
                'simc_agent_consumer.os.environ',
                {'LANG': 'zh_CN.UTF-8', 'LC_ALL': 'zh_CN.UTF-8'}, clear=True,
            ), patch('simc_agent_consumer.subprocess.Popen', side_effect=create_report) as popen:
                consumer = SimcAgentConsumer(config, transport=transport)
                consumer.agent_token = token
                consumer.execute_job(job)

            self.assertEqual(popen.call_args.kwargs['env']['LANG'], 'zh_CN.UTF-8')
            self.assertEqual(popen.call_args.kwargs['env']['LC_ALL'], 'zh_CN.UTF-8')
            transport.put_bytes.assert_called_once_with(
                url='https://bucket.oss.example/signed', body=report,
                headers={'Content-Type': 'text/html; charset=utf-8'},
            )
            completion = transport.json.call_args_list[1].kwargs
            self.assertEqual(completion['path'], '/api/simc-agent/v1/jobs/17/complete/')
            self.assertEqual(completion['authorization'], 'Bearer ' + token)
            self.assertEqual(completion['payload']['status'], 'completed')
            self.assertEqual(completion['payload']['instance_id'], consumer.instance_id)
            self.assertEqual(completion['payload']['stdout'], 'Player: A\nDPS=1234')
            self.assertEqual(completion['payload']['report']['size'], len(report))

    def test_execute_job_keeps_lease_heartbeat_alive_through_upload_and_completion(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            token = 'token-id.' + ('h' * 43)
            self.write_token(values, token)
            process = MagicMock(returncode=0)
            process.communicate.return_value = ('Player: A\nDPS=1234', '')
            captured = {}

            class CapturingThread:
                def __init__(self, *, target, args, **kwargs):
                    captured['stop'] = args[1]
                    captured['joined'] = False

                def start(self):
                    return None

                def join(self, timeout=None):
                    captured['joined'] = True

            job = {
                'run_id': 17, 'task_id': 1, 'sequence': 1,
                'lease_token': 'lease-secret',
                'input': 'warrior="A"\nhtml=simc_task_1_run_17.html',
                'input_hash': hashlib.sha256(
                    b'warrior="A"\nhtml=simc_task_1_run_17.html').hexdigest(),
                'output_filename': 'simc_task_1_run_17.html',
                'timeout_seconds': 600,
                'lease_expires_at': '2999-01-01T00:00:00+00:00',
            }

            def create_report(*args, **kwargs):
                (Path(kwargs['cwd']) / job['output_filename']).write_text(
                    '<html>ok</html>', encoding='utf-8',
                )
                return process

            observed = []
            consumer = SimcAgentConsumer(
                AgentConfig.from_dict(values), transport=MagicMock(),
            )
            consumer.agent_token = token
            consumer._upload_report = MagicMock(side_effect=lambda *args: (
                observed.append(captured['stop'].is_set()) or {
                    'object_key': 'simc_agent_results/simc_task_1_run_17.html',
                    'size': 15, 'sha256': 'a' * 64,
                }
            ))
            consumer._completion_json = MagicMock(side_effect=lambda *args: (
                observed.append(captured['stop'].is_set()) or True
            ))
            with patch('simc_agent_consumer.subprocess.Popen', side_effect=create_report), patch(
                'simc_agent_consumer.threading.Thread', CapturingThread,
            ):
                consumer.execute_job(job)

            self.assertEqual(observed, [False, False])
            self.assertTrue(captured['stop'].is_set())
            self.assertTrue(captured['joined'])

    def test_execute_job_uploads_report_directly_to_oss_and_completes_with_json_only(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            token = 'token-id.' + ('o' * 43)
            self.write_token(values, token)
            transport = MagicMock()
            transport.json.side_effect = [
                {
                    'object_key': 'simc_agent_results/simc_task_1_run_17.html',
                    'url': 'https://bucket.oss.example/signed',
                    'method': 'PUT',
                    'headers': {
                        'Content-Type': 'text/html; charset=utf-8',
                        'x-oss-meta-sha256': hashlib.sha256(b'<html>ok</html>').hexdigest(),
                    },
                },
                {'run_id': 17, 'status': 'completed', 'idempotent': False},
            ]
            process = MagicMock(returncode=0)
            process.communicate.return_value = ('Player: A\nDPS=1234', '')
            job = {
                'run_id': 17, 'task_id': 9, 'sequence': 2,
                'lease_token': 'lease-secret',
                'input': 'warrior="A"\nhtml=simc_task_1_run_17.html',
                'input_hash': hashlib.sha256(
                    b'warrior="A"\nhtml=simc_task_1_run_17.html').hexdigest(),
                'output_filename': 'simc_task_1_run_17.html',
                'timeout_seconds': 600,
                'lease_expires_at': '2999-01-01T00:00:00+00:00',
            }

            def create_report(*args, **kwargs):
                (Path(kwargs['cwd']) / job['output_filename']).write_bytes(b'<html>ok</html>')
                return process

            with patch('simc_agent_consumer.subprocess.Popen', side_effect=create_report):
                consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=transport)
                consumer.agent_token = token
                consumer.execute_job(job)

            ticket = transport.json.call_args_list[0].kwargs
            self.assertEqual(ticket['path'], '/api/simc-agent/v1/jobs/17/report-upload/')
            self.assertEqual(ticket['payload']['size'], len(b'<html>ok</html>'))
            self.assertEqual(ticket['payload']['sha256'], hashlib.sha256(b'<html>ok</html>').hexdigest())
            transport.put_bytes.assert_called_once_with(
                url='https://bucket.oss.example/signed', body=b'<html>ok</html>',
                headers={
                    'Content-Type': 'text/html; charset=utf-8',
                    'x-oss-meta-sha256': hashlib.sha256(b'<html>ok</html>').hexdigest(),
                },
            )
            completion = transport.json.call_args_list[1].kwargs
            self.assertEqual(completion['path'], '/api/simc-agent/v1/jobs/17/complete/')
            self.assertEqual(completion['payload']['report']['object_key'],
                             'simc_agent_results/simc_task_1_run_17.html')
            self.assertEqual(completion['payload']['report']['size'], len(b'<html>ok</html>'))
            self.assertEqual(completion['payload']['report']['sha256'],
                             hashlib.sha256(b'<html>ok</html>').hexdigest())
            transport.multipart.assert_not_called()

    def test_failed_process_reports_failure_without_html(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            token = 'token-id.' + ('q' * 43)
            self.write_token(values, token)
            process = MagicMock(returncode=2)
            process.communicate.return_value = ('', 'simc failed')
            transport = MagicMock()
            with patch('simc_agent_consumer.subprocess.Popen', return_value=process):
                consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=transport)
                consumer.agent_token = token
                consumer.execute_job({
                    'run_id': 18, 'task_id': 9, 'sequence': 1,
                    'lease_token': 'lease', 'input': 'bad input',
                    'input_hash': hashlib.sha256(b'bad input').hexdigest(),
                    'output_filename': 'simc_task_1_run_18.html', 'timeout_seconds': 600, 'lease_expires_at': '2999-01-01T00:00:00+00:00',
                })
            completion = transport.json.call_args.kwargs
            self.assertEqual(completion['payload']['status'], 'failed')
            self.assertIn('simc failed', completion['payload']['stderr'])
            self.assertIsNone(completion['payload']['report'])

    def test_config_rejects_coercible_types_and_non_finite_numbers(self):
        from simc_agent_consumer import AgentConfig, ConfigError

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            for field, value in (
                ('allow_insecure_http', 'false'), ('server_url', 1),
                ('poll_interval_seconds', True), ('request_timeout_seconds', float('inf')),
                ('max_run_seconds', '10'),
            ):
                with self.subTest(field=field), self.assertRaises(ConfigError):
                    AgentConfig.from_dict({**values, field: value})

    def test_token_symlink_is_rejected_without_touching_target(self):
        from simc_agent_consumer import AgentConfig, ConfigError, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            target = Path(root) / 'target'
            target.write_text('keep', encoding='ascii')
            Path(values['token_path']).symlink_to(target)
            with self.assertRaises(ConfigError):
                SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())
            self.assertEqual(target.read_text(encoding='ascii'), 'keep')

    def test_redirect_handler_never_constructs_followup_request(self):
        from simc_agent_consumer import _NoRedirectHandler

        handler = _NoRedirectHandler()
        request = MagicMock()
        self.assertIsNone(handler.redirect_request(
            request, None, 302, 'Found', {}, 'http://other.example/stolen'))

    def test_oss_put_rejects_unsafe_url_and_sensitive_headers(self):
        from simc_agent_consumer import APIError, HTTPTransport

        transport = HTTPTransport('https://control.example', 1)
        with self.assertRaises(APIError):
            transport.put_bytes(url='http://bucket.example/signed', body=b'x', headers={})
        with self.assertRaises(APIError):
            transport.put_bytes(url='https://bucket.example/signed', body=b'x',
                                headers={'Authorization': 'secret'})
        for url in ('https://bucket.example/signed#fragment', 'https://127.0.0.1/signed'):
            with self.subTest(url=url), self.assertRaises(APIError):
                transport.put_bytes(url=url, body=b'x', headers={})
        for header in ('Host', 'Content-Length', 'Transfer-Encoding', 'Connection'):
            with self.subTest(header=header), self.assertRaises(APIError):
                transport.put_bytes(url='https://bucket.example/signed', body=b'x',
                                    headers={header: 'unsafe'})

    def test_completion_log_tail_is_bounded_by_utf8_bytes(self):
        from simc_agent_consumer import COMPLETION_TEXT_MAX_BYTES, _utf8_tail

        value = _utf8_tail('前' * COMPLETION_TEXT_MAX_BYTES, COMPLETION_TEXT_MAX_BYTES)
        self.assertLessEqual(len(value.encode('utf-8')), COMPLETION_TEXT_MAX_BYTES)
        self.assertTrue(value)

    def test_invalid_hash_and_popen_error_report_failed_completion(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            self.write_token(values, 'token')
            transport = MagicMock()
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=transport)
            base = {
                'run_id': 20, 'lease_token': 'lease', 'input': 'input',
                'output_filename': 'simc_task_1_run_20.html', 'timeout_seconds': 10,
                'lease_expires_at': '2999-01-01T00:00:00+00:00',
            }
            consumer.execute_job({**base, 'input_hash': 'A' * 64})
            self.assertIn('input_hash', transport.json.call_args.kwargs['payload']['stderr'])
            transport.reset_mock()
            with patch('simc_agent_consumer.subprocess.Popen', side_effect=OSError('cannot spawn')):
                consumer.execute_job({**base, 'input_hash': hashlib.sha256(b'input').hexdigest()})
            completion = transport.json.call_args.kwargs
            self.assertEqual(completion['payload']['status'], 'failed')
            self.assertIn('cannot spawn', completion['payload']['stderr'])

    def test_claim_timeout_is_capped_and_strictly_validated(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = {**self.config(root), 'max_run_seconds': 7}
            self.write_token(values, 'token')
            process = MagicMock(returncode=2)
            process.communicate.return_value = (b'', b'failed')
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())
            job = {
                'run_id': 21, 'lease_token': 'lease', 'input': 'input',
                'input_hash': hashlib.sha256(b'input').hexdigest(),
                'output_filename': 'simc_task_1_run_21.html', 'timeout_seconds': 99,
                'lease_expires_at': '2999-01-01T00:00:00+00:00',
            }
            with patch('simc_agent_consumer.subprocess.Popen', return_value=process):
                consumer.execute_job(job)
            self.assertEqual(process.communicate.call_args.kwargs['timeout'], 7)
            consumer.transport.reset_mock()
            consumer.execute_job({**job, 'timeout_seconds': True})
            self.assertIn('positive finite number',
                          consumer.transport.json.call_args.kwargs['payload']['stderr'])

    def test_completion_retries_with_one_fixed_completion_id(self):
        from simc_agent_consumer import APIError, AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            self.write_token(values, 'token')
            transport = MagicMock()
            transport.json.side_effect = [APIError('temporary'), APIError('temporary'), {}]
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=transport)
            with patch.object(consumer.stop_event, 'wait', return_value=False):
                consumer._complete(22, 'lease', 'fixed-id', 'failed', '', 'error', None, None)
            self.assertEqual(transport.json.call_count, 3)
            self.assertEqual({call.kwargs['payload']['completion_id']
                              for call in transport.json.call_args_list}, {'fixed-id'})

    def test_completion_defers_to_outbox_after_three_transient_failures(self):
        from simc_agent_consumer import APIError, AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            self.write_token(values, 'token')
            transport = MagicMock()
            transport.json.side_effect = [
                APIError('gateway timeout'), APIError('gateway timeout'),
                APIError('gateway timeout'),
            ]
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=transport)

            with patch.object(consumer.stop_event, 'wait', return_value=False):
                consumer._complete(22, 'lease', 'a' * 32, 'failed', '', 'error', None, None)

            self.assertEqual(transport.json.call_count, 3)
            self.assertEqual(len(list(consumer.completion_outbox_path.glob('*.json'))), 1)
            self.assertEqual({call.kwargs['payload']['completion_id']
                              for call in transport.json.call_args_list}, {'a' * 32})

    def test_completion_outbox_persists_after_three_transient_failures_and_flushes_on_next_consumer(self):
        from simc_agent_consumer import APIError, AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            self.write_token(values, 'token')
            first_transport = MagicMock()
            first_transport.json.side_effect = APIError('network unavailable')
            first = SimcAgentConsumer(AgentConfig.from_dict(values), transport=first_transport)

            with patch.object(first.stop_event, 'wait', return_value=False):
                first._complete(22, 'lease', 'f' * 32, 'failed', '', 'simc failed', None, None)

            self.assertEqual(first_transport.json.call_count, 3)
            outbox = first.completion_outbox_path
            self.assertEqual(len(list(outbox.glob('*.json'))), 1)

            recovered_transport = MagicMock()
            recovered_transport.json.side_effect = APIError('still offline')
            recovered = SimcAgentConsumer(AgentConfig.from_dict(values), transport=recovered_transport)
            self.assertFalse(recovered.flush_completion_outbox())
            self.assertEqual(len(list(outbox.glob('*.json'))), 1)

            recovered_transport.json.side_effect = None
            self.assertTrue(recovered.flush_completion_outbox())
            self.assertEqual(recovered_transport.json.call_count, 4)
            payload = recovered_transport.json.call_args.kwargs['payload']
            self.assertEqual(payload['completion_id'], 'f' * 32)
            self.assertEqual(payload['status'], 'failed')
            self.assertEqual(len(list(outbox.glob('*.json'))), 0)

    def test_completion_outbox_persists_report_when_upload_is_unavailable(self):
        from simc_agent_consumer import APIError, AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            self.write_token(values, 'token')
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())
            with patch.object(consumer, '_upload_report', side_effect=APIError('offline')):
                consumer._complete(
                    23, 'lease', 'e' * 32, 'completed', 'DPS=1', '',
                    b'<html>report</html>', 'simc_task_1_run_23.html',
                )

            entry = next(consumer.completion_outbox_path.glob('*.json'))
            persisted = json.loads(entry.read_text(encoding='utf-8'))
            self.assertEqual(persisted['metadata']['status'], 'completed')
            self.assertIsNone(persisted['metadata']['report'])
            self.assertEqual(persisted['report_name'], 'simc_task_1_run_23.html')
            self.assertEqual(persisted['report_bytes'], 'PGh0bWw+cmVwb3J0PC9odG1sPg==')

    def test_uncertain_success_completion_never_falls_back_to_failed_terminal_state(self):
        from simc_agent_consumer import AgentConfig, APIError, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            Path(values['token_path']).write_text('token', encoding='ascii')
            os.chmod(values['token_path'], 0o600)
            transport = MagicMock()
            transport.json.side_effect = APIError('completion response lost')
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=transport)
            with patch.object(consumer, '_upload_report', return_value={
                    'object_key': 'simc_agent_results/simc_task_1_run_22.html',
                    'size': 16, 'sha256': 'a' * 64,
                 }), patch.object(consumer.stop_event, 'wait', return_value=False):
                consumer._complete(
                    22, 'lease', 'b' * 32, 'completed', 'Player: A\nDPS=1234', '',
                    b'<html>ok</html>', 'simc_task_1_run_22.html',
                )
            completion_calls = [
                call for call in transport.json.call_args_list
                if call.kwargs['path'].endswith('/complete/')
            ]
            self.assertEqual(len(completion_calls), 3)
            self.assertTrue(all(
                call.kwargs['payload']['status'] == 'completed'
                for call in completion_calls
            ))
            self.assertEqual(len(list(consumer.completion_outbox_path.glob('*.json'))), 1)

    def test_oversized_report_is_rejected_before_reading_file(self):
        from simc_agent_consumer import AgentConfig, MAX_REPORT_BYTES, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            self.write_token(values, 'token')
            transport = MagicMock()
            process = MagicMock(returncode=0)
            process.communicate.return_value = (b'', b'')
            job = {
                'run_id': 24, 'lease_token': 'lease',
                'input': 'html=simc_task_1_run_24.html',
                'input_hash': hashlib.sha256(
                    b'html=simc_task_1_run_24.html').hexdigest(),
                'output_filename': 'simc_task_1_run_24.html',
                'timeout_seconds': 10,
                'lease_expires_at': '2999-01-01T00:00:00+00:00',
            }

            def create_sparse_report(*args, **kwargs):
                report = Path(kwargs['cwd']) / job['output_filename']
                with report.open('wb') as handle:
                    handle.truncate(MAX_REPORT_BYTES + 1)
                return process

            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=transport)
            with patch('simc_agent_consumer.subprocess.Popen', side_effect=create_sparse_report), \
                    patch.object(Path, 'read_bytes', side_effect=AssertionError('must not read')):
                consumer.execute_job(job)

            completion = transport.json.call_args.kwargs
            self.assertEqual(completion['payload']['status'], 'failed')
            self.assertIsNone(completion['payload']['report'])
            self.assertIn('20 MiB', completion['payload']['stderr'])

    def test_lease_deadline_terminates_process(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            self.write_token(values, 'token')
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=MagicMock())
            consumer.lease_seconds = 0.01
            process = MagicMock()
            process.poll.return_value = None
            process.wait.return_value = 0
            stopped, lost = __import__('threading').Event(), __import__('threading').Event()
            consumer._lease_heartbeat_loop(
                {'run_id': 23, 'lease_token': 'lease'}, stopped, lost, process,
                __import__('time').monotonic() + 0.01)
            self.assertTrue(lost.is_set())
            process.terminate.assert_called_once_with()
