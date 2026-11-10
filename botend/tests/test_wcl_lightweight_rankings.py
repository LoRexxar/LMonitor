"""轻量榜单选样、按需翻页和详情复用的回归。"""
from copy import deepcopy
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
from botend.models import SeasonMeta, SpecDungeonRanking, SpecRaidRanking, WclCollectionCycle
from botend.services.wcl_collection_cycle import freeze_samples, frozen_combatant_records
from botend.tests.aggregation_cache_helpers import isolate_work_cache


class LightweightRankingTests(TestCase):
    def setUp(self):
        isolate_work_cache(self)
        self.monitor = SpecDetailRankingMonitor(None, None)
        self.season = SeasonMeta.objects.create(season_key='轻量榜单', season_name='轻量榜单',
            mplus_zone_id=1, raid_zone_id=2, mplus_encounters=[{'id': 1, 'name': '副本'}],
            raid_encounters=[{'id': 2, 'name': '首领'}])
        self.enterContext(patch('botend.controller.plugins.portal.SpecDetailRankingMonitor.time.sleep'))
        self.enterContext(patch('botend.controller.plugins.portal.SpecDetailRankingMonitor.CLASS_SPEC_MAP',
                                {'Mage': ['Fire']}))

    @staticmethod
    def row(index):
        return {'name': f'角色{index}', 'server': {'name': 'Realm', 'region': 'eu'},
                'report': {'code': 'Report', 'fightID': index + 1}, 'amount': 1000 - index,
                'hardModeLevel': 20 - index % 10}

    def test_graphql_uses_lightweight_mode_and_page_cache_has_no_details(self):
        now = timezone.now()
        cycle = WclCollectionCycle.objects.create(season=self.season, slot=now,
            started_at=now, last_attempt_at=now, owner='轻量测试')
        self.monitor._cycle = cycle
        row = {**self.row(0), 'gear': [{'large': '装备' * 1000}], 'talents': [{'large': '天赋' * 1000}]}
        payload = {'worldData': {'encounter': {'characterRankings':
            {'rankings': [row], 'hasMorePages': False}}}}
        with patch.object(self.monitor, '_wcl_graphql', return_value=payload) as transport:
            first = self.monitor._fetch_rankings_with_retry(1, 'Mage', 'Fire')
            second = self.monitor._fetch_rankings_with_retry(1, 'Mage', 'Fire')
        self.assertEqual(transport.call_count, 1)
        self.assertIn('includeCombatantInfo: false', transport.call_args.args[0])
        self.assertEqual(first, second)
        self.assertNotIn('gear', first['rankings'][0])
        self.assertNotIn('talents', first['rankings'][0])
        self.assertIn('gear', payload['worldData']['encounter']['characterRankings']['rankings'][0])

    def test_hundred_valid_rows_stop_before_next_page_and_never_read_details(self):
        payload = {'rankings': [self.row(i) for i in range(150)], 'hasMorePages': True}
        with patch.object(self.monitor, 'fetch_wcl_rankings', return_value=payload) as fetch, \
             patch.object(self.monitor, 'fetch_wcl_combatant_info', side_effect=AssertionError('榜单阶段请求详情')):
            rows = self.monitor._select_ranking_headers(1, 'Mage', 'Fire')
        self.assertEqual([r['name'] for r in rows], [f'角色{i}' for i in range(100)])
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(fetch.call_args.kwargs['include_combatant_info'])

    def test_invalid_references_and_duplicates_are_replaced_in_source_order(self):
        duplicate = deepcopy(self.row(0))
        duplicate['name'] = ' 角色0 '
        duplicate['server']['name'] = 'Re-alm'
        first = [self.row(i) for i in range(97)] + [duplicate,
            {**self.row(97), 'report': {}}, {**self.row(98), 'name': 'Anonymous'}]
        second = [self.row(0), {**self.row(99), 'server': {}}, *[self.row(i) for i in range(100, 110)]]
        pages = [{'rankings': first, 'hasMorePages': True}, {'rankings': second, 'hasMorePages': True}]
        for difficulty in (None, 4, 5):
            with self.subTest(difficulty=difficulty), \
                 patch.object(self.monitor, 'fetch_wcl_rankings', side_effect=deepcopy(pages)) as fetch:
                rows = self.monitor._select_ranking_headers(1, 'Mage', 'Fire', difficulty=difficulty)
                self.assertEqual([r['name'] for r in rows], [f'角色{i}' for i in range(97)] +
                                 ['角色100', '角色101', '角色102'])
                self.assertEqual([c.kwargs['page'] for c in fetch.call_args_list], [1, 2])

    def test_no_fixed_three_hundred_candidate_ceiling(self):
        pages = [{'rankings': [{**self.row(i), 'report': {}}
                               for i in range(page * 100, (page + 1) * 100)],
                  'hasMorePages': True} for page in range(4)] + [
            {'rankings': [self.row(i) for i in range(100)], 'hasMorePages': True}]
        with patch.object(self.monitor, 'fetch_wcl_rankings', side_effect=pages) as fetch:
            rows = self.monitor._select_ranking_headers(1, 'Mage', 'Fire')
        self.assertEqual(len(rows), 100)
        self.assertEqual(fetch.call_count, 5)

    def test_repeated_page_checkpoint_is_replaced_on_retry(self):
        now = timezone.now()
        self.monitor._cycle = WclCollectionCycle.objects.create(season=self.season, slot=now,
            started_at=now, last_attempt_at=now, owner='轻量测试')
        repeated = {'rankings': [self.row(0)], 'hasMorePages': True}
        with patch.object(self.monitor, 'fetch_wcl_rankings', return_value=repeated) as fetch:
            self.assertIsNone(self.monitor._select_ranking_headers(1, 'Mage', 'Fire'))
        self.assertEqual(fetch.call_count, 2)
        with patch.object(self.monitor, 'fetch_wcl_rankings', return_value={
                'rankings': [self.row(i) for i in range(1, 100)], 'hasMorePages': False}) as fetch:
            self.assertEqual(len(self.monitor._select_ranking_headers(1, 'Mage', 'Fire')), 100)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs['page'], 2)

    def test_source_exhausted_keeps_fewer_samples_and_transport_failure_is_not_empty(self):
        with patch.object(self.monitor, 'fetch_wcl_rankings', return_value={
                'rankings': [self.row(0)], 'hasMorePages': False}):
            self.assertEqual(len(self.monitor._select_ranking_headers(1, 'Mage', 'Fire')), 1)
        with patch.object(self.monitor, 'fetch_wcl_rankings', side_effect=[
                {'rankings': [self.row(0)], 'hasMorePages': True}, None, None, None]):
            self.assertIsNone(self.monitor._select_ranking_headers(1, 'Mage', 'Fire'))

    def test_collected_hundred_are_not_reduced_by_level_or_median_at_freeze(self):
        payload = {'rankings': [self.row(i) for i in range(100)], 'hasMorePages': True}
        with patch.object(self.monitor, 'fetch_wcl_rankings', return_value=payload):
            self.assertTrue(self.monitor._collect_dungeon_rankings(self.season))
            for difficulty in (4, 5):
                self.assertTrue(self.monitor._collect_raid_rankings(self.season, difficulty, '团本'))
        now = timezone.now()
        cycle = WclCollectionCycle.objects.create(season=self.season, slot=now,
            started_at=now, last_attempt_at=now, owner='轻量测试',
            completed_scopes=['dungeon:1', 'raid:4:2', 'raid:5:2'])
        freeze_samples(cycle, self.season, [('Mage', 'Fire')])
        self.assertEqual(len(cycle.selected_samples['Mage/Fire']['1']), 100)
        self.assertEqual(len(frozen_combatant_records(cycle)), 300)
        self.assertEqual(SpecDungeonRanking.objects.exclude(gear_json=[]).count(), 0)
        self.assertEqual(SpecRaidRanking.objects.exclude(talents_json=[]).count(), 0)

    def test_header_refresh_preserves_exact_log_details_and_does_not_load_large_json(self):
        row = SpecDungeonRanking.objects.create(season_id=self.season.pk, dungeon_id=1,
            dungeon_name='副本', class_name='Mage', spec_name='Fire',
            keystone_level=20, gear_json=[{'id': 42}], talents_json=[{'id': 123}],
            talent_build_code='已有天赋', **self.monitor._header_values(self.row(0)))
        with patch.object(self.monitor, 'fetch_wcl_rankings', return_value={
                'rankings': [{**self.row(0), 'amount': 2000}], 'hasMorePages': False}), \
             CaptureQueriesContext(connection) as queries:
            self.assertTrue(self.monitor._collect_dungeon_rankings(self.season))
        reads = [q['sql'] for q in queries if q['sql'].startswith('SELECT') and
                 'wow_spec_dungeon_ranking' in q['sql']]
        self.assertTrue(reads)
        self.assertTrue(all('gear_json' not in sql and 'talents_json' not in sql for sql in reads))
        row.refresh_from_db()
        self.assertEqual(row.gear_json, [{'id': 42}])
        self.assertEqual(row.talents_json, [{'id': 123}])
        self.assertEqual(row.talent_build_code, '已有天赋')
        self.assertEqual(row.dps, 2000)

    def test_failed_second_page_preserves_previous_scope_for_both_sources(self):
        for raid in (False, True):
            model = SpecRaidRanking if raid else SpecDungeonRanking
            scope = dict(boss_id=2, boss_name='首领', difficulty=5) if raid else dict(dungeon_id=1, dungeon_name='副本')
            old = model.objects.create(season_id=self.season.pk, class_name='Mage', spec_name='Fire',
                **scope, **self.monitor._header_values(self.row(1000)))
            before = model.objects.filter(pk=old.pk).values().get()
            with patch.object(self.monitor, 'fetch_wcl_rankings', side_effect=[
                    {'rankings': [self.row(0)], 'hasMorePages': True}, None, None, None]):
                ok = (self.monitor._collect_raid_rankings(self.season, 5, '团本') if raid else
                      self.monitor._collect_dungeon_rankings(self.season))
            self.assertFalse(ok)
            self.assertEqual(model.objects.filter(pk=old.pk).values().get(), before)
