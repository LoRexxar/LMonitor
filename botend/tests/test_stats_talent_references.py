"""验证统计事实独立存储、现有资料引用、批量只读和旧结果兼容。"""
import json
from copy import deepcopy
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext

from botend.models import WowTalentVersion, WowTalentNodeMetadata
from botend.services.stats_storage_codec import pack_detail, unpack_detail
from botend.services.stats_talent_references import REFERENCE
from botend.wow.talents.models import TalentTreeSetModel, TalentBuildStateModel
from botend.wow.talents.render import build_talent_render_model


class StatsTalentReferenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.version = WowTalentVersion.objects.create(key='storage-reference-test', branch='retail')
        cls.description = '数据库里已有的完整技能说明。' * 200
        cls.row = WowTalentNodeMetadata.objects.create(talent_version=cls.version,
            class_name='Mage', spec_name='Fire', tree_type='spec', node_id=1001,
            talent_id=2001, spell_id=3001, name='测试天赋', icon='https://example.test/talent-icon.jpg',
            description=cls.description, description_zh=cls.description,
            row=1, column=1, max_points=2, source='wago')

    def payload(self):
        tree_set = TalentTreeSetModel(class_name='Mage', spec_name='Fire', trees=[{
            'tree_type': 'spec', 'nodes': [{'node_id': 1001, 'talent_id': 2001, 'spell_id': 3001,
            'name': self.row.name, 'icon': self.row.icon, 'description': self.description,
            'description_zh': self.description, 'row': 1, 'column': 1, 'points': 1,
            'max_points': 2, 'selected': True, 'source': 'wago'}]}])
        model = build_talent_render_model(tree_set=tree_set,
            build_state=TalentBuildStateModel(selected_nodes={'1001'})).to_dict()
        for node in [*model['nodes'], *model['trees'][0]['nodes']]:
            node.update(count=78, usage_pct=78.0, point_distribution=[{'points': 1, 'count': 78}])
        return {'dungeon_id': 1, 'sample_size': 100, 'talent_popularity_tree': {
            'metadata_version_id': self.version.pk, 'render_model': model}}

    def test_database_text_is_absent_and_render_structure_is_rebuilt_losslessly(self):
        original = self.payload()
        untouched = deepcopy(original)
        with CaptureQueriesContext(connection) as queries:
            packed = pack_detail(original)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertTrue(packed['_stats_storage']['recipe'])
        self.assertTrue(packed['_stats_storage']['metadata'])
        encoded = json.dumps(packed, ensure_ascii=False)
        self.assertNotIn(self.description, encoded)
        recipe = packed['talent_popularity_tree']['render_model']
        self.assertNotIn('layout', recipe)
        self.assertNotIn('nodes', recipe)
        self.assertEqual(original, untouched)
        with self.assertNumQueries(2):
            self.assertEqual(unpack_detail(json.loads(encoded)), original)

    def test_many_references_still_use_two_read_queries(self):
        detail = self.payload()
        detail['repeated_nodes'] = [deepcopy(detail['talent_popularity_tree']['render_model']['nodes'][0])
                                    for _ in range(200)]
        packed = pack_detail(detail)
        with self.assertNumQueries(2):
            restored = unpack_detail(json.loads(json.dumps(packed)))
        self.assertEqual(restored, detail)

    def test_one_publication_reuses_catalog_across_scopes(self):
        cache = {}
        with self.assertNumQueries(2):
            first = pack_detail(self.payload(), catalog_cache=cache)
            second = pack_detail({**self.payload(), 'dungeon_id': 2}, catalog_cache=cache)
        self.assertEqual(first['_stats_storage']['metadata'], second['_stats_storage']['metadata'])

    def test_disk_cache_publication_and_gc_keep_reference_boundaries(self):
        from botend.services import spec_aggregation_cache as cache
        from botend.services import spec_stats_snapshot as snapshot
        from botend.services.spec_stats_retention import maintain_scope
        from botend.services.simc_benchmark_result_snapshot import _write
        with tempfile.TemporaryDirectory() as temporary, self.settings(
                SPEC_AGGREGATION_WORK_ROOT=Path(temporary) / 'work'):
            detail = self.payload()
            cache.save_work(1, 'test:reference', 'v1', detail)
            with self.assertNumQueries(2):
                self.assertEqual(cache.load_work(1, 'test:reference', 'v1'), detail)
            scope = Path(temporary) / 'aggregated/1/Mage/Fire'
            path = scope / 'dungeon.json'
            snapshot.publish_projection(path, {'dungeons': [detail]}, _write)
            with self.assertNumQueries(2):
                self.assertEqual(snapshot.read_projection(path, 'dungeon-1')['dungeons'], [detail])
            with self.assertNumQueries(0), patch(
                    'botend.services.stats_talent_references._catalog', side_effect=AssertionError('回收不应展开资料引用')):
                self.assertEqual(maintain_scope(scope, dry_run=True)['status'], 'ok')

    def test_central_text_update_does_not_change_frozen_selection_or_counts(self):
        packed = pack_detail(self.payload())
        WowTalentNodeMetadata.all_objects.filter(pk=self.row.pk).update(
            name='修正后的名称', description='修正后的描述',
            row=99, column=99, max_points=5)
        restored = unpack_detail(packed)['talent_popularity_tree']['render_model']
        node = restored['nodes'][0]
        self.assertEqual(node['name'], '修正后的名称')
        self.assertEqual(node['description'], '修正后的描述')
        self.assertEqual((node['points'], node['count'], node['row'], node['column'], node['max_points']),
                         (1, 78, 1, 1, 2))

    def test_changed_identity_or_foreign_scope_is_rejected(self):
        packed = pack_detail(self.payload())
        wrong = deepcopy(packed)
        wrong['_stats_storage']['metadata']['spec_name'] = 'Frost'
        with self.assertRaises(ValueError):
            unpack_detail(wrong)
        WowTalentNodeMetadata.all_objects.filter(pk=self.row.pk).update(spell_id=9999)
        with self.assertRaises(ValueError):
            unpack_detail(packed)
        wrong = deepcopy(packed)
        wrong['_stats_storage']['metadata']['rows'][0][0] = 'class'
        with self.assertRaises(ValueError):
            unpack_detail(wrong)

    def test_invalid_reference_and_recipe_are_rejected(self):
        packed = pack_detail(self.payload())
        invalid = deepcopy(packed)
        invalid['_stats_storage']['metadata']['rows'][0][0] = 99999999
        with self.assertRaises(ValueError):
            unpack_detail(invalid)
        invalid = deepcopy(packed)
        invalid['talent_popularity_tree']['render_model']['delta'] = {'items': [[999, {'set': 1}]]}
        with self.assertRaises(ValueError):
            unpack_detail(invalid)

    def test_old_full_detail_needs_no_database_read(self):
        detail = self.payload()
        with self.assertNumQueries(0):
            self.assertEqual(unpack_detail(detail), detail)

    def test_render_delta_keeps_numeric_types_and_unknown_fields(self):
        from botend.services.stats_talent_render_codec import _difference, _apply, _encode_delta, _decode_delta
        before = {'rows': [{'count': 1, 'remove': '旧字段'}]}
        after = {'rows': [{'count': 1.0, 'extension': [None, True, {'value': '保留'}]}]}
        restored = _apply(before, _decode_delta(_encode_delta(_difference(before, after))))
        self.assertEqual(json.dumps(restored, sort_keys=True), json.dumps(after, sort_keys=True))

    def test_player_summaries_are_stored_once_and_invalid_indices_rejected(self):
        player = {'name': '样本角色', 'realm': '服务器', 'region': 'CN', 'dps': 123.5}
        detail = self.payload()
        detail['top_players'] = [player] * 100
        packed = pack_detail(detail)
        self.assertEqual(packed['_stats_storage']['players'], [player])
        self.assertEqual(unpack_detail(json.loads(json.dumps(packed))), detail)
        packed['top_players'][0]['_stats_player_ref'] = 2
        with self.assertRaises(ValueError):
            unpack_detail(packed)

    def test_unknown_extension_and_reference_collision_are_preserved(self):
        detail = self.payload()
        detail['unknown'] = {REFERENCE: [9, 9], 'keep': '保留业务扩展'}
        detail['talent_popularity_tree']['render_model']['extension'] = {'value': [None, '未知扩展']}
        packed = pack_detail(detail)
        self.assertIsNone(packed['_stats_storage']['metadata'])
        self.assertEqual(unpack_detail(json.loads(json.dumps(packed))), detail)
