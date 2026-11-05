"""真实底图身份校验、瓦片拼接与每日夜间长任务契约。"""
from copy import deepcopy
from datetime import datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from PIL import Image
from django.test import SimpleTestCase
from botend.plugin_sync import monitor_task_due_at, monitor_default_wait_time, NIGHT_LANE_TASKS
from botend.services.rio_statistics_maps import floor_metadata, stitch_map, script_json
from botend.controller.plugins.portal.RioStatisticsMonitor import RioStatisticsMonitor
from botend.tests.test_rio_statistics import FLOOR


class RioMapTests(SimpleTestCase):
    def inputs(self):
        row = {'id': 394, 'index': 2, 'facade': 0, 'name': 'dungeons.midnight.den_of_nalorakk.floors.the_foraging',
               'ingame_min_x': 6000, 'ingame_min_y': 4000, 'ingame_max_x': 4500, 'ingame_max_y': 5000}
        mapping = {'dungeon': {'challenge_mode_id': 586, 'key': 'den_of_nalorakk', 'expansion': {'key': 'midnight'}}}
        return row, mapping

    def html(self, row):
        return 'mapContextMappingVersionData, ' + json.dumps({'visibleFloors': [row]})

    def test_geometry_and_floor_identity_required_before_map_binding(self):
        row, mapping = self.inputs()
        floors = floor_metadata(self.html(row), mapping, 586, [FLOOR])
        self.assertEqual(floors['394']['name'], '觅食溪谷')
        self.assertEqual(floors['394']['floor_index'], 2)
        with self.assertRaises(ValueError): floor_metadata(self.html(row), mapping, 999, [FLOOR])
        row['ingame_min_x'] += 1
        with self.assertRaises(ValueError): floor_metadata(self.html(row), mapping, 586, [FLOOR])

    def test_facade_or_other_floor_is_not_overlaid_on_physical_grid(self):
        row, mapping = self.inputs()
        row['facade'] = 1
        with self.assertRaises(ValueError): floor_metadata(self.html(row), mapping, 586, [FLOOR])
        row['facade'], row['id'] = 0, 419
        with self.assertRaises(ValueError): floor_metadata(self.html(row), mapping, 586, [FLOOR])

    def test_tile_stitch_preserves_row_and_column_order(self):
        directory = Path(tempfile.mkdtemp(prefix='rio-map-tests-'))
        def tile(url):
            x, y = map(int, url.rsplit('/', 1)[1].split('.')[0].split('_'))
            buffer = BytesIO()
            Image.new('RGB', (384, 256), (x * 70, y * 70, 40)).save(buffer, format='PNG')
            return buffer.getvalue()
        source = Mock(side_effect=tile)
        destination = directory / 'map.webp'
        stitch_map({'tile_base': 'https://assets.keystone.guru/tiles_webp/midnight/den_of_nalorakk/2'}, source, destination)
        self.assertEqual(source.call_count, 16)
        with Image.open(destination) as image:
            self.assertEqual(image.size, (1536, 1024))
            self.assertLess(abs(image.getpixel((1400, 900))[0] - 210), 4)
            self.assertLess(abs(image.getpixel((1400, 900))[1] - 210), 4)
            self.assertLess(image.getpixel((100, 100))[0], 4)
        # 完整瓦片已落盘；重新拼接不能再下载，避免长任务中断后重复请求。
        stitch_map({'tile_base': 'https://assets.keystone.guru/tiles_webp/midnight/den_of_nalorakk/2'},
                   Mock(side_effect=AssertionError('缓存瓦片不应回源')), destination)

    def test_interrupted_tile_collection_resumes_from_last_valid_tile(self):
        directory = Path(tempfile.mkdtemp(prefix='rio-map-resume-'))
        buffer = BytesIO()
        Image.new('RGB', (384, 256), (50, 70, 90)).save(buffer, format='PNG')
        raw = buffer.getvalue()
        floor = {'tile_base': 'https://assets.keystone.guru/tiles_webp/midnight/den_of_nalorakk/2'}
        destination = directory / 'map.webp'
        with self.assertRaises(ConnectionError):
            stitch_map(floor, Mock(side_effect=[raw, raw, ConnectionError('连接中断')]), destination)
        self.assertFalse(destination.exists())
        resumed = Mock(return_value=raw)
        stitch_map(floor, resumed, destination)
        self.assertEqual(resumed.call_count, 14)
        self.assertTrue(resumed.call_args_list[0].args[0].endswith('/2/2_0.webp'))

    def test_source_script_is_parsed_without_executing_trailing_code(self):
        self.assertEqual(script_json('let source = {"dungeon": {}}; throw new Error();'), {'dungeon': {}})

    def test_central_chinese_names_override_fallback_on_every_catalog_read(self):
        from botend.services.rio_statistics_localization import localize_catalog, class_name, spec_name
        data = {'raids': [{'slug': 'test', 'name': '旧译名', 'source_name': 'Raid source'}],
                'seasons': [{'slug': 'season-mn-2', 'dungeons': [
                    {'slug': 'kings-rest', 'name': '诸王之眠', 'source_name': "Kings Rest", 'challenge_mode_id': 249}]}]}
        scope = {'rio_season': 'season-mn-2', 'season_name': '午夜第二赛季'}
        def names(_, candidates):
            self.assertIn('Raid source', candidates)
            self.assertIn('Kings Rest', candidates)
            return {'Raid source': '团本中央名称', 'Kings Rest': '副本中央名称'}
        with patch('botend.services.rio_statistics_localization.name_overrides', side_effect=names):
            for _ in range(2):
                localized = localize_catalog(data, scope)
                self.assertEqual(localized['raids'][0]['name'], '团本中央名称')
                self.assertEqual(localized['seasons'][0]['dungeons'][0]['name'], '副本中央名称')
        self.assertEqual(class_name('mage'), '法师')
        self.assertIn('火', spec_name('mage', 'fire'))

    def test_legacy_english_raid_names_remain_translatable_after_repeated_reads(self):
        from botend.services.rio_statistics_localization import localize_catalog
        data = {'raids': [{'name': 'The Venomous Abyss'}, {'name': 'MN Tier 1 (VS / DR / MQD)'}], 'seasons': []}
        with patch('botend.services.rio_statistics_localization.name_overrides', return_value={}):
            for _ in range(3):
                localize_catalog(data, {'rio_season': 'season-mn-2'})
                self.assertEqual(data['raids'][0]['name'], '烈毒之渊')
                self.assertEqual(data['raids'][0]['source_name'], 'The Venomous Abyss')
                self.assertEqual(data['raids'][1]['name'], '午夜第 1 赛季团本')


