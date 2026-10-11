"""Real ORM + immutable-file contracts for one-cohort dungeon publication."""
import csv
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import (SeasonMeta, SpecDungeonRanking, WclCombatantSnapshot,
                           WclCombatantFetchState, WowItemSnapshot, WowTalentNodeMetadata,
                           WowTalentVersion, WowItemVariantSnapshot, WowSpellSnapshot,
                           WowSpellEffectSnapshot)
from botend.services import spec_stats_service as stats
from botend.services import wcl_combatant_snapshot as facts
from botend.services.spec_stats_snapshot import read_projection
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import publish_stats


class DungeonAggregationReuseTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / '77/Warrior/Arms/dungeon.json'
        self.season = SeasonMeta.objects.create(season_key='reuse-test', season_name='Test',
            mplus_zone_id=1, raid_zone_id=2, mplus_encounters=[
                {'id': 1, 'name': 'One'}, {'id': 2, 'name': 'Two'}])

    def row(self, dungeon=1, name='A', **extra):
        data = dict(season_id=self.season.id, dungeon_id=dungeon, dungeon_name=str(dungeon),
                    class_name='Warrior', spec_name='Arms', character_name=name,
                    realm='Realm', region='eu', report_code=f'R{dungeon}', fight_id=1,
                    dps=100, keystone_level=10, gear_json=[{'id': 123, 'slot': 'head'}])
        data.update(extra)
        return SpecDungeonRanking.objects.create(**data)

    def fact(self, row, rating=100, when=None):
        return WclCombatantSnapshot.objects.create(report_code=row.report_code,
            fight_id=row.fight_id, actor_id=1, fetched_at=when or timezone.now(),
            payload_json={'sourceID': 1, 'source': {'id': 1, 'name': row.character_name,
            'server': row.realm}, 'specID': 71, 'critMelee': rating})

    def aggregate(self, **kwargs):
        from botend.services.dungeon_aggregation import aggregate_dungeon
        return aggregate_dungeon(self.season, 'Warrior', 'Arms', self.path, publish_stats, **kwargs)

    def detail(self, key='all'):
        payload = read_projection(self.path, f'dungeon-{key}')
        return payload['summary'] if key == 'all' else payload['dungeons'][0]

    def test_shared_selection_hydration_facts_and_real_union(self):
        self.row()
        self.row(dungeon=2, dps=300)
        # Same log across dungeons: deduplicate logs, not cross-dungeon characters.
        self.row(dungeon=2, name='B', dps=300)
        self.row(dungeon=1, name='B', report_code='R2')
        expected = stats.SpecStatsService.get_dungeon_summary('Warrior', 'Arms', self.season.id)
        with patch.object(stats, '_select_dungeon_sample_records', wraps=stats._select_dungeon_sample_records) as select, \
             patch.object(facts, '_load_facts', wraps=facts._load_facts) as load, \
             patch.object(facts, 'enrich_dungeon_records', wraps=facts.enrich_dungeon_records) as enrich:
            result = self.aggregate()
        self.assertFalse(result['reused'])
        self.assertEqual(select.call_count, 2)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(enrich.call_count, 1)
        self.assertEqual(self.detail(), expected)
        self.assertEqual(self.detail()['sample_size'], sum(self.detail(i)['sample_size'] for i in (1, 2)) - 1)

    def test_unchanged_skips_compute_enrichment_publish_and_preserves_bytes(self):
        row = self.row()
        self.fact(row)
        first = self.aggregate()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.path.parent.rglob('*.json')}
        with patch.object(stats.SpecStatsService, '_compute_dungeon_records', side_effect=AssertionError('recompute')), \
             patch.object(facts, 'enrich_dungeon_records', side_effect=AssertionError('reenrich')):
            second = self.aggregate(source_cutoff=timezone.now() + timedelta(days=1))
        self.assertTrue(second['reused'])
        self.assertEqual(first['generation'], second['generation'])
        self.assertEqual(first['generated_at'], second['generated_at'])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.path.parent.rglob('*.json')})

    def test_content_edits_late_facts_and_missing_or_corrupt_detail_invalidate(self):
        version, _ = WowTalentVersion.objects.update_or_create(key='retail', defaults={
            'is_active': True, 'status': 'active', 'is_default_stats': True})
        row = self.row(talents_json=[{'tree_type': 'spec', 'node_id': 77, 'talent_id': 77,
                                     'spell_id': 770, 'points': 1, 'name': 'Test'}])
        item = WowItemSnapshot.objects.create(item_id=123, name='Head', name_zh='头盔')
        node = WowTalentNodeMetadata.objects.create(class_name='Warrior', spec_name='Arms',
            talent_version=version, node_id=77, talent_id=77, spell_id=770,
            tree_type='spec', name='Test', name_zh='旧天赋', row=1, column=1)
        previous = self.aggregate()['generation']
        for change in (
            lambda: self.fact(row, 321),
            lambda: WowItemSnapshot.objects.filter(pk=item.pk).update(name_zh='新头盔'),
            lambda: WowItemSnapshot.objects.filter(pk=item.pk).update(metadata={'tooltip': 'changed'}),
            lambda: WowTalentNodeMetadata.objects.filter(pk=node.pk).update(description_zh='新说明', name_zh='新天赋'),
            lambda: SpecDungeonRanking.objects.filter(pk=row.pk).update(dps=222),
        ):
            change()
            result = self.aggregate()
            self.assertFalse(result['reused'])
            self.assertNotEqual(previous, result['generation'])
            previous = result['generation']
        self.assertEqual(next(iter(self.detail(1)['gear_popularity'].values()))[0]['name_zh'], '新头盔')
        self.assertEqual(self.detail(1)['talent_usage'][0]['name_zh'], '新天赋')

        for invalid in (None, '{', '{}'):
            shard = self.path.parent / 'stats-details' / previous / 'dungeon-all.json'
            if invalid is None:
                shard.unlink()
            else:
                shard.write_text(invalid)
            result = self.aggregate()
            self.assertFalse(result['reused'])
            self.assertNotEqual(previous, result['generation'])
            previous = result['generation']
            self.assertEqual(self.detail()['dps']['avg'], 222)
        # Valid JSON with a modified body is also corrupt, not reusable.
        shard = self.path.parent / 'stats-details' / previous / 'dungeon-all.json'
        envelope = json.loads(shard.read_text())
        envelope['data']['sample_size'] = 999
        shard.write_text(json.dumps(envelope))
        self.assertFalse(self.aggregate()['reused'])
        self.assertEqual(self.detail()['sample_size'], 1)

    def talent_text(self, text, field='description_zh'):
        WowTalentVersion.objects.filter(is_default_stats=True).update(is_default_stats=False)
        version, _ = WowTalentVersion.objects.update_or_create(key='ptr', defaults={
            'branch': 'ptr', 'current_build': 'test-build', 'source_dir': self.tmp.name,
            'is_active': True, 'status': 'active', 'is_default_stats': True})
        self.row(talents_json=[{'tree_type': 'spec', 'node_id': 77, 'talent_id': 77,
                               'spell_id': 770, 'points': 1, 'name': 'Test'}])
        return WowTalentNodeMetadata.objects.create(class_name='Warrior', spec_name='Arms',
            talent_version=version, node_id=77, talent_id=77, spell_id=770,
            tree_type='spec', source='db2_backfill', name='Test', name_zh='测试天赋', row=1, column=1,
            **{field: text})

    def spell(self, spell_id, **extra):
        values = dict(branch='ptr', locale='zhCN', snapshot_build='test-build',
                      name='Old', name_zh='旧名称')
        values.update(extra)
        return WowSpellSnapshot.objects.create(spell_id=spell_id, **values)

    def assert_talent_text(self, text, field='description_zh'):
        for scope in (1, 'all'):
            trees = self.detail(scope)['talent_popularity_tree']['render_model']['trees']
            node = next(node for tree in trees for node in tree['nodes'] if node['spell_id'] == 770)
            self.assertEqual(node[field], text)

    def dump(self, filename, rows):
        with (Path(self.tmp.name) / filename).open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_exact_build_dump_name_edit_invalidates_publication(self):
        self.talent_text('触发$@spellname5394。')
        self.dump('SpellName_zhCN.csv', [{'ID': 5394, 'Name_lang': '旧名称'}])
        first = self.aggregate()
        self.assert_talent_text('触发旧名称。')
        self.assertTrue(self.aggregate()['reused'])
        self.dump('SpellName_zhCN.csv', [{'ID': 5394, 'Name_lang': '新名称'}])
        second = self.aggregate()
        self.assertFalse(second['reused'])
        self.assertNotEqual(first['generation'], second['generation'])
        self.assert_talent_text('触发新名称。')
        self.assertTrue(self.aggregate()['reused'])

    def test_unchanged_dump_description_tracks_nested_database_name_and_effect(self):
        self.talent_text('触发$@spelldesc90001。')
        self.dump('Spell_zhCN.csv', [{'ID': 90001,
            'Description_lang': '$@spellname99999：${$99998s1/2}'}])
        leaf = self.spell(99999)
        effect = WowSpellEffectSnapshot.objects.create(branch='ptr', locale='zhCN',
            snapshot_build='test-build', spell_id=99998, effect_index=0, base_points='12')
        self.aggregate()
        self.assert_talent_text('触发旧名称：6。')
        self.assertTrue(self.aggregate()['reused'])
        WowSpellSnapshot.objects.filter(pk=leaf.pk).update(name_zh='新名称')
        self.assertFalse(self.aggregate()['reused'])
        self.assert_talent_text('触发新名称：6。')
        WowSpellEffectSnapshot.objects.filter(pk=effect.pk).update(base_points='24')
        self.assertFalse(self.aggregate()['reused'])
        self.assert_talent_text('触发新名称：12。')
        self.assertTrue(self.aggregate()['reused'])

    def test_dump_numeric_resources_invalidate_only_when_resolved_text_changes(self):
        node = self.talent_text('')
        cases = [
            ('$s1', 'spell_effect_index.csv',
             [{'SpellID': 770, 'EffectIndex': 0, 'EffectBasePointsF': '12'}],
             'EffectBasePointsF', '24', '12', '24'),
            ('$b1', 'spell_effect_index.csv',
             [{'SpellID': 770, 'EffectIndex': 0, 'EffectPointsPerResource': '3'}],
             'EffectPointsPerResource', '6', '3', '6'),
            ('$d', 'SpellDuration.csv', [{'ID': 1, 'Duration': 12000}],
             'Duration', 24000, '12', '24'),
            ('$r', 'SpellRadius.csv', [{'ID': 1, 'Radius': 12}],
             'Radius', 24, '12', '24'),
            ('$u', 'SpellAuraOptions.csv', [{'SpellID': 770, 'CumulativeAura': 3}],
             'CumulativeAura', 6, '3', '6'),
            ('$d', 'SpellMisc.csv', [{'SpellID': 770, 'DurationIndex': 1, 'RangeIndex': 1}],
             'DurationIndex', 2, '12', '36'),
            ('$r', 'spell_misc_index.csv', [{'SpellID': 770, 'DurationIndex': 1, 'RangeIndex': 1}],
             'RangeIndex', 2, '12', '36'),
        ]
        for token, filename, rows, column, new, before, after in cases:
            with self.subTest(resource=filename, token=token):
                # Independent publication baseline for each resource.
                self.path.unlink(missing_ok=True)
                (Path(self.tmp.name) / 'SpellMisc.csv').unlink(missing_ok=True)
                self.dump('spell_misc_index.csv', [
                    {'SpellID': 770, 'DurationIndex': 1, 'RangeIndex': 1}])
                self.dump('SpellDuration.csv', [{'ID': 1, 'Duration': 12000},
                                               {'ID': 2, 'Duration': 36000}])
                self.dump('SpellRadius.csv', [{'ID': 1, 'Radius': 12}, {'ID': 2, 'Radius': 36}])
                self.dump(filename, rows)
                # Route through the dump description, not just a DB token.
                self.dump('Spell_zhCN.csv', [{'ID': 770, 'Description_lang': token}])
                WowTalentNodeMetadata.objects.filter(pk=node.pk).update(description_zh='$@spelldesc770')
                self.aggregate()
                self.assert_talent_text(before)
                self.assertTrue(self.aggregate()['reused'])
                rows[0][column] = new
                self.dump(filename, rows)
                self.assertFalse(self.aggregate()['reused'])
                self.assert_talent_text(after)
                self.assertTrue(self.aggregate()['reused'])

    def test_dump_cache_cost_is_shared_but_database_fallbacks_are_fresh(self):
        import io
        from botend.services import dungeon_aggregation as aggregation
        from botend.wow import spell_text

        node = self.talent_text('$@spelldesc770')
        self.dump('Spell_zhCN.csv', [{'ID': 770,
            'Description_lang': '$@spellname99999 $s1 $d $r $u'}])
        self.dump('spell_effect_index.csv', [
            {'SpellID': 770, 'EffectIndex': 0, 'EffectBasePointsF': 12}])
        self.dump('SpellMisc.csv', [{'SpellID': 770, 'DurationIndex': 1, 'RangeIndex': 1}])
        self.dump('SpellDuration.csv', [{'ID': 1, 'Duration': 12000}])
        self.dump('SpellRadius.csv', [{'ID': 1, 'Radius': 12}])
        self.dump('SpellAuraOptions.csv', [{'SpellID': 770, 'CumulativeAura': 3}])
        self.dump('TraitNode.csv', [{'ID': 77, 'TraitTreeID': 1}])
        self.dump('TraitNodeXTraitNodeEntry.csv', [{'TraitNodeEntryID': 77, '_Index': 0}])
        leaf = self.spell(99999)
        other_version = WowTalentVersion.objects.create(key='historical', branch='ptr', is_active=False)
        other = WowTalentNodeMetadata.objects.create(talent_version=other_version,
            class_name='Warrior', spec_name='Arms', node_id=88, spell_id=88888,
            description_zh='$@spellname88888', source='db2_backfill')
        mage = WowTalentNodeMetadata.objects.create(talent_version=node.talent_version,
            class_name='Mage', spec_name='Fire', node_id=89, spell_id=88889,
            description_zh='$@spellname88889', source='db2_backfill')
        self.aggregate()
        self.assert_talent_text('旧名称 12 12 12 3')
        WowTalentNodeMetadata.objects.filter(pk__in=[other.pk, mage.pk]).update(description_zh='无关')
        # Observe real I/O and SQL; do not replace the resolver or publication.
        with patch('io.open', wraps=io.open) as opened, CaptureQueriesContext(connection) as queries:
            self.assertTrue(self.aggregate()['reused'])
        csv_reads = [call.args[0] for call in opened.call_args_list
                     if str(call.args[0]).endswith('.csv')]
        self.assertEqual(csv_reads, [])
        spell_queries = [q['sql'] for q in queries if 'FROM "wow_spell_snapshot"' in q['sql']]
        self.assertLessEqual(len(spell_queries), 2)
        for query in spell_queries:
            self.assertNotIn('88888', query)
            self.assertNotIn('88889', query)
        self.assertEqual(aggregation._reference_table.cache_info().maxsize, 24)
        self.assertEqual(aggregation._structural_dump_digest.cache_info().maxsize, 16)
        for cache in (spell_text._cached_dump_spell_text, spell_text._cached_misc_index,
                      spell_text._cached_dump_effect_index):
            self.assertIsNotNone(cache.cache_info().maxsize)
        # A second spec shares dump indexes, not stale DB-resolved metadata.
        with patch('io.open', wraps=io.open) as opened:
            aggregation._metadata_dependencies([], {}, 'Warrior', 'Fury')
        self.assertEqual([call.args[0] for call in opened.call_args_list
                          if str(call.args[0]).endswith('.csv')], [])
        WowSpellSnapshot.objects.filter(pk=leaf.pk).update(name_zh='新名称')
        self.assertFalse(self.aggregate()['reused'])
        self.assert_talent_text('新名称 12 12 12 3')

    def test_dump_missing_reference_insertion_and_unrelated_edit(self):
        self.talent_text('$@spelldesc90001', field='description')
        self.dump('Spell_enUS.csv', [{'ID': 90001, 'Description_lang': '$@spellname99999'}])
        self.aggregate()
        self.assert_talent_text('#99999', field='description')
        self.dump('SpellName_enUS.csv', [{'ID': 88888, 'Name_lang': 'Unrelated'}])
        self.assertTrue(self.aggregate()['reused'])
        self.dump('SpellName_enUS.csv', [{'ID': 99999, 'Name_lang': 'New'},
                                       {'ID': 88888, 'Name_lang': 'Unrelated'}])
        self.assertFalse(self.aggregate()['reused'])
        self.assert_talent_text('New', field='description')
        self.dump('SpellName_enUS.csv', [{'ID': 99999, 'Name_lang': 'New'},
                                       {'ID': 88888, 'Name_lang': 'Changed'}])
        self.assertTrue(self.aggregate()['reused'])

    def test_indirect_spell_name_edit_invalidates_published_talent_text(self):
        self.talent_text('触发$@spellname99999。')
        spell = self.spell(99999)
        first = self.aggregate()
        self.assert_talent_text('触发旧名称。')
        self.assertTrue(self.aggregate()['reused'])
        WowSpellSnapshot.objects.filter(pk=spell.pk).update(name_zh='新名称')
        second = self.aggregate()
        self.assertFalse(second['reused'])
        self.assertNotEqual(first['generation'], second['generation'])
        self.assert_talent_text('触发新名称。')
        self.assertTrue(self.aggregate()['reused'])

    def test_nested_description_tooltip_aura_and_name_dependencies(self):
        self.talent_text('$@spelldesc90001')
        self.spell(90001, description='外层$@spelltooltip90002')
        self.spell(90002, description='中层$@spellaura90003')
        aura = self.spell(90003, aura_description='触发$@spellname99999。')
        leaf = self.spell(99999)
        self.aggregate()
        self.assert_talent_text('外层中层触发旧名称。')
        WowSpellSnapshot.objects.filter(pk=leaf.pk).update(name_zh='新名称')
        self.assertFalse(self.aggregate()['reused'])
        self.assert_talent_text('外层中层触发新名称。')
        WowSpellSnapshot.objects.filter(pk=aura.pk).update(aura_description='获得$@spellname99999。')
        self.assertFalse(self.aggregate()['reused'])
        self.assert_talent_text('外层中层获得新名称。')
        self.assertTrue(self.aggregate()['reused'])

    def test_missing_nested_reference_is_rechecked_and_expanded_when_added(self):
        self.talent_text('触发$@spelldesc90001。')
        self.spell(90001, description='$@spelldesc90002')
        self.aggregate()
        self.assert_talent_text('触发。')
        self.assertTrue(self.aggregate()['reused'])
        self.spell(90002, description='$@spellname99999')
        self.assertFalse(self.aggregate()['reused'])
        self.assert_talent_text('触发#99999。')
        self.assertTrue(self.aggregate()['reused'])
        self.spell(99999)
        self.assertFalse(self.aggregate()['reused'])
        self.assert_talent_text('触发旧名称。')
        self.assertTrue(self.aggregate()['reused'])

    def test_english_description_uses_indirect_snapshot(self):
        self.talent_text('Triggers $@spellname99999.', field='description')
        spell = self.spell(99999, locale='enUS')
        self.aggregate()
        self.assert_talent_text('Triggers Old.', field='description')
        WowSpellSnapshot.objects.filter(pk=spell.pk).update(name='New')
        self.assertFalse(self.aggregate()['reused'])
        self.assert_talent_text('Triggers New.', field='description')

    def test_numeric_spell_tokens_track_effect_contents_including_nested_and_local(self):
        node = self.talent_text('')
        self.spell(90001, description='数值$99999s1。')
        for token, spell_id, old, new in (
            ('$99999s1', 99999, '12', '24'),
            ('${$99999s1/2}', 99999, '6', '12'),
            ('$/2;99999s1', 99999, '6', '12'),
            ('$s1', 770, '12', '24'),
            ('$@spelldesc90001', 99999, '数值12。', '数值24。'),
        ):
            with self.subTest(token=token):
                WowTalentNodeMetadata.objects.filter(pk=node.pk).update(description_zh=token)
                effect, _ = WowSpellEffectSnapshot.objects.update_or_create(
                    branch='ptr', locale='zhCN', snapshot_build='test-build',
                    spell_id=spell_id, effect_index=0, defaults={'base_points': '12'})
                self.aggregate()
                self.assert_talent_text(old)
                self.assertTrue(self.aggregate()['reused'])
                WowSpellEffectSnapshot.objects.filter(pk=effect.pk).update(base_points='24')
                self.assertFalse(self.aggregate()['reused'])
                self.assert_talent_text(new)

    def test_cyclic_spell_dependencies_are_bounded_and_unrelated_rows_do_not_invalidate(self):
        self.talent_text('$@spelldesc90001')
        self.spell(90001, description='$@spellname99999$@spelldesc90002')
        self.spell(90002, description='$@spelldesc90001')
        leaf = self.spell(99999)
        unrelated = self.spell(88888, description='$@spelldesc88888')
        self.aggregate()
        self.assert_talent_text('旧名称旧名称')
        WowSpellSnapshot.objects.filter(pk=unrelated.pk).update(name_zh='无关变化')
        with CaptureQueriesContext(connection) as queries:
            self.assertTrue(self.aggregate()['reused'])
        spell_queries = [q['sql'] for q in queries if 'FROM "wow_spell_snapshot"' in q['sql']]
        self.assertTrue(spell_queries)
        self.assertLessEqual(len(spell_queries), 5)
        for query in spell_queries:
            self.assertTrue(' IN ' in query or '"spell_id" = ' in query)
            self.assertNotIn('88888', query)
        WowSpellSnapshot.objects.filter(pk=leaf.pk).update(name_zh='新名称')
        self.assertFalse(self.aggregate()['reused'])
        self.assert_talent_text('新名称新名称')

    def test_frozen_order_and_late_fact_cutoff(self):
        early = self.row(name='Early', dps=100)
        late = self.row(dungeon=2, name='Late', dps=900)
        cutoff = timezone.now()
        self.fact(early, 100, cutoff - timedelta(seconds=1))
        self.fact(late, 900, cutoff + timedelta(seconds=1))
        selected = {'1': [early.pk], '2': [late.pk]}
        with patch.object(stats, '_select_dungeon_sample_records', side_effect=AssertionError('reselect')):
            self.aggregate(selected_ids=selected, source_cutoff=cutoff)
        crit = next(x for x in self.detail()['secondary_stats'] if x['key'] == 'crit')
        self.assertEqual(crit['sample_size'], 1)
        self.assertEqual(crit['rating']['avg'], 100)
        self.assertFalse(self.aggregate(selected_ids=selected)['reused'])
        crit = next(x for x in self.detail()['secondary_stats'] if x['key'] == 'crit')
        self.assertEqual(crit['sample_size'], 2)

    def test_preloaded_empty_facts_never_read_again(self):
        row = dict(character_name='A', realm='Realm', region='eu', report_code='R', fight_id=1)
        with patch.object(facts, '_load_facts', side_effect=AssertionError('second read')):
            self.assertEqual(facts.enrich_dungeon_records([row], 'Warrior', 'Arms', facts={})[0]['stats_json'], {})

    def test_original_300_candidate_selection_and_top5_order_are_preserved(self):
        SpecDungeonRanking.objects.bulk_create([
            SpecDungeonRanking(season_id=self.season.id, dungeon_id=1, dungeon_name='One',
                class_name='Warrior', spec_name='Arms', character_name=f'P{i % 200}',
                realm='Realm', region='eu', report_code='R', fight_id=i + 1,
                dps=i + 100, keystone_level=20 if i < 90 else 10)
            for i in range(300)])
        queryset = SpecDungeonRanking.objects.filter(season_id=self.season.id, dungeon_id=1)
        original = stats._select_dungeon_sample_records(queryset)
        with patch.object(stats, '_hydrate_dungeon_samples', wraps=stats._hydrate_dungeon_samples) as hydrate, \
             CaptureQueriesContext(connection) as queries:
            self.aggregate()
        self.assertEqual([row['id'] for row in hydrate.call_args_list[0].args[1]],
                         [row['id'] for row in original])
        ranking_queries = [q['sql'] for q in queries if 'FROM "wow_spec_dungeon_ranking"' in q['sql']]
        self.assertNotIn('gear_json', ranking_queries[0])
        self.assertIn(' IN ', ranking_queries[1])
        self.assertEqual(self.detail(1)['sample_size'], 100)
        self.assertEqual([row['id'] for row in self.detail(1)['top5']],
                         [row['id'] for row in sorted(original, key=lambda row: row['dps'], reverse=True)[:5]])
        self.assertNotEqual([row['id'] for row in self.detail(1)['top5']],
                            [row['id'] for row in original[:5]])

    def test_frozen_missing_reference_fails_without_publishing(self):
        row = self.row()
        self.aggregate()
        before = self.path.read_bytes()
        row.delete()
        with self.assertRaises(ValueError):
            self.aggregate(selected_ids={'1': [999999], '2': []})
        self.assertEqual(self.path.read_bytes(), before)

    def test_variant_contents_are_scoped_to_current_display_choice(self):
        self.row()
        self.season.gear_batch_key = 'current'
        self.season.is_active = True
        self.season.save()
        item = WowItemSnapshot.objects.create(item_id=123, name='Head', name_zh='头盔')
        def variant(key, **extra):
            values = dict(item=item, season=self.season, batch_key='current', variant_key=key,
                          variant_type='drop_equipment', item_level=300, data_branch='retail',
                          stats_json={'crit': 100})
            values.update(extra)
            return WowItemVariantSnapshot.objects.create(**values)
        chosen = variant('chosen')
        excluded = [variant('history', batch_key='old'), variant('ptr', data_branch='ptr'),
                    variant('unselected', item_level=350)]
        self.aggregate()
        for row in excluded:
            WowItemVariantSnapshot.objects.filter(pk=row.pk).update(metadata={'irrelevant': 'changed'})
        with CaptureQueriesContext(connection) as queries:
            self.assertTrue(self.aggregate()['reused'])
        hydration = [q['sql'] for q in queries if 'FROM "wow_item_variant_snapshot"' in q['sql']
                     and '"stats_json"' in q['sql']]
        self.assertEqual(len(hydration), 1)
        self.assertIn(' IN ', hydration[0])
        WowItemVariantSnapshot.objects.filter(pk=chosen.pk).update(stats_json={'crit': 321})
        self.assertFalse(self.aggregate()['reused'])
        self.assertIn('321', next(iter(self.detail(1)['gear_popularity'].values()))[0]['tooltip'])

    def test_budget_deferral_is_pending_not_negative_cached_or_failed(self):
        retry = timezone.now() + timedelta(minutes=20)
        rows = [dict(report_code=f'R{i}', fight_id=1, character_name='A', realm='Realm',
                     region='eu', class_name='Warrior', spec_name='Arms') for i in range(3)]
        fetcher = Mock(_wcl_retry_at=retry, _wcl_last_error='rate_limit')
        fetcher.fetch_wcl_combatant_info.return_value = None
        result = facts.collect_dungeon_combatants(rows, fetcher, apply=True)
        self.assertEqual(result['pending'], 3)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['missing'], 0)
        self.assertEqual(result['retry_at'], retry.isoformat())
        fetcher.fetch_wcl_combatant_info.assert_called_once()
        self.assertFalse(WclCombatantFetchState.objects.exists())
        self.assertFalse(WclCombatantSnapshot.objects.exists())
        self.assertTrue(all(isinstance(v, int) for k, v in result.items() if k != 'retry_at'))
