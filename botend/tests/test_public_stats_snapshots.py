"""统计页请求不得因缺文件、旧字段或错误文件而回退到数据库聚合。"""
import json
from decimal import Decimal
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, RequestFactory, override_settings
from botend.portal import spec_detail_views as views
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor, publish_stats


class PublicStatsSnapshotTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='public-stats-'))
        self.settings_override = override_settings(MEDIA_ROOT=self.root)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.directory = self.root / 'aggregated/1/Warrior/Arms'
        self.directory.mkdir(parents=True)

    def test_missing_corrupt_legacy_and_complete_files_never_compute(self):
        season = SimpleNamespace(id=1, mplus_encounters=[{'id': 2, 'name': '副本'}], raid_encounters=[{'id': 3}])
        factory = RequestFactory()
        with patch.object(views, '_base_context', side_effect=lambda *args: {'season': season}), \
                patch.object(views, 'render', side_effect=lambda request, template, ctx: ctx), \
                patch.object(views.SpecStatsService, 'get_dungeon_summary', side_effect=AssertionError('请求内重算')), \
                patch.object(views.SpecStatsService, 'get_dungeon_detail', side_effect=AssertionError('请求内重算')), \
                patch.object(views.SpecStatsService, 'get_raid_detail', side_effect=AssertionError('请求内重算')), \
                patch.object(views.SpecStatsService, 'get_raid_overview', side_effect=AssertionError('请求内重算')):
            for content in (None, '{', '{}', json.dumps({'summary': {'sample_size': 7},
                    'dungeons': [{'dungeon_id': 2, 'sample_size': 3}],
                    'zone_groups': [{'bosses': [{'boss_id': 3, 'sample_size': 8}]}]})):
                if content is not None:
                    for filename in ('dungeon.json', 'raid.json'):
                        (self.directory / filename).write_text(content, encoding='utf-8')
                for url, view in (('/', views.SpecDetailDungeonView), ('/?dungeon_id=2', views.SpecDetailDungeonView),
                                  ('/', views.SpecDetailRaidView), ('/?boss_id=3', views.SpecDetailRaidView)):
                    result = view().get(factory.get(url), 'Warrior', 'Arms')
                if content and 'sample_size' in content:
                    self.assertEqual(result['boss_detail']['sample_size'], 8)

    def test_generation_owns_display_refresh_and_both_raid_difficulties(self):
        season = SimpleNamespace(id=1, mplus_encounters=[{'id': 2, 'name': '副本'}],
                                 raid_encounters=[{'id': 3, 'name': '首领'}], raid_zones=[])
        with patch('botend.services.wow_item_display.refresh_aggregate_equipment', side_effect=lambda data, **kw: data) as equipment, \
                patch('botend.services.wow_talent_display.refresh_aggregate_talents', side_effect=lambda data, **kw: data) as talents, \
                patch.object(views.SpecStatsService, '_compute_dungeon_stats', return_value={'dungeon_id': 2}), \
                patch.object(views.SpecStatsService, 'get_dungeon_summary', return_value={'sample_size': 17, 'avg': Decimal('12.5')}), \
                patch.object(views.SpecStatsService, '_compute_raid_stats', side_effect=lambda *a, **kw: {'difficulty': kw['difficulty']}) as raid:
            SpecDetailAggregationMonitor._aggregate_dungeon(season, 'Warrior', 'Arms', self.directory)
            SpecDetailAggregationMonitor._aggregate_raid(season, 'Warrior', 'Arms', self.directory)
            self.assertEqual(equipment.call_count, 2)
            self.assertEqual(talents.call_count, 2)
            self.assertEqual([call.kwargs['difficulty'] for call in raid.call_args_list], [5, 4])
        with patch('botend.services.wow_item_display.refresh_aggregate_equipment', side_effect=AssertionError('请求刷新')):
            data = views._load_json(1, 'Warrior', 'Arms', 'dungeon.json')
            self.assertEqual(data['summary']['sample_size'], 17)
            self.assertEqual(data['summary']['avg'], 12.5)
        before = (self.directory / 'dungeon.json').read_bytes()
        with patch('botend.services.wow_item_display.refresh_aggregate_equipment', side_effect=RuntimeError('生成失败')):
            with self.assertRaises(RuntimeError):
                publish_stats(self.directory / 'dungeon.json', {}, 'Warrior', 'Arms')
        self.assertEqual((self.directory / 'dungeon.json').read_bytes(), before)
