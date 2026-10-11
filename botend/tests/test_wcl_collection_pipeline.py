"""Contracts for unchanged samples and one cross-dungeon collection phase."""
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
from botend.models import SeasonMeta, SpecDungeonRanking, WclCombatantSnapshot
from botend.services.spec_stats_service import _select_dungeon_sample_records
from botend.tests.test_wcl_combatant_snapshot import event


class DungeonCollectionPipelineTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(
            season_key='pipeline', season_name='Pipeline', is_active=True,
            mplus_encounters=[{'id': 1, 'name': 'One'}, {'id': 2, 'name': 'Two'}],
            mplus_zone_id=1, raid_zone_id=2,
        )
        self.monitor = SpecDetailRankingMonitor(None, None)

    @staticmethod
    def ranking(fight, actor=232):
        return {
            'name': f'Player{actor}', 'server': {'name': 'BurningLegion', 'region': 'eu'},
            'report': {'code': 'SharedReport', 'fightID': fight},
            'amount': 100, 'hardModeLevel': 20,
        }

    def run_monitor(self, fetch_rankings, fetch_combatants):
        path = 'botend.controller.plugins.portal.SpecDetailRankingMonitor'
        with patch(path + '.CLASS_SPEC_MAP', {'DeathKnight': ['Frost']}), \
             patch(path + '.time.sleep'), \
             patch.object(self.monitor, '_fetch_rankings_with_retry', side_effect=fetch_rankings), \
             patch.object(self.monitor, 'fetch_wcl_combatant_info', side_effect=fetch_combatants):
            return self.monitor._collect_dungeon_rankings(self.season)

    def test_all_rankings_precede_one_shared_report_fetch_and_resume_uses_cache(self):
        trace = []
        def ranking(enc, *args, **kwargs):
            trace.append(('ranking', enc))
            return {'rankings': [self.ranking(enc)], 'hasMorePages': False}
        def combatants(code, fights):
            trace.append(('combatants', code, fights))
            self.assertEqual(SpecDungeonRanking.objects.count(), 2)
            return [event(fight) for fight in fights]
        self.assertTrue(self.run_monitor(ranking, combatants))
        self.assertEqual(trace, [('ranking', 1), ('ranking', 2),
                                 ('combatants', 'SharedReport', [1, 2])])
        self.assertEqual(WclCombatantSnapshot.objects.count(), 2)
        trace.clear()
        self.assertTrue(self.run_monitor(ranking, combatants))
        self.assertEqual(trace, [('ranking', 1), ('ranking', 2)])

    def test_bad_rankings_preserve_old_scope_and_never_enter_collection_union(self):
        invalid_pages = [
            {}, {'rankings': [], 'hasMorePages': True},
            {'rankings': [self.ranking(1), 'invalid-row'], 'hasMorePages': False},
            {'rankings': [self.ranking(1), {**self.ranking(1, 233), 'gear': 'invalid'}],
             'hasMorePages': False},
        ]
        for payload in invalid_pages:
            with self.subTest(payload=payload):
                SpecDungeonRanking.objects.all().delete()
                WclCombatantSnapshot.objects.all().delete()
                old = SpecDungeonRanking.objects.create(
                    season_id=self.season.id, dungeon_id=1, dungeon_name='One',
                    class_name='DeathKnight', spec_name='Frost', character_name='Previous',
                    realm='BurningLegion', region='eu', report_code='OldReport',
                    fight_id=10, dps=55, keystone_level=20,
                )
                calls = []
                def ranking(enc, *args, **kwargs):
                    return payload if enc == 1 else {
                        'rankings': [self.ranking(2)], 'hasMorePages': False}
                def combatants(code, fights):
                    calls.append((code, fights))
                    return [event(fight) for fight in fights]
                self.assertFalse(self.run_monitor(ranking, combatants))
                old.refresh_from_db()
                self.assertEqual(old.character_name, 'Previous')
                self.assertEqual(list(SpecDungeonRanking.objects.filter(dungeon_id=1)
                                      .values_list('pk', flat=True)), [old.pk])
                self.assertEqual(calls, [('SharedReport', [2])])

    def test_identity_projection_retains_exact_selection_order_without_large_json(self):
        SpecDungeonRanking.objects.bulk_create([
            SpecDungeonRanking(
                season_id=self.season.id, dungeon_id=1, dungeon_name='One',
                class_name='DeathKnight', spec_name='Frost', character_name=f'Player{i}',
                realm='BurningLegion', region='eu', report_code='SharedReport', fight_id=i,
                dps=i, keystone_level=20, gear_json=[{'id': 123, 'name': 'gear'}],
                talents_json=[{'talentID': 456}],
            ) for i in range(1, 301)
        ])
        qs = SpecDungeonRanking.objects.filter(season_id=self.season.id, dungeon_id=1)
        expected = _select_dungeon_sample_records(qs)
        fields = ('id', 'report_code', 'fight_id', 'character_name', 'realm', 'region')
        with CaptureQueriesContext(connection) as queries:
            got = _select_dungeon_sample_records(qs, hydrate_fields=fields)
        self.assertEqual([r['id'] for r in got], [r['id'] for r in expected])
        self.assertEqual(len(got), 100)
        self.assertTrue(all(set(r) == set(fields) for r in got))
        self.assertEqual(len(queries), 2)
        for query in queries:
            for field in ('gear_json', 'talents_json', 'talent_build_code'):
                self.assertNotIn(field, query['sql'])
