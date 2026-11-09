"""层数收敛后的请求规模、历史订阅和发布兼容回归。"""
import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from botend.services import rio_statistics as rio, rio_statistics_views as views
from botend.services.rio_statistics_queries import pending_queries
from botend.services.rio_statistics_runs import import_feeds, inbox
from botend.tests import test_rio_statistics as fixtures
from botend.tests import test_rio_statistics_dimensions as dimension_fixtures


class RioLevelPolicyTests(SimpleTestCase):
    def setUp(self):
        fixtures.RioStatisticsTests.setUp(self)
        views.clear_cache()
        self.addCleanup(views.clear_cache)

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
        self.assertEqual(pairs, {(10, 12), (16, 99), (20, 99)})

    def test_retired_subscriptions_are_not_reintroduced(self):
        base = rio.coordinate('heatmaps', {'dungeon': 'den-of-nalorakk'})
        retired = dict(base, level='10-plus')
        old_custom = dict(base, level='custom', min_level=10, max_level=99)
        rows = {rio.key_for(c): {'coordinate': c, 'requested_at': rio._now()}
                for c in (base, retired, old_custom)}
        rio._write(self.directory / 'queries.json', {'queries': rows})
        self.assertEqual(pending_queries(self.meta), [base])
        self.assertEqual(len(rio._load(self.directory / 'queries.json', {})['queries']), 3)

    def test_old_published_ranges_are_hidden_before_and_after_incremental_rebuild(self):
        with patch.dict(rio.LEVELS, {'10-plus': (10, 99, '10层及以上')}):
            old = rio.coordinate('heatmaps', {'dungeon': 'den-of-nalorakk', 'level': '10-plus'})
            rio._publish(old, rio.normalize_heatmap(fixtures.GRID, [fixtures.FLOOR]))
            views.refresh_views()
            old_path = views.entry_path(old)
        catalog = json.loads(views.read_public('catalog', {}).raw)['data']
        self.assertEqual([item['value'] for item in catalog['levels']], ['10-12', '16-plus', '20-plus'])
        self.assertEqual(catalog['available'], [])
        self.assertIsNone(catalog['availability'])
        with self.assertRaises(ValueError):
            views.read_public('heatmaps', {'dungeon': 'den-of-nalorakk', 'level': '10-plus'})
        new = rio.coordinate('heatmaps', {'dungeon': 'den-of-nalorakk'})
        rio._publish(new, rio.normalize_heatmap(fixtures.GRID, [fixtures.FLOOR]))
        views.refresh_views(keys={rio.key_for(new)}, verify=False)
        publication, _ = views._publication()
        self.assertEqual(set(publication['entries']), {rio.key_for(new)})
        self.assertTrue(old_path.is_file())
        self.assertIn(rio.key_for(old), rio.store().index()['entries'])

    def test_retired_imports_do_not_keep_batch_partial(self):
        source = dimension_fixtures.RioDimensionTests.source(self)
        source['query'].update(minMythicLevel=2, maxMythicLevel=99)
        directory = inbox()
        directory.mkdir(parents=True, exist_ok=True)
        rio._write(directory / 'retired-ready.json', source)
        rio._write(directory / 'retired-pending.json', dict(source, status='generating'))
        result = import_feeds(self.meta)
        self.assertEqual(result, {'imported': 0, 'failed': 0, 'pending': 0, 'skipped': 2})
