"""验证全链路局部失效、分页续跑、发布失败恢复和更新及时性。"""
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from botend.models import (SeasonMeta, SpecRaidRanking, SpecDungeonRanking, PlayerSpecTopPlayer,
    WclCollectionCycle, SpecAggregationWorkCache, WowDataUpdateState, WowTalentVersion, WowTalentNodeMetadata)
from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor, publish_stats
from botend.services.raid_aggregation import raid_stats
from botend.services.dungeon_aggregation import aggregate_dungeon
from botend.services.spec_stats_service import SpecStatsService
from botend.services.spec_stats_snapshot import read_projection
from botend.tests.aggregation_cache_helpers import isolate_work_cache


class FullChainIncrementalTests(TestCase):
    def setUp(self):
        self.work_root = isolate_work_cache(self)
        root = Path(__file__).resolve().parents[2] / 'tmp'
        root.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='全链路验证-', dir=root))
        self.season = SeasonMeta.objects.create(season_key='链路验证', season_name='测试', is_active=True,
            mplus_zone_id=1, raid_zone_id=2,
            mplus_encounters=[{'id': 1, 'name': '一号'}, {'id': 2, 'name': '二号'}],
            raid_encounters=[{'id': 1, 'name': '一号'}, {'id': 2, 'name': '二号'}])
        self.path = self.root / str(self.season.pk) / 'Mage' / 'Fire'
        self.monitor = SpecDetailRankingMonitor(None, None)
        self.cycle = WclCollectionCycle.objects.create(season=self.season, slot=timezone.now(), owner='初始')
        self.monitor._cycle = self.cycle
        self.page = {'rankings': [{'name': '角色', 'amount': 100}], 'hasMorePages': False}
        self.enterContext(patch('botend.controller.plugins.portal.SpecDetailRankingMonitor.time.sleep'))

    def raid(self, boss=1, **extra):
        return SpecRaidRanking.objects.create(season_id=self.season.pk, boss_id=boss, boss_name='首领',
            difficulty=5, class_name='Mage', spec_name='Fire', character_name='角色', realm='Realm',
            region='eu', dps=100, **extra)

    def test_successful_pages_survive_failure_and_restart_but_not_new_cycle(self):
        with patch.object(self.monitor, 'fetch_wcl_rankings', side_effect=[self.page, None]) as fetch:
            self.monitor._fetch_rankings_with_retry(1, 'Mage', 'Fire', page=1)
            self.assertIsNone(self.monitor._fetch_rankings_with_retry(1, 'Mage', 'Fire', page=2, max_retries=1))
        resumed = SpecDetailRankingMonitor(None, None)
        resumed._cycle = self.cycle
        with patch.object(resumed, 'fetch_wcl_rankings', return_value=self.page) as fetch:
            resumed._fetch_rankings_with_retry(1, 'Mage', 'Fire', page=1)
            resumed._fetch_rankings_with_retry(1, 'Mage', 'Fire', page=2)
            self.assertEqual(fetch.call_count, 1)
            self.cycle.phase = 'published'; self.cycle.save()
            resumed._cycle = WclCollectionCycle.objects.create(season=self.season,
                slot=timezone.now() + timedelta(days=1), owner='下一轮')
            resumed._fetch_rankings_with_retry(1, 'Mage', 'Fire', page=1)
            self.assertEqual(fetch.call_count, 2)

    def test_invalid_page_is_not_frozen(self):
        with patch.object(self.monitor, 'fetch_wcl_rankings', return_value={'rankings': [], 'hasMorePages': True}):
            self.monitor._fetch_rankings_with_retry(1, 'Mage', 'Fire')
        self.assertFalse(SpecAggregationWorkCache.objects.exists())
        self.assertFalse(list(self.work_root.rglob('*.json')))

    def test_raid_change_recomputes_only_affected_boss_and_profile_changes_invalidate(self):
        first, second = self.raid(), self.raid(2)
        def run(boss):
            return raid_stats(self.season.pk, boss, '首领', 'Mage', 'Fire', 5)
        run(1); run(2)
        with patch.object(SpecStatsService, '_compute_raid_stats', wraps=SpecStatsService._compute_raid_stats) as compute:
            self.assertTrue(run(1)[2]); self.assertTrue(run(2)[2]); compute.assert_not_called()
            SpecRaidRanking.objects.filter(pk=first.pk).update(dps=200)
            self.assertFalse(run(1)[2]); self.assertTrue(run(2)[2])
            self.assertEqual(compute.call_count, 1)
        PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, class_name='Mage', spec_name='Fire',
            character_name='角色', realm='Realm', region='eu', race='Human', stats_json={'haste': 200})
        self.assertFalse(run(1)[2])

    def test_publication_failure_reuses_computation_and_retries_publication(self):
        self.raid()
        run = lambda publish: SpecDetailAggregationMonitor._aggregate_raid(
            self.season, 'Mage', 'Fire', str(self.path), publish=publish)
        with self.assertRaises(OSError):
            run(Mock(side_effect=OSError('模拟发布失败')))
        with patch.object(SpecStatsService, '_compute_raid_stats', side_effect=AssertionError('重复计算')):
            run(publish_stats)
        index = self.path / 'raid.json'
        before = index.read_bytes(), index.stat().st_mtime_ns
        with patch('botend.services.wow_talent_display.refresh_aggregate_talents', side_effect=AssertionError('重复补全')):
            run(publish_stats)
        self.assertEqual(before, (index.read_bytes(), index.stat().st_mtime_ns))
        self.assertEqual(read_projection(index, 'raid-5-1')['zone_groups'][0]['bosses'][0]['sample_size'], 1)

    def test_dungeon_one_change_reuses_other_dungeon(self):
        rows = [SpecDungeonRanking.objects.create(season_id=self.season.pk, dungeon_id=i, dungeon_name=str(i),
            class_name='Mage', spec_name='Fire', character_name='角色', realm='Realm', region='eu',
            report_code=f'报告{i}', fight_id=1, keystone_level=10, dps=100) for i in (1, 2)]
        def run():
            return aggregate_dungeon(self.season, 'Mage', 'Fire', self.path / 'dungeon.json', publish_stats)
        run()
        SpecDungeonRanking.objects.filter(pk=rows[0].pk).update(dps=200)
        with patch.object(SpecStatsService, '_compute_dungeon_records', wraps=SpecStatsService._compute_dungeon_records) as compute:
            run()
        self.assertEqual([call.args[2] for call in compute.call_args_list], [1, 'all'])

    def test_corrupted_work_payload_recomputes(self):
        self.raid()
        args = (self.season.pk, 1, '首领', 'Mage', 'Fire', 5)
        expected = raid_stats(*args)[0]
        from botend.services.spec_aggregation_cache import work_path
        path = work_path(self.season.pk, 'raid:Mage:Fire:5:1')
        envelope = json.loads(path.read_text(encoding='utf-8'))
        envelope['data']['payload'] = {'错误': True}
        path.write_text(json.dumps(envelope), encoding='utf-8')
        actual, _, hit = raid_stats(*args)
        self.assertFalse(hit); self.assertEqual(actual, expected)

    def test_dungeon_publication_failure_preserves_summary_and_details(self):
        SpecDungeonRanking.objects.create(season_id=self.season.pk, dungeon_id=1, dungeon_name='一号',
            class_name='Mage', spec_name='Fire', character_name='角色', dps=100, keystone_level=10)
        args = (self.season, 'Mage', 'Fire', self.path / 'dungeon.json')
        with self.assertRaises(OSError):
            aggregate_dungeon(*args, Mock(side_effect=OSError('发布中断')))
        with patch.object(SpecStatsService, '_compute_dungeon_records', side_effect=AssertionError('重复计算')):
            aggregate_dungeon(*args, publish_stats)
        self.assertEqual(read_projection(args[-1], 'dungeon-all')['summary']['sample_size'], 1)

    def test_raid_metadata_content_edit_invalidates_without_timestamp_change(self):
        from botend.models import WowItemSnapshot
        self.raid(gear_json=[{'id': 123, 'slot': 'head'}])
        item = WowItemSnapshot.objects.create(item_id=123, name='Helmet', name_zh='旧头盔')
        args = (self.season.pk, 1, '首领', 'Mage', 'Fire', 5)
        raid_stats(*args)
        self.assertTrue(raid_stats(*args)[2])
        WowItemSnapshot.objects.filter(pk=item.pk).update(name_zh='新头盔')
        data, _, hit = raid_stats(*args)
        self.assertFalse(hit)
        self.assertIn('新头盔', json.dumps(data, ensure_ascii=False))

    def test_battlenet_refreshes_daily_and_cached_reads_do_not_sleep(self):
        from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
        player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, rank=1,
            class_name='Mage', spec_name='Fire', character_name='角色', realm='realm', region='eu')
        monitor = SpecDetailPlayerMonitor(None, None)
        with patch.object(monitor, '_get_battlenet_token', return_value='测试'), \
                patch.object(monitor, 'fetch_battlenet_stats', return_value={'数据': True}) as fetch, \
                patch.object(monitor, 'parse_battlenet_stats', return_value={'haste': 100}), \
                patch('botend.controller.plugins.portal.SpecDetailPlayerMonitor.time.sleep') as sleep:
            monitor._crawl_battlenet_stats_for_profile(player)
            sleep.reset_mock()
            resumed = SpecDetailPlayerMonitor(None, None)
            self.assertTrue(resumed._crawl_battlenet_stats_for_profile(player))
            sleep.assert_not_called()
            with patch('botend.controller.plugins.portal.SpecDetailPlayerMonitor.time.time', return_value=time.time() + 86401):
                monitor._crawl_battlenet_stats_for_profile(player)
            self.assertEqual(fetch.call_count, 2)

    def test_publication_lock_excludes_another_process_and_releases(self):
        from botend.services.spec_stats_retention import _scope_lock
        lock = self.root / '进程互斥.lock'
        code = """import sys, django
django.setup()
from pathlib import Path
from botend.services.spec_stats_retention import _scope_lock
with _scope_lock(Path(sys.argv[1]), blocking=False) as acquired:
    print(int(acquired))
"""
        def acquire_elsewhere():
            return subprocess.run([sys.executable, '-c', code, str(lock)],
                cwd=Path(__file__).resolve().parents[2], check=True, capture_output=True,
                text=True, timeout=30).stdout.strip()
        with _scope_lock(lock) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(acquire_elsewhere(), '0')
        self.assertEqual(acquire_elsewhere(), '1')

    def test_metadata_publication_reopens_completed_cycle(self):
        from botend.services.wcl_collection_cycle import begin_aggregation, finish_publication
        past = timezone.now() - timedelta(days=2)
        self.cycle.slot = past; self.cycle.collected_at = past
        self.cycle.phase = 'published'; self.cycle.published_at = past; self.cycle.save()
        WowDataUpdateState.objects.create(branch='retail', published_at=timezone.now(), published_build='12.0.0.1')
        with patch('botend.services.raid_burst_snapshot.needs_raid_burst_refresh', side_effect=AssertionError('普通投影不得检查爆发')):
            reopened = begin_aggregation(self.season)
            self.assertIsNotNone(reopened)
            finish_publication(reopened)
            self.assertIsNone(begin_aggregation(self.season))

    def test_completed_catalog_stage_survives_failure_and_new_build_refetches(self):
        from botend.services import wow_data_update as update
        source = Mock()
        source.build.return_value = {'items': [{'item_id': 123, 'variants': [
            {'key': '普通', 'stats': {'crit': 10}}]}]}
        source._enrich_wowhead.side_effect = OSError('后续阶段中断')
        run = Mock(_source_fingerprint='固定来源')
        with self.settings(BASE_DIR=self.root), patch.object(update, 'active_season', return_value=self.season), \
                patch.object(update, 'BranchCatalogSource', return_value=source):
            for build in ('12.0.0.1', '12.0.0.1', '12.0.0.2'):
                with self.assertRaises(OSError):
                    update.UpdateSource().prepare('retail', build, run, Mock())
            self.assertEqual(source.build.call_count, 2)
            with patch.object(update.time, 'time', return_value=time.time() + 86401):
                with self.assertRaises(OSError):
                    update.UpdateSource().prepare('retail', '12.0.0.2', run, Mock())
            self.assertEqual(source.build.call_count, 3)

    def test_missing_item_effect_does_not_freeze_catalog_stage(self):
        from botend.services import wow_data_update as update
        source = Mock()
        source.build.return_value = {'items': [{'item_id': 123, 'inventory_type': 12,
            'variants': [{'key': '饰品', 'stats': {'crit': 10}, 'effects': []}]}]}
        source._enrich_wowhead.side_effect = OSError('后续阶段中断')
        with self.settings(BASE_DIR=self.root), patch.object(update, 'active_season', return_value=self.season), \
                patch.object(update, 'BranchCatalogSource', return_value=source):
            for _ in range(2):
                with self.assertRaises(OSError):
                    update.UpdateSource().prepare('retail', '12.0.0.1', Mock(_source_fingerprint='来源'), Mock())
        self.assertEqual(source.build.call_count, 2)

    def test_talent_preparation_stage_survives_later_failure(self):
        from botend.services import wow_data_update as update
        directory = self.root / '天赋来源'; directory.mkdir()
        (directory / 'manifest.json').write_text('{}', encoding='utf-8')
        fallback = directory / 'display_fallback.json'
        fallback.write_text('{}', encoding='utf-8')
        icons = directory / 'file_data_icon_cache.csv'
        icons.write_text('ID,Icon\n1,test\n', encoding='utf-8')
        version = WowTalentVersion(branch='retail', label='测试', major_version='12.0.0',
            current_build='12.0.0.1', source_dir=str(directory))
        version._prepared_nodes = [WowTalentNodeMetadata(class_name='Mage', spec_name='Fire', node_id=100)]
        version._missing_fields = ['天赋 100:icon']
        bundle = {'season_id': self.season.pk, 'dump_dir': str(directory)}
        with patch.object(update, '_prepare_talent_version', return_value=version) as prepare:
            update.prepare_talent_version(bundle, 'retail', '12.0.0.1', Mock())
            os.utime(icons, (time.time() + 10, time.time() + 10))
            cached = update.prepare_talent_version(bundle, 'retail', '12.0.0.1', Mock())
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(cached._prepared_nodes[0].node_id, 100)
            fallback.write_text(json.dumps({'1': {'icon': '新图标'}}), encoding='utf-8')
            update.prepare_talent_version(bundle, 'retail', '12.0.0.1', Mock())
            self.assertEqual(prepare.call_count, 2)
            icons.write_text('ID,Icon\n1,new\n', encoding='utf-8')
            update.prepare_talent_version(bundle, 'retail', '12.0.0.1', Mock())
            self.assertEqual(prepare.call_count, 3)
