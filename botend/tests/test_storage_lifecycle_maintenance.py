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

    def attempt(self, table, root=None):
        root = root or self.base / 'downloads' / 'retail' / f'{table}.enUS.parts'
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

    def test_default_discovery_covers_production_tree_without_tooltip_budget(self):
        from django.conf import settings
        from botend.services.journal_source import WagoJournalSource
        from botend.services.storage_lifecycle_maintenance import run_extended_maintenance
        base = self.base / '.cache/wow_data_updates'
        expected = []
        preserved = []

        def files(directory, count, suffix):
            directory.mkdir(parents=True, exist_ok=True)
            for number in range(count):
                (directory / f'{number}{suffix}').write_bytes(b'{}')

        # Production metadata: 24,158 discovery entries, 23,941 tooltip JSONs.
        # Real files and hierarchy, not a mocked scandir/discovery result.
        for branch, slot, tooltips, talents, loot in (
                ('retail', 'a', 8791, 27, 8), ('ptr', 'a', 7575, 27, 4),
                ('ptr', 'b', 7575, 25, 4)):
            directory = base / branch / slot
            files(directory / 'current/wowhead' / ('live' if branch == 'retail' else 'ptr-2'),
                  tooltips, '-295.json')
            files(directory / 'talents/current', talents, '.csv')
            files(directory / 'loot-specializations', loot, '.csv')
            files(directory / 'source/current', 23, '.csv')
            (directory / 'source_build.txt').write_text('12.1.5.70391')
            for table in ('SpellEffect', 'SpellMisc'):
                root = directory / 'source/current' / f'{table}.zhCN.parts'
                root.mkdir()
                legacy = root / '0-1.json'
                legacy.write_bytes(b'legacy without completion proof')
                preserved.append((legacy, legacy.read_bytes()))
                expected.append(root.relative_to(base).as_posix())
        source = WagoJournalSource('12.1.5.70391', base / 'retail/a/source', offline=True)
        files(source.directory, 12, '.csv')
        root = source.directory / 'SpellMisc.zhCN.parts'
        current = PartDownload(root, source.identity('SpellMisc', 'zhCN'), 1, 1, False, per_page=1)
        current.save(0, [{'ID': '1', 'Name': 'in progress'}])
        preserved.extend((p, p.read_bytes()) for p in current.path.parent.rglob('*') if p.is_file())
        expected.append(root.relative_to(base).as_posix())
        # Independently count the unpruned tree, stopping at .parts as the service does.
        entries = 0
        for directory, dirs, names in os.walk(base):
            entries += len(dirs) + len(names)
            dirs[:] = [name for name in dirs if not name.endswith('.parts')]
        self.assertEqual(entries, 24158)
        with override_settings(BASE_DIR=self.base, DOWNLOAD_PARTS_GC_ENABLED=True,
                               DOWNLOAD_PARTS_GC_AUTO_APPLY=True, DOWNLOAD_PARTS_GC_ROOT_LIMIT=8):
            del settings.DOWNLOAD_PARTS_GC_BASE  # Exercise the real default, not the override.
            first = run_extended_maintenance()
            self.assertEqual([r.get('root') for r in first], sorted(expected), first)
            self.assertTrue(all(r['status'] == 'ok' and r['deleted'] == 0 for r in first), first)
            second = run_extended_maintenance()
        self.assertEqual([r.get('root') for r in second], sorted(expected), second)
        self.assertTrue(all(p.read_bytes() == original for p, original in preserved))
        self.assertEqual(read_json(self.base / 'control/state.json')['download_after'], '')

    def test_default_cold_layouts_progress_without_refresh(self):
        from django.conf import settings
        from botend.services.journal_source import WagoJournalSource
        from botend.services.storage_lifecycle_maintenance import run_extended_maintenance
        base = self.base / '.cache/wow_data_updates'
        source = WagoJournalSource('12.1.0.101', base / 'beta/b/source', offline=True)
        roots = [self.attempt('Table', directory / 'Table.enUS.parts') for directory in (
            source.directory, base / 'retail/b/loot-specializations',
            base / 'ptr/a/source/current/wowhead',  # Not the catalog tooltip subtree.
        )]
        original = [manifest.read_bytes() for _, _, manifest in roots]
        with override_settings(BASE_DIR=self.base, DOWNLOAD_PARTS_GC_ENABLED=True,
                               DOWNLOAD_PARTS_GC_AUTO_APPLY=True):
            del settings.DOWNLOAD_PARTS_GC_BASE
            results = [run_extended_maintenance() for _ in roots]
        self.assertEqual(sum(r['deleted'] for batch in results for r in batch), len(roots))
        self.assertEqual(len({r['root'] for batch in results for r in batch}), len(roots))
        for (_, old, manifest), content in zip(roots, original):
            self.assertFalse(old.exists())
            self.assertEqual(manifest.read_bytes(), content)
        self.assertEqual(read_json(self.base / 'control/state.json')['download_after'], '')

    def test_explicit_base_keeps_recursive_override_even_for_tooltip_like_paths(self):
        from botend.services.storage_lifecycle_maintenance import run_extended_maintenance
        default = self.base / '.cache/wow_data_updates'
        _, outside, _ = self.attempt('Outside', default / 'retail/a/source/Outside.enUS.parts')
        custom = self.base / 'downloads'
        _, old, manifest = self.attempt('Table', custom / 'retail/a/current/wowhead/Table.enUS.parts')
        original = manifest.read_bytes()
        with override_settings(BASE_DIR=self.base, DOWNLOAD_PARTS_GC_ENABLED=True,
                               DOWNLOAD_PARTS_GC_AUTO_APPLY=True):
            result = run_extended_maintenance()
        self.assertEqual(result[0]['deleted'], 1, result)
        self.assertFalse(old.exists())
        self.assertTrue(outside.exists())
        self.assertEqual(manifest.read_bytes(), original)
        # An explicit override equal to the default is still an arbitrary recursive base.
        _, old, _ = self.attempt('Table', default / 'retail/a/current/wowhead/Table.enUS.parts')
        with override_settings(BASE_DIR=self.base, DOWNLOAD_PARTS_GC_BASE=default,
                               DOWNLOAD_PARTS_GC_ENABLED=True, DOWNLOAD_PARTS_GC_AUTO_APPLY=True,
                               DOWNLOAD_PARTS_GC_ROOT_LIMIT=8):
            result = run_extended_maintenance()
        self.assertFalse(old.exists(), result)
        self.assertFalse(outside.exists(), result)

    def test_default_discovery_budget_and_links_still_fail_closed(self):
        from django.conf import settings
        from botend.services.storage_lifecycle_maintenance import run_extended_maintenance
        base = self.base / '.cache/wow_data_updates'
        base.mkdir(parents=True)
        for number in range(2048):
            (base / f'{number}.csv').touch()
        with override_settings(BASE_DIR=self.base, DOWNLOAD_PARTS_GC_ENABLED=True):
            del settings.DOWNLOAD_PARTS_GC_BASE
            self.assertEqual(run_extended_maintenance(), [])
            (base / 'over-budget.csv').touch()
            result = run_extended_maintenance()
            self.assertEqual(result[0]['status'], 'refused', result)
            self.assertIn('entry budget exceeded', result[0]['error'])
            for path in base.iterdir():
                path.unlink()
            tooltip = base / 'retail/a/current/wowhead'
            tooltip.parent.mkdir(parents=True)
            outside = self.base / 'outside'
            outside.mkdir()
            sentinel = outside / 'preserve.json'
            sentinel.write_bytes(b'preserve')
            tooltip.symlink_to(outside, target_is_directory=True)
            result = run_extended_maintenance()
            self.assertEqual(result[0]['status'], 'refused', result)
            self.assertIn('linked', result[0]['error'])
            self.assertEqual(sentinel.read_bytes(), b'preserve')

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
