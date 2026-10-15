"""Real idle scheduling and file retention, without crawler or rebuild calls."""
import hashlib
import os
import tempfile
import time
from pathlib import Path
from django.test import SimpleTestCase, override_settings
from botend.services.download_checkpoint import PartDownload, atomic_json, read_json


class StorageLifecycleMaintenanceTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.settings = override_settings(
            STORAGE_MAINTENANCE_ROOT=self.base / 'control',
            DOWNLOAD_PARTS_GC_BASE=self.base / 'downloads',
            SPEC_STATS_GC_ENABLED=False, MYTHICSTATS_GC_ENABLED=False,
            DOWNLOAD_PARTS_GC_ENABLED=False, DOWNLOAD_PARTS_GC_AUTO_APPLY=False,
            DOWNLOAD_PARTS_GC_ROOT_LIMIT=1,
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)

    def attempt(self, table):
        root = self.base / 'downloads' / 'retail' / f'{table}.enUS.parts'
        identity = dict(branch='retail', build='12.1.0.101', table=table, locale='enUS')
        first = PartDownload(root, identity, 1, 1, True, per_page=1)
        first.save(0, [{'ID': '1', 'Name': 'fixture'}])
        csv = b'ID,Name\r\n1,fixture\r\n'
        first.complete(dict(identity=identity, rows=1, sha256=hashlib.sha256(csv).hexdigest(),
                            url='http://fixture.invalid/table', headers=['ID', 'Name']))
        proof = read_json(first.directory / 'complete.json')
        proof['completed_at'] = time.time() - 200000
        atomic_json(first.directory / 'complete.json', proof)
        for path in first.directory.iterdir():
            os.utime(path, (time.time() - 200000,) * 2)
        current = PartDownload(root, identity, 1, 1, True, per_page=1)
        return root, first.directory, current.path

    def test_disabled_has_no_filesystem_side_effects(self):
        from botend.services.storage_lifecycle_maintenance import run_extended_maintenance
        self.assertEqual(run_extended_maintenance(), [])
        self.assertEqual(list(self.base.iterdir()), [])

    def test_idle_download_maintenance_is_opt_in_and_preserves_current(self):
        from botend.services.storage_lifecycle_maintenance import run_extended_maintenance
        root, old, manifest = self.attempt('Table')
        original = manifest.read_bytes()
        with override_settings(DOWNLOAD_PARTS_GC_ENABLED=True):
            dry = run_extended_maintenance()
            self.assertTrue(old.exists())
            self.assertEqual(dry[0]['deleted'], 0)
            with override_settings(DOWNLOAD_PARTS_GC_AUTO_APPLY=True):
                applied = run_extended_maintenance()
        self.assertFalse(old.exists(), applied)
        self.assertEqual(applied[0]['deleted'], 1)
        self.assertEqual(manifest.read_bytes(), original)

    def test_persistent_cursor_visits_cold_roots_without_rebuild(self):
        from botend.services.storage_lifecycle_maintenance import run_extended_maintenance
        roots = [self.attempt(name) for name in ('Alpha', 'Beta')]
        with override_settings(DOWNLOAD_PARTS_GC_ENABLED=True, DOWNLOAD_PARTS_GC_AUTO_APPLY=True):
            first = run_extended_maintenance()
            self.assertEqual(sum(old.exists() for _, old, _ in roots), 1)
            second = run_extended_maintenance()
        self.assertEqual(first[0]['deleted'] + second[0]['deleted'], 2)
        self.assertTrue(all(not old.exists() for _, old, _ in roots))
        self.assertTrue((self.base / 'control' / 'state.json').is_file())

    def test_existing_idle_worker_calls_new_domains(self):
        from botend.services.snapshot_cache_maintenance import run_maintenance_once
        _, old, _ = self.attempt('Table')
        with override_settings(
            DOWNLOAD_PARTS_GC_ENABLED=True, DOWNLOAD_PARTS_GC_AUTO_APPLY=True,
            SNAPSHOT_CACHE_GC_AUTO_APPLY=False,
            NEWS_SNAPSHOT_ROOT=self.base / 'news',
            RIO_RANKINGS_SNAPSHOT_ROOT=self.base / 'rio',
            JOURNAL_SNAPSHOT_ROOT=self.base / 'journal',
        ):
            results = run_maintenance_once()
        self.assertFalse(old.exists(), results)
        self.assertEqual(sum(r['deleted'] for r in results if r['kind'] == 'download_parts'), 1)

    def test_aggregate_paused_work_is_reported_and_revisited(self):
        from unittest.mock import patch
        from botend.services import spec_stats_retention as retention
        from botend.services.spec_stats_snapshot import publish_projection
        from botend.services.simc_benchmark_result_snapshot import _write
        from botend.services.storage_lifecycle_maintenance import run_extended_maintenance
        media = self.base / 'media'
        scope = media / 'aggregated/3/Druid/Restoration'
        for number in range(5):
            publish_projection(scope / 'dungeon.json', {'dungeons': [
                {'dungeon_id': 1, 'sample_size': number, 'padding': 'x' * 6000}]}, _write)
        with override_settings(MEDIA_ROOT=media, SPEC_STATS_GC_ENABLED=True,
                               SPEC_STATS_GC_AUTO_APPLY=True), patch.object(retention, 'MAX_BYTES', 18000):
            first = run_extended_maintenance()
            self.assertEqual(first[0]['status'], 'paused', first)
            for _ in range(12):
                result = run_extended_maintenance()
                if result[0]['status'] == 'ok':
                    break
            self.assertEqual(result[0]['status'], 'ok', result)
        self.assertEqual(len(list((scope / 'stats-details').iterdir())), 5)

    def test_empty_media_root_uses_same_default_as_publisher(self):
        from botend.services.spec_stats_snapshot import publish_projection
        from botend.services.simc_benchmark_result_snapshot import _write
        from botend.services.storage_lifecycle_maintenance import run_extended_maintenance
        scope = self.base / 'media/aggregated/3/Druid/Restoration'
        publish_projection(scope / 'dungeon.json', {'dungeons': [
            {'dungeon_id': 1, 'sample_size': 1}]}, _write)
        original = (scope / 'dungeon.json').read_bytes()
        previous = os.getcwd()
        try:
            os.chdir(self.base)
            with override_settings(MEDIA_ROOT='', SPEC_STATS_GC_ENABLED=True,
                                   SPEC_STATS_GC_AUTO_APPLY=False):
                result = run_extended_maintenance()
        finally:
            os.chdir(previous)
        self.assertEqual(result[0]['scopes'], 1, result)
        self.assertEqual(result[0]['status'], 'ok', result)
        self.assertEqual((scope / 'dungeon.json').read_bytes(), original)
        self.assertFalse((self.base / 'aggregated').exists())

    def test_linked_coordinator_lock_refuses_without_touching_target(self):
        from botend.services.storage_lifecycle_maintenance import run_extended_maintenance
        control = self.base / 'control'
        control.mkdir()
        target = self.base / 'outside'
        (control / 'worker.lock').symlink_to(target)
        with override_settings(DOWNLOAD_PARTS_GC_ENABLED=True):
            result = run_extended_maintenance()
        self.assertFalse(target.exists())
        self.assertEqual(result[0]['status'], 'refused')
