"""Real local HTTP barriers -> Base pagination -> collector ORM/cycle.

Only the transport endpoint and worker-limit read are substituted. No concurrent
ORM is hidden behind SQLite: persistence stays on the calling thread; the shared
budget's concurrent MySQL reservation tests are a separate integration boundary.
"""
import json
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import SeasonMeta, WclCollectionCycle, WclCombatantFetchState, WclCombatantSnapshot
from botend.services import wcl_combatant_snapshot as service
from botend.services.wcl_collection_cycle import begin_collection, finish_collection


def sample(code, fight=11, actor=232):
    return dict(report_code=code, fight_id=fight, character_name=f'Player{actor}',
                realm='Burning-Legion', region='eu', class_name='DeathKnight', spec_name='Frost')


def page(fights, *, cursor=None, spec=251):
    return {'data': {'reportData': {'report': {
        'masterData': {'actors': [{'id': 232, 'name': 'Player232', 'server': 'BurningLegion', 'type': 'Player'}]},
        'events': {'data': [dict(type='combatantinfo', fight=fight, sourceID=232, specID=spec,
                                 critMelee=1410) for fight in fights], 'nextPageTimestamp': cursor}}}}}


@contextmanager
def local_api(respond):
    calls, errors = [], []
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with lock:
                calls.append(body)
            try:
                status, payload = respond(body)
            except Exception as exc:
                errors.append(type(exc).__name__)
                status, payload = 500, {'error': 'fixture_failed'}
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    class Server(ThreadingHTTPServer):
        request_queue_size = 32
    server = Server(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def transport(client_id, token, query, variables, **kwargs):
        response = requests.post(f'http://127.0.0.1:{server.server_port}/',
                                 json=variables, timeout=8)
        payload = response.json()
        return SimpleNamespace(payload=payload, error=payload.get('error', ''),
                               retry_at=timezone.now() + timedelta(minutes=5)
                               if payload.get('error') == 'rate_limit' else None)
    try:
        with patch('botend.services.wcl_api_budget.graphql_request', side_effect=transport):
            yield calls, errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


class CombatantPoolTests(TransactionTestCase):
    def fetcher(self):
        fetcher = SpecDetailBase(None, None)
        fetcher._wcl_token = 'local-fixture-token'
        fetcher._wcl_token_expire = time.time() + 3600
        return fetcher

    def collect(self, rows, **kwargs):
        result = service.collect_dungeon_combatants(rows, self.fetcher(), apply=True, **kwargs)
        self.assertEqual(result['selected'], sum(result[key] for key in (
            'cached', 'matched', 'missing', 'permission_denied', 'failed', 'pending')))
        return result

    @override_settings(WCL_V2_CONFIG={'client_id': 'fixture', 'client_secret': 'fixture'})
    def test_expired_token_refresh_is_shared_by_workers(self):
        from concurrent.futures import ThreadPoolExecutor
        fetcher = self.fetcher()
        factory = fetcher.prepare_combatant_worker_factory()
        workers = [factory() for _ in range(10)]
        fetcher._wcl_token_expire = 0
        for worker in workers:
            worker._wcl_token_expire = 0
        barrier = threading.Barrier(10, timeout=4)
        def token(worker):
            barrier.wait()
            return worker._get_wcl_token()
        def oauth(*args, **kwargs):
            time.sleep(0.05)
            return SimpleNamespace(status_code=200, json=lambda: {
                'access_token': 'refreshed-fixture', 'expires_in': 3600})
        with patch('botend.controller.plugins.portal.SpecDetailBase.requests.post', side_effect=oauth) as post:
            with ThreadPoolExecutor(max_workers=10) as pool:
                self.assertEqual(list(pool.map(token, workers)), ['refreshed-fixture'] * 10)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(fetcher._wcl_token, 'refreshed-fixture')

    @override_settings(WCL_V2_CONFIG={'client_id': 'fixture', 'client_secret': 'fixture'})
    def test_failed_refresh_does_not_fan_out_oauth_attempts(self):
        from concurrent.futures import ThreadPoolExecutor
        fetcher = self.fetcher()
        factory = fetcher.prepare_combatant_worker_factory()
        workers = [factory() for _ in range(10)]
        fetcher._wcl_token_expire = 0
        barrier = threading.Barrier(10, timeout=4)
        def token(worker):
            barrier.wait()
            return worker._get_wcl_token()
        with patch('botend.controller.plugins.portal.SpecDetailBase.requests.post',
                   return_value=SimpleNamespace(status_code=401)) as post:
            with ThreadPoolExecutor(max_workers=10) as pool:
                self.assertEqual(list(pool.map(token, workers)), [None] * 10)
        self.assertEqual(post.call_count, 1)

    def test_legacy_token_override_is_not_discarded_by_worker_factory(self):
        fetcher = SpecDetailBase(None, None)
        with patch.object(fetcher, '_get_wcl_token', return_value='fixture-token'), patch(
                'botend.controller.plugins.portal.SpecDetailBase.requests.post',
                side_effect=AssertionError('unexpected OAuth HTTP')) as post:
            self.assertIsNone(fetcher.prepare_combatant_worker_factory())
        post.assert_not_called()

    def test_http_barriers_hot_10_16_1_persist_cycle_and_zero_http_warm(self):
        limit = [10]
        barriers = {size: threading.Barrier(size, timeout=4,
                    action=lambda size=size: limit.__setitem__(0, 16 if size == 10 else 1))
                    for size in (10, 16)}
        active = [0, 0]
        shrink_peaks = []
        lock = threading.Lock()
        def respond(body):
            number = int(body['code'][1:])
            with lock:
                active[0] += 1
                active[1] = max(active)
                if number >= 26:
                    shrink_peaks.append(active[0])
            if number < 26:
                barriers[10 if number < 10 else 16].wait()
            else:
                time.sleep(0.02)
            with lock:
                active[0] -= 1
            return 200, page(body['fightIDs'])
        rows = [sample(f'R{i}') for i in range(29)]
        season = SeasonMeta.objects.create(season_key='pool', season_name='pool', mplus_zone_id=1, raid_zone_id=2)
        cycle = begin_collection(season)
        cycle.phase = 'combatants'
        cycle.save(update_fields=['phase'])
        logs, progress = [], []
        with local_api(respond) as (calls, errors), patch.object(
                service, '_collection_worker_limit', side_effect=lambda fetcher: limit[0], create=True):
            result = self.collect(rows, log=logs.append, progress=progress.append)
            warm = self.collect(rows)
            dry = service.collect_dungeon_combatants(rows + [sample('Unrequested')], self.fetcher())
        self.assertEqual(errors, [])
        self.assertEqual(active[1], 16)
        self.assertEqual(len(calls), 29)
        self.assertEqual(shrink_peaks, [1, 1, 1])
        self.assertEqual((result['matched'], result['snapshots_written'], warm['cached'], dry['pending']), (29, 29, 29, 1))
        self.assertTrue(finish_collection(cycle, result))
        cycle.refresh_from_db()
        self.assertEqual((cycle.phase, cycle.counts['matched']), ('ready', 29))
        self.assertEqual(WclCombatantSnapshot.objects.count(), 29)
        self.assertEqual(progress[-1]['in_flight'], 0)
        self.assertEqual(progress[-1]['batches_completed'], 29)
        self.assertLessEqual(max(entry['in_flight'] for entry in progress), 16)
        self.assertTrue(any('fetch_seconds=' in line and 'save_seconds=' in line for line in logs))
        self.assertFalse(any('R0' in line or 'Player232' in line for line in logs))

    def test_rate_limit_stops_dispatch_but_drains_success_and_keeps_pending(self):
        barrier = threading.Barrier(2, timeout=4)
        def respond(body):
            barrier.wait()
            if body['code'] == 'Limited':
                return 429, {'error': 'rate_limit'}
            time.sleep(0.15)  # This successful response is still in flight on stop.
            return 200, page(body['fightIDs'])
        rows = [sample('Limited'), sample('Success')] + [sample(f'Unsent{i}') for i in range(50)]
        fetcher = self.fetcher()
        with local_api(respond) as (calls, errors), patch.object(service, '_collection_worker_limit', return_value=2, create=True):
            result = service.collect_dungeon_combatants(rows, fetcher, apply=True)
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 2)
        self.assertEqual((result['matched'], result['pending'], result['failed']), (1, 51, 0))
        self.assertEqual(result['selected'], sum(result[key] for key in (
            'cached', 'matched', 'missing', 'permission_denied', 'failed', 'pending')))
        self.assertIn('retry_at', result)
        self.assertEqual(fetcher._wcl_last_error, 'rate_limit')
        self.assertEqual(fetcher._wcl_retry_at.isoformat(), result['retry_at'])
        self.assertEqual(WclCombatantSnapshot.objects.get().report_code, 'Success')
        self.assertFalse(WclCombatantFetchState.objects.exists())

    @override_settings(WCL_V2_CONFIG={'client_id': 'collector-pool-local-fixture'})
    def test_real_worker_setting_defaults_to_ten_and_scan_persists_fenced_progress(self):
        from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
        from botend.models import SpecDungeonRanking, WclApiBudget
        barrier = threading.Barrier(10, timeout=4)
        seen_threads = set()
        def respond(body):
            seen_threads.add(threading.get_ident())
            barrier.wait()
            return 200, page(body['fightIDs'])
        season = SeasonMeta.objects.create(season_key='scan-pool', season_name='pool', is_active=True,
            mplus_encounters=[{'id': 1}], mplus_zone_id=1, raid_zone_id=2)
        rankings = [SpecDungeonRanking.objects.create(season_id=season.pk, dungeon_id=1,
                    dungeon_name='Test', dps=100, keystone_level=20, **sample(f'Default{i}')) for i in range(10)]
        cycle = begin_collection(season)
        cycle.phase = 'combatants'
        cycle.selected_samples = {'DeathKnight/Frost': {'1': [row.pk for row in rankings]}}
        cycle.completed_scopes = ['dungeon:1']
        cycle.save(update_fields=['phase', 'selected_samples', 'completed_scopes'])
        monitor = SpecDetailRankingMonitor(None, None)
        monitor._wcl_token = 'local-fixture-token'
        monitor._wcl_token_expire = time.time() + 3600
        def finish(current, counts):
            persisted = WclCollectionCycle.objects.get(pk=current.pk)
            self.assertEqual(persisted.phase, 'combatants')
            self.assertEqual(persisted.counts, counts)
            self.assertEqual(counts['workers'], 10)
            return finish_collection(current, counts)
        path = 'botend.controller.plugins.portal.SpecDetailRankingMonitor.finish_collection'
        with local_api(respond) as (calls, errors), patch(path, side_effect=finish):
            self.assertTrue(monitor.scan(''))
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 10)
        self.assertEqual(len(seen_threads), 10)
        self.assertEqual(WclApiBudget.objects.get().collection_workers, 10)
        cycle.refresh_from_db()
        self.assertEqual((cycle.phase, cycle.counts['matched'], cycle.counts['in_flight']), ('ready', 10, 0))
        print('WCL_POOL_DEFAULT_EVIDENCE ' + json.dumps(cycle.counts, sort_keys=True))

    def test_authentication_stops_unsent_and_preserves_concurrent_success(self):
        barrier = threading.Barrier(2, timeout=4)
        def respond(body):
            barrier.wait()
            if body['code'] == 'Auth':
                return 401, {'error': 'authentication'}
            time.sleep(0.15)
            return 200, page(body['fightIDs'])
        with local_api(respond) as (calls, errors), patch.object(service, '_collection_worker_limit', return_value=2):
            result = self.collect([sample('Auth'), sample('Good'), sample('Never')])
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 2)
        self.assertEqual((result['failed'], result['matched'], result['pending']), (1, 1, 1))
        self.assertFalse(WclCombatantFetchState.objects.exists())
        self.assertEqual(WclCombatantSnapshot.objects.get().report_code, 'Good')

    def test_http_403_propagates_on_caller_without_losing_inflight_success(self):
        barrier = threading.Barrier(2, timeout=4)
        fetcher = self.fetcher()
        main_thread = threading.get_ident()
        published = []
        original_setattr = SpecDetailBase.__setattr__

        def observe_setattr(instance, name, value):
            if instance is fetcher and name in ('_wcl_last_error', '_wcl_retry_at'):
                published.append((threading.get_ident(), name, value))
            original_setattr(instance, name, value)

        def respond(body):
            barrier.wait()
            if body['code'] == 'Forbidden':
                return 403, {'error': 'http_403'}
            time.sleep(0.15)
            return 200, page(body['fightIDs'])

        with local_api(respond) as (calls, errors), patch.object(
                service, '_collection_worker_limit', return_value=2), patch.object(
                SpecDetailBase, '__setattr__', observe_setattr):
            result = service.collect_dungeon_combatants(
                [sample('Forbidden'), sample('Good'), sample('Never')], fetcher, apply=True)
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 2)
        self.assertEqual((result['failed'], result['matched'], result['pending']), (1, 1, 1))
        self.assertEqual(published, [(main_thread, '_wcl_last_error', 'http_403'),
                                     (main_thread, '_wcl_retry_at', None)])
        self.assertFalse(WclCombatantFetchState.objects.exists())
        self.assertEqual(WclCombatantSnapshot.objects.get().report_code, 'Good')
        # A new successful run clears aggregate fatal state on the same parent.
        with local_api(lambda body: (200, page(body['fightIDs']))) as (_, errors):
            recovered = service.collect_dungeon_combatants([sample('Forbidden')], fetcher, apply=True)
        self.assertEqual(errors, [])
        self.assertEqual(recovered['matched'], 1)
        self.assertEqual(fetcher._wcl_last_error, '')
        self.assertIsNone(fetcher._wcl_retry_at)

    def test_legacy_fetcher_stays_on_main_thread_and_unattempted_is_pending(self):
        main_thread = threading.get_ident()
        called_on = []
        class Legacy:
            _wcl_last_error = 'authentication'
            def fetch_wcl_combatant_info(inner, code, batch):
                called_on.append(threading.get_ident())
                return None
        result = service.collect_dungeon_combatants([sample('A'), sample('B')], Legacy(), apply=True)
        self.assertEqual(called_on, [main_thread])
        self.assertEqual((result['failed'], result['pending']), (1, 1))
        self.assertEqual(result['batches_dispatched'], 1)

    def test_report_permission_pagination_missing_and_immutable_facts(self):
        def respond(body):
            code, fights, start = body['code'], body['fightIDs'], body['start']
            if code == 'Private':
                return 200, {'error': 'permission'}
            if code == 'Broken':
                if start is None:
                    return 200, page(fights, cursor=100)
                payload = page(fights)
                del payload['data']['reportData']['report']['events']['nextPageTimestamp']
                return 200, payload
            if code == 'Missing':
                return 200, page(fights, spec=0)
            if code == 'Paged' and start is None:
                return 200, page(fights[:1], cursor=100)
            return 200, page(fights)
        old = WclCombatantSnapshot.objects.create(report_code='Public', fight_id=11, actor_id=232,
            payload_json={'fight': 11, 'sourceID': 232, 'specID': 251,
                          'source': {'id': 232, 'name': 'Player232', 'server': 'BurningLegion'}, 'critMelee': -1})
        rows = ([sample('Private', i) for i in range(1, 42)] + [sample('Broken'), sample('Missing')]
                + [sample('Paged', i) for i in range(1, 22)] + [sample('Public'), sample('Public', actor=233)])
        with local_api(respond) as (calls, errors), patch.object(service, '_collection_worker_limit', return_value=2, create=True):
            result = self.collect(rows)
        self.assertEqual(errors, [])
        self.assertEqual((result['permission_denied'], result['failed'], result['missing'],
                          result['matched'], result['cached'], result['pending']), (41, 1, 2, 21, 1, 0))
        self.assertEqual(sum(call['code'] == 'Private' for call in calls), 1)
        self.assertTrue(all(len(call['fightIDs']) <= 20 for call in calls))
        self.assertFalse(WclCombatantSnapshot.objects.filter(report_code='Broken').exists())
        self.assertFalse(WclCombatantFetchState.objects.filter(report_code='Broken').exists())
        old.refresh_from_db()
        self.assertEqual(old.payload_json['critMelee'], -1)
        self.assertEqual(set(WclCombatantFetchState.objects.values_list('report_code', 'fight_id', 'status')),
                         {('Private', 0, 'permission_denied'), ('Missing', 11, 'missing'), ('Public', 11, 'missing')})
