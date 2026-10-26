"""Real MySQL 8/InnoDB publication fences (explicit target, never SQLite skips).

Run with a private MySQL test database and Django's TransactionTestCase runner.
The only intercepted boundary is the JSON writer: it still writes/fsyncs/renames
real files, then waits on Events. ORM fences, metadata checks and flock are real.
No external equipment/talent lookups are needed by these minimal payloads.
"""
import importlib
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from unittest.mock import patch

from django.db import connection, connections, transaction, OperationalError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from botend.models import (SeasonMeta, WclCollectionCycle, WowTalentVersion,
                           WowTalentNodeMetadata)
from botend.services import spec_stats_snapshot as snapshot
from botend.services import spec_stats_retention as retention
from botend.services.stats_talent_dependencies import structure_token
from botend.services.wcl_collection_cycle import CycleOwnershipLost

publisher = importlib.import_module(
    'botend.controller.plugins.portal.SpecDetailAggregationMonitor')


class MySQLPublicationFenceTests(TransactionTestCase):
    """Fail explicitly on the wrong backend; a skipped test is not lock evidence."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != 'mysql':
            raise AssertionError('This explicit integration target requires MySQL 8/InnoDB')
        with connection.cursor() as cursor:
            cursor.execute('SELECT VERSION(), @@version_comment, @@skip_networking')
            version, comment, isolated = cursor.fetchone()
            if not version.startswith('8.') or 'MariaDB' in comment:
                raise AssertionError(f'Requires real MySQL 8, got {version} {comment}')
            print(f'PUBLICATION_BACKEND {version} {comment} skip_networking={isolated}', flush=True)
            for model in (WclCollectionCycle, WowTalentVersion, WowTalentNodeMetadata):
                cursor.execute('SELECT ENGINE FROM information_schema.TABLES '
                               'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s',
                               [model._meta.db_table])
                if cursor.fetchone() != ('InnoDB',):
                    raise AssertionError(f'{model._meta.db_table} must use InnoDB')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='publication-fence-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        override = override_settings(MEDIA_ROOT=str(self.root))
        override.enable()
        self.addCleanup(override.disable)
        self.season = SeasonMeta.objects.create(season_key='publication-fence',
            season_name='Fence', mplus_zone_id=1, raid_zone_id=2)
        self.cycle = WclCollectionCycle.objects.create(season=self.season,
            slot=timezone.now(), owner='original-owner', phase='aggregating')
        self.version = WowTalentVersion.objects.create(key='retail', branch='retail',
            status='active', is_active=True, source_dir=str(self.root / 'absent-metadata'))
        self.node = WowTalentNodeMetadata.objects.create(talent_version=self.version,
            class_name='Mage', spec_name='Fire', tree_type='spec',
            node_id=100, talent_id=200, spell_id=300)
        self.token = structure_token('Mage')
        self.path = self.root / 'aggregated' / str(self.season.pk) / 'Mage/Fire/dungeon.json'
        self._seed()
        with connection.cursor() as cursor:
            cursor.execute('SELECT CONNECTION_ID()')
            self.main_connection_id = cursor.fetchone()[0]
            cursor.execute('SET SESSION innodb_lock_wait_timeout=2')

    def payload(self, sample_size=2):
        detail = {'dungeon_id': 1, 'boss_id': 1, 'sample_size': sample_size,
                  'dps': {'median': 123}, 'name': 'Local fixture'}
        result = ({'dungeons': [detail]} if self.path.stem == 'dungeon' else
                  {'difficulties': [{'difficulty': 5, 'zone_groups': [
                      {'zone_id': 2, 'bosses': [detail]}]}]})
        result['_talent_structure_token'] = self.token
        return result

    def _seed(self):
        snapshot.publish_projection(self.path, self.payload(1), publisher.atomic_dump_json)
        # Preserve literal formatting, not merely semantic JSON equivalence.
        self.old_bytes = (json.dumps(json.loads(self.path.read_bytes()),
                                     indent=3, ensure_ascii=False) + '\n').encode()
        self.path.write_bytes(self.old_bytes)

    def _paused_publish(self, stage, inspect):
        arrived, release = threading.Event(), threading.Event()
        result = queue.Queue()
        observations = {}
        real_write = publisher.atomic_dump_json

        def controlled_writer(target, data, **kwargs):
            real_write(target, data, **kwargs)
            is_index = Path(target) == self.path
            if ((stage == 'index' and is_index) or
                    (stage == 'details' and not is_index)) and not arrived.is_set():
                observations['atomic'] = connection.in_atomic_block
                observations['target'] = Path(target)
                with connection.cursor() as cursor:
                    cursor.execute('SELECT CONNECTION_ID()')
                    observations['connection_id'] = cursor.fetchone()[0]
                arrived.set()
                if not release.wait(10):
                    raise AssertionError('controller did not release real writer within 10s')

        def publish():
            connections.close_all()
            try:
                publisher.publish_stats(self.path, self.payload(), 'Mage', 'Fire', cycle=self.cycle)
                result.put(None)
            except BaseException as exc:
                result.put(exc)
            finally:
                connections.close_all()

        with patch.object(publisher, 'atomic_dump_json', controlled_writer):
            worker = threading.Thread(target=publish, name='real-mysql-publisher', daemon=True)
            worker.start()
            try:
                self.assertTrue(arrived.wait(10), 'publisher did not reach real writer')
                self.assertNotEqual(observations['connection_id'], self.main_connection_id)
                self.assertTrue(observations['target'].is_file())
                inspect(observations)
            finally:
                release.set()
                worker.join(12)
                self.assertFalse(worker.is_alive(), 'publisher did not terminate')
        return result.get(timeout=1)

    def _take_cycle_lock(self):
        started = time.monotonic()
        with transaction.atomic():
            row = WclCollectionCycle.objects.select_for_update(nowait=True).get(pk=self.cycle.pk)
            self.assertEqual(row.owner, self.cycle.owner)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, 'independent cycle lock was not promptly available')
        print(f'PREPARE_CYCLE_LOCK module={self.path.stem} elapsed={elapsed:.6f}s '
              f'connection={self.main_connection_id}', flush=True)

    def _prepare_and_gc(self):
        def inspect(observations):
            # Exercise the actual conflicting SELECT first, including in the
            # pre-fix regression control, rather than relying on ORM state.
            self._take_cycle_lock()
            self.assertFalse(observations['atomic'], 'heavy preparation holds a DB transaction')
            self.assertEqual(self.path.read_bytes(), self.old_bytes)
            # The real maintenance entrypoint tries the actual nonblocking flock.
            self.assertEqual(retention.maintain_scope(self.path.parent)['status'], 'locked')
            self.assertTrue(observations['target'].is_file())
        self.assertIsNone(self._paused_publish('details', inspect))
        self.assertNotEqual(self.path.read_bytes(), self.old_bytes)
        self.assertIsNotNone(snapshot.read_projection(self.path))
        with retention._scope_lock(self.path.parent / retention.LOCK, blocking=False) as acquired:
            self.assertTrue(acquired, 'scope lock leaked after publication')

    def test_dungeon_prepare_releases_cycle_but_excludes_real_gc(self):
        self._prepare_and_gc()

    def test_raid_prepare_releases_cycle_but_excludes_real_gc(self):
        self.path = self.path.with_name('raid.json')
        self._seed()
        self._prepare_and_gc()

    def test_actual_index_replace_still_holds_fenced_cycle(self):
        def inspect(observations):
            self.assertTrue(observations['atomic'])
            self.assertNotEqual(self.path.read_bytes(), self.old_bytes,
                                'pause must be after the actual os.replace')
            with self.assertRaises(OperationalError) as raised:
                with transaction.atomic():
                    WclCollectionCycle.objects.select_for_update(nowait=True).get(pk=self.cycle.pk)
            self.assertEqual(raised.exception.args[0], 3572, 'expected MySQL NOWAIT row-lock conflict')
            print('INDEX_REPLACE_FENCE real second-connection NOWAIT rejected: 3572', flush=True)
        self.assertIsNone(self._paused_publish('index', inspect))
        # Once the publisher commits, the same competing connection can lock it.
        self._take_cycle_lock()

    def _reject_changed_cycle(self, **updates):
        def inspect(observations):
            self.assertFalse(observations['atomic'])
            self._take_cycle_lock()
            self.assertEqual(WclCollectionCycle.objects.filter(pk=self.cycle.pk).update(**updates), 1)
        error = self._paused_publish('details', inspect)
        self.assertIsInstance(error, CycleOwnershipLost)
        self.assertEqual(self.path.read_bytes(), self.old_bytes)
        self.cycle.refresh_from_db()
        for key, value in updates.items():
            self.assertEqual(getattr(self.cycle, key), value)

    def test_owner_changed_during_prepare_rejects_and_preserves_exact_index(self):
        self._reject_changed_cycle(owner='replacement-owner')

    def test_phase_changed_during_prepare_rejects_and_preserves_exact_index(self):
        self._reject_changed_cycle(phase='published')

    def test_metadata_changed_during_prepare_rejects_and_preserves_exact_index(self):
        def inspect(observations):
            self.assertFalse(observations['atomic'])
            self._take_cycle_lock()
            # Real independent DB write; no fabricated token or guard mock.
            WowTalentNodeMetadata.objects.filter(pk=self.node.pk).update(spell_id=301)
            self.assertNotEqual(structure_token('Mage'), self.token)
        error = self._paused_publish('details', inspect)
        self.assertIsInstance(error, ValueError)
        self.assertIn('聚合期间天赋结构已更新', str(error))
        self.assertEqual(self.path.read_bytes(), self.old_bytes)
        self.node.refresh_from_db()
        self.assertEqual(self.node.spell_id, 301)

    def test_real_detail_budget_failure_does_not_replace_index(self):
        payload = self.payload()
        payload['dungeons'][0]['fixture_body'] = 'x' * 4096
        with override_settings(SPEC_STATS_DETAIL_MAX_BYTES=1024):
            with self.assertRaisesMessage(ValueError, '统计详情超过单文件预算'):
                publisher.publish_stats(self.path, payload, 'Mage', 'Fire', cycle=self.cycle)
        self.assertEqual(self.path.read_bytes(), self.old_bytes)
        self._take_cycle_lock()
        self.assertFalse(list(self.path.parent.rglob('.tmp-*.json')))
