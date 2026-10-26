"""Real loopback HTTP + production Inertia parser; no upstream requests."""
import copy
import html
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import requests
from django.test import TestCase, override_settings

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor, WagoDiffUnavailable
from botend.models import WowWagoMonitorState, WowHotfixReport


class WagoPushCheckpointTests(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.enterContext(override_settings(BASE_DIR=self.root))
        self.requests = []
        self.fail_push = None
        self.rows = [dict(id=i + 1, push_id=100, region_id=3, locale='enUS',
                          table_name='SpellEffect', record_id=rid, build=70575,
                          data=[rid, 'original'])
                     for i, rid in enumerate((810, 811, 820, 821))]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                params = parse_qs(urlsplit(self.path).query)
                query, page = params['search'][0], int(params['page'][0])
                owner.requests.append({'path': urlsplit(self.path).path, 'search': query, 'page': page})
                tokens = query.split()
                locale, push = tokens[0], int(tokens[1])
                prefix = tokens[2] if len(tokens) > 2 else ''
                rows = [copy.deepcopy(r) for r in owner.rows if r['push_id'] == push and r['locale'] == locale]
                if len(tokens) > 3:
                    rows = [r for r in rows if str(r['record_id']) == prefix and str(r['build']).startswith(tokens[3])]
                else:
                    rows = [r for r in rows if str(r['record_id']).startswith(prefix)]
                total = len(rows)
                # Deliberately unstable multi-page ordering; real parser must
                # recover via conserved prefix queries, never union snapshots.
                data = rows[:2]
                if push == owner.fail_push:
                    total, data = 1, []
                props = {'filters': {'search': query}, 'hotfixes': {
                    'data': data, 'total': total, 'current_page': page,
                    'last_page': max(1, (total + 1) // 2), 'per_page': 2}}
                body = ('<div id="app" data-page="' + html.escape(json.dumps({'props': props}), quote=True) + '"></div>').encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.session = requests.Session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)
        # Block every non-loopback request, including accidental downstream I/O.
        original = requests.sessions.Session.request
        def guarded(session, method, url, **kwargs):
            if urlsplit(url).hostname != '127.0.0.1':
                raise AssertionError('external HTTP forbidden')
            return original(session, method, url, **kwargs)
        self.enterContext(patch.object(requests.sessions.Session, 'request', guarded))

    def monitor(self):
        monitor = WagoSkillDiffMonitor(None, None)
        def transport(url, **kwargs):
            parts = urlsplit(url)
            self.assertEqual(parts.netloc, 'wago.tools')
            self.assertEqual(parts.path, '/hotfixes')
            response = self.session.get(f'http://127.0.0.1:{self.server.server_port}{parts.path}?{parts.query}', timeout=5)
            response.raise_for_status()
            return response.text
        monitor._http_get_text = transport
        return monitor

    def collect(self, monitor=None, **kwargs):
        return (monitor or self.monitor())._collect_hotfix_interval_rows(
            kwargs.pop('from_push', 99), kwargs.pop('to_push', 100),
            region_id=kwargs.pop('region_id', 3), locale=kwargs.pop('locale', 'enUS'), **kwargs)

    def test_partition_starts_without_three_failed_pagination_sweeps(self):
        self.assertEqual(self.collect(), self.rows)
        self.assertFalse([r for r in self.requests if r['page'] != 1])
        self.assertEqual(sum(r['search'] == 'enUS 100' for r in self.requests), 1)

    def test_interrupted_interval_reuses_only_verified_push_proof(self):
        self.fail_push = 101
        with self.assertRaises(WagoDiffUnavailable):
            self.collect(to_push=101)
        cold = [r for r in self.requests if r['search'].split()[1] == '100']
        files = list((self.root / 'var' / 'wago-hotfix-checkpoints').glob('*.json'))
        self.assertEqual(len(files), 1)
        self.requests.clear()
        self.fail_push = None
        self.assertEqual(self.collect(to_push=101), self.rows)
        warm = [r for r in self.requests if r['search'].split()[1] == '100']
        self.assertLess(len(warm), len(cold))
        self.assertEqual([r['search'] for r in warm], ['enUS 100', 'enUS 100 81', 'enUS 100 82'])
        print('WAGO_LOOPBACK_RESUME', json.dumps({'cold_push_requests': len(cold), 'warm_push_requests': len(warm), 'warm_coordinates': warm}))

    def test_same_total_hidden_payload_change_invalidates_checkpoint(self):
        self.collect()
        self.requests.clear()
        # Root always exposes first two rows, so a root-only fingerprint would
        # incorrectly reuse the old fourth row under the same total/source IDs.
        self.rows[-1]['data'][1] = 'changed outside root page'
        self.assertEqual(self.collect(), self.rows)
        self.assertGreater(len(self.requests), 3)
        self.requests.clear()
        self.assertEqual(self.collect(), self.rows)
        self.assertEqual(len(self.requests), 3)

    def test_new_deleted_and_moved_rows_do_not_reuse_stale_proof(self):
        self.collect()
        for mutate in (
            lambda: self.rows.append(dict(self.rows[-1], id=9, record_id=830)),
            lambda: self.rows.pop(0),
            lambda: self.rows[-1].update(record_id=910),
        ):
            with self.subTest(mutation=mutate):
                mutate()
                self.assertEqual(self.collect(), sorted(self.rows, key=lambda r: r['id']))

    def test_scope_range_build_region_locale_and_source_identity_miss(self):
        from botend.services.wago_push_checkpoint import PushCheckpoints
        self.collect()
        cold_count = len(self.requests)
        self.requests.clear()
        self.collect(to_push=101)
        self.assertEqual(sum(r['search'].split()[1] == '100' for r in self.requests), cold_count)
        for kwargs in ({'build_number': 70575}, {'region_id': 1}, {'locale': 'deDE'}):
            with self.subTest(scope=kwargs):
                self.collect()
                self.requests.clear()
                result = self.collect(**kwargs)
                if kwargs.get('locale') == 'deDE':
                    self.assertEqual(result, [])
                    self.assertEqual(self.requests[0]['search'], 'deDE 100')
                else:
                    self.assertEqual(len(self.requests), cold_count)
                    self.assertEqual(result, self.rows if kwargs.get('build_number') else [])
        self.collect()
        cache = PushCheckpoints(from_push=99, to_push=100, region_id=3, locale='enUS', build_number=None, max_pages=40)
        self.assertIsNotNone(cache.load(100))
        cache.scope['source'] = 'https://different.invalid/hotfixes'
        self.assertIsNone(cache.load(100))

    def test_corrupt_checksum_and_truncated_files_are_cache_misses(self):
        self.collect()
        path = self.root / 'var' / 'wago-hotfix-checkpoints' / '100.json'
        for corruption in ('truncated', 'checksum'):
            with self.subTest(corruption=corruption):
                if corruption == 'truncated':
                    path.write_bytes(b'{"body":')
                else:
                    body = json.loads(path.read_bytes())
                    body['body']['proof']['leaves'][0]['rows'][0]['data'] = ['tampered']
                    path.write_text(json.dumps(body))
                self.requests.clear()
                self.assertEqual(self.collect(), self.rows)
                self.assertEqual(len(self.requests), 13)

    def test_partial_revalidation_never_masks_failed_source_or_updates_checkpoint(self):
        self.collect()
        path = self.root / 'var' / 'wago-hotfix-checkpoints' / '100.json'
        original = path.read_bytes()
        self.fail_push = 100
        with self.assertRaises(WagoDiffUnavailable):
            self.collect()
        self.assertEqual(path.read_bytes(), original)
        self.fail_push = None
        self.assertEqual(self.collect(), self.rows)

    def test_cache_failure_and_budget_limits_do_not_relax_collection(self):
        from botend.services import wago_push_checkpoint as service
        self.collect()
        path = self.root / 'var' / 'wago-hotfix-checkpoints' / '100.json'
        original = path.read_bytes()
        self.rows[-1]['data'][1] = 'new verified payload'
        with patch.object(service, 'atomic_bytes', side_effect=OSError('disk full')):
            self.assertEqual(self.collect(), self.rows)
        self.assertEqual(path.read_bytes(), original)
        with override_settings(WAGO_HOTFIX_MAX_PUSH_SPAN=1):
            before = len(self.requests)
            with self.assertRaisesRegex(WagoDiffUnavailable, 'span limit'):
                self.collect(to_push=101)
            self.assertEqual(len(self.requests), before)
        with override_settings(WAGO_HOTFIX_MAX_ENTRIES=3):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'entry limit'):
                self.collect()

    def test_slot_capacity_replaces_old_scope_and_rejects_links_or_oversize(self):
        from botend.services.wago_push_checkpoint import PushCheckpoints, SLOTS, MAX_BYTES
        cache = PushCheckpoints(from_push=99, to_push=100, region_id=3, locale='enUS', build_number=None, max_pages=40)
        self.collect()
        proof = cache.load(100)
        self.assertTrue(cache.save(100 + SLOTS, proof))
        self.assertIsNone(cache.load(100))
        self.assertIsNotNone(cache.load(100 + SLOTS))
        files = list(cache.root.glob('*.json'))
        self.assertEqual(len(files), 1)
        self.assertFalse(cache.save(100, {'total': 1, 'leaves': ['x' * MAX_BYTES]}))
        self.assertIsNotNone(cache.load(100 + SLOTS))
        pending = cache.root / '.100.json.writing-deadwriter'
        pending.write_bytes(b'partial')
        self.assertTrue(cache.save(100, proof))
        self.assertFalse(pending.exists())
        target = self.root / 'do-not-touch'
        target.write_bytes(b'protected')
        files[0].unlink()
        files[0].symlink_to(target)
        self.assertIsNone(cache.load(100))
        self.assertFalse(cache.save(100, proof))
        self.assertEqual(target.read_bytes(), b'protected')

    def test_real_scan_failure_preserves_orm_cursor_and_last_good_report(self):
        state = WowWagoMonitorState.objects.create(
            branch='wow', locale='enUS', build='12.1.5.70575', hotfix_region_id=3,
            hotfix_push_id=99, hotfix_last_run_status='success')
        good = WowHotfixReport.objects.create(branch='wow', locale='enUS', region_id=3,
                                             from_push=98, to_push=99, collection_complete=True,
                                             source_facts_json='["last-good"]')
        self.fail_push = 101
        monitor = self.monitor()
        with patch.object(monitor, '_fetch_latest_hotfix_push_id', return_value=101):
            self.assertFalse(monitor._scan_hotfix_if_needed(state, 'wow', state.build))
        state.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(state.hotfix_push_id, 99)
        self.assertEqual(state.hotfix_last_run_status, 'failed')
        self.assertTrue(good.collection_complete)
        self.assertEqual(good.source_facts_json, '["last-good"]')
        failed = WowHotfixReport.objects.get(region_id=3, to_push=101)
        self.assertFalse(failed.collection_complete)
        self.assertTrue((self.root / 'static' / failed.content_html_path).exists())
        self.assertEqual(len(list((self.root / 'var' / 'wago-hotfix-checkpoints').glob('*.json'))), 1)

    def test_restarted_scan_publishes_complete_facts_then_advances_real_cursor(self):
        state = WowWagoMonitorState.objects.create(
            branch='wow', locale='enUS', build='12.1.5.70575', hotfix_region_id=3,
            hotfix_push_id=99, hotfix_last_run_status='success')
        # Explicit invalidate rows do not assert decoded values. Real facts and
        # HTML renderers run, with unrelated DB2 enrichment unavailable locally.
        for row in self.rows:
            row.update(status=3, data=None, table_name='CheckpointFixture')
        self.fail_push = 101
        first = self.monitor()
        with patch.object(first, '_fetch_latest_hotfix_push_id', return_value=101):
            self.assertFalse(first._scan_hotfix_if_needed(state, 'wow', state.build))
        self.fail_push = None
        self.requests.clear()
        monitor = self.monitor()
        with override_settings(WAGO_HOTFIX_REPORT_ENRICH_MAX=0), \
             patch.object(monitor, '_fetch_latest_hotfix_push_id', return_value=101), \
             patch.object(monitor, '_fetch_db2_row_by_id', return_value=None), \
             patch.object(monitor, '_load_chr_classes', return_value={}), \
             patch.object(monitor, '_load_chr_specialization_meta', return_value={}):
            self.assertTrue(monitor._scan_hotfix_if_needed(state, 'wow', state.build))
        state.refresh_from_db()
        report = WowHotfixReport.objects.get(region_id=3, to_push=101)
        self.assertTrue(report.collection_complete)
        self.assertEqual(state.hotfix_push_id, 101)
        self.assertEqual(report.entry_count, len(self.rows))
        self.assertEqual([f['source'] for f in json.loads(report.source_facts_json)], self.rows)
        self.assertTrue((self.root / 'static' / report.content_html_path).is_file())
        self.assertEqual(sum(r['search'].split()[1] == '100' for r in self.requests), 3)
