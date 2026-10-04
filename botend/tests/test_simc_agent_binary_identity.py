import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase

from simc_agent_consumer import AgentConfig, SimcAgentConsumer


BANNER = ('SimulationCraft 1210-01 for World of Warcraft 12.1.0.69933 Live '
          '(hotfix 2026-10-03/69933, git build midnight ' + 'a' * 40 + ')\n')


class SimcAgentBinaryIdentityTests(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.binary = Path(self.root.name) / 'simc'
        self.binary.write_bytes(b'first binary')
        self.binary.chmod(0o755)
        self.consumer = SimcAgentConsumer(AgentConfig.from_dict({
            'simc_path': str(self.binary),
            'token_path': str(Path(self.root.name) / 'token'),
            'enrollment_token': 'test',
            'max_concurrent_runs': 2,
        }))
        self.revision = patch('simc_agent_consumer.agent_upstream_revision', return_value='b' * 40)
        self.revision.start()
        self.addCleanup(self.revision.stop)

    def result(self, text=BANNER, returncode=0):
        return subprocess.CompletedProcess([], returncode, text, '')

    def test_report_measures_and_caches_without_changing_marker_semantics(self):
        Path(str(self.binary) + '.lmonitor-build.json').write_text(json.dumps({
            'revision': 'c' * 40, 'html_locale_patch_version': 1,
        }))
        with patch('simc_agent_consumer.subprocess.run', return_value=self.result()) as run:
            first = self.consumer._report('busy')
            with patch.object(Path, 'open', side_effect=AssertionError('binary reread')):
                cached = self.consumer._probe_binary_identity()
            second = self.consumer._report()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(first['capabilities']['binary_sha256'], hashlib.sha256(b'first binary').hexdigest())
        self.assertEqual(cached['binary_sha256'], first['capabilities']['binary_sha256'])
        self.assertEqual(first['capabilities']['dbc_build'], '12.1.0.69933')
        self.assertEqual(first['capabilities']['binary_revision'], 'a' * 40)
        self.assertEqual(first['capabilities'], second['capabilities'])
        self.assertEqual(first['capabilities']['conditional_evidence_protocol_version'], 0)
        self.assertEqual(first['capabilities']['max_concurrent_runs'], 2)
        self.assertEqual(first['current_version'], 'c' * 40)
        self.assertEqual(first['html_locale_patch_version'], 1)
        self.assertEqual(first['status'], 'busy')
        self.assertNotIn('relation', first['capabilities'])

    def test_atomic_replacement_and_deletion_invalidate_cached_identity(self):
        with patch('simc_agent_consumer.subprocess.run', return_value=self.result()) as run:
            before = self.consumer._report()['capabilities']
            replacement = self.binary.with_suffix('.new')
            replacement.write_bytes(b'other binary')
            replacement.chmod(0o755)
            os.replace(replacement, self.binary)
            after = self.consumer._report()['capabilities']
            self.assertNotEqual(before['binary_sha256'], after['binary_sha256'])
            self.assertEqual(run.call_count, 2)
            self.binary.unlink()
            missing = self.consumer._report()
        self.assertFalse(missing['binary_available'])
        self.assertNotIn('binary_sha256', missing['capabilities'])
        self.assertEqual(missing['current_version'], '')

    def test_failures_are_cached_without_trusted_identity_and_retried(self):
        with patch('simc_agent_consumer.subprocess.run', side_effect=subprocess.TimeoutExpired('simc', 5)) as run:
            self.assertEqual(self.consumer._probe_binary_identity(), {})
            self.assertEqual(self.consumer._probe_binary_identity(), {})
            self.assertEqual(run.call_count, 1)
        with patch('simc_agent_consumer.time.monotonic', return_value=float('inf')), patch(
            'simc_agent_consumer.subprocess.run', return_value=self.result('unrecognized output')
        ):
            self.assertEqual(self.consumer._probe_binary_identity(), {})

    def test_unknown_revision_is_not_inferred_from_marker_or_source(self):
        with patch('simc_agent_consumer.subprocess.run', return_value=self.result(
            'Nothing to sim! SimulationCraft 1210-01 for World of Warcraft 12.1.0.69933 Live (no-networking)'
        )):
            measured = self.consumer._probe_binary_identity()
        self.assertIn('binary_sha256', measured)
        self.assertEqual(measured['dbc_build'], '12.1.0.69933')
        self.assertNotIn('binary_revision', measured)

    def test_changed_during_probe_is_not_bound_to_old_hash(self):
        def change(*args, **kwargs):
            self.binary.write_bytes(b'changed during probe')
            return self.result()
        with patch('simc_agent_consumer.subprocess.run', side_effect=change):
            self.assertEqual(self.consumer._probe_binary_identity(), {})

    def test_nonzero_or_ambiguous_banner_is_not_trusted(self):
        for output, code in [(BANNER, 1), ('not simc', 0), (BANNER + BANNER.replace('69933', '69934'), 0)]:
            with self.subTest(output=output, code=code):
                self.binary.write_bytes(self.binary.read_bytes() + b'x')
                with patch('simc_agent_consumer.subprocess.run', return_value=self.result(output, code)):
                    self.assertEqual(self.consumer._probe_binary_identity(), {})
