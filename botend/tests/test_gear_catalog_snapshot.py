"""真实数据库目录与浏览器筛选对照、读路径零查询和发布失败保护。"""
import json
import gzip
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import WowItemVariantSnapshot
from botend.services import gear_builder as gb
from botend.services import gear_catalog_snapshot as snapshots
from botend.tests.test_gear_builder import GearBuilderTestDataMixin


class GearCatalogSnapshotTests(GearBuilderTestDataMixin, TestCase):
    def test_scoped_command_does_not_consume_other_registered_coordinates(self):
        other = snapshots.coordinate('Mage', 'Fire', 'chest')
        enhancement = snapshots.coordinate('Warrior', 'Fury', 'head', 'enhancements')
        snapshots.request_refresh(other)
        snapshots.request_refresh(enhancement)
        call_command('refresh_gear_catalog_snapshots', class_name='Warrior', spec_name='Fury',
                     slot='head', kind='equipment', force=True)
        self.assertIsNotNone(snapshots._index(self.key))
        self.assertIsNone(snapshots._index(other))
        self.assertIsNone(snapshots._index(enhancement))

    def test_command_kind_filters_existing_queue_without_class(self):
        enhancement = snapshots.coordinate('Warrior', 'Fury', 'head', 'enhancements')
        snapshots.request_refresh(self.key)
        snapshots.request_refresh(enhancement)
        call_command('refresh_gear_catalog_snapshots', kind='enhancements', force=True)
        self.assertIsNone(snapshots._index(self.key))
        self.assertIsNotNone(snapshots._index(enhancement))

    def test_command_rejects_busy_lock_and_failed_force_even_with_old_snapshot(self):
        from django.core.management.base import CommandError
        options = dict(class_name='Warrior', spec_name='Fury', slot='head', kind='equipment', force=True)
        with snapshots._lock(self.directory / 'worker.lock'):
            with self.assertRaises(CommandError):
                call_command('refresh_gear_catalog_snapshots', **options)
        self.assertIsNone(snapshots._index(self.key))
        original = self.build()
        with self.assertLogs(snapshots.logger, level='ERROR'), patch.object(
                gb, 'catalog_snapshot_items', side_effect=RuntimeError('测试生成失败')):
            with self.assertRaises(CommandError):
                call_command('refresh_gear_catalog_snapshots', **options)
        self.assertEqual(snapshots._index(self.key), original)

    def test_explicit_targets_cannot_succeed_when_limit_leaves_one_unbuilt(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('refresh_gear_catalog_snapshots', class_name='Warrior', spec_name='Fury',
                         slot='head', kind='all', limit=1)
        call_command('refresh_gear_catalog_snapshots', class_name='Warrior', spec_name='Fury',
                     slot='head', kind='all')
        with self.assertRaises(CommandError):
            call_command('refresh_gear_catalog_snapshots', class_name='Warrior', spec_name='Fury',
                         slot='head', kind='all', limit=1, force=True)

    def test_command_reads_back_files_instead_of_trusting_build_count(self):
        from django.core.management.base import CommandError
        write = snapshots._write

        def corrupt_after_publish(path, payload):
            write(path, payload)
            if path.name == 'index.json':
                body = path.parent / payload['file']
                body.write_bytes(b'!' + body.read_bytes()[1:])

        with patch.object(snapshots, '_write', side_effect=corrupt_after_publish):
            with self.assertRaises(CommandError):
                call_command('refresh_gear_catalog_snapshots', class_name='Warrior', spec_name='Fury',
                             slot='head', kind='equipment')

    def test_force_repairs_equal_length_plain_and_gzip_corruption(self):
        for suffix in ('', '.gz'):
            with self.subTest(suffix=suffix):
                # Each corruption case starts from an independently built generation.
                (snapshots._directory(self.key) / 'index.json').unlink(missing_ok=True)
                old = self.build()
                path = snapshots._directory(self.key) / (old['file'] + suffix)
                raw = path.read_bytes()
                path.write_bytes(b'!' + raw[1:])
                call_command('refresh_gear_catalog_snapshots', class_name='Warrior', spec_name='Fury',
                             slot='head', kind='equipment', force=True)
                index = snapshots._index(self.key)
                self.assertNotEqual(index['generation'], old['generation'])
                plain = (path.parent / index['file']).read_bytes()
                self.assertEqual(json.loads(plain)['snapshot']['coordinate'], self.key)
                self.assertEqual(gzip.decompress((path.parent / (index['file'] + '.gz')).read_bytes()), plain)

    def test_idle_poll_skips_database_but_revision_and_new_shard_wake_worker(self):
        self.build()
        with self.assertNumQueries(0):
            self.assertEqual(snapshots.refresh_catalog_snapshots(poll=True), [])
        other = snapshots.coordinate('Warrior', 'Fury', 'chest')
        snapshots.request_refresh(other)
        self.assertEqual(snapshots.refresh_catalog_snapshots(poll=True), [other])
        with self.captureOnCommitCallbacks(execute=True):
            snapshots.invalidate_catalog_snapshots()
        self.assertTrue(snapshots.refresh_catalog_snapshots(poll=True))

    def setUp(self):
        super().setUp()
        self.directory = Path(tempfile.mkdtemp(prefix='gear-catalog-'))
        self.override = override_settings(GEAR_CATALOG_SNAPSHOT_ROOT=self.directory)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.key = snapshots.coordinate('Warrior', 'Fury', 'head')
        self.url = '/portal/api/gear-builder/catalog/?snapshot=1&class=Warrior&spec=Fury&slot=head'

    def build(self):
        snapshots.request_refresh(self.key)
        self.assertEqual(snapshots.refresh_catalog_snapshots(), [self.key])
        return snapshots._index(self.key)

    def body(self, response):
        try:
            return json.loads(b''.join(response.streaming_content) if response.streaming else response.content)
        finally:
            response.close()

    def test_cold_and_warm_requests_do_not_query_or_aggregate(self):
        from django.test import RequestFactory
        from botend.portal.gear_builder import PortalGearBuilderCatalogAPIView
        view = PortalGearBuilderCatalogAPIView.as_view()
        factory = RequestFactory()
        with self.assertNumQueries(0), patch.object(gb, 'catalog_items', side_effect=AssertionError('不得同步回退')):
            response = view(factory.get(self.url))
            self.assertEqual(response.status_code, 202)
            self.assertEqual(self.body(response)['snapshot']['state'], 'building')
        index = self.build()
        with self.assertNumQueries(0), patch.object(gb, 'catalog_snapshot_items', side_effect=AssertionError('不得构建')):
            response = view(factory.get(self.url))
            self.assertEqual(response.status_code, 200)
            etag = response['ETag']
            payload = self.body(response)
            self.assertEqual(payload['snapshot']['generation'], index['generation'])
            response = view(factory.get(self.url, HTTP_IF_NONE_MATCH=etag))
            self.assertEqual(response.status_code, 304)
            self.assertEqual(response.content, b'')

    def test_browser_filters_equal_existing_catalog_including_variants_and_pages(self):
        # 保留同 ID 分支、同名排序、宝库隐藏组和地下堡无效轨道。
        for key, source, stats, level in (
            ('vault', 'great_vault', {'strength': 100, 'versatility': 10}, 730),
            ('delve-invalid', 'delve', {'strength': 100}, 740),
            ('dungeon', 'mythic_plus', {'strength': 100, 'mastery': 10}, 715),
        ):
            WowItemVariantSnapshot.objects.create(item=self.helm, season=self.season, batch_key='test-batch',
                variant_key=key, variant_type='drop_equipment', item_level=level,
                upgrade_track='myth', compatible_slots=['head'], source_json=[{'type': source}], stats_json=stats)
        self.crafted_item.name_zh = self.helm.name_zh
        self.crafted_item.save(update_fields=['name_zh'])
        WowItemVariantSnapshot.objects.create(item=self.helm, season=self.season, batch_key='test-batch',
            data_branch='ptr', variant_key='ptr-head', variant_type='drop_equipment', item_level=735,
            compatible_slots=['head'], source_json=[{'type': 'raid'}], stats_json={'strength': 1100},
            metadata={'data_branch': 'ptr'})
        self.build()
        payload = self.body(self.client.get(self.url))
        cases = [{}, {'source_type': 'raid'}, {'source_type': 'crafted'}, {'source_type': 'great_vault'},
                 {'excluded_sources': ['mythic_plus']}, {'excluded_sources': ['raid']},
                 {'excluded_stats': ['crit']}, {'excluded_stats': ['versatility', 'mastery']},
                 {'query': '裂隙'}, {'query': 'RIFT'}, {'query': '00010001'}, {'query': '不存在'},
                 {'page': 2, 'page_size': 1}, {'page': 9, 'page_size': 1},
                 {'source_type': 'raid', 'excluded_stats': ['crit']}, {'source_type': '无此来源'}]
        requests, expected = [], []
        names = {'source_type': 'source', 'excluded_sources': 'excludedSources',
                 'excluded_stats': 'excludedStats', 'page_size': 'pageSize'}
        for case in cases:
            result = gb.catalog_items(**self.key, **case)
            expected.append({'items': result['items'], 'total': result['total'], 'snapshot': payload['snapshot']})
            requests.append({'payload': payload, 'options': {names.get(k, k): v for k, v in case.items()}})
        process = subprocess.run(['node', str(Path(settings.BASE_DIR) / 'botend/tests/js/gear-catalog-filter-oracle.mjs')],
                                 input=json.dumps(requests), text=True, encoding='utf-8', capture_output=True, check=True)
        self.assertEqual(json.loads(process.stdout), expected)

    def test_failed_build_preserves_previous_index_and_pending_request(self):
        index = self.build()
        with self.assertLogs(snapshots.logger, level='ERROR'), patch.object(gb, 'catalog_snapshot_items', side_effect=RuntimeError('测试构建失败')):
            self.assertEqual(snapshots.refresh_catalog_snapshots(force=True), [])
        self.assertEqual(snapshots._index(self.key), index)
        self.assertTrue((snapshots._directory(self.key) / 'request.json').exists())
        self.assertEqual(snapshots.refresh_catalog_snapshots(force=True), [self.key])

    def test_source_changes_during_build_do_not_publish_mixed_data(self):
        index = self.build()
        original = gb.catalog_snapshot_items
        def changing(**kwargs):
            payload = original(**kwargs)
            self.hero.item_level += 1
            self.hero.save(update_fields=['item_level', 'updated_at'])
            return payload
        with patch.object(gb, 'catalog_snapshot_items', side_effect=changing):
            self.assertEqual(snapshots.refresh_catalog_snapshots(force=True), [])
        self.assertEqual(snapshots._index(self.key), index)
        self.assertEqual(snapshots.refresh_catalog_snapshots(), [self.key])

    def test_metadata_update_and_batch_switch_rebuild(self):
        old = self.build()
        self.helm.name_zh = '刷新后的头盔'
        self.helm.updated_at = timezone.now()
        self.helm.save(update_fields=['name_zh', 'updated_at'])
        self.assertEqual(snapshots.refresh_catalog_snapshots(), [self.key])
        payload = self.body(self.client.get(self.url))
        self.assertIn('刷新后的头盔', [row['name'] for row in payload['items']])
        self.assertNotEqual(snapshots._index(self.key)['generation'], old['generation'])
        self.season.gear_batch_key = 'new-empty-batch'
        self.season.save(update_fields=['gear_batch_key'])
        self.assertEqual(snapshots.refresh_catalog_snapshots(), [self.key])
        self.assertEqual(self.body(self.client.get(self.url))['items'], [])

    def test_corrupt_index_or_truncated_file_rebuilds_without_database_fallback(self):
        index = self.build()
        path = snapshots._directory(self.key) / index['file']
        path.write_text('{', encoding='utf-8')
        self.assertEqual(self.client.get(self.url).status_code, 202)
        self.assertEqual(snapshots.refresh_catalog_snapshots(), [self.key])
        snapshots._write(snapshots._directory(self.key) / 'index.json', {'file': '../../escape.json'})
        self.assertEqual(self.client.get(self.url).status_code, 202)
        self.assertEqual(snapshots.refresh_catalog_snapshots(), [self.key])

    def test_invalid_coordinates_are_not_enqueued_and_command_prewarms(self):
        self.assertEqual(self.client.get(self.url.replace('slot=head', 'slot=../../escape')).status_code, 400)
        self.assertEqual(list(self.directory.glob('*/request.json')), [])
        call_command('refresh_gear_catalog_snapshots', class_name='Warrior', spec_name='Fury', slot='head')
        self.assertIsNotNone(snapshots._index(self.key))

    def test_warm_worker_does_not_rebuild_and_competing_worker_skips(self):
        self.build()
        with patch.object(gb, 'catalog_snapshot_items', side_effect=AssertionError('不得重复构建')):
            self.assertEqual(snapshots.refresh_catalog_snapshots(), [])
            with snapshots._lock(self.directory / 'worker.lock'):
                self.assertEqual(snapshots.refresh_catalog_snapshots(force=True), [])

    def test_cost_is_moved_out_of_request(self):
        with CaptureQueriesContext(connection) as before:
            gb.catalog_items(**self.key)
        with CaptureQueriesContext(connection) as build:
            index = self.build()
        from django.test import RequestFactory
        with CaptureQueriesContext(connection) as after:
            payload = self.body(snapshots.catalog_snapshot_response(RequestFactory().get(self.url)))
        self.assertEqual(len(after), 0)
        print(f'目录快照对照：原请求 {len(before)} 次查询，后台构建 {len(build)} 次，文件读取 {len(after)} 次；'
              f'{len(payload["items"])} 件装备，{index["bytes"]} 字节。')

    def test_gzip_and_plain_representations_have_separate_etags(self):
        self.build()
        plain = self.client.get(self.url)
        plain_etag = plain['ETag']
        expected = self.body(plain)
        compressed = self.client.get(self.url, HTTP_ACCEPT_ENCODING='gzip, deflate, br')
        self.assertEqual(compressed['Content-Encoding'], 'gzip')
        self.assertEqual(compressed['Vary'], 'Accept-Encoding')
        self.assertNotEqual(compressed['ETag'], plain_etag)
        try:
            self.assertEqual(json.loads(gzip.decompress(b''.join(compressed.streaming_content))), expected)
        finally:
            compressed.close()
        disabled = self.client.get(self.url, HTTP_ACCEPT_ENCODING='gzip;q=0')
        self.assertNotIn('Content-Encoding', disabled)
        disabled.close()

    def test_explicit_invalidation_waits_for_commit(self):
        self.build()
        with self.captureOnCommitCallbacks(execute=True):
            snapshots.invalidate_catalog_snapshots()
            self.assertFalse((self.directory / 'revision.json').exists())
        self.assertEqual(snapshots.refresh_catalog_snapshots(), [self.key])

    def test_identical_content_reuses_generation_and_files(self):
        original = self.build()
        paths = set(snapshots._directory(self.key).glob('*.json.gz'))
        self.assertEqual(snapshots.refresh_catalog_snapshots(force=True), [self.key])
        self.assertEqual(snapshots._index(self.key)['generation'], original['generation'])
        self.assertEqual(set(snapshots._directory(self.key).glob('*.json.gz')), paths)

    def test_one_failed_coordinate_does_not_block_other_coordinates(self):
        second = snapshots.coordinate('Warrior', 'Fury', 'neck')
        snapshots.request_refresh(self.key)
        snapshots.request_refresh(second)
        original = gb.catalog_snapshot_items
        def build(**kwargs):
            if kwargs['slot'] == 'head':
                raise RuntimeError('测试单分片失败')
            return original(**kwargs)
        with self.assertLogs(snapshots.logger, level='ERROR'), patch.object(gb, 'catalog_snapshot_items', side_effect=build):
            self.assertEqual(snapshots.refresh_catalog_snapshots(batch_size=2), [second])
        self.assertIsNotNone(snapshots._index(second))
