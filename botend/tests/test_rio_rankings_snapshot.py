"""验证榜单契约、公开零查询、后台统一发布及失败保留。"""
import json
from io import StringIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command, CommandError
from django.test import TestCase, RequestFactory

from botend.models import PortalMplusRun, PortalPeakSpecRankRow, SeasonMeta
from botend.portal.api import PortalMplusRankingsAPIView, PortalPeakSpecRankingsAPIView
from botend.controller.plugins.portal.PortalMplusRunMonitor import PortalMplusRunMonitor
from botend.controller.plugins.portal.PortalPeakSpecRankMonitor import PortalPeakSpecRankMonitor
from botend.services import rio_rankings_snapshot as snapshots


def source_run(slug='ruby-life-pools', rank=1, run_id=100):
    return {'rank': rank, 'score': 300, 'run': {'keystone_run_id': run_id,
            'dungeon': {'slug': slug, 'name': slug}, 'mythic_level': 20, 'clear_time_ms': 1200000}}


class RioRankingsSnapshotTests(TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='rio-rankings-'))
        override = self.settings(RIO_RANKINGS_SNAPSHOT_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.season = SeasonMeta.objects.create(season_key='rio-test', season_name='榜单测试',
                                                rio_season='season-mn-2', is_active=True, mplus_zone_id=1, raid_zone_id=2)
        self.factory = RequestFactory()
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('测试不得联网'))
        network.start()
        self.addCleanup(network.stop)

    def seed(self):
        for slug in ('ruby-life-pools', 'kings-rest'):
            for rank in range(1, 36):
                PortalMplusRun.objects.create(season=self.season.rio_season, region='world', source='raiderio',
                    dungeon_slug=slug, dungeon=slug, rank=rank, level=40 - rank, time_seconds=1000 + rank,
                    score=300, run_url=f'https://raider.io/mythic-plus-runs/{rank}',
                    party_json='[{"name":"圣骑测试","class_slug":"paladin","spec_slug":"holy","role":"healer"}]')
        # 汇总的最高层数可能不在按来源排名选出的前 30 中。
        PortalMplusRun.objects.filter(dungeon_slug='ruby-life-pools', rank=35).update(level=50)
        for cls, spec, role in (('mage', 'frost', 'dps'), ('death-knight', 'blood', 'tank'), ('paladin', 'holy', 'healer')):
            for rank in range(1, 21):
                PortalPeakSpecRankRow.objects.create(season=self.season.rio_season, region='world',
                    class_slug=cls, spec_slug=spec, spec_role=role, rank=rank,
                    character_name=f'测试玩家{rank}', realm_name='测试服务器', rio_region_slug='us', score=4000 - rank)

    def publish_all(self):
        for module in snapshots.MODULES:
            snapshots.publish_rankings(module)

    def test_public_contract_retains_best_runs_top_thirty_party_and_top_twenty(self):
        self.seed()
        self.publish_all()
        data = snapshots.read_rankings('mplus', catalog=True)
        self.assertEqual([row['slug'] for row in data['dungeons']], ['kings-rest', 'ruby-life-pools'])
        self.assertEqual([(row['dungeon_slug'], row['rank']) for row in data['items']],
                         [('kings-rest', 1), ('ruby-life-pools', 35)])
        for slug in ('kings-rest', 'ruby-life-pools'):
            filtered = snapshots.read_rankings('mplus', dungeon=slug)
            self.assertEqual(filtered['items'], data['runs_by_dungeon'][slug])
            self.assertEqual(snapshots.read_rankings('mplus', dungeon=f' {slug} ')['items'], filtered['items'])
            self.assertEqual([row['rank'] for row in filtered['items']], list(range(1, 31)))
            self.assertNotIn('runs_by_dungeon', filtered)
        self.assertEqual(data['items'][1]['dungeon_cn'], '红玉新生法池')
        self.assertIn('/large/', data['items'][0]['party'][0]['spec_icon_url'])
        self.assertEqual(data['items'][0]['party'][0]['class_name_cn'], '圣骑士')
        peak = snapshots.read_rankings('peak')
        self.assertEqual(len(peak['items']), 3)
        for role in ('tank', 'healer', 'dps'):
            filtered = snapshots.read_rankings('peak', role=role)
            self.assertEqual(len(filtered['items']), 1)
            self.assertEqual(snapshots.read_rankings('peak', role=f' {role.upper()} ')['items'], filtered['items'])
            self.assertEqual(filtered['items'][0]['spec_role'], role)
            self.assertEqual(len(filtered['items'][0]['items']), 20)
            self.assertIn('/portal/spec/', filtered['items'][0]['aggregate_url'])
            self.assertNotIn('profile_url', filtered['items'][0]['items'][0])

    def test_public_cold_warm_invalid_filters_never_query_build_or_write(self):
        self.seed()
        for warm in (False, True):
            if warm:
                self.publish_all()
            before = {p.name: p.read_bytes() for p in self.root.iterdir()}
            with self.assertNumQueries(0), patch.object(snapshots, '_build', side_effect=AssertionError('公开请求不得生成')), \
                    patch.object(snapshots, '_write', side_effect=AssertionError('公开请求不得写文件')):
                for view in (PortalMplusRankingsAPIView, PortalPeakSpecRankingsAPIView):
                    for params in ({}, {'season': 'auto'}, {'season': 'season-mn-1'}, {'season': 'missing'},
                                   {'region': 'cn'}, {'dungeon': '../../bad', 'role': 'invalid'}, {'catalog': '1'}):
                        response = view.as_view()(self.factory.get('/', params))
                        self.assertEqual(response.status_code, 200)
                        result = json.loads(response.content)['data']
                        expected = 'ready' if warm and params.get('season', '') != 'missing' and params.get('region', 'world') != 'cn' else 'pending'
                        self.assertEqual(result['snapshot']['state'], expected)
            self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_season_region_module_isolation_and_current_season_from_background(self):
        self.seed()
        self.publish_all()
        PortalMplusRun.objects.filter(dungeon_slug='kings-rest').update(region='eu')
        snapshots.publish_rankings('mplus', region='eu')
        self.assertEqual(len(snapshots.read_rankings('mplus', region='eu')['items']), 1)
        self.assertEqual(snapshots.read_rankings('peak', region='eu')['snapshot']['state'], 'pending')
        self.season.rio_season = 'season-next'
        self.season.save(update_fields=['rio_season'])
        PortalMplusRun.objects.create(season='season-next', region='world', dungeon_slug='new', rank=1)
        snapshots.publish_rankings('mplus')
        self.assertEqual(snapshots.read_rankings('mplus')['season'], 'season-next')
        self.assertEqual(snapshots.read_rankings('peak')['snapshot']['state'], 'pending')
        self.assertEqual(snapshots.read_rankings('peak', season='season-mn-2')['snapshot']['state'], 'ready')
        snapshots.publish_rankings('peak', season='season-mn-2')
        self.assertEqual(snapshots.read_rankings('mplus')['season'], 'season-next')

    def test_empty_invalid_or_failed_publication_preserves_previous_and_content_reuses_files(self):
        self.seed()
        self.publish_all()
        first = snapshots.publish_rankings('mplus')
        self.assertEqual(len(list(self.root.glob('*.json'))), 3)
        second = snapshots.publish_rankings('mplus')
        self.assertEqual(first['file'], second['file'])
        before = (self.root / 'index.json').read_bytes()
        PortalMplusRun.objects.update(is_active=False)
        with self.assertRaises(ValueError):
            snapshots.publish_rankings('mplus')
        self.assertEqual((self.root / 'index.json').read_bytes(), before)
        PortalMplusRun.objects.update(is_active=True, level=99)
        write = snapshots._write
        def fail_index(path, data):
            if path.name == 'index.json':
                raise OSError('索引发布失败')
            write(path, data)
        with patch.object(snapshots, '_write', side_effect=fail_index), self.assertRaises(OSError):
            snapshots.publish_rankings('mplus')
        self.assertEqual((self.root / 'index.json').read_bytes(), before)
        self.assertEqual(snapshots.read_rankings('mplus')['items'][0]['level'], 39)
        snapshots.mark_failure('mplus', season=self.season.rio_season)
        self.assertEqual(snapshots.read_rankings('mplus')['snapshot']['state'], 'stale')
        snapshots.publish_rankings('mplus')
        self.assertEqual(snapshots.read_rankings('mplus')['snapshot']['state'], 'ready')

    def test_corrupt_missing_or_other_coordinate_file_returns_pending(self):
        self.seed()
        self.publish_all()
        index = snapshots._manifest()
        entry = index['entries'][snapshots._key('mplus', self.season.rio_season, 'world')]
        path = self.root / entry['file']
        body = json.loads(path.read_text(encoding='utf-8'))
        for value in ('{', json.dumps({**body, 'coordinate': ['mplus', 'other', 'world']})):
            path.write_text(value, encoding='utf-8')
            self.assertEqual(snapshots.read_rankings('mplus')['snapshot']['state'], 'pending')
        entry['file'] = '0' * 32 + '.json'
        snapshots._write(self.root / 'index.json', index)
        self.assertEqual(snapshots.read_rankings('mplus')['snapshot']['state'], 'pending')
        entry['file'] = '../unsafe.json'
        snapshots._write(self.root / 'index.json', index)
        self.assertEqual(snapshots.read_rankings('mplus')['snapshot']['state'], 'pending')

    def test_mplus_collector_replaces_complete_batch_and_failed_fetch_keeps_old(self):
        self.seed()
        self.publish_all()
        monitor = PortalMplusRunMonitor(Mock(), Mock())
        dungeons = [{'slug': 'ruby-life-pools'}, {'slug': 'kings-rest'}]
        good = [{'rankings': [source_run(slug)]} for slug in ('ruby-life-pools', 'kings-rest')]
        with patch.object(monitor, '_get_season_dungeons', return_value=dungeons), \
                patch.object(monitor, '_fetch_json', side_effect=good):
            self.assertTrue(monitor.scan(''))
        self.assertEqual(PortalMplusRun.objects.filter(is_active=True).count(), 2)
        old = snapshots.read_rankings('mplus')['items']
        with patch.object(monitor, '_get_season_dungeons', return_value=dungeons), \
                patch.object(monitor, '_fetch_json', side_effect=[good[0], None]):
            self.assertFalse(monitor.scan(''))
        self.assertEqual(PortalMplusRun.objects.filter(is_active=True).count(), 2)
        self.assertEqual(snapshots.read_rankings('mplus')['items'], old)
        self.assertEqual(snapshots.read_rankings('mplus')['snapshot']['state'], 'stale')

    def test_mplus_transaction_rolls_back_all_rows_and_rejects_wrong_or_empty_ranges(self):
        self.seed()
        self.publish_all()
        monitor = PortalMplusRunMonitor(Mock(), Mock())
        save = monitor._upsert_row
        def failed_save(row, **kwargs):
            save(row, **kwargs)
            raise RuntimeError('模拟第二阶段保存失败')
        with patch.object(monitor, '_get_season_dungeons', return_value=[]), \
                patch.object(monitor, '_fetch_json', return_value={'rankings': [source_run()]}), \
                patch.object(monitor, '_upsert_row', side_effect=failed_save):
            self.assertFalse(monitor.scan(''))
        self.assertEqual(PortalMplusRun.objects.filter(is_active=True).count(), 70)

        for invalid in (None, {'rankings': []}, {'rankings': [source_run('wrong')]},
                        {'rankings': [source_run(), source_run()]}):
            with patch.object(monitor, '_get_season_dungeons', return_value=[{'slug': 'ruby-life-pools'}]), \
                    patch.object(monitor, '_fetch_json', return_value=invalid):
                self.assertFalse(monitor.scan(''))
        self.assertEqual(PortalMplusRun.objects.filter(is_active=True).count(), 70)

    def test_mplus_fallback_page_cannot_deactivate_unfetched_dungeons(self):
        self.seed()
        self.publish_all()
        monitor = PortalMplusRunMonitor(Mock(), Mock())
        with patch.object(monitor, '_get_season_dungeons', return_value=[]), \
                patch.object(monitor, '_fetch_json', return_value={'rankings': [source_run()]}):
            self.assertFalse(monitor.scan(''))
        self.assertEqual(PortalMplusRun.objects.filter(dungeon_slug='kings-rest', is_active=True).count(), 35)
        self.assertEqual(PortalMplusRun.objects.filter(dungeon_slug='ruby-life-pools', is_active=True).count(), 1)
        self.assertEqual(snapshots.read_rankings('mplus')['snapshot']['state'], 'stale')

    def test_peak_partial_updates_keep_old_specializations_and_use_same_publisher(self):
        self.seed()
        self.publish_all()
        monitor = PortalPeakSpecRankMonitor(Mock(), SimpleNamespace(flag='', save=Mock()))
        def update(**kwargs):
            if kwargs['class_slug'] == 'mage':
                PortalPeakSpecRankRow.objects.filter(class_slug='mage', rank=1).update(character_name='新玩家')
                return True
            return False
        with patch.object(monitor, '_spec_list', return_value=[{'class_slug': 'mage', 'spec_slug': 'frost'},
                  {'class_slug': 'paladin', 'spec_slug': 'holy'}]), patch.object(monitor, '_fetch_and_upsert', side_effect=update), \
                patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.time.sleep'):
            self.assertFalse(monitor.scan(''))
        result = snapshots.read_rankings('peak')
        self.assertEqual(len(result['items']), 3)
        self.assertEqual(result['snapshot']['state'], 'stale')
        self.assertEqual(next(row for row in result['items'] if row['class_slug'] == 'mage')['items'][0]['name'], '新玩家')
        self.assertEqual(next(row for row in result['items'] if row['class_slug'] == 'paladin')['items'][0]['name'], '测试玩家1')

    def test_management_command_is_offline_and_missing_module_reports_failure(self):
        self.seed()
        call_command('refresh_rio_rankings_snapshots', stdout=StringIO())
        self.assertEqual(snapshots.read_rankings('peak')['snapshot']['state'], 'ready')
        with self.assertRaises(CommandError):
            call_command('refresh_rio_rankings_snapshots', season='missing', stdout=StringIO())

    def test_peak_publication_failure_keeps_file_and_marks_stale_without_success_flag(self):
        self.seed()
        self.publish_all()
        previous = snapshots.read_rankings('peak')['items']
        task = SimpleNamespace(flag='原成功标记', save=Mock())
        monitor = PortalPeakSpecRankMonitor(Mock(), task)
        with patch.object(monitor, '_spec_list', return_value=[{'class_slug': 'mage', 'spec_slug': 'frost'}]), \
                patch.object(monitor, '_fetch_and_upsert', return_value=True), \
                patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.publish_rankings', side_effect=OSError('模拟发布失败')), \
                patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.time.sleep'):
            self.assertFalse(monitor.scan(''))
        result = snapshots.read_rankings('peak')
        self.assertEqual(result['items'], previous)
        self.assertEqual(result['snapshot']['state'], 'stale')
        self.assertEqual(task.flag, '原成功标记')
        task.save.assert_not_called()

    def test_collector_locks_skip_duplicate_fetches(self):
        for module, cls in [('mplus', PortalMplusRunMonitor), ('peak', PortalPeakSpecRankMonitor)]:
            monitor = cls(Mock(), Mock())
            with snapshots._lock(self.root / f'collect-{module}.lock'), patch.object(monitor, '_scan') as scan:
                self.assertFalse(monitor.scan(''))
            scan.assert_not_called()
