"""Real ORM/files: repeated publication must not manufacture new facts/artifacts."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from botend.models import PortalMythicstatsDpsRow, SeasonMeta, SpecRaidRanking
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor
from botend.services import mythicstats_snapshot as mythic, spec_stats_snapshot as stats
from botend.services.simc_benchmark_result_snapshot import _write
from botend.tests.test_mythicstats_snapshot import payload
from botend.tests.test_spec_stats_snapshot import fixture


class RaidIdempotencyTests(SimpleTestCase):
    def test_business_changes_invalidate_even_outside_overview_and_fact_times(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '3/Mage/Frost/raid.json'
            data = fixture('raid', size=10)
            stats.publish_projection(path, data, _write)
            for kind in ('display', 'fact_time', 'algorithm', 'label', 'extra_payload'):
                before = stats.read_projection(path)['_snapshot']['generation']
                boss = data['difficulties'][1]['zone_groups'][0]['bosses'][-1]
                if kind == 'display':
                    boss['talent_usage'][0]['description'] = 'new resolved tooltip'
                elif kind == 'fact_time':
                    boss['last_updated'] = '2026-10-09T10:00:00Z'
                elif kind == 'algorithm':
                    data['_algorithm_version'] = 'v2'
                elif kind == 'label':
                    data['difficulties'][1]['label'] = 'new label'
                else:
                    data['not_projected_but_business_relevant'] = {'version': 2}
                stats.publish_projection(path, data, _write)
                self.assertNotEqual(stats.read_projection(path)['_snapshot']['generation'], before, kind)

    def test_dungeon_fingerprint_body_validation_and_legacy_read_survive(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '3/Mage/Frost/dungeon.json'
            data = fixture('dungeon', size=10)
            _write(path, data)
            self.assertEqual(stats.read_projection(path), data)
            self.assertIsNone(stats.reusable_projection(path, 'abc', ['dungeon-1']))
            data['_input_fingerprint'] = 'abc'
            stats.publish_projection(path, data, _write)
            index = stats.read_projection(path)
            keys = index['_snapshot']['details']
            self.assertEqual(stats.reusable_projection(path, 'abc', keys), index)
            self.assertIsNone(stats.reusable_projection(path, 'changed', keys))
            target = path.parent / 'stats-details' / index['_snapshot']['generation'] / 'dungeon-all.json'
            body = json.loads(target.read_text())
            body['data']['sample_size'] = 999
            _write(target, body)
            self.assertIsNone(stats.reusable_projection(path, 'abc', keys))

    def test_bad_index_scope_missing_body_and_legacy_generation_rebuild(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '3/Mage/Frost/raid.json'
            data = fixture('raid', size=10)
            _write(path, data)
            self.assertEqual(stats.read_projection(path), data)
            stats.publish_projection(path, data, _write)
            for failure in ('index', 'scope', 'missing', 'malformed'):
                index = stats.read_projection(path)
                generation = index['_snapshot']['generation']
                bodypath = path.parent / 'stats-details' / generation / 'raid-4-8.json'
                if failure == 'index':
                    index['difficulties'][1]['label'] = 'damaged'
                    _write(path, index)
                elif failure == 'scope':
                    body = json.loads(bodypath.read_text())
                    body['scope'] = ['wrong', 'Mage', 'Frost']
                    _write(bodypath, body)
                elif failure == 'missing':
                    bodypath.unlink()
                else:
                    bodypath.write_text('{')
                stats.publish_projection(path, data, _write)
                self.assertNotEqual(stats.read_projection(path)['_snapshot']['generation'], generation)

    def test_identical_full_payload_reuses_all_files_and_checks_every_detail(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '3/Mage/Frost/raid.json'
            data = fixture('raid', size=10)
            data['generated_at'] = '2026-01-01T00:00:00Z'
            stats.publish_projection(path, data, _write)
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(root).rglob('*.json')}
            data['generated_at'] = '2026-01-02T00:00:00Z'
            stats.publish_projection(path, data, _write)
            self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(root).rglob('*.json')})
            index = stats.read_projection(path)
            bad = path.parent / 'stats-details' / index['_snapshot']['generation'] / 'raid-4-8.json'
            body = json.loads(bad.read_text())
            body['data']['gear_popularity'][0]['name'] = 'corrupt'
            _write(bad, body)
            stats.publish_projection(path, data, _write)
            self.assertNotEqual(stats.read_projection(path)['_snapshot']['generation'], index['_snapshot']['generation'])


class RaidORMIdempotencyTests(TestCase):
    def test_real_rankings_aggregation_publication_reuses_then_changes(self):
        season = SeasonMeta.objects.create(season_key='raid-idempotency', season_name='Test',
            mplus_zone_id=1, raid_zone_id=1,
            raid_encounters=[{'id': 1, 'name': 'One'}], raid_zones=[])
        for difficulty in (4, 5):
            SpecRaidRanking.objects.create(season_id=season.id, boss_id=1, boss_name='One',
                difficulty=difficulty, class_name='Warrior', spec_name='Arms', character_name='Test',
                realm='Realm', region='eu', dps=100, kill_time=100000)
        with tempfile.TemporaryDirectory() as root, patch('requests.sessions.Session.request',
                side_effect=AssertionError('no network during local regression')):
            directory = Path(root) / str(season.pk) / 'Warrior/Arms'
            def publish():
                SpecDetailAggregationMonitor._aggregate_raid(season, 'Warrior', 'Arms', directory)
            publish()
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(root).rglob('*.json')}
            rows = list(SpecRaidRanking.objects.order_by('pk').values())
            publish()
            self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(root).rglob('*.json')})
            self.assertEqual(rows, list(SpecRaidRanking.objects.order_by('pk').values()))
            old = stats.read_projection(directory / 'raid.json')['_snapshot']['generation']
            SpecRaidRanking.objects.filter(difficulty=4).update(dps=200)
            publish()
            result = stats.read_projection(directory / 'raid.json', 'raid-4-1')
            self.assertNotEqual(result['_snapshot']['generation'], old)
            self.assertEqual(result['difficulties'][1]['zone_groups'][0]['bosses'][0]['dps']['avg'], 200)


class MythicIdempotencyTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        override = override_settings(MYTHICSTATS_SNAPSHOT_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.data = payload()
        self.data['periods'] = self.data['periods'][:1]
        self.data['dungeons'] = [{'id': 0, 'name': 'All'}]
        self.fetch = patch.object(mythic.source, 'fetch_mythicstats_dps', side_effect=lambda **kw: deepcopy(self.data))
        self.fetch.start()
        self.addCleanup(self.fetch.stop)

    def rows(self):
        return list(PortalMythicstatsDpsRow.objects.order_by('pk').values())

    def bodies(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.glob('*.json') if p.name != 'index.json'}

    def test_identical_normalized_source_no_database_writes_or_new_body(self):
        mythic.collect_snapshots()
        rows, bodies = self.rows(), self.bodies()
        for values in self.data['rankings'].values():
            values[0]['spec_name'] = '  ' + values[0]['spec_name'] + '  '
            values[0]['rank'] = str(values[0]['rank'])
            values[0]['avg_value'] = str(values[0]['avg_value'])
        self.data['source_note'] = '  ' + self.data['source_note'] + '  '
        with CaptureQueriesContext(connection) as queries:
            mythic.collect_snapshots()
        self.assertEqual(self.rows(), rows)
        self.assertEqual(self.bodies(), bodies)
        self.assertFalse([q['sql'] for q in queries if q['sql'].lstrip().upper().startswith(('INSERT', 'DELETE', 'UPDATE'))])

    def entry(self):
        return mythic._manifest()['seasons']['test-season']['shards']['0:103']

    def test_historical_skip_checks_file_scope_and_current_database(self):
        requests = []
        def fetch(**kwargs):
            pid = kwargs.get('period_id') or 103
            requests.append(pid)
            result = payload(period_id=pid)
            result['dungeons'] = [{'id': 0, 'name': 'All'}]
            return result
        with patch.object(mythic.source, 'fetch_mythicstats_dps', side_effect=fetch), \
                patch.object(mythic.source, 'fetch_period_season_slug', return_value=('test-season', '')):
            mythic.collect_snapshots()
            for failure in ('scope', 'database'):
                entry = mythic._manifest()['seasons']['test-season']['shards']['0:102']
                if failure == 'scope':
                    target = self.root / entry['file']
                    body = json.loads(target.read_text())
                    body['active_period'] = 101
                    _write(target, body)
                else:
                    PortalMythicstatsDpsRow.objects.filter(period_id=102, role='tank').delete()
                requests.clear()
                self.assertEqual(mythic.collect_snapshots()['failed'], 0)
                self.assertEqual(requests, [103, 102])
                self.assertEqual(PortalMythicstatsDpsRow.objects.filter(period_id=102).count(), 3)
                self.assertEqual(mythic.read_snapshot(period_id=102)['active_period'], 102)

    def test_changed_facts_and_metadata_then_repeat(self):
        mythic.collect_snapshots()
        old = self.entry()
        self.data['rankings']['damage'][0]['runs_value'] = 17000  # not serialized in the body
        mythic.collect_snapshots()
        self.assertNotEqual(self.entry()['file'], old['file'])
        self.assertEqual(PortalMythicstatsDpsRow.objects.get(role='damage').runs_value, 17000)
        rows, old = self.rows(), self.entry()
        self.data['source_note'] = 'changed source note'
        mythic.collect_snapshots()
        self.assertEqual(self.rows(), rows)
        self.assertNotEqual(self.entry()['file'], old['file'])
        bodies = self.bodies()
        mythic.collect_snapshots()
        self.assertEqual(self.rows(), rows)
        self.assertEqual(self.bodies(), bodies)

    def test_corrupt_shards_rebuild_without_recreating_database_facts(self):
        mythic.collect_snapshots()
        rows = self.rows()
        for failure in ('hash', 'scope', 'role', 'missing', 'malformed'):
            old = self.entry()
            target = self.root / old['file']
            body = json.loads(target.read_text())
            if failure == 'missing':
                target.unlink()
            elif failure == 'malformed':
                target.write_text('{')
            else:
                if failure == 'hash':
                    body['roles']['damage'][0]['avg_value'] = -1
                elif failure == 'scope':
                    body['dungeon_id'] = 999
                else:
                    body['roles']['healer'] = []
                _write(target, body)
            result = mythic.collect_snapshots()
            self.assertEqual(result['failed'], 0)
            self.assertNotEqual(self.entry()['file'], old['file'])
            self.assertEqual(self.rows(), rows)
            self.assertEqual(mythic.read_snapshot()['snapshot']['state'], 'ready')

    def test_current_database_facts_are_verified_not_only_manifest_hash(self):
        mythic.collect_snapshots()
        old = self.entry()['file']
        for change in ('update', 'delete', 'extra'):
            if change == 'update':
                PortalMythicstatsDpsRow.objects.filter(role='damage').update(runs_value=-5)
            elif change == 'delete':
                PortalMythicstatsDpsRow.objects.filter(role='tank').delete()
            else:
                PortalMythicstatsDpsRow.objects.create(season='test-season', period_id=103,
                    dungeon_id=0, role='damage', spec_slug='unexpected')
            mythic.collect_snapshots()
            self.assertEqual(PortalMythicstatsDpsRow.objects.count(), 3)
            self.assertEqual(PortalMythicstatsDpsRow.objects.get(role='damage').runs_value, 16000)
            self.assertEqual(self.entry()['file'], old)  # repaired facts match valid existing artifact

    def test_normalized_duplicates_and_wrong_scope_keep_old(self):
        mythic.collect_snapshots()
        rows, old = self.rows(), self.entry()['file']
        original = deepcopy(self.data)
        for failure in ('duplicate', 'empty', 'scope'):
            self.data = deepcopy(original)
            if failure == 'duplicate':
                duplicate = deepcopy(self.data['rankings']['damage'][0])
                duplicate['spec_slug'] = ' ' + duplicate['spec_slug'] + ' '
                self.data['rankings']['damage'].append(duplicate)
            elif failure == 'empty':
                self.data['rankings']['healer'] = []
            else:
                self.data['dungeon_id'] = 999
            with self.assertRaises(ValueError):
                mythic.collect_snapshots()
            self.assertEqual(self.rows(), rows)
            self.assertEqual(self.entry()['file'], old)

    def test_shard_write_failure_rolls_back_all_roles_and_index_failure_keeps_pointer(self):
        mythic.collect_snapshots()
        rows, old = self.rows(), self.entry()['file']
        self.data['rankings']['damage'][0]['avg_value'] = 999
        write = mythic._write
        def fail_body(path, data):
            if path.name != 'index.json':
                raise OSError('body failed')
            return write(path, data)
        with patch.object(mythic, '_write', side_effect=fail_body):
            self.assertEqual(mythic.collect_snapshots()['failed'], 1)
        self.assertEqual(self.rows(), rows)
        self.assertEqual(self.entry()['file'], old)
        self.assertEqual(mythic.read_snapshot()['snapshot']['state'], 'stale')
        before_index = (self.root / 'index.json').read_bytes()
        def fail_index(path, data):
            if path.name == 'index.json':
                raise OSError('index failed')
            return write(path, data)
        with patch.object(mythic, '_write', side_effect=fail_index), self.assertRaises(OSError):
            mythic.collect_snapshots()
        self.assertEqual((self.root / 'index.json').read_bytes(), before_index)
        self.assertEqual(mythic.read_snapshot()['roles']['damage'][0]['avg_value'], 287000)
        # Scope DB commits precede the final pointer, by the existing protocol.
        rows = self.rows()
        mythic.collect_snapshots()
        self.assertEqual(self.rows(), rows)
        self.assertEqual(mythic.read_snapshot()['roles']['damage'][0]['avg_value'], 999)
