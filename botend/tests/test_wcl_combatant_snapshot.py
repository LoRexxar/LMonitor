from copy import deepcopy

from django.test import TestCase
from django.utils import timezone

from botend.models import WclCombatantSnapshot
from botend.services.wcl_combatant_snapshot import enrich_dungeon_records


class SnapshotReadTests(TestCase):
    def test_modern_wcl_eighteenth_slot_is_tabard_with_empty_slots_preserved(self):
        from botend.services.wcl_combatant_snapshot import combatant_gear
        gear = [{'id': 0} for _ in range(18)]
        gear[0] = {'id': 268229}
        gear[17] = {'id': 45574}  # Myzouth fight 11: human tournament tabard.
        self.assertEqual([item['slot'] for item in combatant_gear(gear)], ['head', 'tabard'])

    def test_exact_log_identity_raw_ratings_and_no_profile_fallback(self):
        record = dict(report_code='M2ZDNKHBGrYbzPWk', fight_id=11,
                      character_name='Myzouth', realm='Burning-Legion', region='eu',
                      stats_json={'crit': {'rating': 9999, 'pct': 99}}, race='Human',
                      gear_json=[{'id': 123, 'slot': 'head'}])
        payload = dict(sourceID=232, specID=251,
                       source={'id': 232, 'name': 'Myzouth', 'server': 'BurningLegion'},
                       critMelee=1410, hasteMelee=504, mastery=1185, versatilityDamageDone=-114,
                       gear=[{'id': 456, 'slot': 0, 'gems': [{'id': 789}],
                              'permanentEnchant': 999}])
        WclCombatantSnapshot.objects.create(report_code=record['report_code'], fight_id=11,
                                           actor_id=232, payload_json=payload, fetched_at=timezone.now())
        original = deepcopy(record)
        got = enrich_dungeon_records([record], 'DeathKnight', 'Frost')[0]
        self.assertEqual(got['stats_json'], {'crit': {'rating': 1410}, 'haste': {'rating': 504},
                                           'mastery': {'rating': 1185}, 'versatility': {'rating': -114}})
        self.assertFalse(got.get('race'))
        self.assertEqual(got['gear_json'][0]['slot'], 'head')
        self.assertEqual(got['gear_json'][0]['gems_detail'][0]['id'], 789)
        self.assertNotEqual(got['gear_json'][0]['enchants_detail'][0].get('id'), 999)
        self.assertEqual(record, original)
        self.assertEqual(WclCombatantSnapshot.objects.get().payload_json, payload)
        for change in ({'realm': ''}, {'realm': 'Other'}, {'fight_id': 12}):
            missing = enrich_dungeon_records([{**record, **change}], 'DeathKnight', 'Frost')[0]
            self.assertEqual(missing['stats_json'], {})
            self.assertFalse(missing.get('race'))
            self.assertEqual(missing['gear_json'], original['gear_json'])
        self.assertEqual(enrich_dungeon_records([record], 'Mage', 'Frost')[0]['stats_json'], {})
        WclCombatantSnapshot.objects.create(report_code=record['report_code'], fight_id=11,
                                           actor_id=233, payload_json={**payload, 'sourceID': 233,
                                           'source': {**payload['source'], 'id': 233}}, fetched_at=timezone.now())
        self.assertEqual(enrich_dungeon_records([record], 'DeathKnight', 'Frost')[0]['stats_json'], {})

    def test_enchant_resolution_and_missing_talents_use_central_metadata(self):
        from botend.models import WowItemSnapshot
        from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
        WowItemSnapshot.objects.create(item_id=22222, enchantment_id=7961, name='Real enchant')
        payload = event()
        payload['gear'] = [{'id': '271474', 'itemLevel': '334', 'permanentEnchant': '7961',
                            'temporaryEnchant': 1000, 'gems': [{'id': '240967', 'itemLevel': 295}]}]
        payload['talentTree'] = [{'id': 777, 'rank': 2}]
        WclCombatantSnapshot.objects.create(report_code='Report', fight_id=11, actor_id=232, payload_json=payload)
        row = {**sample(), 'talents_json': []}
        got = enrich_dungeon_records([row], 'DeathKnight', 'Frost')[0]
        self.assertEqual(got['talents_json'][0]['node_id'], 777)
        self.assertEqual(got['gear_json'][0]['enchants_detail'][0]['id'], 22222)
        self.assertNotIn('id', got['gear_json'][0]['enchants_detail'][1])
        row['talents_json'] = [{'node_id': 123}]
        self.assertEqual(enrich_dungeon_records([row], 'DeathKnight', 'Frost')[0]['talents_json'], row['talents_json'])
        parsed = SpecDetailBase.parse_wcl_gear(payload['gear'])
        self.assertEqual(parsed[0]['permanentEnchant'], '7961')
        self.assertEqual(parsed[0]['gems_detail'][0]['id'], '240967')


