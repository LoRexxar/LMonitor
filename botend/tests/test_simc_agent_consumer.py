import hashlib
import json
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
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

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
            })

    def test_existing_token_with_group_or_other_permissions_is_rejected(self):
        from simc_agent_consumer import AgentConfig, ConfigError, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            token_path = Path(values['token_path'])
            token_path.write_text('token-id.' + ('z' * 43), encoding='ascii')
            token_path.chmod(0o644)

            with self.assertRaisesRegex(ConfigError, 'permissions'):
                SimcAgentConsumer(AgentConfig.from_dict(values), MagicMock())

    def test_execute_job_writes_isolated_input_runs_simc_and_uploads_report(self):
        from simc_agent_consumer import AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            token = 'token-id.' + ('z' * 43)
            self.write_token(values, token)
            config = AgentConfig.from_dict(values)
            transport = MagicMock()
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

            with patch('simc_agent_consumer.subprocess.Popen', side_effect=create_report):
                consumer = SimcAgentConsumer(config, transport=transport)
                consumer.agent_token = token
                consumer.execute_job(job)

            upload = transport.multipart.call_args.kwargs
            self.assertEqual(upload['path'], '/api/simc-agent/v1/jobs/17/complete/')
            self.assertEqual(upload['authorization'], 'Bearer ' + token)
            self.assertEqual(upload['metadata']['status'], 'completed')
            self.assertEqual(upload['metadata']['instance_id'], consumer.instance_id)
            self.assertEqual(upload['metadata']['stdout'], 'Player: A\nDPS=1234')
            self.assertEqual(upload['report_bytes'], b'<html>ok</html>')
            self.assertEqual(upload['report_name'], 'simc_task_1_run_17.html')

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
            upload = transport.multipart.call_args.kwargs
            self.assertEqual(upload['metadata']['status'], 'failed')
            self.assertIn('simc failed', upload['metadata']['stderr'])
            self.assertIsNone(upload['report_bytes'])

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

    def test_multipart_requires_object_json_and_enforces_report_limit(self):
        from simc_agent_consumer import APIError, HTTPTransport, MAX_REPORT_BYTES

        transport = HTTPTransport('https://control.example', 1)
        with patch.object(transport, '_request', return_value=b'[]'):
            with self.assertRaises(APIError):
                transport.multipart(path='/complete', metadata={}, report_bytes=None,
                                    report_name=None, authorization='Bearer token')
        with self.assertRaises(APIError):
            transport.multipart(path='/complete', metadata={},
                                report_bytes=b'x' * (MAX_REPORT_BYTES + 1),
                                report_name='run.html', authorization='Bearer token')

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
            self.assertIn('input_hash', transport.multipart.call_args.kwargs['metadata']['stderr'])
            transport.reset_mock()
            with patch('simc_agent_consumer.subprocess.Popen', side_effect=OSError('cannot spawn')):
                consumer.execute_job({**base, 'input_hash': hashlib.sha256(b'input').hexdigest()})
            upload = transport.multipart.call_args.kwargs
            self.assertEqual(upload['metadata']['status'], 'failed')
            self.assertIn('cannot spawn', upload['metadata']['stderr'])

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
                          consumer.transport.multipart.call_args.kwargs['metadata']['stderr'])

    def test_completion_retries_with_one_fixed_completion_id(self):
        from simc_agent_consumer import APIError, AgentConfig, SimcAgentConsumer

        with tempfile.TemporaryDirectory() as root:
            values = self.config(root)
            self.write_token(values, 'token')
            transport = MagicMock()
            transport.multipart.side_effect = [APIError('temporary'), APIError('temporary'), {}]
            consumer = SimcAgentConsumer(AgentConfig.from_dict(values), transport=transport)
            with patch.object(consumer.stop_event, 'wait', return_value=False):
                consumer._complete(22, 'lease', 'fixed-id', 'failed', '', 'error', None, None)
            self.assertEqual(transport.multipart.call_count, 3)
            self.assertEqual({call.kwargs['metadata']['completion_id']
                              for call in transport.multipart.call_args_list}, {'fixed-id'})

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

            upload = transport.multipart.call_args.kwargs
            self.assertEqual(upload['metadata']['status'], 'failed')
            self.assertIsNone(upload['report_bytes'])
            self.assertIn('20 MiB', upload['metadata']['stderr'])

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
