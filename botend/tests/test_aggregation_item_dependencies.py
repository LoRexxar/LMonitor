"""Real ORM/display/publication regressions for item dependency selection."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from botend.models import SeasonMeta, SpecDungeonRanking, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.dungeon_aggregation import _variant_dependencies, aggregate_dungeon
from botend.services.spec_stats_snapshot import read_projection
from botend.services.wow_item_display import load_item_tooltip_metadata
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import publish_stats


class AggregationItemDependenciesTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.enterContext(self.settings(SPEC_AGGREGATION_WORK_ROOT=root / 'work'))
        self.path = root / 'publication' / 'dungeon.json'
        # Fail closed if this read-only local regression ever acquires a network path.
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.season = SeasonMeta.objects.create(
            season_key='item-dependencies', season_name='Items', is_active=True,
            gear_batch_key='current', gear_synced_at=timezone.now(),
            mplus_zone_id=1, raid_zone_id=2, mplus_encounters=[{'id': 1, 'name': 'One'}])
        self.item = WowItemSnapshot.objects.create(
            item_id=250215, name='Item', name_zh='测试装备', slot_key='head',
            effects={'schema': 1, 'branches': {}})
        self.row = SpecDungeonRanking.objects.create(
            season_id=self.season.pk, dungeon_id=1, dungeon_name='One',
            class_name='Warrior', spec_name='Arms', character_name='A', realm='Realm',
            region='eu', report_code='LOCAL', fight_id=1, dps=100, keystone_level=10,
            gear_json=[{'id': self.item.item_id, 'slot': 'head'}])

    def variant(self, key, level=100, **extra):
        values = dict(item=self.item, season=self.season, batch_key='current',
                      variant_key=key, variant_type='drop_equipment', item_level=level,
                      stats_json={'haste': level})
        values.update(extra)
        return WowItemVariantSnapshot.objects.create(**values)

    def display(self, **extra):
        return load_item_tooltip_metadata([{
            'item_id': self.item.item_id, 'allow_default_variant': True,
            'default_variant_order': 'lowest', **extra}])[0]

    def dependencies(self):
        self.item.refresh_from_db()
        self.row.refresh_from_db()
        return _variant_dependencies({self.item.item_id: self.item},
                                     [{'gear_json': self.row.gear_json}])

    def dependency_ids(self):
        return {row['id'] for row in self.dependencies()[1]}

    def aggregate(self):
        return aggregate_dungeon(self.season, 'Warrior', 'Arms', self.path, publish_stats)

    def tooltip(self):
        detail = read_projection(self.path, 'dungeon-1')['dungeons'][0]
        return next(iter(detail['gear_popularity'].values()))[0]['tooltip']

    def test_default_dependencies_match_real_consumer_not_dedicated_drop(self):
        ordinary = self.variant('ordinary', id=43700)
        dedicated = self.variant('dedicated', level=1, id=60542,
                                 metadata={'difficulty_drop': None})
        self.assertEqual(self.display()['variant_id'], ordinary.pk)
        self.assertEqual(self.dependency_ids(), {ordinary.pk})
        self.assertNotIn(dedicated.pk, self.dependency_ids())

    def test_consumed_mutation_misses_but_unselected_and_dedicated_mutations_hit(self):
        ordinary = self.variant('ordinary')
        other = self.variant('unselected', level=200)
        dedicated = self.variant('dedicated', level=1, metadata={'difficulty_drop': {}})
        first = self.aggregate()
        self.assertFalse(first['reused'])
        self.assertIn('+100 急速', self.tooltip())
        self.assertTrue(self.aggregate()['reused'])
        WowItemVariantSnapshot.objects.filter(pk=ordinary.pk).update(stats_json={'haste': 123})
        changed = self.aggregate()
        self.assertFalse(changed['reused'])
        self.assertNotEqual(changed['fingerprint'], first['fingerprint'])
        self.assertIn('+123 急速', self.tooltip())
        for row in (other, dedicated):
            with self.subTest(unconsumed=row.variant_key):
                WowItemVariantSnapshot.objects.filter(pk=row.pk).update(stats_json={'haste': 999})
                unchanged = self.aggregate()
                self.assertTrue(unchanged['reused'])
                self.assertEqual(unchanged['generation'], changed['generation'])
                self.assertIn('+123 急速', self.tooltip())

    def test_legacy_effects_track_cross_variant_translation(self):
        WowItemSnapshot.objects.filter(pk=self.item.pk).update(effects={})
        ordinary = self.variant('ordinary', effects_json=[{'description': 'Gain 100 haste.'}])
        translator = self.variant('translator', level=200, effects_json=[{
            'description': 'Gain 200 haste.', 'description_zh': '获得200点急速。'}])
        self.assertEqual(self.display()['variant_id'], ordinary.pk)
        first = self.aggregate()
        self.assertIn('获得100点急速', self.tooltip())
        self.assertTrue(self.aggregate()['reused'])
        WowItemVariantSnapshot.objects.filter(pk=translator.pk).update(effects_json=[{
            'description': 'Gain 200 haste.', 'description_zh': '提高200点急速。'}])
        changed = self.aggregate()
        self.assertFalse(changed['reused'])
        self.assertNotEqual(changed['fingerprint'], first['fingerprint'])
        self.assertIn('提高100点急速', self.tooltip())
        self.assertEqual(self.dependency_ids(), {ordinary.pk, translator.pk})
        self.item.refresh_from_db()
        self.assertEqual(self.item.effects, {})  # read-time compilation never persists

    def test_only_legacy_schema_expands_to_all_current_item_variants(self):
        ordinary = self.variant('ordinary')
        ptr = self.variant('ptr', data_branch='ptr')
        dedicated = self.variant('dedicated', level=1, metadata={'difficulty_drop': {}})
        other = self.variant('other', level=200)
        self.variant('old-batch', batch_key='old')
        unrelated = WowItemSnapshot.objects.create(item_id=999999, effects={})
        self.variant('unrelated', item=unrelated)
        self.assertEqual(self.dependency_ids(), {ordinary.pk})
        for effects in ({}, [], {'schema': 0}):
            with self.subTest(effects=effects):
                WowItemSnapshot.objects.filter(pk=self.item.pk).update(effects=effects)
                self.assertEqual(self.dependency_ids(), {ordinary.pk, ptr.pk, dedicated.pk, other.pk})

    def test_exact_level_and_branch_inference_share_display_selection(self):
        low = self.variant('ptr-low', level=100, data_branch='ptr')
        exact = self.variant('ptr-exact', level=200, data_branch='ptr')
        self.variant('retail-dedicated', level=1, metadata={'difficulty_drop': {}})
        self.variant('ptr-exact-later', level=200, data_branch='ptr', bonus_ids=[99])
        SpecDungeonRanking.objects.filter(pk=self.row.pk).update(
            gear_json=[{'id': self.item.item_id, 'slot': 'head', 'itemLevel': 200, 'bonusIDs': [99]}])
        # Popularity discards bonuses; the original exact-level/PK tie-break must remain.
        self.assertEqual(self.display()['variant_id'], low.pk)
        self.assertEqual(self.display(item_level=200)['variant_id'], exact.pk)
        self.assertEqual(self.dependency_ids(), {low.pk, exact.pk})
        self.assertFalse(self.aggregate()['reused'])
        self.assertIn('+200 急速', self.tooltip())
        WowItemVariantSnapshot.objects.filter(pk=exact.pk).update(stats_json={'haste': 234})
        self.assertFalse(self.aggregate()['reused'])
        self.assertIn('+234 急速', self.tooltip())

    def test_reader_bonus_default_and_missing_level_contracts_remain_unchanged(self):
        low = self.variant('low', level=100)
        high = self.variant('high', level=200, bonus_ids=[99])
        for request, expected in (
            ({}, low.pk),
            ({'allow_default_variant': False}, None),
            ({'default_variant_order': 'highest'}, high.pk),
            ({'bonus_ids': '99'}, high.pk),
            ({'item_level': 100, 'bonus_ids': [99]}, low.pk),
            ({'item_level': 999}, None),
        ):
            with self.subTest(request=request):
                self.assertEqual(self.display(**request)['variant_id'], expected)

    def test_active_catalog_scope_and_batch_are_shared(self):
        ordinary = self.variant('ordinary')
        empty = SeasonMeta.objects.create(season_key='empty', season_name='Empty',
                                         mplus_zone_id=3, raid_zone_id=4,
                                         is_active=True, gear_batch_key='new')
        self.assertEqual(self.dependencies()[0], [self.season.pk, 'current'])
        self.assertEqual(self.display()['variant_id'], ordinary.pk)
        replacement = self.variant('replacement', season=empty, batch_key='new')
        SeasonMeta.objects.filter(pk=empty.pk).update(gear_synced_at=timezone.now())
        self.assertEqual(self.dependencies()[0], [empty.pk, 'new'])
        self.assertEqual(self.dependency_ids(), {replacement.pk})
        self.assertEqual(self.display()['variant_id'], replacement.pk)
        SeasonMeta.objects.filter(pk=empty.pk).update(is_active=False)
        self.assertEqual(self.dependency_ids(), {ordinary.pk})
        SeasonMeta.objects.filter(pk=self.season.pk).update(gear_batch_key='empty-batch')
        self.assertEqual(self.dependencies(), ([self.season.pk, 'empty-batch'], []))
        self.assertIsNone(self.display()['variant_id'])
