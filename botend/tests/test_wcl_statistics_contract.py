"""Top100 只使用对应日志，缺失字段不补个人资料也不展示空板块。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.template.loader import render_to_string
from django.utils import timezone

from botend.models import (SeasonMeta, SpecRaidRanking, SpecDungeonRanking,
                           PlayerSpecTopPlayer, WclCombatantSnapshot, WclCollectionCycle)
from botend.services.spec_stats_service import SpecStatsService, _select_dungeon_sample_records
from botend.services.raid_aggregation import select_raid_records, raid_stats, raid_fingerprint
from botend.services.spec_stats_snapshot import read_log_projection
from botend.services.wcl_collection_cycle import freeze_samples, frozen_combatant_records, frozen_season
from botend.tests.aggregation_cache_helpers import isolate_work_cache


class WclStatisticsContractTests(TestCase):
    def setUp(self):
        isolate_work_cache(self)
        self.season = SeasonMeta.objects.create(season_key='纯日志统计', season_name='测试',
            mplus_zone_id=1, raid_zone_id=2, mplus_encounters=[{'id': 1, 'name': '副本'}],
            raid_encounters=[{'id': 2, 'name': '首领'}])
        self.row = SpecRaidRanking.objects.create(season_id=self.season.pk, boss_id=2,
            boss_name='首领', class_name='Mage', spec_name='Fire', difficulty=5,
            character_name='Player', realm='Realm', region='eu', report_code='Report', fight_id=7,
            dps=100, kill_time=60000, gear_json=[{'id': 123, 'slot': 'head', 'name': '日志装备'}])
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk,
            class_name='Mage', spec_name='Fire', character_name='Player', realm='Realm', region='eu',
            race='Orc', stats_json={'crit': {'rating': 9999}}, gear_json=[{'id': 999, 'slot': 'head'}])

    def args(self):
        return self.season.pk, 2, '首领', 'Mage', 'Fire', 5

    def fact(self, fight=7, **extra):
        return WclCombatantSnapshot.objects.create(report_code='Report', fight_id=fight, actor_id=3,
            payload_json={'sourceID': 3, 'source': {'id': 3, 'name': 'Player', 'server': 'Realm'},
                          'specID': 63, **extra})

    def test_profile_change_never_changes_statistics_or_cache(self):
        with CaptureQueriesContext(connection) as queries:
            result, key, hit = raid_stats(*self.args())
        self.assertFalse(hit)
        self.assertEqual(result['sample_size'], 1)
        self.assertEqual(result['gear_popularity']['头盔'][0]['id'], 123)
        self.assertNotIn('race_distribution', result)
        self.assertNotIn('secondary_stats', result)
        self.assertFalse(any('wow_spec_top_player' in row['sql'] for row in queries))
        self.player.race = 'Human'
        self.player.gear_json = [{'id': 777}]
        self.player.save()
        self.assertEqual(raid_stats(*self.args()), (result, key, True))

    def test_only_exact_log_supplies_optional_fields_and_invalidates_cache(self):
        _, before, _ = raid_stats(*self.args())
        self.fact(fight=8, critMelee=999, race='Orc')
        self.assertEqual(before, raid_fingerprint(*self.args()))
        self.fact(critMelee=123, race='Human', gear=[{'id': 456, 'slot': 0}])
        result, after, hit = raid_stats(*self.args())
        self.assertFalse(hit)
        self.assertNotEqual(before, after)
        self.assertEqual(result['secondary_stats'][0]['rating']['median'], 123)
        self.assertEqual(result['race_distribution'][0]['race'], 'Human')
        self.assertEqual(result['gear_popularity']['头盔'][0]['id'], 456)
        self.assertEqual(result['kill_time']['median'], 60000)

    def test_top100_excludes_missing_log_references_and_other_difficulties(self):
        base = dict(season_id=self.season.pk, boss_id=2, class_name='Mage', spec_name='Fire')
        SpecRaidRanking.objects.bulk_create([SpecRaidRanking(**base, character_name=f'Player{i}',
            difficulty=5, report_code='Report', fight_id=20+i, dps=200+i) for i in range(105)])
        SpecRaidRanking.objects.create(**base, difficulty=5, character_name='NoLog', dps=10000)
        SpecRaidRanking.objects.create(**base, difficulty=4, character_name='Heroic',
                                      report_code='Report', fight_id=999, dps=20000)
        rows = select_raid_records(self.season.pk, 2, 'Mage', 'Fire', 5)
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]['dps'], 304)
        self.assertEqual(rows[-1]['dps'], 205)
        qs = SpecDungeonRanking.objects.filter(season_id=self.season.pk)
        for name, code, level in [('NoLog', '', 30), ('WithLog', 'Report', 10)]:
            SpecDungeonRanking.objects.create(season_id=self.season.pk, dungeon_id=1,
                class_name='Mage', spec_name='Fire', character_name=name, report_code=code,
                fight_id=1, keystone_level=level, dps=100)
        self.assertEqual([row['character_name'] for row in _select_dungeon_sample_records(qs)], ['WithLog'])

    def test_freeze_collects_raid_logs_and_does_not_reselect_after_resume(self):
        cycle = WclCollectionCycle.objects.create(season=self.season, owner='测试', slot=timezone.now(),
            completed_scopes=['dungeon:1', 'raid:5:2', 'raid:4:2'])
        freeze_samples(cycle, self.season, [('Mage', 'Fire')])
        self.assertEqual(cycle.selected_samples['raid:Mage/Fire'], {'5:2': [self.row.pk], '4:2': []})
        self.assertEqual([row['report_code'] for row in frozen_combatant_records(cycle)], ['Report'])
        self.assertEqual(frozen_season(cycle, self.season).mplus_encounters, self.season.mplus_encounters)
        selected = cycle.selected_samples['raid:Mage/Fire']['5:2']
        self.row.pk = None
        self.row.character_name = 'New'
        self.row.dps = 900
        self.row.save()
        result, _, _ = raid_stats(*self.args(), selected_ids=selected, source_cutoff=timezone.now())
        self.assertEqual(result['sample_size'], 1)
        self.assertEqual(result['dps']['max'], 100)

    def test_absent_optional_fields_have_no_empty_sections(self):
        result = raid_stats(*self.args())[0]
        for template, key in [('raid_stats', 'boss_detail'), ('dungeon_stats', 'dungeon_detail')]:
            html = render_to_string(f'portal/spec_detail/{template}.html',
                {key: result, 'class_name': 'Mage', 'spec_name': 'Fire'})
            self.assertNotIn('<h4>种族分布</h4>', html)
            self.assertNotIn('<h4>日志属性绿字区间</h4>', html)
            self.assertIn('日志装备', html)

    def test_old_mixed_projection_is_blocked_but_new_publication_is_readable(self):
        from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor
        with TemporaryDirectory() as directory:
            path = Path(directory) / str(self.season.pk) / 'Mage' / 'Fire' / 'raid.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'zone_groups': [{'bosses': [{'race_distribution': ['Orc']}]}]}))
            self.assertEqual(read_log_projection(path), {'_stats_refresh_required': True})
            SpecDetailAggregationMonitor._aggregate_raid(self.season, 'Mage', 'Fire', str(path.parent))
            payload = read_log_projection(path, 'raid-5-2')
            self.assertEqual(payload['_data_contract'], 'wcl-log-v2')
            boss = payload['zone_groups'][0]['bosses'][0]
            self.assertEqual(boss['sample_size'], 1)
            self.assertNotIn('race_distribution', boss)
