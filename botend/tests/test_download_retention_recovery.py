import os
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from botend.services.download_checkpoint import PartDownload, atomic_json, read_json
from botend.services.download_retention import maintain_download_parts, check_root_budget


class RetentionRecoveryTests(TestCase):
    def test_boundary_marker_cleanup_fault_retains_exact_intent(self):
        from botend.services import download_retention as r
        a = self.attempt()
        real = r.fsync_directory
        def fail(path):
            if Path(path) == a.root and not a.directory.exists() and not list(a.root.glob('.delete-*')):
                raise OSError('marker cleanup fsync')
            return real(path)
        with patch.object(r, 'fsync_directory', fail):
            report = maintain_download_parts(self.root, apply=True)
        self.assertEqual(report['deleted_attempts'], 1)
        self.assertTrue(report['entries'][0]['cleanup_pending'])
        self.assertTrue(list(a.root.glob('.delete-*')))
        resumed = maintain_download_parts(self.root, apply=True)
        self.assertEqual(resumed['recovered_attempts'], 1)
        self.assertFalse(list(a.root.glob('.delete-*')))

    def test_boundary_signed_cache_limits_and_depth_refuse_before_delete(self):
        import json
        import zlib
        from django.core import signing
        from botend.services import download_retention as r
        a = self.attempt()
        cp = self.root.parent / ('.' + self.root.name + r.CHECKPOINT)
        values = [
            {'files': {}, 'directories': {str(self.root): [[0] * 7, [str(i) for i in range(1025)]]}},
            {'files': {}, 'directories': {}, 'unknown': {}},
            {'files': {str(self.root / 'x'): [[0] * 7, {'x': 'x' * r.MAX_CACHE_BYTES}]}, 'directories': {}},
        ]
        for cache in values:
            raw = zlib.compress(json.dumps(dict(schema=1, root=str(self.root), cache=cache)).encode())
            cp.write_bytes(raw + b'\n' + signing.Signer(salt=r.CHECKPOINT_SALT).signature(raw).encode())
            report = maintain_download_parts(self.root, apply=True)
            self.assertIn('capacity_blocked', report)
            self.assertTrue(a.directory.exists())
        cp.unlink()
        (self.root / 'legacy' / 'a' / 'b' / 'c').mkdir(parents=True)
        report = maintain_download_parts(self.root, apply=True)
        self.assertIn('capacity_blocked', report)
        self.assertTrue(a.directory.exists())

    def test_boundary_checkpoint_failure_is_warning(self):
        from botend.services.download_retention import _Budget
        self.attempt()
        with patch.object(_Budget, 'save_checkpoint', side_effect=OSError('checkpoint fault')):
            report = maintain_download_parts(self.root, apply=True)
        self.assertIn('maintenance_warning', report)
        self.assertEqual(report['deleted_attempts'], 1)

    def test_boundary_partial_unlink_accounting(self):
        a = self.attempt()
        original = Path.unlink
        calls = []
        def fail(path, *args, **kwargs):
            if path.parent == a.directory and path.name.endswith('-3.json'):
                calls.append(path)
                if len(calls) == 2:
                    raise OSError('second unlink')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail):
            report = maintain_download_parts(self.root, apply=True)
        item = next(i for i in report['entries'] if i['path'] == str(a.directory))
        self.assertEqual(item['status'], 'partial')
        self.assertEqual(item['deleted_files'], 1)
        self.assertEqual(len(list(a.directory.glob('*-3.json'))), 2)

    def test_boundary_post_rmdir_accounting(self):
        from botend.services import download_retention as r
        a = self.attempt()
        real = r.fsync_directory
        def fail(path):
            if Path(path) == a.root and not a.directory.exists():
                raise OSError('post rmdir')
            return real(path)
        with patch.object(r, 'fsync_directory', fail):
            report = maintain_download_parts(self.root, apply=True)
        self.assertFalse(a.directory.exists())
        self.assertEqual(report['deleted_attempts'], 1)
        self.assertTrue(report['entries'][0]['durability_pending'])
        self.assertTrue(list(a.root.glob('.delete-*')))
        resumed = maintain_download_parts(self.root, apply=True)
        self.assertEqual(resumed['deleted_attempts'], 0)
        self.assertEqual(resumed['recovered_attempts'], 1)

    def test_boundary_infeasible_budgets_terminate(self):
        a = self.attempt(count=5, payload=30)
        reports = [maintain_download_parts(self.root, apply=True, max_entries=5) for _ in range(4)]
        self.assertTrue(any(r.get('unsupported_budget') for r in reports), reports)
        self.assertTrue(a.directory.exists())

    def test_boundary_checkpoint_plus_part_terminates(self):
        a = self.attempt(count=1, payload=1500)
        reports = [maintain_download_parts(self.root, apply=True, max_bytes=1800) for _ in range(3)]
        self.assertTrue(any(r.get('unsupported_budget') for r in reports), reports)
        self.assertTrue(a.directory.exists())

    def test_boundary_legacy_closure_is_bounded(self):
        a = self.attempt()
        legacy = self.root / 'legacy'
        legacy.mkdir()
        for i in range(1025):
            (legacy / str(i)).touch()
        report = maintain_download_parts(self.root, apply=True, max_entries=2048)
        self.assertIn('capacity_blocked', report)
        self.assertTrue(a.directory.exists())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'Table.enUS.parts'

    def attempt(self, build='1', count=3, payload=2000):
        identity = dict(branch='retail', build=build, table='Table', locale='enUS')
        p = PartDownload(self.root, identity, count, 1, True, per_page=1)
        for i in range(count):
            p.save(i, [{'ID': str(i + 1), 'Name': 'x' * payload}])
        p.complete(dict(identity=identity, rows=count, sha256='a' * 64, url='url', headers=['ID', 'Name']))
        proof = read_json(p.directory / 'complete.json')
        proof['completed_at'] = time.time() - 200000
        atomic_json(p.directory / 'complete.json', proof)
        for f in p.directory.iterdir():
            os.utime(f, (time.time() - 200000,) * 2)
        PartDownload(self.root, identity, count, 1, True, per_page=1)
        return p

    def test_capacity_is_not_read_budget(self):
        self.root.mkdir()
        with (self.root / 'large').open('wb') as f:
            f.truncate(129 * 1024 * 1024)
        check_root_budget(self.root)
        with self.assertRaises(ValueError):
            check_root_budget(self.root, max_bytes=100)

    def test_tail_corruption_prevents_entire_batch(self):
        a = self.attempt('1')
        b = self.attempt('2')
        (b.directory / '2-3.json').write_text('bad')
        os.utime(b.directory / '2-3.json', (time.time() - 200000,) * 2)
        report = maintain_download_parts(self.root, apply=True)
        self.assertEqual(report['deleted_attempts'], 0)
        self.assertTrue((a.directory / '0-3.json').exists())

    def test_large_attempt_makes_bounded_progress(self):
        a = self.attempt(count=8)
        for _ in range(20):
            report = maintain_download_parts(self.root, apply=True, max_bytes=10000)
            self.assertLessEqual(report.get('read_bytes', 0), 10000)
            if not a.directory.exists():
                break
        self.assertFalse(a.directory.exists(), report)

    def test_interrupted_unlink_is_recoverable(self):
        a = self.attempt()
        original = Path.unlink
        calls = []
        def interrupted(path, *args, **kwargs):
            if path.parent == a.directory and path.name.endswith('-3.json'):
                calls.append(path)
                if len(calls) == 2:
                    raise OSError('injected interruption')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', interrupted):
            maintain_download_parts(self.root, apply=True)
        self.assertTrue((a.directory / 'pending-delete.json').exists())
        report = maintain_download_parts(self.root, apply=True)
        self.assertFalse(a.directory.exists(), report)

    def test_global_entry_budget_preflights_all_scopes(self):
        a = self.attempt('1')
        b = self.attempt('2')
        report = maintain_download_parts(self.root, apply=True, max_entries=12)
        self.assertEqual(report['deleted_attempts'], 0)
        self.assertTrue(a.directory.exists())
        self.assertTrue(b.directory.exists())

    def test_shared_read_budget_across_scopes(self):
        a = self.attempt('1')
        b = self.attempt('2')
        report = maintain_download_parts(self.root, apply=True, max_bytes=12000)
        self.assertLessEqual(report['read_bytes'], 12000)
        self.assertTrue(a.directory.exists() or b.directory.exists())

    def test_rmdir_failure_replays_external_intent_not_unknown_empty(self):
        a = self.attempt()
        unknown = a.root / ('f' * 32)
        unknown.mkdir()
        original = Path.rmdir
        def fail(path):
            if path == a.directory:
                raise OSError('injected final rmdir')
            return original(path)
        with patch.object(Path, 'rmdir', fail):
            report = maintain_download_parts(self.root, apply=True)
        self.assertIn('blocked', report)
        report = maintain_download_parts(self.root, apply=True)
        self.assertFalse(a.directory.exists(), report)
        self.assertTrue(unknown.exists())

    def test_metadata_prescan_resumes_without_deleting_unscanned_tail(self):
        attempts = [self.attempt(str(i), count=3, payload=5000) for i in range(8)]
        reports = []
        for _ in range(40):
            report = maintain_download_parts(self.root, apply=True, max_bytes=9000)
            reports.append(report)
            self.assertLessEqual(report['read_bytes'], 9000)
            self.assertLessEqual(report['scanned_entries'], 1024)
            if all(not a.directory.exists() for a in attempts):
                break
        self.assertTrue(all(not a.directory.exists() for a in attempts), reports[-1])

    def test_new_attempt_reserves_expected_members(self):
        self.root.mkdir()
        for i in range(1015):
            (self.root / str(i)).touch()
        with self.assertRaisesRegex(ValueError, 'budget'):
            PartDownload(self.root, dict(branch='retail', build='1', table='Table', locale='enUS'),
                         20, 1, True, per_page=1)

    def test_entry_prescan_resumes_and_dry_run_does_not_write_checkpoint(self):
        attempts = [self.attempt(str(i)) for i in range(4)]
        from botend.services.download_retention import CHECKPOINT
        checkpoint = self.root.parent / ('.' + self.root.name + CHECKPOINT)
        maintain_download_parts(self.root, apply=False, max_entries=12)
        self.assertFalse(checkpoint.exists())
        for _ in range(20):
            report = maintain_download_parts(self.root, apply=True, max_entries=12)
            self.assertLessEqual(report['scanned_entries'], 12)
            if all(not a.directory.exists() for a in attempts):
                break
        self.assertTrue(all(not a.directory.exists() for a in attempts), report)

    def test_checkpoint_tamper_and_metadata_drift_never_authorize_deletion(self):
        from botend.services.download_retention import CHECKPOINT
        a, b = self.attempt('1'), self.attempt('2')
        maintain_download_parts(self.root, apply=True, max_entries=12)
        checkpoint = self.root.parent / ('.' + self.root.name + CHECKPOINT)
        checkpoint.write_bytes(checkpoint.read_bytes() + b'forged')
        (b.directory / 'complete.json').write_text('{}')
        report = maintain_download_parts(self.root, apply=True)
        self.assertIn('blocked', report)
        self.assertTrue((a.directory / '0-3.json').exists())

    def test_128k_metadata_backlog_progresses(self):
        attempts = [self.attempt(str(i), count=30, payload=5000) for i in range(24)]
        for _ in range(120):
            report = maintain_download_parts(self.root, apply=True, max_bytes=128 * 1024)
            self.assertLessEqual(report['read_bytes'], 128 * 1024)
            if all(not a.directory.exists() for a in attempts):
                break
        self.assertTrue(all(not a.directory.exists() for a in attempts), report)

    def test_unexplained_missing_member_is_not_recoverable(self):
        a = self.attempt()
        (a.directory / '0-3.json').unlink()
        report = maintain_download_parts(self.root, apply=True)
        self.assertEqual(report['deleted_attempts'], 0)
        self.assertTrue((a.directory / '1-3.json').exists())
