"""验证存储去重、缓存预算及回收进展，不改变业务统计口径。"""
import gzip
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from django.test import SimpleTestCase
from botend.services import spec_aggregation_cache as cache
from botend.services import spec_stats_retention as retention
from botend.services import spec_stats_snapshot as snapshot
from botend.services.spec_work_retention import maintain_work_cache
from botend.services.stats_storage_codec import pack_detail, unpack_detail
from botend.services.simc_benchmark_result_snapshot import _write


class AggregationStorageTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(self.settings(SPEC_AGGREGATION_WORK_ROOT=self.root / 'work'))
        self.scope = self.root / 'aggregated/1/Mage/Fire'

    def payload(self):
        description = '这是一段保持原意的天赋技能描述。' * 100
        builds = [{'state_key': str(i), 'count': 3, 'template_diffs': [
            {'name': str(i), 'description': description}]} for i in range(10)]
        families = [{'builds': builds[:5], 'representative': builds[0]},
                    {'builds': builds[5:], 'representative': builds[5]}]
        return {'dungeon_id': 1, 'sample_size': 100, 'talent_build_popularity': {
            'builds': builds, 'families': families, 'primary_template': builds[0],
            'template_family': families[0]}}

    def test_lossless_build_and_description_interning(self):
        detail = self.payload()
        original = json.dumps(detail)
        packed = pack_detail(detail)
        recovered = unpack_detail(json.loads(json.dumps(packed)))
        self.assertEqual(detail, recovered)
        self.assertEqual(json.dumps(detail), original)
        self.assertLess(len(json.dumps(packed)), len(original) // 5)
        pop = recovered['talent_build_popularity']
        self.assertIs(pop['primary_template'], pop['builds'][0])
        self.assertIs(pop['template_family'], pop['families'][0])
        self.assertIs(pop['builds'][0]['template_diffs'][0]['description'],
                      pop['builds'][1]['template_diffs'][0]['description'])

    def test_unknown_extensions_and_colliding_reference_are_preserved(self):
        detail = self.payload()
        detail['unknown'] = {'_stats_text_ref': 99}
        detail['talent_build_popularity']['primary_template'] = {'state_key': 'different'}
        self.assertEqual(unpack_detail(pack_detail(detail)), detail)
        self.assertEqual(cache._normalized({1: '旧值', '1': '最终值'}), {'1': '最终值'})

    def test_corrupt_reference_is_rejected(self):
        packed = pack_detail(self.payload())
        packed['talent_build_popularity']['primary_template'] = -1
        with self.assertRaises(ValueError):
            unpack_detail(packed)

    def test_render_node_tables_and_aliases_restore_every_field(self):
        nodes = [{'node_key': str(i), 'name': '天赋', 'points': i, 'custom': [i, None]}
                 for i in range(8)]
        panel = {'nodes': [{'node_id': i, 'x': i * 10} for i in range(8)],
                 'paths': [{'parent': 1, 'child': 2}], 'width': 80}
        tree = {'nodes': nodes, 'panel': panel, 'paths': panel['paths']}
        detail = {'talent_popularity_tree': {'render_model': {
            'nodes': nodes, 'trees': [tree], 'layout': {'panels': [panel]},
            'tree_set': {'trees': [{'nodes': [{'id': i, 'selected': True} for i in range(8)]}]}}}}
        packed = pack_detail(detail)
        self.assertTrue(packed['_stats_storage']['render'])
        self.assertEqual(unpack_detail(json.loads(json.dumps(packed))), detail)
        packed['talent_popularity_tree']['render_model']['nodes']['rows'][0][0] = 999
        with self.assertRaises(ValueError):
            unpack_detail(packed)

        # 部分旧结构没有面板或连线字段，不能在恢复时凭空增加空字段。
        for field in ('panel', 'paths'):
            partial = json.loads(json.dumps(detail))
            del partial['talent_popularity_tree']['render_model']['trees'][0][field]
            self.assertEqual(unpack_detail(pack_detail(partial)), partial)

    def test_background_aggregate_defaults_collect_and_respect_explicit_disable(self):
        from botend.services.storage_lifecycle_maintenance import run_extended_maintenance
        from django.test import override_settings
        self.publish_history()
        with override_settings(MEDIA_ROOT=self.root, STORAGE_MAINTENANCE_ROOT=self.root / 'control',
                               MYTHICSTATS_GC_ENABLED=False, DOWNLOAD_PARTS_GC_ENABLED=False):
            results = run_extended_maintenance()
            self.assertEqual(results[0]['deleted'], 2, results)
            self.assertGreater(results[0]['deleted_bytes'], 0)
            with override_settings(SPEC_STATS_GC_ENABLED=False):
                self.assertEqual(run_extended_maintenance(), [])

    def test_published_read_is_identical_and_storage_is_compact(self):
        detail = self.payload()
        path = self.scope / 'dungeon.json'
        snapshot.publish_projection(path, {'dungeons': [detail]}, _write)
        self.assertEqual(snapshot.read_projection(path, 'dungeon-1')['dungeons'], [detail])
        stored = next((self.scope / 'stats-details').rglob('*.json'))
        self.assertIn('_stats_storage', json.loads(stored.read_text())['data'])
        self.assertEqual(retention.maintain_scope(self.scope, dry_run=True)['status'], 'ok')

    def test_other_domain_failure_does_not_block_aggregation_maintenance(self):
        from botend.services import snapshot_cache_maintenance as maintenance
        work = dict(kind='aggregation_work', status='ok', apply=False, deleted=0,
                    deleted_bytes=0, has_more=False, next_cursor='')
        with patch.object(maintenance, 'maintain_snapshot_caches', side_effect=OSError('其他领域失败')), \
                patch.object(maintenance, 'maintain_work_cache', return_value=work) as collect, \
                patch('botend.services.storage_lifecycle_maintenance.run_extended_maintenance', return_value=[]) as extended:
            results = maintenance.run_maintenance_once()
        collect.assert_called_once()
        extended.assert_called_once()
        self.assertEqual([row['status'] for row in results], ['error', 'ok'])

    def test_oversized_publication_preserves_previous_index(self):
        from botend.controller.plugins.portal.SpecDetailAggregationMonitor import atomic_dump_json
        path = self.scope / 'dungeon.json'
        snapshot.publish_projection(path, {'dungeons': [{'dungeon_id': 1}]}, _write)
        previous = path.read_bytes()
        with self.settings(SPEC_STATS_DETAIL_MAX_BYTES=1024):
            with self.assertRaises(ValueError):
                snapshot.publish_projection(path, {'dungeons': [
                    {'dungeon_id': 1, 'oversize': 'x' * 2000}]}, atomic_dump_json)
        self.assertEqual(path.read_bytes(), previous)

    def test_streamed_compression_and_zero_sql_on_cold_miss(self):
        detail = self.payload()
        with patch.object(cache, '_load_database', side_effect=AssertionError('不应回查旧表')):
            self.assertIsNone(cache.load_work(1, 'dungeon:scope', 'v1'))
            with patch.object(cache.json, 'dumps', side_effect=AssertionError('不应整块编码')):
                self.assertEqual(cache.save_work(1, 'dungeon:scope', 'v1', detail), detail)
            self.assertEqual(cache.load_work(1, 'dungeon:scope', 'v1'), detail)
        path = cache.work_path(1, 'dungeon:scope')
        self.assertTrue(path.read_bytes().startswith(b'\x1f\x8b'))
        self.assertLess(path.stat().st_size, 4096)

    def test_budget_failure_keeps_last_good_and_bounds_read(self):
        cache.save_work(1, 'scope', 'old', {'value': 1})
        with self.settings(SPEC_AGGREGATION_WORK_MAX_BYTES=1024), self.assertLogs(cache.logger):
            with self.assertRaises(cache.WorkCacheTooLarge):
                cache.save_work(1, 'scope', 'new', {'value': 'x' * 2000}, strict=True)
        self.assertEqual(cache.load_work(1, 'scope', 'old'), {'value': 1})
        path = cache.work_path(1, 'large')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(b'x' * 2000))
        with self.settings(SPEC_AGGREGATION_WORK_MAX_BYTES=1024):
            self.assertIsNone(cache._read_file(path))

    def test_legacy_json_remains_readable_without_database(self):
        scope = 'legacy'
        path = cache.work_path(1, scope).with_suffix('')
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({'schema': 1, 'season_id': 1, 'scope': scope,
                           'fingerprint': 'v', 'payload': {'原样': 1}}).encode()
        path.write_bytes(cache.PREFIX + cache.hashlib.sha256(body).hexdigest().encode()
                         + cache.SEPARATOR + body + b'}')
        self.assertEqual(cache.load_work(1, scope, 'v'), {'原样': 1})

    def test_work_gc_expiry_dry_run_cursor_unknown_and_lock(self):
        for i in range(6):
            cache.save_work(1, str(i), 'v', {'i': i})
            os.utime(cache.work_path(1, str(i)), (1, 1))
        cache.save_work(1, 'fresh', 'v', {})
        unknown = cache.cache_root() / 'important.json'
        unknown.write_text('{}')
        result = maintain_work_cache(apply=False)
        self.assertEqual(result['eligible'], 6)
        self.assertEqual(result['deleted'], 0)
        cursor, deleted = '', 0
        for _ in range(10):
            result = maintain_work_cache(apply=True, scan_limit=2, after=cursor)
            deleted += result['deleted']
            cursor = result['next_cursor']
            if not result['has_more']:
                break
        self.assertEqual(deleted, 6)
        self.assertTrue(unknown.exists())
        self.assertTrue(cache.work_path(1, 'fresh').exists())
        self.assertEqual(len(list(cache.cache_root().rglob('*.lock'))), 8)

    def publish_history(self, *, details=1, padding=0):
        for i in range(5):
            snapshot.publish_projection(self.scope / 'dungeon.json', {'dungeons': [
                {'dungeon_id': j + 1, 'sample_size': i, 'padding': 'x' * padding}
                for j in range(details)]}, _write)
        manifest = json.loads((self.scope / retention.MANIFEST).read_text())
        for row in manifest['records']:
            if row['retired_at'] is not None:
                row['retired_at'] = 1
        _write(self.scope / retention.MANIFEST, manifest)
        for path in (self.scope / 'stats-details').rglob('*'):
            os.utime(path, (1, 1))

    def test_more_than_512_historical_detail_files_can_be_reclaimed(self):
        self.publish_history(details=110)
        self.assertEqual(len(list((self.scope / 'stats-details').rglob('*.json'))), 550)
        first = retention.maintain_scope(self.scope)
        self.assertEqual(first['status'], 'paused', first)
        self.assertLessEqual(first['files_read'], retention.MAX_ENTRIES)
        result = retention.maintain_scope(self.scope)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['deleted'], 2)
        self.assertIsNotNone(snapshot.read_projection(self.scope / 'dungeon.json', 'dungeon-1'))

    def test_single_detail_larger_than_cycle_budget_makes_progress(self):
        self.publish_history(padding=22000)
        with patch.object(retention, 'MAX_BYTES', 18000):
            results = [retention.maintain_scope(self.scope) for _ in range(10)]
        self.assertEqual(sum(r['deleted'] for r in results), 2, results)
        self.assertTrue(any(r['status'] == 'paused' for r in results), results)

    def test_new_publications_do_not_reset_immutable_history_progress(self):
        self.publish_history(padding=22000)
        results = []
        for i in range(10):
            snapshot.publish_projection(self.scope / 'dungeon.json', {'dungeons': [
                {'dungeon_id': 1, 'sample_size': 100 + i}]}, _write)
            with patch.object(retention, 'MAX_BYTES', 18000):
                results.append(retention.maintain_scope(self.scope))
        self.assertGreater(sum(row['deleted'] for row in results), 0, results)
        current = snapshot.read_projection(self.scope / 'dungeon.json', 'dungeon-1')
        self.assertEqual(current['dungeons'][0]['sample_size'], 109)

    def test_disabled_repair_validates_everything_and_archives_marker(self):
        self.publish_history()
        retention._disable(self.scope, '旧目录预算阻塞')
        self.assertEqual(retention.maintain_scope(self.scope)['status'], 'disabled')
        before = (self.scope / retention.DISABLED).read_bytes()
        self.assertEqual(retention.repair_disabled_scope(self.scope)['status'], 'repairable')
        self.assertEqual((self.scope / retention.DISABLED).read_bytes(), before)
        self.assertEqual(retention.repair_disabled_scope(self.scope, dry_run=False)['status'], 'repaired')
        self.assertEqual((self.scope / '.stats-retention-repaired.json').read_bytes(), before)
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 2)

    def test_disabled_repair_refuses_damaged_history(self):
        self.publish_history()
        retention._disable(self.scope, '未知故障')
        next((self.scope / 'stats-details').rglob('*.json')).write_text('{}')
        self.assertEqual(retention.repair_disabled_scope(self.scope, dry_run=False)['status'], 'refused')
        self.assertTrue((self.scope / retention.DISABLED).exists())

    def test_disabled_repair_resumes_beyond_single_batch(self):
        self.publish_history()
        retention._disable(self.scope, '历史数量超过旧批次上限')
        with patch.object(retention, 'MAX_ENTRIES', 4):
            results = []
            for _ in range(5):
                result = retention.repair_disabled_scope(self.scope, dry_run=False)
                results.append(result)
                if result['status'] != 'paused':
                    break
        self.assertEqual(results[0]['status'], 'paused', results)
        self.assertEqual(results[-1]['status'], 'repaired', results)
        self.assertEqual(len(list((self.scope / 'stats-details').iterdir())), 5)
        self.assertEqual(retention.maintain_scope(self.scope)['deleted'], 2)
