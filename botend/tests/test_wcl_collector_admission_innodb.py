"""Real InnoDB + budget + Base + collector, with loopback HTTP only."""
import json
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import WclApiBudget, WclCombatantSnapshot
from botend.services import wcl_api_budget as budget, wcl_combatant_snapshot as service
from botend.tests.test_wcl_api_budget import quota
from botend.tests.test_wcl_combatant_pool import sample, page


@skipUnless(connection.vendor == 'mysql', 'Requires real InnoDB')
@override_settings(WCL_V2_CONFIG={'client_id': 'admission-fixture'})
class CollectorAdmissionTests(TransactionTestCase):
    def exercise(self, mode):
        query = next(c for c in SpecDetailBase.fetch_wcl_combatant_info.__code__.co_consts
                     if isinstance(c, str) and 'query($code' in c)
        key = budget._query_info(query, {'code': 'r0', 'fightIDs': [11], 'start': None})[0]
        now = timezone.now()
        row = WclApiBudget.objects.create(account_key=budget.account_key('admission-fixture'),
            limit_per_hour=18000, reset_at=now + timedelta(seconds=600), observed_at=now,
            spent=14320 if mode == 'quota' else 0,
            costs={} if mode == 'cold' else {key: 40},
            lease_owner='external' if mode == 'external' else '',
            lease_expires_at=now + timedelta(seconds=90) if mode == 'external' else None)
        with connection.cursor() as cursor:
            cursor.execute('SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s', [row._meta.db_table])
            self.assertEqual(cursor.fetchone()[0], 'InnoDB')
        calls = []
        lock = threading.Lock()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with lock:
                    calls.append(body)
                time.sleep(.3 if mode == '429' and body['variables']['code'] != 'r0' else .15)
                status = 429 if mode == '429' and body['variables']['code'] == 'r0' else 200
                payload = page(body['variables']['fightIDs'])
                if mode == 'pagination' and body['variables']['start'] is None:
                    payload = page(body['variables']['fightIDs'], cursor=100)
                payload['data']['_wclBudget'] = quota(spent=100, cost=2)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        fetcher = SpecDetailBase(None, None)
        fetcher._wcl_token = 'loopback-only'
        fetcher._wcl_token_expire = time.time() + 3600
        records = [sample('r%s' % i) for i in range(30 if mode == '429' else 10)]
        if mode == 'mixed':
            records.append(sample('r9', fight=12))
        observations = []
        started = time.monotonic()
        try:
            with patch.object(budget, 'URL', 'http://127.0.0.1:%s/' % server.server_port):
                result = service.collect_dungeon_combatants(records, fetcher, apply=True, progress=observations.append)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        for snapshot in observations:
            self.assertEqual(snapshot['selected'], sum(snapshot[k] for k in (
                'cached', 'matched', 'missing', 'permission_denied', 'failed', 'pending')))
        identities = [(c['variables']['code'], tuple(c['variables']['fightIDs']), c['variables']['start']) for c in calls]
        self.assertEqual(len(identities), len(set(identities)), 'Never replay sent HTTP')
        self.assertLess(time.monotonic() - started, 8)
        if mode in ('cold', 'mixed'):
            self.assertEqual((result['matched'], result['pending'], result['failed']), (len(records), 0, 0))
            self.assertEqual(len(calls), 10)
            self.assertGreater(result['batches_dispatched'], 10, 'Must exercise busy admission')
            self.assertNotIn('retry_at', result)
            self.assertEqual(WclCombatantSnapshot.objects.count(), len(records))
            self.assertEqual(fetcher._wcl_last_error, '')
        elif mode == 'external':
            self.assertEqual(len(calls), 0)
            self.assertEqual(result['pending'], 10)
            self.assertEqual(fetcher._wcl_last_error, 'admission_busy')
            self.assertLess((fetcher._wcl_retry_at - now).total_seconds(), 5)
            self.assertLessEqual(result['batches_dispatched'], 20)
        elif mode == '429':
            self.assertLessEqual(len(calls), 10)
            self.assertGreaterEqual(result['pending'], 1)
            self.assertEqual(fetcher._wcl_last_error, 'rate_limit')
            row.refresh_from_db()
            self.assertEqual(fetcher._wcl_retry_at, row.blocked_until)
        elif mode == 'quota':
            self.assertEqual((len(calls), result['matched'], result['pending']), (2, 2, 8))
            self.assertEqual(fetcher._wcl_last_error, 'budget_wait')
        elif mode == 'pagination':
            self.assertGreater(result['pending'], 0)
            self.assertEqual(result['failed'], 0)
            self.assertEqual(fetcher._wcl_last_error, 'admission_deferred')
        print('COLLECTOR_ADMISSION_PROOF', mode, 'http=', len(calls), 'matched=', result['matched'], 'pending=', result['pending'], 'error=', fetcher._wcl_last_error)

    def test_cold_shape_finishes_ten(self):
        self.exercise('cold')

    def test_nine_known_and_one_new_two_fight_shape(self):
        self.exercise('mixed')

    def test_real_429_stops_and_drains(self):
        self.exercise('429')

    def test_real_quota_exhaustion_stops_and_drains(self):
        self.exercise('quota')

    def test_external_calibration_short_bounded_defer(self):
        self.exercise('external')

    def test_busy_later_page_never_replays_first_page(self):
        self.exercise('pagination')
