"""页面投影的读取隔离、发布恢复、周期变化和有界并发缓存验证。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import gzip
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from unittest.mock import Mock

from django.test import SimpleTestCase
from django.utils import timezone

from botend.services import rio_statistics as rio
from botend.services import rio_statistics_views as views
from botend.tests import test_rio_statistics as fixtures
from botend.portal.rio_statistics import RioStatisticsAPIView


class RioPublicViewsTests(SimpleTestCase):
    def setUp(self):
        fixtures.RioStatisticsTests.setUp(self)
        views.clear_cache()
        self.addCleanup(views.clear_cache)
        self.c = rio.coordinate('overview', {'region': 'cn'})
        rio._publish(self.c, {'value': 10})
        self.h = rio.coordinate('heatmaps', {'dungeon': 'den-of-nalorakk'})
        rio._publish(self.h, rio.normalize_heatmap(fixtures.GRID, [fixtures.FLOOR]))
        views.refresh_views()

    def get(self, module='overview', params=None, **headers):
        return RioStatisticsAPIView.as_view()(self.factory.get('/', params or {}, **headers), module=module)

    def data(self, **kwargs):
        return json.loads(self.get(**kwargs).content)['data']

    def test_cold_reads_use_only_prepared_files_and_live_scope(self):
        views.clear_cache()
        with patch.object(rio.store(), '_connect', side_effect=AssertionError('访客不得查询统计缓存库')), \
                patch.object(rio, 'localize_catalog', side_effect=AssertionError('访客不得重复中文投影')), \
                patch.object(rio, 'read_entry', side_effect=AssertionError('访客不得读取和解压原正文')), \
                patch('requests.Session.request', side_effect=AssertionError('访客不得回源')), \
                patch.object(Path, 'write_text', side_effect=AssertionError('访客不得写文件')):
            for module, params in [('catalog', {}), ('catalog', {'status': '1'}), ('overview', {}),
                                   ('heatmaps', {'dungeon': 'den-of-nalorakk'})]:
                response = self.get(module, params)
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.data()['data'], {'value': 10})

    def test_warm_reads_do_not_reparse_files_or_reserialize_payload(self):
        self.get(); self.get('catalog'); self.get('catalog', {'status': '1'})
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('热读不得重复打开正文')), \
                patch.object(views, 'encoded', side_effect=AssertionError('热读不得重新序列化正文')):
            for _ in range(10):
                self.assertEqual(self.get().status_code, 200)
                self.assertEqual(self.get('catalog').status_code, 200)
                self.assertEqual(self.get('catalog', {'status': '1'}).status_code, 200)

    def test_new_publication_invalidates_warm_response_and_status(self):
        before = self.data(module='catalog', params={'status': '1'})['revision']
        self.assertEqual(self.data()['data']['value'], 10)
        rio._publish(self.c, {'value': 20})
        views.refresh_views()
        self.assertEqual(self.data()['data']['value'], 20)
        after = self.data(module='catalog', params={'status': '1'})['revision']
        self.assertNotEqual(before, after)
        self.assertEqual(self.data(module='catalog')['revision'], after)

    def test_failure_and_age_are_not_hidden_by_prebuilt_files(self):
        before = self.get()
        rio._failure(self.c, '来源失败')
        views.refresh_views()
        result = self.data()
        self.assertEqual((result['snapshot']['state'], result['data']['value']), ('stale', 10))
        self.assertNotEqual(before['ETag'], self.get()['ETag'])
        rio._publish(self.c, {'value': 30}); views.refresh_views()
        before = self.get()
        with patch.object(timezone, 'now', return_value=timezone.now() + timedelta(hours=37)):
            result = self.get(HTTP_IF_NONE_MATCH=before['ETag'])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(json.loads(result.content)['data']['snapshot']['state'], 'stale')

    def test_live_database_scope_change_hides_old_files_immediately(self):
        self.assertEqual(self.data()['snapshot']['state'], 'ready')
        changed = dict(self.scope, token='b' * 64)
        with patch.object(rio, 'active_scope', return_value=changed):
            result = self.data()
            self.assertIsNone(result['data'])
            self.assertEqual(result['scope']['token'], changed['token'])
            self.assertEqual(self.data(module='catalog')['available'], [])

    def test_missing_files_never_trigger_request_time_rebuild(self):
        empty = Path(tempfile.mkdtemp(prefix='rio-empty-views-'))
        with self.settings(RIO_STATISTICS_ROOT=empty), \
                patch.object(rio, 'read_entry', side_effect=AssertionError('不得降级聚合')), \
                patch.object(views, 'refresh_views', side_effect=AssertionError('不得请求内重建')):
            self.assertEqual(self.data()['snapshot']['state'], 'pending')
            self.assertFalse((empty / 'views').exists())

    def test_corrupt_file_is_not_served_and_background_repairs_it(self):
        path = views.entry_path(self.c)
        record = json.loads(path.read_bytes())
        record['response']['data']['value'] = 999
        path.write_text(json.dumps(record), encoding='utf-8')
        self.assertEqual(self.data()['snapshot']['state'], 'pending')
        self.assertEqual(views.audit_views(self.scope['token'], deep=True)['state'], 'error')
        result = views.refresh_views()
        self.assertEqual(result['written'], 1)
        self.assertEqual(self.data()['data']['value'], 10)
        self.assertEqual(views.audit_views(self.scope['token'], deep=True)['state'], 'ok')

    def test_failed_build_keeps_previous_directory_and_payload(self):
        before = self.data(module='catalog')['revision']
        rio._publish(self.c, {'value': 20})
        with patch.object(views, '_write_view', side_effect=OSError('模拟展示磁盘写入失败')):
            with self.assertRaises(OSError): views.refresh_views()
        self.assertEqual(self.data(module='catalog')['revision'], before)
        self.assertEqual(self.data()['data']['value'], 10)
        views.refresh_views()
        self.assertEqual(self.data()['data']['value'], 20)

    def test_collector_does_not_complete_when_display_publication_fails(self):
        collector = rio.Collector(transport=Mock())
        with patch.object(views, 'refresh_views', side_effect=OSError('模拟展示发布失败')):
            result = collector.run(modules=['overview'], regions=['cn'])
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['views_error'], 'OSError')
        self.assertEqual(self.data()['data']['value'], 10)

    def test_rebuild_without_changes_does_not_unpack_or_rewrite(self):
        before = {str(p): p.read_bytes() for p in views.directory().rglob('*.json')}
        with patch.object(rio.store(), '_unpack', side_effect=AssertionError('未变化正文无需重新解压')):
            result = views.refresh_views()
        self.assertEqual(result['written'], 0)
        self.assertEqual({str(p): p.read_bytes() for p in views.directory().rglob('*.json')}, before)

    def test_week_reset_changes_status_and_does_not_reuse_previous_coordinate(self):
        self.meta['periods'] = [{'region': 'cn',
            'previous': {'period': 1086, 'start': '2026-10-21T23:00:00Z', 'end': '2026-10-28T23:00:00Z'},
            'current': {'period': 1087, 'start': '2026-10-28T23:00:00Z', 'end': '2026-11-04T23:00:00Z'},
            'next': {'period': 1088, 'start': '2026-11-04T23:00:00Z', 'end': '2026-11-11T23:00:00Z'}}]
        rio._write(self.directory / 'catalog.json', self.meta)
        params = {'dungeon': 'den-of-nalorakk', 'period': 'last-reset'}
        with patch.object(timezone, 'now', return_value=datetime.fromisoformat('2026-11-04T22:59:00+00:00')):
            c = rio.coordinate('heatmaps', params)
            rio._publish(c, rio.normalize_heatmap(fixtures.GRID, [fixtures.FLOOR]))
            views.refresh_views()
            before = self.data(module='catalog', params={'status': '1'})['revision']
            self.assertEqual(self.data(module='heatmaps', params=params)['snapshot']['state'], 'ready')
        with patch.object(timezone, 'now', return_value=datetime.fromisoformat('2026-11-04T23:01:00+00:00')):
            after = self.data(module='catalog', params={'status': '1'})['revision']
            self.assertNotEqual(before, after)
            self.assertEqual(self.data(module='catalog')['revision'], after)
            self.assertEqual(self.data(module='heatmaps', params=params)['snapshot']['state'], 'pending')

    def test_conditional_requests_and_gzip_variants(self):
        response = self.get(HTTP_ACCEPT_ENCODING='gzip')
        self.assertEqual(response['Content-Encoding'], 'gzip')
        self.assertEqual(json.loads(gzip.decompress(response.content))['data']['data'], {'value': 10})
        cached = self.get(HTTP_ACCEPT_ENCODING='gzip', HTTP_IF_NONE_MATCH='W/' + response['ETag'])
        self.assertEqual((cached.status_code, cached.content), (304, b''))
        identity = self.get(HTTP_ACCEPT_ENCODING='gzip;q=0.0', HTTP_IF_NONE_MATCH=response['ETag'])
        self.assertEqual(identity.status_code, 200)
        self.assertNotIn('Content-Encoding', identity)

    def test_cache_is_bounded_and_concurrent_readers_share_responses(self):
        expected = self.get().content
        with patch.object(rio.store(), '_connect', side_effect=AssertionError('并发请求不能查询统计库')):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.get().content, range(80)))
        self.assertTrue(all(value == expected for value in results))
        views.clear_cache()
        with patch.object(views, 'MAX_CACHE_BYTES', 1024):
            self.assertEqual(self.get().status_code, 200)
            self.assertLessEqual(views._cache_bytes, 1024)
