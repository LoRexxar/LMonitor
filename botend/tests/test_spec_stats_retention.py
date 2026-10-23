import json
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
from django.test import SimpleTestCase
from botend.services import spec_stats_snapshot as snapshot
from botend.services import spec_stats_retention as retention
from botend.services.simc_benchmark_result_snapshot import _write, _lock


class RetentionTests(SimpleTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.scope = Path(temp.name) / '77/Warrior/Arms'
        self.scope.mkdir(parents=True)

    def publish(self, module='dungeon', value=1):
        detail = {'dungeon_id': 1, 'boss_id': 1, 'sample_size': value}
        payload = {'dungeons': [detail]} if module == 'dungeon' else {
            'zone_groups': [{'bosses': [detail]}]}
        snapshot.publish_projection(self.scope / f'{module}.json', payload, _write)
        return json.loads((self.scope / f'{module}.json').read_text())['_snapshot']['generation']

    def age(self):
        for path in (self.scope / 'stats-details').rglob('*'):
            os.utime(path, (1, 1))
        manifest = self.scope / retention.MANIFEST
        data = json.loads(manifest.read_text())
        for row in data['records']:
            if row['retired_at'] is not None:
                row['retired_at'] = 1
        _write(manifest, data)

    def test_large_scope_resumes_bounded_reads_and_deletion(self):
        # Real producer + on-disk envelopes: 39 generations, 702 details.
        generations = []
        for i in range(39):
            snapshot.publish_projection(self.scope / 'dungeon.json', {'dungeons': [
                {'dungeon_id': key, 'sample_size': i} for key in range(1, 19)]}, _write)
            generations.append(json.loads((self.scope / 'dungeon.json').read_text())[
                '_snapshot']['generation'])
        self.age()
        root = self.scope / 'stats-details'
        self.assertEqual(len(list(root.rglob('*.json'))), 702)
        first = retention.maintain_scope(self.scope, delete_limit=1)
        self.assertEqual(first['status'], 'paused', first)
        self.assertGreater(first['validated_files'], 0)
        checkpoint = self.scope / retention.CHECKPOINT
        self.assertTrue(checkpoint.exists())
        # Unknown metadata still fails closed, even with authenticated progress.
        unknown = root / 'backup'
        unknown.mkdir()
        refused = retention.maintain_scope(self.scope, delete_limit=1)
        self.assertEqual(refused['status'], 'refused', refused)
        self.assertEqual(refused['deleted'], 0)
        unknown.rmdir()
        # 未变化详情的独立证明继续有效；未知目录仍在每轮盘点时拒绝。
        restarted = retention.maintain_scope(self.scope, delete_limit=1)
        self.assertEqual(restarted['status'], 'ok', restarted)
        self.assertEqual(restarted['deleted'], 1, restarted)
        self.assertLess(restarted['files_read'], first['files_read'])
        results = [retention.maintain_scope(self.scope, delete_limit=1) for _ in range(4)]
        self.assertEqual(sum(r['deleted'] for r in results), 4, results)
        for result in [first, restarted, *results]:
            self.assertLessEqual(result['files_read'], retention.MAX_ENTRIES)
            self.assertLessEqual(result['bytes_read'], retention.MAX_BYTES)
            self.assertLessEqual(result['deleted'], 1)
        self.assertTrue(all((root / g).exists() for g in generations[-3:]))
        self.assertFalse((root / generations[0]).exists())
        self.assertFalse((root / generations[1]).exists())
        # A smaller per-call content budget must accumulate MORE than 512
        # authenticated observations, not silently discard that checkpoint.
        checkpoint.unlink()
        with patch.object(retention, 'MAX_ENTRIES', 200):
            pauses = [retention.maintain_scope(self.scope, delete_limit=1) for _ in range(3)]
            self.assertTrue(all(r['status'] == 'paused' for r in pauses), pauses)
            saved = retention.Budget._signer().unsign_object(json.loads(checkpoint.read_text())['proof'])
            self.assertGreater(len(saved['files']), 512)
            resumed = retention.maintain_scope(self.scope, delete_limit=1)
            self.assertEqual(resumed['deleted'], 1, resumed)
            self.assertLessEqual(resumed['files_read'], 200)

    def test_more_than_512_generations_can_publish_and_reclaim(self):
        generations = [self.publish(value=i) for i in range(514)]
        self.assertFalse((self.scope / retention.DISABLED).exists())
        self.age()
        root = self.scope / 'stats-details'
        self.assertEqual(len(list(root.iterdir())), 514)
        results = [retention.maintain_scope(self.scope, delete_limit=1) for _ in range(4)]
        self.assertEqual(sum(r['deleted'] for r in results), 3, results)
        self.assertEqual(results[0]['status'], 'paused', results)
        for result in results:
            self.assertIn(result['status'], ('paused', 'ok'), result)
            self.assertLessEqual(result['files_read'], retention.MAX_ENTRIES)
            self.assertLessEqual(result['bytes_read'], retention.MAX_BYTES)
        self.assertEqual(len(list(root.iterdir())), 511)
        self.assertTrue(all((root / g).exists() for g in generations[-3:]))

    def test_latest_two_per_module_and_cross_module_current(self):
        raid = self.publish('raid')
        generations = [self.publish(value=i) for i in range(5)]
        self.age()
        result = retention.maintain_scope(self.scope)
        self.assertEqual(result['deleted'], 2)
        self.assertTrue((self.scope / 'stats-details' / raid).exists())
        for generation in generations[-3:]:
            self.assertTrue((self.scope / 'stats-details' / generation).exists())

    def test_whole_batch_corruption_refuses_all_deletion(self):
        generations = [self.publish(value=i) for i in range(5)]
        self.age()
        (self.scope / 'stats-details' / generations[1] / 'dungeon-1.json').write_text('{}')
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 0)
        self.assertTrue((self.scope / 'stats-details' / generations[0]).exists())

    def test_bootstrap_zero_deletion_and_legacy_no_hash_marker(self):
        self.publish()
        (self.scope / retention.MANIFEST).unlink()
        self.assertEqual(retention.maintain_scope(self.scope)['status'], 'bootstrapped')
        path = self.scope / 'dungeon.json'
        data = json.loads(path.read_text())
        del data['_snapshot']['detail_hashes']
        _write(path, data)
        (self.scope / retention.MANIFEST).unlink()
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 0)
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.scope.rglob('*') if p.is_file()}
        with self.assertRaises(RuntimeError):
            self.publish(value=2)
        self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.scope.rglob('*') if p.is_file()})
        self.assertFalse((self.scope / retention.DISABLED).exists())

    def test_failed_write_keeps_current(self):
        self.publish()
        path = self.scope / 'dungeon.json'
        before = path.read_bytes()
        def fail(target, value):
            if target == path:
                raise OSError('injected')
            _write(target, value)
        with self.assertRaises(OSError):
            snapshot.publish_projection(path, {'dungeons': [{'dungeon_id': 2}]}, fail)
        self.assertEqual(before, path.read_bytes())

    def test_grace_uses_retirement_and_mtime_union(self):
        generations = [self.publish(value=i) for i in range(5)]
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 0)
        self.age()
        os.utime(self.scope / 'stats-details' / generations[0] / 'dungeon-1.json', None)
        result = retention.maintain_scope(self.scope, delete_limit=1)
        self.assertEqual(result['deleted'], 1)
        self.assertTrue((self.scope / 'stats-details' / generations[0]).exists())

    def test_unknown_and_symlink_refuse(self):
        for i in range(5):
            self.publish(value=i)
        self.age()
        unknown = self.scope / 'stats-details' / 'backup'
        unknown.mkdir()
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 0)
        unknown.rmdir()
        try:
            unknown.symlink_to('/nonexistent-target')
        except OSError as exc:
            if getattr(exc, 'winerror', None) == 1314:
                self.skipTest('当前 Windows 账号没有创建符号链接的权限')
            raise
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 0)

    def test_corrupt_protected_history_and_missing_other_pointer_refuse(self):
        raid = self.publish('raid')
        for i in range(5):
            self.publish(value=i)
        self.age()
        raid_path = self.scope / 'raid.json'
        raw = raid_path.read_bytes()
        raid_path.unlink()
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 0)
        raid_path.write_bytes(raw)
        (self.scope / 'stats-details' / raid / 'raid-5-1.json').write_text('{}')
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 0)

    def test_budgets_dry_run_and_scope_cursor(self):
        for i in range(5):
            self.publish(value=i)
        self.age()
        with patch.object(retention, 'MAX_BYTES', 20):
            self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 0)
        with patch.object(retention, 'MAX_ENTRIES', 2):
            self.assertEqual(retention.maintain_scope(self.scope, delete_limit=1)['deleted'], 0)
        self.assertEqual(retention.maintain_scope(self.scope, dry_run=True)['eligible'], 2)
        second = self.scope.parent / 'Fury'
        second.mkdir()
        snapshot.publish_projection(second / 'dungeon.json', {'dungeons': [{'dungeon_id': 1}]}, _write)
        root = self.scope.parents[2]
        first = retention.maintain_scopes(root, scope_limit=1, dry_run=True)
        self.assertTrue(first['has_more'])
        following = retention.maintain_scopes(root, after=first['next_cursor'], scope_limit=1, dry_run=True)
        self.assertFalse(following['has_more'])
        self.assertNotEqual(first['next_cursor'], following['next_cursor'])

    def test_manifest_failure_restores_last_good(self):
        self.publish()
        path = self.scope / 'dungeon.json'
        before = path.read_bytes()
        def fail(target, value):
            if target.name == retention.MANIFEST:
                raise OSError('manifest write interrupted')
            _write(target, value)
        with patch.object(retention, '_write', side_effect=fail):
            with self.assertRaises(OSError):
                self.publish(value=9)
        self.assertEqual(before, path.read_bytes())
        self.assertTrue((self.scope / retention.DISABLED).exists())

    def test_legacy_known_orphans_do_not_become_rollback_history(self):
        raid = self.publish('raid')
        generations = [self.publish(value=i) for i in range(5)]
        self.age()
        (self.scope / retention.MANIFEST).unlink()
        self.assertEqual(retention.maintain_scope(self.scope)['status'], 'bootstrapped')
        result = retention.maintain_scope(self.scope, delete_limit=2)
        self.assertEqual(result['deleted'], 2, result)
        data = json.loads((self.scope / retention.MANIFEST).read_text())
        self.assertEqual({r['meta']['generation'] for r in data['records']}, {raid, generations[-1]})
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 2)
        self.assertTrue((self.scope / 'stats-details' / raid).exists())

    def test_failed_publisher_orphan_reclaimed_after_grace(self):
        self.publish()
        before = {p.name for p in (self.scope / 'stats-details').iterdir()}
        def fail(target, value):
            if target == self.scope / 'dungeon.json':
                raise OSError('index failed')
            _write(target, value)
        with self.assertRaises(OSError):
            snapshot.publish_projection(self.scope / 'dungeon.json',
                                        {'dungeons': [{'dungeon_id': 2}]}, fail)
        orphan = next(p for p in (self.scope / 'stats-details').iterdir() if p.name not in before)
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 0)
        self.age()
        result = retention.maintain_scope(self.scope)
        self.assertEqual(result['deleted'], 1, result)
        self.assertFalse(orphan.exists())

    def test_interrupted_delete_resumes_without_dangling_history(self):
        generations = [self.publish(value=i) for i in range(5)]
        self.age()
        original = Path.unlink
        count = 0
        def interrupted(path, *args, **kwargs):
            nonlocal count
            if path.parent.name in generations[:2]:
                count += 1
                if count == 2:
                    raise OSError('simulated interruption after first generation')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', interrupted):
            result = retention.maintain_scope(self.scope)
        self.assertEqual(result['deleted'], 1, result)
        result = retention.maintain_scope(self.scope)
        self.assertEqual(result['status'], 'ok', result)
        for generation in generations[:2]:
            self.assertFalse((self.scope / 'stats-details' / generation).exists())
        self.assertEqual(retention.maintain_scope(self.scope)['eligible'], 0)

    def test_orphan_hardlinks_unknown_members_and_wrong_scope_refuse(self):
        self.publish()
        source = next((self.scope / 'stats-details').iterdir())
        import uuid
        orphan = source.parent / uuid.uuid4().hex
        orphan.mkdir()
        target = orphan / 'dungeon-1.json'
        body = json.loads((source / target.name).read_text())
        body['generation'] = orphan.name
        _write(target, body)
        self.age()
        alias = self.scope / 'alias'
        os.link(target, alias)
        self.assertEqual(retention.maintain_scope(self.scope)['status'], 'refused')
        alias.unlink()
        extra = orphan / 'notes.txt'
        extra.write_text('unknown')
        self.assertEqual(retention.maintain_scope(self.scope)['status'], 'refused')
        extra.unlink()
        body['scope'] = ['wrong', 'scope', 'identity']
        _write(target, body)
        self.assertEqual(retention.maintain_scope(self.scope)['status'], 'refused')

    def test_pending_partial_files_resume_after_intervening_publication(self):
        for i in range(5):
            snapshot.publish_projection(self.scope / 'dungeon.json', {'dungeons': [
                {'dungeon_id': 1, 'sample_size': i}, {'dungeon_id': 2, 'sample_size': i}]}, _write)
        self.age()
        original = Path.unlink
        count = 0
        def interrupted(path, *args, **kwargs):
            nonlocal count
            if path.parent.parent.name == 'stats-details':
                count += 1
                if count == 2:
                    raise OSError('interrupted inside generation')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', interrupted):
            self.assertEqual(retention.maintain_scope(self.scope)['status'], 'refused')
        data = json.loads((self.scope / retention.MANIFEST).read_text())
        self.assertEqual(len(data['pending_deletes']), 2)
        self.publish('raid')
        self.assertFalse((self.scope / retention.DISABLED).exists())
        before = (self.scope / retention.MANIFEST).read_bytes()
        self.assertEqual(retention.maintain_scope(self.scope, dry_run=True)['eligible'], 2)
        self.assertEqual((self.scope / retention.MANIFEST).read_bytes(), before)
        self.assertEqual(retention.maintain_scope(self.scope, delete_limit=1)['pending'], 1)
        result = retention.maintain_scope(self.scope, delete_limit=1)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['pending'], 0)

    def test_pending_commit_failure_recovers_already_absent_directories(self):
        for i in range(5):
            self.publish(value=i)
        self.age()
        calls = 0
        def fail(target, value):
            nonlocal calls
            if target.name == retention.MANIFEST:
                calls += 1
                if calls == 2:
                    raise OSError('final manifest write failed')
            _write(target, value)
        with patch.object(retention, '_write', side_effect=fail):
            result = retention.maintain_scope(self.scope)
        self.assertEqual(result['deleted'], 2, result)
        self.assertEqual(result['status'], 'refused')
        result = retention.maintain_scope(self.scope)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['pending'], 0)

    def test_pending_intent_failure_deletes_nothing(self):
        generations = [self.publish(value=i) for i in range(5)]
        self.age()
        with patch.object(retention, '_write', side_effect=OSError('intent unavailable')):
            result = retention.maintain_scope(self.scope)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result['deleted'], 0)
        self.assertTrue(all((self.scope / 'stats-details' / gen).exists() for gen in generations))
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 2)

    def test_pending_changed_member_refuses_entire_recovery_batch(self):
        for i in range(5):
            self.publish(value=i)
        self.age()
        with patch.object(Path, 'unlink', side_effect=OSError('stop before unlink')):
            self.assertEqual(retention.maintain_scope(self.scope)['status'], 'refused')
        data = json.loads((self.scope / retention.MANIFEST).read_text())
        first, second = data['pending_deletes']
        changed = self.scope / 'stats-details' / second['meta']['generation'] / 'dungeon-1.json'
        raw = changed.read_bytes()
        changed.write_text('{}')
        result = retention.maintain_scope(self.scope)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['deleted'], 0)
        self.assertTrue((self.scope / 'stats-details' / first['meta']['generation']).exists())
        changed.write_bytes(raw)
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 2)

    def test_publisher_and_gc_share_real_lock(self):
        self.publish()
        entered = threading.Event()
        finished = threading.Event()
        def run():
            entered.set()
            self.publish(value=2)
            finished.set()
        with _lock(self.scope / retention.LOCK):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(finished.wait(.1))
            self.assertEqual(retention.maintain_scope(self.scope)['status'], 'locked')
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(finished.is_set())

    def test_hardlinked_lock_refused(self):
        for i in range(5): self.publish(value=i)
        self.age()
        os.link(self.scope / retention.LOCK, self.scope / 'lock-alias')
        self.assertEqual(retention.maintain_scope(self.scope)['status'], 'refused')

    def test_noop_manifest_identity(self):
        self.publish('raid')
        path = self.scope / retention.MANIFEST
        before = path.stat()
        self.publish('raid')
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(path.stat().st_ino, before.st_ino)

    def test_current_only_migration(self):
        self.publish('raid')
        path = self.scope / 'raid.json'
        data = json.loads(path.read_text())
        del data['_snapshot']['detail_hashes']
        _write(path, data)
        before = path.read_bytes()
        manifest = (self.scope / retention.MANIFEST).read_bytes()
        _write(self.scope / retention.DISABLED, {'schema': 1, 'reason': 'sticky'})
        self.assertEqual(retention.migrate_current_hashes(self.scope)['status'], 'dry-run')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(retention.migrate_current_hashes(self.scope, apply=True)['status'], 'migrated')
        after = json.loads(path.read_text())
        del after['_snapshot']['detail_hashes']
        self.assertEqual(after, data)
        self.assertEqual((self.scope / retention.MANIFEST).read_bytes(), manifest)
        self.assertTrue((self.scope / retention.DISABLED).exists())

    def test_checkpoint_progress_and_drift(self):
        for i in range(5):
            snapshot.publish_projection(self.scope / 'dungeon.json', {'dungeons': [
                {'dungeon_id': 1, 'sample_size': i, 'padding': 'x' * 6000}]}, _write)
        self.age()
        with patch.object(retention, 'MAX_BYTES', 18000):
            self.assertEqual(retention.maintain_scope(self.scope, dry_run=True)['status'], 'paused')
            self.assertFalse((self.scope / retention.CHECKPOINT).exists())
            first = retention.maintain_scope(self.scope)
            self.assertEqual(first['status'], 'paused', first)
            current = json.loads((self.scope / 'dungeon.json').read_text())['_snapshot']['generation']
            target = self.scope / 'stats-details' / current / 'dungeon-1.json'
            original = target.read_bytes()
            target.write_text('{}')
            self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 0)
            target.write_bytes(original)
            os.utime(target, (1, 1))
            results = [retention.maintain_scope(self.scope) for _ in range(12)]
            self.assertEqual(sum(r['deleted'] for r in results), 2, results)
            self.assertTrue(target.exists())

    def test_current_plus_two_predecessors_both_modules(self):
        generations = {module: [self.publish(module, i) for i in range(5)] for module in retention.MODULES}
        self.age()
        result = retention.maintain_scope(self.scope)
        self.assertEqual(result['deleted'], 4, result)
        for module, values in generations.items():
            self.assertEqual([g for g in values if (self.scope / 'stats-details' / g).exists()], values[-3:])

    def test_noop_preserves_pending(self):
        for i in range(5): self.publish(value=i)
        self.publish('raid')
        self.age()
        with patch.object(Path, 'unlink', side_effect=OSError('interrupt')):
            self.assertEqual(retention.maintain_scope(self.scope)['status'], 'refused')
        path = self.scope / retention.MANIFEST
        before = path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns
        self.publish('raid')
        self.assertEqual((path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns), before)

    def test_migration_command_default_readonly(self):
        from django.core.management import call_command
        from io import StringIO
        self.publish('raid')
        path = self.scope / 'raid.json'
        data = json.loads(path.read_text())
        del data['_snapshot']['detail_hashes']
        _write(path, data)
        before = path.read_bytes()
        out = StringIO()
        call_command('migrate_spec_stats_current_hashes', str(self.scope), stdout=out)
        self.assertEqual(json.loads(out.getvalue())['status'], 'dry-run')
        self.assertEqual(path.read_bytes(), before)
        call_command('migrate_spec_stats_current_hashes', str(self.scope), apply=True, stdout=StringIO())
        self.assertIn('detail_hashes', json.loads(path.read_text())['_snapshot'])

    def test_forged_checkpoint_cannot_hide_corrupt_current(self):
        generations = [self.publish(value=i) for i in range(5)]
        self.age()
        budget = retention.Budget()
        retention._currents(self.scope, budget)
        retention._manifest(self.scope, budget)
        budget.checkpoint(self.scope)
        for path in (self.scope / 'stats-details').rglob('*.json'):
            budget.read(path)
        target = self.scope / 'stats-details' / generations[-1] / 'dungeon-1.json'
        target.write_text('{}')
        _write(self.scope / retention.CHECKPOINT, {
            'schema': 1, 'inventory': retention._inventory(self.scope),
            'controls': budget.controls, 'files': budget.cache})
        result = retention.maintain_scope(self.scope)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['deleted'], 0)
        self.assertTrue(all((self.scope / 'stats-details' / g).exists() for g in generations))

    def test_authenticated_checkpoint_reuse_invalidation_and_schema(self):
        from django.conf import settings
        self.publish()
        def fresh():
            budget = retention.Budget()
            retention._currents(self.scope, budget)
            retention._manifest(self.scope, budget)
            budget.checkpoint(self.scope)
            return budget
        budget = fresh()
        target = next((self.scope / 'stats-details').rglob('*.json'))
        budget.read(target)
        budget.save()
        checkpoint = self.scope / retention.CHECKPOINT
        original = checkpoint.read_bytes()
        self.assertEqual(fresh().cache, budget.cache)
        saved = json.loads(original)
        payload = budget._signer().unsign_object(saved['proof'])
        self.assertNotIn('sample_size', str(payload['files']))
        for field in ('scope', 'controls', 'inventory', 'files'):
            changed = dict(payload)
            changed[field] = {} if field != 'scope' else '/wrong/scope'
            # Even a correctly signed but mismatched context cannot be reused.
            if field != 'files':
                _write(checkpoint, {'proof': budget._signer().sign_object(changed)})
                self.assertEqual(fresh().cache, {})
        malformed = json.loads(json.dumps(payload))
        next(iter(malformed['files'].values()))['raw_hash'] = 'invalid'
        _write(checkpoint, {'proof': budget._signer().sign_object(malformed)})
        self.assertEqual(fresh().cache, {})
        for corrupt in (b'{', b'[]', json.dumps({'proof': saved['proof'] + 'x'}).encode()):
            checkpoint.write_bytes(corrupt)
            self.assertEqual(fresh().cache, {})
        checkpoint.write_bytes(original)
        old_key = settings.SECRET_KEY
        with self.settings(SECRET_KEY='isolated-test-rotation', SECRET_KEY_FALLBACKS=[old_key]):
            self.assertEqual(fresh().cache, {})
        # Identical bytes with a changed inode are not the signed observation.
        replacement = target.with_suffix('.replacement')
        replacement.write_bytes(target.read_bytes())
        replacement.replace(target)
        self.assertEqual(fresh().cache, {})

    def test_paused_reacquire_does_not_wait_for_real_publisher(self):
        from contextlib import contextmanager
        for i in range(5):
            snapshot.publish_projection(self.scope / 'dungeon.json', {'dungeons': [
                {'dungeon_id': 1, 'sample_size': i, 'padding': 'x' * 6000}]}, _write)
        other = self.scope.parent / 'Fury'
        other.mkdir()
        snapshot.publish_projection(other / 'dungeon.json', {'dungeons': [{'dungeon_id': 1}]}, _write)
        original_lock = retention._scope_lock
        entered, release, holder_finished = threading.Event(), threading.Event(), threading.Event()
        threads, modes = [], []
        @contextmanager
        def contended(path, blocking=True, create=True):
            if path.parent == self.scope:
                modes.append(blocking)
                if len(modes) == 2:
                    def publisher():
                        with original_lock(path):
                            entered.set()
                            release.wait(5)
                        holder_finished.set()
                    thread = threading.Thread(target=publisher)
                    threads.append(thread)
                    thread.start()
                    self.assertTrue(entered.wait(2))
            with original_lock(path, blocking=blocking, create=create) as acquired:
                yield acquired
        try:
            with patch.object(retention, 'MAX_BYTES', 18000), patch.object(retention, '_scope_lock', contended):
                result = retention.maintain_scope(self.scope)
                following = retention.maintain_scope(other)
            # 直接证明发布锁尚未释放时回收已返回，避免磁盘元数据耗时影响结论。
            self.assertFalse(holder_finished.is_set(), '回收不应等发布者释放锁')
            self.assertEqual(modes, [False, False])
            self.assertIn(result['status'], ('paused', 'locked'))
            self.assertEqual(following['status'], 'ok', following)
            self.assertFalse((self.scope / retention.CHECKPOINT).exists())
        finally:
            release.set()
            for thread in threads:
                thread.join(2)

    def files(self):
        return {str(p.relative_to(self.scope)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
                for p in self.scope.rglob('*') if p.is_file()}

    def hashless(self, module):
        path = self.scope / (module + '.json')
        index = json.loads(path.read_text())
        del index['_snapshot']['detail_hashes']
        _write(path, index)

    def test_gate_both_modules_same_and_changed_and_mixed_corruption(self):
        self.publish('dungeon')
        self.publish('raid')
        for legacy in retention.MODULES:
            self.hashless(legacy)
            for module in retention.MODULES:
                for value in (1, 2):
                    with self.subTest(legacy=legacy, module=module, value=value):
                        before = self.files()
                        with self.assertRaises(retention.AdoptionRequired):
                            self.publish(module, value)
                        self.assertEqual(self.files(), before)
            retention.migrate_current_hashes(self.scope, apply=True)
        self.hashless('raid')
        (self.scope / 'dungeon.json').write_text('{')
        before = self.files()
        with self.assertRaises(retention.AdoptionRequired):
            self.publish('dungeon', 2)
        self.assertEqual(self.files(), before)

    def test_gate_lock_initialization_only_and_migration_first_history(self):
        old = self.publish()
        self.hashless('dungeon')
        (self.scope / retention.MANIFEST).unlink()
        (self.scope / retention.LOCK).unlink()
        before = self.files()
        with self.assertRaises(retention.AdoptionRequired):
            self.publish(value=2)
        after = self.files()
        self.assertEqual(set(after) - set(before), {retention.LOCK})
        self.assertEqual({k: v for k, v in after.items() if k != retention.LOCK}, before)
        self.assertEqual(retention.migrate_current_hashes(self.scope)['status'], 'dry-run')
        self.assertEqual(self.files(), after)
        self.assertEqual(retention.migrate_current_hashes(self.scope, apply=True)['changed'], ['dungeon.json'])
        adopted = self.files()
        self.assertEqual(retention.migrate_current_hashes(self.scope, apply=True)['changed'], [])
        self.assertEqual(self.files(), adopted)
        new = self.publish(value=2)
        rows = json.loads((self.scope / retention.MANIFEST).read_text())['records']
        self.assertEqual([row['meta']['generation'] for row in rows], [old, new])
        self.assertIsNotNone(rows[0]['retired_at'])
        self.assertIsNone(rows[1]['retired_at'])
        self.assertFalse((self.scope / retention.DISABLED).exists())

    def test_gate_invalid_currents_still_repair_and_stick(self):
        for kind in ('null_hash', 'empty_hash', 'partial_hash', 'wrong_hash', 'unknown', 'truncated', 'null_index', 'missing_body', 'scope'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                original_scope = self.scope
                self.scope = Path(temp) / '77/Warrior/Arms'
                try:
                    self.publish()
                    path = self.scope / 'dungeon.json'
                    data = json.loads(path.read_text())
                    if kind == 'null_hash': data['_snapshot']['detail_hashes'] = None
                    if kind == 'empty_hash': data['_snapshot']['detail_hashes'] = {}
                    if kind == 'partial_hash': data['_snapshot']['detail_hashes'].pop(next(iter(data['_snapshot']['detail_hashes'])))
                    if kind == 'wrong_hash': data['_snapshot']['detail_hashes'] = {k: '0' * 64 for k in data['_snapshot']['details']}
                    if kind == 'unknown':
                        del data['_snapshot']['detail_hashes']
                        data['unknown'] = True
                    if kind == 'scope': data['_snapshot']['scope'] = ['bad']
                    _write(path, data)
                    if kind == 'truncated': path.write_text('{')
                    if kind == 'null_index': path.write_text('null')
                    if kind == 'missing_body': next((self.scope / 'stats-details').rglob('*.json')).unlink()
                    self.publish(value=2)
                    self.assertTrue((self.scope / retention.DISABLED).exists())
                    self.assertIn('detail_hashes', json.loads(path.read_text())['_snapshot'])
                finally:
                    self.scope = original_scope

    def test_gate_budget_io_and_actual_identity_drift_are_zero_write(self):
        self.publish()
        self.hashless('dungeon')
        before = self.files()
        for fault in (retention.Paused('budget'), PermissionError('temporary IO')):
            with patch.object(retention.Budget, 'read', side_effect=fault):
                with self.assertRaises(retention.PublicationDeferred): self.publish(value=2)
            self.assertEqual(self.files(), before)
        with patch.object(retention, 'PUBLICATION_MAX_BYTES', 1):
            with self.assertRaises(retention.PublicationDeferred): self.publish(value=2)
        self.assertEqual(self.files(), before)
        real = retention._identity
        counts = {}
        def drifting(path):
            identity = real(path)
            if path.name == 'dungeon.json':
                counts[path] = counts.get(path, 0) + 1
                if counts[path] > 1: identity[-1] += 1
            return identity
        with patch.object(retention, '_identity', side_effect=drifting):
            with self.assertRaises(retention.PublicationDeferred): self.publish(value=2)
        self.assertEqual(self.files(), before)

    def test_gate_scan_defers_and_does_not_finish(self):
        from importlib import import_module
        from unittest.mock import Mock
        mod = import_module('botend.controller.plugins.portal.SpecDetailAggregationMonitor')
        self.publish()
        self.hashless('dungeon')
        cycle, season = Mock(), Mock(id=77)
        monitor = object.__new__(mod.SpecDetailAggregationMonitor)
        monitor.task = Mock()
        with patch.object(mod.SeasonMeta.objects, 'filter') as query, \
                patch.object(mod, 'begin_aggregation', return_value=cycle), \
                patch.object(mod, 'frozen_season', return_value=season), \
                patch.object(mod, 'CLASS_SPEC_MAP', {'Warrior': ['Arms']}), \
                patch.object(monitor, '_aggregate_leaderboard'), \
                patch.object(monitor, '_aggregate_dungeon', side_effect=lambda *a, **kw: self.publish(value=2)), \
                patch.object(mod, 'defer_cycle') as defer, \
                patch.object(mod, 'finish_publication') as finish:
            query.return_value.first.return_value = season
            before = self.files()
            self.assertFalse(monitor.scan(''))
            defer.assert_called_once_with(cycle, 'publication_failed:AdoptionRequired', retry_at=None)
            finish.assert_not_called()
            self.assertEqual(self.files(), before)

    def test_gate_cross_process_migration_and_publisher_lock(self):
        import multiprocessing
        if 'fork' not in multiprocessing.get_all_start_methods():
            self.skipTest('此用例需要 POSIX fork；Windows 互斥由独立进程用例验证')
        ctx = multiprocessing.get_context('fork')
        self.publish()
        self.hashless('dungeon')
        lock_identity = (self.scope / retention.LOCK).stat().st_ino
        entered, release = ctx.Event(), ctx.Event()
        original = retention._observe_current
        def migrate():
            def wait(*args):
                entered.set()
                if not release.wait(5): raise RuntimeError('test timeout')
                return original(*args)
            with patch.object(retention, '_observe_current', side_effect=wait):
                result = retention.migrate_current_hashes(self.scope, apply=True)
                assert result['status'] == 'migrated', result
        process = ctx.Process(target=migrate)
        process.start()
        errors, finished = [], threading.Event()
        def publish():
            try: self.publish(value=2)
            except Exception as exc: errors.append(exc)
            finally: finished.set()
        thread = threading.Thread(target=publish)
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(retention.migrate_current_hashes(self.scope)['status'], 'locked')
            thread.start()
            self.assertFalse(finished.wait(.1))
        finally:
            release.set()
            process.join(5)
            if thread.ident is not None: thread.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])
        self.assertEqual((self.scope / retention.LOCK).stat().st_ino, lock_identity)
        # Reverse direction: real publisher context prevents explicit adoption.
        entered.clear(); release.clear()
        def hold_publication():
            with retention.publication(self.scope):
                entered.set()
                assert release.wait(5)
        process = ctx.Process(target=hold_publication)
        process.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(retention.migrate_current_hashes(self.scope, apply=True)['status'], 'locked')
        finally:
            release.set(); process.join(5)
        self.assertEqual(process.exitcode, 0)

    def test_publication_budget_large_real_currents_is_bounded_and_gc_unchanged(self):
        # Real producer envelopes and hashes on disk, not mocked byte accounting.
        padding = 'x' * (34 * 1024 * 1024)
        snapshot.publish_projection(self.scope / 'dungeon.json', {
            'dungeons': [{'dungeon_id': 1, 'players': padding}]}, _write)
        snapshot.publish_projection(self.scope / 'raid.json', {
            'zone_groups': [{'bosses': [{'boss_id': 1, 'players': padding}]}]}, _write)
        del padding
        total = sum(p.stat().st_size for p in self.scope.rglob('*.json'))
        self.assertGreater(total, 64 * 1024 * 1024)
        self.assertLess(total, 128 * 1024 * 1024)
        before = retention._inventory(self.scope)
        gc = retention.maintain_scope(self.scope, dry_run=True)
        self.assertEqual(gc['status'], 'paused', gc)
        self.assertEqual(gc['deleted'], 0)
        self.assertEqual(retention._inventory(self.scope), before)
        # Exercise the actual publisher, including the post-gate manifest check.
        self.publish(value=2)
        self.assertFalse((self.scope / retention.DISABLED).exists())
        self.assertEqual(retention.Budget().remaining, 64 * 1024 * 1024)
        with patch.object(retention, 'MAX_BYTES', 1):
            self.assertEqual(retention.Budget().remaining, 1)
        raid_index = json.loads((self.scope / 'raid.json').read_text())
        detail = (self.scope / 'stats-details' /
                  raid_index['_snapshot']['generation'] / 'raid-5-1.json')
        # A changed content hash still fails the read-only gate; repair policy is unchanged.
        with detail.open('r+b') as stream:
            raw = stream.read(1024)
            position = raw.index(b'xxx')
            stream.seek(position)
            stream.write(b'y')
        before = retention._inventory(self.scope)
        budget = retention.Budget(reuse=True, max_bytes=retention.PUBLICATION_MAX_BYTES)
        self.assertIsInstance(retention._publication_gate(self.scope, budget), retention.Unsafe)
        self.assertEqual(retention._inventory(self.scope), before)
        with detail.open('r+b') as stream:
            stream.seek(position)
            stream.write(b'x')
        # JSON whitespace keeps the exact envelope/content hash valid while the
        # current closure exceeds 128 MiB. The real reader must defer, not write.
        with detail.open('ab') as stream:
            for _ in range(95):
                stream.write(b' ' * (1024 * 1024))
        self.assertGreater(detail.stat().st_size, 128 * 1024 * 1024)
        before = retention._inventory(self.scope)
        with self.assertRaises(retention.PublicationDeferred) as error:
            self.publish(value=3)
        self.assertIsInstance(error.exception.__cause__, retention.Paused)
        self.assertEqual(retention._inventory(self.scope), before)
        self.assertFalse((self.scope / retention.DISABLED).exists())
        self.assertFalse((self.scope / retention.CHECKPOINT).exists())

    def test_gate_reuses_body_reads_within_original_budget(self):
        self.publish()
        (self.scope / retention.MANIFEST).unlink()
        total = sum(p.stat().st_size for p in self.scope.rglob('*.json'))
        with patch.object(retention, 'PUBLICATION_MAX_BYTES', total):
            with retention.publication(self.scope): pass
        self.assertFalse((self.scope / retention.DISABLED).exists())

    def test_absent_pending_does_not_consume_delete_budget(self):
        generations = [self.publish(value=i) for i in range(5)]
        self.age()
        original = Path.unlink
        def interrupted(path, *args, **kwargs):
            if path.parent.name == generations[1]:
                raise OSError('interrupt second generation')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', interrupted):
            self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 1)
        result = retention.maintain_scope(self.scope, delete_limit=1)
        self.assertEqual(result['deleted'], 1, result)
        self.assertEqual(result['pending'], 0, result)
