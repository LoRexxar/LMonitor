import json
import os
from pathlib import Path
import tempfile
import time
from unittest import mock

from django.test import SimpleTestCase

from botend.services import snapshot_cache_gc as gc
from botend.services.simc_benchmark_result_snapshot import _lock, _write


class SnapshotCacheGCTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def content(self, body):
        body = {'schema': 1, **body}
        name = gc.digest(body) + '.json'
        _write(self.root / name, body)
        return name

    def news(self, number):
        home = self.content({'sections': {k: [] for k in gc.NEWS_SECTIONS}})
        shard = self.content({'items': [{'source': 'wowhead', 'id': number}]})
        body = {'schema': 1, 'home': home, 'shards': [
            {'file': shard, 'count': 1, 'sources': {'wowhead': 1}, 'month': '2026-10'}],
            'sources': {'wowhead': 1}}
        return {**body, 'generation': gc.digest(body), 'generated_at': '2026-10-01'}, shard

    def old(self, name):
        os.utime(self.root / name, (time.time() - 200000,) * 2)

    def run_gc(self, **kwargs):
        return gc.maintain_snapshot_cache('news', root=self.root, **kwargs)

    def test_idle_maintenance_is_dry_run_until_explicit_apply_setting(self):
        from botend.services.snapshot_cache_maintenance import run_maintenance_once
        index, current = self.news(1)
        _write(self.root / 'index.json', index)
        self.run_gc(apply=True)
        orphan = self.content({'items': [{'id': 0}]})
        self.old(orphan)
        with self.settings(NEWS_SNAPSHOT_ROOT=self.root,
                           RIO_RANKINGS_SNAPSHOT_ROOT=self.root / 'no-rio',
                           JOURNAL_SNAPSHOT_ROOT=self.root / 'no-journal',
                           SNAPSHOT_CACHE_GC_AUTO_APPLY=False):
            results = run_maintenance_once()
            news = next(row for row in results if row['kind'] == 'news')
            self.assertEqual(news['deleted'], 0)
            self.assertTrue((self.root / orphan).exists())
        with self.settings(NEWS_SNAPSHOT_ROOT=self.root,
                           RIO_RANKINGS_SNAPSHOT_ROOT=self.root / 'no-rio',
                           JOURNAL_SNAPSHOT_ROOT=self.root / 'no-journal',
                           SNAPSHOT_CACHE_GC_AUTO_APPLY=True):
            results = run_maintenance_once()
            news = next(row for row in results if row['kind'] == 'news')
            self.assertEqual(news['deleted'], 1)
            self.assertTrue((self.root / current).exists())

    def test_bootstrap_dry_run_retention_and_reentry(self):
        index, current = self.news(1)
        _write(self.root / 'index.json', index)
        orphan = self.content({'items': [{'id': 0}]})
        self.old(orphan)
        self.assertEqual(self.run_gc()['status'], 'bootstrap_required')
        self.assertFalse((self.root / gc.HISTORY).exists())
        self.assertEqual(self.run_gc(apply=True)['status'], 'initialized')
        self.assertTrue((self.root / orphan).exists())
        self.assertEqual(self.run_gc()['candidates'][0]['file'], orphan)
        self.assertEqual(self.run_gc(apply=True)['deleted'], 1)
        self.assertTrue((self.root / current).exists())
        self.assertEqual(self.run_gc(apply=True)['deleted'], 0)

    def test_persistent_previous_generations_and_limit(self):
        names = []
        for number in range(5):
            index, name = self.news(number)
            names.append(name)
            with _lock(self.root / 'publish.lock'), mock.patch.object(gc.time, 'time', return_value=time.time() - 200000):
                gc.publish_snapshot_index('news', self.root, index)
            self.old(name)
        fresh = self.content({'items': [{'id': 99}]})
        result = self.run_gc(apply=True, limit=1)
        self.assertEqual(result['deleted'], 1)
        self.assertTrue(all((self.root / n).exists() for n in names[-3:]))
        self.assertTrue((self.root / fresh).exists())
        self.assertEqual(self.run_gc(apply=True, limit=1)['deleted'], 1)

    def test_bad_pointer_symlink_and_batch_fail_closed(self):
        index, _ = self.news(1)
        _write(self.root / 'index.json', index)
        self.run_gc(apply=True)
        orphan = self.content({'items': []})
        self.old(orphan)
        bad = 'f' * 64 + '.json'
        (self.root / bad).write_text('{broken')
        self.old(bad)
        self.assertEqual(self.run_gc(apply=True)['status'], 'blocked')
        self.assertTrue((self.root / orphan).exists())
        (self.root / bad).unlink()
        (self.root / 'unexpected-link').symlink_to(self.root / orphan)
        self.assertEqual(self.run_gc(apply=True)['status'], 'blocked')
        (self.root / 'unexpected-link').unlink()
        _write(self.root / 'index.json', {**index, 'home': '../outside.json'})
        self.assertEqual(self.run_gc(apply=True)['status'], 'blocked')
        self.assertTrue((self.root / orphan).exists())

    def test_unknown_content_shape_is_not_a_deletion_candidate(self):
        index, _ = self.news(1)
        gc.publish_snapshot_index('news', self.root, index)
        for body in ({'backup': 'not a projection'}, {'items': 'broken'},
                     {'items': [], 'unknown_reference': 'other.json'}):
            with self.subTest(body=body):
                name = self.content(body)
                self.old(name)
                result = self.run_gc(apply=True)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['deleted'], 0)
                self.assertTrue((self.root / name).exists())
                (self.root / name).unlink()

    def test_dangling_history_symlink_is_not_bootstrapped(self):
        index, _ = self.news(1)
        _write(self.root / 'index.json', index)
        (self.root / gc.HISTORY).symlink_to(self.root / 'missing-history')
        result = self.run_gc(apply=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue((self.root / gc.HISTORY).is_symlink())
        self.assertFalse((self.root / 'missing-history').exists())

    def test_unknown_journal_content_shape_is_rejected(self):
        name = self.content({'backup': 'not a journal projection'})
        with self.assertRaises(gc.UnsafeCache):
            gc.Reader(self.root, 'journal').content(name)

    def test_unknown_rio_content_shape_is_rejected(self):
        name = 'a' * 32 + '.json'
        _write(self.root / name, {'schema': 1, 'coordinate': ['mplus', 's1', 'world'],
                                'season': 's1', 'region': 'world', 'items': [],
                                'unknown_reference': 'other.json'})
        with self.assertRaises(gc.UnsafeCache):
            gc.Reader(self.root, 'rio').content(name)

    def test_publisher_lock_blocks_gc(self):
        index, _ = self.news(1)
        _write(self.root / 'index.json', index)
        with _lock(self.root / 'publish.lock'):
            self.assertEqual(self.run_gc(apply=True)['status'], 'busy')

    def test_pointer_recheck_prevents_delete(self):
        index, _ = self.news(1)
        _write(self.root / 'index.json', index)
        self.run_gc(apply=True)
        orphan = self.content({'items': []})
        self.old(orphan)
        original = gc._unchanged
        def changed(root, fingerprints):
            (root / 'index.json').write_text('{}')
            return original(root, fingerprints)
        with mock.patch.object(gc, '_unchanged', side_effect=changed):
            self.assertEqual(self.run_gc(apply=True)['status'], 'blocked')
        self.assertTrue((self.root / orphan).exists())

    def test_grace_starts_when_last_reference_retires(self):
        names = []
        for number in range(5):
            index, name = self.news(number)
            names.append(name)
            gc.publish_snapshot_index('news', self.root, index)
            self.old(name)
        self.assertEqual(self.run_gc(apply=True)['deleted'], 0)
        self.assertTrue(all((self.root / name).exists() for name in names))

    def test_unknown_files_and_resource_budgets(self):
        index, _ = self.news(1)
        _write(self.root / 'index.json', index)
        self.run_gc(apply=True)
        for name in ('source.json', 'backup.sql', '.writing-unknown'):
            (self.root / name).write_text('keep')
            self.old(name)
        with self.settings(SNAPSHOT_CACHE_GC_SCAN_LIMIT=1):
            self.assertEqual(self.run_gc(apply=True)['status'], 'blocked')
        with self.settings(SNAPSHOT_CACHE_GC_MAX_BYTES=1):
            self.assertEqual(self.run_gc(apply=True)['status'], 'blocked')
        self.assertEqual(self.run_gc(apply=True)['deleted'], 0)
        self.assertTrue((self.root / 'backup.sql').exists())

    def test_interrupted_pointer_commit_keeps_history_recoverable(self):
        first, first_name = self.news(1)
        gc.publish_snapshot_index('news', self.root, first)
        second, _ = self.news(2)
        real_write = gc._write
        def fail_finalize(path, body):
            if path.name == gc.HISTORY and body['current'] == second:
                raise OSError('crash after index commit')
            return real_write(path, body)
        with mock.patch.object(gc, '_write', side_effect=fail_finalize), self.assertRaises(OSError):
            gc.publish_snapshot_index('news', self.root, second)
        self.old(first_name)
        self.assertEqual(self.run_gc(apply=True)['status'], 'ok')
        third, _ = self.news(3)
        gc.publish_snapshot_index('news', self.root, third)
        history = json.loads((self.root / gc.HISTORY).read_text())
        self.assertEqual(history['previous'], [second, first])

    def test_rio_all_scopes_hash_validation(self):
        import hashlib
        import uuid
        def content(season):
            body = {'schema': 1, 'coordinate': ['mplus', season, 'world'], 'season': season,
                    'region': 'world', 'items': [{'id': 1}], 'dungeons': [], 'runs_by_dungeon': {}}
            content_hash = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
            body['generated_at'] = '2026-10-01'
            name = uuid.uuid4().hex + '.json'
            _write(self.root / name, body)
            key = hashlib.sha256(json.dumps(body['coordinate']).encode()).hexdigest()
            return key, {'file': name, 'content_hash': content_hash, 'generated_at': body['generated_at']}
        entries = dict([content('old'), content('new')])
        gc.publish_snapshot_index('rio', self.root, {'schema': 1, 'current_season': 'new', 'entries': entries})
        _, orphan = content('orphan')
        self.old(orphan['file'])
        self.assertEqual(gc.maintain_snapshot_cache('rio', root=self.root, apply=True)['deleted'], 1)
        bad = next(iter(entries.values()))['file']
        (self.root / bad).write_text('{}')
        self.assertEqual(gc.maintain_snapshot_cache('rio', root=self.root, apply=True)['status'], 'blocked')

    def test_journal_transitive_skill_reference_and_symlink(self):
        coordinate = {'release_id': 1, 'instance_id': 10, 'source': 'test'}
        skill = self.content({'coordinate': {**coordinate, 'boss_id': 20, 'difficulty': 2},
                              'boss': {'id': 20}, 'sections': [], 'roles': [], 'tooltips': {}})
        instance = self.content({'coordinate': coordinate, 'instance': {'id': 10},
                                 'bosses': [{'id': 20, 'difficulty_ids': [2], 'skills': {'2': skill}}],
                                 'references': {}, 'loot_versions': {}})
        release = {'id': 1}
        catalog = self.content({'release': release, 'instances': [{'id': 10}], 'tiers': [], 'season_label': ''})
        body = {'schema': 1, 'source_version': 'test', 'release': release, 'art_ids': [],
                'difficulties': [], 'instances': {'10': instance}, 'catalog': catalog}
        index = {**body, 'generation': gc.digest(body), 'generated_at': '2026-10-01'}
        gc.publish_snapshot_index('journal', self.root, index)
        self.old(skill)
        self.assertEqual(gc.maintain_snapshot_cache('journal', root=self.root, apply=True)['deleted'], 0)
        outside = self.root.parent / (self.root.name + '-outside')
        outside.write_bytes((self.root / skill).read_bytes())
        self.addCleanup(outside.unlink)
        (self.root / skill).unlink()
        (self.root / skill).symlink_to(outside)
        self.assertEqual(gc.maintain_snapshot_cache('journal', root=self.root, apply=True)['status'], 'blocked')
        self.assertTrue(outside.exists())

    def test_shared_closures_charge_each_file_once(self):
        first, _ = self.news(1)
        gc.publish_snapshot_index('news', self.root, first)
        second, _ = self.news(2)
        gc.publish_snapshot_index('news', self.root, second)
        third, _ = self.news(3)
        # Exactly enough for every unique content and both control files, not
        # enough for repeated new/old/history/retired/previous validation.
        budget = sum(path.stat().st_size for path in self.root.iterdir()
                     if path.name != 'publish.lock')
        with self.settings(SNAPSHOT_CACHE_GC_MAX_BYTES=budget):
            with _lock(self.root / 'publish.lock'):
                gc.publish_snapshot_index('news', self.root, third)
        self.assertFalse((self.root / gc.BLOCKED).exists())
        history = json.loads((self.root / gc.HISTORY).read_text())
        self.assertEqual(history['previous'], [second, first])
        self.assertEqual(history['current'], third)

    def test_cached_closure_budget_and_identity_guards(self):
        index, shard = self.news(1)
        refs = {index['home'], shard}
        budget = sum((self.root / name).stat().st_size for name in refs)
        with self.settings(SNAPSHOT_CACHE_GC_MAX_BYTES=budget):
            reader = gc.Reader(self.root, 'news')
            self.assertEqual(reader.closure(index), refs)
            self.assertEqual(reader.closure(dict(index)), refs)
            self.assertEqual(reader.bytes, budget)
            extra = self.content({'items': [{'source': 'new'}]})
            with self.assertRaisesRegex(gc.UnsafeCache, 'byte budget'):
                reader.metadata(extra)
        for name in refs:
            with self.subTest(name=name):
                reader = gc.Reader(self.root, 'news')
                reader.closure(index)
                body = (self.root / name).read_bytes()
                replacement = self.root / 'replacement'
                replacement.write_bytes(body)
                replacement.replace(self.root / name)
                with self.assertRaisesRegex(gc.UnsafeCache, 'file changed'):
                    reader.closure(index)
                with self.assertRaisesRegex(gc.UnsafeCache, 'file changed'):
                    reader.metadata(name)
                with self.assertRaisesRegex(gc.UnsafeCache, 'file changed'):
                    gc._unchanged(self.root, reader.fingerprints)

    def test_command_defaults_to_dry_run(self):
        from io import StringIO
        from django.core.management import call_command
        index, _ = self.news(1)
        _write(self.root / 'index.json', index)
        output = StringIO()
        with self.settings(NEWS_SNAPSHOT_ROOT=self.root):
            call_command('maintain_snapshot_caches', kind='news', stdout=output)
        self.assertEqual(json.loads(output.getvalue())[0]['status'], 'bootstrap_required')
        self.assertFalse((self.root / gc.HISTORY).exists())
