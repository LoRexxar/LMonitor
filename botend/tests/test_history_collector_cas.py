"""Real ORM CAS contracts; SQLite proves predicates, not InnoDB lock contention.

Fixture is the unmodified activity 2864 row from current-readonly-v2.json
(SHA256 e867638b0dd53f8952ca41491ec1e4b8fb96633f22ddc364b5fee6d87de8c6c6).
Loopback responses are explicitly synthetic protocol fixtures, NOT source success.
"""
import copy
import gzip
import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils.dateparse import parse_datetime

from botend.models import RaiderIOCharacterActivity as Activity, SeasonMeta
from botend.services import player_observation_history as history

CAPTURE = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures' /
    'history_cas_activity_2864.json.gz').read_bytes()))
DID = 16865
CHARACTER = {'character_id': 309514531, 'identity': {
    k: CAPTURE[k] for k in ('region', 'realm_slug', 'character_name')}}


class HistoryCollectorCASTests(TransactionTestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(id=3, season_key='offline-cas',
            rio_season='season-mn-2', mplus_zone_id=0, raid_zone_id=0)
        self.seed()
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=3)

    def seed(self):
        row = copy.deepcopy(CAPTURE)
        for field in Activity._meta.concrete_fields:
            if field.get_internal_type() == 'DateTimeField' and row[field.attname]:
                row[field.attname] = parse_datetime(row[field.attname])
        if Activity.objects.filter(pk=2864).exists():
            Activity.objects.filter(pk=2864).update(**{k: v for k, v in row.items() if k != 'id'})
        else:
            Activity.objects.bulk_create([Activity(**row)])

    def row(self):
        return Activity.objects.values().get(pk=2864)

    def independent_update(self, changes):
        failures = []
        def writer():
            try:
                Activity.objects.filter(pk=2864).update(**changes)
            except BaseException as exc:
                failures.append(exc)
            finally:
                connections.close_all()
        thread = threading.Thread(target=writer)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), 'writer blocked: HTTP must not hold a transaction')
        if failures:
            raise failures[0]

    def batch(self, callback=None, status=200, body=None):
        case = self
        expected = history.scope('season-mn-2', CHARACTER['character_id'], DID)
        # Complete, explicitly synthetic local protocol fixture; original validator runs.
        body = body if body is not None else {'ui': expected, 'runs': [{'score': 100.5,
            'summary': {'season': 'season-mn-2', 'dungeon': {'id': DID},
                'keystone_run_id': 999001, 'mythic_level': 10, 'num_chests': 1,
                'completed_at': '2026-10-28T10:00:00.123456+00:00'}}]}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                case.received.append(parse_qs(urlsplit(self.path).query))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_args):
                pass
        case.received = []
        case.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        case.server_thread = threading.Thread(target=case.server.serve_forever, daemon=True)
        case.server_thread.start()
        # Register cleanup even when a subtest fails before its explicit close.
        server, thread = case.server, case.server_thread
        def close_server():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        case.addCleanup(close_server)
        class Batch:
            requests = 0
            budget = 1
            stop_event = threading.Event()
            def get(self, url, **kwargs):
                case.assertFalse(connection.in_atomic_block)
                case.assertTrue(connection.get_autocommit())
                case.assertEqual(url, history.URL)
                case.assertEqual(kwargs['params'], expected)
                self.requests += 1
                if callback:
                    callback()
                with requests.Session() as session:
                    session.trust_env = False
                    return session.get('http://127.0.0.1:%s/history' % case.server.server_port,
                        timeout=3, allow_redirects=False, **kwargs)
        return Batch()

    def call(self, batch, snapshot):
        return history.collect_history(batch, CHARACTER, self.season, DID,
                                       expected_snapshot=snapshot)

    def assert_non_target_equal(self, before, after):
        before, after = copy.deepcopy(before), copy.deepcopy(after)
        for row in (before, after):
            row['payload_json'][history.MODULE].pop(str(DID), None)
        self.assertEqual(before, after)

    def test_capture_has_all_17_fields_and_microseconds(self):
        self.assertEqual(len(self.row()), 17)
        self.assertEqual(set(self.row()), set(CAPTURE))
        self.assertEqual(self.row()['fetched_at'].isoformat(), CAPTURE['fetched_at'])
        self.assertEqual(self.row()['payload_json'][history.MODULE][str(DID)]['last_error'], 'http_504')

    def test_claim_rejects_independent_clock_identity_and_payload_drift(self):
        for field in ('clock', 'identity', 'payload'):
            with self.subTest(field=field):
                self.seed()
                approved = self.row()
                if field == 'clock':
                    changes = {'fetched_at': approved['fetched_at'] + timedelta(microseconds=1)}
                elif field == 'identity':
                    changes = {'character_name': 'independent-name'}
                else:
                    payload = copy.deepcopy(approved['payload_json'])
                    payload['raid_progression']['cas_test'] = True
                    changes = {'payload_json': payload}
                self.independent_update(changes)
                drifted = self.row()
                # No server is needed: any transport admission is itself a failure.
                class NoHTTP:
                    requests = 0
                    budget = 1
                    stop_event = threading.Event()
                    def get(self, *_a, **_kw):
                        raise AssertionError('unexpected HTTP')
                with CaptureQueriesContext(connection) as sql:
                    result = self.call(NoHTTP(), approved)
                self.assertEqual(result['status'], 'cas_claim_mismatch')
                self.assertEqual(self.row(), drifted)
                self.assertFalse(any(q['sql'].startswith(('UPDATE', 'INSERT')) for q in sql))

    def test_finalize_rejects_drift_on_success_and_error_without_clearing_lease(self):
        for status in (200, 504):
            for field in ('clock', 'identity', 'payload', 'token'):
                with self.subTest(status=status, field=field):
                    self.seed()
                    approved = self.row()
                    drifted = []
                    def boundary():
                        claimed = self.row()
                        entry = claimed['payload_json'][history.MODULE][str(DID)]
                        self.assertEqual(entry['last_error'], 'http_504')
                        self.assert_non_target_equal(approved, claimed)
                        if field == 'clock':
                            changes = {'fetched_at': claimed['fetched_at'] + timedelta(microseconds=1)}
                        elif field == 'identity':
                            changes = {'identity_digest': 'f' * 64}
                        else:
                            payload = copy.deepcopy(claimed['payload_json'])
                            if field == 'token':
                                payload[history.MODULE][str(DID)]['lease_token'] = 'other-owner'
                            else:
                                payload['raid_progression']['cas_test'] = True
                            changes = {'payload_json': payload}
                        self.independent_update(changes)
                        drifted.append(self.row())
                    batch = self.batch(boundary, status=status)
                    with CaptureQueriesContext(connection) as sql:
                        result = self.call(batch, approved)
                    self.assertEqual(result['status'], 'cas_finalize_mismatch')
                    self.assertEqual(self.row(), drifted[0])
                    self.assertEqual(sum(q['sql'].startswith('UPDATE') for q in sql), 1)
                    self.assertEqual(batch.requests, 1)
                    self.server.shutdown(); self.server.server_close(); self.server_thread.join()
                    self.server = None

    def test_guarded_success_changes_only_target_and_uses_original_validator(self):
        approved = self.row()
        batch = self.batch()
        result = self.call(batch, copy.deepcopy(CAPTURE))
        after = self.row()
        self.assertEqual(result['status'], 'ready')
        self.assert_non_target_equal(approved, after)
        entry = after['payload_json'][history.MODULE][str(DID)]
        self.assertEqual(entry['runs'][0]['run_id'], 999001)
        self.assertEqual(entry['coverage'], 'observed_partial')
        self.assertNotIn('last_error', entry)
        self.assertNotIn('lease_token', entry)
        self.assertEqual(self.received, [{k: [str(v)] for k, v in
            history.scope('season-mn-2', CHARACTER['character_id'], DID).items()}])

    def test_default_none_preserves_original_drift_merge(self):
        approved = self.row()
        batch = self.batch(lambda: self.independent_update({'last_error': 'independent'}), status=504)
        result = history.collect_history(batch, CHARACTER, self.season, DID)
        self.assertEqual(result['status'], 'http_504')
        self.assertEqual(self.row()['last_error'], 'independent')
        self.assertNotIn('lease_token', self.row()['payload_json'][history.MODULE][str(DID)])
        self.assertEqual(self.row()['fetched_at'], approved['fetched_at'])

    def test_guard_refuses_outer_transaction(self):
        before = self.row()
        with transaction.atomic(), CaptureQueriesContext(connection) as sql:
            result = self.call(None, before)
        self.assertEqual(result['status'], 'cas_requires_autocommit')
        self.assertEqual(self.row(), before)
        self.assertFalse(any(q['sql'].startswith('UPDATE') for q in sql))

    def test_guard_rejects_filtering_and_trimming_before_any_update(self):
        for kind in ('wrong_season', 'non_dict', 'overflow'):
            with self.subTest(kind=kind):
                self.seed()
                payload = self.row()['payload_json']
                entries = payload[history.MODULE]
                if kind == 'wrong_season':
                    entries['999'] = {'ui': {'season': 'old'}, 'runs': []}
                elif kind == 'non_dict':
                    entries['999'] = ['preserve-me']
                else:
                    for n in range(20):
                        entries[str(90000+n)] = {'ui': {'season': 'season-mn-2'}, 'runs': []}
                self.independent_update({'payload_json': payload})
                approved = self.row()
                class NoHTTP:
                    requests = 0
                    budget = 1
                    stop_event = threading.Event()
                    def get(self, *_a, **_kw):
                        raise AssertionError('unexpected HTTP')
                with CaptureQueriesContext(connection) as sql:
                    result = self.call(NoHTTP(), approved)
                self.assertEqual(result['status'], 'cas_non_target_change')
                self.assertEqual(self.row(), approved)
                self.assertFalse(any(q['sql'].startswith('UPDATE') for q in sql))

    def test_guard_preserves_budget_backoff_and_invalid_response(self):
        approved = self.row()
        batch = self.batch(body={'ui': {}, 'runs': []})
        batch.budget = 0
        self.assertEqual(self.call(batch, approved)['status'], 'budget_exhausted')
        self.assertEqual(self.row(), approved)
        batch.budget = 1
        self.assertEqual(self.call(batch, approved)['status'], 'invalid_payload')
        after = self.row()
        self.assert_non_target_equal(approved, after)
        self.assertEqual(self.call(batch, after)['status'], 'backoff')
        self.assertEqual(batch.requests, 1)

    def test_guard_preserves_private_tombstone_and_zero_request_backoff(self):
        approved = self.row()
        batch = self.batch(body={'ui': history.scope('season-mn-2',
            CHARACTER['character_id'], DID), 'isPrivate': True, 'runs': []})
        self.assertEqual(self.call(batch, approved)['status'], 'private')
        after = self.row()
        self.assert_non_target_equal(approved, after)
        self.assertTrue(after['payload_json'][history.MODULE][str(DID)]['private'])
        self.assertEqual(self.call(batch, after)['status'], 'private')
        self.assertEqual(batch.requests, 1)

    def test_structural_payload_drift_is_an_explicit_cas_refusal(self):
        approved = self.row()
        self.independent_update({'payload_json': []})
        self.assertEqual(self.call(None, approved)['status'], 'cas_claim_mismatch')
        self.assertEqual(self.row()['payload_json'], [])
        self.seed()
        batch = self.batch(lambda: self.independent_update({'payload_json': []}))
        self.assertEqual(self.call(batch, approved)['status'], 'cas_finalize_mismatch')
        self.assertEqual(self.row()['payload_json'], [])

    def test_missing_snapshot_row_never_creates(self):
        approved = self.row()
        approved['id'] = 999999
        with CaptureQueriesContext(connection) as sql:
            result = self.call(None, approved)
        self.assertEqual(result['status'], 'cas_claim_mismatch')
        self.assertFalse(any(q['sql'].startswith(('INSERT', 'UPDATE')) for q in sql))
