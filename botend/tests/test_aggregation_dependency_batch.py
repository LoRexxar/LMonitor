"""Real ORM contracts for request-local aggregation dependency deduplication."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import (SeasonMeta, SpecRaidRanking, PlayerSpecTopPlayer,
                           WowItemSnapshot, WowItemVariantSnapshot,
                           WowTalentVersion, WowTalentNodeMetadata, WowSpellSnapshot)
from botend.services import dungeon_aggregation as dependencies
from botend.services import raid_aggregation as raid
from botend.services.spec_stats_snapshot import read_projection
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import (
    SpecDetailAggregationMonitor, publish_stats)
from botend.tests.aggregation_cache_helpers import isolate_work_cache


class AggregationDependencyBatchTests(TestCase):
    def setUp(self):
        isolate_work_cache(self)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.season = SeasonMeta.objects.create(season_key='dependency-batch', season_name='Test',
            is_active=True, mplus_zone_id=1, raid_zone_id=2, gear_batch_key='batch',
            raid_encounters=[{'id': 1, 'name': 'One'}, {'id': 2, 'name': 'Two'}])
        self.item = WowItemSnapshot.objects.create(item_id=123, name='Helmet', name_zh='头盔')
        for key, level in [('low', 100), ('a', 200), ('b', 200)]:
            WowItemVariantSnapshot.objects.create(item=self.item, season=self.season,
                batch_key='batch', variant_key=key, variant_type='raid', item_level=level,
                effects_json=[{'text': key}])
        self.rows = [SpecRaidRanking.objects.create(season_id=self.season.pk, boss_id=boss,
            boss_name=name, difficulty=5, class_name='Mage', spec_name='Fire',
            character_name='Player', realm='Realm', region='eu', dps=100 + boss,
            gear_json=[{'id': 123, 'slot': 'head', 'itemLevel': 200}])
            for boss, name in [(1, 'One'), (2, 'Two')]]
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk,
            class_name='Mage', spec_name='Fire', character_name='Player', realm='Realm',
            region='eu', race='Human', stats_json={'haste': 123})
        self.scopes = [(self.season.pk, boss, name, 'Mage', 'Fire', difficulty)
                       for difficulty in (5, 4) for boss, name in [(1, 'One'), (2, 'Two')]]

    def test_variant_dependencies_bind_loaded_items_without_join_or_rehydration(self):
        records = [{'gear_json': [{'id': 123, 'itemLevel': 200}]}]
        # The existing generic path remains the content oracle (including effects decoding).
        selected = WowItemVariantSnapshot.objects.filter(item=self.item).order_by('item_level', 'pk')
        expected = dependencies._content_rows(selected.filter(pk__in=[selected[0].pk, selected[1].pk]))
        with CaptureQueriesContext(connection) as queries:
            scope, actual = dependencies._variant_dependencies({123: self.item}, records)
        self.assertEqual(scope, [self.season.pk, 'batch'])
        self.assertEqual(actual, expected)
        sql_rows = [q['sql'].replace('`', '"') for q in queries]
        variant_sql = [sql for sql in sql_rows if 'FROM "wow_item_variant_snapshot"' in sql]
        self.assertTrue(variant_sql)
        self.assertFalse(any('JOIN "wow_item_snapshot"' in sql for sql in variant_sql))
        self.assertFalse(any('FROM "wow_item_snapshot"' in sql for sql in sql_rows))

    def test_batch_fingerprints_equal_independent_reads_and_reread_dependencies(self):
        with CaptureQueriesContext(connection) as independent_queries:
            expected = [raid.raid_fingerprint(*scope) for scope in self.scopes]
        with CaptureQueriesContext(connection) as batch_queries:
            actual = raid.raid_fingerprints(self.scopes)
        self.assertEqual(actual, expected)
        self.assertLess(len(batch_queries), len(independent_queries))
        print('AGGREGATION_QUERY_COUNTS', json.dumps({
            'independent': len(independent_queries), 'batch': len(batch_queries)}))
        for change in (
            lambda: SpecRaidRanking.objects.filter(pk=self.rows[0].pk).update(dps=333),
            lambda: PlayerSpecTopPlayer.objects.filter(pk=self.player.pk).update(race='Orc'),
            lambda: WowItemSnapshot.objects.filter(pk=self.item.pk).update(name_zh='新名称'),
            lambda: WowItemVariantSnapshot.objects.filter(item=self.item, variant_key='a').update(effects_json=[{'text': 'changed'}]),
        ):
            previous = actual
            change()
            actual = raid.raid_fingerprints(self.scopes)
            self.assertNotEqual(actual, previous)
            self.assertEqual(actual, [raid.raid_fingerprint(*scope) for scope in self.scopes])

    def test_real_cold_hot_publication_preserves_identity_and_invalidates_changes(self):
        path = Path(self.tmp.name) / str(self.season.pk) / 'Mage/Fire'
        run = lambda: SpecDetailAggregationMonitor._aggregate_raid(
            self.season, 'Mage', 'Fire', str(path), publish=publish_stats)
        with patch.object(raid, 'raid_fingerprint', wraps=raid.raid_fingerprint) as fingerprint:
            run()
        self.assertEqual(fingerprint.call_count, len(self.scopes),
                         'No publication exists: do not fingerprint every scope twice')
        index = path / 'raid.json'
        first = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.rglob('*.json')}
        cold = read_projection(index, 'raid-5-1')
        with patch.object(raid, 'load_work', side_effect=AssertionError('hot publication decoded work')):
            run()
        self.assertEqual(first, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.rglob('*.json')})
        self.assertEqual(cold, read_projection(index, 'raid-5-1'))
        SpecRaidRanking.objects.filter(pk=self.rows[0].pk).update(dps=333)
        run()
        changed = read_projection(index, 'raid-5-1')
        self.assertEqual(changed['zone_groups'][0]['bosses'][0]['dps']['avg'], 333)
        self.assertNotEqual(cold, changed)

    def test_talent_batch_reuses_resolver_but_next_call_rechecks_central_publication(self):
        from botend.services.wow_spell_display import sync_spell_display
        from botend.wow.branches import spell_branch
        version = WowTalentVersion.objects.create(key='retail', branch='retail',
            current_build='test-build', source_dir=self.tmp.name,
            is_active=True, status='active', is_default_stats=True)
        node = WowTalentNodeMetadata.objects.create(talent_version=version,
            class_name='Mage', spec_name='Fire', tree_type='spec', node_id=77,
            talent_id=77, spell_id=770, name='Talent', name_zh='天赋',
            description_zh='触发$@spellname99999。', source='db2_backfill')
        leaf = WowSpellSnapshot.objects.create(branch=spell_branch('retail'), locale='zhCN',
            snapshot_build='test-build', spell_id=99999, name='Leaf', name_zh='旧名称')
        # Compile real central display facts, not mocked provider/render results.
        sync_spell_display(branch='retail', apply=True, spell_ids=[770])
        expected = [raid.raid_fingerprint(*scope) for scope in self.scopes]
        with patch.object(dependencies, '_resolved_talent_dependencies',
                          wraps=dependencies._resolved_talent_dependencies) as resolve, \
             CaptureQueriesContext(connection) as queries:
            actual = raid.raid_fingerprints(self.scopes)
        self.assertEqual(actual, expected)
        self.assertEqual(resolve.call_count, 1)
        WowSpellSnapshot.objects.filter(pk=leaf.pk).update(name_zh='新名称')
        sync_spell_display(branch='retail', apply=True, spell_ids=[770])
        changed = raid.raid_fingerprints(self.scopes)
        self.assertNotEqual(changed, actual)
        self.assertEqual(changed, [raid.raid_fingerprint(*scope) for scope in self.scopes])

        # A structure change during enrichment still fails the real final fence.
        path = Path(self.tmp.name) / str(self.season.pk) / 'Mage/Fire'
        run = lambda publish: SpecDetailAggregationMonitor._aggregate_raid(
            self.season, 'Mage', 'Fire', str(path), publish=publish)
        run(publish_stats)
        before = (path / 'raid.json').read_bytes()
        SpecRaidRanking.objects.filter(pk=self.rows[0].pk).update(dps=444)
        def changed_structure(target, payload, class_name, spec_name):
            WowTalentNodeMetadata.objects.filter(pk=node.pk).update(spell_id=771)
            return publish_stats(target, payload, class_name, spec_name)
        with self.assertRaisesMessage(ValueError, '聚合期间天赋结构已更新'):
            run(changed_structure)
        self.assertEqual((path / 'raid.json').read_bytes(), before)
