"""每日自动投影、批次原子发布、故障保留和真实数据库范围门禁回归。"""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase, RequestFactory
from django.utils import timezone

from botend.portal.rio_statistics import RioStatisticsAPIView
from botend.services import rio_statistics as rio
from botend.services import rio_statistics_views as views
from botend.services.rio_statistics_scope import ScopeUnavailable, active_scope
from botend.services.rio_statistics_storage import StorageError
from botend.tests import test_rio_statistics as fixtures


class RioPublicationTests(SimpleTestCase):
    def setUp(self):
        fixtures.RioStatisticsTests.setUp(self)
        views.clear_cache()
        self.addCleanup(views.clear_cache)
        self.c = rio.coordinate('overview', {'region': 'cn'})
        self.h = rio.coordinate('heatmaps', {'dungeon': 'den-of-nalorakk'})
        self.publish(10)
        views.refresh_views()

    def publish(self, value):
        rio._publish(self.c, {'value': value})
        payload = rio.normalize_heatmap(fixtures.GRID, [fixtures.FLOOR])
        payload['num_runs'] = value
        rio._publish(self.h, payload)

    def read(self, module='overview', params=None):
        return json.loads(views.read_public(module, params or {}).raw)['data']

    def active_hashes(self):
        pointer = json.loads((views.directory() / 'current.json').read_bytes())
        catalog = views.directory() / f"catalog-{pointer['slot']}.json"
        paths = [views.directory() / 'current.json', catalog, views.entry_path(self.c), views.entry_path(self.h)]
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}

    def assert_old_values(self):
        views.clear_cache()
        self.assertEqual(self.read()['data']['value'], 10)
        self.assertEqual(self.read('heatmaps', {'dungeon': 'den-of-nalorakk'})['data']['num_runs'], 10)
        self.assertEqual(self.read('catalog')['revision'], self.read('catalog', {'status': '1'})['revision'])

    def test_failure_after_partial_body_or_catalog_or_commit_preserves_whole_previous_publication(self):
        before = self.active_hashes()
        self.publish(20)
        write = views._write_view
        for stage in ('entries', 'catalog', 'commit'):
            counter = 0
            def fail(path, value):
                nonlocal counter
                if path.parent.name == 'entries':
                    counter += 1
                if ((stage == 'entries' and path.parent.name == 'entries' and counter == 2)
                        or (stage == 'catalog' and path.name.startswith('catalog-'))
                        or (stage == 'commit' and path.name == 'current.json')):
                    raise OSError('模拟批次中断')
                write(path, value)
            with self.subTest(stage=stage), patch.object(views, '_write_view', side_effect=fail):
                with self.assertRaises(OSError): views.refresh_views()
            self.assertEqual(self.active_hashes(), before)
            self.assert_old_values()
        views.refresh_views()
        self.assertEqual(self.read()['data']['value'], 20)
        self.assertEqual(self.read('heatmaps', {'dungeon': 'den-of-nalorakk'})['data']['num_runs'], 20)

    def test_real_process_exit_during_staging_keeps_previous_publication_and_can_resume(self):
        before = self.active_hashes()
        self.publish(20)
        code = '''import os,sys,json
os.environ['LMONITOR_USE_SQLITE']='1'
os.environ['DJANGO_SETTINGS_MODULE']='LMonitor.settings'
from django.conf import settings
settings.RIO_STATISTICS_ROOT=sys.argv[1]
import django;django.setup()
from botend.services import rio_statistics as rio
from botend.services import rio_statistics_views as views
rio.active_scope=lambda:json.loads(sys.argv[2])
rio.localize_catalog=lambda data,scope:data
write=views._write_view
def stop(path,value):
    write(path,value)
    if path.parent.name=='entries':os._exit(7)
views._write_view=stop
views.refresh_views()
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.directory), json.dumps(self.scope)],
                                capture_output=True, timeout=25)
        self.assertEqual(result.returncode, 7, result.stderr.decode(errors='replace'))
        self.assertEqual(self.active_hashes(), before)
        self.assert_old_values()
        views.refresh_views()
        self.assertEqual(self.read()['data']['value'], 20)

    def test_scope_change_during_staging_never_commits_old_range(self):
        before = self.active_hashes()
        self.publish(20)
        current = [self.scope]
        write = views._write_view
        def change(path, value):
            write(path, value)
            if path.parent.name == 'entries':
                current[0] = dict(self.scope, token='b' * 64, game_build='12.2.0.80000')
        with patch.object(rio, 'active_scope', side_effect=lambda: current[0]), \
                patch.object(views, '_write_view', side_effect=change):
            with self.assertRaises(ScopeUnavailable): views.refresh_views()
            self.assertEqual(self.read()['snapshot']['state'], 'pending')
        self.assertEqual(self.active_hashes(), before)
        self.assert_old_values()

    def test_interruption_after_commit_only_exposes_complete_new_publication(self):
        self.publish(20)
        write = views._write_view
        def stop(path, value):
            write(path, value)
            if path.name == 'current.json':
                raise SystemExit('提交后中断')
        with patch.object(views, '_write_view', side_effect=stop):
            with self.assertRaises(SystemExit): views.refresh_views()
        views.clear_cache()
        self.assertEqual(self.read()['data']['value'], 20)
        self.assertEqual(self.read('heatmaps', {'dungeon': 'den-of-nalorakk'})['data']['num_runs'], 20)
        self.assertEqual(views.audit_views(self.scope['token'], deep=True)['state'], 'ok')

    def test_unexpected_empty_source_cannot_clear_existing_publication(self):
        before = self.active_hashes()
        with patch.object(rio.store(), 'projection_entries', return_value={}):
            with self.assertRaises(StorageError): views.refresh_views()
        self.assertEqual(self.active_hashes(), before)
        self.assert_old_values()

    def test_metadata_failure_before_projection_keeps_current_files(self):
        before = self.active_hashes()
        collector = rio.Collector(transport=Mock())
        with patch.object(collector, 'metadata', side_effect=ConnectionError('来源元数据失败')):
            with self.assertRaises(ConnectionError): collector.run(maintain=True)
        self.assertEqual(self.active_hashes(), before)
        self.assert_old_values()

    def test_corrupt_statistic_preserves_verified_display_until_source_is_repaired(self):
        with rio.store().connection() as db:
            db.execute('UPDATE snapshots SET payload=? WHERE key=?', (b'broken', rio.key_for(self.c)))
        rio._failure(self.c, '正文损坏，等待恢复')
        result = views.refresh_views()
        self.assertEqual((result['state'], result['retained']), ('partial', 1))
        self.assertEqual((self.read()['data']['value'], self.read()['snapshot']['state']), (10, 'stale'))
        self.assertEqual(views.refresh_views()['state'], 'partial')
        rio._publish(self.c, {'value': 30})
        self.assertEqual(views.refresh_views()['state'], 'complete')
        self.assertEqual((self.read()['data']['value'], self.read()['snapshot']['state']), (30, 'ready'))

    def test_repeated_updates_use_two_slots_and_slow_reader_cannot_use_uncommitted_slot(self):
        original, _ = views._publication()
        self.publish(20); views.refresh_views()
        self.publish(30); views.refresh_views()
        load = views._publication
        calls = 0
        def stale_once(**kwargs):
            nonlocal calls
            calls += 1
            return (original, ('旧发布',)) if calls == 1 else load(**kwargs)
        with patch.object(views, '_publication', side_effect=stale_once):
            self.assertEqual(self.read()['data']['value'], 30)
        self.assertEqual(len(list((views.directory() / 'entries').glob('*.json'))), 4)
        self.assertEqual(len(list(views.directory().glob('catalog-*.json'))), 2)

    def test_next_day_collector_automatically_refreshes_display_and_failed_collection_keeps_values(self):
        # 使用一个总览组合走真实采集、事务发布、展示投影和夜间维护链，固定上游响应与时钟。
        day = datetime(2026, 11, 5, 2, tzinfo=ZoneInfo('Asia/Shanghai'))
        def metadata(collector):
            collector.scope = self.scope
            meta = deepcopy(self.meta)
            meta['updated_at'] = rio._now()
            rio._write(self.directory / 'catalog.json', meta)
            return meta
        def run(value, now, fail=False):
            collector = rio.Collector(transport=Mock())
            fetch = Mock(side_effect=ValueError('模拟来源失败')) if fail else Mock(return_value=({'value': value}, None))
            with patch.object(timezone, 'now', return_value=now), \
                    patch.object(rio.Collector, 'metadata', metadata), patch.object(collector, 'fetch', fetch):
                result = collector.run(modules=['overview'], regions=['cn'], maintain=True)
            return result
        # 首次固定到昨天，保证这三次调用均跨过下一北京时间午夜。
        with patch.object(timezone, 'now', return_value=day - timedelta(days=1)):
            rio._publish(self.c, {'value': 10})
            views.refresh_views()
        first = run(20, day)
        self.assertEqual(first['state'], 'complete')
        revision = self.read('catalog', {'status': '1'})['revision']
        self.assertEqual(self.read()['data']['value'], 20)
        second = run(30, day + timedelta(days=1))
        self.assertEqual(second['state'], 'complete')
        self.assertEqual(self.read()['data']['value'], 30)
        self.assertNotEqual(self.read('catalog', {'status': '1'})['revision'], revision)
        fetched = self.read()['snapshot']['fetched_at']
        third = run(40, day + timedelta(days=2), fail=True)
        self.assertEqual(third['state'], 'partial')
        self.assertEqual((self.read()['data']['value'], self.read()['snapshot']['fetched_at']), (30, fetched))
        self.assertEqual(self.read()['snapshot']['state'], 'stale')


class RioRealDatabasePublicationTests(TestCase):
    def setUp(self):
        from botend.models import SeasonMeta, WowTalentVersion
        SeasonMeta.objects.update(is_active=False)
        WowTalentVersion.objects.update(is_active=False)
        self.season = SeasonMeta.objects.create(season_key='mn-s2-publication', season_name='午夜第二赛季',
            rio_season='season-mn-2', game_build='12.1.5.70000', is_active=True, mplus_zone_id=1, raid_zone_id=2)
        self.version = WowTalentVersion.objects.create(key='retail-publication', branch='retail', is_active=True,
            current_build='12.1.5.70000')
        self.directory = Path(tempfile.mkdtemp(prefix='rio-real-publication-'))
        config = self.settings(RIO_STATISTICS_ROOT=self.directory)
        config.enable(); self.addCleanup(config.disable)
        names = patch.object(rio, 'localize_catalog', side_effect=lambda data, scope: data)
        names.start(); self.addCleanup(names.stop)
        views.clear_cache(); self.addCleanup(views.clear_cache)
        self.factory = RequestFactory()
        self.scope = active_scope()
        self.prepare(10)

    def prepare(self, value):
        scope = active_scope()
        meta = {'schema': 1, 'updated_at': rio._now(), 'scope': rio.public_scope(scope),
                'current_season': scope['rio_season'], 'seasons': [{'slug': scope['rio_season'], 'name': scope['season_name'],
                'dungeons': []}], 'periods': [], 'raids': [], 'floors': {}, 'maps': {}}
        rio._write(self.directory / 'catalog.json', meta)
        rio._publish(rio.coordinate('overview', {'region': 'cn'}, meta=meta), {'value': value})
        views.refresh_views()

    def get(self, **headers):
        return RioStatisticsAPIView.as_view()(self.factory.get('/', **headers), module='overview')

    def test_database_build_change_rejects_old_body_and_old_etag_until_new_display_is_ready(self):
        old = self.get()
        self.version.current_build = '12.1.5.70001'
        self.version.save(update_fields=['current_build'])
        pending = self.get(HTTP_IF_NONE_MATCH=old['ETag'])
        self.assertEqual(pending.status_code, 200)
        self.assertIn('no-cache', pending['Cache-Control'])
        self.assertIn('max-age=0', pending['Cache-Control'])
        self.assertIsNone(json.loads(pending.content)['data']['data'])
        self.prepare(20)
        self.assertEqual(json.loads(self.get().content)['data']['data']['value'], 20)

    def test_database_season_change_never_falls_back_to_previous_season(self):
        self.season.rio_season = 'season-mn-3'
        self.season.season_name = '午夜第三赛季'
        self.season.save(update_fields=['rio_season', 'season_name'])
        pending = json.loads(self.get().content)['data']
        self.assertIsNone(pending['data'])
        self.assertEqual(pending['scope']['rio_season'], 'season-mn-3')
        self.prepare(30)
        self.assertEqual(json.loads(self.get().content)['data']['data']['value'], 30)

    def test_completed_night_is_reopened_for_changed_database_scope_but_never_during_day(self):
        from botend.plugin_sync import monitor_task_due_at
        now = datetime(2026, 11, 5, 2, tzinfo=ZoneInfo('Asia/Shanghai'))
        task = SimpleNamespace(name='RioStatisticsMonitor', last_scan_time=now - timedelta(hours=1),
            flag=json.dumps({'identity': f"2026-11-05@{self.scope['token']}", 'state': 'complete'}))
        self.assertIsNone(monitor_task_due_at(task, now=now))
        self.version.current_build = '12.1.5.70001'
        self.version.save(update_fields=['current_build'])
        self.assertIsNotNone(monitor_task_due_at(task, now=now))
        self.assertIsNone(monitor_task_due_at(task, now=now.replace(hour=10)))
