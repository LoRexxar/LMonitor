"""Scheduler Hotfix publication through the real ORM, HTTP parser and renderer."""
import html
import io
import json
import socket
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import requests
from django.core.management import call_command
from django.db import OperationalError, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
from botend.models import WowHotfixReport, WowWagoHotfixEvent, WowWagoMonitorState
from botend.services.wago_hotfix_source import source_ids_sha256
from utils.log import logger


class WagoHotfixSegmentTests(TransactionTestCase):
    BUILD = '12.1.5.70575'
    DISCOVERY = 'enUS % 70575'
    POINTERS = ('hotfix_report_url', 'hotfix_wago_url', 'hotfix_summary_title',
                'hotfix_spell_count', 'hotfix_class_count')

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.enterContext(override_settings(BASE_DIR=self.root, WAGO_HOTFIX_REPORT_ENRICH_MAX=0))
        self.tip = 0
        self.rows = {}
        self.blocked_push = None
        self.requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urlsplit(self.path)
                if parts.path == '/':
                    props = {'versions': [{'product': 'wow', 'version': fixture.BUILD}]}
                elif parts.path == '/hotfixes':
                    params = parse_qs(parts.query)
                    search, page = params.get('search', ['enUS'])[0], int(params['page'][0])
                    fixture.requests.append((search, page))
                    if search == fixture.DISCOVERY:
                        # Discovery and exact-push views can disagree. A row here
                        # supplies an upper bound, not a proof of the interval.
                        rows = [fixture.row(fixture.tip)]
                    elif search == 'enUS':
                        rows = [fixture.row(fixture.tip), fixture.row(fixture.tip - 3)] if page == 1 else []
                    else:
                        push = int(search.split()[1])
                        rows = fixture.rows.get(push, [])
                    echoed = '' if search == f'enUS {fixture.blocked_push}' else search
                    props = {'filters': {'search': echoed}, 'hotfixes': {
                        'data': rows, 'total': len(rows), 'current_page': page,
                        'last_page': 1, 'per_page': 10,
                    }}
                elif parts.path == '/db2/SegmentFixture':
                    props = {}  # No client-table schema for these invalidate rows.
                else:
                    self.send_error(404)
                    return
                body = ('<div id="app" data-page="' + html.escape(
                    json.dumps({'props': props}), quote=True) + '"></div>').encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        connect = socket.socket.connect

        def loopback_only(sock, address):
            if address != self.server.server_address:
                raise AssertionError(f'Non-fixture socket forbidden: {address!r}')
            return connect(sock, address)

        self.enterContext(patch.object(socket.socket, 'connect', loopback_only))
        self.enterContext(patch.object(WagoSkillDiffMonitor, '_load_chr_classes', return_value={}))
        self.enterContext(patch.object(WagoSkillDiffMonitor, '_load_chr_specialization_meta', return_value={}))
        self.enterContext(patch.object(WagoSkillDiffMonitor, '_fetch_hotfix_page_data_via_browser', return_value=[]))
        self.enterContext(patch('botend.services.wow_spell_display.sync_spell_display'))
        # Redirect only the Wago HTTP transport; source validation stays real.
        session_get = requests.Session.get

        def route(session, url, **kwargs):
            parts = urlsplit(url)
            self.assertEqual(parts.netloc, 'wago.tools')
            session.trust_env = False
            return session_get(session, f'http://127.0.0.1:{self.server.server_port}{parts.path}?{parts.query}', **kwargs)

        self.enterContext(patch.object(requests.Session, 'get', route))

    @staticmethod
    def row(push, region_id=3):
        # Explicit invalidate status, so no unrelated external DB2 enrichment.
        return dict(id=push * 10 + region_id, push_id=push, region_id=region_id, locale='enUS',
                    table_name='SegmentFixture', record_id=push, build=70575, status=3, data=None)

    def state(self, cursor):
        return WowWagoMonitorState.objects.create(branch='wow', locale='enUS', build=self.BUILD,
                                                  hotfix_region_id=3, hotfix_push_id=cursor)

    def scan(self):
        self.requests.clear()
        monitor = WagoSkillDiffMonitor(None, None)
        with monitor._http_session:
            return monitor.scan('wow')

    def assert_requests(self, *pushes, discovery=True):
        expected = [self.DISCOVERY] if discovery else []
        expected.extend(f'enUS {push}' for push in pushes)
        self.assertEqual(self.requests, [(search, 1) for search in expected])

    def report(self, start, end, pushes):
        report = WowHotfixReport.objects.get(region_id=3, to_push=end)
        self.assertEqual(report.from_push, start)
        self.assertTrue(report.collection_complete)
        expected = [self.row(push) for push in pushes]
        self.assertEqual(report.entry_count, len(expected))
        self.assertEqual([fact['source'] for fact in json.loads(report.source_facts_json)], expected)
        artifact = (self.root / 'static' / report.content_html_path).read_bytes()
        self.assertIn(f'{start} → {end}'.encode(), artifact)
        event = WowWagoHotfixEvent.objects.get(region_id=3, to_push=end)
        self.assertEqual((event.from_push, event.status, event.report_id), (start, 'report_generated', report.pk))
        return report, artifact

    def pointers(self, state):
        state.refresh_from_db()
        return tuple(getattr(state, field) for field in self.POINTERS)

    def expire_retry(self, end):
        event = WowWagoHotfixEvent.objects.get(region_id=3, to_push=end)
        WowWagoHotfixEvent.objects.filter(pk=event.pk).update(
            updated_at=timezone.now() - WagoSkillDiffMonitor.FAILURE_RETRY_DELAY - timedelta(seconds=1),
        )

    def test_253_push_backlog_publishes_safe_prefix_then_retries_only_blocked_segment(self):
        state = self.state(113713)
        self.tip = 113966
        self.blocked_push = 113845
        self.rows = {push: [self.row(push)] for push in range(113714, self.tip + 1) if push != self.blocked_push}
        self.assertTrue(self.scan())
        self.assert_requests(*range(113714, 113778))
        self.report(113713, 113777, range(113714, 113778))
        self.assertTrue(self.scan())
        self.assert_requests(*range(113778, 113842), discovery=False)
        good, good_html = self.report(113777, 113841, range(113778, 113842))
        pointers = self.pointers(state)
        self.assertEqual(state.hotfix_push_id, 113841)

        self.assertFalse(self.scan())
        self.assert_requests(*range(113842, 113846), discovery=False)
        self.assertEqual(self.pointers(state), pointers)
        self.assertEqual(state.hotfix_push_id, 113841)
        event = WowWagoHotfixEvent.objects.get(region_id=3, to_push=113905)
        self.assertEqual(event.from_push, 113841)
        self.assertIn(event.status, WagoSkillDiffMonitor.RETRYABLE_FAILURES)
        self.assertIn('113845', event.error_message)
        self.assertFalse(WowHotfixReport.objects.filter(to_push__gt=113841, collection_complete=True).exists())

        self.rows[self.blocked_push] = [self.row(self.blocked_push)]
        self.blocked_push = None
        self.tip += 1
        self.assertFalse(self.scan())  # Same segment's retry deadline, despite a new observed tip.
        self.assert_requests(discovery=False)
        event.refresh_from_db()
        self.assertEqual(event.status, 'generate_failed_fallback_report')
        self.expire_retry(113905)
        self.assertTrue(self.scan())
        self.assert_requests(*range(113842, 113906), discovery=False)
        self.report(113841, 113905, range(113842, 113906))
        state.refresh_from_db()
        self.assertEqual(state.hotfix_push_id, 113905)
        self.assertEqual((self.root / 'static' / good.content_html_path).read_bytes(), good_html)

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=7, WAGO_HOTFIX_MAX_PUSH_SPAN=2)
    def test_verified_empty_intermediate_advances_without_replacing_report(self):
        state = self.state(97)
        self.tip = 103
        self.rows = {98: [self.row(98)], 100: [self.row(100, region_id=1)], 103: [self.row(103)]}
        self.assertTrue(self.scan())
        self.assert_requests(98, 99)
        good, artifact = self.report(97, 99, (98,))
        pointers = self.pointers(state)

        self.assertTrue(self.scan())
        self.assert_requests(100, 101, discovery=False)
        self.assertEqual(self.pointers(state), pointers)
        self.assertEqual((state.hotfix_push_id, state.hotfix_last_event_status), (101, 'verified_empty'))
        event = WowWagoHotfixEvent.objects.get(region_id=3, to_push=101)
        self.assertEqual((event.from_push, event.status, event.entry_count, event.report_id),
                         (99, 'verified_empty', 0, None))
        self.assertFalse(WowHotfixReport.objects.filter(to_push=101).exists())
        self.assertEqual((self.root / 'static' / good.content_html_path).read_bytes(), artifact)

        self.assertTrue(self.scan())
        self.assert_requests(102, 103, discovery=False)
        self.report(101, 103, (103,))

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=1)
    def test_empty_observed_tip_init_and_backfill_are_refused(self):
        state = self.state(0)
        self.tip = 102
        self.assertFalse(self.scan())
        # The legacy initializer observes a prior push and must not clamp.
        self.assertEqual(self.requests, [(self.DISCOVERY, 1), ('enUS', 1), ('enUS', 2),
                                        ('enUS 100', 1), ('enUS 101', 1), ('enUS 102', 1)])
        state.refresh_from_db()
        self.assertEqual(state.hotfix_push_id, 0)
        event = WowWagoHotfixEvent.objects.get(to_push=102)
        self.assertEqual(event.from_push, 99)
        self.assertIn('observed push 102', event.error_message)

        state.hotfix_push_id = 101
        state.save(update_fields=['hotfix_push_id'])
        self.expire_retry(102)
        self.assertFalse(self.scan())
        self.assert_requests(102, discovery=False)
        state.refresh_from_db()
        self.assertEqual(state.hotfix_push_id, 101)
        self.assertFalse(WowHotfixReport.objects.filter(collection_complete=True).exists())
        self.assertFalse(WowWagoHotfixEvent.objects.filter(status='verified_empty').exists())

        self.requests.clear()
        monitor = WagoSkillDiffMonitor(None, None)
        with monitor._http_session:
            self.assertFalse(monitor._scan_hotfix_if_needed(state, 'wow', self.BUILD, backfill_interval=(101, 104)))
        self.assert_requests(102, 103, 104, discovery=False)
        event = WowWagoHotfixEvent.objects.get(to_push=104)
        self.assertEqual((event.from_push, event.status, event.report_id), (101, 'generate_failed', None))
        state.refresh_from_db()
        self.assertEqual(state.hotfix_push_id, 101)

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=1, WAGO_HOTFIX_MAX_PUSH_SPAN=4)
    def test_manual_backfill_collects_exact_interval_without_discovery_or_live_state_write(self):
        state = self.state(110)
        expected = [self.row(102), self.row(105)]
        self.rows = {row['push_id']: [row] for row in expected}
        before = WowWagoMonitorState.objects.values().get(pk=state.pk)
        call_command('backfill_wow_hotfix_interval', from_push=101, to_push=105, region_id=3,
                     expected_count=len(expected), expected_id_sha256=source_ids_sha256(expected), stdout=io.StringIO())
        self.assert_requests(102, 103, 104, 105, discovery=False)
        self.report(101, 105, (102, 105))
        self.assertEqual(WowWagoMonitorState.objects.values().get(pk=state.pk), before)

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=2)
    def test_complete_endpoint_collision_does_not_reuse_or_overwrite_report(self):
        state = self.state(98)
        self.tip = 102
        self.rows = {99: [self.row(99)]}
        good = WowHotfixReport.objects.create(region_id=3, from_push=97, to_push=100,
            collection_complete=True, source_facts_json='["frozen"]',
            content_html_path='portal/reports/frozen.html')
        path = self.root / 'static' / good.content_html_path
        path.parent.mkdir(parents=True)
        path.write_bytes(b'complete artifact')
        event = WowWagoHotfixEvent.objects.create(region_id=3, from_push=97, to_push=100,
                                                status='report_generated', report=good)
        before = WowHotfixReport.objects.values().get(pk=good.pk)
        event_before = WowWagoHotfixEvent.objects.values().get(pk=event.pk)

        self.assertTrue(self.scan())
        self.assert_requests(99)
        state.refresh_from_db()
        self.assertEqual(state.hotfix_push_id, 99)
        self.report(98, 99, (99,))
        self.rows[101] = [self.row(101)]
        self.assertTrue(self.scan())
        self.assert_requests(100, 101, discovery=False)
        self.report(99, 101, (101,))
        self.assertEqual(WowHotfixReport.objects.values().get(pk=good.pk), before)
        self.assertEqual(WowWagoHotfixEvent.objects.values().get(pk=event.pk), event_before)
        self.assertEqual(path.read_bytes(), b'complete artifact')

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=2)
    def test_all_bounded_endpoints_occupied_leaves_cursor_and_reports_untouched(self):
        state = self.state(98)
        self.tip = 102
        for push in (99, 100):
            WowHotfixReport.objects.create(region_id=3, from_push=97, to_push=push, collection_complete=True)
        before = list(WowHotfixReport.objects.order_by('to_push').values())
        with self.assertLogs(logger, level='WARNING') as logs:
            self.assertFalse(self.scan())
        self.assertTrue(any('no free endpoint in (98, 100]' in msg for msg in logs.output), logs.output)
        self.assert_requests()
        state.refresh_from_db()
        self.assertEqual((state.hotfix_push_id, state.hotfix_last_event_status), (98, 'complete_report_preserved'))
        self.assertEqual(list(WowHotfixReport.objects.order_by('to_push').values()), before)
        self.assertFalse(WowWagoHotfixEvent.objects.exists())

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=2)
    def test_moving_tip_cannot_hide_observation_beyond_first_segment(self):
        state = self.state(99)
        self.tip = 102
        self.rows = {101: [self.row(101)]}
        self.assertTrue(self.scan())
        self.assert_requests(100, 101)
        self.report(99, 101, (101,))
        self.tip = 105
        self.rows[103] = [self.row(103)]
        self.rows[105] = [self.row(105)]

        self.assertFalse(self.scan())
        self.assert_requests(102, discovery=False)
        state.refresh_from_db()
        self.assertEqual(state.hotfix_push_id, 101)
        self.assertIn('observed push 102', WowWagoHotfixEvent.objects.get(to_push=102).error_message)
        self.assertFalse(WowHotfixReport.objects.filter(to_push__gt=101, collection_complete=True).exists())
        self.expire_retry(102)
        self.rows[102] = [self.row(102)]
        self.assertTrue(self.scan())
        self.assert_requests(102, discovery=False)
        self.report(101, 102, (102,))

        self.assertTrue(self.scan())
        self.assert_requests(103, 104)
        self.report(102, 104, (103,))
        self.assertTrue(self.scan())
        self.assert_requests(105, discovery=False)
        self.report(104, 105, (105,))

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=2)
    def test_nonempty_interval_missing_observed_endpoint_is_not_complete(self):
        state = self.state(99)
        self.tip = 101
        self.rows[100] = [self.row(100)]
        self.assertFalse(self.scan())
        self.assert_requests(100, 101)
        state.refresh_from_db()
        self.assertEqual(state.hotfix_push_id, 99)
        self.assertIn('observed push 101', WowWagoHotfixEvent.objects.get(to_push=101).error_message)
        self.assertFalse(WowHotfixReport.objects.filter(collection_complete=True).exists())

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=5)
    def test_moving_tip_keeps_exact_retry_and_original_discovery_evidence(self):
        state = self.state(99)
        self.tip = 101
        self.assertFalse(self.scan())
        self.assert_requests(100, 101)
        self.tip = 105
        self.rows[self.tip] = [self.row(self.tip)]
        self.assertFalse(self.scan())
        self.assert_requests(discovery=False)

        self.expire_retry(101)
        self.assertFalse(self.scan())  # 101 was itself observed; empty is not safe.
        self.assert_requests(100, 101, discovery=False)
        state.refresh_from_db()
        self.assertEqual(state.hotfix_push_id, 99)
        self.assertFalse(WowWagoHotfixEvent.objects.filter(status='verified_empty').exists())

        self.rows[101] = [self.row(101)]
        self.expire_retry(101)
        self.assertTrue(self.scan())
        self.assert_requests(100, 101, discovery=False)
        self.report(99, 101, (101,))

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=5)
    def test_cursor_write_failure_rolls_back_complete_flag_and_allows_retry(self):
        state = self.state(99)
        self.tip = 101
        self.rows[101] = [self.row(101)]
        save = WowWagoMonitorState.save

        def fail_cursor_save(instance, **kwargs):
            if 'hotfix_push_id' in kwargs.get('update_fields', []):
                raise RuntimeError('injected cursor write failure')
            return save(instance, **kwargs)

        with patch.object(WowWagoMonitorState, 'save', fail_cursor_save), \
             patch.object(WowHotfixReport, 'refresh_from_db', side_effect=OperationalError('readback unavailable')) as read:
            self.assertFalse(self.scan())
        read.assert_not_called()
        row = WowHotfixReport.objects.get(to_push=101)
        state.refresh_from_db()
        self.assertFalse(row.collection_complete)
        self.assertFalse((self.root / 'static' / row.content_html_path).exists())
        self.assertEqual(state.hotfix_push_id, 99)
        event = WowWagoHotfixEvent.objects.get(to_push=101)
        self.assertEqual((event.status, event.report_id), ('publish_failed', None))
        self.assertIn('injected cursor write failure', event.error_message)

        self.tip = 105
        self.rows[self.tip] = [self.row(self.tip)]
        self.expire_retry(101)
        self.assertTrue(self.scan())
        self.assert_requests(100, 101, discovery=False)
        state.refresh_from_db()
        self.assertEqual(state.hotfix_push_id, 101)
        self.assertEqual(WowHotfixReport.objects.count(), 1)
        self.report(99, 101, (101,))

    @contextmanager
    def lost_commit_ack(self, *, committed=True):
        commit = connection.commit
        save = WowWagoMonitorState.save
        armed = False

        def arm_after_cursor_write(instance, **kwargs):
            nonlocal armed
            result = save(instance, **kwargs)
            if 'hotfix_push_id' in kwargs.get('update_fields', ()):
                armed = True
            return result

        def fail_commit():
            nonlocal armed
            if not armed:
                return commit()
            armed = False
            if committed:
                commit()
            raise OperationalError('injected lost commit acknowledgement')

        with patch.object(connection, 'commit', side_effect=fail_commit), \
             patch.object(WowWagoMonitorState, 'save', arm_after_cursor_write):
            yield
        self.assertFalse(armed, 'fault must run at a real transaction boundary')

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=2)
    def test_report_unknown_commit_preserves_files_and_never_relabels_attempt(self):
        state = self.state(99)
        self.tip = 101
        self.rows[101] = [self.row(101)]
        with self.lost_commit_ack(), \
             patch.object(WowHotfixReport, 'refresh_from_db', side_effect=OperationalError('readback unavailable')):
            self.assertFalse(self.scan())
        state.refresh_from_db()
        self.assertEqual(state.hotfix_push_id, 101)
        report, artifact = self.report(99, 101, (101,))
        self.assertTrue(self.scan())
        self.assertEqual((self.root / 'static' / report.content_html_path).read_bytes(), artifact)

    def assert_empty_commit_error(self, *, committed, fail_readback=False):
        state = self.state(99)
        self.tip = 103
        read = WowWagoHotfixEvent.refresh_from_db

        def event_read(instance, **kwargs):
            if fail_readback:
                raise OperationalError('event readback unavailable')
            return read(instance, **kwargs)

        with self.lost_commit_ack(committed=committed), \
             patch.object(WowWagoHotfixEvent, 'refresh_from_db', autospec=True, side_effect=event_read) as check:
            self.assertEqual(self.scan(), committed and not fail_readback)
        self.assert_requests(100, 101)
        state.refresh_from_db()
        event = WowWagoHotfixEvent.objects.get(to_push=101)
        self.assertFalse(WowHotfixReport.objects.exists())
        self.assertFalse(list((self.root / 'static').rglob('*.html')))
        check.assert_called_once()
        if committed:
            self.assertEqual((state.hotfix_push_id, event.status, event.report_id), (101, 'verified_empty', None))
        else:
            self.assertEqual((state.hotfix_push_id, event.status, event.report_id), (99, 'publish_failed', None))
            self.expire_retry(101)
            self.assertTrue(self.scan())
            self.assert_requests(100, 101, discovery=False)

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=2)
    def test_empty_commit_proven_rolled_back_does_not_create_fallback(self):
        self.assert_empty_commit_error(committed=False)

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=2)
    def test_empty_commit_proven_committed_does_not_create_fallback(self):
        self.assert_empty_commit_error(committed=True)

    @override_settings(WAGO_HOTFIX_AUTO_PUSH_SPAN=2)
    def test_empty_commit_with_failed_readback_does_not_create_fallback(self):
        self.assert_empty_commit_error(committed=True, fail_readback=True)