def sample(fight=11, actor=232, code='Report'):
    return dict(report_code=code, fight_id=fight, character_name=f'Player{actor}',
                realm='Burning-Legion', region='eu', class_name='DeathKnight', spec_name='Frost')


def event(fight=11, actor=232):
    return dict(type='combatantinfo', fight=fight, sourceID=actor, specID=251,
                source={'id': actor, 'name': f'Player{actor}', 'server': 'BurningLegion', 'type': 'Player'},
                critMelee=1410, versatilityDamageDone=-114)


class SnapshotCollectionTests(TestCase):
    def test_batch_by_report_persist_once_and_resume_without_network(self):
        from unittest.mock import Mock
        from botend.services.wcl_combatant_snapshot import collect_dungeon_combatants
        rows = [sample(11, 232), sample(11, 233), sample(12, 232)]
        fetcher = Mock()
        fetcher.fetch_wcl_combatant_info.return_value = [event(11, 232), event(11, 233), event(12, 232)]
        dry = collect_dungeon_combatants(rows, fetcher)
        self.assertEqual(dry['pending'], 3)
        fetcher.fetch_wcl_combatant_info.assert_not_called()
        got = collect_dungeon_combatants(rows, fetcher, apply=True)
        self.assertEqual(got['matched'], 3)
        self.assertEqual(got['snapshots_written'], 3)
        self.assertEqual(got['reports_requested'], 1)
        fetcher.fetch_wcl_combatant_info.assert_called_once_with('Report', [11, 12])
        self.assertEqual(WclCombatantSnapshot.objects.get(fight_id=11, actor_id=232).payload_json, event(11, 232))
        fetcher.reset_mock()
        repeated = collect_dungeon_combatants(rows, fetcher, apply=True)
        self.assertEqual(repeated['cached'], 3)
        self.assertEqual(repeated['snapshots_written'], 0)
        fetcher.fetch_wcl_combatant_info.assert_not_called()

    def test_permission_rate_limit_and_malformed_batches_preserve_old_facts(self):
        from unittest.mock import Mock
        from botend.services.wcl_combatant_snapshot import collect_dungeon_combatants
        old = WclCombatantSnapshot.objects.create(report_code='Report', fight_id=11, actor_id=232, payload_json=event())
        rows = [sample(11, 232), sample(12, 232), sample(13, 232)]
        fetcher = Mock(_wcl_last_error='permission')
        fetcher.fetch_wcl_combatant_info.return_value = None
        result = collect_dungeon_combatants(rows, fetcher, apply=True)
        self.assertEqual((result['cached'], result['permission_denied'], result['failed']), (1, 2, 0))
        fetcher._wcl_last_error = 'rate_limit'
        fetcher.reset_mock()
        result = collect_dungeon_combatants(rows, fetcher, apply=True, batch_size=1)
        self.assertEqual(result['failed'], 2)
        fetcher.fetch_wcl_combatant_info.assert_called_once()
        fetcher.fetch_wcl_combatant_info.return_value = [event(12, 232), {'fight': 13}]
        result = collect_dungeon_combatants(rows, fetcher, apply=True)
        self.assertEqual(result['failed'], 2)
        self.assertEqual(WclCombatantSnapshot.objects.count(), 1)
        old.refresh_from_db()
        self.assertEqual(old.payload_json, event())

    def test_paginated_helper_keeps_source_and_rejects_partial_results(self):
        from unittest.mock import patch
        from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
        collector = SpecDetailBase(None, None)
        def page(fight, cursor):
            payload = event(fight)
            source = payload.pop('source')
            return {'reportData': {'report': {'masterData': {'actors': [source]},
                                              'events': {'data': [payload], 'nextPageTimestamp': cursor}}}}
        with patch.object(collector, '_wcl_graphql', side_effect=[page(11, 200), page(12, None)]) as api:
            self.assertEqual(collector.fetch_wcl_combatant_info('Report', [11, 12]), [event(11), event(12)])
            self.assertEqual(api.call_args_list[1].args[1]['start'], 200)
            self.assertEqual(api.call_args_list[0].args[1]['fightIDs'], [11, 12])
            self.assertIn('nextPageTimestamp', api.call_args_list[0].args[0])
        with patch.object(collector, '_wcl_graphql', side_effect=[page(11, 200), None]):
            self.assertIsNone(collector.fetch_wcl_combatant_info('Report', [11, 12]))
        with patch.object(collector, '_wcl_graphql', side_effect=[page(11, 200), page(12, 200)]):
            self.assertIsNone(collector.fetch_wcl_combatant_info('Report', [11, 12]))
            self.assertEqual(collector._wcl_last_error, 'invalid_pagination')

    def test_command_uses_current_top100_union_dry_run_and_apply(self):
        from io import StringIO
        import json
        from unittest.mock import patch
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from botend.models import SeasonMeta, SpecDungeonRanking
        season = SeasonMeta.objects.create(season_key='combatants', season_name='test', is_active=True,
                                          mplus_encounters=[{'id': 1}, {'id': 2}], mplus_zone_id=1, raid_zone_id=2)
        for dungeon in (1, 2, 999):
            SpecDungeonRanking.objects.bulk_create([
                SpecDungeonRanking(season_id=season.id, dungeon_id=dungeon, dungeon_name='Test',
                                   dps=100, keystone_level=20, **sample(dungeon, actor))
                for actor in range(1, 103)])
        kwargs = dict(season=season.id, class_name='DeathKnight', spec_name='Frost')
        out = StringIO()
        with patch('botend.controller.plugins.portal.SpecDetailBase.SpecDetailBase._wcl_graphql') as api:
            call_command('backfill_wcl_dungeon_combatants', stdout=out, **kwargs)
            api.assert_not_called()
        self.assertEqual(json.loads(out.getvalue().splitlines()[0])['selected'], 200)
        self.assertEqual(WclCombatantSnapshot.objects.count(), 0)
        with patch('botend.controller.plugins.portal.SpecDetailBase.SpecDetailBase._wcl_graphql',
                   return_value={'rateLimitData': {'limitPerHour': 3600, 'pointsSpentThisHour': 1}}), \
             patch('botend.controller.plugins.portal.SpecDetailBase.SpecDetailBase.fetch_wcl_combatant_info',
                   return_value=[event(fight, actor) for fight in (1, 2) for actor in range(1, 103)]) as fetch:
            call_command('backfill_wcl_dungeon_combatants', apply=True, stdout=StringIO(), **kwargs)
            fetch.assert_called_once_with('Report', [1, 2])
        self.assertEqual(WclCombatantSnapshot.objects.count(), 204)
        with self.assertRaises(CommandError):
            call_command('backfill_wcl_dungeon_combatants', season=season.id + 1, stdout=StringIO())

    def test_monitor_collects_after_each_dungeon_not_at_end(self):
        from unittest.mock import patch
        from botend.models import SeasonMeta, SpecDungeonRanking
        from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
        season = SeasonMeta.objects.create(season_key='monitor-combatants', season_name='test', is_active=True,
                                          mplus_encounters=[{'id': 1, 'name': 'One'}, {'id': 2, 'name': 'Two'}],
                                          mplus_zone_id=1, raid_zone_id=2)
        monitor = SpecDetailRankingMonitor(None, None)
        trace = []
        def ranking(enc, *args, **kwargs):
            trace.append(('ranking', enc))
            return {'rankings': [{'name': 'Player232', 'server': {'name': 'BurningLegion', 'region': 'eu'},
                                  'report': {'code': f'Report{enc}', 'fightID': enc},
                                  'amount': 100, 'hardModeLevel': 20}], 'hasMorePages': False}
        def combatants(code, fights):
            trace.append(('combatant', fights[0]))
            self.assertTrue(SpecDungeonRanking.objects.filter(dungeon_id=fights[0]).exists())
            return [event(fights[0])]
        path = 'botend.controller.plugins.portal.SpecDetailRankingMonitor'
        with patch(path + '.CLASS_SPEC_MAP', {'DeathKnight': ['Frost']}), \
             patch(path + '.time.sleep'), \
             patch.object(monitor, '_fetch_rankings_with_retry', side_effect=ranking), \
             patch.object(monitor, 'fetch_wcl_combatant_info', side_effect=combatants):
            self.assertTrue(monitor._collect_dungeon_rankings(season))
        self.assertEqual(trace, [('ranking', 1), ('combatant', 1), ('ranking', 2), ('combatant', 2)])
        self.assertEqual(WclCombatantSnapshot.objects.count(), 2)
