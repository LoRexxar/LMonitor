import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from botend.services import mythicstats_retention as retention
from botend.services.simc_benchmark_result_snapshot import _write, _lock
from botend.services.spec_stats_snapshot import content_digest


class RetentionTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        settings = override_settings(MYTHICSTATS_SNAPSHOT_ROOT=self.root)
        settings.enable()
        self.addCleanup(settings.disable)
        self.serial = 0
        self.clock = patch.object(retention.time, 'time', return_value=1000000)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def body(self, season='s1'):
        self.serial += 1
        name = f'{self.serial:032x}.json'
        body = {'season': season, 'dungeon_id': 0, 'active_period': 103,
                'roles': {role: [{**{key: '' for key in retention.ROW_REQUIRED},
                                  'spec_slug': 'test-spec', 'avg_value': self.serial}] for role in retention.ROLES},
                'source_note': '', 'key_min': None, 'key_max': None, 'generated_at': 'test'}
        _write(self.root / name, body)
        os.utime(self.root / name, (1, 1))
        return {'file': name, 'origin': 'collector', 'content_hash': content_digest(body)}

    def index(self, entries):
        return {'schema': 1, 'current_season': next(iter(entries), ''), 'seasons': {
            season: {'periods': [], 'dungeons': [], 'shards': {'0:103': entry}} for season, entry in entries.items()}}

    def publish(self, entries):
        retention.publish_index(self.index(entries))

    def state(self):
        return json.loads((self.root / retention.STATE).read_bytes())

    def test_hashless_gate_all_public_entrypoints_before_any_side_effect(self):
        from botend.services import mythicstats_snapshot as snapshots
        self.legacy_current()
        _write(self.root / retention.STATE, {'bad_history': 'must not repair'})
        _write(self.root / retention.BLOCKED, {'reason': 'must stay sticky'})
        original = json.loads((self.root / 'index.json').read_bytes())
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        for publish in (lambda: retention.publish_index(self.index({})),
                        lambda: retention.publish_index(original),
                        snapshots.collect_snapshots, snapshots.publish_database_snapshots):
            with self.subTest(publish=publish), \
                 patch.object(snapshots.source, 'fetch_mythicstats_dps', side_effect=AssertionError('network forbidden')) as fetch, \
                 patch.object(snapshots, '_manifest', side_effect=AssertionError('manifest forbidden')) as manifest, \
                 patch.object(snapshots, '_write_shard', side_effect=AssertionError('body forbidden')) as body:
                with self.assertRaises(retention.AdoptionRequired):
                    publish()
                fetch.assert_not_called()
                manifest.assert_not_called()
                body.assert_not_called()
                self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()})

    def test_new_hashless_index_rejected_without_control_writes(self):
        entry = self.body()
        del entry['content_hash']
        with self.assertRaises(retention.Unsafe):
            self.publish({'s1': entry})
        self.assertEqual(set(self.file_bytes()), {entry['file'], 'worker.lock'})

    def test_gate_budget_is_unresolved_without_sticky_or_pointer_write(self):
        self.legacy_current()
        before = self.file_bytes()
        with patch.object(retention, 'MAX_BYTES', 1):
            with self.assertRaises(retention.PublicationDeferred):
                retention.publish_index(self.index({}))
        self.assertEqual(before, self.file_bytes())

    def test_adoption_unblocks_same_process_and_preserves_first_predecessor(self):
        self.legacy_current()
        with self.assertRaises(retention.AdoptionRequired):
            retention.publish_index(self.index({}))
        self.assertFalse(retention.adopt_current_hashes(apply=True)['blocked'])
        adopted = json.loads((self.root / 'index.json').read_bytes())
        self.publish({'s1': self.body()})
        self.assertEqual(self.state()['history'][0]['index'], adopted)
        self.assertFalse((self.root / retention.BLOCKED).exists())

    def test_gate_invalid_legacy_current_repairs_instead_of_requesting_adoption(self):
        original_entry = self.body()
        for damage in ('null-hash', 'empty-hash', 'wrong-hash', 'unknown', 'missing-body', 'scope', 'null-index'):
            with self.subTest(damage=damage):
                entry = deepcopy(original_entry)
                index = self.index({'s1': entry})
                entry.pop('content_hash')
                if damage in ('null-hash', 'empty-hash', 'wrong-hash'):
                    entry['content_hash'] = {'null-hash': None, 'empty-hash': '', 'wrong-hash': '0' * 64}[damage]
                elif damage == 'unknown':
                    index['unknown'] = True
                elif damage == 'missing-body':
                    entry['file'] = 'f' * 32 + '.json'
                elif damage == 'scope':
                    index['seasons']['s1']['shards'] = {'1:103': entry}
                else:
                    index = None
                _write(self.root / 'index.json', index)
                self.publish({'s1': self.body()})
                self.assertTrue((self.root / retention.BLOCKED).exists())
                self.assertEqual(json.loads((self.root / 'index.json').read_bytes())['current_season'], 's1')

    def test_gate_transient_io_and_read_identity_drift_are_zero_write_deferred(self):
        import errno
        self.legacy_current()
        before = self.file_bytes()
        actual_open = os.open
        def fail_read(path, *args, **kwargs):
            if Path(path).name == 'index.json':
                raise OSError(errno.EIO, 'injected temporary IO')
            return actual_open(path, *args, **kwargs)
        with patch.object(retention.os, 'open', fail_read):
            with self.assertRaises(retention.PublicationDeferred):
                retention.publish_index(self.index({}))
        self.assertEqual(before, self.file_bytes())
        self.assertFalse(retention.adopt_current_hashes(apply=True)['blocked'])
        with retention._worker_lock(self.root):
            reader = retention._publication_guard(self.root)
            (self.root / 'index.json').write_bytes((self.root / 'index.json').read_bytes() + b' ')
            drifted = self.file_bytes()
            with self.assertRaises(retention.PublicationDeferred):
                retention._publish_index_locked(self.root, self.index({}), publication_reader=reader)
        self.assertEqual(drifted, self.file_bytes())

    def test_guard_cache_reused_without_charging_current_body_twice(self):
        self.publish({'s1': self.body()})
        index = json.loads((self.root / 'index.json').read_bytes())
        with retention._worker_lock(self.root):
            reader = retention._publication_guard(self.root)
            current_bytes = reader.used
            budget = current_bytes + (self.root / retention.STATE).stat().st_size
            with patch.object(retention, 'MAX_BYTES', budget):
                retention._publish_index_locked(self.root, index, publication_reader=reader)
            self.assertEqual(reader.used, budget)
        self.assertFalse((self.root / retention.BLOCKED).exists())

    def test_discovery_failure_rechecks_pointer_before_error_marker_publish(self):
        from botend.services import mythicstats_snapshot as snapshots
        self.publish({'s1': self.body()})
        index = json.loads((self.root / 'index.json').read_bytes())
        index['seasons']['s1']['periods'] = [{'id': 103, 'label': 'week'}]
        retention.publish_index(index)
        before = self.file_bytes()
        def changed_source(**kwargs):
            (self.root / 'index.json').write_bytes(before['index.json'] + b' ')
            raise ValueError('source discovery failed')
        with patch.object(snapshots.source, 'fetch_mythicstats_dps', changed_source):
            with self.assertRaises(retention.PublicationDeferred):
                snapshots.collect_snapshots()
        self.assertEqual(self.file_bytes(), {**before, 'index.json': before['index.json'] + b' '})

    def test_missing_lock_only_initialization_and_scan_cli_propagation(self):
        from django.core.management import call_command, CommandError
        from io import StringIO
        from types import SimpleNamespace
        from botend.controller.plugins.portal.PortalMythicstatsDpsMonitor import PortalMythicstatsDpsMonitor
        self.legacy_current()
        (self.root / 'worker.lock').unlink()
        before = self.file_bytes()
        with self.assertRaises(retention.AdoptionRequired):
            retention.publish_index(self.index({}))
        self.assertEqual(self.file_bytes(), {**before, 'worker.lock': b''})
        monitor = object.__new__(PortalMythicstatsDpsMonitor)
        monitor.req = None
        monitor.task = SimpleNamespace(target='', flag='unchanged')
        self.assertFalse(monitor.scan(''))
        self.assertEqual(monitor.task.flag, 'unchanged')
        for from_database in (False, True):
            out = StringIO()
            with self.assertRaises(CommandError):
                call_command('refresh_mythicstats_snapshots', from_database=from_database, stdout=out)
            self.assertEqual(out.getvalue(), '')

    def test_rollback_excludes_current_before_selecting_two_predecessors(self):
        indexes = []
        for _ in range(4):
            indexes.append(self.index({'s1': self.body()}))
            retention.publish_index(indexes[-1])
        retention.publish_index(indexes[2])
        self.assertEqual([h['index'] for h in self.state()['history']], [indexes[3], indexes[1]])
        result = retention.collect_garbage(apply=True, now=2000000)
        self.assertFalse(result['blocked'], result)
        self.assertEqual(result['deleted'], [indexes[0]['seasons']['s1']['shards']['0:103']['file']])

    def test_dry_run_missing_root_and_lock_never_create(self):
        missing = self.root / 'missing' / 'scope'
        with override_settings(MYTHICSTATS_SNAPSHOT_ROOT=missing):
            result = retention.collect_garbage()
        self.assertFalse(missing.parent.exists())
        self.assertTrue(result['bootstrap'])
        result = retention.collect_garbage()
        self.assertTrue(result['blocked'], result)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_public_entrypoints_reject_lock_and_ancestor_links_before_writes(self):
        with tempfile.TemporaryDirectory() as outside:
            outside = Path(outside)
            lock = self.root / 'worker.lock'
            lock.symlink_to(outside / 'target')
            self.assertTrue(retention.collect_garbage()['blocked'])
            self.assertFalse((outside / 'target').exists())
            with self.assertRaises((OSError, retention.Unsafe)):
                retention.publish_index(self.index({}))
            self.assertFalse((outside / 'target').exists())
            lock.unlink()
            ancestor = self.root / 'link'
            ancestor.symlink_to(outside, target_is_directory=True)
            with override_settings(MYTHICSTATS_SNAPSHOT_ROOT=ancestor / 'new-scope'):
                self.assertTrue(retention.collect_garbage()['blocked'])
                with self.assertRaises((OSError, retention.Unsafe)):
                    retention.publish_index(self.index({}))
            self.assertEqual(list(outside.iterdir()), [])

    def test_current_only_adoption_preserves_bodies_history_and_sticky_marker(self):
        self.publish({'s1': self.body()})
        self.publish({'s1': self.body()})
        original = json.loads((self.root / 'index.json').read_bytes())
        del original['seasons']['s1']['shards']['0:103']['content_hash']
        _write(self.root / 'index.json', original)
        _write(self.root / retention.BLOCKED, {'schema': 1, 'reason': 'must stay sticky'})
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        dry = retention.adopt_current_hashes()
        self.assertEqual(dry['entries_updated'], 1)
        self.assertFalse(dry['apply'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        result = retention.adopt_current_hashes(apply=True)
        self.assertFalse(result['blocked'], result)
        migrated = json.loads((self.root / 'index.json').read_bytes())
        entry = migrated['seasons']['s1']['shards']['0:103']
        self.assertEqual(entry.pop('content_hash'), retention.Reader(self.root).read(entry['file'])[0]['content_hash'])
        self.assertEqual(migrated, original)
        for name, raw in before.items():
            if name != 'index.json':
                self.assertEqual((self.root / name).read_bytes(), raw)
        self.assertTrue(retention.collect_garbage()['blocked'])
        self.assertEqual(retention.adopt_current_hashes(apply=True)['entries_updated'], 0)

    def legacy_current(self):
        entry = self.body()
        del entry['content_hash']
        _write(self.root / 'index.json', self.index({'s1': entry}))
        with _lock(self.root / 'worker.lock'):
            pass
        return entry

    def file_bytes(self):
        return {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}

    def test_adoption_command_defaults_to_dry_run_and_no_manifest_is_created(self):
        from django.core.management import call_command
        from io import StringIO
        entry = self.legacy_current()
        before = self.file_bytes()
        out = StringIO()
        call_command('adopt_mythicstats_current_hashes', stdout=out)
        self.assertEqual(json.loads(out.getvalue())['entries_updated'], 1)
        self.assertEqual(before, self.file_bytes())
        call_command('adopt_mythicstats_current_hashes', apply=True, stdout=StringIO())
        self.assertFalse((self.root / retention.STATE).exists())
        self.assertEqual((self.root / entry['file']).read_bytes(), before[entry['file']])
        self.assertEqual(retention.adopt_current_hashes()['entries_updated'], 0)

    def test_adoption_validates_every_season_before_any_write(self):
        self.legacy_current()
        good = self.body('other-season')
        index = json.loads((self.root / 'index.json').read_bytes())
        index['seasons'].update(self.index({'other-season': good})['seasons'])
        original_body = (self.root / good['file']).read_bytes()
        for damage in ('bad-hash', 'unknown-field', 'scope', 'missing'):
            with self.subTest(damage=damage):
                changed = deepcopy(index)
                _write(self.root / 'index.json', changed)
                (self.root / good['file']).write_bytes(original_body)
                if damage == 'bad-hash':
                    changed['seasons']['other-season']['shards']['0:103']['content_hash'] = '0' * 64
                    _write(self.root / 'index.json', changed)
                elif damage == 'missing':
                    (self.root / good['file']).unlink()
                else:
                    body = json.loads(original_body)
                    body['future_reference' if damage == 'unknown-field' else 'season'] = 'bad'
                    _write(self.root / good['file'], body)
                before = self.file_bytes()
                result = retention.adopt_current_hashes(apply=True)
                self.assertTrue(result['blocked'], result)
                self.assertEqual(before, self.file_bytes())
                self.assertNotIn(retention.BLOCKED, before)

    def test_adoption_rejects_untrusted_history_and_pending_without_repair(self):
        self.publish({'s1': self.body()})
        self.publish({'s1': self.body()})
        index = json.loads((self.root / 'index.json').read_bytes())
        del index['seasons']['s1']['shards']['0:103']['content_hash']
        _write(self.root / 'index.json', index)
        original = self.state()
        for damage in ('history-hashless', 'stale-current', 'pending-current'):
            with self.subTest(damage=damage):
                state = deepcopy(original)
                if damage == 'history-hashless':
                    hist = state['history'][0]
                    del hist['index']['seasons']['s1']['shards']['0:103']['content_hash']
                    hist['index_hash'] = content_digest(hist['index'])
                elif damage == 'stale-current':
                    state['current'] = {}
                else:
                    state['pending_deletes'] = state['current']
                _write(self.root / retention.STATE, state)
                before = self.file_bytes()
                result = retention.adopt_current_hashes(apply=True)
                self.assertTrue(result['blocked'], result)
                self.assertEqual(before, self.file_bytes())

    def test_adoption_pointer_raw_identity_fence_does_not_overwrite_interference(self):
        self.legacy_current()
        original = (self.root / 'index.json').read_bytes()
        publish = retention._publish_index_locked
        def interfere(root, index, **kwargs):
            # Semantically identical JSON still violates the original raw fence.
            (root / 'index.json').write_bytes(original + b' ')
            return publish(root, index, **kwargs)
        with patch.object(retention, '_publish_index_locked', interfere):
            result = retention.adopt_current_hashes(apply=True)
        self.assertTrue(result['blocked'], result)
        self.assertEqual((self.root / 'index.json').read_bytes(), original + b' ')
        self.assertFalse((self.root / retention.STATE).exists())
        self.assertFalse((self.root / retention.BLOCKED).exists())

    def test_adoption_publish_failure_leaves_original_and_budget_refuses_before_write(self):
        self.legacy_current()
        before = self.file_bytes()
        with patch.object(retention.os, 'replace', side_effect=OSError('injected atomic failure')):
            result = retention.adopt_current_hashes(apply=True)
        self.assertTrue(result['blocked'], result)
        self.assertEqual(before, self.file_bytes())
        reader = retention.Reader(self.root)
        retention._closure(reader.read('index.json')[0], reader, legacy=True)
        with patch.object(retention, 'MAX_BYTES', reader.used + 1):
            result = retention.adopt_current_hashes(apply=True)
        self.assertTrue(result['blocked'], result)
        self.assertIn('budget', result['reason'])
        self.assertEqual(before, self.file_bytes())

    def test_adoption_missing_root_or_lock_never_initializes_even_apply(self):
        for apply in (False, True):
            with override_settings(MYTHICSTATS_SNAPSHOT_ROOT=self.root / 'missing'):
                self.assertTrue(retention.adopt_current_hashes(apply=apply)['blocked'])
            self.assertTrue(retention.adopt_current_hashes(apply=apply)['blocked'])
            self.assertEqual(list(self.root.iterdir()), [])

    def test_whole_index_history_deduplicates_closures_not_control_metadata(self):
        first = self.index({'s1': self.body()})
        second = self.index({'s1': self.body()})
        retention.publish_index(first)
        retention.publish_index(second)
        state = self.state()
        duplicate = deepcopy(state['history'][0])
        duplicate['index']['updated_at'] = 'same real closure'
        duplicate['index_hash'] = content_digest(duplicate['index'])
        state['history'].append(duplicate)
        _write(self.root / retention.STATE, state)
        # Roll back to first: only second is a distinct known noncurrent index.
        # An unrelated old orphan must never be used to fill the empty slot.
        orphan = self.body()
        retention.publish_index(first)
        self.assertEqual([h['index'] for h in self.state()['history']], [second])
        result = retention.collect_garbage(apply=True, now=2000000)
        self.assertFalse(result['blocked'], result)
        self.assertEqual(result['deleted'], [orphan['file']])

    def test_all_public_writers_reject_unsafe_lock_before_source_access(self):
        from botend.services import mythicstats_snapshot as snapshots
        with tempfile.TemporaryDirectory() as outside:
            outside = Path(outside)
            for kind in ('symlink', 'hardlink', 'fifo'):
                lock = self.root / 'worker.lock'
                if kind == 'symlink':
                    lock.symlink_to(outside / 'never-created')
                elif kind == 'hardlink':
                    (outside / 'regular').write_bytes(b'unchanged')
                    os.link(outside / 'regular', lock)
                else:
                    os.mkfifo(lock)
                for apply in (False, True):
                    self.assertTrue(retention.collect_garbage(apply=apply)['blocked'])
                    self.assertTrue(retention.adopt_current_hashes(apply=apply)['blocked'])
                for publisher in (lambda: retention.publish_index(self.index({})),
                                  snapshots.collect_snapshots, snapshots.publish_database_snapshots):
                    with self.assertRaises((OSError, retention.Unsafe)):
                        publisher()
                self.assertFalse((outside / 'never-created').exists())
                self.assertEqual(set(p.name for p in self.root.iterdir()), {'worker.lock'})
                lock.unlink()

    def test_cross_process_legacy_worker_lock_blocks_gc_adoption_and_publishers(self):
        import subprocess
        import sys
        from botend.services import mythicstats_snapshot as snapshots
        self.legacy_current()
        proc = subprocess.Popen([sys.executable, '-c',
            'import fcntl,sys; f=open(sys.argv[1], "a+b"); '
            'fcntl.flock(f, fcntl.LOCK_EX); print("locked", flush=True); sys.stdin.read()',
            str(self.root / 'worker.lock')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'locked')
            self.assertTrue(retention.collect_garbage()['busy'])
            self.assertTrue(retention.adopt_current_hashes(apply=True)['busy'])
            self.assertTrue(snapshots.collect_snapshots()['busy'])
            with self.assertRaises(BlockingIOError):
                snapshots.publish_database_snapshots()
            with self.assertRaises(BlockingIOError):
                retention.publish_index(self.index({}))
        finally:
            proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0)

    def test_interrupted_delete_recovers_missing_retired_refs(self):
        generations = []
        for _ in range(5):
            generations.append(self.body())
            self.publish({'s1': generations[-1]})
        first, second = [item['file'] for item in generations[:2]]
        unlink = Path.unlink
        def interrupt(path, *args, **kwargs):
            if path.name == second:
                raise OSError('injected interruption')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', interrupt):
            result = retention.collect_garbage(apply=True, now=2000000)
        self.assertEqual(result['deleted'], [first])
        self.assertFalse((self.root / first).exists())
        pending = self.state()['pending_deletes']
        self.assertEqual(set(pending), {first, second})
        self.assertNotIn(first, self.state()['retired'])
        self.assertNotIn(second, self.state()['retired'])
        # A surviving pending member must match the original full-body hash.
        survivor = self.root / second
        original = survivor.read_bytes()
        damaged = json.loads(original)
        damaged['source_note'] = 'changed after interruption'
        _write(survivor, damaged)
        refused = retention.collect_garbage(apply=True, now=2000000)
        self.assertTrue(refused['blocked'], refused)
        self.assertEqual(refused['deleted'], [])
        survivor.write_bytes(original)
        before = (self.root / retention.STATE).read_bytes()
        dry = retention.collect_garbage(now=2000000)
        self.assertFalse(dry['blocked'], dry)
        self.assertEqual((self.root / retention.STATE).read_bytes(), before)
        recovered = retention.collect_garbage(apply=True, now=2000000)
        self.assertFalse(recovered['blocked'], recovered)
        self.assertEqual(recovered['deleted'], [second])
        self.assertFalse(self.state().get('pending_deletes'))
        self.assertEqual(retention.collect_garbage(apply=True, now=2000000)['deleted'], [])
        for item in generations[2:]:
            self.assertTrue((self.root / item['file']).exists())

    def test_manifest_failure_after_all_unlinks_recovers(self):
        generations = []
        for _ in range(4):
            generations.append(self.body())
            self.publish({'s1': generations[-1]})
        victim = self.root / generations[0]['file']
        def fail_final(path, data):
            if path.name == retention.STATE and not victim.exists():
                raise OSError('injected final manifest failure')
            return _write(path, data)
        with patch.object(retention, '_write', fail_final):
            result = retention.collect_garbage(apply=True, now=2000000)
        self.assertTrue(result['blocked'])
        self.assertFalse(victim.exists())
        recovered = retention.collect_garbage(apply=True, now=2000000)
        self.assertFalse(recovered['blocked'], recovered)
        self.assertEqual(recovered['deleted'], [])
        self.assertFalse(self.state().get('pending_deletes'))

    def test_reader_does_not_retain_large_bodies(self):
        import gc
        import tracemalloc
        names = []
        data = None
        for _ in range(8):
            entry = self.body()
            path = self.root / entry['file']
            data = json.loads(path.read_bytes())
            data['source_note'] = 'x' * (2 * 1024 * 1024)
            _write(path, data)
            names.append(path.name)
        del data
        tracemalloc.start()
        try:
            reader = retention.Reader(self.root)
            for name in names:
                reader.read(name)
            gc.collect()
            resident, _ = tracemalloc.get_traced_memory()
            self.assertLess(resident, 1024 * 1024)
            used = reader.used
            for name in names:
                reader.read(name)
            self.assertEqual(reader.used, used)
            self.assertGreater(used, 16 * 1024 * 1024)
            # Same-size modification with restored mtime still changes ctime.
            path = self.root / names[0]
            st = path.stat()
            with path.open('r+b') as stream:
                stream.write(b' ')
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            with self.assertRaises(retention.Unsafe):
                reader.read(path.name)
        finally:
            tracemalloc.stop()

    def test_pending_current_or_history_is_never_deleted(self):
        old = self.body()
        self.publish({'s1': old})
        current = self.body()
        self.publish({'s1': current})
        original = self.state()
        for name in (old['file'], current['file']):
            state = deepcopy(original)
            descriptor = retention.Reader(self.root).read(name)[0]
            state['retired'].pop(name, None)
            state['pending_deletes'] = {name: descriptor}
            _write(self.root / retention.STATE, state)
            result = retention.collect_garbage(apply=True, now=2000000)
            self.assertTrue(result['blocked'], result)
            self.assertEqual(result['deleted'], [])
            self.assertTrue((self.root / name).exists())

    def test_empty_bootstrap_deletes_nothing(self):
        result = retention.collect_garbage(apply=True)
        self.assertEqual(result['deleted'], [])
        self.assertTrue(result['bootstrap'])

    def test_legacy_bootstrap_current_only_zero_deletion(self):
        current, orphan = self.body(), self.body()
        del current['content_hash']
        _write(self.root / 'index.json', self.index({'s1': current}))
        result = retention.collect_garbage(apply=True)
        self.assertTrue(result['bootstrap'])
        self.assertEqual(result['deleted'], [])
        self.assertTrue((self.root / orphan['file']).exists())
        self.assertEqual(set(self.state()['current']), {current['file']})

    def test_current_all_seasons_two_histories_and_grace_union(self):
        old_season = self.body('retired-season')
        generations = []
        for _ in range(4):
            latest = self.body()
            generations.append(latest)
            self.publish({'s1': latest, 'retired-season': old_season})
        self.assertEqual(len(self.state()['history']), 2)
        self.assertEqual(retention.collect_garbage(apply=True)['deleted'], [])
        dry = retention.collect_garbage(now=1000000 + retention.GRACE + 1)
        self.assertFalse(dry['blocked'], dry)
        self.assertEqual(dry['candidates'], [generations[0]['file']])
        result = retention.collect_garbage(apply=True, now=1000000 + retention.GRACE + 1)
        self.assertEqual(result['deleted'], [generations[0]['file']])
        self.assertTrue((self.root / old_season['file']).exists())
        self.assertFalse(retention.collect_garbage(apply=True, now=2000000)['blocked'])

    def test_metadata_noop_does_not_rotate_real_history(self):
        entry = self.body()
        self.publish({'s1': entry})
        before = (self.root / retention.STATE).read_bytes()
        entry['checked_at'] = 'another check'
        self.publish({'s1': entry})
        self.assertEqual((self.root / retention.STATE).read_bytes(), before)

    def test_publish_failure_keeps_pointer_and_history(self):
        self.publish({'s1': self.body()})
        before = (self.root / 'index.json').read_bytes()
        history = self.state()
        index = self.index({'s1': self.body()})
        with _lock(self.root / 'worker.lock'):
            with self.assertRaises(OSError):
                retention._publish_index_locked(self.root, index, writer=lambda *a: (_ for _ in ()).throw(OSError('failed')))
        self.assertEqual((self.root / 'index.json').read_bytes(), before)
        self.assertEqual(self.state(), history)

    def test_broken_old_pointer_repair_does_not_unblock_gc(self):
        (self.root / 'index.json').write_text('null')
        self.publish({'s1': self.body()})
        self.assertTrue((self.root / retention.BLOCKED).exists())
        self.publish({'s1': self.body()})
        result = retention.collect_garbage(apply=True, now=2000000)
        self.assertTrue(result['blocked'])
        self.assertEqual(result['deleted'], [])
        self.assertEqual(json.loads((self.root / 'index.json').read_bytes())['current_season'], 's1')

    def test_history_missing_hash_and_wrong_scope_block_entire_batch(self):
        self.publish({'s1': self.body()})
        self.publish({'s1': self.body()})
        orphan = self.body()
        original = self.state()
        for damage in ('sha256', 'scope'):
            state = deepcopy(original)
            ref = next(iter(state['history'][0]['refs'].values()))
            if damage == 'sha256':
                del ref['sha256']
            else:
                ref['scope'][0] = 'wrong'
            _write(self.root / retention.STATE, state)
            result = retention.collect_garbage(apply=True, now=2000000)
            self.assertTrue(result['blocked'], result)
            self.assertEqual(result['deleted'], [])
            self.assertTrue((self.root / orphan['file']).exists())

    def test_unknown_or_symlink_or_bad_tail_blocks_whole_batch(self):
        self.publish({'s1': self.body()})
        first, last = self.body(), self.body()
        path = self.root / last['file']
        original = path.read_bytes()
        for bad in ('unknown', 'symlink', 'bad-json'):
            if bad == 'unknown':
                data = json.loads(original)
                data['future_reference'] = first['file']
                _write(path, data)
            elif bad == 'symlink':
                path.unlink()
                path.symlink_to(self.root / first['file'])
            else:
                path.unlink()
                path.write_text('{')
            result = retention.collect_garbage(apply=True, now=2000000)
            self.assertTrue(result['blocked'], result)
            self.assertEqual(result['deleted'], [])
            self.assertTrue((self.root / first['file']).exists())

    def test_hardlinked_worker_lock_blocks_entire_batch(self):
        self.publish({'s1': self.body()})
        orphan = self.body()
        with tempfile.TemporaryDirectory() as outside:
            os.link(self.root / 'worker.lock', Path(outside) / 'shared.lock')
            result = retention.collect_garbage(apply=True, now=2000000)
        self.assertTrue(result['blocked'], result)
        self.assertEqual(result['deleted'], [])
        self.assertTrue((self.root / orphan['file']).exists())

    def test_lock_contention_and_bounds(self):
        self.publish({'s1': self.body()})
        with _lock(self.root / 'worker.lock'):
            self.assertTrue(retention.collect_garbage(apply=True)['busy'])
            with self.assertRaises(BlockingIOError):
                self.publish({'s1': self.body()})
        for bound in ('MAX_ENTRIES', 'MAX_BYTES', 'MAX_FILE'):
            with patch.object(retention, bound, 1):
                result = retention.collect_garbage(apply=True)
            self.assertTrue(result['blocked'], result)
            self.assertEqual(result['deleted'], [])

    def test_historical_index_without_hash_is_not_inferred(self):
        self.publish({'s1': self.body()})
        self.publish({'s1': self.body()})
        state = self.state()
        history = state['history'][0]
        del history['index']['seasons']['s1']['shards']['0:103']['content_hash']
        history['index_hash'] = content_digest(history['index'])
        _write(self.root / retention.STATE, state)
        result = retention.collect_garbage(apply=True, now=2000000)
        self.assertTrue(result['blocked'], result)
        self.assertEqual(result['deleted'], [])
        self.publish({'s1': self.body()})
        self.assertTrue((self.root / retention.BLOCKED).exists())

    def test_dangling_manifest_symlink_never_bootstraps(self):
        self.publish({'s1': self.body()})
        (self.root / retention.STATE).unlink()
        (self.root / retention.STATE).symlink_to(self.root / 'missing')
        result = retention.collect_garbage(apply=True)
        self.assertTrue(result['blocked'], result)
        self.assertTrue((self.root / retention.STATE).is_symlink())

    def test_pointer_rechecked_before_each_unlink(self):
        current = self.body()
        self.publish({'s1': current})
        first, second = self.body(), self.body()
        original = Path.unlink
        def change_after_first(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.name == first['file']:
                _write(self.root / 'index.json', self.index({'s1': second}))
            return result
        with patch.object(Path, 'unlink', change_after_first):
            result = retention.collect_garbage(apply=True, now=2000000)
        self.assertTrue(result['blocked'], result)
        self.assertEqual(result['deleted'], [first['file']])
        self.assertTrue((self.root / second['file']).exists())

    def test_command_defaults_to_dry_run(self):
        from django.core.management import call_command
        from io import StringIO
        self.publish({'s1': self.body()})
        orphan = self.body()
        output = StringIO()
        call_command('gc_mythicstats_snapshots', stdout=output)
        result = json.loads(output.getvalue())
        self.assertFalse(result['apply'])
        self.assertEqual(result['candidates'], [orphan['file']])
        self.assertTrue((self.root / orphan['file']).exists())

    def test_mtime_and_batch_limit(self):
        self.publish({'s1': self.body()})
        a, b, recent = self.body(), self.body(), self.body()
        os.utime(self.root / recent['file'], (1999999, 1999999))
        result = retention.collect_garbage(apply=True, now=2000000, limit=1)
        self.assertEqual(result['deleted'], [a['file']])
        self.assertTrue((self.root / b['file']).exists())
        self.assertTrue((self.root / recent['file']).exists())
