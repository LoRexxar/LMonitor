"""核对消耗品的事件归属、覆盖率和既有日志的增量补采。"""
import json
from pathlib import Path
from unittest.mock import Mock, patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import WclConsumableObservation
from botend.services import wcl_combatant_snapshot as snapshots
from botend.services import wcl_combatant_store as store
from botend.services.spec_stats_service import SpecStatsService
from botend.services.wcl_consumables import fetch_usage, statistics
from botend.tests.test_wcl_combatant_snapshot import event, sample


def envelope(events, cursor=None):
    return {'consumableEvents': {'data': events, 'nextPageTimestamp': cursor},
            'fights': [{'id': 11, 'startTime': 0, 'endTime': 100},
                       {'id': 12, 'startTime': 200, 'endTime': 500}]}


class ConsumableEventTests(SimpleTestCase):
    def test_pagination_fight_and_actor_attribution(self):
        rows = [dict(event(), auras=[{'ability': 1294727}]), dict(event(actor=2), auras=[]), event(12)]
        initial = envelope([{'type': 'applybuff', 'fight': 11, 'sourceID': 2, 'targetID': 232,
                             'abilityGameID': 1235108}], 50)
        cast = dict(type='cast', sourceID=2, timestamp=80, abilityGameID=1236994)
        last = envelope([cast, cast, dict(cast, timestamp=204, sourceID=232, abilityGameID=1234768),
                         dict(cast, type='begincast', sourceID=232)])
        client = Mock()
        client._wcl_graphql.return_value = {'reportData': {'report': last}}
        self.assertIs(fetch_usage(client, 'Report', [11, 12], rows, initial), rows)
        self.assertEqual(client._wcl_graphql.call_args.args[1]['useStart'], 50)
        self.assertEqual(rows[0]['consumables'], dict(food=[1294727], flask=[1235108], potion=[], healing=[]))
        self.assertEqual(rows[1]['consumables']['potion'], [1236994])
        self.assertEqual(rows[2]['consumables'], dict(potion=[], healing=[1234768]))
        groups = {g['key']: g for g in statistics(rows + [{}])}
        self.assertEqual(groups['potion']['samples'], 3)
        self.assertEqual(groups['potion']['rows'][0]['pct'], 33.3)
        self.assertEqual(groups['food']['rows'][0]['pct'], 50)

    def test_failed_or_ambiguous_timeline_never_publishes_zeroes(self):
        bad_event = dict(type='cast', sourceID=232, abilityGameID=1236994, timestamp=150)
        for first in ({}, envelope([bad_event]), envelope([], 10)):
            rows = [dict(event(), auras=[])]
            client = Mock(_wcl_last_error='')
            client._wcl_graphql.return_value = {'reportData': {'report': envelope([], 10)}}
            self.assertIsNone(fetch_usage(client, 'Report', [11, 12], rows, first))
            self.assertNotIn('consumables', rows[0])

    def test_duplicate_ranking_reference_counts_one_actor_fight(self):
        row = dict(sample(), wcl_actor_id=232, consumables={'potion': [1236994, 1236994]})
        groups = {g['key']: g for g in statistics([row, dict(row, realm='BurningLegion')])}
        self.assertEqual(groups['potion']['samples'], 1)
        self.assertEqual(groups['potion']['rows'][0]['count'], 1)

    def test_captured_raid_opening_buffs(self):
        path = Path(__file__).parent / 'fixtures' / 'wcl_raid_unknown_spec.json'
        rows = json.loads(path.read_text())['events']
        fetch_usage(Mock(), 'Report', [rows[0]['fight']], rows, envelope([]))
        groups = {g['key']: g for g in statistics(rows)}
        self.assertEqual(groups['flask']['samples'], 30)
        self.assertEqual(sum(row['count'] for row in groups['flask']['rows']), 21)
        self.assertEqual(sum(row['count'] for row in groups['food']['rows']), 16)


class ConsumableAggregationTests(TestCase):
    def test_backfill_cutoff_reuse_and_both_detail_pages(self):
        row = dict(sample(), dps=123)
        original = dict(event(), auras=[], gear=[{'slot': 15, 'id': 999, 'temporaryEnchant': 8052},
                                                 {'slot': 16, 'id': 888, 'temporaryEnchant': 8051}])
        store.persist_events('Report', {11: [original]})
        cutoff = timezone.now()
        fetcher = SpecDetailBase(None, None)
        report = dict(envelope([dict(type='cast', fight=11, sourceID=232, abilityGameID=1236994)]),
                      masterData={'actors': [original['source']]},
                      events={'data': [original], 'nextPageTimestamp': None})
        with patch.object(fetcher, '_wcl_graphql', return_value={'reportData': {'report': report}}) as http, \
                patch.object(snapshots, 'load_staged_events', return_value=None), \
                patch.object(snapshots, 'stage_events', return_value={'status': 'stored'}):
            first = snapshots.collect_dungeon_combatants([row], fetcher, apply=True)
            second = snapshots.collect_dungeon_combatants([row], fetcher, apply=True)
        self.assertEqual((first['matched'], first['snapshots_written'], second['cached']), (1, 0, 1))
        self.assertEqual(http.call_count, 1)
        self.assertEqual(WclConsumableObservation.objects.count(), 1)
        old = snapshots.load_matched_facts([row], 'DeathKnight', 'Frost', fetched_before=cutoff)['Report', 11][0]
        new = snapshots.load_matched_facts([row], 'DeathKnight', 'Frost')['Report', 11][0]
        self.assertNotIn('consumables', old)
        self.assertNotEqual(old.cache_token, new.cache_token)
        wrong = dict(row, realm='其他服务器')
        for compute in (SpecStatsService._compute_dungeon_records, SpecStatsService._compute_raid_records):
            detail = compute([row, wrong], 1, 10, '测试', 'DeathKnight', 'Frost')
            groups = {g['key']: g for g in detail['consumables']}
            self.assertEqual((detail['sample_size'], groups['potion']['samples']), (2, 1))
            self.assertEqual(groups['potion']['rows'], [dict(name='鲁莽药水', count=1, pct=100.0)])
            self.assertEqual(groups['weapon']['rows'], [dict(name='萨拉斯凤凰之油', count=1, pct=100.0)])
            dungeon = 'dungeon_id' in detail
            template = 'dungeon_stats' if dungeon else 'raid_stats'
            context = {'class_name': 'DeathKnight', 'spec_name': 'Frost',
                       ('dungeon_detail' if dungeon else 'boss_detail'): detail}
            html = render_to_string(f'portal/spec_detail/{template}.html', context)
            self.assertIn('已采集 1 / 2', html)
            self.assertIn('鲁莽药水', html)
            self.assertIn('未识别到此类消耗品', html)
