"""M+ observations never borrow another time's character profile."""
from django.test import TestCase
from botend.models import SeasonMeta, SpecDungeonRanking, PlayerSpecTopPlayer
from botend.services.spec_stats_service import SpecStatsService


class DungeonWclOnlyTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(
            season_key='wcl-only', season_name='WCL', mplus_zone_id=1, raid_zone_id=2,
            mplus_encounters=[{'id': 1, 'name': 'Dungeon'}])
        self.row = SpecDungeonRanking.objects.create(
            season_id=self.season.id, dungeon_id=1, dungeon_name='Dungeon',
            class_name='DeathKnight', spec_name='Frost', character_name='Player',
            realm='Burning Legion', region='EU', report_code='ReportCode', fight_id=11,
            dps=100, keystone_level=20,
            gear_json=[{'id': 123, 'slot': 'head', 'name': 'WCL item',
                        'gems': [{'id': 456, 'name': 'WCL gem'}]}])
        PlayerSpecTopPlayer.objects.create(
            season_id=self.season.id, class_name='DeathKnight', spec_name='Frost',
            character_name='Player', realm='Burning Legion', region='eu', race='Orc',
            stats_json={'crit': {'rating': 916, 'pct': 24.9}},
            gear_json=[{'id': 999, 'slot': 'head', 'name': 'Current RIO item'}])

    def test_exact_fight_log_rating_wins_and_missing_percent_is_not_invented(self):
        from botend.models import WclCombatantSnapshot
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        WclCombatantSnapshot.objects.create(
            report_code='ReportCode', fight_id=11, actor_id=232,
            payload_json={'type': 'combatantinfo', 'fight': 11, 'sourceID': 232,
                          'source': {'id': 232, 'name': 'Player', 'server': 'BurningLegion',
                                     'type': 'Player', 'subType': 'DeathKnight'},
                          'specID': 251, 'critMelee': 1410, 'hasteMelee': 504,
                          'mastery': 1185, 'versatilityDamageDone': -114})
        WclCombatantSnapshot.objects.create(
            report_code='ReportCode', fight_id=12, actor_id=232,
            payload_json={'type': 'combatantinfo', 'fight': 12, 'sourceID': 232,
                          'source': {'id': 232, 'name': 'Player', 'server': 'BurningLegion',
                                     'type': 'Player', 'subType': 'DeathKnight'},
                          'specID': 251, 'critMelee': 9999})
        with CaptureQueriesContext(connection) as queries:
            result = SpecStatsService.get_dungeon_summary('DeathKnight', 'Frost', self.season.id)
        stats = {s['key']: s for s in result['secondary_stats']}
        self.assertEqual(stats['crit']['rating']['median'], 1410)
        self.assertIsNone(stats['crit']['pct'])
        self.assertEqual(stats['crit']['sample_size'], 1)
        self.assertEqual(stats['versatility']['rating']['median'], -114)
        self.assertFalse(any('wow_player_spec_top' in q['sql'] for q in queries))

    def test_single_and_combined_never_borrow_current_profile(self):
        for result in (
            SpecStatsService.get_dungeon_summary('DeathKnight', 'Frost', self.season.id),
            SpecStatsService.get_dungeon_detail(1, 'DeathKnight', 'Frost', self.season.id),
        ):
            self.assertEqual(result['secondary_stats'], [])
            self.assertEqual(result['race_distribution'][0]['race'], 'unknown')
            self.assertEqual(result['gear_popularity']['头盔'][0]['id'], 123)
            self.assertEqual(result['gem_popularity'][0]['id'], 456)
            self.assertEqual(result['sample_size'], 1)
            self.assertEqual(result['data_contract'], 'wcl-log-v1')
            self.assertTrue(all('Warcraft Logs' in v for v in result['field_sources'].values()))
