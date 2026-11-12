"""Real publications/files/codecs: read and reuse share integrity gates."""
from copy import deepcopy
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.test import TestCase

from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.services import spec_stats_snapshot as snapshot
from botend.services.simc_benchmark_result_snapshot import _load, _write
from botend.services.stats_talent_dependencies import structure_token


class StatsSnapshotIntegrityTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='stats-integrity-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / '77/Mage/Fire/dungeon.json'
        self.keys = ['dungeon-1', 'dungeon-2', 'dungeon-all']
        self.detail = {'dungeon_id': 1, 'sample_size': 20,
                       'data_contract': 'wcl-log-v2',
                       'notes': ['long description ' * 20] * 2,
                       'top_players': [{'name': 'Fixture', 'realm': 'Realm',
                                        'region': 'eu', 'dps': 100}]}
        self.payload = {'_data_contract': 'wcl-log-v2', '_input_fingerprint': 'frozen-input',
                        'generated_at': '2026-01-01T00:00:00Z',
                        'dungeons': [self.detail, {**self.detail, 'dungeon_id': 2}],
                        'summary': {**self.detail, 'dungeon_id': 'all'}}
        self.enterContext(patch('requests.sessions.Session.request',
                                side_effect=AssertionError('local regression forbids HTTP')))

    def publish(self):
        snapshot.publish_projection(self.path, self.payload, _write)
        return _load(self.path)

    def body_path(self, index, key='dungeon-1'):
        return self.path.parent / 'stats-details' / index['_snapshot']['generation'] / f'{key}.json'

    def reuse(self):
        return snapshot.reusable_projection(self.path, 'frozen-input', self.keys)

    def write_changed_body(self, index, body, *, rehash=False, key='dungeon-1'):
        _write(self.body_path(index, key), body)
        if rehash:
            index['_snapshot']['detail_hashes'][key] = snapshot.content_digest(body['data'])
            _write(self.path, index)

    def test_valid_round_trip_reads_each_file_and_decodes_each_body_once(self):
        index = self.publish()
        body = _load(self.body_path(index))
        self.assertEqual(body['data']['_stats_storage']['schema'], 4)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.root.rglob('*.json')}
        with patch.object(snapshot, '_load', wraps=snapshot._load) as loads, \
                patch.object(snapshot, 'unpack_detail', wraps=snapshot.unpack_detail) as decode:
            self.assertEqual(snapshot.read_projection(self.path, 'dungeon-1')['dungeons'], [self.detail])
        self.assertEqual([call.args[0] for call in loads.call_args_list],
                         [self.path, self.body_path(index)])
        self.assertEqual(decode.call_count, 1)
        with patch.object(snapshot, '_load', wraps=snapshot._load) as loads, \
                patch.object(snapshot, 'unpack_detail', wraps=snapshot.unpack_detail) as decode:
            self.assertEqual(self.reuse(), index)
        self.assertEqual([call.args[0] for call in loads.call_args_list],
                         [self.path, *[self.body_path(index, key) for key in self.keys]])
        self.assertEqual(decode.call_count, len(self.keys))
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns)
                                  for p in self.root.rglob('*.json')})

    def test_changed_body_checksum_is_rejected_by_read_and_reuse(self):
        index = self.publish()
        body = _load(self.body_path(index))
        body['data']['sample_size'] = 999
        self.write_changed_body(index, body)
        self.assertIsNone(self.reuse())
        self.assertIsNone(snapshot.read_projection(self.path, 'dungeon-1'))

    def test_changed_index_checksum_is_rejected_without_loading_details(self):
        index = self.publish()
        index['dungeons'][0]['sample_size'] = 999
        _write(self.path, index)
        for key in (None, 'dungeon-1'):
            with self.subTest(key=key), patch.object(snapshot, '_load', wraps=snapshot._load) as loads:
                self.assertIsNone(snapshot.read_projection(self.path, key))
                self.assertEqual([call.args[0] for call in loads.call_args_list], [self.path])
        self.assertIsNone(self.reuse())

    def test_hash_consistent_unknown_codec_and_illegal_references_cannot_reuse(self):
        index = self.publish()
        original = _load(self.body_path(index))
        for kind in ('unknown-schema', 'player-reference', 'local-text-reference'):
            with self.subTest(kind=kind):
                body = deepcopy(original)
                if kind == 'unknown-schema':
                    body['data']['_stats_storage']['schema'] = 999
                elif kind == 'player-reference':
                    body['data']['top_players'][0]['_stats_player_ref'] = 999
                else:
                    body['data']['notes'][0]['_stats_text_ref'] = 999
                self.write_changed_body(index, body, rehash=True)
                self.assertIsNone(snapshot.read_projection(self.path, 'dungeon-1'))
                self.assertIsNone(self.reuse())

    def test_single_detail_does_not_load_unrelated_corrupt_body_but_reuse_rejects_it(self):
        index = self.publish()
        body = _load(self.body_path(index, 'dungeon-2'))
        body['data']['_stats_storage']['schema'] = 999
        self.write_changed_body(index, body, rehash=True, key='dungeon-2')
        with patch.object(snapshot, '_load', wraps=snapshot._load) as loads:
            self.assertEqual(snapshot.read_projection(self.path, 'dungeon-1')['dungeons'], [self.detail])
        self.assertEqual([call.args[0] for call in loads.call_args_list],
                         [self.path, self.body_path(index)])
        self.assertIsNone(self.reuse())

    def test_central_references_must_resolve_without_replacing_codec(self):
        version = WowTalentVersion.objects.create(key='integrity-reference', branch='retail')
        description = ('Central reference fixture description. ' * 10).strip()
        row = WowTalentNodeMetadata.objects.create(
            talent_version=version, class_name='Mage', spec_name='Fire', tree_type='spec',
            node_id=1001, talent_id=2001, spell_id=3001, name='Fixture',
            description=description, description_zh=description, source='wago')
        self.detail.update(talent_popularity_tree={
            'metadata_version_id': version.pk,
            'render_model': {'tree_set': {'class_name': 'Mage', 'spec_name': 'Fire'}}},
            talent_usage=[{'node_id': 1001, 'talent_id': 2001, 'spell_id': 3001,
                           'description': description, 'description_zh': description}])
        index = self.publish()
        packed = _load(self.body_path(index))['data']
        self.assertIsNotNone(packed['_stats_storage']['metadata'])
        self.assertIn('_stats_talent_meta', packed['talent_usage'][0])
        self.assertEqual(snapshot.read_projection(self.path, 'dungeon-1')['dungeons'], [self.detail])
        self.assertEqual(self.reuse(), index)
        # A hash-consistent body still cannot dereference a replaced central identity.
        WowTalentNodeMetadata.all_objects.filter(pk=row.pk).update(spell_id=9999)
        self.assertEqual(snapshot.read_projection(self.path, 'dungeon-1'),
                         {'_stats_refresh_required': True})
        self.assertIsNone(self.reuse())

    def test_structure_token_and_data_contract_guards_remain(self):
        self.payload['_talent_structure_token'] = structure_token('Mage')
        index = self.publish()
        self.assertEqual(self.reuse(), index)
        index['_snapshot']['talent_structure_token'] = 'changed-structure'
        _write(self.path, index)
        self.assertEqual(snapshot.read_projection(self.path, 'dungeon-1'),
                         {'_stats_refresh_required': True})
        self.assertIsNone(self.reuse())
        self.payload.pop('_talent_structure_token')
        self.payload['_data_contract'] = 'old-mixed-data'
        self.publish()
        self.assertEqual(snapshot.read_log_projection(self.path, 'dungeon-1'),
                         {'_stats_refresh_required': True})

    def test_envelope_scope_generation_schema_and_required_order_remain_strict(self):
        index = self.publish()
        original = _load(self.body_path(index))
        for field, value in (('schema', 999), ('scope', ['78', 'Mage', 'Fire']),
                             ('generation', '0' * 32), ('key', 'dungeon-2')):
            with self.subTest(field=field):
                self.write_changed_body(index, {**original, field: value})
                self.assertEqual(snapshot.read_projection(self.path, 'dungeon-1')['dungeons'], [])
                self.assertIsNone(self.reuse())
        self.write_changed_body(index, original)
        self.assertIsNone(snapshot.reusable_projection(self.path, 'changed-input', self.keys))
        self.assertIsNone(snapshot.reusable_projection(self.path, 'frozen-input', self.keys[::-1]))
        for field, value in (('schema', 999), ('scope', ['78', 'Mage', 'Fire']),
                             ('generation', '../other'), ('module', 'raid')):
            with self.subTest(index_field=field):
                invalid = deepcopy(index)
                invalid['_snapshot'][field] = value
                _write(self.path, invalid)
                self.assertIsNone(snapshot.read_projection(self.path, 'dungeon-1'))
                self.assertIsNone(self.reuse())

    def test_legacy_readability_does_not_enable_unverified_reuse(self):
        _write(self.path, self.payload)
        self.assertEqual(snapshot.read_projection(self.path, 'dungeon-1'), self.payload)
        self.assertIsNone(self.reuse())
        index = self.publish()
        del index['_snapshot']['index_hash']
        del index['_snapshot']['detail_hashes']
        _write(self.path, index)
        self.assertEqual(snapshot.read_projection(self.path, 'dungeon-1')['dungeons'], [self.detail])
        self.assertIsNone(self.reuse())

    def test_raid_reuse_and_failed_publication_preserve_last_good(self):
        self.path = self.path.with_name('raid.json')
        boss = {**self.detail, 'boss_id': 1}
        self.payload = {'_data_contract': 'wcl-log-v2', '_input_fingerprint': 'frozen-input',
                        'zone_groups': [{'zone_id': 1, 'bosses': [boss]}]}
        self.keys = ['raid-5-1']
        index = self.publish()
        self.assertEqual(self.reuse(), index)
        self.assertEqual(snapshot.read_projection(self.path, 'raid-5-1')['zone_groups'][0]['bosses'], [boss])
        before = self.path.read_bytes()
        snapshot.publish_projection(self.path, self.payload, _write)
        self.assertEqual(self.path.read_bytes(), before)
        self.payload['zone_groups'][0]['bosses'][0]['sample_size'] = 21
        def failing_write(path, value):
            if path == self.path:
                raise OSError('publication interrupted')
            _write(path, value)
        with self.assertRaises(OSError):
            snapshot.publish_projection(self.path, self.payload, failing_write)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(snapshot.read_projection(self.path, 'raid-5-1')['zone_groups'][0]['bosses'][0]['sample_size'], 20)
