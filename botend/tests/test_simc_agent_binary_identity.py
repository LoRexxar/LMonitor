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
        return subprocess.CompletedProcess([], returncode, text.encode('utf-8'), b'')

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

    def test_report_exposes_cached_probe_timeout_without_raw_output(self):
        error = subprocess.TimeoutExpired('secret/path', 5, output=b'secret-output')
        with patch('simc_agent_consumer.subprocess.run', side_effect=error) as run:
            first = self.consumer._report()['capabilities']
            second = self.consumer._report()['capabilities']
        self.assertEqual(first.get('binary_identity_status'), 'failed')
        self.assertEqual(first.get('binary_identity_reason'), 'probe_timeout')
        self.assertEqual(first, second)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('secret', json.dumps(first))

    def test_failure_reasons_are_bounded_and_success_clears_them(self):
        cases = [
            (self.result(BANNER, 7), 'nonzero_exit', None),
            (self.result('secret-output'), 'banner_count', 0),
            (self.result(BANNER + BANNER), 'banner_count', 2),
            (self.result('SimulationCraft unknown secret-output'), 'build_missing', 1),
            (subprocess.CompletedProcess([], 0, b'\xffsecret-output', b''), 'decode_error', None),
        ]
        for result, reason, count in cases:
            with self.subTest(reason=reason, count=count):
                self.binary.write_bytes(self.binary.read_bytes() + b'x')
                with patch('simc_agent_consumer.subprocess.run', return_value=result) as run, patch(
                    'simc_agent_consumer.locale.getencoding', return_value='utf-8'
                ):
                    caps = self.consumer._report()['capabilities']
                    self.assertEqual(caps, self.consumer._report()['capabilities'])
                    self.assertEqual(run.call_count, 1)
                    self.assertFalse(run.call_args.kwargs['text'])
                self.assertEqual(caps['binary_identity_status'], 'failed')
                self.assertEqual(caps['binary_identity_reason'], reason)
                self.assertEqual(caps.get('binary_identity_banner_count'), count)
                self.assertEqual(caps['binary_identity_returncode'], result.returncode)
                for name in ('stdout', 'stderr'):
                    data = getattr(result, name)
                    self.assertEqual(caps[f'binary_identity_{name}_bytes'], len(data))
                    self.assertEqual(caps[f'binary_identity_{name}_sha256'], hashlib.sha256(data).hexdigest())
                self.assertNotIn('binary_sha256', caps)
                self.assertNotIn('secret', json.dumps(caps))
                self.assertNotIn(str(self.binary), json.dumps(caps))
                self.assertLess(len(json.dumps(caps)), 1024)
                with patch('simc_agent_consumer.time.monotonic', return_value=float('inf')), patch(
                    'simc_agent_consumer.subprocess.run', return_value=self.result()
                ):
                    recovered = self.consumer._report()['capabilities']
                self.assertEqual(recovered['binary_identity_status'], 'ok')
                self.assertEqual([k for k in recovered if k.startswith('binary_identity_')], ['binary_identity_status'])
                self.assertIn('binary_sha256', recovered)

    def test_missing_hash_timeout_and_hash_io_diagnostics(self):
        with patch('simc_agent_consumer.time.monotonic', side_effect=[0, 31]):
            self.assertEqual(self.consumer._probe_binary_identity(), {})
        self.assertEqual(self.consumer._binary_identity_diagnostics['binary_identity_reason'], 'hash_timeout')
        self.binary.write_bytes(b'changed')
        with patch.object(Path, 'open', side_effect=OSError('secret-path')):
            self.assertEqual(self.consumer._probe_binary_identity(), {})
        self.assertEqual(self.consumer._binary_identity_diagnostics['binary_identity_reason'], 'hash_io_error')
        self.binary.unlink()
        caps = self.consumer._report()['capabilities']
        self.assertEqual(caps['binary_identity_reason'], 'not_available')
        self.assertNotIn('binary_sha256', caps)
        self.assertNotIn('secret', json.dumps(caps))

    def test_process_start_failure_has_no_exception_text(self):
        with patch('simc_agent_consumer.subprocess.run', side_effect=OSError('secret-path')):
            caps = self.consumer._report()['capabilities']
        self.assertEqual(caps['binary_identity_reason'], 'probe_os_error')
        self.assertNotIn('secret', json.dumps(caps))

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
        self.assertEqual(self.consumer._binary_identity_diagnostics['binary_identity_reason'], 'file_changed')

    def test_stat_mismatch_reports_exact_stage_and_only_integer_differences(self):
        from types import SimpleNamespace

        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode')
        baseline = self.binary.stat()
        expected = {name: getattr(baseline, name) for name in fields}
        for stage in ('fstat_before_hash', 'fstat_after_hash', 'path_after_probe'):
            for field in fields:
                with self.subTest(stage=stage, field=field):
                    self.consumer._binary_identity_key = None
                    changed = SimpleNamespace(**{**expected, field: expected[field] + 1})
                    stats = [changed] if stage == 'fstat_before_hash' else [baseline, changed]
                    if stage == 'path_after_probe':
                        stats = [baseline, baseline]
                    from contextlib import ExitStack
                    with ExitStack() as stack:
                        def probe(*args, **kwargs):
                            if stage == 'path_after_probe':
                                stack.enter_context(patch.object(Path, 'stat', return_value=changed))
                            return self.result()
                        stack.enter_context(patch('simc_agent_consumer.os.fstat', side_effect=stats))
                        run = stack.enter_context(patch('simc_agent_consumer.subprocess.run', side_effect=probe))
                        caps = self.consumer._report()['capabilities']
                    self.assertEqual(caps['binary_identity_reason'], 'file_changed')
                    self.assertEqual(caps.get('binary_identity_comparison_stage'), stage)
                    self.assertEqual(caps.get('binary_identity_stat_differences'), {
                        field: {'expected': expected[field], 'observed': expected[field] + 1},
                    })
                    self.assertEqual(run.call_count, int(stage == 'path_after_probe'))
                    self.assertNotIn('binary_sha256', caps)
                    self.assertNotIn(str(self.binary), json.dumps(caps))
                    self.assertLess(len(json.dumps(caps)), 2048)
        with patch('simc_agent_consumer.time.monotonic', return_value=float('inf')), patch(
            'simc_agent_consumer.subprocess.run', return_value=self.result()
        ):
            recovered = self.consumer._report()['capabilities']
        self.assertEqual([k for k in recovered if k.startswith('binary_identity_')], ['binary_identity_status'])

    def windows_stats(self):
        from types import SimpleNamespace
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode')
        common = {name: getattr(self.binary.stat(), name) for name in fields}
        # Agent13 real differing fields; equal fields use the local fixture.
        return (SimpleNamespace(**{**common, 'st_mode': 33279,
                                   'st_ctime_ns': 1791054205847172200}),
                SimpleNamespace(**{**common, 'st_mode': 33206,
                                   'st_ctime_ns': 1791054205972792500}))

    def probe_with_stats(self, path_before, fd_before, fd_after, path_after, windows=True):
        with patch('simc_agent_consumer._is_windows', return_value=windows), patch(
            'simc_agent_consumer._is_executable_regular_file', return_value=True
        ), patch.object(Path, 'stat', side_effect=[path_before, path_after]), patch(
            'simc_agent_consumer.os.fstat', side_effect=[fd_before, fd_after]
        ), patch('simc_agent_consumer.subprocess.run', return_value=self.result()):
            return self.consumer._probe_binary_identity()

    def test_windows_real_mode_and_ctime_pair_succeeds(self):
        path, fd = self.windows_stats()
        from dataclasses import replace
        self.consumer.config = replace(self.consumer.config, platform='linux')  # Not runtime.
        result = self.probe_with_stats(path, fd, fd, path)
        self.assertEqual(result.get('binary_sha256'), hashlib.sha256(b'first binary').hexdigest())
        self.assertEqual(result.get('dbc_build'), '12.1.0.69933')

    def test_windows_same_api_changes_are_rejected(self):
        from types import SimpleNamespace
        path, fd = self.windows_stats()
        for source, original in [('fstat_before_hash', fd), ('path_before_hash', path)]:
            for field in ('st_ctime_ns', 'st_mode'):
                with self.subTest(source=source, field=field):
                    self.consumer._binary_identity_key = None
                    changed = SimpleNamespace(**{**vars(original), field: getattr(original, field) + 1})
                    result = self.probe_with_stats(path, fd,
                        changed if source == 'fstat_before_hash' else fd,
                        changed if source == 'path_before_hash' else path)
                    self.assertEqual(result, {})
                    diagnostics = self.consumer._binary_identity_diagnostics
                    self.assertEqual(diagnostics['binary_identity_expected_source'], source)
                    self.assertEqual(diagnostics['binary_identity_stat_differences'], {
                        field: {'expected': getattr(original, field), 'observed': getattr(changed, field)}})

    def test_windows_cross_api_comparable_fields_are_rejected(self):
        from types import SimpleNamespace
        path, fd = self.windows_stats()
        changes = {'st_dev': fd.st_dev + 1, 'st_ino': fd.st_ino + 1,
                   'st_size': fd.st_size + 1, 'st_mtime_ns': fd.st_mtime_ns + 1,
                   'st_mode': fd.st_mode ^ 0o200}
        for field, value in [*changes.items(), ('st_mode', 0o40666)]:
            with self.subTest(field=field, value=value):
                self.consumer._binary_identity_key = None
                changed = SimpleNamespace(**{**vars(fd), field: value})
                self.assertEqual(self.probe_with_stats(path, changed, changed, path), {})
                diagnostics = self.consumer._binary_identity_diagnostics
                self.assertEqual(diagnostics['binary_identity_comparison_stage'], 'fstat_before_hash')
                self.assertEqual(diagnostics['binary_identity_expected_source'], 'path_before_hash')
                self.assertIn(field, diagnostics['binary_identity_stat_differences'])

    def test_linux_does_not_relax_windows_differences(self):
        path, fd = self.windows_stats()
        from dataclasses import replace
        self.consumer.config = replace(self.consumer.config, platform='windows')
        self.assertEqual(self.probe_with_stats(path, fd, fd, path, windows=False), {})
        self.assertEqual(set(self.consumer._binary_identity_diagnostics['binary_identity_stat_differences']),
                         {'st_mode', 'st_ctime_ns'})

    def test_windows_path_ctime_and_mode_invalidate_success_cache(self):
        from types import SimpleNamespace
        path, fd = self.windows_stats()
        for field in ('st_ctime_ns', 'st_mode'):
            with self.subTest(field=field):
                self.consumer._binary_identity_key = None
                self.assertTrue(self.probe_with_stats(path, fd, fd, path))
                changed = SimpleNamespace(**{**vars(path), field: getattr(path, field) ^ 1})
                # Cache invalidation must re-enter hashing; failure cannot reuse success.
                with patch.object(Path, 'open', side_effect=OSError('unreadable')):
                    self.assertEqual(self.probe_with_stats(changed, fd, fd, changed), {})
                self.assertEqual(self.consumer._binary_identity_diagnostics['binary_identity_reason'], 'hash_io_error')

    def test_nonzero_or_ambiguous_banner_is_not_trusted(self):
        for output, code in [(BANNER, 1), ('not simc', 0), (BANNER + BANNER.replace('69933', '69934'), 0)]:
            with self.subTest(output=output, code=code):
                self.binary.write_bytes(self.binary.read_bytes() + b'x')
                with patch('simc_agent_consumer.subprocess.run', return_value=self.result(output, code)):
                    self.assertEqual(self.consumer._probe_binary_identity(), {})
