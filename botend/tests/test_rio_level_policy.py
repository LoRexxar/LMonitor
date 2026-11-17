"""免费预设层数、历史订阅及发布兼容回归。"""
import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from botend.services import rio_statistics as rio, rio_statistics_views as views
from botend.services.rio_statistics_queries import pending_queries
from botend.services.rio_statistics_runs import import_feeds, inbox, normalize_runs, RetiredLevel
from botend.tests import test_rio_statistics as fixtures
from botend.tests import test_rio_statistics_dimensions as dimension_fixtures


class RioLevelPolicyTests(SimpleTestCase):
    def setUp(self):
        fixtures.RioStatisticsTests.setUp(self)
        views.clear_cache()
        self.addCleanup(views.clear_cache)

    def test_shared_free_presets_and_defaults_match_for_both_modules(self):
        self.assertEqual(rio.LEVELS, {'10-plus': (10, 99, '10 层及以上'),
                                    '15-plus': (15, 99, '15 层及以上'),
                                    '20-plus': (20, 99, '20 层及以上')})
        self.assertEqual(rio.DEFAULT_LEVELS, tuple(rio.LEVELS))
        for module in ('heatmaps', 'runs'):
            with self.subTest(module=module):
                self.assertEqual(rio.coordinate(module, {'dungeon': 'den-of-nalorakk'} if module == 'heatmaps' else {})['level'], '10-plus')

    def test_full_heatmap_plan_has_48_coordinates_per_dungeon(self):
        self.meta['periods'].append({'region': 'us', 'previous': {'period': 1087}})
        rio._write(self.directory / 'catalog.json', self.meta)
        transport = Mock(return_value=Mock(status_code=200, headers={}, json=Mock(return_value=fixtures.GRID)))
        with patch.object(rio, '_publish'), patch('botend.services.rio_statistics_maps.collect_maps'):
            result = rio.Collector(transport=transport).run(modules=['heatmaps'])
        self.assertEqual(result['ready'], 48)
        self.assertEqual(transport.call_count, 48)
        pairs = {(call.kwargs['params']['minMythicLevel'], call.kwargs['params']['maxMythicLevel'])
                 for call in transport.call_args_list}
        self.assertEqual(pairs, {(10, 99), (15, 99), (20, 99)})
        self.assertEqual((transport.call_args_list[0].kwargs['params']['minMythicLevel'],
                          transport.call_args_list[0].kwargs['params']['maxMythicLevel']), (10, 99))

    def test_retired_subscriptions_are_not_reintroduced(self):
        base = rio.coordinate('heatmaps', {'dungeon': 'den-of-nalorakk', 'level': '20-plus'})
        retired = [dict(base, level=level) for level in ('10-12', '16-plus')]
        old_custom = dict(base, level='custom', min_level=10, max_level=99)
        rows = {rio.key_for(c): {'coordinate': c, 'requested_at': rio._now()}
                for c in (base, *retired, old_custom)}
        rio._write(self.directory / 'queries.json', {'queries': rows})
        self.assertEqual(pending_queries(self.meta), [base])
        self.assertEqual(len(rio._load(self.directory / 'queries.json', {})['queries']), 4)

    def test_retired_links_are_rejected_for_both_modules_without_aliasing(self):
        for module in ('heatmaps', 'runs'):
            for level in ('10-12', '16-plus'):
                params = {'level': level}
                if module == 'heatmaps':
                    params['dungeon'] = 'den-of-nalorakk'
                with self.subTest(module=module, level=level), self.assertRaisesRegex(ValueError, '已停用'):
                    views.read_public(module, params)

    def test_old_published_ranges_are_hidden_before_and_after_incremental_rebuild(self):
        historical = {'10-12': (10, 12, '10–12 层'), '16-plus': (16, 99, '16 层及以上'),
                      '20-plus': (20, 99, '20 层及以上')}
        old = []
        with patch.dict(rio.LEVELS, historical, clear=True):
            for level in ('10-12', '16-plus'):
                c = rio.coordinate('heatmaps', {'dungeon': 'den-of-nalorakk', 'level': level})
                rio._publish(c, rio.normalize_heatmap(fixtures.GRID, [fixtures.FLOOR]))
                old.append(c)
            views.refresh_views()
            old_paths = [views.entry_path(c) for c in old]
        catalog = json.loads(views.read_public('catalog', {}).raw)['data']
        self.assertEqual([item['value'] for item in catalog['levels']], ['10-plus', '15-plus', '20-plus'])
        self.assertEqual(catalog['available'], [])
        self.assertIsNone(catalog['availability'])
        new = rio.coordinate('heatmaps', {'dungeon': 'den-of-nalorakk'})
        self.assertEqual(rio.read_entry(new)['snapshot']['state'], 'pending', 'old ranges cannot populate a new identity')
        rio._publish(new, rio.normalize_heatmap(fixtures.GRID, [fixtures.FLOOR]))
        views.refresh_views(keys={rio.key_for(new)}, verify=False)
        publication, _ = views._publication()
        self.assertEqual(set(publication['entries']), {rio.key_for(new)})
        self.assertTrue(all(path.is_file() for path in old_paths))
        self.assertTrue(all(rio.key_for(c) in rio.store().index()['entries'] for c in old))

    def test_unchanged_10_and_20_plus_cache_identity_and_real_clock_are_reused(self):
        payload = rio.normalize_heatmap(fixtures.GRID, [fixtures.FLOOR])
        coordinates = [rio.coordinate('heatmaps', {'dungeon': 'den-of-nalorakk', 'level': level})
                       for level in ('10-plus', '20-plus')]
        for c in coordinates:
            rio._publish(c, payload)
        views.refresh_views()
        before = {rio.key_for(c): rio.store().read(c) for c in coordinates}
        transport = Mock(side_effect=AssertionError('valid existing ranges must not be fetched again'))
        with patch('botend.services.rio_statistics_maps.collect_maps'):
            result = rio.Collector(transport=transport).run(modules=['heatmaps'], regions=['cn'],
                levels=['10-plus', '20-plus'], periods=['season'],
                queries=[{'dungeon': 'den-of-nalorakk', 'level': c['level']} for c in coordinates],
                only_queries=True)
        self.assertEqual(result['requests'], 0)
        for c in coordinates:
            key = rio.key_for(c)
            self.assertEqual(rio.store().read(c), before[key])
            params = {k: v for k, v in c.items() if k not in ('module', 'scope', 'min_period', 'max_period')}
            current = json.loads(views.read_public('heatmaps', params).raw)['data']
            self.assertEqual(current['snapshot']['state'], 'ready')
            self.assertEqual(current['snapshot']['fetched_at'], before[key][0]['fetched_at'])
            self.assertEqual(current['coordinate'], c)
            with patch.object(rio.timezone, 'now', return_value=rio._stamp(before[key][0]['fetched_at']) + timedelta(hours=40)):
                stale = rio.read_entry(c)
            self.assertEqual(stale['snapshot']['state'], 'stale')
            self.assertEqual(stale['snapshot']['fetched_at'], before[key][0]['fetched_at'])
        transport.assert_not_called()

    def test_retired_sources_and_imports_are_skipped_not_remapped(self):
        directory = inbox()
        directory.mkdir(parents=True, exist_ok=True)
        for low, high in ((10, 12), (16, 99)):
            for status in ('ready', 'generating'):
                source = dimension_fixtures.RioDimensionTests.source(self)
                source['query'].update(minMythicLevel=low, maxMythicLevel=high)
                source['status'] = status
                with self.subTest(low=low, high=high, status=status), self.assertRaises(RetiredLevel):
                    normalize_runs(source, self.meta)
                rio._write(directory / f'retired-{low}-{status}.json', source)
        result = import_feeds(self.meta)
        self.assertEqual(result, {'imported': 0, 'failed': 0, 'pending': 0, 'skipped': 4})
        self.assertEqual(len(list(directory.glob('*.json'))), 4)
        self.assertFalse(rio.store().index()['entries'])
