"""用本机HTTP验证局部报告故障、持久化退避和全局熔断。"""
from botend.tests.test_wcl_raw_staging import RawStagingIsolationMixin, persisted_event
from botend.models import WclCombatantFact
from botend.services.wcl_combatant_store import canonical_event

import time
import threading
from io import StringIO
from datetime import timedelta
from unittest.mock import patch

from django.db import DatabaseError
from django.core.management import call_command
from django.test import TransactionTestCase
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import WclCombatantFetchState, WclCombatantSnapshot
from botend.services import wcl_combatant_snapshot as service
from botend.services.wcl_collection_cycle import begin_collection, finish_collection
from botend.tests.test_wcl_combatant_pool import local_api, page, sample


class TransientReportTests(RawStagingIsolationMixin, TransactionTestCase):
    def fetcher(self):
        fetcher = SpecDetailBase(None, None)
        fetcher._wcl_token = 'local-fixture-token'
        fetcher._wcl_token_expire = time.time()+3600
        return fetcher

    def collect(self, rows, fetcher=None):
        with patch.object(service, '_collection_worker_limit', return_value=1):
            result = service.collect_dungeon_combatants(rows, fetcher or self.fetcher(), apply=True)
        self.assertEqual(result['selected'], sum(result[key] for key in (
            'cached', 'matched', 'missing', 'permission_denied', 'failed', 'pending')))
        return result

    def test_one_503_defers_only_its_report_and_resumes_after_deadline(self):
        rows = [sample('Broken', fight) for fight in range(1, 42)] + [sample('Good'), sample('AlsoGood')]
        def respond(body):
            return (503, {'error': 'http_503'}) if body['code'] == 'Broken' else (200, page(body['fightIDs']))
        with local_api(respond) as (calls, errors):
            result = self.collect(rows)
        self.assertEqual(errors, [])
        self.assertEqual(result['matched'], 2)
        self.assertEqual(result['pending'], 41)
        self.assertEqual(result['failed'], 0)
        self.assertEqual([call['code'] for call in calls], ['Broken', 'Good', 'AlsoGood'])
        state = WclCombatantFetchState.objects.get(report_code='Broken', fight_id=0)
        self.assertEqual(state.status, 'transient_error')
        self.assertGreaterEqual(state.retry_after-state.checked_at, timedelta(minutes=5))
        with local_api(respond) as (calls, _):
            repeated = self.collect(rows)
        self.assertEqual(calls, [])
        self.assertEqual((repeated['cached'], repeated['pending'], repeated['transient_cached']), (2, 41, 41))
        self.assertEqual(repeated['retry_at'], state.retry_after.isoformat())
        with patch('django.utils.timezone.now', return_value=state.retry_after+timedelta(seconds=1)), \
                local_api(lambda body: (200, page(body['fightIDs']))) as (calls, errors):
            recovered = self.collect(rows)
        self.assertEqual(errors, [])
        self.assertEqual([call['code'] for call in calls], ['Broken']*3)
        self.assertEqual((recovered['cached'], recovered['matched'], recovered['pending']), (2, 41, 0))
        self.assertFalse(WclCombatantFetchState.objects.exists())

    def test_connection_closed_does_not_stop_healthy_report(self):
        def respond(body):
            return (0, {}) if body['code'] == 'Disconnected' else (200, page(body['fightIDs']))
        with local_api(respond) as (calls, errors):
            result = self.collect([sample('Disconnected'), sample('Healthy')])
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 2)
        self.assertEqual((result['matched'], result['pending'], result['failed']), (1, 1, 0))
        self.assertEqual(result['transient_errors'], 1)
        self.assertEqual(WclCombatantFetchState.objects.get().status, 'transient_error')

    def test_later_page_503_never_publishes_partial_facts(self):
        def respond(body):
            if body['code'] == 'Paged':
                return (200, page(body['fightIDs'], cursor=100)) if body['start'] is None else (503, {'error': 'http_503'})
            return 200, page(body['fightIDs'])
        with local_api(respond) as (calls, errors):
            result = self.collect([sample('Paged'), sample('Healthy')])
        self.assertEqual(errors, [])
        self.assertEqual([call['code'] for call in calls], ['Paged', 'Paged', 'Healthy'])
        self.assertEqual((result['matched'], result['pending']), (1, 1))
        self.assertFalse(WclCombatantFact.objects.filter(report_code='Paged').exists())
        self.assertFalse(WclCombatantFetchState.objects.filter(status='missing').exists())

    def test_three_consecutive_reports_open_circuit_but_allow_publication_before_repair(self):
        from botend.models import SeasonMeta
        season = SeasonMeta.objects.create(season_key='transient', season_name='测试',
                                          mplus_zone_id=1, raid_zone_id=1)
        cycle = begin_collection(season)
        cycle.phase = 'combatants'
        cycle.save(update_fields=['phase'])
        fetcher = self.fetcher()
        with local_api(lambda body: (503, {'error': 'http_503'})) as (calls, errors):
            result = self.collect([sample(f'Broken{i}') for i in range(8)], fetcher)
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 3)
        self.assertEqual((result['pending'], result['failed'], result['circuit_open']), (8, 0, 1))
        self.assertEqual(result['deferred_reports'], 3)
        self.assertEqual(fetcher._wcl_last_error, 'http_503')
        self.assertEqual(fetcher._wcl_retry_at.isoformat(), result['retry_at'])
        self.assertTrue(finish_collection(cycle, result))
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'ready')
        self.assertIsNone(cycle.next_retry_at)
        from botend.tests.wcl_cycle_helpers import publish_attempt
        publish_attempt(cycle)
        self.assertEqual(cycle.phase, 'combatants')
        self.assertEqual(cycle.next_retry_at, fetcher._wcl_retry_at)

    def test_success_resets_consecutive_failure_count(self):
        def respond(body):
            return (200, page(body['fightIDs'])) if body['code'].startswith('Good') else (502, {'error': 'http_502'})
        rows = [sample(code) for code in ('A', 'B', 'Good1', 'C', 'D', 'Good2')]
        fetcher = self.fetcher()
        with local_api(respond) as (calls, errors):
            result = self.collect(rows, fetcher)
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 6)
        self.assertEqual((result['matched'], result['pending'], result['circuit_open']), (2, 4, 0))
        self.assertEqual(fetcher._wcl_last_error, '')

    def test_global_rate_limit_deadline_overrides_later_local_deadline(self):
        now = timezone.now()
        class Legacy:
            def fetch_wcl_combatant_info(self, code, batch):
                self._wcl_last_error = 'http_503' if code == 'Local' else 'rate_limit'
                self._wcl_retry_at = now + timedelta(minutes=30 if code == 'Local' else 10)
                return None
        result = service.collect_dungeon_combatants(
            [sample('Local'), sample('Limited'), sample('Unsent')], Legacy(), apply=True)
        self.assertEqual(result['reports_requested'], 2)
        self.assertEqual(result['pending'], 3)
        self.assertEqual(result['retry_at'], (now+timedelta(minutes=10)).isoformat())
        self.assertEqual(WclCombatantFetchState.objects.get().retry_after, now+timedelta(minutes=30))

    def test_earliest_local_deadline_and_positive_fact_priority_survive_new_collector(self):
        now = timezone.now()
        WclCombatantFetchState.objects.bulk_create([
            WclCombatantFetchState(report_code=code, fight_id=0, status='transient_error',
                                  checked_at=now, retry_after=now+timedelta(minutes=minutes))
            for code, minutes in [('A', 10), ('B', 20), ('Cached', 30)]])
        from botend.tests.test_wcl_combatant_snapshot import event
        WclCombatantSnapshot.objects.create(report_code='Cached', fight_id=11, actor_id=232,
                                            payload_json=event())
        rows = [sample(code) for code in ('A', 'B', 'Cached', 'Healthy')]
        with local_api(lambda body: (200, page(body['fightIDs']))) as (calls, errors):
            result = self.collect(rows)
        self.assertEqual(errors, [])
        self.assertEqual([c['code'] for c in calls], ['Healthy'])
        self.assertEqual((result['cached'], result['matched'], result['pending']), (1, 1, 2))
        self.assertEqual(result['retry_at'], (now+timedelta(minutes=10)).isoformat())
        with patch('django.utils.timezone.now', return_value=now+timedelta(minutes=11)), \
                local_api(lambda body: (200, page(body['fightIDs']))) as (calls, _):
            result = self.collect(rows)
        self.assertEqual([c['code'] for c in calls], ['A'])
        self.assertEqual((result['cached'], result['matched'], result['pending']), (2, 1, 1))
        self.assertEqual(result['retry_at'], (now+timedelta(minutes=20)).isoformat())

    def test_active_permission_is_not_downgraded_by_transient_write(self):
        now = timezone.now()
        state = WclCombatantFetchState.objects.create(report_code='Private', fight_id=0,
            status='permission_denied', checked_at=now, retry_after=now+timedelta(hours=1))
        self.assertEqual(service._defer_transient_report('Private'), state.retry_after)
        state.refresh_from_db()
        self.assertEqual(state.status, 'permission_denied')
        self.assertEqual(state.checked_at, now)

    def test_storage_failure_while_deferring_stops_other_reports(self):
        fetcher = self.fetcher()
        with local_api(lambda body: (503, {'error': 'http_503'})) as (calls, _), \
                patch.object(service, '_defer_transient_report', side_effect=DatabaseError('测试写入失败')):
            result = self.collect([sample('Failed'), sample('Unsent')], fetcher)
        self.assertEqual(len(calls), 1)
        self.assertEqual((result['failed'], result['pending']), (1, 1))
        self.assertEqual(fetcher._wcl_last_error, 'storage_error')

    def test_local_failure_preserves_inflight_success_and_continues_dispatch(self):
        barrier = threading.Barrier(2, timeout=4)
        def respond(body):
            if body['code'] != 'Later':
                barrier.wait()
            if body['code'] == 'Broken':
                return 503, {'error': 'http_503'}
            time.sleep(0.05)
            return 200, page(body['fightIDs'])
        with local_api(respond) as (calls, errors), \
                patch.object(service, '_collection_worker_limit', return_value=2):
            result = service.collect_dungeon_combatants(
                [sample('Broken'), sample('Good'), sample('Later')], self.fetcher(), apply=True)
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 3)
        self.assertEqual((result['matched'], result['pending'], result['failed']), (2, 1, 0))

    def test_status_migration_has_no_table_or_column_ddl(self):
        out = StringIO()
        call_command('sqlmigrate', 'botend', '0271_wcl_transient_report_state', stdout=out)
        sql = out.getvalue().upper()
        self.assertNotIn('ALTER TABLE', sql)
        self.assertNotIn('CREATE TABLE', sql)
        self.assertNotIn('DROP TABLE', sql)
        self.assertIn('transient_error', dict(WclCombatantFetchState._meta.get_field('status').choices))

    def test_mixed_completion_order_does_not_falsely_open_circuit(self):
        # 主线程等待所有响应入队，再验证按实际接收顺序处理，而非遍历无序集合。
        barrier = threading.Barrier(4, timeout=4)
        received = []
        sequence = {'A': 0.01, 'Good': 0.05, 'B': 0.10, 'C': 0.15}
        def respond(body):
            barrier.wait()
            time.sleep(sequence[body['code']])
            return (200, page(body['fightIDs'])) if body['code'] == 'Good' else (503, {'error': 'http_503'})
        original_fetch = service._fetch_combatant_batch
        def fetch(*args, **kwargs):
            result = original_fetch(*args, **kwargs)
            received.append(args[1])
            return result
        from concurrent.futures import ALL_COMPLETED, wait
        def wait_all(futures, **kwargs):
            return wait(futures, timeout=4, return_when=ALL_COMPLETED)
        with local_api(respond) as (calls, errors), \
                patch.object(service, '_collection_worker_limit', return_value=4), \
                patch.object(service, '_fetch_combatant_batch', side_effect=fetch), \
                patch.object(service, 'wait', side_effect=wait_all):
            result = service.collect_dungeon_combatants(
                [sample(code) for code in ('A', 'Good', 'B', 'C')], self.fetcher(), apply=True)
        self.assertEqual(errors, [])
        self.assertEqual(received, ['A', 'Good', 'B', 'C'])
        self.assertEqual((result['matched'], result['pending'], result['circuit_open']), (1, 3, 0))
