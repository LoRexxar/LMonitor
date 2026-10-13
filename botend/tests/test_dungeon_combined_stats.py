from unittest.mock import patch

from bs4 import BeautifulSoup
from botend.services import spec_stats_service as stats_module
from django.test import TestCase, override_settings
from botend.models import SeasonMeta, SpecDungeonRanking, PlayerSpecTopPlayer
from botend.services.spec_stats_service import SpecStatsService


@override_settings(ALLOWED_HOSTS=['testserver'])
class DungeonCombinedStatsTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='test-current', season_name='Test',
            mplus_zone_id=1, raid_zone_id=2,
            mplus_encounters=[{'id': i, 'name': f'Dungeon {i}'} for i in range(1, 9)])

    def row(self, dungeon=1, name='A', dps=100, **extra):
        data = dict(season_id=self.season.id, dungeon_id=dungeon, dungeon_name=f'Dungeon {dungeon}',
            class_name='Warrior', spec_name='Arms', character_name=name, realm='Realm', region='eu',
            report_code=f'report-{dungeon}', fight_id=1, dps=dps, keystone_level=10, faction=0)
        data.update(extra)
        return SpecDungeonRanking.objects.create(**data)

    def test_raw_weighted_union_identity_missing_and_isolation(self):
        self.row(faction=1, gear_json=[{'id': 123, 'name': 'Raw WCL item', 'slot': 'head',
                                      'gems': [{'id': 456, 'name': 'Raw gem'}]}])
        self.row(faction=1)  # identical report/fight/actor: one observation
        self.row(dungeon=2, name='A', dps=300)
        self.row(dungeon=2, name='B', dps=300)  # same report/fight, different actor
        self.row(dungeon=2, name='C', dps=300)
        self.row(dungeon=3, name='A', dps=500, report_code='report-1', fight_id=2)
        self.row(dungeon=4, name='A', report_code='report-1')  # duplicate across boundaries
        self.row(dungeon=99, dps=9999)
        self.row(spec_name='Fury', dps=9999)
        self.row(class_name='Mage', dps=9999)
        self.row(season_id=self.season.id + 10, dps=9999)
        PlayerSpecTopPlayer.objects.create(season_id=self.season.id, class_name='Warrior',
            spec_name='Arms', character_name='Unrelated', realm='Realm', region='eu', score=9999)
        for season_id, spec in [(self.season.id + 10, 'Arms'), (self.season.id, 'Fury')]:
            PlayerSpecTopPlayer.objects.create(season_id=season_id, class_name='Warrior', spec_name=spec,
                character_name='A', realm='Realm', region='eu', gear_json=[{'id': 999, 'slot': 'head'}],
                race='Orc')
        result = SpecStatsService.get_dungeon_summary('Warrior', 'Arms', self.season.id)
        self.assertEqual(result['gem_popularity'][0]['id'], 456)
        self.assertEqual(result['gem_popularity'][0]['pct'], 20)
        self.assertEqual(result['race_distribution'], [
            {'race': 'unknown', 'race_cn': '未知', 'count': 5, 'pct': 100.0},
        ])
        self.assertNotIn(999, [item['id'] for items in result['gear_popularity'].values() for item in items])
        self.assertEqual(result['sample_size'], 5)
        self.assertEqual(result['dps']['avg'], 300)
        self.assertEqual(result['dps']['median'], 300)
        self.assertEqual(result['faction_distribution']['1']['pct'], 20)
        self.assertEqual(result['target_sample_size'], 800)
        self.assertEqual(result['missing_sample_size'], 795)
        self.assertEqual(len(result['dungeon_samples']), 8)
        self.assertEqual(result['dungeon_samples'][-1]['sample_size'], 0)
        self.assertEqual(result['source'], 'Warcraft Logs')
        self.assertNotIn('talent_build_popularity', result)
        self.assertNotIn('talent_build_popularity', result['field_sources'])
        for key in ('talent_usage', 'talent_popularity_tree',
                    'gear_popularity', 'gem_popularity', 'enchant_popularity', 'secondary_stats', 'race_distribution', 'top5'):
            self.assertIn(key, result)

    def test_summary_skips_build_computation_and_enrichment_only(self):
        self.row(gear_json=[{'id': 123, 'slot': 'head'}])
        with patch.object(stats_module, '_compute_talent_build_popularity',
                          wraps=stats_module._compute_talent_build_popularity) as builds, \
             patch.object(stats_module, '_merge_player_profile_fields',
                          wraps=stats_module._merge_player_profile_fields) as enrichment:
            summary = SpecStatsService.get_dungeon_summary('Warrior', 'Arms', self.season.id)
            builds.assert_not_called()
            enrichment.assert_not_called()
            single = SpecStatsService.get_dungeon_detail(1, 'Warrior', 'Arms', self.season.id)
            builds.assert_called_once()
            enrichment.assert_not_called()
        self.assertIn('talent_build_popularity', single)
        self.assertIn('talent_build_popularity', single['field_sources'])
        self.assertNotIn('talent_build_popularity', summary)
        self.assertNotIn('talent_build_popularity', summary['field_sources'])
        # Same real cohort: every unrelated statistic and source must be identical.
        excluded = {'dungeon_id', 'dungeon_name', 'talent_build_popularity', 'field_sources'}
        for key, value in single.items():
            if key not in excluded:
                self.assertEqual(summary[key], value, key)
        self.assertEqual(summary['field_sources'], {
            key: value for key, value in single['field_sources'].items()
            if key != 'talent_build_popularity'
        })

    def test_empty_summary_omits_build_source(self):
        result = SpecStatsService.get_dungeon_summary('Warrior', 'Arms', self.season.id)
        self.assertNotIn('talent_build_popularity', result)
        self.assertNotIn('talent_build_popularity', result['field_sources'])

    def test_each_dungeon_keeps_100_and_same_player_in_different_logs_counts(self):
        for dungeon in range(1, 9):
            SpecDungeonRanking.objects.bulk_create([
                SpecDungeonRanking(season_id=self.season.id, dungeon_id=dungeon, dungeon_name=str(dungeon),
                    class_name='Warrior', spec_name='Arms', character_name=f'P{i}', realm='Realm', region='eu',
                    report_code=f'R{dungeon}', fight_id=i + 1, dps=100, keystone_level=10)
                for i in range(101)])
        result = SpecStatsService.get_dungeon_summary('Warrior', 'Arms', self.season.id)
        self.assertEqual(result['sample_size'], 800)
        self.assertEqual([d['sample_size'] for d in result['dungeon_samples']], [100] * 8)
        self.assertEqual(SpecStatsService.get_dungeon_detail(1, 'Warrior', 'Arms', self.season.id)['sample_size'], 100)

    def test_selection_fetches_large_payloads_only_for_selected_ids(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from botend.services.spec_stats_service import _select_dungeon_sample_records

        for i in range(3):
            self.row(name=f'P{i}', dps=100 + i)
        qs = SpecDungeonRanking.objects.filter(season_id=self.season.id)
        with CaptureQueriesContext(connection) as queries:
            selected = _select_dungeon_sample_records(qs, max_samples=1)
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(queries), 2)
        self.assertNotIn('talents_json', queries[0]['sql'])
        self.assertNotIn('gear_json', queries[0]['sql'])
        self.assertIn('talents_json', queries[1]['sql'])
        self.assertIn(' IN ', queries[1]['sql'])

    def test_level_floor_is_scope_local_inclusive_and_keeps_selection_rules(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        high = self.row(name='High', dps=300, keystone_level=20)
        median = self.row(name='Median', dps=200, keystone_level=20)
        self.row(name='BelowMedian', dps=100, keystone_level=20)
        self.row(name='High', dps=300, keystone_level=20)  # duplicate log
        self.row(name='High', dps=900, keystone_level=19, fight_id=2)
        next_level = self.row(name='Next', dps=500, keystone_level=19)
        self.row(name='Low19', dps=100, keystone_level=19)
        floor_a = self.row(name='FloorA', keystone_level=18)
        floor_b = self.row(name='FloorB', keystone_level=18)
        self.row(name='Excluded17', dps=99999, keystone_level=17)
        gap_high = self.row(dungeon=2, name='Gap20', keystone_level=20)
        gap_floor = self.row(dungeon=2, name='Gap18', keystone_level=18)
        self.row(dungeon=2, name='Gap15', keystone_level=15)
        local_high = self.row(dungeon=3, name='Local12', keystone_level=12)
        local_floor = self.row(dungeon=3, name='Local10', keystone_level=10)
        self.row(dungeon=3, name='Local9', keystone_level=9)
        for scope in ({'spec_name': 'Fury'}, {'class_name': 'Mage'},
                      {'season_id': self.season.id + 10}, {'dungeon': 99}):
            self.row(name='OtherScope', keystone_level=99, **scope)

        expected = {1: [high.pk, median.pk, next_level.pk, floor_a.pk, floor_b.pk],
                    2: [gap_high.pk, gap_floor.pk], 3: [local_high.pk, local_floor.pk]}
        for dungeon, ids in expected.items():
            with self.subTest(dungeon=dungeon):
                qs = SpecDungeonRanking.objects.filter(season_id=self.season.id,
                    dungeon_id=dungeon, class_name='Warrior', spec_name='Arms')
                with CaptureQueriesContext(connection) as queries:
                    selected = stats_module._select_dungeon_sample_records(qs)
                self.assertEqual([row['id'] for row in selected], ids)
                self.assertEqual(len(queries), 2)
                self.assertNotIn('gear_json', queries[0]['sql'])
                self.assertIn(' IN ', queries[1]['sql'])
                # The identity-only backfill/freeze path uses the same cohort.
                slim = stats_module._select_dungeon_sample_records(qs, hydrate_fields=('report_code',))
                self.assertEqual([row['id'] for row in slim], ids)
                self.assertTrue(all(set(row) == {'id', 'report_code'} for row in slim))
                single = SpecStatsService._compute_dungeon_stats(
                    self.season.id, dungeon, str(dungeon), 'Warrior', 'Arms')
                self.assertEqual(single['sample_size'], len(ids))
        summary = SpecStatsService.get_dungeon_summary('Warrior', 'Arms', self.season.id)
        self.assertEqual(summary['sample_size'], 9)
        self.assertEqual(summary['missing_sample_size'], 791)
        self.assertEqual([row['sample_size'] for row in summary['dungeon_samples']],
                         [5, 2, 2, 0, 0, 0, 0, 0])

    def test_page_default_summary_and_all_single_links(self):
        import tempfile
        from django.core.management import call_command
        self.row()
        isolated = self.settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='stats-page-'))
        isolated.enable()
        self.addCleanup(isolated.disable)
        # This fixture predates collection cycles; only explicit legacy warmup
        # may publish it. The normal collection/publication barrier stays on.
        call_command('aggregate_spec_stats', class_name='Warrior', spec_name='Arms',
                     dungeon_only=True, allow_legacy=True)
        response = self.client.get('/portal/spec/Warrior/Arms/dungeons/')
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertIn('全部副本汇总', soup.get_text())
        self.assertNotIn('实际样本', soup.get_text())
        self.assertNotIn('各副本沿用单本筛选规则', soup.get_text())
        self.assertNotIn('去重后贡献', soup.get_text())
        self.assertIn('平均 DPS', soup.get_text())
        self.assertNotIn('天赋字符串使用率', soup.get_text())
        self.assertIsNone(soup.select_one('.talent-build-section'))
        self.assertNotContains(response, "querySelectorAll('.talent-build-copy[data-build-code]')")
        self.assertContains(response, 'function scaleTalentStage()')
        nav = soup.select_one('nav.spec-overview-links')
        self.assertEqual([a.get_text() for a in nav.select('a')],
                         ['人物榜', '大秘境详细统计', '团本详细统计'])
        self.assertIsNotNone(nav.select_one('a[href$="/raid/"]'))
        options = soup.select('[aria-label="副本统计选择"] a')
        self.assertEqual(len(options), 9)
        self.assertEqual(options[0].get('aria-current'), 'page')
        for i in range(1, 9):
            single = self.client.get(f'/portal/spec/Warrior/Arms/dungeons/?dungeon_id={i}')
            self.assertEqual(single.status_code, 200)
            self.assertEqual(single.context['dungeon_detail']['dungeon_id'], i)
            if i == 1:
                self.assertContains(single, '天赋字符串使用率')
                self.assertContains(single, "querySelectorAll('.talent-build-copy[data-build-code]')")
        self.assertEqual(self.client.get('/portal/spec/Warrior/Arms/dungeons/?dungeon_id=bad').status_code, 404)