class RioNightScheduleTests(SimpleTestCase):
    def task(self, scanned=None):
        return SimpleNamespace(name='RioStatisticsMonitor', last_scan_time=scanned, wait_time=1, flag='', save=Mock())

    def test_only_night_slot_and_once_per_local_day(self):
        zone = ZoneInfo('Asia/Shanghai')
        night = datetime(2026, 11, 5, 1, 0, tzinfo=zone)
        task = self.task(night - timedelta(days=1))
        self.assertEqual(monitor_default_wait_time(task.name), 86400)
        self.assertIn(task.name, NIGHT_LANE_TASKS)
        self.assertIsNotNone(monitor_task_due_at(task, now=night))
        task.last_scan_time = night
        task.flag = json.dumps({'identity': '2026-11-05@test', 'state': 'complete'})
        with patch('botend.services.rio_statistics_scope.active_scope', return_value={'token': 'test'}):
            self.assertIsNone(monitor_task_due_at(task, now=night + timedelta(hours=1)))
        self.assertIsNone(monitor_task_due_at(task, now=night + timedelta(hours=10)))
        self.assertIsNotNone(monitor_task_due_at(task, now=night + timedelta(days=1)))

    def test_new_task_never_catches_up_during_daytime(self):
        now = datetime(2026, 11, 5, 10, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.assertIsNone(monitor_task_due_at(self.task(), now=now))

    def test_night_plugin_does_not_repeat_successful_same_day_scope(self):
        self.assertFalse(RioStatisticsMonitor.requires_browser)
        task = self.task()
        now = datetime(2026, 11, 5, 2, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
        result = {'state': 'complete', 'ready': 4, 'failed': 0, 'remaining': 0, 'maps_failed': 0}
        with patch('botend.controller.plugins.portal.RioStatisticsMonitor.active_scope', return_value={'token': 'test'}), \
                patch('botend.controller.plugins.portal.RioStatisticsMonitor.timezone.now', return_value=now), \
                patch('botend.controller.plugins.portal.RioStatisticsMonitor.run_nightly_collection', return_value=result) as collect:
            monitor = RioStatisticsMonitor(Mock(), task)
            self.assertTrue(monitor.scan())
            self.assertTrue(monitor.scan())
            collect.assert_called_once()

    def test_night_plugin_runs_again_when_same_day_scope_changes(self):
        task = self.task()
        now = datetime(2026, 11, 5, 2, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
        result = {'state': 'complete', 'ready': 1, 'failed': 0, 'remaining': 0, 'maps_failed': 0}
        with patch('botend.controller.plugins.portal.RioStatisticsMonitor.active_scope', return_value={'token': 'old'}) as scope, \
                patch('botend.controller.plugins.portal.RioStatisticsMonitor.timezone.now', return_value=now), \
                patch('botend.controller.plugins.portal.RioStatisticsMonitor.run_nightly_collection', return_value=result) as collect:
            monitor = RioStatisticsMonitor(Mock(), task)
            self.assertTrue(monitor.scan())
            scope.return_value = {'token': 'new'}
            self.assertTrue(monitor.scan())
            self.assertTrue(monitor.scan())
            self.assertEqual(collect.call_count, 2)
            self.assertEqual(json.loads(task.flag)['identity'], '2026-11-05@new')

    def test_registration_appends_without_shifting_existing_plugin_ids(self):
        import ast
        # 注册序号是数据库协议；不加载其他插件的 Linux 专用依赖。
        source = Path(__file__).resolve().parents[2] / 'LMonitor/config.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        registration = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == 'Monitor_Type_BaseObject_List' for t in n.targets))
        names = [n.id for n in registration.elts]
        self.assertEqual(names[-1], 'RioStatisticsMonitor')
        self.assertEqual(names[39], 'PlayerRaiderIOActivityMonitor')
