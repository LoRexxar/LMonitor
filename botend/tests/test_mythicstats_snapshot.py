"""验证单一采集路径、完整覆盖、公开零查询和失败时保留历史。"""
from io import StringIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command, CommandError
from django.test import TestCase, RequestFactory, override_settings

from botend.models import MonitorTask, PortalMythicstatsDpsRow
from botend.portal.api import PortalMythicstatsDpsAPIView
from botend.services import mythicstats_snapshot as snapshots


def payload(*, season='test-season', dungeon_id=0, period_id=103, value=287000):
    return {'season': season, 'dungeon_id': dungeon_id, 'period_id': period_id,
            'period_label': f'Week {period_id - 100}',
            'periods': [{'id': pid, 'label': f'Week {pid - 100}'} for pid in (103, 102, 101)],
            'dungeons': [{'id': 0, 'name': 'All dungeons'}, {'id': 11, 'name': '测试副本甲'}, {'id': 12, 'name': '测试副本乙'}],
            'source_note': 'Top 10% Mythic+ 10–20 keys.', 'key_min': 10, 'key_max': 20,
            'rankings': {role: [{'spec_slug': slug, 'spec_name': slug, 'rank': 1, 'tier': 'A',
                               'avg_value': value + dungeon_id, 'avg_text': '287K', 'top_value': 367000,
                               'top_text': '367K', 'runs_text': '16K', 'runs_value': 16000,
                               'diff_raw': '+2', 'diff_value': 2, 'spec_url': f'/spec/{slug}'}]
                         for role, slug in [('damage', 'frost-mage'), ('tank', 'blood-death-knight'), ('healer', 'holy-priest')]}}


