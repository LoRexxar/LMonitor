"""Captured WCL specID=0 excludes one player, not the complete batch.

Fixture: locally captured ranking + complete 30-player CombatantInfo batch;
name/report/server identifiers anonymized consistently, event content retained.
Only HTTP is replaced: scan, Base fetch/transport, validation and ORM are real.
"""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import patch

import requests
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
from botend.models import (SeasonMeta, SpecRaidRanking, WclApiBudget,
                           WclCollectionCycle, WclCombatantSnapshot, WclCombatantFetchState)
from botend.services.wcl_api_budget import account_key
from botend.services.wcl_combatant_snapshot import collect_dungeon_combatants, _validated_events
from botend.tests.aggregation_cache_helpers import isolate_work_cache
from botend.tests.test_wcl_input_completeness import Response, page

RANKING = 'botend.controller.plugins.portal.SpecDetailRankingMonitor'


@override_settings(WCL_V2_CONFIG={'client_id': 'raid-unknown-client', 'client_secret': 'not-real'})
class RaidUnknownSpecTests(TransactionTestCase):
    def setUp(self):
        isolate_work_cache(self)
        self.fixture = json.loads((Path(__file__).parent / 'fixtures' / 'wcl_raid_unknown_spec.json').read_text())
        self.ranking = self.fixture['ranking']
        self.events = self.fixture['events']
        self.target = next(e for e in self.events if e['source']['name'] == self.ranking['name'])
        self.now = timezone.now()
        self.season = SeasonMeta.objects.create(season_key='raid-unknown', season_name='Raid',
            is_active=True, mplus_zone_id=1, raid_zone_id=2,
            mplus_encounters=[], raid_encounters=[{'id': 3455, 'name': 'Boss'}])
        WclApiBudget.objects.create(account_key=account_key('raid-unknown-client'),
            limit_per_hour=18000, spent=0, reset_at=self.now + timedelta(hours=1), observed_at=self.now)
        self.monitor = SpecDetailRankingMonitor(None, None)
        self.monitor._wcl_token, self.monitor._wcl_token_expire = 'not-real', float('inf')
        for p in (patch(RANKING + '.CLASS_SPEC_MAP', {'Warlock': ['Destruction']}),
                  patch(RANKING + '.time.sleep')):
            p.start()
            self.addCleanup(p.stop)

    def old_raid(self):
        return SpecRaidRanking.objects.create(season_id=self.season.pk, boss_id=3455,
            boss_name='Boss', difficulty=5, class_name='Warlock', spec_name='Destruction',
            character_name=self.ranking['name'], realm=self.ranking['server']['name'], region='EU',
            report_code=self.ranking['report']['code'], fight_id=self.ranking['report']['fightID'],
            dps=1, talents_json=[{'talentID': 999, 'points': 1}], talent_build_code='old-code')

    def http(self, *args, **kwargs):
        self.assertFalse(connection.in_atomic_block)
        if 'characterRankings' in kwargs['json']['query']:
            return Response({'data': {'worldData': {'encounter': {'characterRankings': {
                'rankings': [deepcopy(self.ranking)], 'hasMorePages': False}}}}})
        self.assertEqual(kwargs['json']['variables']['fightIDs'], [self.ranking['report']['fightID']])
        return page(self.events)

    def test_captured_empty_talents_finish_scan_without_stale_talents_or_facts(self):
        self.assertIs(type(self.target['specID']), int)
        self.assertEqual((self.target['specID'], self.target['talents'], self.target['talentTree']), (0, [], []))
        self.assertEqual(len(self.target['gear']), 18)
        old = self.old_raid()
        original = deepcopy(self.events)
        with patch('requests.post', side_effect=self.http) as http:
            self.assertTrue(self.monitor.scan(''))
        self.assertEqual(http.call_count, 3)  # two rankings share one report fetch
        self.assertEqual(self.events, original)
        old.refresh_from_db()
        self.assertEqual(old.talents_json, [])
        self.assertEqual(old.talent_build_code, '')
        self.assertEqual(old.dps, self.ranking['amount'])
        self.assertTrue(old.gear_json)
        self.assertEqual(SpecRaidRanking.objects.count(), 2)
        self.assertFalse(SpecRaidRanking.objects.exclude(talents_json=[]).exists())
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual(cycle.phase, 'ready')
        self.assertEqual(set(cycle.completed_scopes), {'raid:5:3455', 'raid:4:3455'})
        self.assertIsNone(cycle.next_retry_at)
        facts = WclCombatantSnapshot.objects.filter(report_code=self.ranking['report']['code'])
        self.assertEqual(facts.count(), 29)
        self.assertFalse(facts.filter(actor_id=self.target['sourceID']).exists())
        state = WclCombatantFetchState.objects.get()
        self.assertEqual((state.report_code, state.fight_id, state.status),
                         (self.ranking['report']['code'], self.ranking['report']['fightID'], 'missing'))
        self.assertEqual(state.retry_after - state.checked_at, timedelta(hours=6))

    def test_valid_players_talents_survive_unknown_spec_bystander(self):
        valid = next(e for e in self.events if e['specID'] == 261 and e['talentTree'])
        self.ranking['name'] = valid['source']['name']
        self.ranking['server']['name'] = valid['source']['server']
        # Captured spec 261 is Rogue/Subtlety, not the unknown Warlock target.
        with patch(RANKING + '.CLASS_SPEC_MAP', {'Rogue': ['Subtlety']}), \
             patch('requests.post', side_effect=self.http):
            self.assertTrue(self.monitor.scan(''))
        expected = self.monitor.parse_wcl_talent_tree(valid['talentTree'])
        self.assertTrue(expected)
        self.assertEqual(set(SpecRaidRanking.objects.values_list('class_name', 'spec_name')),
                         {('Rogue', 'Subtlety')})
        self.assertEqual(list(SpecRaidRanking.objects.values_list('talents_json', flat=True)), [expected, expected])

    def test_raid_unknown_spec_no_longer_blocks_real_mplus_supplement(self):
        self.season.mplus_encounters = [{'id': 101, 'name': 'Dungeon'}]
        self.season.save(update_fields=['mplus_encounters'])
        valid = next(e for e in self.events if e['specID'] == 266)
        dungeon_rank = deepcopy(self.ranking)
        dungeon_rank.update(name=valid['source']['name'], hardModeLevel=10)
        dungeon_rank['server']['name'] = valid['source']['server']
        dungeon_rank['report']['code'] = 'DungeonReport'
        def fetch(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            query, variables = kwargs['json']['query'], kwargs['json']['variables']
            if 'characterRankings' in query:
                dungeon = 'encounter(id: 101)' in query
                demonology = 'specName: "Demonology"' in query
                missing_rank = {**self.ranking, 'hardModeLevel': 10,
                                'report': {**self.ranking['report'], 'code': 'DungeonReport'}}
                rows = ([dungeon_rank] if demonology else [missing_rank]) if dungeon else (
                    [self.ranking] if not demonology else [])
                return Response({'data': {'worldData': {'encounter': {'characterRankings': {
                    'rankings': rows, 'hasMorePages': False}}}}})
            if variables['code'] == 'DungeonReport':
                cycle = WclCollectionCycle.objects.get()
                self.assertEqual(cycle.phase, 'combatants')
                self.assertEqual(set(cycle.completed_scopes), {'dungeon:101', 'raid:5:3455', 'raid:4:3455'})
                return page(self.events)
            return self.http(*args, **kwargs)
        with patch(RANKING + '.CLASS_SPEC_MAP', {'Warlock': ['Destruction', 'Demonology']}), \
             patch('requests.post', side_effect=fetch):
            self.assertTrue(self.monitor.scan(''))
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual(cycle.phase, 'ready')
        self.assertEqual((cycle.counts['selected'], cycle.counts['matched'], cycle.counts['missing'],
                          cycle.counts['failed'], cycle.counts['snapshots_written']), (2, 1, 1, 0, 29))
        self.assertIsNone(cycle.next_retry_at)
        self.assertEqual(WclCombatantSnapshot.objects.count(), 58)
        reports = {self.ranking['report']['code'], 'DungeonReport'}
        self.assertEqual(set(WclCombatantSnapshot.objects.values_list('report_code', flat=True)), reports)
        self.assertEqual(WclCombatantFetchState.objects.count(), 2)
        for code in reports:
            with self.subTest(report=code):
                facts = WclCombatantSnapshot.objects.filter(report_code=code)
                self.assertEqual(facts.count(), 29)
                self.assertFalse(facts.filter(actor_id=self.target['sourceID']).exists())
                self.assertEqual(facts.get(actor_id=valid['sourceID']).payload_json, valid)
                state = WclCombatantFetchState.objects.get(report_code=code)
                self.assertEqual((state.fight_id, state.status),
                                 (self.ranking['report']['fightID'], 'missing'))
                self.assertEqual(state.retry_after - state.checked_at, timedelta(hours=6))
        self.assertFalse(SpecRaidRanking.objects.exclude(talents_json=[]).exists())

    def test_unknown_spec_is_missing_without_discarding_valid_immutable_facts(self):
        validated = _validated_events(self.events, [self.ranking['report']['fightID']])
        self.assertEqual(len(validated[self.ranking['report']['fightID']]), 29)
        record = dict(report_code=self.ranking['report']['code'], fight_id=self.ranking['report']['fightID'],
            character_name=self.ranking['name'], realm=self.ranking['server']['name'], region='EU',
            class_name='Warlock', spec_name='Destruction')
        with patch('requests.post', side_effect=self.http):
            counts = collect_dungeon_combatants([record], self.monitor, apply=True)
        self.assertEqual((counts['failed'], counts['missing'], counts['snapshots_written']), (0, 1, 29))
        self.assertFalse(WclCombatantSnapshot.objects.filter(actor_id=self.target['sourceID']).exists())
        self.assertEqual(WclCombatantFetchState.objects.get().status, 'missing')

    def test_raid_missing_identity_or_talents_is_unavailable_without_retry(self):
        cases = [('source', key, value) for key, values in (
            ('name', [None, '', 'Anonymous', 'MISSING']),
            ('server', [None, '', 'MISSING'])) for value in values]
        cases += [('event', key, value) for key in ('specID', 'talents', 'talentTree')
                  for value in (None, 'MISSING')]
        original = deepcopy(self.target)
        for container, key, value in cases:
            with self.subTest(container=container, field=key, value=value):
                self.target.clear()
                self.target.update(deepcopy(original))
                target = self.target['source'] if container == 'source' else self.target
                if value == 'MISSING':
                    target.pop(key)
                else:
                    target[key] = value
                cache = {}
                with patch('requests.post', side_effect=self.http):
                    self.assertEqual(self.monitor._parse_rank_talents(self.ranking, cache), [])
                self.assertEqual(len(cache), 1)

    def test_malformed_types_are_not_authoritative_missing(self):
        cases = [(key, value) for key, values in (
            ('specID', [False, '0', -1, 'invalid', {}, []]),
            ('talents', [{}, 'bad', [None]]),
            ('talentTree', [{}, 'bad', [None]]),
            ('gear', [{}, 'bad', [None]])) for value in values]
        original = deepcopy(self.target)
        for key, value in cases:
            with self.subTest(field=key, value=value):
                self.target.clear()
                self.target.update(deepcopy(original))
                if value == 'MISSING':
                    self.target.pop(key)
                else:
                    self.target[key] = value
                cache = {}
                with patch('requests.post', side_effect=self.http):
                    with self.assertRaises(ValueError):
                        self.monitor._parse_rank_talents(self.ranking, cache)
                self.assertEqual(cache, {})

    def test_unknown_spec_does_not_relax_player_fight_or_actor_identity(self):
        original = deepcopy(self.target)
        for container, key, value in (
            ('source', 'name', 123), ('source', 'server', {}),
            ('source', 'type', None), ('source', 'id', 999999),
            ('event', 'sourceID', 999999), ('event', 'fight', 999999)):
            with self.subTest(container=container, field=key):
                self.target.clear()
                self.target.update(deepcopy(original))
                target = self.target['source'] if container == 'source' else self.target
                target[key] = value
                cache = {}
                with patch('requests.post', side_effect=self.http):
                    with self.assertRaises(ValueError):
                        self.monitor._parse_rank_talents(self.ranking, cache)
                self.assertEqual(cache, {})

    def test_transport_or_incomplete_page_preserves_boss_and_cycle_for_retry(self):
        old = self.old_raid()
        before = SpecRaidRanking.objects.filter(pk=old.pk).values().get()
        malformed = page(self.events)
        malformed.payload['data']['reportData']['report']['events'].pop('nextPageTimestamp')
        for response in (Response(status=403), Response(status=401),
                         requests.ConnectionError('offline fixture'), malformed):
            with self.subTest(response=type(response).__name__, status=getattr(response, 'status_code', None)):
                WclCollectionCycle.objects.all().delete()
                def fetch(*args, **kwargs):
                    if 'characterRankings' in kwargs['json']['query']:
                        return self.http(*args, **kwargs)
                    if isinstance(response, Exception):
                        raise response
                    return response
                with patch('requests.post', side_effect=fetch) as http:
                    self.assertFalse(self.monitor.scan(''))
                self.assertEqual(http.call_count, 2)
                self.assertEqual(SpecRaidRanking.objects.filter(pk=old.pk).values().get(), before)
                cycle = WclCollectionCycle.objects.get()
                self.assertEqual((cycle.phase, cycle.completed_scopes), ('rankings', []))
                self.assertIsNotNone(cycle.next_retry_at)
                self.assertFalse(WclCombatantFetchState.objects.exists())

    def test_explicit_report_permission_is_unavailable_not_transient(self):
        denied = Response({'errors': [{'message': 'You do not have permission to view this report.',
                                     'path': ['reportData', 'report']}]})
        def fetch(*args, **kwargs):
            return self.http(*args, **kwargs) if 'characterRankings' in kwargs['json']['query'] else denied
        with patch('requests.post', side_effect=fetch) as http:
            self.assertTrue(self.monitor.scan(''))
        self.assertEqual(http.call_count, 3)  # second difficulty reuses report denial
        self.assertEqual(WclCollectionCycle.objects.get().phase, 'ready')
        self.assertFalse(SpecRaidRanking.objects.exclude(talents_json=[]).exists())
        self.assertFalse(WclCombatantSnapshot.objects.exists())
        state = WclCombatantFetchState.objects.get()
        self.assertEqual((state.report_code, state.fight_id, state.status),
                         (self.ranking['report']['code'], 0, 'permission_denied'))
        self.assertEqual(state.retry_after - state.checked_at, timedelta(hours=1))
        record = dict(report_code=state.report_code, fight_id=self.ranking['report']['fightID'],
            character_name=self.ranking['name'], realm=self.ranking['server']['name'], region='EU',
            class_name='Warlock', spec_name='Destruction')
        with patch('requests.post', side_effect=fetch) as http:
            counts = collect_dungeon_combatants([record], self.monitor, apply=True)
        http.assert_not_called()
        self.assertEqual((counts['permission_denied'], counts['negative_cached'], counts['failed']), (1, 1, 0))
