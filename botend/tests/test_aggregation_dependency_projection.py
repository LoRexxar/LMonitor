"""Effective-reader dependencies, with real ORM/publication and CSV parsing."""
import copy
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from botend.models import WowSpellSnapshot, WowTalentNodeMetadata
from botend.services import dungeon_aggregation as aggregation
from botend.services.spec_stats_snapshot import content_digest
from botend.services.wow_spell_display import context_key
from botend.tests import test_dungeon_aggregation_reuse as fixtures
from botend.wow.spell_text import _file_cache_key
from botend.wow.talents.metadata import TalentMetadataProvider


class EffectiveDependencyTests(TestCase):
    setUp = fixtures.DungeonAggregationReuseTests.setUp
    row = fixtures.DungeonAggregationReuseTests.row
    fact = fixtures.DungeonAggregationReuseTests.fact
    talent_metadata = fixtures.DungeonAggregationReuseTests.talent_metadata
    talent_text = fixtures.DungeonAggregationReuseTests.talent_text
    spell = fixtures.DungeonAggregationReuseTests.spell
    aggregate = fixtures.DungeonAggregationReuseTests.aggregate
    detail = fixtures.DungeonAggregationReuseTests.detail
    def dump(self, filename, rows):
        # Filesystems may coalesce rapid same-size writes in one timestamp tick.
        # Model a distinct dump publication without sleeps or clock changes.
        import os
        path = Path(self.tmp.name) / filename
        previous = path.stat().st_mtime_ns if path.exists() else None
        fixtures.DungeonAggregationReuseTests.dump(self, filename, rows)
        if previous is not None:
            stamp = max(path.stat().st_mtime_ns, previous + 1_000_000)
            os.utime(path, ns=(stamp, stamp))

    def setup_display(self):
        self.node = self.talent_text('普通说明')
        self.display = {'default': {'name': '中央技能', 'name_zh': '中央技能',
                        'description': 'English description', 'description_zh': '中央说明',
                        'icon': 'spell_effective'},
                        'contexts': {context_key('Mage', 'Frost'): {'icon': 'other'}}}
        self.snapshot = self.spell(770, display_data=self.display)

    def dependencies(self):
        return aggregation._metadata_dependencies([], {}, 'Warrior', 'Arms')

    def test_irrelevant_storage_edits_reuse_identical_publication(self):
        self.setup_display()
        first = self.aggregate()
        before = self.detail()
        for branch, locale in [('wowt', 'zhCN'), ('wowxptr', 'enUS'), ('wow', 'frFR')]:
            WowSpellSnapshot.objects.create(spell_id=770, branch=branch, locale=locale,
                snapshot_build='test-build', name='Unrelated', description='Other branch')
        changed = copy.deepcopy(self.display)
        changed['contexts'][context_key('Mage', 'Frost')]['icon'] = 'another'
        WowSpellSnapshot.objects.filter(pk=self.snapshot.pk).update(
            name='Raw backfill', name_zh='原始回填', description='Raw description',
            aura_description='Raw aura', icon='raw_icon', snapshot_build='new-observation',
            display_data=changed)
        WowTalentNodeMetadata.objects.filter(pk=self.node.pk).update(
            reference_aliases=[{'source': 'audit', 'id': 123}], localization_evidence='核对说明')
        second = self.aggregate()
        self.assertTrue(second['reused'])
        self.assertEqual(first['generation'], second['generation'])
        self.assertEqual(first['generated_at'], second['generated_at'])
        self.assertEqual(before, self.detail())

    def test_effective_node_context_invalidates_and_is_published(self):
        self.setup_display()
        first = self.aggregate()
        changed = copy.deepcopy(self.display)
        changed['contexts'][context_key('Warrior', 'Arms', self.node.node_id)] = {'icon': 'effective_changed'}
        WowSpellSnapshot.objects.filter(pk=self.snapshot.pk).update(display_data=changed)
        second = self.aggregate()
        self.assertFalse(second['reused'])
        self.assertNotEqual(first['generation'], second['generation'])
        nodes = self.detail(1)['talent_popularity_tree']['render_model']['nodes']
        self.assertEqual(next(n for n in nodes if n['spell_id'] == 770)['icon'], 'effective_changed')

    def test_english_raw_fallback_remains_a_dependency(self):
        self.talent_text('普通说明')
        self.spell(770, name='中文', description='中文说明')
        english = WowSpellSnapshot.objects.create(spell_id=770, branch='wow', locale='enUS',
            snapshot_build='test-build', name='English', description='Original English',
            display_data={'default': {'name': 'Compiled but not the selected row'}})
        before = content_digest(self.dependencies())
        WowSpellSnapshot.objects.filter(pk=english.pk).update(description='Changed English')
        self.assertNotEqual(before, content_digest(self.dependencies()))

    def test_no_unused_text_resolver_prewarm(self):
        self.setup_display()
        from botend.wow.spell_text import SpellTextResolver
        expected = self.dependencies()
        with patch.object(SpellTextResolver, '_load_duration_table', side_effect=AssertionError('unused duration read')), \
             patch.object(SpellTextResolver, '_load_radius_table', side_effect=AssertionError('unused radius read')), \
             patch.object(SpellTextResolver, '_max_stacks_value', side_effect=AssertionError('unused stacks read')):
            # Bypass the prior LRU warmup so this also catches waste on cold start.
            if hasattr(aggregation, '_reference_table'):
                aggregation._reference_table.cache_clear()
            self.assertEqual(expected, self.dependencies())

    def test_unique_csv_reordering_does_not_change_structure(self):
        self.setup_display()
        rows = [{'ID': 1, 'TraitNodeEntryID': 77, '_Index': 0},
                {'ID': 2, 'TraitNodeEntryID': 78, '_Index': 1}]
        name = 'TraitNodeXTraitNodeEntry.csv'
        self.dump(name, rows)
        path = Path(self.tmp.name) / name
        first = aggregation._structural_dump_digest(_file_cache_key(path), (77, 78), ())
        before = TalentMetadataProvider(talent_version=self.node.talent_version).get_choice_entry_order()
        self.dump(name, list(reversed(rows)))
        after = TalentMetadataProvider(talent_version=self.node.talent_version).get_choice_entry_order()
        self.assertEqual(before, after)
        self.assertEqual(first, aggregation._structural_dump_digest(_file_cache_key(path), (77, 78), ()))

    def test_public_structure_token_keeps_legacy_csv_representation(self):
        from botend.services.stats_talent_dependencies import structure_token
        self.setup_display()
        self.node.db2_tree_id = 9
        self.node.save(update_fields=['db2_tree_id'])
        rows = [{'ID': '2', 'TraitTreeID': '9'}, {'ID': '1', 'TraitTreeID': '9'}]
        self.dump('TraitNode.csv', rows)
        legacy = {'TraitNode.csv': content_digest(rows), 'TraitNodeXTraitNodeEntry.csv': None}
        with patch.object(aggregation, '_structural_dump_digest',
                          side_effect=lambda key, *a, **kw: legacy[Path(key[0]).name]):
            expected = structure_token('Warrior', version=self.node.talent_version)
        # Public reading/fencing must not invalidate an unchanged published tree
        # merely because calculation-cache dependency hashing became canonical.
        self.assertEqual(structure_token('Warrior', version=self.node.talent_version), expected)

    def test_invalid_consumer_id_keeps_csv_order_without_crashing(self):
        rows = [{'ID': '²', 'TraitTreeID': 9}, {'ID': '1', 'TraitTreeID': 9}]
        name = 'TraitNode.csv'
        self.dump(name, rows)
        path = Path(self.tmp.name) / name
        first = aggregation._structural_dump_digest(_file_cache_key(path), (), (9,))
        self.assertEqual(first, content_digest([dict(row, TraitTreeID='9') for row in rows]))
        self.dump(name, list(reversed(rows)))
        self.assertNotEqual(first, aggregation._structural_dump_digest(_file_cache_key(path), (), (9,)))

    def test_duplicate_entry_order_remains_semantic(self):
        self.setup_display()
        rows = [{'ID': 1, 'TraitNodeEntryID': 77, '_Index': 0},
                {'ID': 2, 'TraitNodeEntryID': 77, '_Index': 1}]
        name = 'TraitNodeXTraitNodeEntry.csv'
        self.dump(name, rows)
        path = Path(self.tmp.name) / name
        first = aggregation._structural_dump_digest(_file_cache_key(path), (77,), ())
        before = TalentMetadataProvider(talent_version=self.node.talent_version).get_choice_entry_order()
        self.dump(name, list(reversed(rows)))
        after = TalentMetadataProvider(talent_version=self.node.talent_version).get_choice_entry_order()
        self.assertNotEqual(before, after)
        self.assertNotEqual(first, aggregation._structural_dump_digest(_file_cache_key(path), (77,), ()))
