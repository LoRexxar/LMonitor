"""验证中间文件的持久复用、原子提交、范围隔离和旧表升级兼容。"""
import json
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from botend.models import SeasonMeta, SpecAggregationWorkCache
from botend.services import spec_aggregation_cache as cache
from botend.tests.aggregation_cache_helpers import isolate_work_cache


class AggregationFileCacheTests(TestCase):
    def setUp(self):
        self.root = isolate_work_cache(self)
        self.season = SeasonMeta.objects.create(season_key='文件缓存', season_name='文件缓存',
                                               mplus_zone_id=1, raid_zone_id=2)
        self.scope = 'raid:Mage:Fire:5:1'
        self.payload = {'统计': [{'数值': 123, '天赋': '甲'}]}

    def save(self, fingerprint='输入一', payload=None):
        return cache.save_work(self.season.pk, self.scope, fingerprint,
                               self.payload if payload is None else payload)

    def load(self, fingerprint='输入一'):
        return cache.load_work(self.season.pk, self.scope, fingerprint)

    def test_new_results_and_warm_reads_do_not_touch_database(self):
        with self.assertNumQueries(0):
            self.save()
            self.assertEqual(self.load(), self.payload)
            self.assertIsNone(self.load('输入二'))
        self.assertFalse(SpecAggregationWorkCache.objects.exists())
        envelope = json.loads(cache.work_path(self.season.pk, self.scope).read_text(encoding='utf-8'))
        self.assertEqual(envelope['data']['payload'], self.payload)

    def test_scope_and_season_cannot_share_results(self):
        self.save()
        self.assertIsNone(cache.load_work(self.season.pk, 'raid:Mage:Frost:5:1', '输入一'))
        self.assertIsNone(cache.load_work(self.season.pk + 1, self.scope, '输入一'))
        source = cache.work_path(self.season.pk, self.scope)
        destination = cache.work_path(self.season.pk, 'raid:Mage:Frost:5:1')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        self.assertIsNone(cache.load_work(self.season.pk, 'raid:Mage:Frost:5:1', '输入一'))

    def test_changed_payload_or_fingerprint_is_rejected(self):
        for old, new in (('"天赋":"甲"', '"天赋":"乙"'),
                         ('"fingerprint":"输入一"', '"fingerprint":"输入二"')):
            self.save()
            path = cache.work_path(self.season.pk, self.scope)
            original = path.read_bytes()
            changed = original.replace(old.encode('utf-8'), new.encode('utf-8'))
            self.assertNotEqual(original, changed)
            path.write_bytes(changed)
            self.assertIsNone(self.load('输入二' if 'fingerprint' in old else '输入一'))

    def test_truncated_or_unknown_schema_rebuilds(self):
        self.save()
        path = cache.work_path(self.season.pk, self.scope)
        path.write_text('{未完成', encoding='utf-8')
        self.assertIsNone(self.load())
        with patch.object(cache, 'SCHEMA', 999):
            self.save()
        self.assertIsNone(self.load())
        self.save()
        self.assertEqual(self.load(), self.payload)

    def test_legacy_result_is_adopted_once_without_deleting_old_row(self):
        row = SpecAggregationWorkCache.objects.create(season=self.season, scope=self.scope,
            fingerprint='输入一', payload=self.payload, payload_hash=cache.content_digest(self.payload))
        with self.assertNumQueries(1):
            self.assertEqual(self.load(), self.payload)
        with self.assertNumQueries(0):
            self.assertEqual(self.load(), self.payload)
            self.save('输入二', {'更新': True})
            self.assertIsNone(self.load())
        row.refresh_from_db()
        self.assertEqual(row.payload, self.payload)

    def test_corrupt_or_outdated_legacy_result_is_not_adopted(self):
        row = SpecAggregationWorkCache.objects.create(season=self.season, scope=self.scope,
            fingerprint='输入二', payload=self.payload, payload_hash=cache.content_digest(self.payload))
        self.assertIsNone(self.load())
        row.fingerprint = '输入一'; row.payload_hash = '损坏'; row.save()
        self.assertIsNone(self.load())
        self.assertFalse(cache.work_path(self.season.pk, self.scope).exists())

    def test_failed_replace_preserves_previous_result_and_does_not_fail_computation(self):
        self.save()
        with patch.object(cache.os, 'replace', side_effect=OSError('模拟磁盘失败')), \
                self.assertLogs(cache.logger, level='WARNING'):
            for _ in range(2):
                self.assertEqual(self.save('输入二', {'新结果': True}), {'新结果': True})
        self.assertEqual(self.load(), self.payload)
        self.assertIsNone(self.load('输入二'))
        self.assertEqual(len(list(self.root.rglob('*.pending'))), 1)
        self.save('输入二', {'新结果': True})
        self.assertEqual(self.load('输入二'), {'新结果': True})

    def test_reader_sees_old_complete_result_until_commit(self):
        self.save()
        replace = os.replace
        def observe(source, destination):
            self.assertEqual(self.load(), self.payload)
            self.assertIsNone(self.load('输入二'))
            replace(source, destination)
        with patch.object(cache.os, 'replace', side_effect=observe):
            self.save('输入二', {'新结果': True})
        self.assertEqual(self.load('输入二'), {'新结果': True})

    def test_failed_flush_never_exposes_uncommitted_data(self):
        self.save()
        with patch.object(cache.os, 'fsync', side_effect=OSError('模拟写盘失败')), \
                self.assertLogs(cache.logger, level='WARNING'):
            self.save('输入二', {'新结果': True})
        self.assertEqual(self.load(), self.payload)
        self.assertIsNone(self.load('输入二'))

    def test_only_latest_result_is_retained_for_each_scope(self):
        for index in range(12):
            self.save(str(index), {'次数': index})
        self.assertEqual(len(list(self.root.rglob('*.json'))), 1)
        self.assertEqual(self.load('11'), {'次数': 11})
        self.assertIsNone(self.load('10'))

    def test_date_decimal_and_empty_payload_survive_serialization(self):
        self.assertEqual(self.save(payload={'日期': date(2026, 10, 21), '数值': Decimal('1.25')}),
                         {'日期': '2026-10-21', '数值': 1.25})
        self.save(payload={})
        self.assertEqual(self.load(), {})
        with self.assertRaises(ValueError):
            self.save(payload={'非法': float('nan')})
        self.assertEqual(self.load(), {})

    def test_public_directory_is_rejected_and_scope_names_are_hashed(self):
        with self.settings(MEDIA_ROOT=self.root):
            with self.assertRaises(ValueError):
                self.save()
        path = cache.work_path(self.season.pk, '../../角色/范围')
        self.assertTrue(path.is_relative_to(self.root))
        self.assertNotIn('角色', str(path))

    def test_battlenet_state_stays_in_database(self):
        scope = 'battlenet:人物身份'
        state = {'stats': {'haste': 100}, 'next_attempt': 123, 'failures': 0}
        cache.save_state(self.season.pk, scope, '版本', state)
        self.assertEqual(cache.load_state(self.season.pk, scope, '版本'), state)
        self.assertEqual(SpecAggregationWorkCache.objects.count(), 1)
        self.assertFalse(list(self.root.rglob('*.json')))
        with self.assertRaises(ValueError):
            cache.save_state(self.season.pk, self.scope, '版本', {})

    def test_multiple_processes_write_complete_files_and_restart_without_database(self):
        self.save()
        code = '''import sys, django
django.setup()
from django.test import override_settings
from unittest.mock import patch
from botend.services import spec_aggregation_cache as cache
with override_settings(SPEC_AGGREGATION_WORK_ROOT=sys.argv[1]):
    with patch.object(cache, '_load_database', side_effect=AssertionError('禁止查询数据库')):
        season = int(sys.argv[2])
        if sys.argv[3] == '读取':
            assert cache.load_work(season, 'raid:Mage:Fire:5:1', '输入一') is not None
        else:
            for i in range(10):
                cache.save_work(season, 'raid:Mage:Fire:5:1', '输入一', {'进程': sys.argv[3], '次数': i})
                assert cache.load_work(season, 'raid:Mage:Fire:5:1', '输入一') is not None
'''
        def command(label):
            return [sys.executable, '-c', code, str(self.root), str(self.season.pk), label]
        env = {**os.environ, 'DJANGO_SETTINGS_MODULE': 'LMonitor.settings_test_sqlite'}
        subprocess.run(command('读取'), check=True, capture_output=True, timeout=30, env=env)
        workers = [subprocess.Popen(command(str(i)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                   for i in range(2)]
        for worker in workers:
            _, error = worker.communicate(timeout=30)
            self.assertEqual(worker.returncode, 0, error.decode(errors='replace'))
        result = self.load()
        self.assertEqual(result['次数'], 9)
        self.assertIn(result['进程'], ['0', '1'])
