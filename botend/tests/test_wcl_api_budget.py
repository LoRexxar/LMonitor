"""Shared-budget integration contracts: real ORM, controlled HTTP boundary."""
import json
import threading
from datetime import timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from botend.models import WclApiBudget
from botend.services import wcl_api_budget as budget

QUERY = 'query($code:String!){reportData{report(code:$code){masterData{actors{id}}}}}'
DATA = {'reportData': {'report': {'masterData': {'actors': []}}}}


def quota(spent=20, reset=713, cost=1, limit=18000):
    # Actual historical currentRequest is a JSON scalar, not a GraphQL object.
    return dict(limitPerHour=limit, pointsSpentThisHour=spent, pointsResetIn=reset,
                currentRequest={'total': cost, 'application': cost})


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code, self.headers = status, headers or {}
        self._payload = payload

    def json(self):
        return self._payload


@override_settings(WCL_V2_CONFIG={'client_id': 'shared-client', 'client_secret': 'not-real'})
class WclBudgetTests(TransactionTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.clock = patch('botend.services.wcl_api_budget.timezone.now', return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def row(self, **kwargs):
        values = dict(account_key=budget.account_key('shared-client'), limit_per_hour=18000,
                      spent=100, reset_at=self.now + timedelta(seconds=713), observed_at=self.now)
        values.update(kwargs)
        return WclApiBudget.objects.create(**values)

    def request(self, post, client_id='shared-client', query=QUERY):
        return budget.graphql_request(client_id, 'not-real', query, {'code': 'Report'}, post=post)

    def test_discover_actual_window_learn_cost_and_stop_adding_quota(self):
        queries = []

        def post(url, **kwargs):
            self.assertFalse(connection.in_atomic_block, 'HTTP must not hold a DB transaction')
            q = kwargs['json']['query']
            queries.append(q)
            if len(queries) == 1:
                return Response({'data': {'rateLimitData': quota()}})
            data = dict(DATA)
            if '_wclBudget' in q:
                data['_wclBudget'] = quota(spent=21, cost=2.36)
            return Response({'data': data})

        first = self.request(post)
        self.assertEqual(first.payload, {'data': DATA})
        row = WclApiBudget.objects.get()
        self.assertEqual(row.limit_per_hour, 18000)
        self.assertEqual(row.reset_at, self.now + timedelta(seconds=713))
        self.assertGreaterEqual(row.spent, 23.36)  # inline spent may omit this request's full cost
        self.assertTrue(row.costs)
        second = self.request(post)
        self.assertEqual(second.payload, {'data': DATA})
        self.assertEqual(len(queries), 3)
        self.assertIn('currentRequest', queries[0])
        self.assertNotIn('currentRequest {', queries[0])
        self.assertIn('_wclBudget', queries[1])
        self.assertNotIn('rateLimitData', queries[2])

    def test_shared_reserve_blocks_second_instance_but_not_other_account(self):
        row = self.row(spent=14399)
        calls = []
        def post(url, **kwargs):
            calls.append(kwargs)
            return Response({'data': {'rateLimitData': quota(), **DATA}})
        result = self.request(post)
        self.assertEqual((result.error, result.retry_at), ('budget_wait', row.reset_at))
        self.assertEqual(calls, [])
        self.request(post, client_id='other-client')
        self.assertEqual(WclApiBudget.objects.count(), 2)
        self.assertTrue(calls)

    @override_settings(WCL_BUDGET_RESERVE_FRACTION=0.5)
    def test_reserve_fraction_is_configurable(self):
        row = self.row(spent=9000)
        with patch('requests.post') as http:
            result = self.request(http)
        self.assertEqual(result.retry_at, row.reset_at)
        http.assert_not_called()

    def test_reset_reconciles_real_window_without_guessing_next_hour(self):
        row = self.row(spent=18000, reset_at=self.now - timedelta(seconds=1))
        calls = []
        def post(url, **kwargs):
            calls.append(kwargs['json']['query'])
            return Response({'data': {'rateLimitData': quota(reset=3597), **DATA}})
        self.assertIsNotNone(self.request(post).payload)
        row.refresh_from_db()
        self.assertEqual(row.reset_at, self.now + timedelta(seconds=3597))
        self.assertEqual(len(calls), 2)
        self.assertIn('rateLimitData', calls[0])

    def test_live_lease_defers_without_http_and_expired_lease_recovers(self):
        row = self.row(lease_owner='other', lease_expires_at=self.now + timedelta(seconds=40))
        with patch('requests.post') as http:
            result = self.request(http)
        self.assertEqual((result.error, result.retry_at), ('budget_wait', row.lease_expires_at))
        http.assert_not_called()
        row.lease_expires_at = self.now - timedelta(seconds=1)
        row.save()
        result = self.request(lambda *a, **k: Response({'data': DATA}))
        self.assertIsNotNone(result.payload)
        row.refresh_from_db()
        self.assertEqual(row.lease_owner, '')

    def test_nested_consumer_cannot_enter_during_http(self):
        self.row()
        def post(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            with patch('requests.post') as second_http:
                second = self.request(second_http)
            second_http.assert_not_called()
            self.assertEqual(second.error, 'budget_wait')
            return Response({'data': DATA})
        self.assertIsNotNone(self.request(post).payload)

    def test_429_is_one_request_and_waits_for_later_retry_after_or_reset(self):
        row = self.row()
        for header, seconds in [('900', 900), (format_datetime(self.now + timedelta(seconds=1100)), 1100), ('3', 713)]:
            row.blocked_until = None
            row.save()
            with patch('requests.post', return_value=Response(status=429, headers={'Retry-After': header})) as http:
                result = self.request(http)
            self.assertEqual(http.call_count, 1)
            # HTTP-date headers have second resolution.
            self.assertLess(abs((result.retry_at - self.now).total_seconds() - seconds), 1)
            self.assertEqual(result.error, 'rate_limit')
            row.refresh_from_db()
            with patch('requests.post') as denied:
                again = self.request(denied)
            self.assertEqual((again.error, again.retry_at), ('rate_limit', result.retry_at))
            denied.assert_not_called()

    def test_network_auth_permission_and_bad_quota_are_not_deferrals(self):
        row = self.row()
        for status, error in [(401, 'authentication'), (403, 'http_403'), (500, 'http_500')]:
            result = self.request(lambda *a, **k: Response(status=status))
            self.assertEqual((result.error, result.retry_at), (error, None))
        with patch('requests.post', side_effect=requests.Timeout('must-not-log-credentials')) as http:
            result = self.request(http)
        self.assertEqual((result.error, result.retry_at), ('network', None))
        row.refresh_from_db()
        self.assertEqual(row.lease_owner, '')
        row.reset_at = None
        row.save()
        with patch('requests.post', return_value=Response({'data': {}})) as http:
            result = self.request(http)
        self.assertEqual((result.error, result.retry_at), ('invalid_quota', None))
        self.assertEqual(http.call_count, 1)

    def test_stale_owner_cannot_settle_or_release_successor_lease(self):
        row = self.row()
        def post(*args, **kwargs):
            WclApiBudget.objects.filter(pk=row.pk).update(lease_owner='successor', spent=777)
            return Response({'data': {**DATA, '_wclBudget': quota(spent=5)}})
        result = self.request(post)
        self.assertEqual(result.error, 'budget_wait')
        row.refresh_from_db()
        self.assertEqual((row.lease_owner, row.spent), ('successor', 777))

    def test_base_and_dashboard_share_budget_and_preserve_payload_contracts(self):
        from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
        from botend.dashboard.api import WclAnalysisTaskAPIView
        first, second = SpecDetailBase(None, None), WclAnalysisTaskAPIView()
        first._wcl_token, first._wcl_token_expire = 'not-real', float('inf')
        row = self.row(spent=18000)
        with patch('requests.post') as http:
            self.assertIsNone(first._wcl_graphql(QUERY, {}, retries=3))
            with self.assertRaises(budget.WclTransportError) as exc:
                second._wcl_graphql('not-real', QUERY, {})
        http.assert_not_called()
        self.assertEqual(first._wcl_retry_at, row.reset_at)
        self.assertEqual(first._wcl_last_error, 'budget_wait')
        self.assertEqual(exc.exception.retry_at, row.reset_at)
        row.spent = 0
        row.save()
        with patch('requests.post', return_value=Response({'data': DATA})):
            self.assertEqual(first._wcl_graphql(QUERY, {}), DATA)
            self.assertEqual(second._wcl_graphql('not-real', QUERY, {}), {'data': DATA})
        self.assertIsNone(first._wcl_retry_at)
        self.assertEqual(first._wcl_last_error, '')

    def test_periodic_sync_and_request_shapes_are_bounded(self):
        self.row()
        calls = []
        def post(url, **kwargs):
            q = kwargs['json']['query']
            calls.append(q)
            data = {**DATA, '_wclBudget': quota(spent=101, cost=2.36)} if '_wclBudget' in q else DATA
            return Response({'data': data})
        self.request(post)
        self.request(post)
        self.assertNotIn('rateLimitData', calls[-1])
        row = WclApiBudget.objects.get()
        row.observed_at = self.now - timedelta(seconds=301)
        row.save()
        self.request(post)
        self.assertIn('_wclBudget', calls[-1])
        self.request(post)
        self.assertNotIn('rateLimitData', calls[-1])
        # Different batch cardinalities must not re-use a cheap scalar estimate.
        budget.graphql_request('shared-client', 'not-real', QUERY, {'code': 'Report', 'fightIDs': [1, 2]}, post=post)
        self.assertIn('_wclBudget', calls[-1])

    def test_network_failure_keeps_reservation_and_graphql_permission_is_terminal(self):
        row = self.row()
        with patch('requests.post', side_effect=requests.Timeout('private')) as http:
            result = self.request(http)
        row.refresh_from_db()
        self.assertEqual(result.error, 'network')
        self.assertGreater(row.spent, 100)
        errors = [{'message': 'You do not have permission to view this report.', 'path': ['reportData', 'report']}]
        result = self.request(lambda *a, **k: Response({'errors': errors}))
        self.assertEqual((result.error, result.retry_at), ('permission', None))

    def test_429_unknown_window_stops_discovery_and_base_does_not_retry(self):
        from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
        fetcher = SpecDetailBase(None, None)
        fetcher._wcl_token, fetcher._wcl_token_expire = 'not-real', float('inf')
        with patch('requests.post', return_value=Response(status=429)) as http:
            self.assertIsNone(fetcher._wcl_graphql(QUERY, {}, retries=3))
        self.assertEqual(http.call_count, 1)
        self.assertEqual(fetcher._wcl_last_error, 'rate_limit')
        self.assertEqual(fetcher._wcl_retry_at, self.now + timedelta(seconds=60))
        fetcher._wcl_token = None
        with patch.object(fetcher, '_get_wcl_token', return_value=None):
            self.assertIsNone(fetcher._wcl_graphql(QUERY, {}))
        self.assertEqual(fetcher._wcl_last_error, 'authentication')
        self.assertIsNone(fetcher._wcl_retry_at)

    @override_settings(WCL_V2_CONFIG={}, WCL_API_CONFIG={'client_id': 'fallback-client', 'client_secret': 'not-real'})
    def test_dashboard_uses_actual_fallback_client_identity(self):
        from botend.dashboard.api import WclAnalysisTaskAPIView
        row = self.row(account_key=budget.account_key('fallback-client'), spent=18000)
        with patch('requests.post') as http:
            with self.assertRaises(budget.WclTransportError) as exc:
                WclAnalysisTaskAPIView()._wcl_graphql('not-real', QUERY, {})
        http.assert_not_called()
        self.assertEqual(exc.exception.retry_at, row.reset_at)
        self.assertEqual(WclApiBudget.objects.count(), 1)

    def test_real_local_http_and_real_orm(self):
        self.row()
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'data': {**DATA, '_wclBudget': quota(spent=101, cost=2.36)}}).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def local_post(url, **kwargs):
                self.assertFalse(connection.in_atomic_block)
                return requests.post('http://127.0.0.1:%s/' % server.server_port, **kwargs)
            self.assertEqual(self.request(local_post).payload, {'data': DATA})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(len(seen), 1)
        row = WclApiBudget.objects.get()
        self.assertGreater(row.spent, 100)
        self.assertEqual(row.lease_owner, '')
        self.assertTrue(row.costs)
