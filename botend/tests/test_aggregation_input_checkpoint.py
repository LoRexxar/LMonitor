"""提前复用真实范围，覆盖补采、元数据变化、歧义、旧表和损坏恢复。"""
from collections import defaultdict
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import (SeasonMeta, SpecRaidRanking, SpecDungeonRanking, WowItemSnapshot,
                           WowItemVariantSnapshot, WclCombatantSnapshot)
from botend.services import raid_aggregation as raid, dungeon_aggregation as dungeon
from botend.services import wcl_combatant_snapshot as combatants, wcl_combatant_store as store
from botend.services.aggregation_input_checkpoint import dependency_references, reference_records
from botend.services.spec_aggregation_cache import work_path
from botend.services.spec_stats_snapshot import content_digest, read_projection
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor, publish_stats
from botend.tests.aggregation_cache_helpers import isolate_work_cache


class EarlyInputReuseTests(TestCase):
    def setUp(self):
        self.root = isolate_work_cache(self)
        self.season = SeasonMeta.objects.create(season_key='early-input', season_name='测试',
            is_active=True, mplus_zone_id=1, raid_zone_id=2, gear_batch_key='batch',
            mplus_encounters=[{'id': 1, 'name': 'One'}, {'id': 2, 'name': 'Two'}],
            raid_encounters=[{'id': 1, 'name': 'One'}, {'id': 2, 'name': 'Two'}])
        self.rows = []
        for boss, name in ((1, 'One'), (2, 'Two')):
            item = WowItemSnapshot.objects.create(item_id=100 + boss, name=name, name_zh='装备' + name,
                                                   effects={'schema': 1, 'effects': []})
            WowItemVariantSnapshot.objects.create(item=item, season=self.season, batch_key='batch',
                variant_key=name, variant_type='raid', item_level=200, effects_json=[{'text': name}])
            self.rows.append(SpecRaidRanking.objects.create(season_id=self.season.pk, boss_id=boss,
                boss_name=name, difficulty=5, class_name='Mage', spec_name='Fire', character_name='Player',
                realm='Realm', region='eu', dps=100 + boss, report_code='Report', fight_id=boss))
        self.scopes = [(self.season.pk, boss, name, 'Mage', 'Fire', difficulty)
                       for difficulty in (5, 4) for boss, name in ((1, 'One'), (2, 'Two'))]
        self.path = self.root / 'media' / str(self.season.pk) / 'Mage' / 'Fire'

    def event(self, boss, *, actor=1, name='Player'):
        return {'sourceID': actor, 'specID': 63, 'source': {'id': actor, 'name': name, 'server': 'Realm'},
                'gear': [{'id': 100 + boss, 'itemLevel': 200, 'slot': 0}], 'talentTree': [], 'race': 'Human'}

    def persist(self, bosses=(1, 2), **kwargs):
        store.persist_events('Report', {boss: [self.event(boss)] for boss in bosses}, **kwargs)

    def run_raid(self):
        return SpecDetailAggregationMonitor._aggregate_raid(self.season, 'Mage', 'Fire', str(self.path))

    def assert_matches_full(self, fingerprints, **kwargs):
        with patch('botend.services.aggregation_input_checkpoint.ScopeInputCheckpoint.reuse', return_value=None):
            self.assertEqual(fingerprints, raid.raid_fingerprints(self.scopes, **kwargs))

    def test_unchanged_real_publication_skips_all_log_bodies_and_database_writes(self):
        self.persist()
        self.run_raid()
        before = (self.path / 'raid.json').read_bytes()
        with patch.object(combatants, 'load_matched_facts', side_effect=AssertionError('未变化范围不得读取正文')), \
             CaptureQueriesContext(connection) as queries:
            self.run_raid()
        self.assertEqual(before, (self.path / 'raid.json').read_bytes())
        self.assertFalse([q for q in queries if 'FROM "wow_wcl_combatant_document"' in q['sql']])
        self.assertFalse([q for q in queries if q['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE '))])

    def test_one_recovered_log_reloads_only_affected_boss_and_preserves_full_result(self):
        self.persist((2,))
        first = raid.raid_fingerprints(self.scopes)
        self.persist((1,))
        stages = defaultdict(float)
        with patch.object(raid, '_inputs', wraps=raid._inputs) as inputs:
            current = raid.raid_fingerprints(self.scopes, phase_timings=stages)
        self.assertEqual([call.args[1] for call in inputs.call_args_list], [1])
        self.assertNotEqual(current[0], first[0])
        self.assertEqual(current[1:], first[1:])
        self.assertEqual(stages['raid_input_reused'], 3)
        self.assert_matches_full(current)

    def test_item_and_variant_updates_without_timestamp_reload_only_dependent_scope(self):
        self.persist()
        previous = raid.raid_fingerprints(self.scopes)
        for change in (
            lambda: WowItemSnapshot.objects.filter(item_id=101).update(name_zh='修改装备'),
            lambda: WowItemVariantSnapshot.objects.filter(item__item_id=101).update(effects_json=[{'text': '修改效果'}]),
        ):
            change()
            with patch.object(raid, '_inputs', wraps=raid._inputs) as inputs:
                current = raid.raid_fingerprints(self.scopes)
            self.assertEqual([call.args[1] for call in inputs.call_args_list], [1])
            self.assertNotEqual(current[0], previous[0])
            self.assertEqual(current[1:], previous[1:])
            self.assert_matches_full(current)
            previous = current

    def test_new_same_name_actor_invalidates_unique_match_but_teammate_does_not(self):
        self.persist()
        previous = raid.raid_fingerprints(self.scopes)
        store.persist_events('Report', {1: [self.event(1, actor=2, name='Other')]})
        with patch.object(combatants, 'load_matched_facts', side_effect=AssertionError('无关队友')):
            self.assertEqual(previous, raid.raid_fingerprints(self.scopes))
        store.persist_events('Report', {1: [self.event(1, actor=3)]})
        current = raid.raid_fingerprints(self.scopes)
        self.assertNotEqual(current[0], previous[0])
        self.assert_matches_full(current)

    def test_cutoff_ignores_future_fact_then_detects_it_when_visible(self):
        cutoff = timezone.now()
        previous = raid.raid_fingerprints(self.scopes, source_cutoff=cutoff)
        self.persist(fetched_at=cutoff + timedelta(seconds=1))
        with patch.object(combatants, 'load_matched_facts', side_effect=AssertionError('截止以后事实')):
            self.assertEqual(previous, raid.raid_fingerprints(self.scopes, source_cutoff=cutoff))
        current = raid.raid_fingerprints(self.scopes, source_cutoff=cutoff + timedelta(seconds=2))
        self.assertNotEqual(current[:2], previous[:2])
        self.assert_matches_full(current, source_cutoff=cutoff + timedelta(seconds=2))

    def test_legacy_mutable_payload_always_uses_full_validation(self):
        legacy = WclCombatantSnapshot.objects.create(report_code='Report', fight_id=1, actor_id=1,
                                                      payload_json=self.event(1), fetched_at=timezone.now())
        previous = raid.raid_fingerprints(self.scopes)
        changed = self.event(1)
        changed['race'] = 'Orc'
        WclCombatantSnapshot.objects.filter(pk=legacy.pk).update(payload_json=changed)
        with patch.object(raid, '_inputs', wraps=raid._inputs) as inputs:
            current = raid.raid_fingerprints(self.scopes)
        self.assertEqual([call.args[1] for call in inputs.call_args_list], [1])
        self.assertNotEqual(current[0], previous[0])

    def test_corrupt_checkpoint_falls_back_and_algorithm_change_invalidates(self):
        self.persist()
        previous = raid.raid_fingerprints(self.scopes)
        check = raid._input_checkpoint(*self.scopes[0])
        work_path(self.season.pk, check.scope).write_bytes('损坏'.encode())
        with patch.object(raid, '_inputs', wraps=raid._inputs) as inputs:
            self.assertEqual(previous, raid.raid_fingerprints(self.scopes))
        self.assertEqual(inputs.call_count, 1)
        with patch.object(raid, 'ALGORITHM_VERSION', '测试新算法'):
            self.assertNotEqual(previous, raid.raid_fingerprints(self.scopes))

    def test_reference_recipe_is_equivalent_without_copying_descriptions(self):
        rows = [{'gear_json': [{'itemID': 101, 'itemLevel': 200,
                  'gems': [{'id': 102}], 'gems_detail': [{'id': 103}],
                  'enchants_detail': [{'id': 104}], 'permanentEnchant': 777,
                  'description': '不保存说明'}], 'talents_json': [{'node_id': 12, 'child': {'spellID': 99}}]}]
        facts = {('Report', 1): [{'gear': [{'id': 102, 'itemLevel': 150}], 'talentTree': [{'id': 123}]}]}
        refs = dependency_references(rows, facts)
        self.assertNotIn('不保存说明', str(refs))
        self.assertEqual(content_digest(dungeon._metadata_dependencies(rows, facts, 'Mage', 'Fire')),
                         content_digest(dungeon._metadata_dependencies(reference_records(refs), {}, 'Mage', 'Fire')))

    def test_unchanged_dungeon_skips_bodies_but_recovery_updates_summary(self):
        for boss in (1, 2):
            SpecDungeonRanking.objects.create(season_id=self.season.pk, dungeon_id=boss, dungeon_name=str(boss),
                class_name='Mage', spec_name='Fire', character_name='Player', realm='Realm', region='eu',
                report_code='Report', fight_id=boss, dps=100, keystone_level=10)
        path = self.path / 'dungeon.json'
        run = lambda: dungeon.aggregate_dungeon(self.season, 'Mage', 'Fire', path, publish_stats)
        self.persist((2,))
        first = run()
        with patch.object(combatants, 'load_matched_facts', side_effect=AssertionError('未变化大秘境')):
            second = run()
        self.assertTrue(second['reused'])
        self.assertEqual(first['fingerprint'], second['fingerprint'])
        self.persist((1,))
        with patch.object(combatants, 'load_matched_facts', wraps=combatants.load_matched_facts) as load:
            current = run()
        self.assertEqual([len(call.args[0]) for call in load.call_args_list], [1, 1])
        self.assertFalse(current['reused'])
        actual = read_projection(path, 'dungeon-all')
        with patch('botend.services.aggregation_input_checkpoint.ScopeInputCheckpoint.reuse', return_value=None):
            self.assertEqual(run()['fingerprint'], current['fingerprint'])
        self.assertEqual(actual, read_projection(path, 'dungeon-all'))
