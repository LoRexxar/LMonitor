"""大目录读取隔离、增量发布和初始化就绪顺序回归。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import StringIO
import hashlib
import json
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import SimpleTestCase

from botend.services import rio_statistics as rio
from botend.services import rio_statistics_views as views
from botend.tests import test_rio_statistics as fixtures


class RioScaleTests(SimpleTestCase):
    def setUp(self):
        fixtures.RioStatisticsTests.setUp(self)
        views.clear_cache(); self.addCleanup(views.clear_cache)
        self.c = rio.coordinate('overview', {'region': 'cn'})
        self.h = rio.coordinate('heatmaps', {'dungeon': 'den-of-nalorakk'})
        rio._publish(self.c, {'value': 10})
        rio._publish(self.h, rio.normalize_heatmap(fixtures.GRID, [fixtures.FLOOR]))
        views.refresh_views()

    def read(self, module='overview', params=None):
        return json.loads(views.read_public(module, params or {}).raw)['data']

    def test_public_reads_never_open_work_manifest_even_on_cold_cache(self):
        original = Path.read_bytes
        accessed = []
        def read(path):
            accessed.append(path.name)
            if path.name.startswith('catalog-'):
                raise AssertionError('公开请求不能读取全量工作清单')
            return original(path)
        views.clear_cache()
        with patch.object(Path, 'read_bytes', read), \
                patch.object(rio.store(), '_connect', side_effect=AssertionError('不得查询统计库')):
            self.assertEqual(self.read()['data']['value'], 10)
            self.assertEqual(self.read('catalog')['availability']['total'], 2)
        self.assertTrue(any(name.startswith('reader-') for name in accessed))
        self.assertTrue(any(name.endswith('.json') for name in accessed))

    def test_incremental_update_only_reads_selected_source(self):
        rio._publish(self.c, {'value': 20})
        original = rio.store().read
        def source(coordinate):
            self.assertEqual(coordinate, self.c)
            return original(coordinate)
        with patch.object(rio.store(), 'read', side_effect=source), \
                patch.object(rio.store(), 'projection_entries', wraps=rio.store().projection_entries) as select:
            result = views.refresh_views(keys={rio.key_for(self.c)}, verify=False)
        self.assertTrue(result['incremental'])
        self.assertEqual(result['written'], 1)
        self.assertEqual(select.call_args.kwargs['keys'], {rio.key_for(self.c)})
        self.assertEqual(self.read()['data']['value'], 20)
        self.assertEqual(self.read('heatmaps', {'dungeon': 'den-of-nalorakk'})['data']['num_runs'], 10)

    def test_metadata_date_change_does_not_rebuild_historical_bodies(self):
        paths = [views.entry_path(self.c), views.entry_path(self.h)]
        before = {p: p.read_bytes() for p in paths}
        meta = deepcopy(self.meta); meta['updated_at'] = '2026-11-07T00:00:00+00:00'
        rio._write(self.directory / 'catalog.json', meta)
        with patch.object(rio, 'read_entry', side_effect=AssertionError('日期变化不能重建正文')):
            result = views.refresh_views(keys=set(), verify=False)
        self.assertEqual(result['written'], 0)
        self.assertEqual({p: p.read_bytes() for p in paths}, before)

    def test_old_format_upgrades_by_reusing_committed_bodies(self):
        context = hashlib.sha256(views.encoded(self.meta)).hexdigest()
        entries = {}; files = {}; available = []
        for key, entry in rio.store().projection_entries(self.scope['token']).items():
            c = entry['coordinate']; marker = hashlib.sha256(views.encoded([context, entry])).hexdigest()
            entries[key] = marker; files[key] = 'a'
            response = rio.read_entry(c, meta=self.meta); response['scope'] = rio.public_scope(self.scope)
            views._write_view(views.directory() / 'entries' / f'{key}-a.json', {
                'schema': 2, 'coordinate': c, 'marker': marker, 'response': response, 'retained': False})
            available.append({'coordinate': c, 'state': 'ready'})
        views._write_view(views.directory() / 'catalog-a.json', {'schema': 2, 'revision': 'legacy',
            'scope': self.scope['token'], 'meta': self.meta, 'entries': entries, 'files': files, 'available': available})
        views._write_view(views.directory() / 'current.json', {'schema': 2, 'revision': 'legacy',
            'scope': self.scope['token'], 'slot': 'a', 'period_boundaries': []})
        with patch.object(rio, 'read_entry', side_effect=AssertionError('升级目录不得重解压所有正文')):
            result = views.refresh_views(keys=set(), verify=False)
        self.assertEqual(result['written'], 0)
        self.assertEqual(self.read()['data']['value'], 10)

    def test_one_slow_cold_key_does_not_lock_other_public_requests(self):
        entered = Event(); release = Event()
        def blocked():
            entered.set(); release.wait(5); return b'value'
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(views._remember, ('slow-cold-file',), blocked, len)
            self.assertTrue(entered.wait(2))
            try:
                fast = pool.submit(self.read, 'catalog', {'status': '1'})
                self.assertEqual(fast.result(timeout=2)['scope_state'], 'ready')
            finally:
                release.set()
            slow.result(timeout=2)

    def test_index_corruption_is_detected_and_full_rebuild_repairs_it(self):
        catalog, _ = views._publication()
        key = rio.key_for(self.c)
        reference = catalog['indexes'][key[:2]]
        path = views.directory() / 'indexes' / f"{key[:2]}-{reference['slot']}.json"
        path.write_text('{}', encoding='utf-8')
        self.assertEqual(self.read()['snapshot']['state'], 'pending')
        self.assertEqual(views.audit_views(self.scope['token'], deep=True)['state'], 'error')
        views.refresh_views()
        self.assertEqual(self.read()['data']['value'], 10)
        self.assertEqual(views.audit_views(self.scope['token'], deep=True)['state'], 'ok')

    def test_layout_upgrade_keeps_legacy_reader_fields_usable(self):
        pointer = json.loads((views.directory() / 'current.json').read_bytes())
        self.assertEqual((pointer['schema'], pointer['layout']), (2, 3))
        catalog = json.loads((views.directory() / f"catalog-{pointer['slot']}.json").read_bytes())
        self.assertEqual(catalog['schema'], 2)
        key = rio.key_for(self.c)
        body = json.loads((views.directory() / 'entries' / f"{key}-{catalog['files'][key]}.json").read_bytes())
        self.assertEqual(body['schema'], 2)
        self.assertEqual(body['marker'], catalog['entries'][key])
        self.assertEqual(body['response']['data']['value'], 10)

    def test_first_success_is_public_before_remaining_collection_finishes(self):
        collector = rio.Collector(force=True, transport=Mock())
        def metadata():
            collector.scope = self.scope
            return self.meta
        observed = []
        fetched = []
        def fetch(c, meta):
            if fetched:
                region = fetched[0]
                observed.append(self.read(params={'region': region})['data']['value'])
            fetched.append(c['region'])
            return {'value': 20 if c['region'] == 'cn' else 30}, None
        with patch.object(collector, 'metadata', side_effect=metadata), patch.object(collector, 'fetch', side_effect=fetch):
            result = collector.run(modules=['overview'], regions=['cn', 'world'])
        self.assertEqual(observed, [20 if fetched[0] == 'cn' else 30])
        self.assertEqual(result['ready'], 2)

    def test_maps_are_prepared_before_bulk_grids_when_budget_allows(self):
        collector = rio.Collector(max_requests=100, transport=Mock(return_value=Mock(
            status_code=200, headers={}, json=Mock(return_value=fixtures.GRID))))
        order = []
        original = collector.fetch
        def fetch(c, meta):
            order.append('grid'); return original(c, meta)
        with patch.object(collector, 'fetch', side_effect=fetch), \
                patch('botend.services.rio_statistics_maps.collect_maps', side_effect=lambda *a, **kw: order.append('maps')) as maps:
            collector.run(modules=['heatmaps'], regions=['cn'], levels=['10-plus'], periods=['season'])
        self.assertEqual(order[0], 'maps')
        maps.assert_called_once()

    def test_light_progress_does_not_scan_bodies_or_files(self):
        rio.store().cycle({'state': 'running', 'stage': 'snapshots', 'ready': 10, 'requests': 12})
        output = StringIO()
        with patch.object(rio.store(), 'audit', side_effect=AssertionError('不得扫描全库')), \
                patch.object(Path, 'rglob', side_effect=AssertionError('不得遍历文件目录')):
            call_command('check_rio_statistics', progress_only=True, stdout=output)
        self.assertEqual(json.loads(output.getvalue())['details']['ready'], 10)

    def test_map_backoff_does_not_mark_unrequested_grid_as_failed(self):
        transport = Mock()
        collector = rio.Collector(max_requests=100, transport=transport)
        def limited(*args, **kwargs):
            collector.stopped = True
            raise rio.RateLimited('地图来源限流')
        with patch('botend.services.rio_statistics_maps.collect_maps', side_effect=limited):
            result = collector.run(modules=['heatmaps'], regions=['cn'], levels=['10-plus'], periods=['season'])
        transport.assert_not_called()
        self.assertEqual((result['state'], result['failed']), ('backoff', 0))
        self.assertGreater(result['remaining'], 0)
