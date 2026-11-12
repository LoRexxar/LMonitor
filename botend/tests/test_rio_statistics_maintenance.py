"""存储事务、真实进程中断、并发发布和凌晨恢复回归。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
import subprocess
import sys
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from botend.services import rio_statistics as rio
from botend.services.rio_statistics_dimensions import project_periods, add_period
from botend.services.rio_statistics_storage import StorageError
from botend.services.rio_statistics_schedule import resume_due_at
from botend.tests import test_rio_statistics as fixtures


class RioStorageMaintenanceTests(SimpleTestCase):
    def setUp(self):
        fixtures.RioStatisticsTests.setUp(self)
        self.c = rio.coordinate('overview', {'region': 'cn'})

    def response(self, data):
        return Mock(status_code=200, headers={}, json=Mock(return_value=data))

    def test_legacy_migration_is_lossless_idempotent_and_reads_do_not_initialize(self):
        key = rio.key_for(self.c)
        body = {'schema': 1, 'coordinate': self.c, 'fetched_at': timezone.now().isoformat(),
                'upstream_at': None, 'data': {'value': 10}}
        rio._write(self.directory / (key + '.json'), body)
        rio._write(self.directory / 'index.json', {'schema': 1, 'entries': {key: {'coordinate': self.c}}})
        before = {p.name: p.read_bytes() for p in self.directory.glob('*.json')}
        self.assertEqual(rio.read_entry(self.c)['data'], {'value': 10})
        self.assertFalse(rio.store().path.exists())
        rio.store().ensure(); rio.store().ensure()
        self.assertEqual(rio.read_entry(self.c)['data'], {'value': 10})
        self.assertEqual(rio.store().audit(deep=True)['entries'], 1)
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.glob('*.json')}, before)

    def test_orphan_body_from_old_interrupted_publish_is_recovered(self):
        key = rio.key_for(self.c)
        rio._write(self.directory / (key + '.json'), {'schema': 1, 'coordinate': self.c,
                  'fetched_at': timezone.now().isoformat(), 'upstream_at': None, 'data': {'value': 10}})
        rio.store().ensure()
        self.assertEqual(rio.read_entry(self.c)['data']['value'], 10)

    def test_invalid_legacy_index_does_not_get_replaced_with_empty_cache(self):
        path = self.directory / 'index.json'; path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(StorageError): rio.store().ensure()
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')
        self.assertFalse(rio.store().path.exists())

    def test_body_index_and_history_rollback_together(self):
        rio._publish(self.c, {'value': 1})
        old = rio.read_entry(self.c)
        with patch.object(rio.store(), '_history', side_effect=RuntimeError('模拟事务中断')):
            with self.assertRaises(RuntimeError): rio._publish(self.c, {'value': 2})
        result = rio.read_entry(self.c)
        self.assertEqual(result['data'], old['data'])
        self.assertEqual(result['fetched_at'], old['fetched_at'])
        self.assertEqual(rio.store().audit(deep=True)['history_entries'], 1)
        self.assertFalse((self.directory / 'index.json').exists())

    def test_hard_process_exit_before_commit_restores_old_snapshot(self):
        rio._publish(self.c, {'value': 1})
        code = "import sqlite3,sys,os; db=sqlite3.connect(sys.argv[1]); db.execute('BEGIN IMMEDIATE'); db.execute('UPDATE snapshots SET payload=? WHERE key=?',(b\"broken\",sys.argv[2])); os._exit(7)"
        result = subprocess.run([sys.executable, '-c', code, str(rio.store().path), rio.key_for(self.c)],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(rio.read_entry(self.c)['data'], {'value': 1})
        self.assertEqual(rio.store().audit(deep=True)['state'], 'ok')

    def test_concurrent_imports_cannot_replace_new_data_with_older_source(self):
        now = timezone.now()
        rio._publish(self.c, {'value': 0}, upstream_at=(now - timedelta(minutes=5)).isoformat())
        def publish(offset, value):
            return rio._publish(self.c, {'value': value}, upstream_at=(now - timedelta(minutes=offset)).isoformat(), newer_only=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(publish, 3, 1), pool.submit(publish, 2, 2)]
            for future in futures: future.result(timeout=15)
        self.assertEqual(rio.read_entry(self.c)['data'], {'value': 2})

    def test_readers_see_consistent_body_and_index_during_writes(self):
        rio._publish(self.c, {'counter': 0})
        def writer():
            for value in range(1, 30): rio._publish(self.c, {'counter': value})
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            for _ in range(40):
                body, entry = rio.store().read(self.c)
                self.assertEqual(body['fetched_at'], entry['fetched_at'])
            future.result(timeout=15)
        self.assertEqual(rio.read_entry(self.c)['data']['counter'], 29)

    def test_compressed_latest_payload_and_small_daily_history(self):
        for day in range(1, 4):
            now = datetime(2026, 11, day, 2, tzinfo=ZoneInfo('Asia/Shanghai'))
            with patch.object(rio.timezone, 'now', return_value=now):
                rio._publish(self.c, {'value': '重复数据' * 10000})
        report = rio.store().audit(deep=True)
        self.assertEqual(report['entries'], 1)
        self.assertEqual(report['history_entries'], 3)
        self.assertLess(report['compressed_bytes'], report['raw_bytes'] / 10)

    def test_capacity_failure_preserves_valid_data_and_stops_before_network(self):
        rio._publish(self.c, {'value': 1})
        transport = Mock()
        with self.settings(RIO_STATISTICS_MAX_STORAGE_BYTES=1):
            with self.assertRaises(StorageError): rio._publish(self.c, {'value': 2})
            with self.assertRaises(StorageError): rio.Collector(transport=transport).run(modules=['overview'])
        transport.assert_not_called()
        self.assertEqual(rio.read_entry(self.c)['data'], {'value': 1})

    def test_checksum_failure_is_not_served_and_backup_can_restore_it(self):
        rio._publish(self.c, {'value': 1})
        original_time = rio.read_entry(self.c)['fetched_at']
        rio.store().backup()
        with rio.store().connection() as db:
            db.execute('UPDATE snapshots SET payload=? WHERE key=?', (b'broken', rio.key_for(self.c)))
        self.assertEqual(rio.read_entry(self.c)['snapshot']['state'], 'error')
        report = rio.store().audit(deep=True)
        self.assertEqual(report['invalid_payloads'], [rio.key_for(self.c)])
        with self.assertRaises(StorageError): rio.store().backup()
        self.assertEqual(rio.store().repair_from_backups(report), 1)
        result = rio.read_entry(self.c)
        self.assertEqual(result['data']['value'], 1)
        self.assertEqual(result['fetched_at'], original_time)
        self.assertEqual(result['snapshot']['state'], 'stale')

    def test_backup_uses_seven_fixed_slots(self):
        rio._publish(self.c, {'value': 1})
        for index in range(8):
            now = datetime(2026, 11, 1, 2, tzinfo=ZoneInfo('Asia/Shanghai')) + timedelta(days=index)
            with patch.object(rio.timezone, 'now', return_value=now): rio.store().backup()
        self.assertEqual(len(list((self.directory / 'backups').glob('slot-*.sqlite3'))), 7)

    def test_abandoned_cycle_is_recovered_before_new_collection(self):
        old_id = rio.store().cycle({'state': 'running', 'ready': 1})
        result = rio.Collector(transport=Mock(return_value=self.response(fixtures.OVERVIEW))).run(modules=['overview'], regions=['cn'])
        with rio.store().connection() as db:
            row = db.execute('SELECT * FROM cycles WHERE id=?', (old_id,)).fetchone()
        self.assertEqual(row['state'], 'interrupted')
        self.assertNotEqual(result['cycle_id'], old_id)

    def test_completed_statistics_with_unfinished_maps_is_partial(self):
        result = rio.Collector(max_requests=4, transport=Mock(return_value=self.response(fixtures.GRID))).run(
            modules=['heatmaps'], regions=['cn'], levels=['10-12'], periods=['season'])
        self.assertEqual(result['remaining'], 0)
        self.assertEqual(result['maps_remaining'], 1)
        self.assertEqual(result['state'], 'partial')

    def test_deferred_failed_combination_cannot_be_reported_complete(self):
        rio._failure(self.c, '来源暂未就绪')
        transport = Mock()
        result = rio.Collector(transport=transport).run(modules=['overview'], regions=['cn'])
        transport.assert_not_called()
        self.assertEqual((result['state'], result['remaining']), ('partial', 1))

    def test_pending_import_cannot_be_reported_complete(self):
        with patch('botend.services.rio_statistics_runs.import_feeds', return_value={'imported': 0, 'failed': 0, 'pending': 1}):
            result = rio.Collector(transport=Mock()).run(modules=['runs'], regions=['cn'])
        self.assertEqual((result['state'], result['runs_pending']), ('partial', 1))

    def test_missing_automatic_run_source_is_explicit_health_warning(self):
        from botend.services.rio_statistics_health import check_health
        with patch('botend.services.rio_statistics_health.active_scope', return_value=self.scope):
            result = check_health(deep=True, initialize=True, backup=True)
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(result['runs_source_mode'], 'file-import')
        self.assertIn('完成记录尚未配置自动上游来源，无法保证每日产生新统计', result['issues'])
        self.assertIsNone(result['last_nightly'])

    def test_persistent_connection_failure_opens_circuit_instead_of_exhausting_budget(self):
        import requests
        transport = Mock(side_effect=requests.Timeout('来源不可用'))
        result = rio.Collector(max_requests=100, transport=transport).run(modules=['heatmaps'], regions=['cn'], periods=['season'])
        grid_requests = [call for call in transport.call_args_list if '/heatmaps/grid' in call.args[0]]
        self.assertEqual(len(grid_requests), 6)
        self.assertEqual(transport.call_count, 8)  # 三范围预算足够，先尝试底图及一次连接重试。
        self.assertEqual((result['state'], result['failed']), ('backoff', 3))
        self.assertTrue((self.directory / 'cooldown.json').is_file())

    def test_failed_combinations_are_retryable_before_next_midnight(self):
        import requests
        start = datetime(2026, 11, 5, 8, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.meta['updated_at'] = start.isoformat()
        rio._write(self.directory / 'catalog.json', self.meta)
        bad = self.response({}); bad.status_code = 503
        bad.raise_for_status.side_effect = requests.HTTPError(response=bad)
        with patch.object(rio.timezone, 'now', return_value=start):
            result = rio.Collector(transport=Mock(return_value=bad)).run(modules=['overview'], regions=['cn', 'world'])
        self.assertEqual(result['failed'], 2)
        def response(url, **kwargs):
            return self.response(dict(fixtures.OVERVIEW, region=kwargs['params']['region']))
        with patch.object(rio.timezone, 'now', return_value=start + timedelta(minutes=31)):
            result = rio.Collector(transport=Mock(side_effect=response)).run(modules=['overview'], regions=['cn', 'world'])
        self.assertEqual(result['ready'], 2)

    def test_daytime_retry_fetches_only_failed_coordinate(self):
        import requests
        start = datetime(2026, 11, 5, 5, 40, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.meta['updated_at'] = start.isoformat()
        rio._write(self.directory / 'catalog.json', self.meta)
        bad = self.response({})
        bad.status_code = 503
        bad.raise_for_status.side_effect = requests.HTTPError(response=bad)

        def first_response(url, **kwargs):
            region = kwargs['params']['region']
            return bad if region == 'world' else self.response(dict(fixtures.OVERVIEW, region=region))

        with patch.object(rio.timezone, 'now', return_value=start):
            first = rio.Collector(transport=Mock(side_effect=first_response)).run(modules=['overview'], regions=['cn', 'world'])
        self.assertEqual((first['ready'], first['failed']), (1, 1))
        transport = Mock(side_effect=lambda url, **kwargs:
                         self.response(dict(fixtures.OVERVIEW, region=kwargs['params']['region'])))
        with patch.object(rio.timezone, 'now', return_value=start + timedelta(minutes=31)):
            resumed = rio.Collector(transport=transport).run(modules=['overview'], regions=['cn', 'world'])
        self.assertEqual((resumed['ready'], resumed['failed']), (1, 0))
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.kwargs['params']['region'], 'world')

    def test_rate_limit_still_allows_daily_storage_check_and_backup(self):
        rio._publish(self.c, {'value': 1})
        rio._write(self.directory / 'cooldown.json', {'until': (timezone.now() + timedelta(hours=1)).isoformat()})
        transport = Mock()
        result = rio.Collector(transport=transport).run(modules=['overview'], maintain=True)
        transport.assert_not_called()
        self.assertEqual(result['state'], 'backoff')
        self.assertEqual(rio.store().audit()['maintenance']['backup']['day'], timezone.now().astimezone(rio.LOCAL_TZ).date().isoformat())

    def test_metadata_failure_still_checks_and_backs_up_local_data(self):
        rio._publish(self.c, {'value': 1})
        collector = rio.Collector(transport=Mock())
        with patch.object(collector, 'metadata', side_effect=ValueError('来源元数据无效')):
            with self.assertRaises(ValueError): collector.run(maintain=True)
        report = rio.store().audit(deep=True)
        self.assertEqual(report['last_nightly']['state'], 'failed')
        self.assertIn('backup', report['maintenance'])
        self.assertEqual(rio.read_entry(self.c)['data']['value'], 1)

    def test_sync_command_reports_partial_as_failure_by_default(self):
        from io import StringIO
        with patch('botend.management.commands.sync_rio_statistics.Collector.run', return_value={'state': 'partial', 'ready': 1, 'remaining': 2}):
            with self.assertRaises(CommandError): call_command('sync_rio_statistics', stdout=StringIO())
            call_command('sync_rio_statistics', allow_partial=True, stdout=StringIO())

    def test_period_projection_crosses_reset_without_relabeling_old_week(self):
        meta = deepcopy(self.meta)
        meta['periods'] = [{'region': 'cn',
            'previous': {'period': 1086, 'start': '2026-10-21T23:00:00Z', 'end': '2026-10-28T23:00:00Z'},
            'current': {'period': 1087, 'start': '2026-10-28T23:00:00Z', 'end': '2026-11-04T23:00:00Z'},
            'next': {'period': 1088, 'start': '2026-11-04T23:00:00Z', 'end': '2026-11-11T23:00:00Z'}}]
        before = project_periods(meta, datetime.fromisoformat('2026-11-04T22:59:00+00:00'))
        after = project_periods(meta, datetime.fromisoformat('2026-11-04T23:01:00+00:00'))
        c1 = {'region': 'cn'}; add_period(c1, {'period': 'last-reset'}, before)
        c2 = {'region': 'cn'}; add_period(c2, {'period': 'last-reset'}, after)
        self.assertEqual((c1['period_id'], c2['period_id']), (1086, 1087))
        too_old = project_periods(meta, datetime.fromisoformat('2026-11-12T00:00:00+00:00'))
        with self.assertRaises(ValueError): add_period({'region': 'cn'}, {'period': 'last-reset'}, too_old)


class RioNightResumeIntegrationTests(TestCase):
    def setUp(self):
        from botend.models import MonitorTask, SeasonMeta
        from botend.services.rio_statistics_scope import active_scope
        SeasonMeta.objects.all().delete()
        SeasonMeta.objects.create(season_key='mn-s2', season_name='午夜第二赛季',
            rio_season='season-mn-2', game_build='12.1.5.70000', is_active=True,
            mplus_zone_id=1, raid_zone_id=2)
        self.identity = '2026-11-05@' + active_scope()['token']
        MonitorTask.objects.update(is_active=False)
        self.now = datetime(2026, 11, 5, 1, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.task = MonitorTask.objects.create(name='RioStatisticsMonitor', type=980, target='', is_active=True,
                                              last_scan_time=self.now - timedelta(days=1), flag='')

    def test_expired_claim_without_completion_can_resume_same_night(self):
        from botend.plugin_sync import claim_next_monitor_task
        first = claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=self.now, lease_owner='old')
        self.assertEqual(first.pk, self.task.pk)
        self.assertIsNone(claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=self.now + timedelta(minutes=20), lease_owner='new'))
        resumed = claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=self.now + timedelta(minutes=31), lease_owner='new')
        self.assertEqual(resumed.pk, self.task.pk)

    def test_partial_waits_for_retry_then_complete_is_not_repeated(self):
        from botend.plugin_sync import claim_next_monitor_task, complete_monitor_task_lease
        first = claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=self.now, lease_owner='one')
        flag = json.dumps({'identity': self.identity, 'state': 'partial', 'retry_at': (self.now + timedelta(minutes=32)).isoformat()})
        self.assertTrue(complete_monitor_task_lease(first.pk, 'one', now=self.now + timedelta(minutes=2), task_updates={'flag': flag}))
        self.assertIsNone(claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=self.now + timedelta(minutes=31), lease_owner='two'))
        second = claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=self.now + timedelta(minutes=33), lease_owner='two')
        flag = json.dumps({'identity': self.identity, 'state': 'complete'})
        complete_monitor_task_lease(second.pk, 'two', now=self.now + timedelta(minutes=34), task_updates={'flag': flag})
        self.assertIsNone(claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=self.now + timedelta(hours=2), lease_owner='three'))

    def test_daytime_only_resumes_started_batch(self):
        from botend.plugin_sync import claim_next_monitor_task
        self.assertIsNone(claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=self.now.replace(hour=8)))
        self.task.last_scan_time = self.now
        self.task.save(update_fields=['last_scan_time'])
        claimed = claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=self.now.replace(hour=8), lease_owner='day')
        self.assertEqual(claimed.pk, self.task.pk)

    def test_partial_resumes_after_six_with_cooldown_and_completion_guard(self):
        from botend.plugin_sync import claim_next_monitor_task, complete_monitor_task_lease
        start = self.now.replace(hour=5, minute=50)
        retry = start.replace(hour=6, minute=25)
        first = claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=start, lease_owner='night')
        flag = json.dumps({'identity': self.identity, 'state': 'partial', 'retry_at': retry.isoformat()})
        self.assertTrue(complete_monitor_task_lease(first.pk, 'night', now=start + timedelta(minutes=2), task_updates={'flag': flag}))
        self.assertIsNone(claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=retry - timedelta(seconds=1)))
        resumed = claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=retry, lease_owner='day')
        self.assertEqual(resumed.pk, self.task.pk)
        flag = json.dumps({'identity': self.identity, 'state': 'complete'})
        self.assertTrue(complete_monitor_task_lease(resumed.pk, 'day', now=retry + timedelta(minutes=2), task_updates={'flag': flag}))
        self.assertIsNone(claim_next_monitor_task(task_names={'RioStatisticsMonitor'}, now=self.now.replace(hour=12)))