class MythicstatsSnapshotTests(TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='mythicstats-test-'))
        override = override_settings(MYTHICSTATS_SNAPSHOT_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.requests = []
        self.values = {}
        self.fail = set()
        self.season = 'test-season'
        def fetch(*, req=None, season='', dungeon_id=0, period_id=None):
            pid = period_id or 103
            self.requests.append((dungeon_id, pid))
            if (dungeon_id, pid) in self.fail:
                raise RuntimeError('模拟上游不可用')
            return payload(season=self.season, dungeon_id=dungeon_id, period_id=pid,
                           value=self.values.get((dungeon_id, pid), 287000))
        self.fetch = patch.object(snapshots.source, 'fetch_mythicstats_dps', side_effect=fetch).start()
        self.addCleanup(patch.stopall)
        self.owner = patch.object(snapshots.source, 'fetch_period_season_slug', side_effect=lambda **kw: (self.season, '')).start()
        patch('requests.sessions.Session.request', side_effect=AssertionError('测试不得访问真实网络')).start()

    def test_collection_covers_every_dungeon_period_and_role_with_one_published_pointer(self):
        result = snapshots.collect_snapshots(season_hint='https://mythicstats.com/dps?season=test-season')
        self.assertEqual((result['built'], result['failed']), (9, 0))
        self.assertEqual(set(self.requests), {(did, pid) for did in (0, 11, 12) for pid in (101, 102, 103)})
        self.assertEqual(len(self.requests), 9)
        self.assertEqual(PortalMythicstatsDpsRow.objects.count(), 27)
        data = snapshots.read_snapshot()
        self.assertEqual((data['season'], data['active_period']), ('test-season', 103))
        self.assertEqual(data['source_note'], 'Top 10% Mythic+ 10–20 keys.')
        self.assertEqual([row['id'] for row in data['periods']], [103, 102, 101])
        for did in (0, 11, 12):
            for pid in (101, 102, 103):
                data = snapshots.read_snapshot(dungeon_id=did, period_id=pid)
                self.assertEqual(data['snapshot']['state'], 'ready')
                self.assertEqual(set(data['roles']), set(snapshots.ROLES))
                self.assertEqual(data['roles']['damage'][0]['avg_value'], 287000 + did)
                self.assertEqual(data['roles']['damage'][0]['class_name'], 'Mage')

    def test_public_requests_never_fetch_write_or_query_including_cold_and_unknown_filters(self):
        view = PortalMythicstatsDpsAPIView.as_view()
        factory = RequestFactory()
        for warm in (False, True):
            if warm:
                snapshots.collect_snapshots()
            before = sorted(path.name for path in self.root.iterdir())
            with self.assertNumQueries(0), patch.object(snapshots.source, 'fetch_mythicstats_dps', side_effect=AssertionError('请求不得抓取')), \
                    patch.object(snapshots.source, 'fetch_current_season_slug', side_effect=AssertionError('请求不得检测赛季')), \
                    patch.object(snapshots, '_write', side_effect=AssertionError('请求不得写文件')):
                for params in ({}, {'season': 'auto'}, {'season': 'season-mn-1'}, {'season': 'test-season', 'dungeon': 11},
                               {'season': 'missing'}, {'dungeon': 999, 'period': 999}, {'dungeon': '../bad', 'period': 'bad'}):
                    response = view(factory.get('/', params))
                    self.assertEqual(response.status_code, 200)
            self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)

    def test_partial_failure_keeps_old_shard_and_all_roles_rollback_together(self):
        snapshots.collect_snapshots()
        before = snapshots.read_snapshot(dungeon_id=11)['roles']
        self.values[(11, 103)] = 888000
        original = snapshots.source.upsert_mythicstats_dps_rows
        def fail_healer(**kwargs):
            if kwargs['dungeon_id'] == 11 and kwargs['role'] == 'healer':
                raise RuntimeError('第三个职责保存失败')
            return original(**kwargs)
        with patch.object(snapshots.source, 'upsert_mythicstats_dps_rows', side_effect=fail_healer):
            result = snapshots.collect_snapshots()
        self.assertEqual(result['failed'], 1)
        data = snapshots.read_snapshot(dungeon_id=11)
        self.assertEqual(data['roles'], before)
        self.assertEqual(data['snapshot']['state'], 'stale')
        self.assertEqual(PortalMythicstatsDpsRow.objects.get(season=self.season, dungeon_id=11, period_id=103, role='damage').avg_value, 287011)
        self.assertEqual(snapshots.read_snapshot(dungeon_id=12)['snapshot']['state'], 'ready')
        snapshots.collect_snapshots()
        self.assertEqual(snapshots.read_snapshot(dungeon_id=11)['snapshot']['state'], 'ready')
        self.assertEqual(snapshots.read_snapshot(dungeon_id=11)['roles']['damage'][0]['avg_value'], 888011)

    def test_completed_historical_shards_are_reused_and_missing_historical_shards_retry(self):
        self.fail = {(11, 102)}
        snapshots.collect_snapshots()
        self.assertEqual(snapshots.read_snapshot(dungeon_id=11, period_id=102)['snapshot']['state'], 'pending')
        self.fail.clear()
        self.requests.clear()
        result = snapshots.collect_snapshots()
        self.assertEqual(result['built'], 4)
        self.assertEqual(set(self.requests), {(0, 103), (11, 103), (12, 103), (11, 102)})

    def test_new_season_never_relabels_previous_season_periods_or_drops_history(self):
        snapshots.collect_snapshots()
        self.season = 'next-season'
        self.owner.side_effect = lambda **kw: ('test-season', '')
        result = snapshots.collect_snapshots()
        self.assertEqual(result['built'], 3)
        self.assertEqual([p['id'] for p in snapshots.read_snapshot()['periods']], [103])
        self.assertEqual(snapshots.read_snapshot()['season'], 'next-season')
        self.assertEqual(snapshots.read_snapshot(season='test-season', period_id=102)['snapshot']['state'], 'ready')
        self.assertFalse(PortalMythicstatsDpsRow.objects.filter(season='next-season', period_id=102).exists())

    def test_invalid_empty_source_and_publication_failure_preserve_previous_index(self):
        snapshots.collect_snapshots()
        before = (self.root / 'index.json').read_bytes()
        for invalid in ({**payload(), 'rankings': {role: [] for role in snapshots.ROLES}},
                        {**payload(), 'season': 'unknown'}, {**payload(), 'dungeons': []}):
            with patch.object(snapshots.source, 'fetch_mythicstats_dps', return_value=invalid):
                with self.assertRaises(ValueError):
                    snapshots.collect_snapshots()
            self.assertEqual((self.root / 'index.json').read_bytes(), before)
        with self.assertRaises(ValueError):
            snapshots.collect_snapshots(season_hint='wrong-season')
        original = snapshots._write
        def fail_index(path, data):
            if path.name == 'index.json':
                raise OSError('索引写入失败')
            return original(path, data)
        with patch.object(snapshots, '_write', side_effect=fail_index):
            with self.assertRaises(OSError):
                snapshots.collect_snapshots()
        self.assertEqual((self.root / 'index.json').read_bytes(), before)

    def test_worker_lock_prevents_duplicate_collection(self):
        with snapshots._lock(self.root / 'worker.lock'):
            self.assertTrue(snapshots.collect_snapshots()['busy'])
        self.fetch.assert_not_called()

    def test_database_export_is_offline_and_idempotent_then_uses_same_read_path(self):
        PortalMythicstatsDpsRow.objects.create(season=self.season, period_id=103, period_label='Week 3', role='damage',
                                             spec_slug='frost-mage', spec_name='冰霜法师', rank=1, avg_value=123000)
        # 即使库内已有记录，公开请求也不能绕过发布路径回退查询。
        with self.assertNumQueries(0):
            self.assertEqual(snapshots.read_snapshot(season=self.season)['snapshot']['state'], 'pending')
        self.assertEqual(snapshots.publish_database_snapshots(), 1)
        self.assertEqual(snapshots.read_snapshot()['season'], '')
        self.assertEqual(snapshots.read_snapshot(season=self.season)['snapshot']['state'], 'ready')
        MonitorTask.objects.create(name='PortalMythicstatsDpsMonitor', target='auto', flag=f'{self.season}@103')
        output = StringIO()
        call_command('refresh_mythicstats_snapshots', from_database=True, stdout=output)
        self.fetch.assert_not_called()
        self.assertEqual(snapshots.read_snapshot()['roles']['damage'][0]['avg_value'], 123000)
        self.assertEqual(snapshots.publish_database_snapshots(), 0)
        # 旧数据库导出的分片仍由同一采集器补齐来源与其他职责。
        snapshots.collect_snapshots()
        self.assertEqual(snapshots.read_snapshot()['roles']['healer'][0]['spec_slug'], 'holy-priest')

    def test_monitor_and_command_call_the_same_collector(self):
        from botend.controller.plugins.portal.PortalMythicstatsDpsMonitor import PortalMythicstatsDpsMonitor
        task = SimpleNamespace(target='auto', flag='', save=Mock())
        result = {'busy': False, 'season': self.season, 'period_id': 103, 'built': 9, 'failed': 0, 'latest_published': True}
        with patch('botend.controller.plugins.portal.PortalMythicstatsDpsMonitor.collect_snapshots', return_value=result) as monitor:
            request = object()
            self.assertTrue(PortalMythicstatsDpsMonitor(request, task).scan(''))
            monitor.assert_called_once_with(req=request, season_hint='auto')
        self.assertEqual(task.flag, 'test-season@103')
        with patch('botend.management.commands.refresh_mythicstats_snapshots.collect_snapshots', return_value=result) as command:
            call_command('refresh_mythicstats_snapshots', stdout=StringIO())
            command.assert_called_once_with(season_hint='')
        with patch('botend.management.commands.refresh_mythicstats_snapshots.collect_snapshots', return_value={**result, 'failed': 1}):
            with self.assertRaises(CommandError):
                call_command('refresh_mythicstats_snapshots', stdout=StringIO())
