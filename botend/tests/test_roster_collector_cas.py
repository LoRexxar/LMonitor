"""Offline real-ORM CAS tests. Captured facts are not new source success.

SQLite independent connections prove fencing/uniqueness, not InnoDB row locks.
"""
import copy
import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import requests
from django.db import connection, connections, transaction, IntegrityError, OperationalError
from django.test import TransactionTestCase
from django.utils.dateparse import parse_datetime

from botend.models import RaiderIORunRoster as Roster
from botend.services import raiderio_run_roster as service

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/roster_cas_capture.json').read_text())


class RosterCASTests(TransactionTestCase):
    def setUp(self):
        self.raw = copy.deepcopy(FIXTURE['row'])
        self.run = FIXTURE['run']
        self.season = self.raw['rio_season']
        self.now = parse_datetime('2026-11-03T00:00:00.123456+00:00')
        self.seed()
        self.clock = patch.object(service.timezone, 'now', return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.calls = 0

    def seed(self):
        Roster.objects.all().delete()
        values = copy.deepcopy(self.raw)
        for f in Roster._meta.concrete_fields:
            if f.get_internal_type() == 'DateTimeField' and values[f.attname]:
                values[f.attname] = parse_datetime(values[f.attname])
        Roster.objects.create(**values)

    def row(self):
        return Roster.objects.values().get(run_id=self.raw['run_id'])

    def independent(self, fn):
        errors = []
        def work():
            try:
                fn()
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()
        t = threading.Thread(target=work)
        t.start(); t.join(4)
        self.assertFalse(t.is_alive(), 'independent connection blocked')
        if errors:
            raise errors[0]

    def collector(self, callback=None, body=None, status=200, error=None):
        def transport(url, **kwargs):
            self.calls += 1
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(connection.get_autocommit())
            self.assertEqual(kwargs['params'], {'season': self.season, 'id': self.raw['run_id']})
            self.assertEqual(url, 'https://raider.io/api/v1/mythic-plus/run-details')
            if callback:
                callback()
            if error:
                raise error
            response = requests.Response()
            response.status_code = status
            response.headers['Retry-After'] = '600'
            response._content = json.dumps(body if body is not None else self.raw['payload_json']).encode()
            return response
        return service.RosterCollector(transport=transport)

    def call(self, collector, **kw):
        return collector.refresh(self.run, self.season, **kw)['status']

    def test_claim_all_fields_and_microseconds_drift(self):
        for changes in ({'fetched_at': parse_datetime(self.raw['fetched_at']) + timedelta(microseconds=1)},
                        {'payload_json': {'changed': True}}, {'roster_visible': False},
                        {'run_id': self.raw['run_id'] + 1}, {'failure_count': 99}):
            with self.subTest(changes=changes):
                self.seed(); before = self.row()
                self.independent(lambda: Roster.objects.filter(pk=before['id']).update(**changes))
                drift = list(Roster.objects.values())
                self.assertEqual(self.call(self.collector(), expected_snapshot=before), 'cas_claim_mismatch')
                self.assertEqual(list(Roster.objects.values()), drift)
        self.assertEqual(self.calls, 0)

    def test_finalize_full_row_and_lease_drift(self):
        for changes in ({'fetched_at': self.now + timedelta(microseconds=1)},
                        {'payload_json': {'concurrent': True}}, {'lease_token': 'replacement'},
                        {'roster_visible': False}, {'failure_count': 77}):
            with self.subTest(changes=changes):
                self.seed(); before = self.row(); observed = []
                def drift():
                    self.independent(lambda: Roster.objects.filter(pk=before['id']).update(**changes))
                    observed.append(self.row())
                self.assertEqual(self.call(self.collector(drift), expected_snapshot=before), 'cas_finalize_mismatch')
                self.assertEqual(self.row(), observed[0])
                self.assertTrue(self.row()['lease_token'])

    def test_success_uses_original_validator_and_claim_baseline(self):
        before = self.row()
        self.assertEqual(self.call(self.collector(), expected_snapshot=before), 'ready')
        after = self.row()
        self.assertEqual(after['payload_json'], service.validate_details(self.raw['payload_json'], self.run, self.season))
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(after['lease_token'], '')
        self.assertEqual(self.calls, 1)

    def test_absent_collision_unique_constraint_and_new_claim(self):
        before = self.row()
        self.assertEqual(self.call(self.collector(), expected_absent=True), 'cas_claim_mismatch')
        self.assertEqual(self.row(), before)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Roster.objects.create(rio_season=self.season, run_id=self.raw['run_id'])
        Roster.objects.all().delete()
        # Keep two refresh calls in flight on distinct real connections, but
        # arbitrate the loser after the winner's claim commits. Shared-cache
        # SQLite need not wait for an uncommitted INSERT (tested separately).
        in_transport = threading.Event()
        release = threading.Event()
        results, errors, owner_connections = [], [], []

        def hold_transport():
            owner_connections.append(connection.connection)
            in_transport.set()
            self.assertTrue(release.wait(10), 'winner transport release timeout')

        def owner():
            try:
                results.append(self.call(self.collector(hold_transport), expected_absent=True))
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        thread = threading.Thread(target=owner)
        thread.start()
        try:
            self.assertTrue(in_transport.wait(10), 'winner never committed its claim')
            self.assertIsNot(owner_connections[0], connection.connection)
            claimed = self.row()
            self.assertEqual(claimed['status'], 'fetching')
            self.assertTrue(claimed['lease_token'])
            self.assertEqual(self.calls, 1)
            self.assertEqual(self.call(self.collector(), expected_absent=True), 'cas_claim_mismatch')
            self.assertEqual(self.calls, 1, 'loser must never send HTTP')
            self.assertEqual(self.row(), claimed, 'loser must not adopt or mutate winner')
            self.assertEqual(Roster.objects.count(), 1)
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive(), 'winner connection blocked')
        self.assertEqual(errors, [])
        self.assertEqual(results, ['ready'])
        self.assertEqual(Roster.objects.count(), 1)
        after = self.row()
        self.assertEqual(after['id'], claimed['id'])
        self.assertEqual(after['status'], 'ready')
        self.assertEqual(after['lease_token'], '')
        self.assertEqual(after['payload_json'], service.validate_details(self.raw['payload_json'], self.run, self.season))
        self.assertEqual(self.calls, 1)

    def test_absent_uncommitted_insert_refuses_without_http_or_mutation(self):
        Roster.objects.all().delete()
        inserted = threading.Event()
        release = threading.Event()
        snapshots, errors, owner_connections = [], [], []

        def owner():
            try:
                with transaction.atomic():
                    Roster.objects.create(
                        rio_season=self.season, run_id=self.raw['run_id'],
                        status='fetching', lease_token='competing-owner',
                        last_error='uncommitted competitor', payload_json={'owner': True},
                    )
                    snapshots.append(self.row())
                    owner_connections.append(connection.connection)
                    inserted.set()
                    self.assertTrue(release.wait(10), 'insert owner release timeout')
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        thread = threading.Thread(target=owner)
        thread.start()
        timer = None
        try:
            self.assertTrue(inserted.wait(10), 'owner never inserted')
            self.assertIsNot(owner_connections[0], connection.connection)
            shared_memory = (connection.vendor == 'sqlite'
                             and connection.is_in_memory_db()
                             and 'cache=shared' in str(connection.settings_dict['NAME']))
            if shared_memory:
                # No timer releases this writer: a successful claim or generic
                # OperationalError is NOT an accepted outcome. Only SQLite's
                # precise shared-cache lock refusal is safe here.
                with self.assertRaises(OperationalError) as caught:
                    self.call(self.collector(), expected_absent=True)
                cause = caught.exception.__cause__
                self.assertIsInstance(cause, sqlite3.OperationalError)
                self.assertEqual(cause.sqlite_errorcode, sqlite3.SQLITE_LOCKED_SHAREDCACHE)
                self.assertEqual(cause.sqlite_errorname, 'SQLITE_LOCKED_SHAREDCACHE')
                self.assertFalse(release.is_set())
                self.assertTrue(thread.is_alive(), 'owner must still hold its transaction')
            else:
                # File SQLite / locking backends may wait for commit instead.
                # Do not accept any database exception on that path.
                timer = threading.Timer(0.2, release.set)
                timer.start()
                self.assertEqual(self.call(self.collector(), expected_absent=True), 'cas_claim_mismatch')
            self.assertEqual(self.calls, 0)
        finally:
            release.set()
            if timer is not None:
                timer.cancel()
                timer.join(10)
            thread.join(10)
        self.assertFalse(thread.is_alive(), 'insert owner connection blocked')
        self.assertEqual(errors, [])
        self.assertEqual(Roster.objects.count(), 1)
        self.assertEqual(self.row(), snapshots[0], 'refused claim must preserve entire owner row')
        self.assertEqual(self.calls, 0)

    def test_backoff_force_and_private_preserved(self):
        Roster.objects.update(status='error')
        before = self.row()
        with patch.object(service.timezone, 'now', return_value=parse_datetime('2026-10-28T22:00:00+00:00')):
            self.assertEqual(self.call(self.collector(), expected_snapshot=before, force=True), 'backoff')
        self.assertEqual(self.row(), before)
        private = {**self.raw['payload_json'], 'runPrivacyMode': 'private'}
        self.assertEqual(self.call(self.collector(body=private), expected_snapshot=before), 'unavailable')
        self.assertFalse(self.row()['roster_visible'])
        self.assertEqual(self.row()['payload_json'], before['payload_json'])

    def test_wrong_response_429_transport_and_unknown_preserve_old(self):
        for options, expected in (({'body': {**self.raw['payload_json'], 'keystone_run_id': 1}}, 'invalid_payload'),
                                  ({'status': 429}, 'rate_limited'),
                                  ({'error': requests.Timeout()}, 'transport_error')):
            with self.subTest(expected=expected):
                self.seed(); before = self.row(); c = self.collector(**options)
                self.assertEqual(self.call(c, expected_snapshot=before), expected)
                after = self.row()
                for key in ('payload_json', 'fetched_at', 'roster_visible', 'run_id', 'rio_season'):
                    self.assertEqual(after[key], before[key])
                self.assertGreater(after['next_attempt_at'], self.now)
                if expected == 'rate_limited': self.assertTrue(c.stopped)
        self.seed(); before = self.row()
        with self.assertRaises(RuntimeError):
            self.call(self.collector(error=RuntimeError('unknown offline failure')), expected_snapshot=before)
        self.assertEqual(self.row()['payload_json'], before['payload_json'])
        self.assertEqual(self.row()['status'], 'fetching')
        self.assertTrue(self.row()['lease_token'])

    def test_outer_transaction_and_invalid_contract_no_write(self):
        before = self.row()
        with transaction.atomic():
            self.assertEqual(self.call(self.collector(), expected_snapshot=before), 'cas_requires_autocommit')
        for kw in ({'expected_snapshot': {}}, {'expected_snapshot': before, 'expected_absent': True}):
            self.assertEqual(self.call(self.collector(), **kw), 'cas_claim_mismatch')
        self.assertEqual(self.row(), before)
        self.assertEqual(self.calls, 0)

    def test_captured_error_backoffs_and_real_missing_references(self):
        for raw in FIXTURE['error_rows']:
            with self.subTest(run_id=raw['run_id']):
                self.raw = raw
                self.run = FIXTURE['error_runs'][str(raw['run_id'])]
                self.seed(); before = self.row()
                with patch.object(service.timezone, 'now', return_value=parse_datetime('2026-10-28T22:00:00+00:00')):
                    self.assertEqual(self.call(self.collector(), expected_snapshot=before, force=True), 'backoff')
                self.assertEqual(self.row(), before)
        self.assertEqual(self.calls, 0)
        for run_id, run in FIXTURE['missing_runs'].items():
            with self.subTest(missing=run_id):
                self.raw = {**FIXTURE['row'], 'run_id': int(run_id)}
                self.run = run
                Roster.objects.all().delete()
                # Unknown transport, not invented source facts: retain fetching.
                with self.assertRaises(RuntimeError):
                    self.call(self.collector(error=RuntimeError('offline unknown')), expected_absent=True)
                after = self.row()
                self.assertEqual(after['payload_json'], {})
                self.assertEqual(after['status'], 'fetching')
                self.assertIsNone(after['fetched_at'])
                self.assertTrue(after['lease_token'])

    def test_exact_encoded_snapshot_and_deleted_finalize(self):
        before = self.row()
        encoded = json.loads(service._snapshot_json(before))
        self.assertEqual(self.call(self.collector(), expected_snapshot=encoded), 'ready')
        self.seed(); before = self.row()
        changed = copy.deepcopy(before)
        changed['roster_visible'] = int(changed['roster_visible'])
        self.assertEqual(self.call(self.collector(), expected_snapshot=changed), 'cas_claim_mismatch')
        self.assertEqual(self.row(), before)
        def delete():
            self.independent(lambda: Roster.objects.all().delete())
        self.assertEqual(self.call(self.collector(delete), expected_snapshot=before), 'cas_finalize_mismatch')
        self.assertFalse(Roster.objects.exists())

    def test_default_fresh_and_force_compatibility(self):
        Roster.objects.update(status='ready', next_attempt_at=self.now + timedelta(days=1))
        self.assertEqual(self.call(self.collector()), 'fresh')
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.call(self.collector(), force=True), 'ready')

    def test_stop_cleanup_is_full_row_fenced(self):
        before = self.row(); observed = []
        def stop(_event):
            self.independent(lambda: Roster.objects.filter(pk=before['id']).update(failure_count=91))
            observed.append(self.row())
            return False
        with patch.object(service, '_pace', side_effect=stop):
            self.assertEqual(self.call(self.collector(), expected_snapshot=before), 'cas_finalize_mismatch')
        self.assertEqual(self.row(), observed[0])
        self.assertEqual(self.calls, 0)
