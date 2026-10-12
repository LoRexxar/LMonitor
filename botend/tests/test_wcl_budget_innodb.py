"""Real InnoDB row locks plus loopback HTTP; never contacts WCL."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import skipUnless

import requests
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from botend.models import WclApiBudget
from botend.services import wcl_api_budget as budget
from botend.tests.test_wcl_api_budget import DATA, QUERY, quota


@skipUnless(connection.vendor == 'mysql', 'Requires real MySQL/InnoDB row locks')
class InnoDBParallelBudgetTests(TransactionTestCase):
    def test_ten_real_http_requests_overlap_and_budget_admission_is_atomic(self):
        self.exercise(workers=10, spent=0, expected=10)

    def test_ten_contenders_only_two_can_afford_http(self):
        self.exercise(workers=10, spent=700, expected=2)

    def test_real_late_quota_cannot_erase_newer_settlement(self):
        self.ordered_responses(rate_limit=False)

    def test_real_late_success_cannot_clear_concurrent_429(self):
        self.ordered_responses(rate_limit=True)

    def ordered_responses(self, *, rate_limit):
        now = timezone.now()
        key = budget._query_info(QUERY, {'code': 'early'})[0]
        row = WclApiBudget.objects.create(account_key=budget.account_key('ordered'),
            limit_per_hour=1000, spent=100, reset_at=now + timedelta(seconds=600),
            observed_at=now, costs={key: 40})
        arrived, release = threading.Event(), threading.Event()
        failures = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                early = body['variables']['code'] == 'early'
                if early:
                    arrived.set()
                    if not release.wait(timeout=10):
                        failures.append('late response release timed out')
                self.send_response(429 if rate_limit and not early else 200)
                self.send_header('Content-Type', 'application/json')
                if rate_limit and not early:
                    self.send_header('Retry-After', '900')
                self.end_headers()
                self.wfile.write(json.dumps({'data': {**DATA, '_wclBudget': quota(
                    spent=101 if early else 600, cost=40, limit=1000)}}).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def worker(code):
            close_old_connections()
            try:
                def local_post(url, **kwargs):
                    self.assertFalse(connection.in_atomic_block)
                    return requests.post('http://127.0.0.1:%s/' % server.server_port, **kwargs)
                return budget.graphql_request('ordered', 'local-token', QUERY, {'code': code}, post=local_post)
            finally:
                connections.close_all()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                early = pool.submit(worker, 'early')
                self.assertTrue(arrived.wait(timeout=10))
                later = pool.submit(worker, 'later').result(timeout=10)
                row.refresh_from_db()
                watermark, blocked = row.spent, row.blocked_until
                self.assertEqual(later.error, 'rate_limit' if rate_limit else '')
                release.set()
                self.assertEqual(early.result(timeout=10).error, '')
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join()
        row.refresh_from_db()
        self.assertEqual(failures, [])
        self.assertGreaterEqual(row.spent, watermark)
        if rate_limit:
            self.assertIsNotNone(blocked)
            self.assertEqual(row.blocked_until, blocked)
        else:
            self.assertGreaterEqual(row.spent, 680)
        print('INNODB_ORDER_PROOF rate_limit=%s spent=%s late_success_preserves_block=%s' %
              (rate_limit, row.spent, row.blocked_until == blocked))

    def exercise(self, *, workers, spent, expected):
        now = timezone.now()
        key = budget._query_info(QUERY, {})[0]
        row = WclApiBudget.objects.create(account_key=budget.account_key('innodb'),
            limit_per_hour=1000, spent=spent, reset_at=now + timedelta(seconds=600),
            observed_at=now, costs={key: 40})
        with connection.cursor() as cursor:
            cursor.execute('SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s', [row._meta.db_table])
            self.assertEqual(cursor.fetchone()[0], 'InnoDB')
        arrived = threading.Barrier(expected)
        started = threading.Barrier(workers)
        lock = threading.Lock()
        seen, failures = [], []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with lock:
                    seen.append(body)
                try:
                    arrived.wait(timeout=10)
                except threading.BrokenBarrierError:
                    failures.append('HTTP did not overlap')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'data': DATA}).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def worker():
            close_old_connections()
            try:
                started.wait(timeout=10)
                def local_post(url, **kwargs):
                    self.assertFalse(connection.in_atomic_block)
                    return requests.post('http://127.0.0.1:%s/' % server.server_port, **kwargs)
                return budget.graphql_request('innodb', 'local-token', QUERY, {}, post=local_post)
            finally:
                connections.close_all()
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(lambda _: worker(), range(workers)))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(len(seen), expected)
        self.assertEqual(sum(not r.error for r in results), expected)
        self.assertTrue(all(r.error in ('', 'budget_wait') for r in results))
        row.refresh_from_db()
        self.assertEqual(row.spent, spent + expected * 40)
        self.assertLessEqual(row.spent, 800)
        self.assertEqual(row.reservations.count(), 0)
        print('INNODB_HTTP_PROOF workers=%s overlapping=%s admitted=%s spent=%s' %
              (workers, expected, len(seen), row.spent))
