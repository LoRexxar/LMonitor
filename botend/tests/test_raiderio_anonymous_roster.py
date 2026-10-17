"""Anonymous source sentinel is unavailable, not a valid zero ID.

Uses a real loopback HTTP response and ORM for the collector/publication path.
The fixture shape comes from the observed public run 20386648 (anonymous realm,
empty path, integer id=0); other values are deliberately local test data.
"""
import copy
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import timedelta

import requests
from django.test import TestCase
from django.utils import timezone
from botend.models import RaiderIORunRoster
from botend.services.raiderio_run_roster import RosterCollector, validate_details, project_run_rosters
from botend.tests.test_raiderio_run_roster import details
from botend.tests.test_player_raiderio_activity import payload


class AnonymousRosterTests(TestCase):
    def setUp(self):
        self.run = payload()['mythic_plus_best_runs'][0]
        self.run['keystone_run_id'] = 123
        self.data = details()
        for i, member in enumerate(self.data['roster']):
            member['character']['id'] = 100 + i

    def anonymous(self):
        data = copy.deepcopy(self.data)
        data['roster'][4]['character'].update(id=0, realm={'slug': 'anonymous'}, path='')
        return data

    def test_observed_zero_anonymous_placeholder_retracts_whole_roster(self):
        data = self.anonymous()
        self.assertNotEqual(data['roster'][4]['character']['name'].lower(), 'anonymous')
        self.assertIsNone(validate_details(data, self.run, 'season-mn-2'))

    def test_only_exact_anonymous_placeholder_is_tolerated(self):
        for value in (0, -1, True, False, '0', '100', None):
            data = copy.deepcopy(self.data)
            data['roster'][4]['character']['id'] = value
            with self.subTest(public_id=value), self.assertRaises(ValueError):
                validate_details(data, self.run, 'season-mn-2')
        for value in (-1, False, True, '0', None):
            data = self.anonymous()
            data['roster'][4]['character']['id'] = value
            with self.subTest(anonymous_id=value), self.assertRaises(ValueError):
                validate_details(data, self.run, 'season-mn-2')
        data = self.anonymous()
        data['roster'][4]['character']['path'] = '/characters/eu/test-realm/Member4'
        with self.assertRaises(ValueError):
            validate_details(data, self.run, 'season-mn-2')
        facts = validate_details(self.data, self.run, 'season-mn-2')
        self.assertEqual([m['character']['id'] for m in facts['roster']], [100, 101, 102, 103, 104])

    def test_anonymous_member_never_bypasses_run_scope(self):
        for changes in ({'season': 'season-wrong'}, {'keystone_run_id': 124},
                        {'dungeon': {'name': 'Wrong dungeon'}}, {'mythic_level': 99}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_details({**self.anonymous(), **changes}, self.run, 'season-mn-2')

    def test_http_orm_withdraws_last_good_and_clears_error_without_partial_publication(self):
        envelope = {'data': self.data}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(envelope['data']).encode()
                self.send_response(200);self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        def transport(_url, **kwargs):
            return requests.get(f'http://127.0.0.1:{server.server_port}/run-details', **kwargs)
        collector = RosterCollector(transport=transport, proxies={})
        try:
            self.assertEqual(collector.refresh(self.run, 'season-mn-2')['status'], 'ready')
            row = RaiderIORunRoster.objects.get();last_good = row.payload_json
            row.next_attempt_at = timezone.now() - timedelta(seconds=1)
            row.status = 'error';row.last_error = 'invalid_payload';row.failure_count = 1
            row.save()
            envelope['data'] = self.anonymous()
            self.assertEqual(collector.refresh(self.run, 'season-mn-2', force=False)['status'], 'unavailable')
            row.refresh_from_db()
            self.assertEqual(row.last_error, '')
            self.assertEqual(row.failure_count, 0)
            self.assertFalse(row.roster_visible)
            self.assertEqual(row.payload_json, last_good)
            self.assertEqual(project_run_rosters([self.run], 'season-mn-2'), [{'roster_status': 'unavailable', 'roster': []}])
        finally:
            collector.close();server.shutdown();server.server_close();thread.join(timeout=2)
